"""CLI for the team feature: ``login az``, ``logout az``, ``keyvault`` and ``team``.

Pre-dispatched from ``cli.main`` like ``config``: each command must come
first on the command line. The Azure SDK is imported only inside the
handlers (through ``claude_swap.team``), so a base install keeps working and
reports the missing ``claude-swap[azure]`` extra only when one runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from claude_swap import paths
from claude_swap.exceptions import ClaudeSwitchError, TeamError
from claude_swap.json_output import error_envelope
from claude_swap.printer import accent, dimmed, error, muted, warning


def _run(fn, *, json_mode: bool = False) -> None:
    """Run a handler body with the CLI's uniform error and Ctrl-C handling."""
    try:
        fn()
    except ClaudeSwitchError as e:
        if json_mode:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}", file=sys.stderr)
        sys.exit(130)


def _set_team_settings(**values: str) -> None:
    from claude_swap.settings import set_setting

    root = paths.get_backup_root()
    for key, value in values.items():
        if value:
            set_setting(root, f"team.{key}", value)


def _vault_check(vault_url: str) -> None:
    """List one page of the vault, to fail at login rather than at first use."""
    from claude_swap.team.stores import get_backend

    get_backend().secret_store(vault_url).list_secrets()


def login_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="cswap login",
        description="Sign in to Entra ID for team accounts (Azure Key Vault).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap login az --subscription-id <id> --keyvault stw-team-kv
  cswap login az --tenant-id <tenant> --keyvault https://stw-team-kv.vault.azure.net
  cswap login az --subscription-id <id> --keyvault stw-team-kv --device-code   # no browser here
        """,
    )
    parser.add_argument("provider", choices=["az"], help="Identity provider (az = Entra ID)")
    parser.add_argument("--subscription-id", metavar="ID", help="Azure subscription; its tenant is used")
    parser.add_argument("--tenant-id", metavar="ID", help="Entra ID tenant (skips the subscription lookup)")
    parser.add_argument("--keyvault", metavar="NAME|URL", help="Team Key Vault to use by default")
    parser.add_argument("--device-code", action="store_true", help="Sign in with a device code instead of a browser")
    parser.add_argument(
        "--allow-unencrypted-cache",
        action="store_true",
        help="Store the Entra ID token cache unencrypted when no OS keyring is available (Linux)",
    )
    args = parser.parse_args(argv)
    if not args.tenant_id and not args.subscription_id:
        parser.error("pass --subscription-id or --tenant-id")

    def body():
        from claude_swap.settings import load_team_settings
        from claude_swap.team import auth, naming

        auth.require_azure()
        root = paths.get_backup_root()
        vault = naming.vault_url(args.keyvault) if args.keyvault else None
        tenant = args.tenant_id or auth.discover_tenant(args.subscription_id)
        print(dimmed(f"Signing in to tenant {tenant}…"))
        user = auth.login(
            root,
            tenant_id=tenant,
            device_code=args.device_code,
            allow_unencrypted=args.allow_unencrypted_cache,
        )
        _set_team_settings(tenantId=tenant, subscriptionId=args.subscription_id or "", vaultUrl=vault or "")
        print(f"{accent('Signed in')} as {user}")
        vault = vault or load_team_settings(root).vault_url
        if vault:
            _vault_check(vault)
            print(f"Key Vault {vault} {muted('(access ok)')}")
        else:
            print(muted("No Key Vault set yet: cswap keyvault <name>"))

    _run(body)


def logout_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="cswap logout", description="Forget the Entra ID sign-in.")
    parser.add_argument("provider", choices=["az"], help="Identity provider (az = Entra ID)")
    parser.parse_args(argv)

    def body():
        from claude_swap.team import auth

        if auth.logout(paths.get_backup_root()):
            print(f"{accent('Signed out')} of Entra ID")
        else:
            print(muted("Not signed in"))

    _run(body)


def keyvault_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="cswap keyvault",
        description="Show or set the default team Key Vault (also set by 'cswap login az --keyvault').",
    )
    parser.add_argument("vault", nargs="?", metavar="NAME|URL", help="Vault name or https:// URL")
    args = parser.parse_args(argv)

    def body():
        from claude_swap.settings import load_team_settings
        from claude_swap.team import naming

        if args.vault is None:
            current = load_team_settings(paths.get_backup_root()).vault_url
            print(current or muted("No Key Vault set: cswap keyvault <name>"))
            return
        url = naming.vault_url(args.vault)
        _vault_check(url)
        _set_team_settings(vaultUrl=url)
        print(f"Key Vault {url} {muted('(access ok)')}")

    _run(body)


def _ago(ts: float, now: float) -> str:
    seconds = max(0, int(now - ts))
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def _print_report(report) -> None:
    if not report.changed and not report.skipped:
        print(f"Team {report.team}: {muted('up to date')}")
        return
    print(f"Team {report.team}:")
    for email in report.added:
        print(f"  {accent('+')} {email}")
    for email in report.updated:
        note = " (re-activated)" if email in report.reactivated else ""
        print(f"  {accent('~')} {email} {muted('token updated' + note)}")
    for email in report.removed:
        print(f"  {accent('-')} {email}")
    for email in report.unlinked:
        print(f"  {accent('-')} {email} {muted('left the vault: your login stays, its usage is no longer shared')}")
    for line in report.skipped:
        warning(f"  ! {line}")


def team_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="cswap team",
        description=(
            "Share setup-token / API-key accounts with a team through Azure Key Vault "
            "(one vault per team) and see who is on which account. See docs/team.md."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap team register xpto --storage https://stwteam.table.core.windows.net
  cswap add-token sk-ant-oat01-... --email shared1@example.com --into xpto
  cswap team join xpto
  cswap team sync
  cswap team status
  cswap team remove xpto shared1@example.com
  cswap team leave xpto
        """,
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output (sync, status, list)")
    sub = parser.add_subparsers(dest="action", metavar="{register,join,sync,status,list,remove,leave}")

    p_reg = sub.add_parser("register", help="Create a team in the Key Vault")
    p_reg.add_argument("team")
    p_reg.add_argument("--storage", required=True, metavar="URL", help="Table Storage endpoint for presence")
    p_reg.add_argument("--table", default=None, metavar="NAME", help="Presence table (default cswappresence)")
    p_reg.add_argument("--keyvault", metavar="NAME|URL", help="Vault (default: the one from 'cswap keyvault')")
    p_reg.add_argument("--force", action="store_true", help="Overwrite an existing manifest")

    p_join = sub.add_parser("join", help="Join a team and pull its accounts")
    p_join.add_argument("team")
    p_join.add_argument("--keyvault", metavar="NAME|URL", help="Vault (default: the one from 'cswap keyvault')")

    p_sync = sub.add_parser("sync", help="Pull account changes from the vault")
    p_sync.add_argument("team", nargs="?")

    p_status = sub.add_parser("status", help="Who is on which team account")
    p_status.add_argument("team", nargs="?")

    sub.add_parser("list", help="Teams joined on this machine")

    p_rm = sub.add_parser("remove", help="Delete an account from the team's vault")
    p_rm.add_argument("team")
    p_rm.add_argument("email")

    p_leave = sub.add_parser("leave", help="Remove this team's accounts from this machine")
    p_leave.add_argument("team")

    for p in (p_sync, p_status):
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help()
        sys.exit(2)

    def body():
        from claude_swap.settings import load_team_settings
        from claude_swap.switcher import ClaudeAccountSwitcher
        from claude_swap.team import naming, registry, service, sync

        switcher = ClaudeAccountSwitcher()
        root = switcher.backup_dir

        def vault_arg(value):
            vault = value or load_team_settings(root).vault_url
            if not vault:
                raise TeamError("No Key Vault: pass --keyvault or run 'cswap keyvault <name>'")
            return naming.vault_url(vault)

        if args.action == "register":
            team = naming.validate_team_name(args.team)
            service.register(
                switcher, team,
                vault_url=vault_arg(args.keyvault),
                table_endpoint=args.storage,
                table=args.table or naming.DEFAULT_TABLE,
                force=args.force,
            )
            print(f"{accent('Registered')} team {team}")
            print(muted(f"Add accounts with: cswap add-token <token> --email <email> --into {team}"))
        elif args.action == "join":
            report = service.join(switcher, args.team, vault_url=vault_arg(args.keyvault))
            print(f"{accent('Joined')} team {report.team}")
            _print_report(report)
        elif args.action == "sync":
            if args.team:
                reports, errors = [sync.sync_team(switcher, naming.validate_team_name(args.team))], {}
            else:
                reports, errors = sync.sync_all(switcher)
                if not reports and not errors:
                    raise TeamError("No team joined on this machine: cswap team join <team>")
            from claude_swap.team.usage import exchange

            exchange(switcher)  # share/adopt usage readings too (best-effort)
            if args.json:
                print(json.dumps({
                    "schemaVersion": 1,
                    "teams": [r.to_json() for r in reports],
                    "errors": errors,
                }, indent=2))
            else:
                for report in reports:
                    _print_report(report)
                for team, message in errors.items():
                    warning(f"Team {team}: {message}")
            if errors:
                sys.exit(1)
        elif args.action == "status":
            data = service.status(switcher, args.team)
            if args.json:
                print(json.dumps({"schemaVersion": 1, "teams": data}, indent=2))
                return
            if not data:
                raise TeamError("No team joined on this machine: cswap team join <team>")
            now = datetime.now().timestamp()
            for team, info in data.items():
                print(accent(f"Team {team}"))
                if info["error"]:
                    warning(f"  presence unavailable: {info['error']}")
                    continue
                if not info["people"]:
                    print(muted("  nobody yet"))
                for person in info["people"]:
                    account = person["account"] or muted("(not on a team account)")
                    seen = _ago(person["lastSeen"], now)
                    line = f"  {person['user']:<32} {account}  {muted(person['machine'] + ' · ' + seen)}"
                    print(dimmed(line) if person["away"] else line)
        elif args.action == "list":
            teams = registry.load(root)
            if args.json:
                print(json.dumps({"schemaVersion": 1, "teams": teams}, indent=2))
                return
            if not teams:
                print(muted("No team joined on this machine"))
            for team, entry in teams.items():
                print(f"{team}  {muted(entry['vaultUrl'])}")
        elif args.action == "remove":
            report = service.remove_account(switcher, args.team, args.email.strip().lower())
            print(f"{accent('Removed')} {args.email} from team {report.team}")
            _print_report(report)
        elif args.action == "leave":
            removed = service.leave(switcher, args.team)
            print(f"{accent('Left')} team {args.team}" + (muted(f" ({len(removed)} account(s) removed)") if removed else ""))

    _run(body, json_mode=bool(getattr(args, "json", False)))
