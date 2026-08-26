import json

from homeconnect import daemon, store
from homeconnect.api import NoProgrammeActive
from homeconnect.appliances import Appliance


DISHWASHER = Appliance(
    ha_id="000000000000000000", name="Dishwasher", type="Dishwasher",
    brand="Bosch", vib="SMV000000", enumber="SMV000000/00", connected=True,
)

DISHWASHER_OFFLINE = Appliance(
    ha_id="000000000000000000", name="Dishwasher", type="Dishwasher",
    brand="Bosch", vib="SMV000000", enumber="SMV000000/00", connected=False,
)


class FakeClient:
    def __init__(self, responses):
        self._responses = dict(responses)

    def get(self, path):
        value = self._responses[path]
        if isinstance(value, Exception):
            raise value
        return value


def test_label_is_the_appliance_name_lowercased_not_the_serial():
    label = daemon.label_for(DISHWASHER)
    assert label == "dishwasher"
    assert DISHWASHER.ha_id not in label


def test_observe_flattens_to_short_keys():
    client = FakeClient({
        "/homeappliances": {"homeappliances": [{
            "haId": "000000000000000000", "name": "Dishwasher",
            "type": "Dishwasher", "brand": "Bosch", "vib": "SMV000000",
            "enumber": "SMV000000/00", "connected": True,
        }]},
        "/homeappliances/000000000000000000/status": {"status": [
            {"key": "BSH.Common.Status.OperationState",
             "value": "BSH.Common.EnumType.OperationState.Run"},
        ]},
        "/homeappliances/000000000000000000/programs/active": {
            "key": "Dishcare.Dishwasher.Program.Eco50", "options": [],
        },
    })

    observed = daemon.observe(client)

    assert observed["dishwasher"]["OperationState"] == "Run"
    assert observed["dishwasher"]["Programme"] == "Eco50"


def test_observe_records_no_programme_when_idle():
    client = FakeClient({
        "/homeappliances": {"homeappliances": [{
            "haId": "000000000000000000", "name": "Dishwasher",
            "type": "Dishwasher", "brand": "Bosch", "vib": "SMV000000",
            "enumber": "SMV000000/00", "connected": True,
        }]},
        "/homeappliances/000000000000000000/status": {"status": []},
        "/homeappliances/000000000000000000/programs/active":
            NoProgrammeActive("SDK.Error.NoProgramActive"),
    })

    observed = daemon.observe(client)

    assert observed["dishwasher"].get("Programme") is None


def test_record_changes_appends_one_line_per_change(tmp_path):
    path = tmp_path / "events.jsonl"
    before = {"dishwasher": {"OperationState": "Ready"}}
    after = {"dishwasher": {"OperationState": "Run", "DoorState": "Closed"}}

    written = daemon.record_changes(before, after, path, now=lambda: "T0")

    records, _ = store.read_records(path)
    assert written == 2
    assert {r["key"] for r in records} == {"OperationState", "DoorState"}
    assert all(r["ts"] == "T0" for r in records)


def test_record_changes_writes_nothing_when_nothing_changed(tmp_path):
    path = tmp_path / "events.jsonl"
    same = {"dishwasher": {"OperationState": "Ready"}}

    assert daemon.record_changes(same, same, path, now=lambda: "T0") == 0
    assert not path.exists()


def test_apply_event_updates_state_and_logs_only_real_changes(tmp_path):
    path = tmp_path / "events.jsonl"
    state = {"dishwasher": {"OperationState": "Ready"}}

    daemon.apply_event(
        state, "dishwasher",
        [("BSH.Common.Status.OperationState", "BSH.Common.EnumType.OperationState.Run")],
        path, now=lambda: "T1",
    )
    daemon.apply_event(
        state, "dishwasher",
        [("BSH.Common.Status.OperationState", "BSH.Common.EnumType.OperationState.Run")],
        path, now=lambda: "T2",
    )

    records, _ = store.read_records(path)
    assert state["dishwasher"]["OperationState"] == "Run"
    assert len(records) == 1, "a repeated event is not a transition"


