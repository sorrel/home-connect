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


def test_a_second_run_flushes_the_unseen_end_of_the_first():
    """Missing the end of one cycle before the next starts is expected after
    a coverage gap, not corruption — it must not silently vanish."""
    run_1 = {"ts": "2026-08-26T08:00:00Z", "ha": "dishwasher",
             "key": "OperationState", "from": "Ready", "to": "Run"}
    run_2 = {"ts": "2026-08-26T09:00:00Z", "ha": "dishwasher",
             "key": "OperationState", "from": "Ready", "to": "Run"}
    end_2 = {"ts": "2026-08-26T10:00:00Z", "ha": "dishwasher",
             "key": "OperationState", "from": "Run", "to": "Finished"}

    found = report.cycles([run_1, run_2, end_2])

    assert len(found) == 2
    by_start = {c.started: c for c in found}
    assert by_start["2026-08-26T08:00:00Z"].ended is None
    assert by_start["2026-08-26T09:00:00Z"].ended == "2026-08-26T10:00:00Z"


def test_an_end_with_no_preceding_start_is_not_a_phantom_cycle():
    found = report.cycles([FINISHED])
    assert found == []


def test_interleaved_cycles_from_two_appliances_are_kept_apart():
    washer_run = {"ts": "2026-08-26T08:00:00Z", "ha": "washer",
                  "key": "OperationState", "from": "Ready", "to": "Run"}
    dryer_run = {"ts": "2026-08-26T09:00:00Z", "ha": "dryer",
                 "key": "OperationState", "from": "Ready", "to": "Run"}
    washer_end = {"ts": "2026-08-26T10:00:00Z", "ha": "washer",
                  "key": "OperationState", "from": "Run", "to": "Finished"}
    dryer_end = {"ts": "2026-08-26T11:00:00Z", "ha": "dryer",
                 "key": "OperationState", "from": "Run", "to": "Finished"}

    found = {c.label: c for c in
             report.cycles([washer_run, dryer_run, washer_end, dryer_end])}

    assert found["washer"].started == "2026-08-26T08:00:00Z"
    assert found["washer"].ended == "2026-08-26T10:00:00Z"
    assert found["dryer"].started == "2026-08-26T09:00:00Z"
    assert found["dryer"].ended == "2026-08-26T11:00:00Z"


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
    """Isolate the demotion itself: SaltNearlyEmpty is explicitly reported
    `ok` (not `Present`) before the gap, so if the gap-demotion logic were
    deleted this fixture would render it `ok`. Naming the line, not just
    searching for the word "unknown" anywhere, is what proves the logic
    actually ran rather than every never-reported consumable defaulting to
    `unknown` regardless."""
    salt_ok = dict(SALT_LOW, to="Absent")
    text = report.render([salt_ok, GAP], skipped=0)

    salt_line = next(line for line in text.splitlines()
                      if line.strip().startswith("SaltNearlyEmpty"))
    assert salt_line.split()[1] == "unknown"


def test_render_trusts_a_consumable_reported_after_the_last_gap():
    """Mirror case: the same consumable, reported after the most recent gap,
    must render its real state rather than being blanketed as unknown."""
    salt_ok_after_gap = dict(SALT_LOW, to="Absent", ts="2026-08-27T09:00:00Z")
    text = report.render([GAP, salt_ok_after_gap], skipped=0)

    salt_line = next(line for line in text.splitlines()
                      if line.strip().startswith("SaltNearlyEmpty"))
    assert salt_line.split()[1] == "ok"
