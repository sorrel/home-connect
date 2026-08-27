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


def test_coverage_counts_gaps():
    count, latest = report.coverage([RUN, GAP, FINISHED])
    assert count == 1
    assert latest == "2026-08-27T07:00:00Z"


def test_render_mentions_skipped_lines_when_there_are_any():
    text = report.render([RUN], skipped=3)
    assert "3" in text


def test_render_says_so_when_the_log_is_empty():
    assert "no" in report.render([], skipped=0).lower()


def test_a_programme_named_in_the_same_batch_as_the_start_is_attached():
    """The stream may name the programme either side of the state change.

    A poll always writes OperationState before Programme; the stream sends
    whatever order the vendor chose, and the pair then share a timestamp.
    """
    found = report.cycles([PROG, RUN, FINISHED])

    assert found[0].programme == "Eco50"


def test_a_programme_named_at_an_earlier_instant_is_not_attributed():
    """Attributing an older programme to a later run would be a guess."""
    earlier = dict(PROG, ts="2026-08-26T10:00:00Z")

    found = report.cycles([earlier, RUN, FINISHED])

    assert found[0].programme is None


def test_every_consumable_key_is_one_the_recorder_carries_across_a_poll():
    from homeconnect import store

    assert set(report.CONSUMABLE_KEYS) <= set(store.EVENT_ONLY_KEYS)

# --- Alerts as arrivals, never as a current state ---------------------------
#
# The API sends no all-clear. A consumable alert arrives and is never
# withdrawn, so the only honest record is *when it fired*, not what is true
# now. These tests hold that line: nothing here may infer a state.

NOW = "2026-08-26T12:00:00Z"


def plain(text):
    """Strip the ANSI colour so assertions read the words, not the escapes."""
    import click
    return click.unstyle(text)


def salt_at(stamp):
    return dict(SALT_LOW, ts=stamp)


def test_an_arrival_is_recorded_with_its_instant():
    found = {a.name: a for a in report.alerts([SALT_LOW])}
    assert found["SaltNearlyEmpty"].occurrences == ("2026-08-26T08:00:00Z",)


def test_every_arrival_is_kept_not_merely_the_latest():
    """Data once recorded is never discarded — the intervals are the point."""
    records = [salt_at("2025-09-02T08:10:00Z"),
               salt_at("2026-01-14T07:55:00Z"),
               salt_at("2026-04-30T19:02:00Z")]

    found = {a.name: a for a in report.alerts(records)}

    assert found["SaltNearlyEmpty"].occurrences == (
        "2025-09-02T08:10:00Z", "2026-01-14T07:55:00Z", "2026-04-30T19:02:00Z")


def test_an_alert_never_seen_has_no_occurrences():
    found = {a.name: a for a in report.alerts([SALT_LOW])}
    assert found["RinseAidNearlyEmpty"].occurrences == ()


def test_a_transition_away_from_present_is_not_an_arrival():
    """Only a transition *to* Present is news. Were the appliance ever to
    send something else, it must not be counted as the alert firing."""
    found = {a.name: a for a in report.alerts([dict(SALT_LOW, to="Absent")])}
    assert found["SaltNearlyEmpty"].occurrences == ()


def test_intervals_are_the_days_between_consecutive_arrivals():
    days = report.intervals_days(("2026-01-01T00:00:00Z",
                                  "2026-01-11T00:00:00Z",
                                  "2026-02-10T00:00:00Z"))
    assert days == [10.0, 30.0]


def test_one_arrival_yields_no_interval():
    assert report.intervals_days(("2026-01-01T00:00:00Z",)) == []


def test_the_expanded_view_shows_every_arrival_and_the_arithmetic():
    records = [salt_at("2026-01-01T00:00:00Z"),
               salt_at("2026-01-11T00:00:00Z"),
               salt_at("2026-02-10T00:00:00Z")]

    text = plain(report.render(records, expanded=True, now=NOW))

    for stamp in ("2026-01-01T00:00:00Z", "2026-01-11T00:00:00Z",
                  "2026-02-10T00:00:00Z"):
        assert stamp in text
    assert "3 arrivals" in text
    assert "+10 days" in text and "+30 days" in text
    assert "20 days" in text          # the mean of 10 and 30
    assert "shortest 10" in text and "longest 30" in text


def test_the_default_view_does_not_list_the_whole_record():
    """Two weeks is the default horizon; the rest is behind -x."""
    records = [salt_at("2026-01-01T00:00:00Z"), salt_at("2026-08-20T00:00:00Z")]

    text = plain(report.render(records, now=NOW))

    assert "2026-01-01T00:00:00Z" not in text
    assert "-x" in text


