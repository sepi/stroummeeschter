import sqlite3

import pytest

from stroummeeschter import db
from stroummeeschter.aggregates import energy_totals


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    db.init_db(connection)
    for eid in (
        "sensor-energy_consumed_luxembourg",
        "sensor-energy_produced_luxembourg",
        "sensor-power_consumed",
        "sensor-power_produced",
        "envoy-production_wh_lifetime",
    ):
        db.upsert_entity(connection, eid, "2026-07-25T00:00:00+00:00")
    yield connection
    connection.close()


def test_totals_from_first_and_last_reading_in_window(conn):
    db.insert_reading(conn, "sensor-energy_consumed_luxembourg", 10_000.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "sensor-energy_consumed_luxembourg", 10_500.0, "2026-07-25T09:00:00+00:00")
    db.insert_reading(conn, "sensor-energy_produced_luxembourg", 5_000.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "sensor-energy_produced_luxembourg", 5_800.0, "2026-07-25T09:00:00+00:00")
    conn.commit()

    totals = energy_totals(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert totals["imported_wh"] == 500.0
    assert totals["exported_wh"] == 800.0
    assert totals["net_import_wh"] == -300.0
    assert totals["net_export_wh"] == 300.0


def test_totals_are_none_when_no_data_in_window(conn):
    totals = energy_totals(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert totals["imported_wh"] is None
    assert totals["exported_wh"] is None
    assert totals["net_import_wh"] is None
    assert totals["net_export_wh"] is None
    assert totals["net_exporting_share"] is None
    assert totals["pv_production_wh"] is None
    assert totals["consumed_wh"] is None


def test_consumed_wh_when_net_exporting_overall(conn):
    # C = P + In (energy balance identity), using *net* import, not gross
    # export alone.
    db.insert_reading(conn, "sensor-energy_consumed_luxembourg", 10_000.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "sensor-energy_consumed_luxembourg", 10_200.0, "2026-07-25T09:00:00+00:00")  # 200 Wh imported
    db.insert_reading(conn, "sensor-energy_produced_luxembourg", 5_000.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "sensor-energy_produced_luxembourg", 5_800.0, "2026-07-25T09:00:00+00:00")  # 800 Wh exported
    db.insert_reading(conn, "envoy-production_wh_lifetime", 100_000.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "envoy-production_wh_lifetime", 101_000.0, "2026-07-25T09:00:00+00:00")  # 1000 Wh produced
    conn.commit()

    totals = energy_totals(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert totals["pv_production_wh"] == 1000.0
    assert totals["net_import_wh"] == -600.0  # imported 200 - exported 800
    assert totals["consumed_wh"] == 400.0  # 1000 produced - 600 net export


def test_consumed_wh_when_net_importing_overall(conn):
    db.insert_reading(conn, "sensor-energy_consumed_luxembourg", 10_000.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "sensor-energy_consumed_luxembourg", 10_300.0, "2026-07-25T09:00:00+00:00")  # 300 Wh imported
    db.insert_reading(conn, "sensor-energy_produced_luxembourg", 5_000.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "sensor-energy_produced_luxembourg", 5_100.0, "2026-07-25T09:00:00+00:00")  # 100 Wh exported
    db.insert_reading(conn, "envoy-production_wh_lifetime", 100_000.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "envoy-production_wh_lifetime", 100_500.0, "2026-07-25T09:00:00+00:00")  # 500 Wh produced
    conn.commit()

    totals = energy_totals(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert totals["net_import_wh"] == 200.0  # imported 300 - exported 100, net importing
    assert totals["consumed_wh"] == 700.0  # 500 produced + 200 net import


def test_consumed_wh_is_none_without_pv_data(conn):
    db.insert_reading(conn, "sensor-energy_produced_luxembourg", 5_000.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "sensor-energy_produced_luxembourg", 5_800.0, "2026-07-25T09:00:00+00:00")
    conn.commit()

    totals = energy_totals(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert totals["pv_production_wh"] is None
    assert totals["consumed_wh"] is None


def test_consumed_wh_is_none_without_import_data(conn):
    # Missing gross-import data means net_import (and thus consumed_wh) can't
    # be computed.
    db.insert_reading(conn, "sensor-energy_produced_luxembourg", 5_000.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "sensor-energy_produced_luxembourg", 5_800.0, "2026-07-25T09:00:00+00:00")
    db.insert_reading(conn, "envoy-production_wh_lifetime", 100_000.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "envoy-production_wh_lifetime", 101_000.0, "2026-07-25T09:00:00+00:00")
    conn.commit()

    totals = energy_totals(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert totals["imported_wh"] is None
    assert totals["consumed_wh"] is None


def test_net_exporting_share_uses_power_balance_view(conn):
    db.insert_reading(conn, "sensor-power_consumed", 500.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "sensor-power_produced", 100.0, "2026-07-25T08:00:00+00:00")  # net importing
    db.insert_reading(conn, "sensor-power_consumed", 200.0, "2026-07-25T08:00:10+00:00")
    db.insert_reading(conn, "sensor-power_produced", 900.0, "2026-07-25T08:00:10+00:00")  # net exporting
    conn.commit()

    totals = energy_totals(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert totals["net_exporting_share"] == 0.5
