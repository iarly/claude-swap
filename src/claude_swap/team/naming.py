"""Names, validation and payloads for team secrets in Key Vault.

Key Vault secret names allow only ``[0-9a-zA-Z-]`` (1-127 chars), so the
manifest is ``cswap-<team>`` and an account is ``cswap-<team>--<slug>``. Team
names never contain ``--``, which keeps the separator unambiguous.
"""

from __future__ import annotations

import hashlib
import json
import re

from claude_swap.exceptions import TeamError, ValidationError

SECRET_PREFIX = "cswap-"
ACCOUNT_SEPARATOR = "--"
MAX_SECRET_NAME = 127
SCHEMA = 1

TAG_KIND = "cswap-kind"
TAG_TEAM = "cswap-team"
TAG_EMAIL = "cswap-email"
KIND_MANIFEST = "manifest"
KIND_ACCOUNT = "account"
ACCOUNT_CONTENT_TYPE = "application/vnd.cswap.account+json"
MANIFEST_CONTENT_TYPE = "application/vnd.cswap.manifest+json"

DEFAULT_TABLE = "cswappresence"

_TEAM_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
_VAULT_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]$")
_TABLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{2,62}$")
_ROW_KEY_FORBIDDEN = re.compile(r"[/\\#?\x00-\x1f\x7f-\x9f]")


def validate_team_name(name: str) -> str:
    """Return ``name`` lower-cased, or raise when it can't name a team."""
    team = (name or "").strip().lower()
    if not _TEAM_RE.match(team) or ACCOUNT_SEPARATOR in team:
        raise ValidationError(
            f"Invalid team name: {name!r} (use 1-32 lowercase letters, digits "
            "and single hyphens, starting and ending with a letter or digit)"
        )
    return team


def validate_table_name(name: str) -> str:
    if not _TABLE_RE.match(name or ""):
        raise ValidationError(
            f"Invalid table name: {name!r} (3-63 letters and digits, starting with a letter)"
        )
    return name


def vault_url(name_or_url: str) -> str:
    """Normalize a vault name or URL to ``https://<name>.vault.azure.net/``.

    A full ``https://`` URL is kept as given (sovereign clouds use other
    domains), with exactly one trailing slash.
    """
    value = (name_or_url or "").strip()
    if value.lower().startswith("https://"):
        host = value[len("https://"):].split("/", 1)[0]
        if not host:
            raise ValidationError(f"Invalid Key Vault URL: {name_or_url!r}")
        return f"https://{host}/"
    if value.lower().startswith("http://"):
        raise ValidationError("The Key Vault URL must use https://")
    if not _VAULT_NAME_RE.match(value):
        raise ValidationError(
            f"Invalid Key Vault name: {name_or_url!r} (3-24 letters, digits and "
            "hyphens, starting with a letter)"
        )
    return f"https://{value.lower()}.vault.azure.net/"


def table_endpoint(url: str) -> str:
    value = (url or "").strip()
    if not value.lower().startswith("https://"):
        raise ValidationError(
            f"Invalid Table Storage endpoint: {url!r} "
            "(expected https://<account>.table.core.windows.net)"
        )
    host = value[len("https://"):].split("/", 1)[0]
    if not host:
        raise ValidationError(f"Invalid Table Storage endpoint: {url!r}")
    return f"https://{host}"


def manifest_secret_name(team: str) -> str:
    return f"{SECRET_PREFIX}{team}"


def account_secret_name(team: str, email: str) -> str:
    """``cswap-<team>--<email slug>-<6 hex>``, at most 127 characters.

    The hash makes the name unique even when two emails slug the same way
    (``a.b@x`` and ``a-b@x``) or the slug is cut to fit.
    """
    normalized = email.strip().lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:6]
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-") or "account"
    head = f"{SECRET_PREFIX}{team}{ACCOUNT_SEPARATOR}"
    room = MAX_SECRET_NAME - len(head) - len(digest) - 1
    slug = slug[:room].rstrip("-") or "account"
    return f"{head}{slug}-{digest}"


def manifest_tags(team: str) -> dict[str, str]:
    return {TAG_KIND: KIND_MANIFEST, TAG_TEAM: team}


def account_tags(team: str, email: str) -> dict[str, str]:
    return {TAG_KIND: KIND_ACCOUNT, TAG_TEAM: team, TAG_EMAIL: email.strip().lower()}


def build_manifest(team: str, endpoint: str, table: str, created_by: str, created_at: str) -> str:
    return json.dumps({
        "schema": SCHEMA,
        "team": team,
        "storage": {"tableEndpoint": endpoint, "table": table},
        "createdBy": created_by,
        "createdAt": created_at,
    })


def parse_manifest(team: str, value: str) -> dict:
    """The manifest's ``{"tableEndpoint", "table"}``, or raise on a bad payload."""
    try:
        data = json.loads(value)
        storage = data["storage"]
        endpoint = table_endpoint(storage["tableEndpoint"])
        table = validate_table_name(storage["table"])
    except (ValueError, KeyError, TypeError, ValidationError) as e:
        raise TeamError(f"The manifest of team '{team}' is malformed: {e}") from None
    if data.get("team") != team:
        raise TeamError(f"The manifest in the vault belongs to team {data.get('team')!r}, not '{team}'")
    return {"tableEndpoint": endpoint, "table": table}


def build_account_payload(email: str, kind: str, token: str) -> str:
    return json.dumps({"schema": SCHEMA, "email": email.strip().lower(), "kind": kind, "token": token})


def parse_account_payload(value: str) -> dict:
    """``{"email", "kind", "token"}`` from an account secret, or raise TeamError."""
    try:
        data = json.loads(value)
        email = str(data["email"]).strip().lower()
        kind = data["kind"]
        token = str(data["token"]).strip()
    except (ValueError, KeyError, TypeError) as e:
        raise TeamError(f"Malformed team account secret: {e}") from None
    if kind not in ("setup_token", "api_key") or not token or "@" not in email:
        raise TeamError("Malformed team account secret: bad kind, token or email")
    return {"email": email, "kind": kind, "token": token}


def presence_row_key(user: str, machine: str) -> str:
    """``<user>|<machine>`` with the characters Table Storage forbids replaced."""
    return _ROW_KEY_FORBIDDEN.sub("_", f"{user.lower()}|{machine.lower()}")[:512]


def short_user(upn: str) -> str:
    """The part of a UPN before ``@`` — what the watch shows per person."""
    return (upn or "?").split("@", 1)[0]
