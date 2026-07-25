import sqlite3

from stroummeeschter import db
from stroummeeschter.envoy_cli import (
    ENTITY_PRODUCTION_W,
    ENTITY_PRODUCTION_WH_LIFETIME,
    ENTITY_PRODUCTION_WH_TODAY,
    _pick_production_entry,
    poll_once,
)


class FakeEnvoyClient:
    def __init__(self, payload):
        self.payload = payload

    def production(self):
        return self.payload


def test_pick_production_entry_prefers_active_metered_eim():
    payload = {
        "production": [
            {"type": "inverters", "wNow": 1},
            {"type": "eim", "activeCount": 1, "wNow": 2},
        ]
    }
    assert _pick_production_entry(payload)["type"] == "eim"


def test_pick_production_entry_ignores_inactive_eim_placeholder():
    # Real payload from a microinverter-only install (no production CT
    # clamp): the "eim" entry always exists but activeCount is 0 and every
    # field is hardcoded to 0 - must not be mistaken for real data.
    payload = {
        "production": [
            {
                "type": "inverters",
                "activeCount": 18,
                "readingTime": 1784975936,
                "wNow": 4599,
                "whLifetime": 10394678,
            },
            {
                "type": "eim",
                "activeCount": 0,
                "measurementType": "production",
                "wNow": 0.0,
                "whLifetime": 0.0,
                "whToday": 0.0,
            },
        ]
    }
    entry = _pick_production_entry(payload)
    assert entry["type"] == "inverters"
    assert entry["wNow"] == 4599


def test_pick_production_entry_falls_back_to_inverters_when_no_eim_present():
    payload = {"production": [{"type": "inverters", "wNow": 1}]}
    assert _pick_production_entry(payload)["type"] == "inverters"


def test_pick_production_entry_none_when_missing():
    assert _pick_production_entry({"production": []}) is None


def test_poll_once_records_readings():
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    client = FakeEnvoyClient(
        {
            "production": [
                {
                    "type": "eim",
                    "activeCount": 1,
                    "wNow": 2500.5,
                    "whLifetime": 123456.0,
                    "whToday": 4321.0,
                }
            ]
        }
    )

    poll_once(client, conn)

    assert conn.execute(
        "SELECT value FROM readings WHERE entity_id = ?", (ENTITY_PRODUCTION_W,)
    ).fetchone() == (2500.5,)
    assert conn.execute(
        "SELECT value FROM readings WHERE entity_id = ?", (ENTITY_PRODUCTION_WH_LIFETIME,)
    ).fetchone() == (123456.0,)
    assert conn.execute(
        "SELECT value FROM readings WHERE entity_id = ?", (ENTITY_PRODUCTION_WH_TODAY,)
    ).fetchone() == (4321.0,)


def test_poll_once_handles_missing_production_gracefully():
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    client = FakeEnvoyClient({"production": []})

    poll_once(client, conn)  # must not raise

    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0
