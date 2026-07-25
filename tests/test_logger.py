import sqlite3
from unittest.mock import patch

import pytest

from stroummeeschter import db
from stroummeeschter.client import Reading
from stroummeeschter.logger import ReadingLogger


class _StopTest(BaseException):
    """Not an Exception subclass, so it escapes ReadingLogger's broad
    `except Exception` and lets the test end the infinite run() loop
    deterministically once it's seen what it needs to see."""


class _FlakyThenWorkingClient:
    """First connection attempt raises an exception that is neither a
    requests.RequestException nor a sqlite3.Error - the exact failure mode
    that silently killed the real process (see logger.py's broad except)."""

    def __init__(self, base_url="http://fake"):
        self.base_url = base_url
        self.calls = 0

    def stream_readings(self):
        self.calls += 1
        if self.calls == 1:
            raise ValueError("unexpected malformed payload, not a network/db error")
        if self.calls == 2:
            yield Reading(entity_id="sensor-power_consumed", value=42.0, unit="W", category=0)
            raise _StopTest()
        raise _StopTest()


def test_run_survives_non_network_non_db_exceptions_and_keeps_recording():
    conn = sqlite3.connect(":memory:")
    reading_logger = ReadingLogger(base_url="http://fake", db_path=":memory:")
    reading_logger.client = _FlakyThenWorkingClient()

    with patch("stroummeeschter.logger.db.connect", return_value=conn), patch(
        "stroummeeschter.logger.time.sleep"
    ):
        with pytest.raises(_StopTest):
            reading_logger.run()

    assert reading_logger.client.calls == 2
    row = conn.execute(
        "SELECT value FROM readings WHERE entity_id = 'sensor-power_consumed'"
    ).fetchone()
    assert row == (42.0,)
