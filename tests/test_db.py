import sqlite3

import pytest

from stroummeeschter import db


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()


def test_upsert_entity_then_insert_reading(conn):
    db.upsert_entity(conn, "sensor-power_consumed", "2026-07-25T10:00:00+00:00", name="Power Consumed", unit="W", category=0)
    db.insert_reading(conn, "sensor-power_consumed", 588.0, "2026-07-25T10:00:00+00:00")
    conn.commit()

    row = conn.execute("SELECT name, unit, category FROM entities WHERE id = ?", ("sensor-power_consumed",)).fetchone()
    assert row == ("Power Consumed", "W", 0)

    reading = conn.execute("SELECT value FROM readings").fetchone()
    assert reading == (588.0,)


def test_insert_reading_accepts_text_values():
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    db.upsert_entity(conn, "text_sensor-dsmr_identification", "2026-07-25T10:00:00+00:00")
    db.insert_reading(conn, "text_sensor-dsmr_identification", "Lux5\\253663629_D", "2026-07-25T10:00:00+00:00")
    conn.commit()

    reading = conn.execute("SELECT value FROM readings").fetchone()
    assert reading == ("Lux5\\253663629_D",)


def test_upsert_entity_does_not_clobber_metadata_with_nulls(conn):
    db.upsert_entity(conn, "sensor-power_consumed", "2026-07-25T10:00:00+00:00", name="Power Consumed", unit="W", category=0)
    # A later delta event carries no name/unit/category - must not erase what we already know.
    db.upsert_entity(conn, "sensor-power_consumed", "2026-07-25T10:00:05+00:00")
    conn.commit()

    row = conn.execute("SELECT name, unit, category, last_seen FROM entities WHERE id = ?", ("sensor-power_consumed",)).fetchone()
    assert row == ("Power Consumed", "W", 0, "2026-07-25T10:00:05+00:00")
