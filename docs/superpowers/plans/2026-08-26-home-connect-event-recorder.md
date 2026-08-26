# Home Connect Event Recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An always-running listener that records appliance state transitions to append-only JSONL, plus a command that reports on them honestly about coverage.

**Architecture:** Extends the existing `homeconnect` package; `api.py`, `auth.py` and `appliances.py` are reused unchanged. An SSE stream supplies live events at low overhead, an hourly reconciliation poll re-establishes ground truth, and every loss of coverage is recorded so reports can distinguish "nothing happened" from "nobody was watching".

**Tech Stack:** Python >= 3.12, uv, Click, requests, pytest, launchd.

**Spec:** `docs/superpowers/specs/2026-08-26-home-connect-event-recorder-design.md`

**Status (26 August 2026):** Not started. The CLI it builds on is complete, committed and live-tested.

## Global Constraints

- **Read-only.** Same `IdentifyAppliance Monitor` token. No POST/PUT/PATCH/DELETE except the three existing OAuth token endpoints in `auth.py`. The recorder observes; it never acts.
- **Append-only.** Lines are appended, never rewritten. A corrupt line is skipped and counted, never repaired in place.
- **Unknown is not ok.** Where coverage was lost, the log says so and reports render affected values as `unknown`, never as a reassuring default.
- **The `haId` never enters the log or any report.** It is a device serial. Use the stable human label instead.
- **Timestamps are UTC, ISO 8601, with a trailing `Z`.** The clock change must not corrupt ordering.
- **No live API calls in tests, ever.** Fake sessions and fixtures throughout. A test that opens a real stream would hang, not fail.
- **`data/` is gitignored.** Never assert in a test that it exists; tests use `tmp_path`.
- British English in code, comments, docstrings, output and commit messages; vendor key strings keep the vendor's spelling.
- No credentials, real email addresses, hostnames, developer-portal user IDs, appliance serials, or `/Users/<name>/...` paths in any tracked file. This repository is intended to become public.
- Python >= 3.12. Run everything via `uv`.
- Never commit to `main`. Commits are signed as the user; if 1Password is locked, leave work staged and say so.

---

### Task 1: The store — JSONL, state, and transition diffing

**Files:**
- Create: `src/homeconnect/store.py`
- Create: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `DATA_DIR: Path`, `EVENTS_PATH: Path`, `STATE_PATH: Path`, `LOCK_PATH: Path`
  - `def short_key(key: str) -> str`
  - `def utc_now() -> str`
  - `def transition_record(ts: str, label: str, key: str, before, after) -> dict`
  - `def gap_record(ts: str, since: str | None, reason: str) -> dict`
  - `def append_record(record: dict, path: Path) -> None`
  - `def read_records(path: Path) -> tuple[list[dict], int]` — records and the count of unparseable lines skipped
  - `def load_state(path: Path) -> dict`
  - `def save_state(state: dict, path: Path) -> None`
  - `def diff_states(before: dict, after: dict) -> list[tuple[str, str, object, object]]` — `(label, key, before, after)`
  - `def single_instance_lock(path: Path)` — a context manager

- [ ] **Step 1: Write the failing tests**

`tests/test_store.py`:

