"""Usage readings shared through the team's table, so setup-tokens need no probe.

A setup-token cannot read the usage endpoint; only a probe request, billed to
the account's own window, can measure it. The admin usually holds the
account's normal login too, which reads usage for free. With that slot linked
(``team/links.py``), this module:

- ``publish``: on the admin's machine, writes each linked slot's latest
  reading to the team's usage partition, once per new measurement;
- ``adopt``: on members' machines, hands those readings to the local usage
  store (``UsageStore.adopt``, the ``import-usage`` path) for the team's
  setup-token slots, with a hold. The hold keeps every local collector,
  and so the probe, off those slots while readings keep arriving, and keeps
  an older reading decision-trusted: the admin polls an idle account slowly.

The age travels as "age when written" plus the time since the table stamped
the row, so no two machines' clocks have to agree. Freshness follows the
admin's own poll cadence for that account, and stops while the admin's cswap
(watch or auto) is not running; the hold lapses and members fall back to
"usage unavailable" or their own probe.

Runs only on background paths (the watch and ``auto``) and ``team sync``,
outside every cswap lock. Best-effort: failures come back as messages.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.fsutil import atomic_write_text
from claude_swap.team import links, registry
from claude_swap.team.stores import UsageRow, get_backend

_logger = logging.getLogger("claude-swap")


def _state_path(backup_dir: Path) -> Path:
    return Path(backup_dir) / "azure" / "usage.json"


def _load_state(backup_dir: Path) -> dict:
    try:
        data = json.loads(_state_path(backup_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(backup_dir: Path, state: dict) -> None:
    p = _state_path(backup_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(state))


def _identity(record: dict) -> tuple[str, str]:
    return (record.get("email", ""), record.get("organizationUuid", "") or "")


def publish(switcher, *, now: float | None = None) -> list[str]:
    """Write each linked slot's newest reading to its team. Returns problems."""
    from claude_swap.json_output import usage_to_json

    backup_dir = switcher.backup_dir
    joined = registry.load(backup_dir)
    if not joined:
        return []
    seq = switcher._get_sequence_data() or {}
    pending: list[tuple[str, str, str, dict]] = []  # (team, email, slot, record)
    for team in joined:
        for email, (number, record) in links.linked_slots(seq, team).items():
            pending.append((team, email, number, record))
    if not pending:
        return []
    now = time.time() if now is None else now
    entries = switcher._usage_store.entries({num: _identity(rec) for _t, _e, num, rec in pending})
    state = _load_state(backup_dir)
    published = state.setdefault("published", {})
    problems: list[str] = []
    try:
        backend = get_backend()
        user = backend.current_user()
    except ClaudeSwitchError as e:
        return [str(e)]
    for team, email, number, _record in pending:
        entry = entries.get(number)
        if entry is None or entry.last_good is None or entry.fetched_at is None:
            continue
        key = f"{team}|{email}"
        if published.get(key, 0) >= entry.fetched_at:
            continue  # this measurement is already in the table
        usage = usage_to_json(entry.last_good)
        if not usage:
            continue
        row = UsageRow(
            team=team, email=email, usage=usage,
            age_at_write=max(0.0, now - entry.fetched_at), written_at=0.0, publisher=user,
        )
        info = joined[team]
        try:
            backend.presence_store(info["tableEndpoint"], info["table"], fast=True).upsert_usage(row)
        except ClaudeSwitchError as e:
            problems.append(f"team {team}: usage of {email} not shared ({e})")
            continue
        published[key] = entry.fetched_at
    try:
        _save_state(backup_dir, state)
    except OSError as e:
        _logger.debug("team usage state not saved: %s", e)
    return problems


def adopt(switcher, *, now: float | None = None, hold_s: float | None = None) -> tuple[set[str], list[str]]:
    """Adopt the team's published readings into this machine's team slots.

    Returns the slots whose reading was replaced, and problems. ``hold_s``
    defaults to ``team.presenceStaleSeconds``: past it without a new reading,
    the admin counts as away and members' collectors take over again.
    """
    from claude_swap.json_output import usage_from_json
    from claude_swap.settings import load_team_settings

    backup_dir = switcher.backup_dir
    joined = registry.load(backup_dir)
    if not joined:
        return set(), []
    accounts = (switcher._get_sequence_data() or {}).get("accounts") or {}
    slots_by_team: dict[str, dict[str, tuple[str, dict]]] = {}
    for number, record in accounts.items():
        team = record.get("team")
        if team in joined:
            slots_by_team.setdefault(team, {})[(record.get("email") or "").lower()] = (number, record)
    if not slots_by_team:
        return set(), []
    now = time.time() if now is None else now
    if hold_s is None:
        hold_s = float(load_team_settings(backup_dir).presence_stale_seconds)
    readings: dict[str, tuple[dict, float]] = {}
    identities: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    try:
        backend = get_backend()
    except ClaudeSwitchError as e:
        return set(), [str(e)]
    for team, slots in slots_by_team.items():
        info = joined[team]
        try:
            rows = backend.presence_store(info["tableEndpoint"], info["table"], fast=True).list_usage(team)
        except ClaudeSwitchError as e:
            problems.append(f"team {team}: shared usage unavailable ({e})")
            continue
        for row in rows:
            slot = slots.get(row.email.lower())
            if slot is None:
                continue
            try:
                usage = usage_from_json(row.usage)
            except ValueError as e:
                _logger.debug("team usage row for %s ignored: %s", row.email, e)
                continue
            age = max(0.0, row.age_at_write + (now - row.written_at))
            if age > hold_s:
                # The admin stopped publishing. Adopting would re-hold the slot
                # (``UsageStore.adopt`` holds even a reading it does not take)
                # and keep this machine's own collector, or probe, off it.
                continue
            number, record = slot
            readings[number] = (usage, age)
            identities[number] = _identity(record)
    if not readings:
        return set(), problems
    adopted = switcher._usage_store.adopt(readings, identities, hold_s=hold_s)
    return adopted, problems


def exchange(switcher, *, now: float | None = None) -> list[str]:
    """``publish`` then ``adopt``; never raises. Returns problems."""
    problems: list[str] = []
    try:
        problems += publish(switcher, now=now)
    except Exception as e:  # noqa: BLE001 — usage sharing never breaks its caller
        problems.append(f"usage not shared ({e})")
    try:
        _adopted, more = adopt(switcher, now=now)
        problems += more
    except Exception as e:  # noqa: BLE001
        problems.append(f"shared usage not read ({e})")
    for line in problems:
        _logger.debug("team usage: %s", line)
    return problems
