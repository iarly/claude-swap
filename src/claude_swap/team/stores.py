"""Storage protocols the team feature runs on, and the backend that makes them.

``get_backend()`` returns the Azure backend unless a test installed another
with ``set_backend``. Implementations raise only cswap exceptions
(``TeamError`` and subclasses), so callers never see an Azure SDK type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class SecretInfo:
    """A secret's listing entry: name, change marker and tags, never its value.

    ``revision`` changes whenever the secret gets a new value (the Azure
    backend uses the last-updated timestamp), which is what lets ``sync``
    read only the secrets that changed.
    """

    name: str
    revision: str
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PresenceRow:
    """Which team account one person is on, from one machine.

    ``account_email`` is empty when that person is on an account outside the
    team (a personal account's email is never published).
    """

    team: str
    user: str
    machine: str
    account_email: str
    since: float
    last_seen: float


@dataclass(frozen=True)
class UsageRow:
    """A usage reading of one team account, published by the machine that owns
    the account's normal login (its "usage source", see ``team/usage.py``).

    ``usage`` is the ``list --json`` projection (``json_output.usage_to_json``).
    ``age_at_write`` is how old the measurement was when written, and
    ``written_at`` when the store received it (the table's own timestamp), so a
    reader computes the age without trusting the publisher's clock.
    """

    team: str
    email: str
    usage: dict
    age_at_write: float
    written_at: float
    publisher: str = ""


class SecretStore(Protocol):
    def list_secrets(self) -> list[SecretInfo]: ...

    def get_secret(self, name: str) -> tuple[str, str] | None:
        """``(value, revision)``, or None when the secret does not exist."""
        ...

    def set_secret(
        self, name: str, value: str, *, tags: dict[str, str], content_type: str
    ) -> str:
        """Store a new value (recovering a soft-deleted name) and return its revision."""
        ...

    def delete_secret(self, name: str) -> None: ...


class PresenceStore(Protocol):
    def ensure_table(self) -> None: ...

    def upsert(self, row: PresenceRow) -> None: ...

    def list(self, team: str) -> list[PresenceRow]: ...

    def upsert_usage(self, row: UsageRow) -> None:
        """Write a reading in the team's usage partition (never the presence one)."""
        ...

    def list_usage(self, team: str) -> list[UsageRow]: ...


class Backend(Protocol):
    def secret_store(self, vault_url: str, *, fast: bool = False) -> SecretStore:
        """``fast`` = short timeouts and a single retry, for background paths."""
        ...

    def presence_store(self, endpoint: str, table: str, *, fast: bool = False) -> PresenceStore: ...

    def current_user(self) -> str:
        """The signed-in Entra ID user (UPN); raises TeamAuthRequired when there is none."""
        ...


_backend: Backend | None = None


def get_backend() -> Backend:
    if _backend is not None:
        return _backend
    from claude_swap.team.azure_backend import AzureBackend

    return AzureBackend()


def set_backend(backend: Backend | None) -> None:
    global _backend
    _backend = backend