```python
import json

import pytest

from homeconnect import store


def test_short_key_strips_the_vendor_path():
    assert store.short_key("BSH.Common.Status.OperationState") == "OperationState"
    assert store.short_key("Dishcare.Dishwasher.Event.SaltNearlyEmpty") == "SaltNearlyEmpty"
    assert store.short_key("Programme") == "Programme"


def test_utc_now_is_iso_utc_with_a_trailing_z():
    stamp = store.utc_now()
    assert stamp.endswith("Z")
    assert "T" in stamp
    assert "+" not in stamp


def test_transition_record_shape():
    record = store.transition_record(
        "2026-08-26T19:04:11Z", "dishwasher",
        "BSH.Common.Status.OperationState", "Ready", "Run",
    )
    assert record == {
        "ts": "2026-08-26T19:04:11Z",
        "ha": "dishwasher",
        "key": "OperationState",
        "from": "Ready",
        "to": "Run",
    }


def test_records_never_contain_a_device_identifier():
    """haId is a device serial and must not reach the log."""
    record = store.transition_record(
        "2026-08-26T19:04:11Z", "dishwasher",
        "BSH.Common.Status.DoorState", None, "Open",
    )
    assert "haId" not in json.dumps(record)


def test_append_and_read_round_trip(tmp_path):
    path = tmp_path / "events.jsonl"
    store.append_record({"ts": "1", "a": 1}, path)
    store.append_record({"ts": "2", "a": 2}, path)

    records, skipped = store.read_records(path)

    assert [r["a"] for r in records] == [1, 2]
    assert skipped == 0


def test_append_creates_the_directory(tmp_path):
    """data/ is gitignored, so it will not exist on a fresh clone."""
    path = tmp_path / "nested" / "events.jsonl"
    store.append_record({"ts": "1"}, path)
    assert path.is_file()


def test_read_skips_corrupt_lines_and_counts_them(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"ts": "1"}\nnot json at all\n{"ts": "2"}\n')

    records, skipped = store.read_records(path)

    assert len(records) == 2
    assert skipped == 1


def test_read_missing_file_is_empty_not_an_error(tmp_path):
    records, skipped = store.read_records(tmp_path / "absent.jsonl")
    assert records == []
    assert skipped == 0


def test_state_round_trip(tmp_path):
    path = tmp_path / "state.json"
    store.save_state({"dishwasher": {"OperationState": "Ready"}}, path)
    assert store.load_state(path) == {"dishwasher": {"OperationState": "Ready"}}


def test_load_state_missing_or_corrupt_is_empty(tmp_path):
    assert store.load_state(tmp_path / "absent.json") == {}
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    assert store.load_state(corrupt) == {}


def test_diff_states_reports_only_changes():
    before = {"dishwasher": {"OperationState": "Ready", "DoorState": "Open"}}
    after = {"dishwasher": {"OperationState": "Run", "DoorState": "Open"}}

    assert store.diff_states(before, after) == [
        ("dishwasher", "OperationState", "Ready", "Run")
    ]


def test_diff_states_reports_newly_appearing_keys():
    before = {"dishwasher": {}}
    after = {"dishwasher": {"SaltNearlyEmpty": "Present"}}

    assert store.diff_states(before, after) == [
        ("dishwasher", "SaltNearlyEmpty", None, "Present")
    ]


def test_diff_states_reports_a_disappearing_key_as_none():
    before = {"dishwasher": {"Programme": "Eco50"}}
    after = {"dishwasher": {}}

    assert store.diff_states(before, after) == [
        ("dishwasher", "Programme", "Eco50", None)
    ]


def test_diff_states_handles_an_appliance_seen_for_the_first_time():
    assert store.diff_states({}, {"oven": {"OperationState": "Ready"}}) == [
        ("oven", "OperationState", None, "Ready")
    ]


def test_single_instance_lock_excludes_a_second_holder(tmp_path):
    path = tmp_path / "recorder.lock"
    with store.single_instance_lock(path):
        with pytest.raises(store.AlreadyRunning):
            with store.single_instance_lock(path):
                pass


def test_single_instance_lock_releases_on_exit(tmp_path):
    path = tmp_path / "recorder.lock"
    with store.single_instance_lock(path):
        pass
    with store.single_instance_lock(path):
        pass
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'homeconnect.store'`

- [ ] **Step 3: Write `src/homeconnect/store.py`**

```python
"""Append-only JSONL log, last-known state, and transition diffing.

The log is the only record that a transition ever happened: the API exposes no
history, and its event stream reports changes only. Lines are therefore appended
and never rewritten, and a line that will not parse is skipped and counted rather
than repaired — losing one observation is far better than corrupting the rest.
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DATA_DIR = Path("data")
EVENTS_PATH = DATA_DIR / "events.jsonl"
STATE_PATH = DATA_DIR / "state.json"
LOCK_PATH = DATA_DIR / "recorder.lock"


class AlreadyRunning(Exception):
    """Another recorder already holds the lock."""


def short_key(key: str) -> str:
    """`BSH.Common.Status.OperationState` -> `OperationState`.

    The full vendor path is recoverable and the short form is what a reader of
    the log actually wants.
    """
    return key.rsplit(".", 1)[-1]


def utc_now() -> str:
    """An ISO 8601 instant in UTC, with a trailing Z.

    Always UTC: a log stamped in local time reorders itself when the clocks go
    back, and this file is append-only, so that damage would be permanent.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def transition_record(ts: str, label: str, key: str, before: Any, after: Any) -> dict:
    """One observed change.

    `label` is the appliance's human name, never its haId — that is a device
    serial, and a local file is exactly the sort of thing that gets pasted into
    a bug report.
    """
    return {
        "ts": ts,
        "ha": label,
        "key": short_key(key),
        "from": before,
        "to": after,
    }


def gap_record(ts: str, since: str | None, reason: str) -> dict:
    """A period during which nothing was watching.

    Without these a reader cannot tell an uneventful night from a sleeping
    laptop, and would read silence as reassurance.
    """
    return {"ts": ts, "event": "coverage_gap", "from": since, "reason": reason}


def append_record(record: dict, path: Path = EVENTS_PATH) -> None:
    """Append one record. Creates the directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def read_records(path: Path = EVENTS_PATH) -> tuple[list[dict], int]:
    """Return every parseable record, and how many lines were skipped."""
    if not path.is_file():
        return [], 0

    records: list[dict] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
        else:
            skipped += 1
    return records, skipped


def load_state(path: Path = STATE_PATH) -> dict:
    """Last-known state, or empty if absent or unreadable.

    An unreadable state file is treated as no state: the next reconciliation
    poll rebuilds it, and every rebuilt key is reported as a transition from
    `null`, which is honest.
    """
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    """Write state atomically, so a crash mid-write cannot truncate it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def diff_states(before: dict, after: dict) -> list[tuple[str, str, Any, Any]]:
    """Every value that differs, as `(label, key, before, after)`.

    A key that appears is a change from `None`; a key that disappears is a
    change to `None` — an active programme ending is exactly that.
    """
    changes: list[tuple[str, str, Any, Any]] = []
    for label in sorted(set(before) | set(after)):
        old = before.get(label, {}) or {}
        new = after.get(label, {}) or {}
        for key in sorted(set(old) | set(new)):
            was, now = old.get(key), new.get(key)
            if was != now:
                changes.append((label, key, was, now))
    return changes


@contextmanager
def single_instance_lock(path: Path = LOCK_PATH) -> Iterator[None]:
    """Ensure only one recorder writes to the log.

    Two recorders would interleave transitions and double-count cycles. The lock
    is advisory and released by the OS if the process dies, so a crash does not
    leave it wedged.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning(f"another recorder holds {path}") from exc
        yield
    finally:
        handle.close()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_store.py -v`
