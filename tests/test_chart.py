import math
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from stroummeeschter import db
from stroummeeschter.chart import (
    DEFAULT_POWER_SIGNALS,
    DEFAULT_TREND_SIGNALS,
    _fetch_series,
    _fmt_balance,
    _money_balance,
    _resample,
    _shift_by_minutes,
    _time_grid,
    render_phase_chart,
    render_power_chart,
    render_trends_chart,
)
from stroummeeschter.trends import trend_buckets

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    db.init_db(connection)
    for eid in ("sensor-power_consumed", "sensor-power_produced"):
        db.upsert_entity(connection, eid, "2026-07-25T00:00:00+00:00", unit="W", category=0)
    for i in range(5):
        ts = f"2026-07-25T08:00:{i:02d}+00:00"
        db.insert_reading(connection, "sensor-power_consumed", 500.0 + i, ts)
        db.insert_reading(connection, "sensor-power_produced", 900.0 - i, ts)
    connection.commit()
    yield connection
    connection.close()


def test_fetch_series_dedupes_same_second_readings():
    # Regression: two genuine readings for the same entity landing in the
    # same second (recorded_at only has second precision - e.g. an SSE
    # reconnect's full-snapshot replay overlapping a real delta) used to
    # crash every downstream _resample() with "cannot reindex on an axis
    # with duplicate labels". The later reading should win.
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    db.upsert_entity(conn, "sensor-power_consumed_phase_1", "2026-07-25T00:00:00+00:00", unit="W", category=0)
    db.insert_reading(conn, "sensor-power_consumed_phase_1", 100.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "sensor-power_consumed_phase_1", 200.0, "2026-07-25T08:00:00+00:00")
    conn.commit()

    series = _fetch_series(conn, "sensor-power_consumed_phase_1", "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert len(series) == 1
    assert series.iloc[0] == 200.0


def test_render_phase_chart_survives_same_second_duplicate_readings():
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    for eid in ("sensor-power_consumed_phase_1", "sensor-power_produced_phase_2", "sensor-power_produced_phase_3"):
        db.upsert_entity(conn, eid, "2026-07-25T00:00:00+00:00", unit="W", category=0)
        db.insert_reading(conn, eid, 500.0, "2026-07-25T08:00:00+00:00")
        db.insert_reading(conn, eid, 510.0, "2026-07-25T08:00:00+00:00")  # duplicate second
    conn.commit()

    png = render_phase_chart(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert png.startswith(PNG_MAGIC)


def test_render_power_chart_returns_valid_png(conn):
    png = render_power_chart(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert png.startswith(PNG_MAGIC)


def test_render_power_chart_handles_empty_window(conn):
    png = render_power_chart(conn, "2020-01-01T00:00:00+00:00", "2020-01-02T00:00:00+00:00")
    assert png.startswith(PNG_MAGIC)


def test_render_power_chart_with_production_data(conn):
    db.upsert_entity(conn, "envoy-production_w", "2026-07-25T00:00:00+00:00", unit="W", category=0)
    db.insert_reading(conn, "envoy-production_w", 3000.0, "2026-07-25T08:00:02+00:00")
    conn.commit()

    png = render_power_chart(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert png.startswith(PNG_MAGIC)


def test_default_power_signals_hide_gross_import_export():
    # Confirmed net-metered billing: gross import/export aren't interesting
    # day to day - the default view shows net_import instead. net_export is
    # the same line mirrored, also hidden to avoid redundant default clutter.
    assert "import" not in DEFAULT_POWER_SIGNALS
    assert "export" not in DEFAULT_POWER_SIGNALS
    assert "net_export" not in DEFAULT_POWER_SIGNALS
    assert "net_import" in DEFAULT_POWER_SIGNALS


def test_render_power_chart_with_gross_signals_explicitly_requested(conn):
    png = render_power_chart(
        conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00", signals={"import", "export"}
    )
    assert png.startswith(PNG_MAGIC)


def test_render_power_chart_with_net_export_explicitly_requested(conn):
    png = render_power_chart(
        conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00", signals={"net_export"}
    )
    assert png.startswith(PNG_MAGIC)


def test_render_power_chart_with_prod_shift_returns_valid_png(conn):
    db.upsert_entity(conn, "envoy-production_w", "2026-07-25T00:00:00+00:00", unit="W", category=0)
    db.insert_reading(conn, "envoy-production_w", 3000.0, "2026-07-25T08:00:02+00:00")
    conn.commit()

    png = render_power_chart(
        conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00", prod_shift_min=5.0
    )
    assert png.startswith(PNG_MAGIC)


def test_shift_by_minutes_zero_is_a_noop():
    grid = pd.date_range("2026-07-25T08:00:00+00:00", periods=5, freq="10s")
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=grid)
    result = _shift_by_minutes(series, 0.0)
    assert result is series


def test_shift_by_minutes_positive_moves_curve_left_toward_the_past():
    # Positive minutes must move the curve left (toward the past): a value
    # that used to appear later should now appear earlier - i.e. at grid
    # position 0, we should see what used to be at position +1 (one 10s
    # grid step = 1/6 minute), pulling a "future" reading backward.
    grid = pd.date_range("2026-07-25T08:00:00+00:00", periods=5, freq="10s")
    series = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0], index=grid)
    result = _shift_by_minutes(series, 10 / 60)  # exactly one 10s grid step
    assert result.iloc[0] == 20.0
    assert result.iloc[1] == 30.0
    assert math.isnan(result.iloc[-1])  # nothing further in the future to pull from


def test_shift_by_minutes_negative_moves_curve_right_toward_the_future():
    grid = pd.date_range("2026-07-25T08:00:00+00:00", periods=5, freq="10s")
    series = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0], index=grid)
    result = _shift_by_minutes(series, -10 / 60)
    assert math.isnan(result.iloc[0])
    assert result.iloc[1] == 10.0
    assert result.iloc[2] == 20.0


