"""In-memory stand-ins for the team stores (Key Vault, Table Storage).

``tests/conftest.py`` installs a fresh :class:`FakeBackend` for every test,
so no test can reach Azure, and the suite passes without the azure extra.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import replace

from claude_swap.exceptions import TeamAuthRequired, TeamError
from claude_swap.team.naming import presence_row_key, usage_row_key
from claude_swap.team.stores import PresenceRow, SecretInfo, UsageRow

_revisions = itertools.count(1)


class FakeSecretStore:
    def __init__(self, backend: "FakeBackend") -> None:
        self.backend = backend
        self.secrets: dict[str, dict] = {}
        self.deleted: set[str] = set()
        self.get_calls: list[str] = []
        self.recovered: list[str] = []

    def _check(self) -> None:
        self.backend.check()

    def list_secrets(self) -> list[SecretInfo]:
        self._check()
        return [
            SecretInfo(name, s["revision"], dict(s["tags"]))
            for name, s in self.secrets.items()
        ]

    def get_secret(self, name: str):
        self._check()
        self.get_calls.append(name)
        s = self.secrets.get(name)
        return None if s is None else (s["value"], s["revision"])

    def set_secret(self, name, value, *, tags, content_type):
        self._check()
        if self.backend.read_only:
            raise TeamError("Access denied to Key Vault (fake)")
        if name in self.deleted:
            self.deleted.discard(name)
            self.recovered.append(name)
        revision = f"r{next(_revisions)}"
        self.secrets[name] = {
            "value": value, "revision": revision, "tags": dict(tags), "content_type": content_type,
        }
        return revision

    def delete_secret(self, name: str) -> None:
        self._check()
        if self.secrets.pop(name, None) is not None:
            self.deleted.add(name)


class FakePresenceStore:
    def __init__(self, backend: "FakeBackend") -> None:
        self.backend = backend
        self.rows: dict[str, dict[str, PresenceRow]] = {}
        self.usage: dict[str, dict[str, UsageRow]] = {}
        self.table_ensured = False

    def ensure_table(self) -> None:
        self.backend.check()
        self.table_ensured = True

    def upsert(self, row: PresenceRow) -> None:
        self.backend.check(presence=True)
        self.rows.setdefault(row.team, {})[presence_row_key(row.user, row.machine)] = row

    def list(self, team: str) -> list[PresenceRow]:
        self.backend.check(presence=True)
        return list(self.rows.get(team, {}).values())

    def upsert_usage(self, row: UsageRow) -> None:
        self.backend.check(presence=True)
        # The store stamps its own write time, like Table Storage's Timestamp.
        stored = replace(row, written_at=self.backend.clock())
        self.usage.setdefault(row.team, {})[usage_row_key(row.email)] = stored

    def list_usage(self, team: str) -> list[UsageRow]:
        self.backend.check(presence=True)
        return list(self.usage.get(team, {}).values())


class FakeBackend:
    def __init__(self, user: str = "me@corp.example") -> None:
        self.user = user
        self.vaults: dict[str, FakeSecretStore] = {}
        self.tables: dict[tuple[str, str], FakePresenceStore] = {}
        self.signed_in = True
        self.offline = False
        self.presence_offline = False
        self.read_only = False
        self.fast_requests: list[str] = []
        self.clock = time.time  # the "server" clock usage rows are stamped with

    def check(self, *, presence: bool = False) -> None:
        if not self.signed_in:
            raise TeamAuthRequired("Entra ID sign-in expired: run 'cswap login az' to sign in to Entra ID")
        if self.offline or (presence and self.presence_offline):
            raise TeamError("Could not reach Azure (fake)")

    def vault(self, url: str) -> FakeSecretStore:
        return self.vaults.setdefault(url, FakeSecretStore(self))

    def table(self, endpoint: str, table: str) -> FakePresenceStore:
        return self.tables.setdefault((endpoint, table), FakePresenceStore(self))

    def secret_store(self, vault_url: str, *, fast: bool = False) -> FakeSecretStore:
        if fast:
            self.fast_requests.append(vault_url)
        return self.vault(vault_url)

    def presence_store(self, endpoint: str, table: str, *, fast: bool = False) -> FakePresenceStore:
        return self.table(endpoint, table)

    def current_user(self) -> str:
        if not self.signed_in:
            raise TeamAuthRequired("Not signed in to Entra ID: run 'cswap login az' to sign in to Entra ID")
        return self.user
