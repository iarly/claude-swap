"""Linking the admin's own login to a team account, and sharing its usage."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.exceptions import ValidationError
from claude_swap.json_output import usage_from_json
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.team import links, naming, presence, service, sync
from claude_swap.team import usage as team_usage
from claude_swap.usage_store import FetchRecord

VAULT = "https://kv-xpto.vault.azure.net/"
ENDPOINT = "https://acc.table.core.windows.net"
TABLE = "cswappresence"
SETUP_TOKEN = "sk-ant-oat01-" + "s" * 40
EMAIL = "shared@example.com"


def _switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _seed_login(s, num: int, email: str = EMAIL, org: str = "org-1") -> None:
    """A normal /login slot: an OAuth credential with a refresh token."""
    s._write_account_credentials(str(num), email, json.dumps({"claudeAiOauth": {
        "accessToken": f"at-{num}", "refreshToken": f"rt-{num}",
        "scopes": ["user:inference", "user:profile"],
    }}))
    s._write_account_config(str(num), email, json.dumps({"oauthAccount": {
        "emailAddress": email, "accountUuid": f"uuid-{num}",
        "organizationUuid": org, "organizationName": "Org",
    }}))
    data = s._get_sequence_data()
    data["accounts"][str(num)] = {
        "email": email, "uuid": f"uuid-{num}", "organizationUuid": org,
        "organizationName": "Org", "added": "2026-01-01T00:00:00Z",
    }
    data["sequence"] = sorted(set(data["sequence"]) | {num})
    s._write_json(s.sequence_file, data)


def _accounts(s) -> dict:
    return s._get_sequence_data()["accounts"]


def _admin(team_backend):
    s = _switcher()
    _seed_login(s, 1)
    service.register(s, "xpto", vault_url=VAULT, table_endpoint=ENDPOINT, table=TABLE)
    return s


def _vault(team_backend):
    return team_backend.vault(VAULT)


def _reading(pct5: float = 40.0, pct7: float = 10.0) -> dict:
    later = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    return usage_from_json({
        "fiveHour": {"pct": pct5, "resetsAt": later},
        "sevenDay": {"pct": pct7, "resetsAt": later},
    })


def _record_reading(s, num: str, usage: dict, at: float) -> None:
    record = _accounts(s)[num]
    identity = (record["email"], record.get("organizationUuid", "") or "")
    s._usage_store.clock = lambda: at
    s._usage_store.record({num: FetchRecord(usage=usage)}, {num: identity})


class TestLinking:
    def test_setup_token_links_the_existing_login(self, temp_home: Path, team_backend, capsys):
        s = _admin(team_backend)
        assert service.publish_account(s, "xpto", SETUP_TOKEN, EMAIL) == "1"
        accounts = _accounts(s)
        assert list(accounts) == ["1"]  # no second slot
        assert accounts["1"]["teamLink"] == "xpto"
        assert "team" not in accounts["1"]
        assert naming.account_secret_name("xpto", EMAIL) in _vault(team_backend).secrets
        out = capsys.readouterr().out
        assert "Linked to slot 1" in out and "trusts --email" in out

    def test_without_a_login_a_team_slot_is_added(self, temp_home: Path, team_backend):
        s = _admin(team_backend)
        assert service.publish_account(s, "xpto", SETUP_TOKEN, "other@example.com") is None
        added = [r for r in _accounts(s).values() if r["email"] == "other@example.com"]
        assert len(added) == 1 and added[0]["team"] == "xpto"

    def test_several_logins_need_slot(self, temp_home: Path, team_backend):
        s = _admin(team_backend)
        _seed_login(s, 2, org="org-2")
        with pytest.raises(ValidationError, match="--slot"):
            service.publish_account(s, "xpto", SETUP_TOKEN, EMAIL)
        assert naming.account_secret_name("xpto", EMAIL) not in _vault(team_backend).secrets
        assert service.publish_account(s, "xpto", SETUP_TOKEN, EMAIL, slot=2) == "2"
        assert "teamLink" not in _accounts(s)["1"]

    def test_slot_must_be_a_normal_login(self, temp_home: Path, team_backend):
        s = _admin(team_backend)
        s.add_account_from_token(token="sk-ant-oat01-" + "t" * 40, email="tok@example.com")
        token_slot = next(n for n, r in _accounts(s).items() if r["email"] == "tok@example.com")
        with pytest.raises(ValidationError, match="not a normal login"):
            service.publish_account(s, "xpto", SETUP_TOKEN, "tok@example.com", slot=token_slot)
        assert _vault(team_backend).secrets.keys() == {naming.manifest_secret_name("xpto")}

    def test_api_key_is_never_linked(self, temp_home: Path, team_backend):
        s = _admin(team_backend)
        with pytest.raises(ValidationError, match="API key"):
            service.publish_account(s, "xpto", "sk-ant-api03-" + "k" * 40, EMAIL, slot=1)

    def test_relogin_keeps_the_link(self):
        assert ClaudeAccountSwitcher._carried_settings(
            {"teamLink": "xpto", "alias": "w"}
        ) == {"alias": "w", "teamLink": "xpto"}


class TestAdminSync:
    def test_sync_neither_duplicates_nor_removes_the_linked_login(self, temp_home: Path, team_backend):
        s = _admin(team_backend)
        service.publish_account(s, "xpto", SETUP_TOKEN, EMAIL)
        report = sync.sync_team(s, "xpto")
        assert (report.added, report.removed, report.unlinked) == ([], [], [])
        assert list(_accounts(s)) == ["1"]
        assert _accounts(s)["1"]["teamLink"] == "xpto"

    def test_account_removed_from_vault_unlinks_and_keeps_the_login(self, temp_home: Path, team_backend):
        s = _admin(team_backend)
        service.publish_account(s, "xpto", SETUP_TOKEN, EMAIL)
        report = service.remove_account(s, "xpto", EMAIL)
        assert report.unlinked == [EMAIL] and report.removed == []
        assert list(_accounts(s)) == ["1"]
        assert "teamLink" not in _accounts(s)["1"]

    def test_leave_unlinks_and_keeps_the_login(self, temp_home: Path, team_backend):
        s = _admin(team_backend)
        service.publish_account(s, "xpto", SETUP_TOKEN, EMAIL)
        assert service.leave(s, "xpto") == []
        assert list(_accounts(s)) == ["1"]
        assert "teamLink" not in _accounts(s)["1"]


class TestPresenceAndGrouping:
    def test_active_linked_login_is_on_the_team_account(self, temp_home: Path, team_backend):
        s = _admin(team_backend)
        service.publish_account(s, "xpto", SETUP_TOKEN, EMAIL)
        seq = s._get_sequence_data()
        seq["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, seq)
        assert presence.active_team_account(s._get_sequence_data()) == ("xpto", EMAIL)

    def test_usage_partition_is_no_team_name(self):
        with pytest.raises(ValidationError):
            naming.validate_team_name(naming.usage_partition("xpto"))

    def test_presence_list_ignores_usage_rows(self, temp_home: Path, team_backend):
        s = _admin(team_backend)
        service.publish_account(s, "xpto", SETUP_TOKEN, EMAIL)
        _record_reading(s, "1", _reading(), at=1000.0)
        team_usage.publish(s, now=1000.0)
        assert team_backend.table(ENDPOINT, TABLE).list("xpto") == []


class TestUsageSharing:
    def test_publish_once_per_measurement(self, temp_home: Path, team_backend):
        s = _admin(team_backend)
        service.publish_account(s, "xpto", SETUP_TOKEN, EMAIL)
        table = team_backend.table(ENDPOINT, TABLE)
        _record_reading(s, "1", _reading(), at=1000.0)
        with patch.object(table, "upsert_usage", wraps=table.upsert_usage) as upsert:
            team_usage.publish(s, now=1010.0)
            team_usage.publish(s, now=1020.0)
            assert upsert.call_count == 1
            _record_reading(s, "1", _reading(pct5=55.0), at=1100.0)
            team_usage.publish(s, now=1110.0)
            assert upsert.call_count == 2
        (row,) = table.list_usage("xpto")
        assert row.usage["fiveHour"]["pct"] == 55.0
        assert row.age_at_write == pytest.approx(10.0)

    def test_member_setup_token_slot_adopts_the_reading(self, temp_home: Path, team_backend):
        # Admin side: linked login publishes a reading taken at t=900, written at t=1000.
        s = _admin(team_backend)
        service.publish_account(s, "xpto", SETUP_TOKEN, EMAIL)
        _record_reading(s, "1", _reading(pct5=40.0), at=900.0)
        team_backend.clock = lambda: 5000.0  # the table's own clock
        team_usage.publish(s, now=1000.0)  # age at write: 100 s
        # Member side (same home for brevity): the team's setup-token slot.
        s.add_account_from_token(token=SETUP_TOKEN, email=EMAIL, team="xpto", assume_yes=True)
        member = next(n for n, r in _accounts(s).items() if r.get("team") == "xpto")
        s._usage_store.clock = lambda: 7000.0
        adopted, problems = team_usage.adopt(s, now=5030.0, hold_s=600)
        assert problems == [] and adopted == {member}
        entry = s._usage_store.entries({member: (EMAIL, "")})[member]
        assert entry.last_good["five_hour"]["pct"] == 40.0
        assert entry.age_s == pytest.approx(130.0)  # 100 at write + 30 since, on no shared clock
        assert entry.held(7000.0)  # keeps the collector, and so the probe, off
        assert isinstance(entry.decision_value(), dict)  # drives auto-switching

    def test_adopted_reading_shows_instead_of_unavailable(self, temp_home: Path, team_backend):
        from claude_swap.switcher import _usage_entry_lines

        s = _admin(team_backend)
        service.publish_account(s, "xpto", SETUP_TOKEN, EMAIL)
        _record_reading(s, "1", _reading(pct5=40.0), at=1000.0)
        team_usage.publish(s, now=1000.0)
        s.add_account_from_token(token=SETUP_TOKEN, email=EMAIL, team="xpto", assume_yes=True)
        member = next(n for n, r in _accounts(s).items() if r.get("team") == "xpto")
        s._usage_store.clock = lambda: team_backend.clock()
        team_usage.adopt(s)
        entry = s._usage_store.entries({member: (EMAIL, "")})[member]
        text = " ".join(_usage_entry_lines(entry))
        assert "usage unavailable" not in text and "40" in text

    def test_reading_older_than_the_hold_is_neither_adopted_nor_held(self, temp_home: Path, team_backend):
        s = _admin(team_backend)
        service.publish_account(s, "xpto", SETUP_TOKEN, EMAIL)
        _record_reading(s, "1", _reading(pct5=40.0), at=1000.0)
        team_backend.clock = lambda: 1000.0
        team_usage.publish(s, now=1000.0)
        s.add_account_from_token(token=SETUP_TOKEN, email=EMAIL, team="xpto", assume_yes=True)
        member = next(n for n, r in _accounts(s).items() if r.get("team") == "xpto")
        # The admin stopped: the row is now older than the hold.
        s._usage_store.clock = lambda: 1601.0
        adopted, _ = team_usage.adopt(s, now=1601.0, hold_s=600)
        assert adopted == set()
        entry = s._usage_store.entries({member: (EMAIL, "")})[member]
        assert not entry.held(1601.0) and entry.last_good is None
        # This machine's own reading (e.g. its probe) is not re-held by the stale row.
        _record_reading(s, member, _reading(pct5=70.0), at=1700.0)
        team_usage.adopt(s, now=1700.0, hold_s=600)
        entry = s._usage_store.entries({member: (EMAIL, "")})[member]
        assert not entry.held(1700.0)
        assert entry.last_good["five_hour"]["pct"] == 70.0

    def test_offline_table_is_reported_not_raised(self, temp_home: Path, team_backend):
        s = _admin(team_backend)
        service.publish_account(s, "xpto", SETUP_TOKEN, EMAIL)
        _record_reading(s, "1", _reading(), at=1000.0)
        team_backend.presence_offline = True
        problems = team_usage.exchange(s, now=1000.0)
        assert any("not shared" in p for p in problems)
