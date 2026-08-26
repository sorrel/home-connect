# Home Connect event recorder — design

**Date:** 26 August 2026
**Status:** design, not yet implemented
**Builds on:** `2026-08-26-home-connect-status-cli-design.md` (the CLI, complete)

## What this is

A small, always-running listener that records **when things changed** on Home
Connect appliances, as append-only JSONL, plus a command to report on what it
has collected.

It exists because the on-demand CLI cannot answer two questions the API refuses
to serve directly: *does the machine need salt, rinse aid or a clean?* (available
only as change-only SSE events) and *how often does it actually run, on which
programmes, for how long?* (no consumption or history endpoint exists at all).
Both become answerable by observing over time.

## Hard rules

- **Read-only, unchanged.** Same `IdentifyAppliance Monitor` token, same absence
  of write verbs. The recorder observes; it never acts.
- **Append-only.** Lines are appended and never rewritten. Corruption of history
  is a worse failure than a missing line.
- **Unknown is not the same as ok.** Where coverage was lost, the log says so
  explicitly. Reports must never imply a state was observed when it was inferred
  across a gap.
- **No live API calls in tests.** Fixtures and a fake stream throughout.
- **Local-first.** The log lives in a gitignored `data/` directory. Nothing is
  uploaded.

## Decisions taken

- **Host: this Mac, under launchd.** Accepted with open eyes: the machine sleeps,
  so coverage will be incomplete. See "Honesty about gaps" below.
- **Schema: transitions only.** One line per observed change, not per
  observation. A few lines a day rather than hundreds.

## Architecture

Extends the existing package rather than starting a new project — `api.py`,
`auth.py` and `appliances.py` are reused unchanged.

```
src/homeconnect/
  stream.py     SSE client: connect, parse, yield events, reconnect with backoff
  store.py      JSONL append, last-known state, transition diffing
  daemon.py     orchestration: seed, listen, reconcile, mark gaps
  report.py     turn the log into answers (cycles, consumables, uptime)
launchd/
  com.homeconnect.recorder.plist, install.sh, uninstall.sh
data/
  events.jsonl  the log (gitignored)
  state.json    last-known state, for diffing across restarts (gitignored)
```

**Why SSE primary rather than polling.** With "low overhead" as a stated
requirement, one held-open TLS connection idling in a blocking read beats 432
fresh requests a day, and it is the only source of consumable and machine-care
events. Its weakness — seeing nothing while disconnected — is covered by the
reconciliation poll below rather than by polling hard.

**The loop:**

1. **Seed.** On start, poll once (`/homeappliances`, `/status`,
   `/programs/active`). The stream sends nothing on connect — verified live — so
   without this the recorder has no baseline to diff against.
2. **Listen.** Hold the SSE connection. Each event updates last-known state; any
   value that actually changed is appended as a transition.
3. **Reconcile.** Hourly, and after every reconnection, poll again and diff.
   This catches anything missed and re-establishes ground truth. ~72 calls/day.
4. **On disconnection.** Reconnect with exponential backoff, capped. Record when
   coverage was lost and regained.

**Quota:** roughly 100–500 calls/day including reconnections, against a limit of
about 1000, leaving headroom for interactive CLI use.

## The log format

One JSON object per line. Transitions:

```json
{"ts":"2026-08-26T19:04:11Z","ha":"dishwasher","key":"OperationState","from":"Ready","to":"Run"}
```

- `ts` — UTC, ISO 8601, always UTC to survive the clock change.
- `ha` — a stable, human-readable appliance label, not the `haId`. The `haId`
  is a device serial and must never enter the log: this file is local, but it is
  the sort of thing that gets pasted into an issue.
- `key` — the short form (`OperationState`, `SaltNearlyEmpty`), not the full
  vendor path. The full key is recoverable and the short form is what a reader
  wants.
- `from` / `to` — enum tails or `null`.

Coverage markers, written on every gap:

```json
{"ts":"2026-08-27T07:02:10Z","event":"coverage_gap","from":"2026-08-26T23:14:02Z","reason":"stream_lost"}
```

`reason` is one of `stream_lost`, `startup` (the recorder was not running), or
`error`.

## Honesty about gaps

The recorder runs on a laptop that sleeps, so the record will be incomplete.
This is a correctness concern, not a cosmetic one — a log that cannot
distinguish "nothing happened" from "nobody was watching" will silently imply
the salt is fine.

Therefore:

- Every loss of coverage is written to the log as a marker, with the instant
  coverage was last known good.
- `report` must render a consumable's state as **unknown** when the most recent
  relevant information predates a gap, never as "ok".
- Cycle statistics must state the coverage they are drawn from ("14 cycles over
  31 days, 6 h 20 m uncovered") rather than presenting a bare count.

## Reporting

`homeconnect history` reads the log and answers:

- **Cycles** — when each ran, which programme, how long, with coverage caveats.
- **Consumables** — current believed state of salt, rinse aid and machine care,
  each with the date it changed, or `unknown` if it falls the wrong side of a gap.
- **Coverage** — how much of the period was actually observed.

Flags mirror the existing CLI: `--json` for machine output, `-a` to filter,
`--since` to bound the period.

## Failure handling

| Condition | Behaviour |
|---|---|
| Stream drops | Reconnect with exponential backoff (1s doubling to 5 min, jittered). Write a `coverage_gap` marker. |
| Token rejected mid-stream | Refresh once and reconnect. If that fails, exit non-zero so launchd restarts with backoff; do not spin. |
| Access token expiry (24h) | Refresh proactively and re-establish the stream before it lapses. |
| Quota exhausted | Stop polling, keep the stream, log it. Never hammer a 429. |
| Corrupt line in the log | `report` skips it and says how many were skipped. Never rewrite the file. |
| Two recorders started | A lockfile in `data/`; the second exits cleanly rather than double-writing. |

## Out of scope

Notifications; any write to the appliance; multi-machine sync; a web view;
energy or water figures, which do not exist in the API at any frequency.
