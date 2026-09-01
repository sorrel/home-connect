import contextlib
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests

from homeconnect import auth, daemon, report, store
from homeconnect.api import NoProgrammeActive, QuotaExceeded
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
        self.closed = False

    def close(self):
        self.closed = True

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


class _SilentStreamSession:
    """A session whose stream connects, says nothing, and closes."""

    def get(self, *args, **kwargs):
        return FakeStreamResponse([])


def _stamps(step_seconds=100, start="2026-08-26T19:00:00Z"):
    """A fake `now()` whose wall clock advances by a fixed step per call.

    Gap lengths are measured on the wall clock from these very stamps, so a
    test that wants a long or a short interruption says so here rather than
    by manipulating a monotonic clock that, on a sleeping Mac, would not have
    advanced at all.
    """
    moment = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")

    def now() -> str:
        nonlocal moment
        stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        moment += timedelta(seconds=step_seconds)
        return stamp

    return now


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

    # Coverage is lost at 19:00:00 and the next successful poll is at
    # 19:05:00 — a five-minute interruption on the wall clock, comfortably
    # past GAP_THRESHOLD_SECONDS (120).
    daemon.run(
        build_client=_dishwasher_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=slept.append,
        delays_factory=lambda: iter([5.0, 5.0, 5.0]),
        now=_stamps(step_seconds=100),
        iterations=2,
    )

    records, _ = store.read_records(events_path)
    recovered = [
        r for r in records
        if r.get("event") == "coverage_gap" and r.get("reason") == "stream_lost"
    ]
    assert len(recovered) == 1, "one bracketed gap, not one per cycle"
    assert recovered[0]["from"] is not None
    assert slept == [5.0], "a reconnection must wait, not spin"