Expected: 16 passed

- [ ] **Step 5: Commit**

```bash
git add src/homeconnect/store.py tests/test_store.py
git commit -m "feat: add append-only event store with transition diffing"
```

---

### Task 2: The SSE stream client

**Files:**
- Create: `src/homeconnect/stream.py`
- Create: `tests/test_stream.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `EVENTS_PATH_SUFFIX: str` (the literal `"/homeappliances/events"`)
  - `def parse_sse(lines: Iterable[str]) -> Iterator[dict]` — yields `{"event": str, "data": dict | None}`
  - `def backoff_delays(initial: float = 1.0, cap: float = 300.0, factor: float = 2.0, jitter=None) -> Iterator[float]`
  - `def extract_changes(payload: dict) -> list[tuple[str, Any]]` — `(key, value)` pairs from an event body

**Design note:** this module parses and computes delays. It opens no connection
of its own beyond a session handed to it, so every test drives it with a list of
strings. Nothing here can reach the network by accident.

- [ ] **Step 1: Write the failing tests**

`tests/test_stream.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_stream.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'homeconnect.stream'`

- [ ] **Step 3: Write `src/homeconnect/stream.py`**

```python
"""Server-sent events: parsing, and reconnection timing.

The vendor's stream reports changes only — connecting to it sends nothing, which
was confirmed against the live API. Everything here is therefore about surviving
a long-lived connection: parsing what arrives, and deciding how long to wait when
it stops arriving.

This module opens no connection of its own. It is handed lines, and it yields
events, so nothing in it can reach the network.
"""

from __future__ import annotations

import json
import random
from typing import Any, Callable, Iterable, Iterator

EVENTS_PATH_SUFFIX = "/homeappliances/events"


def _decode(raw: str) -> dict | None:
    """Parse an event body, or return None if it will not parse.

    A malformed body must never end the stream: losing one event costs a
    transition, whereas raising here would cost every event after it.
    """
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_sse(lines: Iterable[str]) -> Iterator[dict]:
    """Yield `{"event": name, "data": body-or-None}` for each SSE event."""
    name: str | None = None
    body: list[str] = []

    def flush() -> dict | None:
        if name is None and not body:
            return None
        return {"event": name or "message", "data": _decode("".join(body))}

    for line in lines:
        line = line.rstrip("\n").rstrip("\r")
        if line.startswith(":"):
            continue  # an SSE comment, commonly used as a keep-alive
        if not line:
            event = flush()
            if event is not None:
                yield event
            name, body = None, []
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            name = value
        elif field == "data":
            body.append(value)

    event = flush()
    if event is not None:
        yield event


