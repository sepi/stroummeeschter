import os
from datetime import date, datetime, timedelta, timezone

from stroummeeschter import db
from stroummeeschter.chart import LOCAL_TZ
from stroummeeschter.chart_cli import day_window, write_chart

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _seed(db_path):
    conn = db.connect(db_path)
    db.init_db(conn)
    for eid in ("sensor-power_consumed", "sensor-power_produced"):
        db.upsert_entity(conn, eid, "2026-07-25T00:00:00+00:00", unit="W", category=0)
    db.insert_reading(conn, "sensor-power_consumed", 500.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "sensor-power_produced", 900.0, "2026-07-25T08:00:00+00:00")
    conn.commit()
    conn.close()


def test_write_chart_creates_png(tmp_path):
    db_path = str(tmp_path / "test.db")
    out_path = str(tmp_path / "power.png")
    _seed(db_path)

    write_chart(db_path, out_path, hours=24, width_px=300, height_px=200)

    with open(out_path, "rb") as f:
        assert f.read(8) == PNG_MAGIC


def test_write_chart_initializes_a_fresh_unmigrated_db(tmp_path):
    # Regression: write_chart() must migrate the db itself, the same way
    # the logger does - otherwise power_balance (from migration 0002)
    # doesn't exist yet on a db that's never seen the logger run.
    db_path = str(tmp_path / "fresh.db")
    out_path = str(tmp_path / "power.png")
    assert not os.path.exists(db_path)

    write_chart(db_path, out_path, hours=24, width_px=300, height_px=200)

    with open(out_path, "rb") as f:
        assert f.read(8) == PNG_MAGIC


def test_write_chart_leaves_no_temp_file_behind(tmp_path):
    db_path = str(tmp_path / "test.db")
    out_path = str(tmp_path / "power.png")
    _seed(db_path)

    write_chart(db_path, out_path, hours=24, width_px=300, height_px=200)

    assert os.path.exists(out_path)
    assert not os.path.exists(out_path + ".tmp")


def test_day_window_for_explicit_date_is_6am_to_6am_local():
    since, until = day_window(on_date=date(2026, 7, 25))
    assert until - since == timedelta(hours=24)
    assert since.astimezone(LOCAL_TZ) == datetime(2026, 7, 25, 6, 0, tzinfo=LOCAL_TZ)
    assert until.astimezone(LOCAL_TZ) == datetime(2026, 7, 26, 6, 0, tzinfo=LOCAL_TZ)


def test_day_window_respects_custom_start_hour():
    since, until = day_window(day_start_hour=0, on_date=date(2026, 7, 25))
    assert since.astimezone(LOCAL_TZ) == datetime(2026, 7, 25, 0, 0, tzinfo=LOCAL_TZ)


def test_day_window_without_date_contains_now():
    since, until = day_window()
    now = datetime.now(timezone.utc)
    assert since <= now < until
    assert until - since == timedelta(hours=24)


def test_write_chart_supports_phases_selector(tmp_path):
    db_path = str(tmp_path / "test.db")
    out_path = str(tmp_path / "phases.png")
    conn = db.connect(db_path)
    db.init_db(conn)
    db.upsert_entity(conn, "sensor-power_consumed_phase_1", "2026-07-25T00:00:00+00:00", unit="W", category=0)
    db.insert_reading(conn, "sensor-power_consumed_phase_1", 500.0, "2026-07-25T08:00:00+00:00")
    conn.commit()
    conn.close()

    write_chart(db_path, out_path, hours=24, width_px=300, height_px=200, chart="phases")

    with open(out_path, "rb") as f:
        assert f.read(8) == PNG_MAGIC


def test_write_chart_supports_trends_chart(tmp_path):
    db_path = str(tmp_path / "test.db")
    out_path = str(tmp_path / "trends.png")
    conn = db.connect(db_path)
    db.init_db(conn)
    for eid in (
        "sensor-energy_consumed_luxembourg",
        "sensor-energy_produced_luxembourg",
        "envoy-production_wh_lifetime",
    ):
        db.upsert_entity(conn, eid, "2026-07-20T00:00:00+00:00", unit="Wh", category=0)
        db.insert_reading(conn, eid, 0.0, "2026-07-24T04:00:00+00:00")
        db.insert_reading(conn, eid, 1000.0, "2026-07-25T03:59:59+00:00")
    conn.commit()
    conn.close()

    write_chart(db_path, out_path, width_px=300, height_px=200, chart="trends", period="week", count=2)

    with open(out_path, "rb") as f:
        assert f.read(8) == PNG_MAGIC
