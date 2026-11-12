"""Team presence: the switch hook, heartbeat state, occupancy and the CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.team import naming, presence, registry, service
from claude_swap.team.stores import PresenceRow

VAULT = "https://kv-xpto.vault.azure.net/"
ENDPOINT = "https://acc.table.core.windows.net"
TABLE = "cswappresence"
TOKEN_A = "sk-ant-oat01-" + "a" * 40
TOKEN_B = "sk-ant-oat01-" + "c" * 40


def _switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _team_setup(team_backend):
    s = _switcher()
    s.add_account_from_token(TOKEN_B, email="mine@example.com")
    service.register(s, "xpto", vault_url=VAULT, table_endpoint=ENDPOINT, table=TABLE)
    service.publish_account(s, "xpto", TOKEN_A, "shared@example.com")
    return s


def _slot(s, email) -> str:
    return next(n for n, r in s._get_sequence_data()["accounts"].items() if r["email"] == email)


def _set_active(s, number):
    seq = s._get_sequence_data()
    seq["activeAccountNumber"] = int(number)
    s._write_json(s.sequence_file, seq)


def _rows(team_backend):
    return list(team_backend.table(ENDPOINT, TABLE).rows.get("xpto", {}).values())


def _op(s, from_email, to_email):
    ref = lambda e: {"number": int(_slot(s, e)), "email": e} if e else None  # noqa: E731
    return {"from": ref(from_email), "to": ref(to_email), "warnings": []}


class TestSwitchHook:
    def test_no_team_no_calls(self, temp_home: Path, team_backend):
        s = _switcher()
        s.add_account_from_token(TOKEN_B, email="mine@example.com")
        team_backend.signed_in = False  # any backend use would show up as a hint
        presence.after_switch(s, {"from": None, "to": {"number": 1, "email": "mine@example.com"}})
        assert team_backend.tables == {}

    def test_switch_onto_a_team_account_publishes_it(self, temp_home: Path, team_backend):
        s = _team_setup(team_backend)
        _set_active(s, _slot(s, "shared@example.com"))
        presence.after_switch(s, _op(s, "mine@example.com", "shared@example.com"))
        (row,) = _rows(team_backend)
        assert row.user == team_backend.user
        assert row.account_email == "shared@example.com"
        assert row.machine == presence.machine_name()

    def test_switch_to_personal_clears_without_naming_it(self, temp_home: Path, team_backend):
        s = _team_setup(team_backend)
        _set_active(s, _slot(s, "shared@example.com"))
        presence.after_switch(s, _op(s, "mine@example.com", "shared@example.com"))
        _set_active(s, _slot(s, "mine@example.com"))
        presence.after_switch(s, _op(s, "shared@example.com", "mine@example.com"))
        (row,) = _rows(team_backend)
        assert row.account_email == ""
        assert "mine@example.com" not in json.dumps(row.__dict__)

    def test_personal_to_personal_is_not_published(self, temp_home: Path, team_backend):
        s = _team_setup(team_backend)
        s.add_account_from_token(TOKEN_B.replace("c", "d"), email="other@example.com")
        presence.after_switch(s, _op(s, "mine@example.com", "other@example.com"))
        assert _rows(team_backend) == []

    def test_same_account_is_not_published(self, temp_home: Path, team_backend):
        s = _team_setup(team_backend)
        op = _op(s, "shared@example.com", "shared@example.com")
        presence.after_switch(s, op)
        assert _rows(team_backend) == []

    def test_signed_out_prints_one_hint_and_never_raises(self, temp_home: Path, team_backend, capsys):
        s = _team_setup(team_backend)
        team_backend.signed_in = False
        _set_active(s, _slot(s, "shared@example.com"))
        presence.after_switch(s, _op(s, "mine@example.com", "shared@example.com"))
        err = capsys.readouterr().err
        assert err.count("cswap login az") == 1
        assert _rows(team_backend) == []

    def test_table_offline_is_silent(self, temp_home: Path, team_backend, capsys):
        s = _team_setup(team_backend)
        team_backend.presence_offline = True
        _set_active(s, _slot(s, "shared@example.com"))
        presence.after_switch(s, _op(s, "mine@example.com", "shared@example.com"))
        assert capsys.readouterr().err == ""

    def test_since_survives_heartbeats(self, temp_home: Path, team_backend):
        s = _team_setup(team_backend)
        _set_active(s, _slot(s, "shared@example.com"))
        presence.publish(s, now=1000.0)
        presence.publish(s, now=1300.0)
        (row,) = _rows(team_backend)
        assert (row.since, row.last_seen) == (1000.0, 1300.0)

    def test_perform_switch_runs_the_hook(self, temp_home: Path, team_backend):
        s = _team_setup(team_backend)
        op = _op(s, "mine@example.com", "shared@example.com")
        with patch.object(s, "_switch_core", return_value=op), \
             patch("claude_swap.team.presence.after_switch") as hook:
            assert s._perform_switch(_slot(s, "shared@example.com")) is op
        hook.assert_called_once_with(s, op)


class TestOccupancy:
    def _presence(self, rows):
        return presence.TeamPresence(rows={"xpto": rows}, taken_at=1000.0)

    def test_counts_present_people_except_this_machine(self):
        rows = [
            PresenceRow("xpto", "me@corp", "pc1", "a@x.io", 0, 1000),
            PresenceRow("xpto", "ana@corp", "pc2", "a@x.io", 0, 990),
            PresenceRow("xpto", "bob@corp", "pc3", "A@x.io", 0, 995),
            PresenceRow("xpto", "old@corp", "pc4", "a@x.io", 0, 10),  # away
            PresenceRow("xpto", "cy@corp", "pc5", "", 0, 1000),  # personal
        ]
        counts = presence.occupancy(
            self._presence(rows), 1000.0, 900.0, self_user="me@corp", self_machine="pc1"
        )
        assert counts == {("xpto", "a@x.io"): 2}

    def test_people_by_account_orders_present_first(self):
        rows = [
            PresenceRow("xpto", "zed@corp", "pc", "a@x.io", 0, 1000),
            PresenceRow("xpto", "old@corp", "pc", "a@x.io", 0, 10),
            PresenceRow("xpto", "ana@corp", "pc", "a@x.io", 0, 1000),
        ]
        people = presence.people_by_account(self._presence(rows), 1000.0, 900.0)[("xpto", "a@x.io")]
        assert [(r.user, away) for r, away in people] == [
            ("ana@corp", False), ("zed@corp", False), ("old@corp", True),
        ]

    def test_cache_reads_once_per_ttl(self, temp_home: Path, team_backend):
        s = _team_setup(team_backend)
        cache = presence.PresenceCache(ttl=60)
        with patch.object(presence, "read_all", wraps=presence.read_all) as read_all:
            cache.get(s, now=1000.0)
            cache.get(s, now=1030.0)
            cache.get(s, now=1061.0)
        assert read_all.call_count == 2


def _cli(argv, capsys):
    with patch("os.geteuid", return_value=1000, create=True), \
         patch.object(sys, "argv", ["cswap", *argv]):
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestCli:
    def _seed(self, team_backend):
        from claude_swap.settings import set_setting
        from claude_swap.paths import get_backup_root

        set_setting(get_backup_root(), "team.vaultUrl", VAULT)

    def test_register_add_into_status(self, temp_home: Path, team_backend, capsys):
        self._seed(team_backend)
        code, out, _ = _cli(["team", "register", "xpto", "--storage", ENDPOINT], capsys)
        assert code == 0 and "Registered" in out
        code, out, err = _cli(
            ["add-token", TOKEN_A, "--email", "shared@example.com", "--into", "xpto"], capsys
        )
        assert code == 0, err
        assert "[team xpto]" in out
        code, out, _ = _cli(["team", "status", "--json"], capsys)
        assert code == 0
        assert json.loads(out)["teams"]["xpto"]["error"] is None

    def test_into_without_email(self, temp_home: Path, team_backend, capsys):
        self._seed(team_backend)
        _cli(["team", "register", "xpto", "--storage", ENDPOINT], capsys)
        code, out, err = _cli(["add-token", TOKEN_A, "--into", "xpto"], capsys)
        assert code == 1 and "--email" in out + err

    def test_into_with_slot_must_name_a_login(self, temp_home: Path, team_backend, capsys):
        self._seed(team_backend)
        _cli(["team", "register", "xpto", "--storage", ENDPOINT], capsys)
        code, out, err = _cli(["add-token", TOKEN_A, "--email", "a@x.io", "--into", "xpto", "--slot", "3"], capsys)
        assert code == 1 and "Slot 3" in out + err

    def test_into_only_with_add_token(self, temp_home: Path, team_backend, capsys):
        code, _, err = _cli(["list", "--into", "xpto"], capsys)
        assert code == 2 and "--into" in err

    def test_sync_json(self, temp_home: Path, team_backend, capsys):
        self._seed(team_backend)
        _cli(["team", "register", "xpto", "--storage", ENDPOINT], capsys)
        code, out, _ = _cli(["team", "sync", "--json"], capsys)
        assert code == 0
        assert json.loads(out)["teams"][0]["team"] == "xpto"

    def test_sync_without_teams(self, temp_home: Path, team_backend, capsys):
        code, _, err = _cli(["team", "sync"], capsys)
        assert code == 1 and "team join" in err

    def test_register_needs_a_vault(self, temp_home: Path, team_backend, capsys):
        code, _, err = _cli(["team", "register", "xpto", "--storage", ENDPOINT], capsys)
        assert code == 1 and "keyvault" in err

    def test_list_shows_team_tag_and_json_field(self, temp_home: Path, team_backend, capsys):
        self._seed(team_backend)
        _cli(["team", "register", "xpto", "--storage", ENDPOINT], capsys)
        _cli(["add-token", TOKEN_A, "--email", "shared@example.com", "--into", "xpto"], capsys)
        code, out, _ = _cli(["list", "--json"], capsys)
        (row,) = json.loads(out)["accounts"]
        assert row["team"] == "xpto"

    def test_missing_extra_message(self, temp_home: Path, team_backend, capsys):
        from claude_swap.exceptions import AzureExtraMissing
        from claude_swap.team import auth

        with patch.object(auth, "require_azure", side_effect=AzureExtraMissing("install 'claude-swap[azure]'")):
            code, _, err = _cli(["login", "az", "--tenant-id", "t"], capsys)
        assert code == 1 and "claude-swap[azure]" in err

    def test_login_needs_tenant_or_subscription(self, temp_home: Path, capsys):
        code, _, err = _cli(["login", "az"], capsys)
        assert code == 2 and "--subscription-id" in err


class TestCaeCredential:
    """Key Vault asks CAE tokens, Tables does not; both must hit one cache."""

    class _Inner:
        def __init__(self):
            self.calls = []

        def get_token(self, *scopes, **kwargs):
            self.calls.append(("get_token", scopes, kwargs))
            return "t"

        def get_token_info(self, *scopes, options=None):
            self.calls.append(("get_token_info", scopes, options))
            return "t"

    def test_forces_cae_on_every_request(self):
        from claude_swap.team.auth import _CaeCredential

        inner = self._Inner()
        cred = _CaeCredential(inner)
        cred.get_token("https://storage.azure.com/.default")
        cred.get_token("https://vault.azure.net/.default", enable_cae=False, tenant_id="t")
        cred.get_token_info("https://storage.azure.com/.default")
        cred.get_token_info("https://vault.azure.net/.default", options={"tenant_id": "t"})
        assert inner.calls == [
            ("get_token", ("https://storage.azure.com/.default",), {"enable_cae": True}),
            ("get_token", ("https://vault.azure.net/.default",), {"enable_cae": True, "tenant_id": "t"}),
            ("get_token_info", ("https://storage.azure.com/.default",), {"enable_cae": True}),
            ("get_token_info", ("https://vault.azure.net/.default",), {"tenant_id": "t", "enable_cae": True}),
        ]


class TestTenantDiscovery:
    def test_reads_tenant_from_www_authenticate(self):
        import urllib.error
        from email.message import Message

        from claude_swap.team import auth

        tenant = "72f988bf-86f1-41af-91ab-2d7cd011db47"
        headers = Message()
        headers["WWW-Authenticate"] = (
            f'Bearer authorization_uri="https://login.windows.net/{tenant}", error="invalid_token"'
        )
        err = urllib.error.HTTPError("u", 401, "Unauthorized", headers, None)
        with patch.object(auth.urllib.request, "urlopen", side_effect=err):
            assert auth.discover_tenant("00000000-0000-0000-0000-000000000001") == tenant

    def test_rejects_non_guid(self):
        from claude_swap.exceptions import TeamError
        from claude_swap.team import auth

        with pytest.raises(TeamError, match="GUID"):
            auth.discover_tenant("my-sub")
