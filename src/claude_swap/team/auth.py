"""Entra ID sign-in for the team feature.

``cswap login az`` is the only place that may open a browser (or print a
device code). It stores an ``AuthenticationRecord`` (account and tenant ids,
no secret) in ``<backup_dir>/azure/auth_record.json``. The tokens live in
MSAL's persistent cache named ``claude-swap`` (DPAPI on Windows, Keychain on
macOS, libsecret on Linux). Every other path builds the credential with
``disable_automatic_authentication=True``: a missing or expired sign-in raises
``TeamAuthRequired`` instead of prompting, so ``auto`` and the watch never pop
windows.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path

from claude_swap.exceptions import AzureExtraMissing, TeamAuthRequired, TeamError
from claude_swap.fsutil import atomic_write_text

CACHE_NAME = "claude-swap"
VAULT_SCOPE = "https://vault.azure.net/.default"
STORAGE_SCOPE = "https://storage.azure.com/.default"
_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

LOGIN_HINT = "run 'cswap login az' to sign in to Entra ID"


def require_azure():
    """Import ``azure.identity``, or raise with the install instruction."""
    try:
        import azure.identity as identity  # noqa: PLC0415
        # The SDK logs failed token requests at WARNING; with no handler of its
        # own that reaches stderr through logging.lastResort, next to cswap's
        # own message. A NullHandler keeps them off the terminal (they still
        # propagate to any handler cswap configured).
        _azure_logger = logging.getLogger("azure")
        if not _azure_logger.handlers:
            _azure_logger.addHandler(logging.NullHandler())
    except ImportError:
        raise AzureExtraMissing(
            "Team features need the Azure SDK: install 'claude-swap[azure]' "
            "(e.g. 'uv tool install \"claude-swap[azure]\"')"
        ) from None
    return identity


def record_path(backup_dir: Path) -> Path:
    return Path(backup_dir) / "azure" / "auth_record.json"


def _read_state(backup_dir: Path) -> dict | None:
    p = record_path(backup_dir)
    if not p.exists():
        return None
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) and state.get("record") else None


def signed_in_user(backup_dir: Path) -> str | None:
    """The UPN recorded at sign-in, without touching the network or the SDK."""
    state = _read_state(backup_dir)
    return state.get("username") if state else None


def _cache_options(identity, allow_unencrypted: bool):
    return identity.TokenCachePersistenceOptions(
        name=CACHE_NAME, allow_unencrypted_storage=allow_unencrypted
    )


class _CaeCredential:
    """Ask every token with ``enable_cae=True``.

    azure-identity keeps CAE and non-CAE tokens in separate persistent caches
    (``claude-swap.cae`` / ``claude-swap.nocae``), with separate refresh tokens.
    The Key Vault SDK always asks for CAE, the Tables SDK never does, so a single
    sign-in would fill only one of them and the other client would find no
    token. Login signs in with CAE and this wrapper keeps every request on that
    cache; CAE only adds a client capability, which Storage ignores.
    """

    def __init__(self, inner):
        self._inner = inner

    def get_token(self, *scopes, **kwargs):
        kwargs["enable_cae"] = True
        return self._inner.get_token(*scopes, **kwargs)

    def get_token_info(self, *scopes, options=None):
        options = dict(options or {})
        options["enable_cae"] = True
        return self._inner.get_token_info(*scopes, options=options)

    def close(self):
        close = getattr(self._inner, "close", None)
        if close:
            close()


def credential(backup_dir: Path):
    """A silent credential for the signed-in user; raises TeamAuthRequired otherwise."""
    state = _read_state(backup_dir)
    if state is None:
        raise TeamAuthRequired(f"Not signed in to Entra ID: {LOGIN_HINT}")
    identity = require_azure()
    try:
        record = identity.AuthenticationRecord.deserialize(state["record"])
        return _CaeCredential(identity.InteractiveBrowserCredential(
            tenant_id=record.tenant_id,
            authentication_record=record,
            cache_persistence_options=_cache_options(
                identity, bool(state.get("allowUnencryptedCache"))
            ),
            disable_automatic_authentication=True,
        ))
    except ValueError as e:
        raise TeamError(_cache_error(e)) from None


def _cache_error(e: Exception) -> str:
    return (
        f"The Entra ID token cache is unavailable ({e}). On Linux it needs "
        "libsecret; to store it unencrypted instead, sign in again with "
        "'cswap login az --allow-unencrypted-cache'"
    )


def login(
    backup_dir: Path,
    *,
    tenant_id: str,
    device_code: bool = False,
    allow_unencrypted: bool = False,
) -> str:
    """Interactive sign-in. Returns the signed-in UPN and stores the record."""
    identity = require_azure()
    try:
        options = _cache_options(identity, allow_unencrypted)
        if device_code:
            cred = identity.DeviceCodeCredential(
                tenant_id=tenant_id, cache_persistence_options=options
            )
        else:
            cred = identity.InteractiveBrowserCredential(
                tenant_id=tenant_id, cache_persistence_options=options
            )
        # CAE: the cache every later request reads (see _CaeCredential).
        record = cred.authenticate(scopes=[VAULT_SCOPE], enable_cae=True)
    except ValueError as e:
        raise TeamError(_cache_error(e)) from None
    except Exception as e:  # azure.core ClientAuthenticationError and friends
        raise TeamError(f"Entra ID sign-in failed: {e}") from None
    p = record_path(backup_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        p,
        json.dumps({
            "record": record.serialize(),
            "username": record.username,
            "tenantId": record.tenant_id,
            "allowUnencryptedCache": allow_unencrypted,
        }),
    )
    return record.username


def logout(backup_dir: Path) -> bool:
    """Forget the sign-in record. Returns False when there was none."""
    p = record_path(backup_dir)
    if not p.exists():
        return False
    p.unlink()
    return True


def discover_tenant(subscription_id: str, timeout: float = 10.0) -> str:
    """The tenant that owns ``subscription_id``, without signing in.

    Azure Resource Manager answers an unauthenticated request with 401 and a
    ``WWW-Authenticate: Bearer authorization_uri="https://login.windows.net/<tenant>"``
    header naming the subscription's tenant; the Azure CLI uses the same
    response.
    """
    if not re.fullmatch(_GUID, subscription_id or ""):
        raise TeamError(f"Invalid subscription id: {subscription_id!r} (expected a GUID)")
    url = f"https://management.azure.com/subscriptions/{subscription_id}?api-version=2022-12-01"
    req = urllib.request.Request(url, headers={"User-Agent": "claude-swap"})
    try:
        urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        header = e.headers.get("WWW-Authenticate", "") if e.headers else ""
        match = re.search(r'authorization_uri="https://[^/"]+/(' + _GUID + r')"', header)
        if match:
            return match.group(1)
        raise TeamError(
            f"Could not find the tenant of subscription {subscription_id} "
            f"(HTTP {e.code}); pass --tenant-id"
        ) from None
    except (urllib.error.URLError, OSError) as e:
        raise TeamError(f"Could not reach Azure Resource Manager: {e}; pass --tenant-id") from None
    raise TeamError(f"Could not find the tenant of subscription {subscription_id}; pass --tenant-id")
