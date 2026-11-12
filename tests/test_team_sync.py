"""Team sync, register/join, add-token --into, remove, leave and export."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.exceptions import TeamError, TransferError, ValidationError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.team import naming, registry, service, sync
from claude_swap.transfer import export_accounts

VAULT = "https://kv-xpto.vault.azure.net/"
ENDPOINT = "https://acc.table.core.windows.net"
TABLE = "cswappresence"
TOKEN_A = "sk-ant-oat01-" + "a" * 40
TOKEN_A2 = "sk-ant-oat01-" + "b" * 40
TOKEN_B = "sk-ant-oat01-" + "c" * 40
API_KEY = "sk-ant-api03-" + "k" * 40


def _switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _register(s, team="xpto"):
    service.register(s, team, vault_url=VAULT, table_endpoint=ENDPOINT, table=TABLE)


def _put(backend, email, token, team="xpto", kind="setup_token"):
    return backend.vault(VAULT).set_secret(
        naming.account_secret_name(team, email),
        naming.build_account_payload(email, kind, token),
        tags=naming.account_tags(team, email),
        content_type=naming.ACCOUNT_CONTENT_TYPE,
    )


def _token_of(s, email) -> str:
    seq = s._get_sequence_data()
    num = next(n for n, r in seq["accounts"].items() if r["email"] == email)
    creds = s._read_account_credentials(num, email)
    try:
        return json.loads(creds)["claudeAiOauth"]["accessToken"]
    except ValueError:
        return creds


def _record(s, email) -> tuple[str, dict]:
    seq = s._get_sequence_data()
    return next((n, r) for n, r in seq["accounts"].items() if r["email"] == email)


class TestRegisterJoin:
    def test_register_writes_manifest_table_and_registry(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        secret = team_backend.vault(VAULT).secrets["cswap-xpto"]
        assert secret["tags"] == {"cswap-kind": "manifest", "cswap-team": "xpto"}
        manifest = json.loads(secret["value"])
        assert manifest["storage"] == {"tableEndpoint": ENDPOINT, "table": TABLE}
        assert manifest["createdBy"] == team_backend.user
        assert team_backend.table(ENDPOINT, TABLE).table_ensured
        assert registry.get(s.backup_dir, "xpto")["vaultUrl"] == VAULT

    def test_register_twice_needs_force(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        with pytest.raises(TeamError, match="already exists"):
            _register(s)
        service.register(s, "xpto", vault_url=VAULT, table_endpoint=ENDPOINT, table=TABLE, force=True)

    def test_join_reads_manifest_and_pulls_accounts(self, temp_home: Path, team_backend):
        admin = _switcher()
        _register(admin)
        _put(team_backend, "one@example.com", TOKEN_A)
        _put(team_backend, "two@example.com", API_KEY, kind="api_key")
        registry.remove(admin.backup_dir, "xpto")  # now act as a fresh member

        report = service.join(admin, "xpto", vault_url=VAULT)
        assert sorted(report.added) == ["one@example.com", "two@example.com"]
        num, rec = _record(admin, "one@example.com")
        assert rec["team"] == "xpto" and rec["teamSecretRevision"]
        assert _token_of(admin, "one@example.com") == TOKEN_A
        assert admin._account_kind(_record(admin, "two@example.com")[0]) == "api_key"
        entry = registry.get(admin.backup_dir, "xpto")
        assert (entry["tableEndpoint"], entry["table"]) == (ENDPOINT, TABLE)

    def test_join_unknown_team(self, temp_home: Path, team_backend):
        with pytest.raises(TeamError, match="not found"):
            service.join(_switcher(), "nope", vault_url=VAULT)


class TestSync:
    def test_unchanged_secrets_are_not_read_again(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        _put(team_backend, "one@example.com", TOKEN_A)
        sync.sync_team(s, "xpto")
        store = team_backend.vault(VAULT)
        store.get_calls.clear()
        report = sync.sync_team(s, "xpto")
        assert not report.changed
        assert store.get_calls == ["cswap-xpto"]  # the manifest only

    def test_changed_token_updates_the_slot(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        _put(team_backend, "one@example.com", TOKEN_A)
        sync.sync_team(s, "xpto")
        num_before, _ = _record(s, "one@example.com")
        _put(team_backend, "one@example.com", TOKEN_A2)
        report = sync.sync_team(s, "xpto")
        assert report.updated == ["one@example.com"]
        assert _token_of(s, "one@example.com") == TOKEN_A2
        assert _record(s, "one@example.com")[0] == num_before

    def test_changed_token_of_the_active_account_is_reactivated(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        _put(team_backend, "one@example.com", TOKEN_A)
        sync.sync_team(s, "xpto")
        num, _ = _record(s, "one@example.com")
        seq = s._get_sequence_data()
        seq["activeAccountNumber"] = int(num)
        s._write_json(s.sequence_file, seq)
        _put(team_backend, "one@example.com", TOKEN_A2)
        with patch.object(s, "switch_to") as switch_to:
            report = sync.sync_team(s, "xpto")
        switch_to.assert_called_once_with(num, json_output=True, force=True)
        assert report.reactivated == ["one@example.com"]

    def test_account_gone_from_vault_is_removed(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        _put(team_backend, "one@example.com", TOKEN_A)
        _put(team_backend, "two@example.com", TOKEN_B)
        sync.sync_team(s, "xpto")
        team_backend.vault(VAULT).delete_secret(naming.account_secret_name("xpto", "two@example.com"))
        report = sync.sync_team(s, "xpto")
        assert report.removed == ["two@example.com"]
        emails = [r["email"] for r in s._get_sequence_data()["accounts"].values()]
        assert emails == ["one@example.com"]

    def test_active_account_gone_switches_away_first(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        _put(team_backend, "one@example.com", TOKEN_A)
        sync.sync_team(s, "xpto")
        num, _ = _record(s, "one@example.com")
        seq = s._get_sequence_data()
        seq["activeAccountNumber"] = int(num)
        s._write_json(s.sequence_file, seq)
        team_backend.vault(VAULT).delete_secret(naming.account_secret_name("xpto", "one@example.com"))
        calls = []
        with patch.object(s, "switch", side_effect=lambda **kw: calls.append(kw)):
            report = sync.sync_team(s, "xpto")
        assert calls == [{"json_output": True}]
        assert report.removed == ["one@example.com"]

    def test_personal_account_with_the_same_email_is_left_alone(self, temp_home: Path, team_backend):
        s = _switcher()
        s.add_account_from_token(TOKEN_B, email="one@example.com")
        _register(s)
        _put(team_backend, "one@example.com", TOKEN_A)
        report = sync.sync_team(s, "xpto")
        assert report.added == []
        assert report.skipped and "personal" in report.skipped[0]
        assert _token_of(s, "one@example.com") == TOKEN_B
        assert "team" not in _record(s, "one@example.com")[1]

    def test_secret_naming_another_email_is_skipped(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        team_backend.vault(VAULT).set_secret(
            naming.account_secret_name("xpto", "one@example.com"),
            naming.build_account_payload("evil@example.com", "setup_token", TOKEN_A),
            tags=naming.account_tags("xpto", "one@example.com"),
            content_type=naming.ACCOUNT_CONTENT_TYPE,
        )
        report = sync.sync_team(s, "xpto")
        assert report.added == [] and report.skipped

    def test_other_teams_secrets_are_ignored(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        _put(team_backend, "x@example.com", TOKEN_A, team="other")
        assert sync.sync_team(s, "xpto").added == []

    def test_sync_all_reports_errors_per_team(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        team_backend.offline = True
        reports, errors = sync.sync_all(s, fast=True)
        assert reports == [] and "xpto" in errors
        assert team_backend.fast_requests == [VAULT]


class TestPublish:
    def test_into_writes_vault_then_local(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        service.publish_account(s, "xpto", TOKEN_A, "One@Example.com")
        name = naming.account_secret_name("xpto", "one@example.com")
        secret = team_backend.vault(VAULT).secrets[name]
        assert json.loads(secret["value"])["token"] == TOKEN_A
        assert secret["tags"]["cswap-email"] == "one@example.com"
        num, rec = _record(s, "one@example.com")
        assert rec["team"] == "xpto" and rec["teamSecretRevision"] == secret["revision"]
        # the admin's own next sync has nothing to do
        assert not sync.sync_team(s, "xpto").changed

    def test_into_needs_email(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        with pytest.raises(ValidationError, match="--email"):
            service.publish_account(s, "xpto", TOKEN_A, None)

    def test_vault_failure_leaves_nothing_local(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        team_backend.read_only = True
        with pytest.raises(TeamError):
            service.publish_account(s, "xpto", TOKEN_A, "one@example.com")
        assert s._get_sequence_data()["accounts"] == {}

    def test_personal_email_is_refused_before_the_vault(self, temp_home: Path, team_backend):
        s = _switcher()
        s.add_account_from_token(TOKEN_B, email="one@example.com")
        _register(s)
        with pytest.raises(ValidationError, match="personal"):
            service.publish_account(s, "xpto", TOKEN_A, "one@example.com")
        assert naming.account_secret_name("xpto", "one@example.com") not in team_backend.vault(VAULT).secrets

    def test_unjoined_team(self, temp_home: Path, team_backend):
        with pytest.raises(TeamError, match="team join"):
            service.publish_account(_switcher(), "xpto", TOKEN_A, "one@example.com")

    def test_readding_a_removed_account_recovers_the_secret(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        service.publish_account(s, "xpto", TOKEN_A, "one@example.com")
        service.remove_account(s, "xpto", "one@example.com")
        service.publish_account(s, "xpto", TOKEN_A2, "one@example.com")
        name = naming.account_secret_name("xpto", "one@example.com")
        assert team_backend.vault(VAULT).recovered == [name]


class TestRemoveLeave:
    def test_remove_deletes_secret_and_local_slot(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        service.publish_account(s, "xpto", TOKEN_A, "one@example.com")
        report = service.remove_account(s, "xpto", "one@example.com")
        assert report.removed == ["one@example.com"]
        assert team_backend.vault(VAULT).secrets.keys() == {"cswap-xpto"}

    def test_remove_unknown(self, temp_home: Path, team_backend):
        s = _switcher()
        _register(s)
        with pytest.raises(TeamError, match="not an account"):
            service.remove_account(s, "xpto", "nobody@example.com")

    def test_leave_removes_team_slots_only(self, temp_home: Path, team_backend):
        s = _switcher()
        s.add_account_from_token(TOKEN_B, email="mine@example.com")
        _register(s)
        service.publish_account(s, "xpto", TOKEN_A, "one@example.com")
        removed = service.leave(s, "xpto")
        assert removed == ["one@example.com"]
        emails = [r["email"] for r in s._get_sequence_data()["accounts"].values()]
        assert emails == ["mine@example.com"]
        assert not registry.has_teams(s.backup_dir)
        # the vault keeps the account: leaving is local
        assert naming.account_secret_name("xpto", "one@example.com") in team_backend.vault(VAULT).secrets


class TestExport:
    def test_export_skips_team_accounts(self, temp_home: Path, team_backend, tmp_path, capsys):
        s = _switcher()
        s.add_account_from_token(TOKEN_B, email="mine@example.com")
        _register(s)
        service.publish_account(s, "xpto", TOKEN_A, "one@example.com")
        out = tmp_path / "backup.cswap"
        export_accounts(s, str(out))
        emails = [a["email"] for a in json.loads(out.read_text())["accounts"]]
        assert emails == ["mine@example.com"]
        assert "Skipping 1 team account" in capsys.readouterr().err

    def test_export_of_a_named_team_account_is_refused(self, temp_home: Path, team_backend, tmp_path):
        s = _switcher()
        _register(s)
        service.publish_account(s, "xpto", TOKEN_A, "one@example.com")
        with pytest.raises(TransferError, match="team"):
            export_accounts(s, str(tmp_path / "x.cswap"), account="one@example.com")
