"""Streaming client for a SlimmeLezer (ESPHome web_server) device.

Connects to ``/events`` and yields one Reading per ``event: state`` message.
On (re)connect the device immediately replays the current value of every
entity, so a fresh connection is itself a complete snapshot - no separate
poll loop is needed to "catch up".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterator

import requests

from stroummeeschter.sse import iter_sse_events
from stroummeeschter.units import parse_state

logger = logging.getLogger(__name__)

# ESPHome web_server entity_category values.
CATEGORY_PRIMARY = 0
CATEGORY_DIAGNOSTIC = 2


@dataclass
class Reading:
    entity_id: str  # e.g. "sensor-power_consumed"
    value: float | str | None  # normalized to its SI base unit for numeric sensors
    name: str | None = None
    unit: str | None = None
    category: int | None = None


class SlimmelezerClient:
    def __init__(self, base_url: str, connect_timeout: float = 30.0, read_timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.connect_timeout = connect_timeout
        # The telegram-driven "state" burst arrives roughly every ~10s under
        # normal operation; 20s gives 2x margin. The device also sends a
        # separate "ping" keepalive every ~30s ("retry: 30000" in its SSE
        # stream), so an occasional single telegram running long can trigger
        # a reconnect before the ping would've - harmless (backoff resets
        # immediately on the next successful connect), just slightly more
        # sensitive than waiting for the ping. Without a finite timeout at
        # all, a dead socket (WiFi drop with no TCP FIN/RST) hangs forever:
        # no exception, so no reconnect ever triggers, and the process sits
        # there "stalled" with no error.
        self.read_timeout = read_timeout
        self._session = requests.Session()

    def stream_readings(self) -> Iterator[Reading]:
        """Open the SSE stream and yield Readings until the connection ends.

        Raises requests.RequestException on connection failure or on a
        silently-dead connection (read timeout) - callers are expected to
        handle reconnect/backoff around this generator.
        """
        url = f"{self.base_url}/events"
        with self._session.get(
            url,
            stream=True,
            timeout=(self.connect_timeout, self.read_timeout),
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            lines = response.iter_lines(decode_unicode=True)
            for event in iter_sse_events(lines):
                if event.event != "state":
                    continue  # skip "ping" keepalives and "log" debug lines
                reading = self._parse_state_event(event.data)
                if reading is not None:
                    yield reading

    @staticmethod
    def _parse_state_event(data: str) -> Reading | None:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Skipping unparseable SSE payload: %r", data)
            return None

        entity_id = payload.get("id")
        state = payload.get("state")
        if not entity_id or state is None:
            return None

        value, unit = parse_state(state)
        return Reading(
            entity_id=entity_id,
            value=value,
            name=payload.get("name"),
            unit=unit,
            category=payload.get("entity_category"),
        )
