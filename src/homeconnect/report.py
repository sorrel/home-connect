"""Turn the event log into answers, without overclaiming.

The log records only what was observed, and this module never states more than
that. Two rules follow, and neither may be relaxed for a tidier-looking report:

Alerts are reported as arrivals, never as a current state. The API sends no
all-clear — salt says it is low and nothing is ever sent to say it was
refilled — so any `ok` would be inferred from a message that never comes.

Where coverage was lost, silence means nothing was watched, not that nothing
happened. An alert that fired during a gap was never seen, so every count is a
floor and says so.
"""

from __future__ import annotations

from dataclasses import dataclass

import click

from . import store
from .present import display_width

#: The consumables this report answers for, in the order it lists them. Every
#: one is a `store.EVENT_ONLY_KEYS` member — it has to be, since none can be
#: polled — and `tests/test_report.py` holds that relationship in place.
CONSUMABLE_KEYS = ("SaltNearlyEmpty", "RinseAidNearlyEmpty", "MachineCareReminder")

#: The only transition that counts as an alert arriving.
ARRIVED = "Present"

#: How far back the default view looks. The full record is behind `-x`.
RECENT_DAYS = 14

_DAY = 86400.0

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
class Alert:
    """Every instant at which one alert was observed to arrive.

    Deliberately not a state. The API sends no all-clear: salt runs low and
    says so, and nothing is ever sent to say it was refilled. Any `ok` derived
    here would be inferred from a message that never comes. What can be stated
    truthfully is when the alert fired, and how long elapsed between firings —
    so that is all this holds.
    """

    name: str
    occurrences: tuple[str, ...]

    @property
    def last(self) -> str | None:
        return self.occurrences[-1] if self.occurrences else None


def _transitions(records: list[dict]) -> list[dict]:
    return [r for r in records if "key" in r and "event" not in r]


def _last_gap(records: list[dict]) -> str | None:
    stamps = [r["ts"] for r in records if r.get("event") == "coverage_gap"]
    return max(stamps) if stamps else None


def cycles(records: list[dict]) -> list[Cycle]:
    """Pair each transition into `Run` with the next one out of it."""
    found: list[Cycle] = []
    open_cycles: dict[str, dict] = {}
    # A programme named before its run was seen to start, held only for the
    # instant it was named. A poll always writes OperationState before
    # Programme, but the stream sends whatever order the vendor chose, and the
    # pair then share a timestamp. Held no longer than that: attributing an
    # older programme to a later run would be a guess dressed as an
    # observation, and this report does not guess.
    pending: dict[str, tuple[str, str | None]] = {}

    for record in sorted(_transitions(records), key=lambda r: r["ts"]):
        label = record.get("ha", "appliance")
        if record["key"] == "Programme":
            if label in open_cycles:
                open_cycles[label]["programme"] = record.get("to")
            else:
                pending[label] = (record["ts"], record.get("to"))
            continue
        if record["key"] != "OperationState":
            continue

        if record.get("to") == RUNNING:
            if label in open_cycles:
                # A new run started before the previous one was seen to end —
                # expected after a coverage gap, not corruption. Flush the
                # stale one as unfinished rather than silently dropping it.
                stale = open_cycles.pop(label)
                found.append(Cycle(label, stale["started"], None, stale["programme"]))
            named_at, named = pending.pop(label, (None, None))
            open_cycles[label] = {
                "started": record["ts"],
                "programme": named if named_at == record["ts"] else None,
            }
        elif label in open_cycles and record.get("to") in FINISHED_STATES:
            started = open_cycles.pop(label)
            found.append(Cycle(label, started["started"], record["ts"],
                               started["programme"]))

    for label, started in open_cycles.items():
        found.append(Cycle(label, started["started"], None, started["programme"]))

    return sorted(found, key=lambda c: c.started)


def alerts(records: list[dict]) -> list[Alert]:
    """Every arrival of every alert, oldest first, nothing discarded."""
    seen: dict[str, list[str]] = {name: [] for name in CONSUMABLE_KEYS}

    for record in sorted(_transitions(records), key=lambda r: r["ts"]):
        if record["key"] in seen and record.get("to") == ARRIVED:
            seen[record["key"]].append(record["ts"])

    return [Alert(name, tuple(stamps)) for name, stamps in seen.items()]


