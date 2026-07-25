from stroummeeschter.sse import iter_sse_events

SAMPLE = (
    "retry: 30000\n"
    "id: 866998\n"
    'event: ping\n'
    'data: {"title":"slimmelezer"}\n'
    "\n"
    "event: state\n"
    'data: {"id":"sensor-power_consumed","name":"Power Consumed","value":0.588,"state":"0.588 kW","uom":"kW","entity_category":0}\n'
    "\n"
    "id: 870362\n"
    "event: log\n"
    'data: [debug] some log line\n'
    "\n"
    "event: state\n"
    'data: {"id":"sensor-power_consumed","value":0.591,"state":"0.591 kW"}\n'
    "\n"
)


def test_parses_ping_state_and_log_events():
    events = list(iter_sse_events(SAMPLE.split("\n")))
    types = [e.event for e in events]
    assert types == ["ping", "state", "log", "state"]


def test_state_event_data_is_the_raw_json_string():
    events = [e for e in iter_sse_events(SAMPLE.split("\n")) if e.event == "state"]
    assert events[0].data == (
        '{"id":"sensor-power_consumed","name":"Power Consumed",'
        '"value":0.588,"state":"0.588 kW","uom":"kW","entity_category":0}'
    )
    assert events[1].data == '{"id":"sensor-power_consumed","value":0.591,"state":"0.591 kW"}'
