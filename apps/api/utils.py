"""Shared API utilities."""

from datetime import datetime, timezone, timedelta

# Beijing time (UTC+8)
_CST = timezone(timedelta(hours=8))


def fmt_cst(dt: datetime | None) -> str | None:
    """Format a datetime as Beijing time string (YYYY-MM-DD HH:MM:SS).

    If the datetime is naive (no timezone), treat it as UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_CST).strftime("%Y-%m-%d %H:%M:%S")
