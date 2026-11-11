"""Bring this machine's team slots in line with the team's vault.

Two phases, so no network call ever runs under a cswap lock
(docs/architecture.md): ``plan`` reads the vault, fetching values only for
secrets whose revision differs from the slot's ``teamSecretRevision``; then
``apply`` writes slots through the same switcher methods the CLI uses
(``add_account_from_token`` / ``remove_account``, which take their own locks
and never prompt with an explicit token and ``assume_yes``).

When the active account's token changes it is re-activated with
``switch_to(..., force=True)``, so Claude Code stops using the replaced token.
When the active account leaves the vault, this machine switches to another
account first and then removes the slot.
"""

from __future__ import annotations

import contextlib
import io
import logging
from dataclasses import dataclass, field

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.team import naming, registry
from claude_swap.team.stores import SecretStore, get_backend

_logger = logging.getLogger("claude-swap")


@dataclass
class SyncReport:
    team: str
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    reactivated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # "email: reason"

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def to_json(self) -> dict:
        return {
            "team": self.team,
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "reactivated": self.reactivated,
            "skipped": self.skipped,
        }


@dataclass
class _Plan:
    team: str
    fetched: list[tuple[dict, str]] = field(default_factory=list)  # (payload, revision)
    gone: list[str] = field(default_factory=list)  # emails no longer in the vault
    skipped: list[str] = field(default_factory=list)


def _team_slots(switcher, team: str) -> dict[str, tuple[str, dict]]:
    """``{email: (slot, record)}`` of this team's local slots."""
    data = switcher._get_sequence_data() or {}
    return {
        (rec.get("email") or "").lower(): (num, rec)
        for num, rec in (data.get("accounts") or {}).items()
        if rec.get("team") == team
    }


def plan(switcher, team: str, store: SecretStore) -> _Plan:
    """Read the vault (network) and decide what changes, without writing anything."""
    result = _Plan(team)
    local = _team_slots(switcher, team)
    seen: set[str] = set()
    for info in store.list_secrets():
        tags = info.tags
        if tags.get(naming.TAG_KIND) != naming.KIND_ACCOUNT or tags.get(naming.TAG_TEAM) != team:
            continue
        email = (tags.get(naming.TAG_EMAIL) or "").lower()
        if not email:
            continue
        seen.add(email)
        slot = local.get(email)
        if slot is not None and slot[1].get("teamSecretRevision") == info.revision:
            continue
        got = store.get_secret(info.name)
        if got is None:  # deleted between list and get
            seen.discard(email)
            continue
        value, revision = got
        try:
            payload = naming.parse_account_payload(value)
        except ClaudeSwitchError as e:
            result.skipped.append(f"{email}: {e}")
            continue
        if payload["email"] != email:
            result.skipped.append(f"{email}: the secret names {payload['email']}")
            continue
        result.fetched.append((payload, revision))
    result.gone = sorted(set(local) - seen)
    return result


def _quiet(fn, *args, **kwargs):
    """Run a printing switcher method with its output discarded."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def _active_slot(switcher) -> str | None:
    data = switcher._get_sequence_data() or {}
    active = data.get("activeAccountNumber")
    return str(active) if active is not None else None


def apply(switcher, p: _Plan) -> SyncReport:
    report = SyncReport(p.team, skipped=list(p.skipped))
    for payload, revision in p.fetched:
        email = payload["email"]
        local = _team_slots(switcher, p.team).get(email)
        try:
            _quiet(
                switcher.add_account_from_token,
                token=payload["token"],
                email=email,
                assume_yes=True,
                team=p.team,
                team_revision=revision,
            )
        except ClaudeSwitchError as e:
            report.skipped.append(f"{email}: {e}")
            continue
        if local is None:
            report.added.append(email)
            continue
        report.updated.append(email)
        if _active_slot(switcher) == local[0]:
            try:
                _quiet(switcher.switch_to, local[0], json_output=True, force=True)
                report.reactivated.append(email)
            except ClaudeSwitchError as e:
                report.skipped.append(f"{email}: updated, but not re-activated ({e})")
    for email in p.gone:
        local = _team_slots(switcher, p.team).get(email)
        if local is None:
            continue
        number = local[0]
        if _active_slot(switcher) == number:
            try:
                _quiet(switcher.switch, json_output=True)
            except ClaudeSwitchError as e:
                _logger.info("team sync: no account to move to before removing %s: %s", email, e)
        try:
            _quiet(switcher.remove_account, number, assume_yes=True)
        except ClaudeSwitchError as e:
            report.skipped.append(f"{email}: not removed ({e})")
            continue
        report.removed.append(email)
    return report


def sync_team(switcher, team: str, *, fast: bool = False) -> SyncReport:
    entry = registry.get(switcher.backup_dir, team)
    backend = get_backend()
    store = backend.secret_store(entry["vaultUrl"], fast=fast)
    manifest = store.get_secret(naming.manifest_secret_name(team))
    if manifest is not None:
        storage = naming.parse_manifest(team, manifest[0])
        if (storage["tableEndpoint"], storage["table"]) != (entry.get("tableEndpoint"), entry.get("table")):
            registry.put(
                switcher.backup_dir, team, vault_url=entry["vaultUrl"],
                table_endpoint=storage["tableEndpoint"], table=storage["table"],
            )
    return apply(switcher, plan(switcher, team, store))


def sync_all(switcher, *, fast: bool = False) -> tuple[list[SyncReport], dict[str, str]]:
    """Sync every joined team; returns the reports and ``{team: error}``."""
    reports: list[SyncReport] = []
    errors: dict[str, str] = {}
    for team in registry.load(switcher.backup_dir):
        try:
            reports.append(sync_team(switcher, team, fast=fast))
        except ClaudeSwitchError as e:
            errors[team] = str(e)
    return reports, errors