def test_apply_event_creates_an_appliance_it_has_not_seen(tmp_path):
    path = tmp_path / "events.jsonl"
    state: dict = {}

    daemon.apply_event(
        state, "oven",
        [("BSH.Common.Status.DoorState", "BSH.Common.EnumType.DoorState.Open")],
        path, now=lambda: "T1",
    )

    assert state["oven"]["DoorState"] == "Open"


def test_record_gap_writes_a_marker(tmp_path):
    path = tmp_path / "events.jsonl"

    daemon.record_gap("2026-08-26T23:14:02Z", "stream_lost", path, now=lambda: "T9")

    records, _ = store.read_records(path)
    assert records[0]["event"] == "coverage_gap"
    assert records[0]["from"] == "2026-08-26T23:14:02Z"
    assert records[0]["reason"] == "stream_lost"


def test_observe_carries_state_forward_when_appliance_goes_offline(tmp_path):
    path = tmp_path / "events.jsonl"
    previous = {"dishwasher": {"OperationState": "Run", "Connected": True}}
    client = FakeClient({})  # no calls should be made for an offline appliance

    observed = daemon.observe(client, found=[DISHWASHER_OFFLINE], previous=previous)

    assert observed["dishwasher"] == {"OperationState": "Run", "Connected": False}
    written = daemon.record_changes(previous, observed, path, now=lambda: "T0")
    records, _ = store.read_records(path)
    assert written == 1
    assert records[0]["key"] == "Connected"
    assert records[0]["from"] is True and records[0]["to"] is False


def test_observe_reconnect_yields_connectivity_plus_genuine_changes(tmp_path):
    path = tmp_path / "events.jsonl"
    previous = {"dishwasher": {"OperationState": "Run", "Connected": False}}
    client = FakeClient({
        "/homeappliances/000000000000000000/status": {"status": [
            {"key": "BSH.Common.Status.OperationState",
             "value": "BSH.Common.EnumType.OperationState.Run"},
            {"key": "BSH.Common.Status.DoorState",
             "value": "BSH.Common.EnumType.DoorState.Closed"},
        ]},
        "/homeappliances/000000000000000000/programs/active":
            NoProgrammeActive("SDK.Error.NoProgramActive"),
    })

    observed = daemon.observe(client, found=[DISHWASHER], previous=previous)
    written = daemon.record_changes(previous, observed, path, now=lambda: "T0")

    records, _ = store.read_records(path)
    keys_changed = {r["key"] for r in records}
    assert written == 2
    assert keys_changed == {"Connected", "DoorState"}


def test_observe_without_previous_still_works():
    client = FakeClient({
        "/homeappliances/000000000000000000/status": {"status": []},
        "/homeappliances/000000000000000000/programs/active":
            NoProgrammeActive("SDK.Error.NoProgramActive"),
    })

    observed = daemon.observe(client, found=[DISHWASHER])

    assert observed["dishwasher"]["Connected"] is True

    offline_client = FakeClient({})
    observed_offline = daemon.observe(offline_client, found=[DISHWASHER_OFFLINE])
    assert observed_offline["dishwasher"] == {"Connected": False}


def test_observe_disambiguates_colliding_status_and_option_keys():
    client = FakeClient({
        "/homeappliances/000000000000000000/status": {"status": [
            {"key": "BSH.Common.Status.DoorState", "value": "closed"},
        ]},
        "/homeappliances/000000000000000000/programs/active": {
            "key": "Dishcare.Dishwasher.Program.Eco50",
            "options": [
                {"key": "Vendor.Other.DoorState", "value": "open"},
            ],
        },
    })

    observed = daemon.observe(client, found=[DISHWASHER])

    assert observed["dishwasher"]["DoorState"] == "closed"
    assert observed["dishwasher"]["OptionDoorState"] == "open"


