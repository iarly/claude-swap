"""Team view of the TUI: grouped account lists, presence and vault sync.

``group_accounts`` orders a snapshot into ``Team <name>`` groups and a
``Personal accounts`` group (no headings at all when no account belongs to a
team, so a team-less list looks exactly as before).

``TeamWatcher`` runs while the TUI is open and a team is joined. On thread
workers, never the UI loop, it re-reads presence every
``team.presenceRefreshSeconds``, refreshes this machine's row every
``team.heartbeatSeconds`` and syncs every team from its vault every
``team.syncIntervalSeconds``. A team account whose token reads as dead
triggers a sync right away (at most once a minute), which is how a token
the admin replaced in the vault reaches this machine without a restart.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from claude_swap.json_output import USAGE_RELOGIN_REQUIRED, USAGE_TOKEN_EXPIRED
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.team import registry
from claude_swap.team.naming import short_user

if TYPE_CHECKING:
    from claude_swap.team.presence import TeamPresence

PERSONAL_LABEL = "Personal accounts"
DEAD_SENTINELS = {USAGE_TOKEN_EXPIRED, USAGE_RELOGIN_REQUIRED}
DEAD_TOKEN_SYNC_GAP_S = 60.0


def group_accounts(snap: AccountsSnapshot) -> list[tuple[str | None, list[AccountSnapshot]]]:
    """``[(heading or None, accounts)]``: teams by name, then personal accounts."""
    if not any(acc.team for acc in snap.accounts):
        return [(None, list(snap.accounts))]
    teams: dict[str, list[AccountSnapshot]] = {}
    personal: list[AccountSnapshot] = []
    for acc in snap.accounts:
        if acc.team:
            teams.setdefault(acc.team, []).append(acc)
        else:
            personal.append(acc)
    groups: list[tuple[str | None, list[AccountSnapshot]]] = [
        (f"Team {team}", teams[team]) for team in sorted(teams)
    ]
    if personal:
        groups.append((PERSONAL_LABEL, personal))
    return groups


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def people_for(
    presence: "TeamPresence | None", acc: AccountSnapshot, now: float, stale_seconds: float
) -> list[tuple[str, bool, str]]:
    """``[(short name, away, age)]`` of the people on ``acc``, present first."""
    if presence is None or not acc.team:
        return []
    from claude_swap.team.presence import is_away

    out = []
    for row in presence.rows.get(acc.team, []):
        if row.account_email.lower() != acc.email.lower():
            continue
        away = is_away(row, now, stale_seconds)
        out.append((short_user(row.user), away, _age(now - row.last_seen)))
    out.sort(key=lambda p: (p[1], p[0].lower()))
    return out


def dead_team_tokens(snap: AccountsSnapshot) -> bool:
    """Whether any team account currently reads as an expired or refused token.

    Only slots the vault owns count: a linked login is the admin's own, its
    "token expired" just means Claude Code has yet to refresh it, and a
    vault sync could not change it anyway.
    """
    for acc in snap.accounts:
        if not acc.team or acc.team_linked:
            continue
        if acc.usage.sentinel in DEAD_SENTINELS or acc.usage.auth_dead_strikes:
            return True
        if "401" in (acc.usage.last_error or ""):
            return True
    return False


class TeamWatcher:
    """Background team work for the TUI; inert when no team is joined."""

    def __init__(self, app) -> None:
        self.app = app
        self.switcher = app.switcher
        self.enabled = registry.has_teams(self.switcher.backup_dir)
        self._syncing = False
        self._reading = False
        self._last_dead_sync = 0.0
        if not self.enabled:
            return
        from claude_swap.settings import load_team_settings
        from claude_swap.team.presence import PresenceCache

        self.settings = load_team_settings(self.switcher.backup_dir)
        self._cache = PresenceCache(self.settings.presence_refresh_seconds)

    def start(self) -> None:
        if not self.enabled:
            return
        app = self.app
        app.set_interval(self.settings.presence_refresh_seconds, self.refresh_presence)
        app.set_interval(self.settings.heartbeat_seconds, self.heartbeat)
        app.set_interval(self.settings.sync_interval_seconds, self.sync)
        self.heartbeat()
        self.sync()

    # -- presence ---------------------------------------------------------------

    def refresh_presence(self, *, force: bool = False) -> None:
        if self._reading:
            return
        self._reading = True
        if force:
            self._cache.invalidate()
        self.app.run_worker(
            self._read_blocking, thread=True, group="team-presence",
            exit_on_error=False, name="team-presence",
        )

    def _read_blocking(self) -> None:
        try:
            value = self._cache.get(self.switcher)
        except Exception as e:  # noqa: BLE001 — never take the TUI down
            value = None
            self.app.call_from_thread(self.app.set_team_status, f"presence unavailable ({e})")
        # Same cadence: share linked slots' usage, adopt the team's readings.
        # The 3 s poll then shows adopted readings like any other.
        from claude_swap.team.usage import exchange

        exchange(self.switcher)
        self.app.call_from_thread(self._apply_presence, value)

    def _apply_presence(self, value) -> None:
        self._reading = False
        self.app.team_presence = value
        if value is None:
            return
        if value.auth_required:
            self.app.set_team_status("presence unavailable · run 'cswap login az'")
        elif value.errors and not value.rows:
            self.app.set_team_status("presence unavailable")

    def heartbeat(self) -> None:
        self.app.run_worker(
            self._heartbeat_blocking, thread=True, group="team-heartbeat",
            exit_on_error=False, name="team-heartbeat",
        )

    def _heartbeat_blocking(self) -> None:
        from claude_swap.team.presence import publish

        try:
            publish(self.switcher)
        except Exception:  # noqa: BLE001
            pass

    # -- vault sync -------------------------------------------------------------

    def sync(self) -> None:
        # Sync writes slots through the same switcher methods as the app's
        # actions, so it takes the app's single-flight slot: skipped while a
        # user action runs, and holding user actions off for its few seconds.
        if self._syncing or self.app.busy:
            return
        self._syncing = True
        self.app.busy = True
        self.app.run_worker(
            self._sync_blocking, thread=True, group="team-sync",
            exit_on_error=False, name="team-sync",
        )

    def _sync_blocking(self) -> None:
        from claude_swap.team.sync import sync_all

        try:
            reports, errors = sync_all(self.switcher, fast=True)
        except Exception as e:  # noqa: BLE001
            reports, errors = [], {"*": str(e)}
        self.app.call_from_thread(self._apply_sync, reports, errors)

    def _apply_sync(self, reports, errors: dict[str, str]) -> None:
        self._syncing = False
        self.app.busy = False
        events = []
        for report in reports:
            events += [f"{email}: token renewed from the vault" for email in report.updated]
            events += [f"{email}: added by team {report.team}" for email in report.added]
            events += [f"{email}: removed from team {report.team}" for email in report.removed]
        if events:
            for event in events:
                self.app.notify(event, title="team sync")
            self.app.set_team_status(events[-1])
            self.app.request_refresh()
        elif errors:
            message = next(iter(errors.values()))
            if "cswap login az" in message:
                self.app.set_team_status("vault sync paused · run 'cswap login az'")
            else:
                self.app.set_team_status(f"vault sync failed ({message})")

    def on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        """Sync now when a team token reads as dead (rate-limited)."""
        if not self.enabled or snap is None or not dead_team_tokens(snap):
            return
        now = time.time()
        if now - self._last_dead_sync < DEAD_TOKEN_SYNC_GAP_S:
            return
        self._last_dead_sync = now
        self.sync()
