"""Minimal Server-Sent-Events line parser.

ESPHome's web_server component speaks plain SSE: blocks of ``field: value``
lines separated by a blank line. We only need ``event`` and ``data``, so this
stays deliberately small instead of pulling in a dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator


@dataclass
class SSEEvent:
    event: str = "message"
    data: str = ""
    id: str | None = None


def iter_sse_events(lines: Iterable[str]) -> Iterator[SSEEvent]:
    """Parse an iterable of decoded text lines (e.g. requests' iter_lines) into SSEEvents."""
    event_type = "message"
    data_lines: list[str] = []
    event_id: str | None = None

    for raw_line in lines:
        line = raw_line.rstrip("\n")

        if line == "":
            if data_lines:
                yield SSEEvent(event=event_type, data="\n".join(data_lines), id=event_id)
            event_type = "message"
            data_lines = []
            continue

        if line.startswith(":"):
            continue  # comment / keepalive

        if ":" in line:
            field_name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
        else:
            field_name, value = line, ""

        if field_name == "event":
            event_type = value
        elif field_name == "data":
            data_lines.append(value)
        elif field_name == "id":
            event_id = value
        # "retry" is ignored; we do our own reconnect/backoff.

    if data_lines:
        yield SSEEvent(event=event_type, data="\n".join(data_lines), id=event_id)
