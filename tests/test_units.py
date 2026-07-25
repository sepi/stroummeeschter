from stroummeeschter.units import parse_state


def test_normalizes_kwh_to_wh():
    # Real example from the device: the JSON `value` field for this reading
    # was truncated to 15006.39, but `state` carries the full precision.
    value, unit = parse_state("15006.392 kWh")
    assert value == 15006.392 * 1000
    assert unit == "Wh"


def test_normalizes_kw_to_w():
    value, unit = parse_state("0.591 kW")
    assert value == 591.0
    assert unit == "W"


def test_leaves_already_base_units_unchanged():
    assert parse_state("235.0 V") == (235.0, "V")
    assert parse_state("3.0 A") == (3.0, "A")


def test_unitless_count():
    assert parse_state("110") == (110.0, None)


def test_na_is_none():
    assert parse_state("NA") == (None, None)


def test_text_sensor_passthrough():
    assert parse_state("Lux5\\253663629_D") == ("Lux5\\253663629_D", None)
    assert parse_state("FRITZ!Box 4060 ZB") == ("FRITZ!Box 4060 ZB", None)
