from stroummeeschter.client import SlimmelezerClient


def test_parse_state_event_normalizes_and_extracts_fields():
    data = '{"id":"sensor-power_consumed","name":"Power Consumed","value":0.588,"state":"0.588 kW","uom":"kW","entity_category":0}'
    reading = SlimmelezerClient._parse_state_event(data)
    assert reading.entity_id == "sensor-power_consumed"
    assert reading.value == 588.0
    assert reading.unit == "W"
    assert reading.category == 0


def test_parse_state_event_handles_delta_without_metadata():
    data = '{"id":"sensor-power_consumed","value":0.591,"state":"0.591 kW"}'
    reading = SlimmelezerClient._parse_state_event(data)
    assert reading.entity_id == "sensor-power_consumed"
    assert reading.value == 591.0
    assert reading.unit == "W"
    assert reading.name is None


def test_parse_state_event_prefers_state_precision_over_value():
    # Observed on the real device: `value` was truncated to 15006.39 while
    # `state` retained the full 15006.392 - we must derive from `state`.
    data = '{"id":"sensor-energy_consumed_luxembourg","value":15006.39,"state":"15006.392 kWh"}'
    reading = SlimmelezerClient._parse_state_event(data)
    assert reading.value == 15006392.0


def test_parse_state_event_ignores_malformed_json():
    assert SlimmelezerClient._parse_state_event("not json") is None


def test_parse_state_event_ignores_missing_state():
    assert SlimmelezerClient._parse_state_event('{"id":"sensor-x"}') is None