def intervals_days(occurrences: tuple[str, ...] | list[str]) -> list[float]:
    """Days between each consecutive pair of arrivals.

    An unreadable stamp drops its interval rather than the whole series: a
    single corrupt line should cost one gap in the arithmetic, not all of it.
    """
    found: list[float] = []
    for earlier, later in zip(occurrences, occurrences[1:]):
        seconds = store.elapsed_seconds(earlier, later)
        if seconds is not None:
            found.append(seconds / _DAY)
    return found


def cycle_intervals_days(found: list[Cycle]) -> list[float]:
    """Days between the start of each run and the start of the next."""
    starts = [c.started for c in sorted(found, key=lambda c: c.started)]
    return intervals_days(starts)


def cycle_durations_seconds(found: list[Cycle]) -> list[float]:
    """How long each run took, skipping any never seen to finish.

    An unfinished run's end is unknown, not zero: averaging in a nought would
    quietly understate every duration reported beside it.
    """
    durations = []
    for cycle in found:
        if cycle.ended is None:
            continue
        seconds = store.elapsed_seconds(cycle.started, cycle.ended)
        if seconds is not None:
            durations.append(seconds)
    return durations


def commonest_programme(found: list[Cycle]) -> tuple[str, int] | None:
    """The programme run most often, and how many times, or `None`.

    Runs whose programme was never named are not counted for or against: an
    unnamed run is missing information, not a vote for anything.
    """
    named = [c.programme for c in found if c.programme]
    if not named:
        return None
    winner = max(sorted(set(named)), key=named.count)
    return winner, named.count(winner)


def days_since(stamp: str, now: str) -> float | None:
    seconds = store.elapsed_seconds(stamp, now)
    return None if seconds is None else seconds / _DAY


def coverage(records: list[dict]) -> tuple[int, str | None]:
    """How many coverage gaps, and when the most recent one was recorded."""
    stamps = [r["ts"] for r in records if r.get("event") == "coverage_gap"]
    return len(stamps), (max(stamps) if stamps else None)


# --- presentation ----------------------------------------------------------
#
# Colours assume a dark terminal. Padding is always computed on the plain text
# and the style applied afterwards: an ANSI escape has no width, so styling
# first and measuring after misaligns every box on the screen.

_BORDER = dict(fg="bright_black")
_TITLE = dict(fg="bright_cyan", bold=True)
_STAMP = dict(fg="cyan")
_SPAN = dict(fg="green")
_QUIET = dict(fg="bright_black")
_CAVEAT = dict(fg="yellow")

_MIN_BOX = 58

#: The badge for an alert with no recorded arrival — quiet, not a warning.
_NEVER = "never fired"

#: The same, for a log that has yet to see the appliance run.
_NEVER_RUN = "none recorded"


def _measure(text: str) -> int:
    """Display width of `text` once its colour is stripped.

    An ANSI escape occupies no columns but plenty of characters, so measuring
    styled text inflates every width and skews the box it is padding.
    """
    return display_width(click.unstyle(text))


def _number(value: float) -> str:
    """A day count a person would say out loud.

    Whole days once past a couple of them: an alert that fires every few
    months is not made clearer by half a day of precision. Below that the
    decimal is the whole story, so it stays.
    """
    return f"{value:.1f}" if value < 2 else f"{value:.0f}"


def _plural(count: int, noun: str, verb: tuple[str, str] | None = None) -> str:
    phrase = f"{count} {noun}" if count == 1 else f"{count} {noun}s"
    if verb is None:
        return phrase
    singular, plural = verb
    return f"{phrase} {singular if count == 1 else plural}"


