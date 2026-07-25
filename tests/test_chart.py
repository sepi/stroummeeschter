import math
import sqlite3
from unittest.mock import patch

import pandas as pd
import pytest

from stroummeeschter import db
from stroummeeschter.chart import _fetch_series, _resample, _time_grid, render_phase_chart, render_power_chart

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


def test_render_power_chart_with_assume_netting(conn):
    db.upsert_entity(conn, "envoy-production_w", "2026-07-25T00:00:00+00:00", unit="W", category=0)
    db.insert_reading(conn, "envoy-production_w", 3000.0, "2026-07-25T08:00:02+00:00")
    conn.commit()

    png = render_power_chart(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00", assume_netting=True)
    assert png.startswith(PNG_MAGIC)


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
    "net_export_wh": None,
    "net_exporting_share": None,
    "pv_production_wh": None,
    "self_consumption_ratio": None,
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
