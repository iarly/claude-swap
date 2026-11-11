"""Who is on which team account: one Table Storage row per person and machine.

``publish`` writes this machine's row for each joined team, saying which of
that team's accounts is active here (empty when the active account is not
that team's). The switch hook publishes after every real switch; the watch
and ``auto`` publish again every ``team.heartbeatSeconds`` so ``lastSeen``
stays fresh. A row older than ``team.presenceStaleSeconds`` counts as away.

``since`` survives heartbeats: ``<backup_dir>/azure/presence.json`` keeps,
per team, the account last published and when that started.

Everything here is best-effort and runs outside cswap's locks.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError, TeamAuthRequired
from claude_swap.fsutil import atomic_write_text
from claude_swap.team import registry
from claude_swap.team.naming import presence_row_key
from claude_swap.team.stores import PresenceRow, get_backend

_logger = logging.getLogger("claude-swap")


def machine_name() -> str:
    try:
        return socket.gethostname() or "unknown"
    except OSError:
        return "unknown"


def _state_path(backup_dir: Path) -> Path:
    return Path(backup_dir) / "azure" / "presence.json"


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


def active_team_account(seq_data: dict) -> tuple[str, str]:
    """``(team, email)`` of the recorded active account; ``("", "")`` when personal or none."""
    active = seq_data.get("activeAccountNumber")
    record = (seq_data.get("accounts") or {}).get(str(active)) if active is not None else None
    if not record or not record.get("team"):
        return "", ""
    return record["team"], record.get("email", "")


def publish(switcher, teams: list[str] | None = None, *, now: float | None = None) -> list[str]:
    """Upsert this machine's row in each team (all joined teams by default).

    Returns warnings (one per failing team) instead of raising: callers are
    a switch, the watch or ``auto``, none of which may fail because presence
    is unreachable.
    """
    backup_dir = switcher.backup_dir
    joined = registry.load(backup_dir)
    targets = [t for t in (teams if teams is not None else list(joined)) if t in joined]
    if not targets:
        return []
    now = time.time() if now is None else now
    seq = switcher._get_sequence_data() or {}
    active_team, active_email = active_team_account(seq)
    state = _load_state(backup_dir)
    warnings: list[str] = []
    try:
        backend = get_backend()
        user = backend.current_user()
    except ClaudeSwitchError as e:
        return [str(e)]
    machine = machine_name()
    for team in targets:
        entry = joined[team]
        email = active_email if active_team == team else ""
        prior = state.get(team) or {}
        since = prior.get("since") if prior.get("account") == email else None
        since = float(since) if isinstance(since, (int, float)) else now
        try:
            store = backend.presence_store(entry["tableEndpoint"], entry["table"], fast=True)
            store.upsert(PresenceRow(team, user, machine, email, since, now))
        except ClaudeSwitchError as e:
            warnings.append(f"team {team}: presence not updated ({e})")
            continue
        state[team] = {"account": email, "since": since}
    try:
        _save_state(backup_dir, state)
    except OSError as e:
        _logger.debug("presence state not saved: %s", e)
    return warnings


def after_switch(switcher, op: dict) -> None:
    """The switch hook: publish presence for the teams the switch touched.

    Cheap when no team is joined (one stat). Never raises; a sign-in problem
    becomes one line on stderr so JSON on stdout stays intact.
    """
    try:
        if not registry.has_teams(switcher.backup_dir):
            return
        from_ref, to_ref = op.get("from"), op.get("to")
        if not to_ref or from_ref == to_ref:
            return
        seq = switcher._get_sequence_data() or {}
        accounts = seq.get("accounts") or {}
        touched = set()
        for ref in (from_ref, to_ref):
            number = ref.get("number") if isinstance(ref, dict) else None
            team = (accounts.get(str(number)) or {}).get("team") if number is not None else None
            if team:
                touched.add(team)
        if not touched:
            return
        for line in publish(switcher, sorted(touched)):
            _logger.debug("presence: %s", line)
            if "cswap login az" in line:
                print(f"cswap: {line}", file=sys.stderr)
                break
    except Exception as e:  # noqa: BLE001 — presence must never break a switch
        _logger.debug("presence hook failed: %s", e)


@dataclass
class TeamPresence:
    """A presence read of every joined team, plus what went wrong."""

    rows: dict[str, list[PresenceRow]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    taken_at: float = 0.0
    auth_required: bool = False


def read_all(switcher, *, now: float | None = None) -> TeamPresence:
    joined = registry.load(switcher.backup_dir)
    result = TeamPresence(taken_at=time.time() if now is None else now)
    if not joined:
        return result
    try:
        backend = get_backend()
    except ClaudeSwitchError as e:
        result.errors = {t: str(e) for t in joined}
        return result
    for team, entry in joined.items():
        try:
            store = backend.presence_store(entry["tableEndpoint"], entry["table"], fast=True)
            result.rows[team] = store.list(team)
        except TeamAuthRequired as e:
            result.auth_required = True
            result.errors[team] = str(e)
        except ClaudeSwitchError as e:
            result.errors[team] = str(e)
    return result


class PresenceCache:
    """``read_all`` at most once per ``ttl`` seconds; safe across threads."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._value: TeamPresence | None = None

    def get(self, switcher, *, now: float | None = None) -> TeamPresence:
        now = time.time() if now is None else now
        with self._lock:
            if self._value is not None and now - self._value.taken_at < self.ttl:
                return self._value
        value = read_all(switcher, now=now)
        with self._lock:
            self._value = value
        return value

    def invalidate(self) -> None:
        with self._lock:
            self._value = None


def is_away(row: PresenceRow, now: float, stale_seconds: float) -> bool:
    return now - row.last_seen > stale_seconds


def people_by_account(
    presence: TeamPresence, now: float, stale_seconds: float
) -> dict[tuple[str, str], list[tuple[PresenceRow, bool]]]:
    """``{(team, email): [(row, away), ...]}``, present people first, then by name."""
    out: dict[tuple[str, str], list[tuple[PresenceRow, bool]]] = {}
    for team, rows in presence.rows.items():
        for row in rows:
            if not row.account_email:
                continue
            out.setdefault((team, row.account_email.lower()), []).append(
                (row, is_away(row, now, stale_seconds))
            )
    for people in out.values():
        people.sort(key=lambda p: (p[1], p[0].user.lower()))
    return out


def occupancy(
    presence: TeamPresence,
    now: float,
    stale_seconds: float,
    *,
    self_user: str | None,
    self_machine: str,
) -> dict[tuple[str, str], int]:
    """People present on each ``(team, email)``, not counting this machine's own row."""
    own = presence_row_key(self_user, self_machine) if self_user else None
    counts: dict[tuple[str, str], int] = {}
    for team, rows in presence.rows.items():
        for row in rows:
            if not row.account_email or is_away(row, now, stale_seconds):
                continue
            if own is not None and presence_row_key(row.user, row.machine) == own:
                continue
            key = (team, row.account_email.lower())
            counts[key] = counts.get(key, 0) + 1
    return counts
