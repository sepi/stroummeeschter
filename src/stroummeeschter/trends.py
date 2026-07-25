"""Bucket boundaries for the long-term trends chart (daily/weekly/monthly/
yearly bars) - the same "which local window are we in" logic as
chart_cli.day_window, generalized to coarser, calendar-aware periods.

Kept separate from chart_cli.py's day_window (rather than folded into a
single generic function) since day/week are fixed-size steps but month/year
aren't (months vary in length) - the stepping logic genuinely differs per
period, not just the step size.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from stroummeeschter.chart import LOCAL_TZ

PERIODS = ("day", "week", "month", "year")

# How many buckets to show by default, per period - enough to see a trend
# without the bar chart turning into an unreadable wall of bars.
DEFAULT_COUNT = {"day": 30, "week": 12, "month": 12, "year": 5}

_LABEL_FORMAT = {"day": "%b %d", "week": "%b %d", "month": "%b %Y", "year": "%Y"}


def _step(dt: datetime, period: str, n: int) -> datetime:
    """Move a bucket-start `dt` forward/back by `n` buckets of `period`.
    Only ever called on actual bucket starts (day=1 for month/year), so the
    replace() calls below never land on an invalid day-of-month."""
    if period == "day":
        return dt + timedelta(days=n)
    if period == "week":
        return dt + timedelta(days=7 * n)
    if period == "month":
        month0 = dt.month - 1 + n
        return dt.replace(year=dt.year + month0 // 12, month=month0 % 12 + 1)
    if period == "year":
        return dt.replace(year=dt.year + n)
    raise ValueError(f"unknown period {period!r}, expected one of {PERIODS}")


def _current_bucket_start(period: str, day_start_hour: int, now_local: datetime) -> datetime:
    if period == "day":
        start = now_local.replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
    elif period == "week":
        monday = now_local - timedelta(days=now_local.weekday())
        start = monday.replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now_local.replace(day=1, hour=day_start_hour, minute=0, second=0, microsecond=0)
    elif period == "year":
        start = now_local.replace(month=1, day=1, hour=day_start_hour, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unknown period {period!r}, expected one of {PERIODS}")

    if now_local < start:
        start = _step(start, period, -1)
    return start


def trend_buckets(
    period: str, count: int, day_start_hour: int = 6, now: datetime | None = None
) -> list[tuple[str, str, str]]:
    """Return `count` (label, since_iso_utc, until_iso_utc) buckets of
    `period`, oldest first, ending with the bucket containing "now" - which
    may be a still-in-progress period (today so far, this week so far, ...),
    same convention as chart_cli.day_window."""
    if period not in PERIODS:
        raise ValueError(f"unknown period {period!r}, expected one of {PERIODS}")

    now = now or datetime.now(timezone.utc)
    now_local = now.astimezone(LOCAL_TZ)
    latest_start = _current_bucket_start(period, day_start_hour, now_local)
    label_fmt = _LABEL_FORMAT[period]

    buckets = []
    for i in range(count):
        start = _step(latest_start, period, -(count - 1 - i))
        end = _step(start, period, 1)
        buckets.append(
            (
                start.strftime(label_fmt),
                start.astimezone(timezone.utc).isoformat(timespec="seconds"),
                end.astimezone(timezone.utc).isoformat(timespec="seconds"),
            )
        )
    return buckets
