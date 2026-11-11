"""Team secret names, payloads and the local team registry."""

from __future__ import annotations

import json

import pytest

from claude_swap.exceptions import TeamError, ValidationError
from claude_swap.team import naming, registry


class TestTeamName:
    @pytest.mark.parametrize("name", ["xpto", "a", "team-1", "a" * 32, "Squad9"])
    def test_valid(self, name):
        assert naming.validate_team_name(name) == name.lower()

    @pytest.mark.parametrize(
        "name", ["", "-x", "x-", "a--b", "a_b", "a.b", "a" * 33, "ação"]
    )
    def test_invalid(self, name):
        with pytest.raises(ValidationError):
            naming.validate_team_name(name)


class TestSecretNames:
    def test_manifest(self):
        assert naming.manifest_secret_name("xpto") == "cswap-xpto"

    def test_account_name_shape(self):
        name = naming.account_secret_name("xpto", "Shared.One@Example.com")
        assert name.startswith("cswap-xpto--shared-one-example-com-")
        assert len(name.rsplit("-", 1)[1]) == 6
        assert all(c.isalnum() or c == "-" for c in name)

    def test_case_insensitive(self):
        assert naming.account_secret_name("t", "A@x.io") == naming.account_secret_name("t", "a@x.io")

    def test_emails_that_slug_alike_stay_distinct(self):
        assert naming.account_secret_name("t", "a.b@x.io") != naming.account_secret_name("t", "a-b@x.io")

    def test_long_email_fits_127(self):
        email = ("x" * 200) + "@example.com"
        name = naming.account_secret_name("a" * 32, email)
        assert len(name) <= 127
        assert name.startswith("cswap-" + "a" * 32 + "--")


class TestUrls:
    def test_vault_name_becomes_url(self):
        assert naming.vault_url("stw-team-kv") == "https://stw-team-kv.vault.azure.net/"

    def test_vault_url_kept(self):
        assert naming.vault_url("https://kv.vault.azure.cn/x") == "https://kv.vault.azure.cn/"

    @pytest.mark.parametrize("bad", ["", "ab", "1abc", "http://kv.vault.azure.net", "a_b_c"])
    def test_bad_vault(self, bad):
        with pytest.raises(ValidationError):
            naming.vault_url(bad)

    def test_table_endpoint(self):
        assert naming.table_endpoint("https://acc.table.core.windows.net/") == "https://acc.table.core.windows.net"
        with pytest.raises(ValidationError):
            naming.table_endpoint("acc.table.core.windows.net")


class TestPayloads:
    def test_manifest_round_trip(self):
        value = naming.build_manifest("xpto", "https://a.table.core.windows.net", "cswappresence", "me", "now")
        assert naming.parse_manifest("xpto", value) == {
            "tableEndpoint": "https://a.table.core.windows.net",
            "table": "cswappresence",
        }

    def test_manifest_of_another_team_is_refused(self):
        value = naming.build_manifest("other", "https://a.table.core.windows.net", "cswappresence", "me", "now")
        with pytest.raises(TeamError):
            naming.parse_manifest("xpto", value)

    def test_account_round_trip(self):
        value = naming.build_account_payload("A@x.io", "setup_token", "sk-ant-oat01-abc")
        assert naming.parse_account_payload(value) == {
            "email": "a@x.io", "kind": "setup_token", "token": "sk-ant-oat01-abc",
        }

    @pytest.mark.parametrize(
        "value",
        [
            "not json",
            json.dumps({"email": "a@x.io", "kind": "oauth", "token": "t"}),
            json.dumps({"email": "a@x.io", "kind": "setup_token", "token": ""}),
            json.dumps({"email": "nope", "kind": "setup_token", "token": "t"}),
        ],
    )
    def test_bad_account_payload(self, value):
        with pytest.raises(TeamError):
            naming.parse_account_payload(value)

    def test_presence_row_key_strips_forbidden(self):
        assert naming.presence_row_key("A/B@x", "PC#1?") == "a_b@x|pc_1_"


class TestRegistry:
    def test_put_get_remove(self, tmp_path):
        assert registry.has_teams(tmp_path) is False
        registry.put(tmp_path, "xpto", vault_url="https://kv/", table_endpoint="https://t", table="tb")
        entry = registry.get(tmp_path, "xpto")
        assert entry["vaultUrl"] == "https://kv/"
        joined = entry["joinedAt"]
        registry.put(tmp_path, "xpto", vault_url="https://kv/", table_endpoint="https://t2", table="tb")
        assert registry.get(tmp_path, "xpto")["joinedAt"] == joined
        registry.remove(tmp_path, "xpto")
        assert registry.has_teams(tmp_path) is False

    def test_unknown_team(self, tmp_path):
        with pytest.raises(TeamError, match="cswap team join"):
            registry.get(tmp_path, "nope")
