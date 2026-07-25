"""Parses ESPHome's human-readable `state` string into a normalized value.

We parse `state` rather than trust the JSON `value` field the device also
sends: ESPHome's JSON serializer has been observed to drop a trailing
significant digit (e.g. value=15006.39 vs state="15006.392 kWh" for the same
reading), while `state` always reflects the sensor's configured decimal
accuracy. Numeric results are normalized to their SI base unit (kW -> W,
kWh -> Wh) so nothing downstream has to reason about metric prefixes.
"""

from __future__ import annotations

import re

_SI_MULTIPLIERS = {
    "kWh": ("Wh", 1000),
    "kW": ("W", 1000),
}

_STATE_RE = re.compile(r"^(-?\d+(?:\.\d+)?)(?:\s+(\S+))?$")


def parse_state(state: str) -> tuple[float | str | None, str | None]:
    """Return (value, unit) for a raw ESPHome `state` string.

    - "NA" (ESPHome's placeholder for a sensor with no value yet) -> (None, None)
    - A number with a known kilo-prefixed unit -> normalized (value, base_unit)
    - A number with any other (or no) unit -> (value, unit_or_None), unchanged
    - Anything else (identification strings, SSIDs, ...) -> (state, None)
    """
    if state == "NA":
        return None, None

    match = _STATE_RE.match(state.strip())
    if not match:
        return state, None

    number, unit = match.groups()
    value = float(number)
    if unit in _SI_MULTIPLIERS:
        unit, factor = _SI_MULTIPLIERS[unit]
        value *= factor
    return value, unit
