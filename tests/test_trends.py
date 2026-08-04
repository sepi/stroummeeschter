from datetime import datetime, timezone

import pytest

from stroummeeschter.trends import trend_buckets


def test_quarter_hour_bucket_aligns_to_15min_marks():
    # 10:07 UTC = 12:07 CEST local (summer) -> floors to the 12:00-12:15 mark.
    now = datetime(2026, 7, 25, 10, 7, 0, tzinfo=timezone.utc)
    buckets = trend_buckets("quarter_hour", 1, now=now)
    _, since, until = buckets[0]
    assert since.startswith("2026-07-25T10:00:00")
    assert until.startswith("2026-07-25T10:15:00")


def test_hour_bucket_aligns_to_the_hour():
    now = datetime(2026, 7, 25, 10, 37, 0, tzinfo=timezone.utc)
    buckets = trend_buckets("hour", 1, now=now)
    _, since, until = buckets[0]
    assert since.startswith("2026-07-25T10:00:00")
    assert until.startswith("2026-07-25T11:00:00")


def test_hour_and_quarter_hour_ignore_day_start_hour():
    # day_start_hour is meaningless once the bucket is smaller than a day -
    # must not shift these away from the plain clock-aligned mark.
    now = datetime(2026, 7, 25, 10, 30, 0, tzinfo=timezone.utc)
    _, hour_since, _ = trend_buckets("hour", 1, day_start_hour=6, now=now)[0]
    assert hour_since.startswith("2026-07-25T10:00:00")
    _, qh_since, _ = trend_buckets("quarter_hour", 1, day_start_hour=6, now=now)[0]
    assert qh_since.startswith("2026-07-25T10:30:00")


def test_quarter_hour_and_hour_buckets_are_contiguous():
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    for period in ("quarter_hour", "hour"):
        buckets = trend_buckets(period, 5, now=now)
        assert len(buckets) == 5
        for (_, _, until_a), (_, since_b, _) in zip(buckets, buckets[1:]):
            assert until_a == since_b


def test_day_bucket_contains_now():
    # 2026-07-25 10:00 UTC is noon local (Europe/Luxembourg, summer) - well
    # past the 6am day-start-hour boundary, so "today" runs 6am-6am local.
    now = datetime(2026, 7, 25, 10, 0, 0, tzinfo=timezone.utc)
    buckets = trend_buckets("day", 1, now=now)
    assert len(buckets) == 1
    _, since, until = buckets[0]
    assert since.startswith("2026-07-25T04:00:00")  # 6am CEST == 4am UTC
    assert until.startswith("2026-07-26T04:00:00")


def test_day_bucket_rolls_back_before_boundary():
    # 2026-07-25 03:00 UTC is 5am local - before the 6am boundary, so this
    # is still "yesterday"'s bucket.
    now = datetime(2026, 7, 25, 3, 0, 0, tzinfo=timezone.utc)
    buckets = trend_buckets("day", 1, now=now)
    _, since, until = buckets[0]
    assert since.startswith("2026-07-24T04:00:00")
    assert until.startswith("2026-07-25T04:00:00")


def test_buckets_are_contiguous_and_oldest_first():
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    buckets = trend_buckets("day", 5, now=now)
    assert len(buckets) == 5
    for (_, _, until_a), (_, since_b, _) in zip(buckets, buckets[1:]):
        assert until_a == since_b
    # Oldest first: the last bucket's until must be the most recent boundary.
    assert buckets[0][1] < buckets[-1][1]


def test_week_bucket_starts_monday():
    # 2026-07-25 is a Saturday.
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    _, since, _ = trend_buckets("week", 1, now=now)[0]
    # 2026-07-20 is the preceding Monday.
    assert since.startswith("2026-07-20")


def test_month_bucket_anchors_on_first():
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    _, since, until = trend_buckets("month", 1, now=now)[0]
    assert since.startswith("2026-06-30") or since.startswith("2026-07-01")
    # Regardless of UTC offset shift, the bucket must be ~1 month wide.
    since_dt = datetime.fromisoformat(since)
    until_dt = datetime.fromisoformat(until)
    assert 27 <= (until_dt - since_dt).days <= 31


def test_year_bucket_rollover_december_to_january():
    now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    buckets = trend_buckets("year", 2, now=now)
    labels = [label for label, _, _ in buckets]
    assert labels == ["2025", "2026"]
    # The 2025 bucket must end exactly where the 2026 bucket begins.
    assert buckets[0][2] == buckets[1][1]


def test_month_buckets_step_calendar_months_not_fixed_days():
    now = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
    buckets = trend_buckets("month", 3, now=now)
    labels = [label for label, _, _ in buckets]
    assert labels == ["Jan 2026", "Feb 2026", "Mar 2026"]


def test_unknown_period_raises():
    with pytest.raises(ValueError):
        trend_buckets("fortnight", 3)
