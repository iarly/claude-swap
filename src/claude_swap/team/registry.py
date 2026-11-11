"""The local list of joined teams: ``<backup_dir>/teams.json``.

Shape: ``{"schemaVersion": 1, "teams": {<team>: {"vaultUrl", "tableEndpoint",
"table", "joinedAt"}}}``. Each team keeps its own vault, so one person can be
in several teams. The storage fields are copied from the vault manifest on
``register``/``join``/``sync``, so presence never needs the vault.
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_swap.exceptions import TeamError
from claude_swap.fsutil import atomic_write_text
from claude_swap.models import get_timestamp

FILENAME = "teams.json"


def path(backup_dir: Path) -> Path:
    return Path(backup_dir) / FILENAME


def has_teams(backup_dir: Path) -> bool:
    """Cheap check (one stat) used on every switch before anything else."""
    return path(backup_dir).exists()


def load(backup_dir: Path) -> dict[str, dict]:
    p = path(backup_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise TeamError(f"Could not read {p}: {e}") from None
    teams = data.get("teams") if isinstance(data, dict) else None
    return {k: v for k, v in (teams or {}).items() if isinstance(v, dict)}


def get(backup_dir: Path, team: str) -> dict:
    entry = load(backup_dir).get(team)
    if entry is None:
        raise TeamError(
            f"Not in team '{team}' on this machine. Run 'cswap team join {team}' "
            "(or 'cswap team register' to create it)."
        )
    return entry


def _save(backup_dir: Path, teams: dict[str, dict]) -> None:
    p = path(backup_dir)
    if not teams:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps({"schemaVersion": 1, "teams": teams}, indent=2))


def put(backup_dir: Path, team: str, *, vault_url: str, table_endpoint: str, table: str) -> None:
    teams = load(backup_dir)
    prior = teams.get(team, {})
    teams[team] = {
        "vaultUrl": vault_url,
        "tableEndpoint": table_endpoint,
        "table": table,
        "joinedAt": prior.get("joinedAt") or get_timestamp(),
    }
    _save(backup_dir, teams)


def remove(backup_dir: Path, team: str) -> None:
    teams = load(backup_dir)
    if teams.pop(team, None) is not None:
        _save(backup_dir, teams)
