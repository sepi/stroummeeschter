import threading
from http.server import ThreadingHTTPServer

import pytest
import requests

from stroummeeschter import db
from stroummeeschter.webapp import build_server, make_handler

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def server(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)
    db.init_db(conn)
    for eid in ("sensor-power_consumed", "sensor-power_produced"):
        db.upsert_entity(conn, eid, "2026-07-25T00:00:00+00:00", unit="W", category=0)
    db.insert_reading(conn, "sensor-power_consumed", 500.0, "2026-07-25T08:00:00+00:00")
    db.insert_reading(conn, "sensor-power_produced", 900.0, "2026-07-25T08:00:00+00:00")
    conn.commit()
    conn.close()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    thread.join()


def test_index_serves_html(server):
    resp = requests.get(server + "/", timeout=5)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["Content-Type"]
    assert b"/chart.png" in resp.content


def test_chart_png_default_params(server):
    resp = requests.get(server + "/chart.png", timeout=5)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "image/png"
    assert resp.content.startswith(PNG_MAGIC)


def test_chart_png_accepts_all_documented_params(server):
    resp = requests.get(
        server + "/chart.png",
        params={"chart": "phases", "hours": "3", "width": "300", "height": "200", "signals": "phase1_import"},
        timeout=5,
    )
    assert resp.status_code == 200
    assert resp.content.startswith(PNG_MAGIC)


def test_chart_png_with_explicit_date(server):
    resp = requests.get(server + "/chart.png", params={"date": "2026-07-25"}, timeout=5)
    assert resp.status_code == 200
    assert resp.content.startswith(PNG_MAGIC)


def test_chart_png_supports_prod_shift_min(server):
    resp = requests.get(server + "/chart.png", params={"prod_shift_min": "5.5"}, timeout=5)
    assert resp.status_code == 200
    assert resp.content.startswith(PNG_MAGIC)


def test_chart_png_clamps_out_of_range_prod_shift_min(server):
    resp = requests.get(server + "/chart.png", params={"prod_shift_min": "99999"}, timeout=5)
    assert resp.status_code == 200
    assert resp.content.startswith(PNG_MAGIC)


def test_chart_png_rejects_unknown_chart_type(server):
    resp = requests.get(server + "/chart.png", params={"chart": "nonsense"}, timeout=5)
    assert resp.status_code == 400


def test_chart_png_supports_trends_chart(server):
    resp = requests.get(
        server + "/chart.png",
        params={"chart": "trends", "period": "week", "count": "4", "width": "300", "height": "200"},
        timeout=5,
    )
    assert resp.status_code == 200
    assert resp.content.startswith(PNG_MAGIC)


def test_chart_png_trends_defaults_count_from_period(server):
    resp = requests.get(server + "/chart.png", params={"chart": "trends", "period": "month"}, timeout=5)
    assert resp.status_code == 200
    assert resp.content.startswith(PNG_MAGIC)


def test_chart_png_rejects_unknown_period(server):
    resp = requests.get(server + "/chart.png", params={"chart": "trends", "period": "fortnight"}, timeout=5)
    assert resp.status_code == 400


def test_chart_png_rejects_invalid_date(server):
    resp = requests.get(server + "/chart.png", params={"date": "not-a-date"}, timeout=5)
    assert resp.status_code == 400


def test_chart_png_clamps_out_of_range_dimensions(server):
    # Absurd values must not crash the server - just get clamped.
    resp = requests.get(server + "/chart.png", params={"width": "999999999", "height": "-5"}, timeout=5)
    assert resp.status_code == 200
    assert resp.content.startswith(PNG_MAGIC)


def test_unknown_path_is_404(server):
    resp = requests.get(server + "/nope", timeout=5)
    assert resp.status_code == 404


def test_build_server_initializes_a_fresh_unmigrated_db(tmp_path):
    # render_png() calls db.init_db() on every request, so even a brand new
    # db path (never touched by the logger) must work immediately.
    db_path = str(tmp_path / "fresh.db")
    httpd = build_server(db_path, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        resp = requests.get(f"http://127.0.0.1:{httpd.server_port}/chart.png", timeout=5)
        assert resp.status_code == 200
        assert resp.content.startswith(PNG_MAGIC)
    finally:
        httpd.shutdown()
        thread.join()