def test_money_balance_worst_pays_max_import_earns_min_export():
    # 2 kWh imported, 3 kWh exported. Worst case: pay the max import price,
    # earn only the min export price.
    worst, best = _money_balance(2000.0, 3000.0, import_price_min=0.20, import_price_max=0.30, export_price_min=0.05, export_price_max=0.15)
    assert worst == pytest.approx(3 * 0.05 - 2 * 0.30)  # -0.45
    assert best == pytest.approx(3 * 0.15 - 2 * 0.20)  # 0.05


def test_money_balance_flat_single_price_collapses_worst_and_best():
    worst, best = _money_balance(2000.0, 3000.0, import_price_min=0.25, import_price_max=0.25, export_price_min=0.10, export_price_max=0.10)
    assert worst == best == pytest.approx(3 * 0.10 - 2 * 0.25)


def test_money_balance_is_none_when_any_input_missing():
    assert _money_balance(2000.0, 3000.0, 0.2, 0.3, 0.1, None) is None
    assert _money_balance(None, 3000.0, 0.2, 0.3, 0.1, 0.15) is None


def test_fmt_balance_none_is_none():
    assert _fmt_balance(None) is None


def test_fmt_balance_shows_single_figure_when_worst_equals_best():
    assert _fmt_balance((1.5, 1.5)) == "Balance +1.50"


def test_fmt_balance_shows_range_when_worst_differs_from_best():
    assert _fmt_balance((-0.45, 0.05)) == "Balance -0.45 (worst) to +0.05 (best)"


def test_render_power_chart_with_prices_includes_balance_in_title(conn):
    png = render_power_chart(
        conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00",
        import_price_min=0.2, import_price_max=0.3, export_price_min=0.05, export_price_max=0.15,
    )
    assert png.startswith(PNG_MAGIC)


