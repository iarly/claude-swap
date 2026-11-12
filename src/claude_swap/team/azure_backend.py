"""The Azure implementation of the team stores (Key Vault + Table Storage).

The only module that imports the Azure SDK beyond ``auth``. SDK exceptions
are translated into ``TeamError``/``TeamAuthRequired`` at this boundary.
Background paths (the switch hook, the watch, auto-switch) ask for ``fast``
clients: azure-core's defaults retry with backoff for minutes, which would
stall a switch on a dead network.
"""

from __future__ import annotations

import functools
import json
import time
from datetime import datetime, timezone

from claude_swap.exceptions import TeamAuthRequired, TeamError
from claude_swap.paths import get_backup_root
from claude_swap.team import auth
from claude_swap.team.stores import PresenceRow, SecretInfo, UsageRow

_FAST = {"retry_total": 1, "connection_timeout": 3, "read_timeout": 5}
_NORMAL = {"retry_total": 3, "connection_timeout": 10, "read_timeout": 30}


def _translate(what: str):
    """Decorator: map SDK errors raised by a store method to cswap errors."""

    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            from azure.core import exceptions as az  # noqa: PLC0415
            from azure.identity import AuthenticationRequiredError  # noqa: PLC0415

            try:
                return fn(*args, **kwargs)
            except TeamError:
                raise
            except AuthenticationRequiredError:
                raise TeamAuthRequired(f"Entra ID sign-in expired: {auth.LOGIN_HINT}") from None
            except az.ClientAuthenticationError as e:
                raise TeamAuthRequired(f"Entra ID refused the sign-in ({e}): {auth.LOGIN_HINT}") from None
            except az.HttpResponseError as e:
                if e.status_code == 403:
                    raise TeamError(
                        f"Access denied to {what}. Ask an admin for the role described in docs/team.md"
                    ) from None
                raise TeamError(f"{what} failed: HTTP {e.status_code} {e.reason or ''}".rstrip()) from None
            except (az.ServiceRequestError, az.ServiceResponseError) as e:
                raise TeamError(f"Could not reach {what}: {e}") from None
            except az.AzureError as e:
                raise TeamError(f"{what} failed: {e}") from None

        return inner

    return wrap


def _revision(props) -> str:
    updated = getattr(props, "updated_on", None)
    if isinstance(updated, datetime):
        return updated.astimezone(timezone.utc).isoformat()
    return getattr(props, "version", None) or ""