def extract_changes(payload: dict | None) -> list[tuple[str, Any]]:
    """Pull `(key, value)` pairs out of an event body, tolerating odd shapes."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [
        (item["key"], item.get("value"))
        for item in items
        if isinstance(item, dict) and "key" in item
    ]


def backoff_delays(
    initial: float = 1.0,
    cap: float = 300.0,
    factor: float = 2.0,
    jitter: Callable[[float], float] | None = None,
) -> Iterator[float]:
    """Yield reconnection delays, growing geometrically to a ceiling.

    Jittered by default: without it every client that lost an outage reconnects
    in lockstep the instant the service returns, which is how an outage becomes
    a second outage.
    """
    if jitter is None:
        jitter = lambda delay: random.uniform(delay / 2, delay)  # noqa: E731

    delay = initial
    while True:
        yield jitter(delay)
        delay = min(delay * factor, cap)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_stream.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/homeconnect/stream.py tests/test_stream.py
git commit -m "feat: add SSE parsing and jittered reconnection backoff"
```

---

### Task 3: The daemon

**Files:**
- Create: `src/homeconnect/daemon.py`
- Create: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `store` (all of it), `stream.parse_sse`, `stream.extract_changes`, `stream.backoff_delays`, `appliances.list_appliances`, `appliances.fetch_state`, `api.Client`, `api.HomeConnectError`.
- Produces:
  - `def label_for(appliance) -> str`
  - `def observe(client, found: list | None = None) -> dict[str, dict]` — label -> flat `{short_key: value}`; `found` reuses an existing enumeration
  - `def record_changes(before: dict, after: dict, events_path, now=store.utc_now) -> int`
  - `def apply_event(state: dict, label: str, changes: list, events_path, now=store.utc_now) -> None`
  - `def record_gap(since: str | None, reason: str, events_path, now=store.utc_now) -> None`

**Design note:** every function here takes its dependencies as arguments so
tests never touch the network, the Keychain, or `data/`. The long-running
`main()` is a thin wrapper over these; the logic lives in the testable parts.

- [ ] **Step 1: Write the failing tests**

`tests/test_daemon.py`:

```python
import json

from homeconnect import daemon, store
from homeconnect.api import NoProgrammeActive
from homeconnect.appliances import Appliance


DISHWASHER = Appliance(
    ha_id="000000000000000000", name="Dishwasher", type="Dishwasher",
    brand="Bosch", vib="SMV000000", enumber="SMV000000/00", connected=True,
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


def test_no_record_ever_contains_the_device_serial(tmp_path):
    path = tmp_path / "events.jsonl"
    daemon.record_changes(
        {}, {"dishwasher": {"OperationState": "Run"}}, path, now=lambda: "T0"
    )
    assert "000000000000000000" not in path.read_text()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_daemon.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'homeconnect.daemon'`

- [ ] **Step 3: Write `src/homeconnect/daemon.py`**

```python
"""The recorder: seed, listen, reconcile, and mark what was missed.

The vendor's stream reports changes only and sends nothing on connect, so the
recorder must poll once to establish a baseline before it has anything to diff
against. It then holds the stream, and polls again hourly to re-establish ground
truth — cheap insurance against a missed event or a silent disconnection.

