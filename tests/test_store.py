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
