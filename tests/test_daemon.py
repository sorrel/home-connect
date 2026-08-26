import contextlib
import itertools
import json

import pytest

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
    state_path = tmp_path / "state.json"
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
        state_path=state_path,
    )

    records, _ = store.read_records(path)
    assert state["dishwasher"]["OperationState"] == "Run"
    assert records[0]["to"] == "Run"
    assert reason == "stream_ended"


def _dishwasher_client():
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


def test_run_marks_a_gap_that_brackets_a_real_interruption(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    slept: list[float] = []

    monkeypatch.setattr(daemon, "stream_once", lambda *a, **k: "stream_ended")

    # 0.0 at the moment coverage is lost, 200.0 at the next successful poll —
    # a 200-second interruption, comfortably past GAP_THRESHOLD_SECONDS (120).
    clock_values = itertools.chain([0.0, 200.0], itertools.count(1000.0, 1000.0))

    daemon.run(
        build_client=_dishwasher_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=slept.append,
        delays_factory=lambda: iter([5.0, 5.0, 5.0]),
        clock=lambda: next(clock_values),
        iterations=2,
    )

    records, _ = store.read_records(events_path)
    recovered = [
        r for r in records
        if r.get("event") == "coverage_gap" and r.get("reason") == "recovered"
    ]
    assert len(recovered) == 1, "one bracketed gap, not one per cycle"
    assert recovered[0]["from"] is not None
    assert slept == [5.0], "a reconnection must wait, not spin"


def test_run_does_not_mark_a_gap_for_a_brief_interruption(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    monkeypatch.setattr(daemon, "stream_once", lambda *a, **k: "stream_ended")

    # 0.0 then 50.0 — a 50-second interruption, under the 120-second threshold.
    clock_values = itertools.chain([0.0, 50.0], itertools.count(1000.0, 1000.0))

    daemon.run(
        build_client=_dishwasher_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        clock=lambda: next(clock_values),
        iterations=2,
    )

    records, _ = store.read_records(events_path)
    recovered = [
        r for r in records
        if r.get("event") == "coverage_gap" and r.get("reason") == "recovered"
    ]
    assert recovered == [], "a two-minute blind spot cannot hide an hours-scale change"


def test_run_reconcile_ending_never_starts_a_gap(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    def build_client():
        return FakeClient({"/homeappliances": {"homeappliances": []}})

    monkeypatch.setattr(daemon, "stream_once", lambda *a, **k: "reconcile")

    clock_calls: list[int] = []

    def clock() -> float:
        clock_calls.append(1)
        return 0.0

    daemon.run(
        build_client=build_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        clock=clock,
        iterations=3,
    )

    records, _ = store.read_records(events_path)
    reasons = [r.get("reason") for r in records if r.get("event") == "coverage_gap"]
    assert reasons == ["startup"], "a healthy reconnection must not be logged as a gap"
    assert not clock_calls, "a reconcile ending must never touch the gap clock"


def test_run_resets_backoff_after_a_healthy_cycle(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    slept: list[float] = []

    def build_client():
        return FakeClient({"/homeappliances": {"homeappliances": []}})

    monkeypatch.setattr(daemon, "stream_once", lambda *a, **k: "stream_ended")

    daemon.run(
        build_client=build_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=slept.append,
        delays_factory=lambda: iter([1.0, 2.0, 4.0, 8.0]),
        iterations=3,
    )

    # A successful cycle recreates the generator, so every wait starts back
    # at the beginning of the sequence rather than continuing to climb.
    assert slept == [1.0, 1.0]


def test_run_backoff_advances_across_unhealthy_cycles(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    slept: list[float] = []

    def build_client():
        return FakeClient({"/homeappliances": {"homeappliances": []}})

    monkeypatch.setattr(daemon, "stream_once", lambda *a, **k: "http_500")

    daemon.run(
        build_client=build_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=slept.append,
        delays=iter([1.0, 2.0, 4.0, 8.0]),
        iterations=3,
    )

    assert slept == [1.0, 2.0], "an unhealthy cycle must not reset the backoff"


def test_run_invalidates_token_after_a_single_401_and_refreshes(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    def build_client():
        return FakeClient({"/homeappliances": {"homeappliances": []}})

    reasons = iter(["http_401", "stream_ended"])
    monkeypatch.setattr(daemon, "stream_once", lambda *a, **k: next(reasons))

    fetch_calls: list[str] = []

    def fetch() -> str:
        fetch_calls.append(f"token-{len(fetch_calls)}")
        return fetch_calls[-1]

    holder = daemon.TokenHolder(fetch)

    daemon.run(
        build_client=build_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        token_holder=holder,
        iterations=2,
    )

    assert len(fetch_calls) == 2, "the cycle after a 401 must mint a fresh token"


def test_run_raises_after_two_consecutive_authentication_failures(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    def build_client():
        return FakeClient({"/homeappliances": {"homeappliances": []}})

    monkeypatch.setattr(daemon, "stream_once", lambda *a, **k: "http_401")

    holder = daemon.TokenHolder(lambda: "token")

    with pytest.raises(daemon.AuthenticationExhausted):
        daemon.run(
            build_client=build_client,
            session_factory=lambda: None,
            events_path=events_path,
            state_path=state_path,
            sleep=lambda _: None,
            delays=iter([0.0] * 10),
            token_holder=holder,
            iterations=5,
        )


def test_run_catches_not_authenticated_from_token_refresh(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    def build_client():
        return FakeClient({"/homeappliances": {"homeappliances": []}})

    class FakeSession:
        def get(self, *a, **k):
            return FakeStreamResponse([])

    def fetch() -> str:
        raise daemon.auth.NotAuthenticated("stored token rejected")

    holder = daemon.TokenHolder(fetch)

    with pytest.raises(daemon.AuthenticationExhausted):
        daemon.run(
            build_client=build_client,
            session_factory=FakeSession,
            events_path=events_path,
            state_path=state_path,
            sleep=lambda _: None,
            delays=iter([0.0] * 10),
            token_holder=holder,
            iterations=5,
        )


def test_main_returns_nonzero_on_authentication_exhausted(monkeypatch):
    monkeypatch.setattr(daemon.auth, "load_credentials", lambda: object())

    @contextlib.contextmanager
    def fake_lock():
        yield

    monkeypatch.setattr(daemon.store, "single_instance_lock", fake_lock)

    def fake_run(**kwargs):
        raise daemon.AuthenticationExhausted("two consecutive failures")

    monkeypatch.setattr(daemon, "run", fake_run)

    assert daemon.main() == 4


def test_stream_once_skips_events_for_an_unrecognised_haid(tmp_path):
    path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    state = {"dishwasher": {"OperationState": "Ready"}}
    response = FakeStreamResponse([
        "event: NOTIFY",
        'data: {"haId":"999999999999999999","items":'
        '[{"key":"BSH.Common.Status.OperationState",'
        '"value":"BSH.Common.EnumType.OperationState.Run"}]}',
        "",
    ])

    class FakeSession:
        def get(self, *a, **k):
            return response

    unknown: list[int] = []
    reason = daemon.stream_once(
        FakeSession(), "https://example.invalid/events", "tok",
        state, {"000000000000000000": "dishwasher"}, path, now=lambda: "T1",
        state_path=state_path, on_unknown_haid=lambda: unknown.append(1),
    )

    records, _ = store.read_records(path)
    assert records == [], "an unrecognised haId must never produce a transition"
    assert state["dishwasher"]["OperationState"] == "Ready"
    assert unknown == [1]
    assert reason == "stream_ended"


def test_stream_once_persists_state_after_each_event(tmp_path):
    path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    state = {"dishwasher": {"OperationState": "Ready"}}
    response = FakeStreamResponse([
        "event: NOTIFY",
        'data: {"haId":"000000000000000000","items":'
        '[{"key":"BSH.Common.Status.OperationState",'
        '"value":"BSH.Common.EnumType.OperationState.Run"}]}',
        "",
    ])

    class FakeSession:
        def get(self, *a, **k):
            return response

    daemon.stream_once(
        FakeSession(), "https://example.invalid/events", "tok",
        state, {"000000000000000000": "dishwasher"}, path, now=lambda: "T1",
        state_path=state_path,
    )

    saved = store.load_state(state_path)
    assert saved["dishwasher"]["OperationState"] == "Run", (
        "a kill mid-stream must not lose progress already applied to state"
    )


def test_stream_once_returns_reconcile_after_the_duration_bound(tmp_path):
    path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    state: dict = {}
    response = FakeStreamResponse([
        "event: NOTIFY",
        'data: {"haId":"000000000000000000","items":'
        '[{"key":"BSH.Common.Status.OperationState",'
        '"value":"BSH.Common.EnumType.OperationState.Run"}]}',
        "",
        "event: NOTIFY",
        'data: {"haId":"000000000000000000","items":'
        '[{"key":"BSH.Common.Status.DoorState",'
        '"value":"BSH.Common.EnumType.DoorState.Open"}]}',
        "",
    ])

    class FakeSession:
        def get(self, *a, **k):
            return response

    clock_values = iter([0.0, 0.0, float(daemon.RECONCILE_SECONDS)])

    reason = daemon.stream_once(
        FakeSession(), "https://example.invalid/events", "tok",
        state, {"000000000000000000": "dishwasher"}, path, now=lambda: "T1",
        state_path=state_path, clock=lambda: next(clock_values),
    )

    assert reason == "reconcile"
    assert state["dishwasher"]["OperationState"] == "Run"
    assert state["dishwasher"]["DoorState"] == "Open"


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
