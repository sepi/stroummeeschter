import sqlite3
from datetime import datetime, timezone

import pytest

from stroummeeschter import db
from stroummeeschter.stats import HORIZONS, compute_stats


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    db.init_db(connection)
    for eid in (
        "sensor-power_consumed",
        "sensor-power_produced",
        "envoy-production_w",
        "sensor-energy_consumed_luxembourg",
        "sensor-energy_produced_luxembourg",
        "envoy-production_wh_lifetime",
    ):
        db.upsert_entity(connection, eid, "2026-07-25T00:00:00+00:00", unit="W", category=0)
    yield connection
    connection.close()


def test_compute_stats_has_all_horizons(conn):
    stats = compute_stats(conn, now=datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc))
    assert set(stats["horizons"]) == set(HORIZONS)


def test_compute_stats_handles_empty_db(conn):
    stats = compute_stats(conn, now=datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc))
    for horizon in stats["horizons"].values():
        assert horizon["import_w"] == {"min_w": None, "avg_w": None, "max_w": None}
        assert horizon["export_w"] == {"min_w": None, "avg_w": None, "max_w": None}
        assert horizon["production_w"] == {"min_w": None, "avg_w": None, "max_w": None}
        assert horizon["avg_consumption_w"] is None
        assert horizon["imported_wh"] is None
        assert horizon["produced_wh"] is None
        assert horizon["consumed_wh"] is None


def test_power_min_avg_max_within_last_hour(conn):
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    for i, value in enumerate([100.0, 300.0, 200.0]):
        ts = f"2026-07-25T11:{30 + i * 10:02d}:00+00:00"
        db.insert_reading(conn, "sensor-power_consumed", value, ts)
    conn.commit()

    stats = compute_stats(conn, now=now)
    last_hour = stats["horizons"]["last_hour"]["import_w"]
    assert last_hour == {"min_w": 100.0, "avg_w": 200.0, "max_w": 300.0}


def test_power_reading_outside_horizon_excluded(conn):
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    db.insert_reading(conn, "sensor-power_consumed", 100.0, "2026-07-25T10:00:00+00:00")  # 2h ago
    db.insert_reading(conn, "sensor-power_consumed", 500.0, "2026-07-25T11:50:00+00:00")  # within last hour
    conn.commit()

    stats = compute_stats(conn, now=now)
    assert stats["horizons"]["last_hour"]["import_w"]["max_w"] == 500.0
    assert stats["horizons"]["last_day"]["import_w"]["max_w"] == 500.0
    assert stats["horizons"]["last_day"]["import_w"]["min_w"] == 100.0


def test_avg_consumption_derived_from_energy_balance(conn):
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    since = "2026-07-25T11:00:00+00:00"  # exactly 1 hour before `now`
    # `until` in the horizon query is exclusive (recorded_at < until), so the
    # "last" reading must land strictly before `now`, not exactly at it.
    just_before_now = "2026-07-25T11:59:59+00:00"
    # 1000 Wh produced, 200 Wh imported, 300 Wh exported over the hour
    # -> consumed = 1000 + 200 - 300 = 900 Wh over 1h -> avg 900 W
    db.insert_reading(conn, "envoy-production_wh_lifetime", 50_000.0, since)
    db.insert_reading(conn, "envoy-production_wh_lifetime", 51_000.0, just_before_now)
    db.insert_reading(conn, "sensor-energy_consumed_luxembourg", 10_000.0, since)
    db.insert_reading(conn, "sensor-energy_consumed_luxembourg", 10_200.0, just_before_now)
    db.insert_reading(conn, "sensor-energy_produced_luxembourg", 5_000.0, since)
    db.insert_reading(conn, "sensor-energy_produced_luxembourg", 5_300.0, just_before_now)
    conn.commit()

    stats = compute_stats(conn, now=now)
    last_hour = stats["horizons"]["last_hour"]
    assert last_hour["avg_consumption_w"] == pytest.approx(900.0, rel=0.01)
    assert last_hour["produced_wh"] == pytest.approx(1000.0)
    assert last_hour["imported_wh"] == pytest.approx(200.0)
    assert last_hour["exported_wh"] == pytest.approx(300.0)
    assert last_hour["consumed_wh"] == pytest.approx(900.0)
    assert last_hour["net_import_wh"] == pytest.approx(-100.0)  # imported 200 - exported 300


def test_total_horizon_uses_actual_earliest_reading_not_epoch(conn):
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    db.insert_reading(conn, "sensor-power_consumed", 100.0, "2026-07-25T10:00:00+00:00")
    conn.commit()

    stats = compute_stats(conn, now=now)
    assert stats["horizons"]["total"]["since"] == "2026-07-25T10:00:00+00:00"
