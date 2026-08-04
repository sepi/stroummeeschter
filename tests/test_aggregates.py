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


def test_totals_are_netted_per_15min_bucket_not_a_single_lump_diff(conn):
    # Three 15-min buckets: [08:00,08:15) nets +100 (import), [08:15,08:30)
    # nets -300 (export), [08:30,08:45) nets -500 (export). imported_wh
    # (In+) only sums the positive buckets; exported_wh (En+) only the
    # negative ones - NOT a plain first/last diff over the whole window.
    for ts, imp, exp in [
        ("2026-07-25T08:00:00+00:00", 10_000.0, 5_000.0),
        ("2026-07-25T08:15:00+00:00", 10_100.0, 5_000.0),
        ("2026-07-25T08:30:00+00:00", 10_100.0, 5_300.0),
        ("2026-07-25T08:45:00+00:00", 10_200.0, 5_900.0),
    ]:
        db.insert_reading(conn, "sensor-energy_consumed_luxembourg", imp, ts)
        db.insert_reading(conn, "sensor-energy_produced_luxembourg", exp, ts)
    conn.commit()

    totals = energy_totals(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    assert totals["imported_wh"] == 100.0  # only the [08:00,08:15) bucket
    assert totals["exported_wh"] == 800.0  # the other two buckets: 300 + 500
    # net_import_wh still telescopes correctly regardless of bucket width -
    # it's just imported_wh - exported_wh (100 - 900 gross... no: it's the
    # lump diff, matching total import 200 - total export 900).
    assert totals["net_import_wh"] == -700.0
    assert totals["net_export_wh"] == 700.0


def test_same_bucket_offsetting_activity_nets_away(conn):
    # [08:00,08:15): 100 Wh imported AND 100 Wh exported in the same
    # bucket - these must cancel (net 0), contributing nothing to either
    # imported_wh or exported_wh, unlike a plain gross total which would
    # count both. [08:15,08:30): 150 Wh imported with no offsetting
    # export - this one counts in full.
    for ts, imp, exp in [
        ("2026-07-25T08:00:00+00:00", 10_000.0, 5_000.0),
        ("2026-07-25T08:15:00+00:00", 10_100.0, 5_100.0),
        ("2026-07-25T08:30:00+00:00", 10_250.0, 5_100.0),
    ]:
        db.insert_reading(conn, "sensor-energy_consumed_luxembourg", imp, ts)
        db.insert_reading(conn, "sensor-energy_produced_luxembourg", exp, ts)
    conn.commit()

    totals = energy_totals(conn, "2026-07-25T00:00:00+00:00", "2026-07-26T00:00:00+00:00")
    # Gross totals over the window would be 250 Wh imported / 100 Wh
    # exported - the correct netted figures are smaller for imported_wh
    # because 100 Wh of it was offset within its own bucket.
    assert totals["imported_wh"] == 150.0
    assert totals["exported_wh"] == 0.0
    assert totals["net_import_wh"] == 150.0
    assert totals["net_export_wh"] == -150.0


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