Every function that matters takes its dependencies as arguments, so the tests
drive all of it without a network, a Keychain, or a data directory.
"""

from __future__ import annotations

from typing import Any, Callable

from . import appliances as appliances_module
from . import store, stream
from .api import NoProgrammeActive

#: Poll again this often even while the stream looks healthy.
RECONCILE_SECONDS = 3600


def label_for(appliance: Any) -> str:
    """A stable, human label for an appliance.

    Deliberately the name and never the haId: the haId is a device serial, and
    this value is written to a file people will paste into bug reports.
    """
    return (appliance.name or appliance.type or "appliance").strip().lower()


def observe(client: Any, found: list | None = None) -> dict[str, dict]:
    """Poll every appliance and flatten it to `{label: {short_key: value}}`.

    `found` lets a caller that has already enumerated pass the result in rather
    than paying for a second listing.
    """
    observed: dict[str, dict] = {}
    for appliance in (found if found is not None else
                      appliances_module.list_appliances(client)):
        state = appliances_module.fetch_state(client, appliance)
        flat = {
            store.short_key(key): _tail(value)
            for key, value in state.status.items()
        }
        if state.programme is not None:
            flat["Programme"] = store.short_key(state.programme.key)
            for key, value in state.programme.options.items():
                flat[store.short_key(key)] = _tail(value)
        observed[label_for(appliance)] = flat
    return observed


def _tail(value: Any) -> Any:
    """Reduce a vendor enum to its final segment; leave other values alone."""
    if isinstance(value, str) and value.startswith("BSH.") and "." in value:
        return value.rsplit(".", 1)[-1]
    return value


def record_changes(
    before: dict,
    after: dict,
    events_path=store.EVENTS_PATH,
    now: Callable[[], str] = store.utc_now,
) -> int:
    """Append one line per differing value. Returns how many were written."""
    stamp = now()
    changes = store.diff_states(before, after)
    for label, key, was, is_now in changes:
        store.append_record(
            store.transition_record(stamp, label, key, was, is_now), events_path
        )
    return len(changes)


def apply_event(
    state: dict,
    label: str,
    changes: list[tuple[str, Any]],
    events_path=store.EVENTS_PATH,
    now: Callable[[], str] = store.utc_now,
) -> None:
    """Fold a stream event into state, logging only genuine transitions.

    The vendor repeats values freely — a repeated value is not a change, and
    logging it would inflate every cycle count drawn from this file.
    """
    stamp = now()
    current = state.setdefault(label, {})
    for raw_key, raw_value in changes:
        key = store.short_key(raw_key)
        value = _tail(raw_value)
        if current.get(key) != value:
            store.append_record(
                store.transition_record(stamp, label, key, current.get(key), value),
                events_path,
            )
            current[key] = value


def record_gap(
    since: str | None,
    reason: str,
    events_path=store.EVENTS_PATH,
    now: Callable[[], str] = store.utc_now,
) -> None:
    """Mark a period during which nothing was watching."""
    store.append_record(store.gap_record(now(), since, reason), events_path)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_daemon.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/homeconnect/daemon.py tests/test_daemon.py
git commit -m "feat: add recorder logic for seeding, folding events, and marking gaps"
```

---

### Task 4: The run loop and its entry point

**Files:**
- Modify: `src/homeconnect/daemon.py` (add the long-running parts)
- Modify: `tests/test_daemon.py` (add loop tests)

**Interfaces:**
- Consumes: everything Task 3 produced, plus `auth`, `api`.
- Produces:
  - `def stream_once(session, url, token, state, labels: dict[str, str], events_path, now) -> str` — consumes one connection to exhaustion, returns the reason it ended. `labels` maps haId to the human label, so the haId is resolved away before anything is written.
  - `def run(build_client, session_factory, events_path, state_path, sleep, delays, iterations=None) -> None`
  - `def main() -> int` — the console-script entry point

**Design note:** `run()` takes an `iterations` bound so tests can drive a finite
number of cycles. Production passes `None` for an unbounded loop. Nothing else
about the loop differs between test and production — a loop that is only ever
tested in a special mode is not tested.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_daemon.py -v`
Expected: FAIL with `AttributeError: module 'homeconnect.daemon' has no attribute 'stream_once'`

- [ ] **Step 3: Add the loop to `src/homeconnect/daemon.py`**

Append these imports at the top of the existing file:

```python
import sys
import time

import requests

from . import auth
from .api import ACCEPT, BASE_URL, Client, HomeConnectError
```

Append to the end of the file:

```python
def stream_once(
    session: Any,
    url: str,
    token: str,
    state: dict,
    labels: dict[str, str],
    events_path=store.EVENTS_PATH,
    now: Callable[[], str] = store.utc_now,
) -> str:
    """Consume one stream connection until it ends. Returns why it ended."""
    response = session.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        stream=True,
        timeout=(10, 90),
    )
    if getattr(response, "status_code", 200) != 200:
        return f"http_{response.status_code}"

    with response:
        for event in stream.parse_sse(response.iter_lines(decode_unicode=True)):
            if event["event"] == "KEEP-ALIVE" or not event["data"]:
                continue
            payload = event["data"]
            label = labels.get(str(payload.get("haId", "")), "appliance")
            apply_event(
                state, label, stream.extract_changes(payload), events_path, now
            )
    return "stream_ended"


def run(
    build_client: Callable[[], Any],
    session_factory: Callable[[], Any],
    events_path=store.EVENTS_PATH,
    state_path=store.STATE_PATH,
    sleep: Callable[[float], None] = time.sleep,
    delays: Any = None,
    iterations: int | None = None,
    now: Callable[[], str] = store.utc_now,
    token_provider: Callable[[], str] | None = None,
) -> None:
    """Seed, listen, reconcile, and mark every gap. Loops until `iterations`.

    `iterations=None` means forever. Tests pass a finite count; nothing else
    about the loop differs between test and production, because a loop that only
    runs in a special mode is not a loop anybody has tested.
    """
    if delays is None:
        delays = stream.backoff_delays()

    state = store.load_state(state_path)
    if not state:
        record_gap(None, "startup", events_path, now)

    completed = 0
    while iterations is None or completed < iterations:
        last_good = now()
        try:
            client = build_client()
            found = appliances_module.list_appliances(client)
            # Enumerate once and reuse: a second listing per cycle would spend
            # quota to re-learn what we already know.
            labels = {a.ha_id: label_for(a) for a in found}
            observed = observe(client, found)
            record_changes(state, observed, events_path, now)
            state = observed
            store.save_state(state, state_path)

            token = token_provider() if token_provider else ""
            reason = stream_once(
                session_factory(), f"{BASE_URL}{stream.EVENTS_PATH_SUFFIX}",
                token, state, labels, events_path, now,
            )
        except HomeConnectError as exc:
            reason = f"api_error:{type(exc).__name__}"
        except requests.RequestException:
            reason = "network_error"

        store.save_state(state, state_path)
        record_gap(last_good, reason, events_path, now)

        completed += 1
        if iterations is None or completed < iterations:
            sleep(next(delays))


def main() -> int:
    """Console-script entry point for the recorder."""
    try:
        credentials = auth.load_credentials()
    except auth.MissingCredentials as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    cached: list[str] = []

    def token_provider() -> str:
        if not cached:
            cached.append(auth.access_token(credentials))
        return cached[0]

    try:
        with store.single_instance_lock():
            run(
                build_client=lambda: Client(token_provider=token_provider),
                session_factory=requests.Session,
                token_provider=token_provider,
            )
    except store.AlreadyRunning as exc:
        print(f"{exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 0
    return 0
```

Add to `pyproject.toml` under `[project.scripts]`:

```toml
homeconnect-recorder = "homeconnect.daemon:main"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_daemon.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add src/homeconnect/daemon.py tests/test_daemon.py pyproject.toml
git commit -m "feat: add the recorder run loop and its entry point"
```

---

### Task 5: Reporting

**Files:**
- Create: `src/homeconnect/report.py`
- Create: `tests/test_report.py`
- Modify: `src/homeconnect/cli.py` (add the `history` command)
- Modify: `tests/test_cli.py` (add its tests)

**Interfaces:**
- Consumes: `store.read_records`.
- Produces:
  - `@dataclass Cycle` with `label: str`, `started: str`, `ended: str | None`, `programme: str | None`
  - `@dataclass Consumable` with `name: str`, `state: str`, `since: str | None` — `state` is one of `ok`, `low`, `unknown`
  - `def cycles(records: list[dict]) -> list[Cycle]`
  - `def consumables(records: list[dict]) -> list[Consumable]`
  - `def coverage(records: list[dict]) -> tuple[int, str | None]` — gap count and the latest gap's timestamp
  - `def render(records: list[dict], skipped: int) -> str`

**Design note — the point of this task.** A consumable whose last news predates
a coverage gap must be reported `unknown`, never `ok`. The whole log is
untrustworthy in exactly that one way, and a report that hides it is worse than
no report.

- [ ] **Step 1: Write the failing tests**

`tests/test_report.py`:

```python
from homeconnect import report

RUN = {"ts": "2026-08-26T19:00:00Z", "ha": "dishwasher",
       "key": "OperationState", "from": "Ready", "to": "Run"}
PROG = {"ts": "2026-08-26T19:00:00Z", "ha": "dishwasher",
        "key": "Programme", "from": None, "to": "Eco50"}
FINISHED = {"ts": "2026-08-26T21:00:00Z", "ha": "dishwasher",
            "key": "OperationState", "from": "Run", "to": "Finished"}
GAP = {"ts": "2026-08-27T07:00:00Z", "event": "coverage_gap",
       "from": "2026-08-26T23:00:00Z", "reason": "stream_lost"}
SALT_LOW = {"ts": "2026-08-26T08:00:00Z", "ha": "dishwasher",
            "key": "SaltNearlyEmpty", "from": None, "to": "Present"}


def test_a_completed_cycle_is_paired():
    found = report.cycles([RUN, PROG, FINISHED])
    assert len(found) == 1
    assert found[0].started == "2026-08-26T19:00:00Z"
    assert found[0].ended == "2026-08-26T21:00:00Z"
    assert found[0].programme == "Eco50"


def test_an_unfinished_cycle_has_no_end():
    found = report.cycles([RUN, PROG])
    assert found[0].ended is None


def test_a_cycle_with_no_programme_line_is_still_counted():
    found = report.cycles([RUN, FINISHED])
    assert len(found) == 1
    assert found[0].programme is None


def test_a_consumable_reported_low_reads_low():
    found = {c.name: c for c in report.consumables([SALT_LOW])}
    assert found["SaltNearlyEmpty"].state == "low"
    assert found["SaltNearlyEmpty"].since == "2026-08-26T08:00:00Z"


def test_a_consumable_whose_news_predates_a_gap_is_unknown():
    """The whole point: silence after a gap is not reassurance."""
    found = {c.name: c for c in report.consumables([SALT_LOW, GAP])}
    assert found["SaltNearlyEmpty"].state == "unknown"


def test_a_consumable_reported_after_the_last_gap_is_trusted():
    later = dict(SALT_LOW, ts="2026-08-27T09:00:00Z")
    found = {c.name: c for c in report.consumables([SALT_LOW, GAP, later])}
    assert found["SaltNearlyEmpty"].state == "low"


def test_coverage_counts_gaps():
    count, latest = report.coverage([RUN, GAP, FINISHED])
    assert count == 1
    assert latest == "2026-08-27T07:00:00Z"


def test_render_mentions_skipped_lines_when_there_are_any():
    text = report.render([RUN], skipped=3)
    assert "3" in text


def test_render_says_so_when_the_log_is_empty():
    assert "no" in report.render([], skipped=0).lower()


def test_render_never_claims_ok_after_a_gap():
    text = report.render([SALT_LOW, GAP], skipped=0)
    assert "unknown" in text.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'homeconnect.report'`

- [ ] **Step 3: Write `src/homeconnect/report.py`**

```python
"""Turn the event log into answers, without overclaiming.