def test_the_default_view_still_says_when_an_old_alert_last_fired():
    """An alert that has not fired in months is current status, not history:
    saying nothing about it would read as if it had never fired at all."""
    text = plain(report.render([salt_at("2026-01-01T00:00:00Z")], now=NOW))

    assert "SaltNearlyEmpty" in text
    assert "237 days ago" in text


def test_an_alert_never_seen_says_so_rather_than_being_omitted():
    text = plain(report.render([RUN, FINISHED], now=NOW))
    assert "never" in text.lower()
    assert "RinseAidNearlyEmpty" in text


def test_counts_are_declared_a_floor_when_coverage_was_lost():
    """An alert that fired whilst nothing was listening was never seen, so a
    count is a lower bound. Saying `x2` without that caveat overclaims."""
    text = plain(report.render([SALT_LOW, GAP], expanded=True, now=NOW))
    assert "floor" in text or "at least" in text


def test_no_caveat_about_counts_when_coverage_was_unbroken():
    text = plain(report.render([SALT_LOW], expanded=True, now=NOW))
    assert "at least" not in text


def test_the_boxes_stay_square_whatever_the_content():
    """Padding is computed on display width, never len()."""
    records = [salt_at("2026-01-01T00:00:00Z"), salt_at("2026-01-11T00:00:00Z")]

    box = [line for line in plain(report.render(records, expanded=True, now=NOW))
           .splitlines() if line.startswith(("┌", "│", "├", "└"))]

    assert len({report_width(line) for line in box}) == 1, box


def report_width(line):
    from homeconnect.present import display_width
    return display_width(line)


def test_a_lone_gap_is_described_in_the_singular():
    """`1 gap(s) mean an arrival may have gone unseen` is not English."""
    text = plain(report.render([SALT_LOW, GAP], expanded=True, now=NOW))
    assert "1 coverage gap means" in text and "gap(s)" not in text


# --- Cycles, given the same arithmetic ------------------------------------

def run_at(started, ended=None, programme=None):
    records = [{"ts": started, "ha": "dishwasher", "key": "OperationState",
                "from": "Ready", "to": "Run"}]
    if programme:
        records.append({"ts": started, "ha": "dishwasher", "key": "Programme",
                        "from": None, "to": programme})
    if ended:
        records.append({"ts": ended, "ha": "dishwasher", "key": "OperationState",
                        "from": "Run", "to": "Finished"})
    return records


def test_cycle_intervals_are_the_days_between_consecutive_starts():
    found = report.cycles(run_at("2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z")
                          + run_at("2026-01-04T00:00:00Z", "2026-01-04T02:00:00Z"))
    assert report.cycle_intervals_days(found) == [3.0]


def test_a_run_never_seen_to_finish_contributes_no_duration():
    """Its end is unknown, not zero. Averaging in a nought would understate
    every duration around it."""
    found = report.cycles(run_at("2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z")
                          + run_at("2026-01-04T00:00:00Z"))
    assert report.cycle_durations_seconds(found) == [7200.0]


def test_the_expanded_view_boxes_the_cycles_with_their_arithmetic():
    records = (run_at("2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z", "Eco50")
               + run_at("2026-01-04T00:00:00Z", "2026-01-04T03:00:00Z", "Eco50")
               + run_at("2026-01-09T00:00:00Z", "2026-01-09T02:00:00Z", "Auto65"))

    text = plain(report.render(records, expanded=True, now=NOW))

    assert "3 runs" in text
    assert "+3 days" in text and "+5 days" in text
    assert "4 days" in text            # the mean of 3 and 5
    assert "Eco50 (2 of 3)" in text    # the programme used most
    assert "2h 20m" in text            # the mean of 2h, 3h and 2h


def test_the_cycle_box_and_the_alert_boxes_share_one_width():
    """Every panel is one column down the page, not a ragged stack."""
    records = (run_at("2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z", "Eco50")
               + [salt_at("2026-01-02T00:00:00Z")])

    box = [line for line in plain(report.render(records, expanded=True, now=NOW))
           .splitlines() if line.startswith(("┌", "│", "├", "└"))]

    assert len({report_width(line) for line in box}) == 1, box


def test_a_log_with_no_cycles_says_so_inside_the_box():
    text = plain(report.render([SALT_LOW], expanded=True, now=NOW))
    assert "no cycles recorded" in text