class AzureSecretStore:
    def __init__(self, vault_url: str, credential, transport: dict):
        from azure.keyvault.secrets import SecretClient  # noqa: PLC0415

        self._what = f"Key Vault {vault_url}"
        self._client = SecretClient(vault_url=vault_url, credential=credential, **transport)

    @property
    def what(self) -> str:
        return self._what

    def list_secrets(self) -> list[SecretInfo]:
        return _translate_call(self._what, lambda: [
            SecretInfo(name=p.name, revision=_revision(p), tags=dict(p.tags or {}))
            for p in self._client.list_properties_of_secrets()
            if p.enabled is not False
        ])

    def get_secret(self, name: str) -> tuple[str, str] | None:
        from azure.core.exceptions import ResourceNotFoundError  # noqa: PLC0415

        def call():
            try:
                secret = self._client.get_secret(name)
            except ResourceNotFoundError:
                return None
            return secret.value or "", _revision(secret.properties)

        return _translate_call(self._what, call)

    def set_secret(self, name: str, value: str, *, tags: dict[str, str], content_type: str) -> str:
        from azure.core.exceptions import ResourceExistsError  # noqa: PLC0415

        def call():
            try:
                secret = self._client.set_secret(name, value, tags=tags, content_type=content_type)
            except ResourceExistsError:
                # The name is soft-deleted (a removed account added back):
                # recover it, then write the new value as a new version.
                self._client.begin_recover_deleted_secret(name).wait()
                secret = self._client.set_secret(name, value, tags=tags, content_type=content_type)
            return _revision(secret.properties)

        return _translate_call(self._what, call)

    def delete_secret(self, name: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError  # noqa: PLC0415

        def call():
            try:
                self._client.begin_delete_secret(name)
            except ResourceNotFoundError:
                pass

        _translate_call(self._what, call)


class AzurePresenceStore:
    def __init__(self, endpoint: str, table: str, credential, transport: dict):
        from azure.data.tables import TableClient, TableServiceClient  # noqa: PLC0415

        self._what = f"Table Storage {endpoint} (table {table})"
        self._table = table
        self._service = TableServiceClient(endpoint=endpoint, credential=credential, **transport)
        self._client = TableClient(endpoint=endpoint, table_name=table, credential=credential, **transport)

    def ensure_table(self) -> None:
        _translate_call(self._what, lambda: self._service.create_table_if_not_exists(self._table))

    def upsert(self, row: PresenceRow) -> None:
        from azure.data.tables import UpdateMode  # noqa: PLC0415

        from claude_swap.team.naming import presence_row_key  # noqa: PLC0415

        entity = {
            "PartitionKey": row.team,
            "RowKey": presence_row_key(row.user, row.machine),
            "user": row.user,
            "machine": row.machine,
            "accountEmail": row.account_email,
            "since": float(row.since),
            "lastSeen": float(row.last_seen),
        }
        _translate_call(self._what, lambda: self._client.upsert_entity(entity, mode=UpdateMode.REPLACE))

    def list(self, team: str) -> list[PresenceRow]:
        def call():
            rows = []
            for e in self._client.query_entities("PartitionKey eq @pk", parameters={"pk": team}):
                rows.append(PresenceRow(
                    team=team,
                    user=str(e.get("user", "")),
                    machine=str(e.get("machine", "")),
                    account_email=str(e.get("accountEmail", "") or ""),
                    since=float(e.get("since", 0) or 0),
                    last_seen=float(e.get("lastSeen", 0) or 0),
                ))
            return rows

        return _translate_call(self._what, call)

    def upsert_usage(self, row: UsageRow) -> None:
        from azure.data.tables import UpdateMode  # noqa: PLC0415

        from claude_swap.team.naming import usage_partition, usage_row_key  # noqa: PLC0415

        entity = {
            "PartitionKey": usage_partition(row.team),
            "RowKey": usage_row_key(row.email),
            "email": row.email,
            "usage": json.dumps(row.usage),
            "ageAtWrite": float(row.age_at_write),
            "publisher": row.publisher,
        }
        _translate_call(self._what, lambda: self._client.upsert_entity(entity, mode=UpdateMode.REPLACE))

    def list_usage(self, team: str) -> list[UsageRow]:
        from claude_swap.team.naming import usage_partition  # noqa: PLC0415

        def call():
            rows = []
            pk = usage_partition(team)
            for e in self._client.query_entities("PartitionKey eq @pk", parameters={"pk": pk}):
                try:
                    usage = json.loads(e.get("usage") or "")
                except ValueError:
                    continue
                # The service's own write time: the reader's clock is compared
                # with Azure's, never with the publisher's.
                stamp = (getattr(e, "metadata", None) or {}).get("timestamp")
                written_at = stamp.timestamp() if stamp is not None else time.time()
                rows.append(UsageRow(
                    team=team,
                    email=str(e.get("email", "") or "").lower(),
                    usage=usage if isinstance(usage, dict) else {},
                    age_at_write=float(e.get("ageAtWrite", 0) or 0),
                    written_at=written_at,
                    publisher=str(e.get("publisher", "") or ""),
                ))
            return rows

        return _translate_call(self._what, call)


def _translate_call(what: str, fn):
    return _translate(what)(fn)()


class AzureBackend:
    def __init__(self, backup_dir=None):
        auth.require_azure()
        self._backup_dir = backup_dir or get_backup_root()
        self._credential = None

    def _cred(self):
        if self._credential is None:
            self._credential = auth.credential(self._backup_dir)
        return self._credential

    def secret_store(self, vault_url: str, *, fast: bool = False) -> AzureSecretStore:
        return AzureSecretStore(vault_url, self._cred(), dict(_FAST if fast else _NORMAL))

    def presence_store(self, endpoint: str, table: str, *, fast: bool = False) -> AzurePresenceStore:
        return AzurePresenceStore(endpoint, table, self._cred(), dict(_FAST if fast else _NORMAL))

    def current_user(self) -> str:
        user = auth.signed_in_user(self._backup_dir)
        if not user:
            raise TeamAuthRequired(f"Not signed in to Entra ID: {auth.LOGIN_HINT}")
        return user