The log records only what was observed. Where coverage was lost, silence means
nothing was watched, not that nothing happened — so anything whose last news
predates the most recent gap is reported `unknown` rather than `ok`. Getting
this wrong would produce a report that cheerfully implies the salt is fine.
"""

from __future__ import annotations

from dataclasses import dataclass

CONSUMABLE_KEYS = ("SaltNearlyEmpty", "RinseAidNearlyEmpty", "MachineCareReminder")

RUNNING = "Run"
FINISHED_STATES = ("Finished", "Inactive", "Ready")


@dataclass(frozen=True)
class Cycle:
    """One observed run, which may not have been seen to finish."""

    label: str
    started: str
    ended: str | None
    programme: str | None


@dataclass(frozen=True)
class Consumable:
    """A consumable's believed state: `ok`, `low`, or `unknown`."""

    name: str
    state: str
    since: str | None


def _transitions(records: list[dict]) -> list[dict]:
    return [r for r in records if "key" in r and "event" not in r]


def _last_gap(records: list[dict]) -> str | None:
    stamps = [r["ts"] for r in records if r.get("event") == "coverage_gap"]
    return max(stamps) if stamps else None


def cycles(records: list[dict]) -> list[Cycle]:
    """Pair each transition into `Run` with the next one out of it."""
    found: list[Cycle] = []
    open_cycles: dict[str, dict] = {}

    for record in sorted(_transitions(records), key=lambda r: r["ts"]):
        label = record.get("ha", "appliance")
        if record["key"] == "Programme" and label in open_cycles:
            open_cycles[label]["programme"] = record.get("to")
            continue
        if record["key"] != "OperationState":
            continue

        if record.get("to") == RUNNING:
            open_cycles[label] = {"started": record["ts"], "programme": None}
        elif label in open_cycles and record.get("to") in FINISHED_STATES:
            started = open_cycles.pop(label)
            found.append(Cycle(label, started["started"], record["ts"],
                               started["programme"]))

    for label, started in open_cycles.items():
        found.append(Cycle(label, started["started"], None, started["programme"]))

    return sorted(found, key=lambda c: c.started)


