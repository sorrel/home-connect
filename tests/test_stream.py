from homeconnect import stream


def test_parses_a_simple_event():
    lines = [
        "event: NOTIFY",
        'data: {"items":[{"key":"BSH.Common.Status.DoorState","value":"Open"}]}',
        "",
    ]
    events = list(stream.parse_sse(lines))

    assert len(events) == 1
    assert events[0]["event"] == "NOTIFY"
    assert events[0]["data"]["items"][0]["value"] == "Open"


def test_parses_multiple_events_separated_by_blank_lines():
    lines = [
        "event: STATUS", 'data: {"items":[]}', "",
        "event: NOTIFY", 'data: {"items":[]}', "",
    ]
    assert [e["event"] for e in stream.parse_sse(lines)] == ["STATUS", "NOTIFY"]


def test_keep_alive_events_carry_no_data():
    """The vendor sends a periodic KEEP-ALIVE with an empty body."""
    events = list(stream.parse_sse(["event: KEEP-ALIVE", "data: ", ""]))
    assert events[0]["event"] == "KEEP-ALIVE"
    assert events[0]["data"] is None


def test_ignores_comment_lines():
    """A line beginning with a colon is an SSE comment, not an event."""
    lines = [": ping", "event: NOTIFY", 'data: {"items":[]}', ""]
    assert len(list(stream.parse_sse(lines))) == 1


def test_unparseable_data_yields_the_event_with_none_data():
    """A malformed body must not kill the stream."""
    events = list(stream.parse_sse(["event: NOTIFY", "data: {broken", ""]))
    assert events[0]["event"] == "NOTIFY"
    assert events[0]["data"] is None


def test_a_trailing_event_without_a_blank_line_is_still_yielded():
    events = list(stream.parse_sse(["event: NOTIFY", 'data: {"items":[]}']))
    assert len(events) == 1


def test_multi_line_data_is_concatenated():
    """SSE allows a body split across several data: lines."""
    lines = ["event: NOTIFY", 'data: {"items":', 'data: []}', ""]
    events = list(stream.parse_sse(lines))
    assert events[0]["data"] == {"items": []}


def test_extract_changes_pulls_key_value_pairs():
    payload = {"items": [
        {"key": "BSH.Common.Status.OperationState", "value": "Run"},
        {"key": "BSH.Common.Option.ProgramProgress", "value": 12},
    ]}
    assert stream.extract_changes(payload) == [
        ("BSH.Common.Status.OperationState", "Run"),
        ("BSH.Common.Option.ProgramProgress", 12),
    ]


def test_extract_changes_tolerates_a_missing_or_odd_body():
    assert stream.extract_changes({}) == []
    assert stream.extract_changes({"items": "nonsense"}) == []
    assert stream.extract_changes({"items": [{"no_key": 1}]}) == []


def test_backoff_grows_then_caps():
    delays = stream.backoff_delays(initial=1.0, cap=8.0, factor=2.0, jitter=lambda d: d)
    assert [next(delays) for _ in range(6)] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_backoff_applies_jitter():
    """Without jitter every client reconnects in lockstep after an outage."""
    delays = stream.backoff_delays(initial=4.0, cap=60.0, jitter=lambda d: d / 2)
    assert next(delays) == 2.0
