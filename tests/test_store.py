import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from homeconnect import store


def test_short_key_strips_the_vendor_path():
    assert store.short_key("BSH.Common.Status.OperationState") == "OperationState"
    assert store.short_key("Dishcare.Dishwasher.Event.SaltNearlyEmpty") == "SaltNearlyEmpty"
    assert store.short_key("Programme") == "Programme"


def test_utc_now_is_iso_utc_with_a_trailing_z():
    before = datetime.now(timezone.utc)
    stamp = store.utc_now()

    assert stamp.endswith("Z")
    assert "T" in stamp
    assert "+" not in stamp

    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert abs(parsed - before) < timedelta(seconds=5)


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
    """haId is a device serial and must not reach the log.

    The protection is structural: neither record type has a field for it, so
    assert the field sets exactly, not just the absence of a substring — that
    genuinely fails if anyone later widens a record with an extra field, which
    is how a serial would actually get in.
    """
    transition = store.transition_record(
        "2026-08-26T19:04:11Z", "dishwasher",
        "BSH.Common.Status.DoorState", None, "Open",
    )
    assert set(transition) == {"ts", "ha", "key", "from", "to"}
    assert "haId" not in json.dumps(transition)

    gap = store.gap_record("2026-08-26T19:04:11Z", "2026-08-26T18:00:00Z", "sleep")
    assert set(gap) == {"ts", "event", "from", "reason"}
    assert "haId" not in json.dumps(gap)


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


# --- Where the data lives --------------------------------------------------

def test_default_data_dir_is_anchored_to_the_repository_not_the_cwd(monkeypatch):
    """`homeconnect history` run from elsewhere must not report an empty log.

    Resolved from `data`, a report run from any other directory finds nothing
    and says "No events recorded yet. Is the recorder running?" — which is
    indistinguishable from a recorder that has genuinely died.
    """
    monkeypatch.delenv("HOMECONNECT_DATA_DIR", raising=False)

    found = store.default_data_dir()

    assert found.is_absolute()
    assert found.name == "data"
    assert found.parent == Path(store.__file__).resolve().parents[2]


def test_data_dir_can_be_overridden_by_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMECONNECT_DATA_DIR", str(tmp_path / "elsewhere"))

    assert store.default_data_dir() == tmp_path / "elsewhere"


def test_data_dir_override_expands_a_home_relative_path(monkeypatch):
    monkeypatch.setenv("HOMECONNECT_DATA_DIR", "~/home-connect-data")

    found = store.default_data_dir()

    assert found.is_absolute()
    assert "~" not in str(found)


# --- Wall-clock spans ------------------------------------------------------

def test_elapsed_seconds_measures_a_span_between_two_stamps():
    assert store.elapsed_seconds(
        "2026-08-26T19:00:00Z", "2026-08-27T03:00:00Z"
    ) == 8 * 3600


def test_elapsed_seconds_is_none_for_an_unreadable_stamp():
    assert store.elapsed_seconds("not a stamp", "2026-08-26T19:00:00Z") is None
    assert store.elapsed_seconds("2026-08-26T19:00:00Z", None) is None


def test_parse_stamp_round_trips_utc_now():
    parsed = store.parse_stamp(store.utc_now())

    assert parsed is not None
    assert parsed.tzinfo is not None, "a naive stamp would compare wrongly"