def consumables(records: list[dict]) -> list[Consumable]:
    """Believed state of each consumable, honest about coverage."""
    gap = _last_gap(records)
    latest: dict[str, dict] = {}

    for record in sorted(_transitions(records), key=lambda r: r["ts"]):
        if record["key"] in CONSUMABLE_KEYS:
            latest[record["key"]] = record

    found: list[Consumable] = []
    for name in CONSUMABLE_KEYS:
        record = latest.get(name)
        if record is None:
            found.append(Consumable(name, "unknown", None))
            continue
        if gap is not None and record["ts"] < gap:
            # News older than the last gap tells us nothing about now.
            found.append(Consumable(name, "unknown", record["ts"]))
            continue
        state = "low" if record.get("to") == "Present" else "ok"
        found.append(Consumable(name, state, record["ts"]))
    return found


def coverage(records: list[dict]) -> tuple[int, str | None]:
    """How many coverage gaps, and when the most recent one was recorded."""
    stamps = [r["ts"] for r in records if r.get("event") == "coverage_gap"]
    return len(stamps), (max(stamps) if stamps else None)


def render(records: list[dict], skipped: int = 0) -> str:
    """A readable summary of the log."""
    if not records:
        return "No events recorded yet. Is the recorder running?"

    lines: list[str] = []

    found = cycles(records)
    lines.append(f"Cycles: {len(found)}")
    for cycle in found[-10:]:
        ended = cycle.ended or "still running"
        programme = cycle.programme or "unknown programme"
        lines.append(f"  {cycle.started}  {programme}  -> {ended}")

    lines.append("")
    lines.append("Consumables:")
    for consumable in consumables(records):
        since = f" since {consumable.since}" if consumable.since else ""
        lines.append(f"  {consumable.name:22} {consumable.state}{since}")

    gaps, latest = coverage(records)
    lines.append("")
    if gaps:
        lines.append(
            f"Coverage: {gaps} gap(s), most recent {latest}. "
            "Anything marked unknown was last seen before that."
        )
    else:
        lines.append("Coverage: no gaps recorded.")

    if skipped:
        lines.append(f"Skipped {skipped} unreadable line(s) in the log.")

    return "\n".join(lines)
