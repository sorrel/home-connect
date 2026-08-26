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