def test_tail_reduces_non_bsh_vendor_namespaces():
    assert daemon._tail("Dishcare.Dishwasher.Program.Eco50") == "Eco50"


def test_tail_leaves_non_vendor_dotted_values_untouched():
    assert daemon._tail("1.5") == "1.5"


def test_no_record_ever_contains_the_device_serial(tmp_path):
    path = tmp_path / "events.jsonl"
    daemon.record_changes(
        {}, {"dishwasher": {"OperationState": "Run"}}, path, now=lambda: "T0"
    )
    assert "000000000000000000" not in path.read_text()


class FakeStreamResponse:
    """Mimics requests' streaming response closely enough to parse."""

    def __init__(self, lines, status_code=200):
        self.status_code = status_code
        self._lines = list(lines)

    def iter_lines(self, decode_unicode=False):
        yield from self._lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_stream_once_folds_events_into_state(tmp_path):
    path = tmp_path / "events.jsonl"
    state = {"dishwasher": {"OperationState": "Ready"}}
    response = FakeStreamResponse([
        "event: NOTIFY",
        'data: {"haId":"000000000000000000","items":'
        '[{"key":"BSH.Common.Status.OperationState",'
        '"value":"BSH.Common.EnumType.OperationState.Run"}]}',
        "",
    ])

    class FakeSession:
        def get(self, url, headers=None, stream=None, timeout=None):
            return response

    reason = daemon.stream_once(
        FakeSession(), "https://example.invalid/events", "tok",
        state, {"000000000000000000": "dishwasher"}, path, now=lambda: "T1",
    )

    records, _ = store.read_records(path)
    assert state["dishwasher"]["OperationState"] == "Run"
    assert records[0]["to"] == "Run"
    assert reason == "stream_ended"


def test_run_marks_a_gap_when_the_stream_ends(tmp_path):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    slept: list[float] = []

    def build_client():
        return FakeClient({
            "/homeappliances": {"homeappliances": []},
        })

    class FakeSession:
        def get(self, *a, **k):
            return FakeStreamResponse([])

    daemon.run(
        build_client=build_client,
        session_factory=FakeSession,
        events_path=events_path,
        state_path=state_path,
        sleep=slept.append,
        delays=iter([0.0, 0.0, 0.0]),
        iterations=2,
    )

    records, _ = store.read_records(events_path)
    gaps = [r for r in records if r.get("event") == "coverage_gap"]
    assert len(gaps) >= 1
    assert slept, "a reconnection must wait rather than spin"


def test_run_persists_state_between_iterations(tmp_path):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    def build_client():
        return FakeClient({
            "/homeappliances": {"homeappliances": [{
                "haId": "000000000000000000", "name": "Dishwasher",
                "type": "Dishwasher", "brand": "Bosch", "vib": "SMV000000",
                "enumber": "SMV000000/00", "connected": True,
            }]},
            "/homeappliances/000000000000000000/status": {"status": [
                {"key": "BSH.Common.Status.OperationState",
                 "value": "BSH.Common.EnumType.OperationState.Ready"},
            ]},
            "/homeappliances/000000000000000000/programs/active":
                NoProgrammeActive("idle"),
        })

    class FakeSession:
        def get(self, *a, **k):
            return FakeStreamResponse([])

    daemon.run(
        build_client=build_client, session_factory=FakeSession,
        events_path=events_path, state_path=state_path,
        sleep=lambda _: None, delays=iter([0.0, 0.0, 0.0]), iterations=2,
    )

    saved = store.load_state(state_path)
    assert saved["dishwasher"]["OperationState"] == "Ready"

    records, _ = store.read_records(events_path)
    transitions = [r for r in records if r.get("key") == "OperationState"]
    assert len(transitions) == 1, "the same state must not be logged twice"
