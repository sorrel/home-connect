"""Append-only JSONL log, last-known state, and transition diffing.

The log is the only record that a transition ever happened: the API exposes no
history, and its event stream reports changes only. Lines are therefore appended
and never rewritten, and a line that will not parse is skipped and counted rather
than repaired — losing one observation is far better than corrupting the rest.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

#: Keys that only ever arrive on the event stream — a poll can never return
#: them, because the API answers `SDK.Error.UnsupportedStatus` for each. They
#: matter here because a reconciliation poll rebuilds state from what it can
#: read, and anything in this set would otherwise appear to have vanished:
#: `SaltNearlyEmpty: "Present" -> null`, which a report reads as "ok". The
#: recorder therefore carries these forward across a poll instead.
#:
#: `SelectedProgramme` is here for the same reason: the recorder polls
#: `/programs/active`, never `/programs/selected`, so a selected programme
#: learnt from the stream is unpollable too.
EVENT_ONLY_KEYS = (
    "SaltNearlyEmpty",
    "RinseAidNearlyEmpty",
    "MachineCareReminder",
    "IDos1FillLevelPoor",
    "IDos2FillLevelPoor",
    "SelectedProgramme",
)


def default_data_dir() -> Path:
    """Where the log and state live, independent of the current directory.

    Anchored to the repository root derived from this module's own location,
    not to `Path("data")`: `homeconnect history` run from anywhere but the
    repository would otherwise find an empty directory and report "No events
    recorded yet", which is indistinguishable from a dead recorder.

    `HOMECONNECT_DATA_DIR` overrides it, which also covers the case of the
    package being installed somewhere other than a checkout.
    """
    override = os.environ.get("HOMECONNECT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    # …/<repository>/src/homeconnect/store.py -> …/<repository>
    return Path(__file__).resolve().parents[2] / "data"


DATA_DIR = default_data_dir()
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


_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_stamp(stamp: Any) -> datetime | None:
    """Turn a stamp written by `utc_now` back into a datetime, or `None`.

    `None` for anything that will not parse, so a caller decides what an
    unreadable stamp means rather than having a `ValueError` decide for it.
    """
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.strptime(stamp, _STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def elapsed_seconds(since: Any, until: Any) -> float | None:
    """Wall-clock seconds between two stamps, or `None` if either is unreadable.

    Deliberately the wall clock and not `time.monotonic()`. On macOS the
    monotonic clock is `CLOCK_UPTIME_RAW`, which does not advance while the
    machine sleeps — so an overnight sleep, the exact interruption this
    recorder exists to notice, can measure as a couple of seconds and be
    dismissed as too short to record.
    """
    start, end = parse_stamp(since), parse_stamp(until)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


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
            # flock, not lockf/POSIX record locks: flock locks are per-open-file-
            # description, so two acquisitions within one process genuinely
            # conflict; POSIX record locks would silently succeed here and let
            # two recorders interleave writes.
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning(f"another recorder holds {path}") from exc
        yield
    finally:
        handle.close()
