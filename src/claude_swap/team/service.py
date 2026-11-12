"""Team operations behind the CLI: register, join, publish, remove, leave, status."""

from __future__ import annotations

import getpass
import sys
import time

from claude_swap.credentials import looks_like_api_key
from claude_swap.exceptions import ClaudeSwitchError, TeamError, ValidationError
from claude_swap.models import get_timestamp
from claude_swap.team import links, naming, presence, registry
from claude_swap.team.stores import get_backend
from claude_swap.team.sync import SyncReport, _quiet, sync_team


def register(
    switcher, team: str, *, vault_url: str, table_endpoint: str, table: str, force: bool = False
) -> None:
    """Create a team in ``vault_url``: manifest secret, presence table, local entry."""
    team = naming.validate_team_name(team)
    vault_url = naming.vault_url(vault_url)
    table_endpoint = naming.table_endpoint(table_endpoint)
    table = naming.validate_table_name(table)
    backend = get_backend()
    user = backend.current_user()
    store = backend.secret_store(vault_url)
    name = naming.manifest_secret_name(team)
    if store.get_secret(name) is not None and not force:
        raise TeamError(
            f"Team '{team}' already exists in this vault: use 'cswap team join {team}', "
            "or --force to overwrite its manifest"
        )
    backend.presence_store(table_endpoint, table).ensure_table()
    store.set_secret(
        name,
        naming.build_manifest(team, table_endpoint, table, user, get_timestamp()),
        tags=naming.manifest_tags(team),
        content_type=naming.MANIFEST_CONTENT_TYPE,
    )
    registry.put(switcher.backup_dir, team, vault_url=vault_url, table_endpoint=table_endpoint, table=table)


def join(switcher, team: str, *, vault_url: str) -> SyncReport:
    team = naming.validate_team_name(team)
    vault_url = naming.vault_url(vault_url)
    store = get_backend().secret_store(vault_url)
    manifest = store.get_secret(naming.manifest_secret_name(team))
    if manifest is None:
        raise TeamError(f"Team '{team}' was not found in {vault_url} (no manifest secret)")
    storage = naming.parse_manifest(team, manifest[0])
    registry.put(
        switcher.backup_dir, team, vault_url=vault_url,
        table_endpoint=storage["tableEndpoint"], table=storage["table"],
    )
    report = sync_team(switcher, team)
    presence.publish(switcher, [team])
    return report


def read_token(token: str) -> str:
    """The token argument as ``add-token`` reads it: ``-`` is stdin, empty prompts."""
    if token == "-":
        token = sys.stdin.readline().rstrip("\n")
    elif not token:
        token = getpass.getpass("Token: ")
    token = token.strip()
    if not token:
        raise ValidationError("Token cannot be empty")
    return token


def publish_account(
    switcher, team: str, token: str, email: str | None, *, slot: str | int | None = None
) -> str | None:
    """``add-token --into``: write the vault secret first, then this machine.

    When this machine already holds a normal login of ``email`` (``slot``
    picks it when there are several), a setup-token is not added as a second
    slot: that login is linked to the team instead (``team/links.py``) and
    publishes the account's usage, read for free, to the members. Otherwise a
    team slot is added as before. Returns the linked slot, or None.

    Everything that can refuse runs before the vault write, and a failed vault
    write leaves nothing local, so this machine never claims a team account
    the vault doesn't know about.
    """
    team = naming.validate_team_name(team)
    if not email:
        raise ValidationError(
            "--into needs --email: a setup-token or API key does not say which account it is"
        )
    email = email.strip().lower()
    if not switcher._validate_email(email):
        raise ValidationError(f"Invalid email format: {email}")
    entry = registry.get(switcher.backup_dir, team)
    token = read_token(token)
    kind = "api_key" if looks_like_api_key(token) else "setup_token"
    link = None
    if kind == "setup_token":
        link = links.find_link_target(switcher, email, slot)
    elif slot is not None:
        raise ValidationError("--slot with --into links a login to a setup-token; this is an API key")
    if link is None:
        switcher._reject_cross_team_collision(email, team)
    store = get_backend().secret_store(entry["vaultUrl"])
    revision = store.set_secret(
        naming.account_secret_name(team, email),
        naming.build_account_payload(email, kind, token),
        tags=naming.account_tags(team, email),
        content_type=naming.ACCOUNT_CONTENT_TYPE,
    )
    if link is not None:
        links.set_link(switcher, link, team)
        print(f"Published {email} to team {team}.")
        print(
            f"Linked to slot {link}, your login of {email}: no second slot is added here, "
            "and its usage is shared with the team."
        )
        print(
            "  cswap cannot check that the setup-token belongs to this same account; "
            "it trusts --email."
        )
        return link
    switcher.add_account_from_token(
        token=token, email=email, team=team, team_revision=revision, assume_yes=True
    )
    return None


def remove_account(switcher, team: str, email: str) -> SyncReport:
    """Delete an account from the team's vault, then sync this machine."""
    team = naming.validate_team_name(team)
    entry = registry.get(switcher.backup_dir, team)
    store = get_backend().secret_store(entry["vaultUrl"])
    name = naming.account_secret_name(team, email)
    if store.get_secret(name) is None:
        raise TeamError(f"{email} is not an account of team '{team}'")
    store.delete_secret(name)
    return sync_team(switcher, team)


def leave(switcher, team: str) -> list[str]:
    """Remove this team's local slots and forget the team. Returns the removed emails."""
    team = naming.validate_team_name(team)
    registry.get(switcher.backup_dir, team)
    data = switcher._get_sequence_data() or {}
    removed: list[str] = []
    for number, rec in sorted((data.get("accounts") or {}).items(), key=lambda kv: int(kv[0])):
        if rec.get("team") != team:
            continue
        current = (switcher._get_sequence_data() or {}).get("activeAccountNumber")
        if current is not None and str(current) == number:
            try:
                _quiet(switcher.switch, json_output=True)
            except ClaudeSwitchError:
                pass  # nothing else to move to; the slot goes anyway
        _quiet(switcher.remove_account, number, assume_yes=True)
        removed.append(rec.get("email", ""))
    # A linked slot is this machine's own login: it stays, only the link goes.
    for _email, (number, _rec) in links.linked_slots(switcher._get_sequence_data() or {}, team).items():
        links.set_link(switcher, number, None)
    # Say goodbye in the table so this machine stops counting on the team.
    presence.publish(switcher, [team])
    registry.remove(switcher.backup_dir, team)
    return removed


def status(switcher, team: str | None) -> dict:
    """Presence rows per team (fresh read), with the away flag computed."""
    from claude_swap.settings import load_team_settings

    joined = registry.load(switcher.backup_dir)
    if team is not None:
        team = naming.validate_team_name(team)
        registry.get(switcher.backup_dir, team)
        joined = {team: joined[team]}
    stale = load_team_settings(switcher.backup_dir).presence_stale_seconds
    data = presence.read_all(switcher)
    now = time.time()
    out = {}
    for name in joined:
        rows = sorted(data.rows.get(name, []), key=lambda r: (r.account_email == "", r.account_email, r.user))
        out[name] = {
            "error": data.errors.get(name),
            "people": [
                {
                    "user": r.user,
                    "machine": r.machine,
                    "account": r.account_email or None,
                    "since": r.since,
                    "lastSeen": r.last_seen,
                    "away": presence.is_away(r, now, stale),
                }
                for r in rows
            ],
        }
    return out