def test_run_does_not_mark_a_gap_for_a_brief_interruption(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    monkeypatch.setattr(daemon, "stream_once", lambda *a, **k: "stream_ended")

    # Ten seconds per call, so the interruption spans well under the
    # 120-second threshold on the wall clock.
    daemon.run(
        build_client=_dishwasher_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        now=_stamps(step_seconds=10),
        iterations=2,
    )

    records, _ = store.read_records(events_path)
    recovered = [
        r for r in records
        if r.get("event") == "coverage_gap" and r.get("reason") != "startup"
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


def test_run_resets_backoff_after_a_reconcile_cycle(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    slept: list[float] = []

    def build_client():
        return FakeClient({"/homeappliances": {"homeappliances": []}})

    # "reconcile" is the only ending that counts as healthy: it is a
    # deliberate, scheduled reconnection, not a lost one.
    monkeypatch.setattr(daemon, "stream_once", lambda *a, **k: "reconcile")

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


def test_run_backoff_escalates_across_repeated_stream_ended_cycles(
    tmp_path, monkeypatch
):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    slept: list[float] = []

    def build_client():
        return FakeClient({"/homeappliances": {"homeappliances": []}})

    # A connection that is accepted and then immediately dropped, repeatedly,
    # must not be treated as healthy — that would disable the backoff in
    # exactly the failure mode it exists for, and hammer an unhappy server.
    monkeypatch.setattr(daemon, "stream_once", lambda *a, **k: "stream_ended")

    daemon.run(
        build_client=build_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=slept.append,
        delays=daemon.stream.backoff_delays(
            initial=1.0, factor=2.0, jitter=lambda delay: delay
        ),
        iterations=3,
    )

    assert slept == [1.0, 2.0], "a lost connection must escalate, not stay constant"


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


def test_run_logs_unrecognised_haid_events(tmp_path, monkeypatch, capsys):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    def build_client():
        return FakeClient({"/homeappliances": {"homeappliances": []}})

    def fake_stream_once(*args, **kwargs):
        on_unknown_haid = kwargs["on_unknown_haid"]
        on_unknown_haid()
        on_unknown_haid()
        return "stream_ended"

    monkeypatch.setattr(daemon, "stream_once", fake_stream_once)

    daemon.run(
        build_client=build_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        iterations=1,
    )

    err = capsys.readouterr().err
    assert "2" in err, "the count must actually reach the log, not just the counter"


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

    # The deadline is now checked once per line (six lines here), not once
    # per parsed event, so the clock must stay under the deadline for every
    # line except the last.
    calls = [0]

    def clock() -> float:
        calls[0] += 1
        return float(daemon.RECONCILE_SECONDS) if calls[0] > 6 else 0.0

    reason = daemon.stream_once(
        FakeSession(), "https://example.invalid/events", "tok",
        state, {"000000000000000000": "dishwasher"}, path, now=lambda: "T1",
        state_path=state_path, clock=clock,
    )

    assert reason == "reconcile"
    assert state["dishwasher"]["OperationState"] == "Run"
    assert state["dishwasher"]["DoorState"] == "Open"


def test_stream_once_checks_the_deadline_on_a_comment_only_line(tmp_path):
    """A bare SSE comment (a vendor keep-alive) never becomes an event, but
    the deadline must still be checked against it — otherwise a stream that
    keeps itself alive only with comments would never reconcile."""
    path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    state: dict = {}
    response = FakeStreamResponse([":keep-alive"])

    class FakeSession:
        def get(self, *a, **k):
            return response

    clock_values = iter([0.0, float(daemon.RECONCILE_SECONDS)])

    reason = daemon.stream_once(
        FakeSession(), "https://example.invalid/events", "tok",
        state, {}, path, now=lambda: "T1",
        state_path=state_path, clock=lambda: next(clock_values),
    )

    assert reason == "reconcile"


def test_run_forwards_its_clock_to_stream_once(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    def build_client():
        return FakeClient({"/homeappliances": {"homeappliances": []}})

    captured: dict = {}

    def fake_stream_once(*args, **kwargs):
        captured["clock"] = kwargs.get("clock")
        return "stream_ended"

    monkeypatch.setattr(daemon, "stream_once", fake_stream_once)

    sentinel_clock = lambda: 0.0  # noqa: E731

    daemon.run(
        build_client=build_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        clock=sentinel_clock,
        iterations=1,
    )

    assert captured["clock"] is sentinel_clock, (
        "run()'s own clock must reach stream_once, not the real time.monotonic"
    )


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


# --- Keys that only the stream can ever report -----------------------------

SALT_EVENT = [(
    "Dishcare.Dishwasher.Event.SaltNearlyEmpty",
    "BSH.Common.EnumType.EventPresentState.Present",
)]


def test_observe_carries_event_only_keys_forward_for_a_connected_appliance():
    """A poll cannot report salt, so a poll must not appear to deny it."""
    client = _dishwasher_client()
    previous = {"dishwasher": {"SaltNearlyEmpty": "Present"}}

    observed = daemon.observe(client, previous=previous)

    assert observed["dishwasher"]["SaltNearlyEmpty"] == "Present"
    assert observed["dishwasher"]["Connected"] is True


def test_a_salt_warning_survives_the_next_reconciliation_poll(tmp_path):
    """The whole sequence, because this is the failure that mattered most.

    A salt event arrives on the stream; an hour later the hourly
    reconciliation poll rebuilds state from `/status`, which cannot report
    salt at all. Before the fix that wrote `SaltNearlyEmpty: "Present" ->
    null`, and `report.consumables` read the absence of "Present" as `ok` —
    the recorder's entire purpose failing in the dangerous direction, in a log
    that cannot be rebuilt.
    """
    events_path = tmp_path / "events.jsonl"
    client = _dishwasher_client()

    state = daemon.observe(client)
    daemon.apply_event(
        state, "dishwasher", SALT_EVENT, events_path,
        now=lambda: "2026-08-26T19:00:00Z",
    )

    observed = daemon.observe(client, previous=state)
    daemon.record_changes(
        state, observed, events_path, now=lambda: "2026-08-26T20:00:00Z"
    )

    records, _ = store.read_records(events_path)
    salt = next(a for a in report.alerts(records) if a.name == "SaltNearlyEmpty")
    assert salt.occurrences == ("2026-08-26T19:00:00Z",), \
        "a poll that cannot see salt must neither clear nor duplicate it"
    assert not [
        r for r in records
        if r.get("key") == "SaltNearlyEmpty" and r.get("to") is None
    ], "no phantom transition to null may be written"


def test_every_consumable_the_report_names_is_carried_across_a_poll():
    """The two lists must not drift apart; a missed one clears itself."""
    assert set(report.CONSUMABLE_KEYS) <= set(store.EVENT_ONLY_KEYS)


# --- One concept, one name -------------------------------------------------

def test_apply_event_folds_the_stream_programme_key_onto_the_polled_one(tmp_path):
    events_path = tmp_path / "events.jsonl"
    state = {"dishwasher": {}}

    daemon.apply_event(
        state, "dishwasher",
        [("BSH.Common.Root.ActiveProgram", "Dishcare.Dishwasher.Program.Eco50")],
        events_path, now=lambda: "T1",
    )

    assert state["dishwasher"] == {"Programme": "Eco50"}
    assert "ActiveProgram" not in state["dishwasher"]


def test_a_cycle_whose_programme_arrived_on_the_stream_is_named(tmp_path):
    """`report.cycles` matches "Programme"; the stream says "ActiveProgram"."""
    events_path = tmp_path / "events.jsonl"
    state = {"dishwasher": {"OperationState": "Ready"}}

    daemon.apply_event(
        state, "dishwasher",
        [
            ("BSH.Common.Root.ActiveProgram", "Dishcare.Dishwasher.Program.Eco50"),
            ("BSH.Common.Status.OperationState",
             "BSH.Common.EnumType.OperationState.Run"),
        ],
        events_path, now=lambda: "2026-08-26T19:00:00Z",
    )

    records, _ = store.read_records(events_path)
    cycle = report.cycles(records)[0]
    assert cycle.programme == "Eco50", "a live-observed cycle must not be unknown"


def test_a_selected_programme_is_carried_across_a_poll_too():
    """`/programs/selected` is never polled, so the stream is its only source."""
    client = _dishwasher_client()
    previous = {"dishwasher": {"SelectedProgramme": "Eco50"}}

    observed = daemon.observe(client, previous=previous)

    assert observed["dishwasher"]["SelectedProgramme"] == "Eco50"


# --- Restarts, and gaps measured on a clock that survives sleep ------------

def test_run_marks_a_gap_when_it_restarts_over_existing_state(tmp_path):
    """The commonest interruption is a restart, not a crash."""
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    store.save_state({"dishwasher": {"OperationState": "Ready"}}, state_path)
    an_hour_ago = time.time() - 3600
    os.utime(state_path, (an_hour_ago, an_hour_ago))

    daemon.run(
        build_client=_dishwasher_client,
        session_factory=_SilentStreamSession,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        iterations=1,
    )

    records, _ = store.read_records(events_path)
    markers = [r for r in records if r.get("event") == "coverage_gap"]
    assert [m["reason"] for m in markers] == ["restart"]
    assert markers[0]["from"] is not None, "the gap must say when watching stopped"


def test_run_does_not_mark_a_restart_that_lost_no_time(tmp_path):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    store.save_state({"dishwasher": {"OperationState": "Ready"}}, state_path)

    daemon.run(
        build_client=_dishwasher_client,
        session_factory=_SilentStreamSession,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        iterations=1,
    )

    records, _ = store.read_records(events_path)
    assert [r for r in records if r.get("event") == "coverage_gap"] == []


def test_run_records_a_gap_the_monotonic_clock_slept_through(tmp_path, monkeypatch):
    """macOS's monotonic clock does not advance while the machine sleeps.

    An eight-hour sleep is the exact interruption this deployment exists to
    notice, and `CLOCK_UPTIME_RAW` measures it as very nearly nothing. The
    span must therefore come off the wall clock.
    """
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    monkeypatch.setattr(daemon, "stream_once", lambda *a, **k: "stream_ended")

    daemon.run(
        build_client=_dishwasher_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        now=_stamps(step_seconds=3600),
        clock=lambda: 0.0,  # a clock frozen by sleep, as macOS's would be
        iterations=2,
    )

    records, _ = store.read_records(events_path)
    reasons = [r["reason"] for r in records if r.get("event") == "coverage_gap"]
    assert "stream_lost" in reasons


def test_run_writes_the_diagnosed_reason_into_the_marker(tmp_path, monkeypatch):
    """The reason is the only diagnostic a later reader has."""
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    endings = iter(["stream_ended", "reconcile"])
    monkeypatch.setattr(daemon, "stream_once", lambda *a, **k: next(endings))

    daemon.run(
        build_client=_dishwasher_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        now=_stamps(step_seconds=100),
        iterations=2,
    )

    records, _ = store.read_records(events_path)
    reasons = [r["reason"] for r in records if r.get("event") == "coverage_gap"]
    assert reasons == ["startup", "stream_lost"], "not the undiagnostic 'recovered'"


def test_run_marks_a_network_failure_as_such(tmp_path):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    attempts = iter([requests.ConnectionError("down"), None])

    def build_client():
        problem = next(attempts)
        if problem is not None:
            raise problem
        return _dishwasher_client()

    daemon.run(
        build_client=build_client,
        session_factory=_SilentStreamSession,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        now=_stamps(step_seconds=200),
        iterations=2,
    )

    records, _ = store.read_records(events_path)
    reasons = [r["reason"] for r in records if r.get("event") == "coverage_gap"]
    assert "network_error" in reasons


# --- Failures that must not kill a process meant to run for weeks ----------

def test_run_treats_a_locked_keychain_as_an_authentication_failure(
    tmp_path, capsys
):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    def build_client():
        raise auth.KeyringError("the Keychain is locked")

    with pytest.raises(daemon.AuthenticationExhausted):
        daemon.run(
            build_client=build_client,
            session_factory=lambda: None,
            events_path=events_path,
            state_path=state_path,
            sleep=lambda _: None,
            delays=iter([0.0] * 10),
            iterations=5,
        )

    assert "Keychain" in capsys.readouterr().err


def test_run_backs_off_far_harder_when_the_quota_is_exhausted(tmp_path):
    """Re-asking an exhausted daily quota every five minutes keeps it that way."""
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    slept: list[float] = []

    def build_client():
        raise QuotaExceeded("SDK.Error.RequestQuotaExceeded")

    daemon.run(
        build_client=build_client,
        session_factory=lambda: None,
        events_path=events_path,
        state_path=state_path,
        sleep=slept.append,
        delays=iter([0.5] * 10),
        iterations=2,
    )

    assert slept == [daemon.QUOTA_BACKOFF_SECONDS]
    assert daemon.QUOTA_BACKOFF_SECONDS > 300, "harder than the backoff ceiling"


def test_stream_once_closes_a_rejected_response(tmp_path):
    """A rejected stream is still an open connection."""
    response = FakeStreamResponse([], status_code=401)

    class FakeSession:
        def get(self, url, headers=None, stream=None, timeout=None):
            return response

    reason = daemon.stream_once(
        FakeSession(), "https://example.invalid/events", "tok", {}, {},
        tmp_path / "events.jsonl", now=lambda: "T1",
        state_path=tmp_path / "state.json",
    )

    assert reason == "http_401"
    assert response.closed, "one leaked connection per failing cycle otherwise"


def test_log_prefixes_a_utc_instant(capsys):
    """launchd appends and never truncates, so an unstamped line is unreadable."""
    daemon.log("hello")
    out = capsys.readouterr().out.strip()
    stamp, _, message = out.partition(" ")
    assert message == "hello"
    assert stamp.endswith("Z")
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0)
    assert abs(parsed - datetime.now(timezone.utc)) < timedelta(seconds=5)


def test_log_sends_errors_to_stderr_and_notices_to_stdout(capsys):
    daemon.log("a notice")
    daemon.log("a problem", error=True)
    captured = capsys.readouterr()
    assert "a notice" in captured.out
    assert "a notice" not in captured.err
    assert "a problem" in captured.err
    assert "a problem" not in captured.out


# --- What a network failure leaves behind in the log ----------------------


def test_run_logs_the_detail_of_a_network_error(tmp_path, capsys):
    """A `network_error` marker names no cause; the log must name one.

    `events.jsonl` records that coverage was lost and nothing about why. The
    reason lives only in the exception, so if it is not logged here it is
    gone — which is how a nineteen-hour outage came to have no diagnosis.
    """
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    attempts = iter([requests.ConnectionError("name resolution failed"), None])

    def build_client():
        problem = next(attempts)
        if problem is not None:
            raise problem
        return _dishwasher_client()

    daemon.run(
        build_client=build_client,
        session_factory=_SilentStreamSession,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        now=_stamps(step_seconds=200),
        iterations=2,
    )

    err = capsys.readouterr().err
    assert "ConnectionError" in err, "the exception type must survive"
    assert "name resolution failed" in err, "the exception message must survive"


def test_run_logs_a_network_error_once_not_once_per_retry(tmp_path, capsys):
    """These logs are never rotated, so a long outage must not flood them.

    The backoff tops out at five minutes, so nineteen hours of failure is
    upwards of two hundred cycles. One line per cycle would bury the very
    thing the log is being read for.
    """
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    def build_client():
        raise requests.ConnectionError("still down")

    daemon.run(
        build_client=build_client,
        session_factory=_SilentStreamSession,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        now=_stamps(step_seconds=200),
        iterations=5,
    )

    err = capsys.readouterr().err
    assert err.count("still down") == 1, "one line for the whole outage"


def test_run_logs_how_long_coverage_was_lost_when_it_resumes(tmp_path, capsys):
    """The recovery is as worth knowing as the failure, and carries the length."""
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    attempts = iter([requests.ConnectionError("down"), None])

    def build_client():
        problem = next(attempts)
        if problem is not None:
            raise problem
        return _dishwasher_client()

    daemon.run(
        build_client=build_client,
        session_factory=_SilentStreamSession,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        now=_stamps(step_seconds=200),
        iterations=2,
    )

    out = capsys.readouterr().out
    assert "resumed" in out
    # Two stamps are drawn between coverage being lost and the poll that
    # ends it, so the gap spans two 200-second steps.
    assert "400s" in out, "the gap's length in seconds"


# --- Escalation: an outage the backoff loop alone never clears --------------


def test_run_raises_after_a_coverage_gap_exceeds_the_escalation_threshold(tmp_path):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"

    def build_client():
        raise requests.ConnectionError("still down")

    with pytest.raises(daemon.CoverageExhausted):
        daemon.run(
            build_client=build_client,
            session_factory=lambda: None,
            events_path=events_path,
            state_path=state_path,
            sleep=lambda _: None,
            delays=iter([0.0] * 10),
            now=_stamps(step_seconds=1000),
            iterations=5,
        )


def test_run_does_not_escalate_a_gap_under_the_threshold(tmp_path):
    """Two short-lived outages must not sum towards the threshold."""
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    attempts = iter([
        requests.ConnectionError("down"), None,
        requests.ConnectionError("down"), None,
    ])

    def build_client():
        problem = next(attempts)
        if problem is not None:
            raise problem
        return _dishwasher_client()

    daemon.run(
        build_client=build_client,
        session_factory=_SilentStreamSession,
        events_path=events_path,
        state_path=state_path,
        sleep=lambda _: None,
        delays=iter([0.0] * 10),
        now=_stamps(step_seconds=1000),
        iterations=4,
    )
    # Reaching here at all is the assertion: each outage recovers before the
    # next one starts, so neither alone nor combined should ever escalate.


def test_run_notifies_desktop_when_escalating(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    notified: list[str] = []
    monkeypatch.setattr(daemon, "notify_desktop", notified.append)

    def build_client():
        raise requests.ConnectionError("still down")

    with pytest.raises(daemon.CoverageExhausted):
        daemon.run(
            build_client=build_client,
            session_factory=lambda: None,
            events_path=events_path,
            state_path=state_path,
            sleep=lambda _: None,
            delays=iter([0.0] * 10),
            now=_stamps(step_seconds=1000),
            iterations=5,
        )

    assert len(notified) == 1, "one notification for the whole outage, not one per retry"


def test_main_returns_nonzero_on_coverage_exhausted(monkeypatch):
    monkeypatch.setattr(daemon.auth, "load_credentials", lambda: object())

    @contextlib.contextmanager
    def fake_lock():
        yield

    monkeypatch.setattr(daemon.store, "single_instance_lock", fake_lock)

    def fake_run(**kwargs):
        raise daemon.CoverageExhausted("coverage lost for 1900s")

    monkeypatch.setattr(daemon, "run", fake_run)

    assert daemon.main() == 5


def test_notify_desktop_never_raises_when_osascript_is_unavailable(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("no osascript on this machine")

    monkeypatch.setattr(daemon.subprocess, "run", boom)

    daemon.notify_desktop("a test message")  # must not raise