def _duration(seconds: float) -> str:
    """`2h 20m`, the way a person describes a wash."""
    minutes = int(round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _ago(stamp: str, now: str) -> str:
    days = days_since(stamp, now)
    if days is None:
        return "at an unreadable time"
    if days < 1:
        hours = days * 24
        return "less than an hour ago" if hours < 1 else f"{hours:.0f} hours ago"
    return f"{int(days)} days ago"


def _heading(text: str) -> str:
    return click.style(text, fg="bright_white", bold=True)


def _box_width(panels: list[tuple[str, str, list[str], list[str]]]) -> int:
    """One width for every panel, so the boxes line up as a column."""
    needed = [_MIN_BOX]
    for title, badge, rows, footer in panels:
        needed.append(_measure(title) + _measure(badge) + 9)
        needed.extend(_measure(line) + 4 for line in rows + footer)
    return max(needed)


def _box(title: str, badge: str, rows: list[str], footer: list[str],
         width: int, *, quiet_badge: bool = False) -> list[str]:
    """One bordered panel, every line the same display width."""
    fill = width - _measure(title) - _measure(badge) - 6
    out = [
        click.style("┌ ", **_BORDER) + click.style(title, **_TITLE)
        + click.style(" " + "─" * fill + " ", **_BORDER)
        + click.style(badge, **(_QUIET if quiet_badge else _CAVEAT))
        + click.style(" ┐", **_BORDER)
    ]

    def row(text: str) -> str:
        pad = " " * (width - _measure(text) - 4)
        return (click.style("│ ", **_BORDER) + text + pad
                + click.style(" │", **_BORDER))

    out.extend(row(r) for r in rows)
    if footer:
        out.append(click.style("├" + "─" * (width - 2) + "┤", **_BORDER))
        out.extend(row(f) for f in footer)
    out.append(click.style("└" + "─" * (width - 2) + "┘", **_BORDER))
    return out


def _alert_panel(alert: Alert, now: str
                 ) -> tuple[str, str, list[str], list[str]]:
    if not alert.occurrences:
        # The message goes in the body, not a footer: a separator drawn
        # directly under the header, with nothing above it, reads as a fault.
        return (alert.name, _NEVER,
                [click.style("no arrivals recorded", **_QUIET)], [])

    spans = intervals_days(alert.occurrences)
    rows = [click.style(alert.occurrences[0], **_STAMP)]
    for stamp, span in zip(alert.occurrences[1:], spans):
        rows.append(click.style(stamp, **_STAMP) + "    "
                    + click.style(f"+{_number(span)} days", **_SPAN))

    footer = []
    if spans:
        mean = sum(spans) / len(spans)
        footer.append(
            f"every {click.style(_number(mean) + ' days', **_SPAN)} on average"
            + click.style("  ·  ", **_QUIET)
            + f"shortest {_number(min(spans))}, longest {_number(max(spans))}"
        )
    footer.append(click.style(f"last arrival {_ago(alert.last, now)}", **_QUIET))

    count = len(alert.occurrences)
    return (alert.name, f"{count} arrival{'s' if count != 1 else ''}",
            rows, footer)


def _cycle_panel(found: list[Cycle], expanded: bool
                 ) -> tuple[str, str, list[str], list[str]]:
    """Every run, with the interval since the previous one and the totals."""
    if not found:
        return ("Cycles", _NEVER_RUN,
                [click.style("no cycles recorded", **_QUIET)], [])

    ordered = sorted(found, key=lambda c: c.started)
    programmes = [c.programme or "unknown programme" for c in ordered]
    width = max(_measure(p) for p in programmes)

    rows = []
    previous = None
    for cycle, programme in zip(ordered, programmes):
        took = (_duration(store.elapsed_seconds(cycle.started, cycle.ended) or 0)
                if cycle.ended else "still running")
        row = (click.style(cycle.started, **_STAMP) + "  "
               + click.style(programme, fg="bright_yellow")
               + " " * (width - _measure(programme)) + "  "
               + click.style(f"{took:>13}", **_QUIET))
        if previous is not None:
            span = days_since(previous, cycle.started)
            if span is not None:
                row += click.style(f"   +{_number(span)} days", **_SPAN)
        previous = cycle.started
        rows.append(row)

    footer = []
    spans = cycle_intervals_days(ordered)
    if spans:
        mean = sum(spans) / len(spans)
        footer.append(
            f"every {click.style(_number(mean) + ' days', **_SPAN)} on average"
            + click.style("  ·  ", **_QUIET)
            + f"shortest {_number(min(spans))}, longest {_number(max(spans))}"
        )
    durations = cycle_durations_seconds(ordered)
    summary = []
    if durations:
        summary.append("typical run "
                       + click.style(_duration(sum(durations) / len(durations)),
                                     **_SPAN))
    commonest = commonest_programme(ordered)
    if commonest:
        name, count = commonest
        summary.append(f"most used {click.style(name, fg='bright_yellow')} "
                       f"({count} of {len(ordered)})")
    if summary:
        footer.append(click.style("  ·  ", **_QUIET).join(summary))

    return ("Cycles", _plural(len(ordered), "run"), rows, footer)


def _cycle_line(cycle: Cycle) -> str:
    ended = cycle.ended or "still running"
    programme = cycle.programme or "unknown programme"
    return ("  " + click.style(cycle.started, **_STAMP) + "  "
            + click.style(programme, fg="bright_yellow") + "  "
            + click.style(f"-> {ended}", **_QUIET))


def _recent(stamps: list[str], now: str) -> list[str]:
    return [s for s in stamps
            if (days_since(s, now) or 0) <= RECENT_DAYS]


def render(records: list[dict], skipped: int = 0, *,
           expanded: bool = False, now: str | None = None) -> str:
    """A readable summary of the log.

    The default view answers "what is the state of things?" over the last
    fortnight. `expanded` answers "how often does this happen?" over the whole
    record — which is why the log is never pruned.
    """
    if not records:
        return "No events recorded yet. Is the recorder running?"

    now = now or store.utc_now()
    gaps, latest_gap = coverage(records)
    lines: list[str] = []

    found = cycles(records)
    every = alerts(records)

    if expanded:
        lines.append(_heading("The complete record"))
        lines.append(click.style(
            "No all-clear is ever sent, so alerts are arrivals, not current "
            "state.", **_QUIET))

        # One width across every panel, so they read as a single column
        # rather than a ragged stack.
        panels = [_cycle_panel(found, expanded)] + [
            _alert_panel(alert, now) for alert in every]
        width = _box_width(panels)
        for panel in panels:
            lines.append("")
            lines.extend(_box(*panel, width=width,
                              quiet_badge=panel[1] in (_NEVER, _NEVER_RUN)))

        if gaps:
            lines.append("")
            lines.append(click.style(
                "Counts are at least this many: "
                f"{_plural(gaps, 'coverage gap', ('means', 'mean'))} "
                "a run or an arrival may have gone unseen.", **_CAVEAT))
    else:
        lines.append(_heading(f"Cycles: {len(found)}"))
        shown = [c for c in found
                 if (days_since(c.started, now) or 0) <= RECENT_DAYS]
        if len(shown) < len(found):
            lines.append(click.style(
                f"  (the {len(shown)} in the last {RECENT_DAYS} days; "
                f"-x shows all {len(found)})", **_QUIET))
        if not shown:
            lines.append(click.style(
                f"  none in the last {RECENT_DAYS} days", **_QUIET))
        lines.extend(_cycle_line(cycle) for cycle in shown)

        lines.append("")
        lines.append(_heading(f"Alerts — the last {RECENT_DAYS} days"))
        for alert in every:
            label = f"  {alert.name:22}"
            if alert.last is None:
                lines.append(label + click.style("never fired", **_QUIET))
                continue
            recent = _recent(list(alert.occurrences), now)
            body = (click.style(alert.last, **_STAMP) + " "
                    + click.style(f"({_ago(alert.last, now)})", **_QUIET))
            if recent:
                lines.append(label + click.style("fired ", **_CAVEAT) + body
                             + click.style(f"  x{len(recent)} in window", **_CAVEAT))
            else:
                lines.append(label + click.style("last fired ", **_QUIET) + body)
        lines.append("")
        lines.append(click.style(
            "  Run `homeconnect history -x` for every arrival and the intervals.",
            **_QUIET))

    # --- coverage ---
    lines.append("")
    if gaps:
        lines.append(click.style(
            f"Coverage: {_plural(gaps, 'gap')}, most recent {latest_gap}. "
            "Nothing was watched then.", **_CAVEAT))
    else:
        lines.append(click.style("Coverage: no gaps recorded.", **_QUIET))

    if skipped:
        lines.append(click.style(
            f"Skipped {_plural(skipped, 'unreadable line')} in the log.",
            **_CAVEAT))

    return "\n".join(lines)
