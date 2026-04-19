"""Canonical business time helpers for wb-core runtime semantics."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


CANONICAL_BUSINESS_TIMEZONE_NAME = "Asia/Yekaterinburg"
CANONICAL_BUSINESS_TIMEZONE = ZoneInfo(CANONICAL_BUSINESS_TIMEZONE_NAME)
DAILY_REFRESH_BUSINESS_HOURS = (11, 20)
DAILY_REFRESH_SYSTEMD_UTC_TIMES = ("06:00:00 UTC", "15:00:00 UTC")
DAILY_REFRESH_SYSTEMD_UTC_ONCALENDAR_VALUES = tuple(
    f"*-*-* {value}" for value in DAILY_REFRESH_SYSTEMD_UTC_TIMES
)
DAILY_REFRESH_BUSINESS_HOUR = DAILY_REFRESH_BUSINESS_HOURS[0]
DAILY_REFRESH_SYSTEMD_UTC_TIME = ", ".join(DAILY_REFRESH_SYSTEMD_UTC_TIMES)
DAILY_REFRESH_SYSTEMD_UTC_ONCALENDAR = "; ".join(DAILY_REFRESH_SYSTEMD_UTC_ONCALENDAR_VALUES)


def to_business_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("business time helpers require a timezone-aware datetime")
    return value.astimezone(CANONICAL_BUSINESS_TIMEZONE)


def business_date_iso(value: datetime) -> str:
    return to_business_datetime(value).date().isoformat()


def current_business_date_iso(now: datetime | None = None) -> str:
    return business_date_iso(now or datetime.now(timezone.utc))


def default_business_as_of_date(now: datetime | None = None) -> str:
    return str(to_business_datetime(now or datetime.now(timezone.utc)).date() - timedelta(days=1))


def business_datetime_for_override(override_date: str) -> datetime:
    parsed = date.fromisoformat(override_date)
    return datetime.combine(parsed, time(hour=12), tzinfo=CANONICAL_BUSINESS_TIMEZONE)


def business_date_from_timestamp(timestamp: str) -> str:
    normalized = str(timestamp or "").strip()
    if not normalized:
        raise ValueError("timestamp must be a non-empty string")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return business_date_iso(datetime.fromisoformat(normalized))
