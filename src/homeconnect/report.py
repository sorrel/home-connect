"""Turn the event log into answers, without overclaiming.

The log records only what was observed. Where coverage was lost, silence means
nothing was watched, not that nothing happened — so anything whose last news
predates the most recent gap is reported `unknown` rather than `ok`. Getting
this wrong would produce a report that cheerfully implies the salt is fine.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The consumables this report answers for, in the order it lists them. Every
#: one is a `store.EVENT_ONLY_KEYS` member — it has to be, since none can be
#: polled — and `tests/test_report.py` holds that relationship in place.
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
    shown = found[-10:]
    if len(shown) < len(found):
        # Said out loud rather than truncated in silence: a reader comparing
        # the count above with the lines below must not be left to guess.
        lines.append(f"  (showing the {len(shown)} most recent)")
    for cycle in shown:
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