```

- [ ] **Step 4: Add the `history` command to `src/homeconnect/cli.py`**

Add near the other commands, following the file's existing style:

```python
@cli.command(name="history")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def history_command(as_json: bool) -> None:
    """Report on what the recorder has observed."""
    from . import report, store

    records, skipped = store.read_records(store.EVENTS_PATH)

    if as_json:
        click.echo(json.dumps({
            "cycles": [vars(c) for c in report.cycles(records)],
            "consumables": [vars(c) for c in report.consumables(records)],
            "gaps": report.coverage(records)[0],
            "skipped": skipped,
        }, indent=2))
        return

    click.echo(report.render(records, skipped))
```

Add to `tests/test_cli.py`:

```python
def test_history_reports_an_empty_log(monkeypatch, tmp_path):
    from homeconnect import store

    monkeypatch.setattr(store, "EVENTS_PATH", tmp_path / "absent.jsonl")

    result = CliRunner().invoke(cli_module.cli, ["history"])

    assert result.exit_code == 0
    assert "no events" in result.output.lower()


def test_history_json_is_parseable(monkeypatch, tmp_path):
    from homeconnect import store

    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"ts":"2026-08-26T19:00:00Z","ha":"dishwasher",'
        '"key":"OperationState","from":"Ready","to":"Run"}\n'
    )
    monkeypatch.setattr(store, "EVENTS_PATH", path)

    result = CliRunner().invoke(cli_module.cli, ["history", "--json"])

    payload = json.loads(result.output)
    assert payload["cycles"][0]["started"] == "2026-08-26T19:00:00Z"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/homeconnect/report.py tests/test_report.py src/homeconnect/cli.py tests/test_cli.py
git commit -m "feat: add history reporting that is honest about coverage gaps"
```

---

### Task 6: launchd and documentation

**Files:**
- Create: `launchd/com.homeconnect.recorder.plist`
- Create: `launchd/install.sh`, `launchd/uninstall.sh`
- Modify: `.gitignore` (add `data/`)
- Modify: `README.md`, `CLAUDE.md`

**Design note:** the plist must contain no absolute home path. `install.sh`
generates the installed copy from a template at install time, substituting the
repository's own location, so the tracked file stays free of personal paths.

- [ ] **Step 1: Add `data/` to `.gitignore`**

Append `data/` to the existing file. Do not remove any existing entry.

- [ ] **Step 2: Write `launchd/com.homeconnect.recorder.plist`**

Use `__REPO__` as the placeholder that `install.sh` substitutes. No real path
may appear in this tracked file.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.homeconnect.recorder</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-lc</string>
    <string>cd __REPO__ &amp;&amp; exec uv run homeconnect-recorder</string>
  </array>
  <key>WorkingDirectory</key>
  <string>__REPO__</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string>__REPO__/data/recorder.log</string>
  <key>StandardErrorPath</key>
  <string>__REPO__/data/recorder.err</string>
</dict>
</plist>
```

- [ ] **Step 3: Write `launchd/install.sh`**

```sh
#!/bin/sh
# Install the recorder as a per-user LaunchAgent.
#
# The tracked plist carries a __REPO__ placeholder rather than a real path, so
# that no home directory ends up in a repository intended to be public. The
# substitution happens here, at install time.
set -eu

repo="$(cd "$(dirname "$0")/.." && pwd)"
label="com.homeconnect.recorder"
target="$HOME/Library/LaunchAgents/$label.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$repo/data"
sed "s|__REPO__|$repo|g" "$repo/launchd/$label.plist" > "$target"

launchctl unload "$target" 2>/dev/null || true
launchctl load "$target"

echo "Loaded $label."
echo "Logs: $repo/data/recorder.log and recorder.err"
echo "Check it is running:  launchctl list | grep homeconnect"
```

- [ ] **Step 4: Write `launchd/uninstall.sh`**

```sh
#!/bin/sh
# Remove the recorder LaunchAgent. Leaves data/ alone.
set -eu

label="com.homeconnect.recorder"
target="$HOME/Library/LaunchAgents/$label.plist"

launchctl unload "$target" 2>/dev/null || true
rm -f "$target"

echo "Removed $label. The event log in data/ has been left untouched."
```

- [ ] **Step 5: Make them executable and update the documentation**

```bash
chmod +x launchd/install.sh launchd/uninstall.sh
```

Add a README section covering: what the recorder is for; that it records
transitions only; that the log will have gaps because the machine sleeps, and
that `history` says so rather than hiding it; how to install and remove it; and
where the log lives. Add the recorder's module map and the coverage-honesty rule
to CLAUDE.md.

- [ ] **Step 6: Verify the whole suite and commit**

```bash
uv run pytest -q
git add launchd .gitignore README.md CLAUDE.md
git commit -m "feat: add launchd agent for the recorder, and document it"
```
