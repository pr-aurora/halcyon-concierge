"""Clock helpers. Members and venues are in London, timestamps are stored in UTC."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(moment: datetime | None = None) -> str:
    """ISO 8601 timestamp in UTC, for example 2026-11-16T09:12:03Z."""
    moment = moment or utc_now()
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def london_now() -> datetime:
    return utc_now().astimezone(LONDON)


def london_today() -> date:
    return london_now().date()
