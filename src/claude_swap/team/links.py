"""Linked slots: a team account whose usage comes from the admin's own login.

The admin who publishes a setup-token for an account usually already holds
that account's normal login (``/login``, with a refresh token), which reads
usage for free. Instead of adding a second, setup-token slot for the same
account, ``add-token --into`` links the existing slot: its record gets
``teamLink: <team>``, and ``team/usage.py`` publishes its readings so the
team's setup-token slots show usage without a probe.

A linked slot is the admin's own account, never the team's: ``team sync``,
``team remove`` and ``team leave`` only ever unlink it, and ``export`` keeps
it. Only presence and the watch treat it as being on the team account.
"""

from __future__ import annotations

from claude_swap import oauth
from claude_swap.exceptions import ValidationError

LINK_FIELD = "teamLink"


def record_team(record: dict | None) -> str:
    """The team a record is on for presence and grouping: its own or its link."""
    if not record:
        return ""
    return record.get("team") or record.get(LINK_FIELD) or ""


def linked_slots(seq_data: dict, team: str) -> dict[str, tuple[str, dict]]:
    """``{email: (slot, record)}`` of the slots linked to ``team``."""
    return {
        (rec.get("email") or "").lower(): (num, rec)
        for num, rec in (seq_data.get("accounts") or {}).items()
        if rec.get(LINK_FIELD) == team and not rec.get("team")
    }


def linked_emails(seq_data: dict, team: str) -> set[str]:
    return set(linked_slots(seq_data, team))


def _is_refreshable_login(switcher, number: str, record: dict) -> bool:
    """Whether a slot holds a normal OAuth login (one that reads usage for free)."""
    if record.get("kind") == "api_key" or record.get("team"):
        return False
    try:
        creds = switcher._read_account_credentials(number, record.get("email", ""))
    except Exception:  # noqa: BLE001 — an unreadable slot is just not a candidate
        return False
    data = oauth.extract_oauth_data(creds) if creds else None
    return bool(data and data.get("refreshToken")) and not oauth.is_setup_token(data)


def find_link_target(switcher, email: str, slot: str | int | None = None) -> str | None:
    """The local slot ``add-token --into`` should link instead of adding one.

    ``slot`` given: that slot, which must be a normal login of ``email``.
    Otherwise the one normal login of ``email`` here, None when there is none
    (the caller then adds a setup-token slot as before), and an error when
    there are several (one per organization) so the caller picks with --slot.
    """
    email = email.lower()
    accounts = (switcher._get_sequence_data() or {}).get("accounts") or {}
    if slot is not None:
        number = str(slot)
        record = accounts.get(number)
        if not record or (record.get("email") or "").lower() != email:
            raise ValidationError(f"Slot {number} is not an account of {email}")
        if not _is_refreshable_login(switcher, number, record):
            raise ValidationError(
                f"Slot {number} is not a normal login of {email} (a setup-token, an "
                "API key or a team slot cannot read usage for the team)"
            )
        return number
    candidates = [
        number for number, record in sorted(accounts.items(), key=lambda kv: int(kv[0]))
        if (record.get("email") or "").lower() == email
        and _is_refreshable_login(switcher, number, record)
    ]
    if len(candidates) > 1:
        raise ValidationError(
            f"{email} has several logins here (slots {', '.join(candidates)}): "
            "pick the one to link with --slot"
        )
    return candidates[0] if candidates else None


def set_link(switcher, number: str, team: str | None) -> None:
    """Link slot ``number`` to ``team``, or unlink it (``team=None``)."""
    data = switcher._get_sequence_data() or {}
    record = (data.get("accounts") or {}).get(str(number))
    if record is None:
        return
    if team:
        if record.get(LINK_FIELD) == team:
            return
        record[LINK_FIELD] = team
    else:
        if LINK_FIELD not in record:
            return
        record.pop(LINK_FIELD)
    switcher._write_json(switcher.sequence_file, data)