def test_render_power_chart_without_prices_has_no_balance_line(conn):
    with patch("matplotlib.axes.Axes.set_title") as mocked_title:
        render_power_chart(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    title_text = mocked_title.call_args[0][0]
    assert "Balance" not in title_text


def test_fetch_series_returns_a_series_indexed_by_time(conn):
    series = _fetch_series(conn, "sensor-power_consumed", "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert isinstance(series, pd.Series)
    assert len(series) == 5
    assert list(series.values) == [500.0, 501.0, 502.0, 503.0, 504.0]


def test_fetch_series_empty_when_no_data(conn):
    series = _fetch_series(conn, "sensor-power_consumed", "2020-01-01T00:00:00+00:00", "2020-01-02T00:00:00+00:00")
    assert isinstance(series, pd.Series)
    assert len(series) == 0


def test_resample_holds_last_known_value():
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    db.upsert_entity(conn, "sensor-power_consumed", "2026-07-25T00:00:00+00:00", unit="W", category=0)
    db.insert_reading(conn, "sensor-power_consumed", 100.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "sensor-power_consumed", 200.0, "2026-07-25T08:01:00+00:00")
    conn.commit()

    grid = pd.to_datetime(
        [
            "2026-07-25T07:59:00+00:00",  # before any data
            "2026-07-25T08:00:00+00:00",  # exactly on first point
            "2026-07-25T08:00:30+00:00",  # between points - holds first value
            "2026-07-25T08:01:30+00:00",  # after second point
        ]
    )
    result = _resample(conn, "sensor-power_consumed", "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00", grid)
    assert math.isnan(result.iloc[0])
    assert list(result.iloc[1:]) == [100.0, 100.0, 200.0]


def test_resample_breaks_on_a_real_gap_instead_of_holding_forever():
    # Regression for the actual incident: the SlimmeLezer logger stalled for
    # 32 minutes while Envoy kept polling; forward-filling straight through
    # that gap made a frozen value look like continuous real data, which in
    # turn made a derived signal (Consumption) appear to track a trend it
    # wasn't. A gap past max_gap must show as NaN, not a held value.
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    db.upsert_entity(conn, "sensor-power_consumed", "2026-07-25T00:00:00+00:00", unit="W", category=0)
    db.insert_reading(conn, "sensor-power_consumed", 789.0, "2026-07-25T11:23:42+00:00")  # last real reading
    conn.commit()

    grid = pd.to_datetime(
        [
            "2026-07-25T11:24:00+00:00",  # 18s later - well within normal cadence, holds
            "2026-07-25T11:28:41+00:00",  # 4:59 later - just under the 5 min cutoff, holds
            "2026-07-25T11:28:43+00:00",  # 5:01 later - past the cutoff, must break
            "2026-07-25T11:55:37+00:00",  # 32 min later (the real stall's end) - still broken
        ]
    )
    result = _resample(conn, "sensor-power_consumed", "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00", grid)
    assert result.iloc[0] == 789.0
    assert result.iloc[1] == 789.0
    assert math.isnan(result.iloc[2])
    assert math.isnan(result.iloc[3])


def test_resample_respects_custom_max_gap():
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    db.upsert_entity(conn, "sensor-power_consumed", "2026-07-25T00:00:00+00:00", unit="W", category=0)
    db.insert_reading(conn, "sensor-power_consumed", 100.0, "2026-07-25T08:00:00+00:00")
    conn.commit()

    grid = pd.to_datetime(["2026-07-25T08:00:30+00:00"])  # 30s after the only reading
    tight = _resample(
        conn, "sensor-power_consumed", "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00", grid,
        max_gap=pd.Timedelta(seconds=10),
    )
    loose = _resample(
        conn, "sensor-power_consumed", "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00", grid,
        max_gap=pd.Timedelta(minutes=1),
    )
    assert math.isnan(tight.iloc[0])
    assert loose.iloc[0] == 100.0


def test_time_grid_is_regular_and_excludes_the_end():
    grid = _time_grid("2026-07-25T08:00:00+00:00", "2026-07-25T08:01:00+00:00")
    assert len(grid) == 6  # every 10s: 08:00:00, :10, :20, :30, :40, :50
    assert grid[0] == pd.Timestamp("2026-07-25T08:00:00+00:00")
    assert pd.Timestamp("2026-07-25T08:01:00+00:00") not in grid


def test_consumption_is_positive_energy_balance():
    # consumption = production + import - export, plotted positive alongside
    # Production so a surplus/deficit shows as a direct crossover.
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    for eid in ("sensor-power_consumed", "sensor-power_produced", "envoy-production_w"):
        db.upsert_entity(conn, eid, "2026-07-25T00:00:00+00:00", unit="W", category=0)
    ts = "2026-07-25T08:00:00+00:00"
    db.insert_reading(conn, "sensor-power_consumed", 200.0, ts)  # importing 200W
    db.insert_reading(conn, "sensor-power_produced", 0.0, ts)  # exporting nothing
    db.insert_reading(conn, "envoy-production_w", 800.0, ts)  # producing 800W
    conn.commit()

    grid = pd.to_datetime(["2026-07-25T08:00:00+00:00"])
    i = _resample(conn, "sensor-power_consumed", "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00", grid).iloc[0]
    e = _resample(conn, "sensor-power_produced", "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00", grid).iloc[0]
    p = _resample(conn, "envoy-production_w", "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00", grid).iloc[0]

    # consumption = 800 (produced) + 200 (import) - 0 (export) = 1000W
    assert (p + i - e) == 1000.0


def test_render_phase_chart_returns_valid_png():
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    ts = "2026-07-25T08:00:00+00:00"
    # phase 1 importing, phases 2/3 exporting - the imbalance scenario.
    for eid, value in (
        ("sensor-power_consumed_phase_1", 597.0),
        ("sensor-power_produced_phase_2", 1393.0),
        ("sensor-power_produced_phase_3", 1595.0),
    ):
        db.upsert_entity(conn, eid, ts, unit="W", category=0)
        db.insert_reading(conn, eid, value, ts)
    conn.commit()

    png = render_phase_chart(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert png.startswith(PNG_MAGIC)


def test_render_phase_chart_handles_empty_window():
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    png = render_phase_chart(conn, "2020-01-01T00:00:00+00:00", "2020-01-02T00:00:00+00:00")
    assert png.startswith(PNG_MAGIC)


STUB_TOTALS = {
    "imported_wh": None,
    "exported_wh": None,
    "net_import_wh": None,
    "net_export_wh": None,
    "net_exporting_share": None,
    "pv_production_wh": None,
    "consumed_wh": None,
}


def test_render_power_chart_totals_default_to_plot_window(conn):
    with patch("stroummeeschter.chart.energy_totals", wraps=lambda *a: STUB_TOTALS) as mocked:
        render_power_chart(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
        mocked.assert_called_once_with(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")


def test_render_power_chart_totals_use_totals_window_when_given(conn):
    with patch("stroummeeschter.chart.energy_totals", wraps=lambda *a: STUB_TOTALS) as mocked:
        render_power_chart(
            conn,
            "2026-07-25T08:00:00+00:00",
            "2026-07-25T09:00:00+00:00",  # a short zoomed-in plot window
            totals_since="2026-07-25T00:00:00+00:00",  # but totals cover the full day
            totals_until="2026-07-26T00:00:00+00:00",
        )
        mocked.assert_called_once_with(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")


def test_render_phase_chart_exporting_share_uses_totals_window_when_given(conn):
    with patch("stroummeeschter.chart._phase_exporting_shares", wraps=lambda *a: {1: None, 2: None, 3: None}) as mocked:
        render_phase_chart(
            conn,
            "2026-07-25T08:00:00+00:00",
            "2026-07-25T09:00:00+00:00",
            totals_since="2026-07-25T00:00:00+00:00",
            totals_until="2026-07-26T00:00:00+00:00",
        )
        mocked.assert_called_once_with(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")


@pytest.fixture
def trends_conn():
    connection = sqlite3.connect(":memory:")
    db.init_db(connection)
    for eid in (
        "sensor-energy_consumed_luxembourg",
        "sensor-energy_produced_luxembourg",
        "envoy-production_wh_lifetime",
    ):
        db.upsert_entity(connection, eid, "2026-07-20T00:00:00+00:00", unit="Wh", category=0)
    # 3 days of cumulative counters, each day adding 1000 Wh consumed,
    # 300 Wh exported, 1200 Wh produced (so each day: imported 1000,
    # surplus = exported - imported = -700, consumed = 1200+1000-300=1900).
    for day, base in enumerate([0, 1, 2]):
        ts_start = f"2026-07-{22 + day}T04:00:00+00:00"
        ts_end = f"2026-07-{23 + day}T03:59:59+00:00"
        db.insert_reading(connection, "sensor-energy_consumed_luxembourg", base * 1000.0, ts_start)
        db.insert_reading(connection, "sensor-energy_consumed_luxembourg", base * 1000.0 + 1000.0, ts_end)
        db.insert_reading(connection, "sensor-energy_produced_luxembourg", base * 300.0, ts_start)
        db.insert_reading(connection, "sensor-energy_produced_luxembourg", base * 300.0 + 300.0, ts_end)
        db.insert_reading(connection, "envoy-production_wh_lifetime", base * 1200.0, ts_start)
        db.insert_reading(connection, "envoy-production_wh_lifetime", base * 1200.0 + 1200.0, ts_end)
    connection.commit()
    yield connection
    connection.close()


def test_default_trend_signals_hide_gross_imported_exported():
    assert "imported" not in DEFAULT_TREND_SIGNALS
    assert "exported" not in DEFAULT_TREND_SIGNALS
    assert "net_import" in DEFAULT_TREND_SIGNALS


def test_render_trends_chart_returns_valid_png(trends_conn):
    buckets = trend_buckets("day", 3, now=datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc))
    png = render_trends_chart(trends_conn, buckets)
    assert png.startswith(PNG_MAGIC)


def test_render_trends_chart_with_prices_includes_balance_in_title(trends_conn):
    buckets = trend_buckets("day", 3, now=datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc))
    with patch("matplotlib.axes.Axes.set_title") as mocked_title:
        render_trends_chart(
            trends_conn, buckets,
            import_price_min=0.2, import_price_max=0.3, export_price_min=0.05, export_price_max=0.15,
        )
    assert "Balance" in mocked_title.call_args[0][0]


def test_render_trends_chart_without_prices_has_no_balance_line(trends_conn):
    buckets = trend_buckets("day", 3, now=datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc))
    with patch("matplotlib.axes.Axes.set_title") as mocked_title:
        render_trends_chart(trends_conn, buckets)
    assert "Balance" not in mocked_title.call_args[0][0]


def test_render_trends_chart_signals_subset_returns_valid_png(trends_conn):
    buckets = trend_buckets("day", 3, now=datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc))
    png = render_trends_chart(trends_conn, buckets, signals={"imported", "surplus"})
    assert png.startswith(PNG_MAGIC)


def test_render_trends_chart_handles_empty_buckets():
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    buckets = trend_buckets("week", 2, now=datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc))
    png = render_trends_chart(conn, buckets)
    assert png.startswith(PNG_MAGIC)


def test_render_trends_chart_calls_energy_totals_once_per_bucket(trends_conn):
    buckets = trend_buckets("day", 3, now=datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc))
    with patch("stroummeeschter.chart.energy_totals", wraps=lambda *a: STUB_TOTALS) as mocked:
        render_trends_chart(trends_conn, buckets)
        assert mocked.call_count == len(buckets)
        for (_, since, until), call in zip(buckets, mocked.call_args_list):
            assert call.args == (trends_conn, since, until)
