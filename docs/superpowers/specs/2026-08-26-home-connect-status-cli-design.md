# Home Connect status CLI — design

**Date:** 26 August 2026
**Status:** approved design, not yet implemented

## What this is

A small, local, **read-only** CLI that answers "what is the dishwasher doing,
and does it need anything?" on demand. One command, one glance, no daemon.

Built to generalise: the household will have more than one Home Connect
appliance in time, so the plumbing is appliance-agnostic from the start and
only the presentation layer knows what a dishwasher is.

## Hard rules

- **Read-only, enforced twice.** The OAuth token is requested with
  `IdentifyAppliance Monitor` and nothing else. Both are read-only: `Monitor`
  reads status and programmes, `IdentifyAppliance` permits listing appliances
  (without it, `GET /homeappliances` returns 403 — confirmed live) — a write request fails at the server regardless of what the
  code does. On top of that, no POST/PUT/DELETE call exists anywhere in the
  codebase. Adding one is a separate, explicitly approved piece of work.
- **No `Settings` scope.** There is no read-only settings scope; `Settings`
  grants read *and modify*. Reading `PowerState`/`ChildLock` is not worth a
  token that can change them. Settings are out of scope for v1.
- **No live calls in the test suite.** Tests run against recorded JSON
  fixtures. Never verify behaviour by running against the real appliance.
- **Local-first.** Credentials never written to disk as plaintext. No
  telemetry, no cloud storage, nothing leaves the machine except the API calls
  themselves.

## What the API actually gives us

Confirmed against the official OpenAPI spec (`hcsdk-production.yaml`) and the
key documentation.

**Available:**

| Concern | Source |
|---|---|
| Running / idle / finished / error | `BSH.Common.Status.OperationState` |
| Door open, closed, locked | `BSH.Common.Status.DoorState` |
| Time left, progress | `BSH.Common.Option.RemainingProgramTime`, `ProgramProgress` |
| Which programme, which options | `/programs/active` |
| Salt low | `Dishcare.Dishwasher.Event.SaltNearlyEmpty` |
| Rinse aid low | `Dishcare.Dishwasher.Event.RinseAidNearlyEmpty` |
| Needs a cleaning cycle | `Dishcare.Dishwasher.Event.MachineCareReminder` |
| Appliance online at all | `connected` on the appliance record |

**Not available, and not obtainable:** energy used, water used, cycle counts,
run history. The Home Connect *app* shows all of these under Easy Access, but
no documented endpoint exposes them. The only route to such stats would be to
observe cycles over time and accumulate them locally — which needs a
long-running listener and is deliberately out of scope here.

## Open question to settle before building the presentation layer

The three consumable/care values above are defined as **events**, delivered
over the SSE stream. It is not established whether they are *also* readable
from a plain `GET /status` snapshot. The OpenAPI spec does not settle this: it
types `/status` as an untyped `ArrayOfStatus` and does not enumerate keys at
all.

This matters because those values are the main thing the tool is for. If they
are stream-only, an on-demand command cannot see them, and the design must
change rather than quietly grow a daemon.

**Resolution:** build `auth.py` and `api.py` (needed either way), then perform
one `GET /status` against the real appliance and inspect the keys returned.
If the event keys are absent, stop and re-decide the design.

## Architecture

`scripts/home-connect/`, src layout, uv, Python >= 3.12, Click, pytest —
matching the sibling projects.

```
src/homeconnect/
  auth.py         OAuth2 device flow; refresh token in macOS Keychain
  api.py          GET-only HTTP client; error mapping
  appliances.py   enumerate and fetch per-appliance state
  present.py      renderers keyed by appliance type
  cli.py          Click entry point
```

**`auth.py`** — one-off consent via the device authorisation flow
(`x-homeconnect-deviceAuthorizationUrl`): the user is shown a code and a URL,
approves in a browser once, and the resulting refresh token is stored in the
macOS Keychain via `keyring`. Refresh tokens rotate on use; each rotation
overwrites the Keychain item. Client ID (and secret, if the registered app has
one) come from a 1Password-mounted `.env`, per the hue-control pattern.
Device flow avoids configuring a redirect URI and needs no local web server.

**`api.py`** — thin wrapper over `https://api.home-connect.com/api`. Exposes
`get(path)` and nothing else. Maps the documented failure modes to meaningful
outcomes rather than raising raw HTTP errors.

**`appliances.py`** — `GET /homeappliances`, then per appliance `GET /status`
and `GET /programs/active`. Returns plain dataclasses; knows nothing about
formatting.

**`present.py`** — a registry of renderers keyed on appliance `type`
(`Dishwasher` first). An unknown type falls back to a generic renderer that
shows only `BSH.Common.*` fields, so a new oven degrades to a useful summary
instead of crashing. Adding an appliance means adding one renderer and
touching nothing else.

**`cli.py`** — bare `homeconnect` prints the one-line verdict. Flags:
`--verbose` (every key, with raw names), `--json` (machine-readable),
`-a/--appliance` (filter by name or type).

## Output

```
$ homeconnect
Dishwasher  running Eco 50  47 min left  ⚠ salt low

$ homeconnect --verbose
Dishwasher (Bosch SMV6ZCX01G)
  Operation      Run
  Door           Closed
  Programme      Dishcare.Dishwasher.Program.Eco50
  Progress       38%
  Remaining      00:47:00
  Options        Half Load, Extra Dry
  Salt           ⚠ nearly empty
  Rinse aid      ok
  Machine care   not due
```

Terminal alignment uses a `display_width()` helper, not `len()` — the warning
glyphs are double-width and `len()` will misalign the columns.

## Error handling

| Condition | Behaviour |
|---|---|
| `409` on `/programs/active` | Normal idle state, not an error. This is the single most common bug in third-party clients. |
| `connected: false` | Report the appliance as offline; do not attempt further calls on it. |
| `401` | Refresh the token once, retry once, then fail with a clear message. |
| `429` | Report the quota plainly. The daily limit is roughly 1000 calls; this tool spends about four per run. |
| No credentials yet | Point the user at the one-off `homeconnect auth` step rather than a stack trace. |

## Testing

Recorded JSON fixtures covering: idle appliance, running programme, finished,
offline, salt low, machine care due, and an unrecognised appliance type. No
network access in the suite.

Tests must pass on a fresh clone — in particular, do not assert that
gitignored runtime directories exist. CI reuses the shared `tests.yml`
workflow copied verbatim from a sibling project under `scripts/`, not rewritten.

## Out of scope for v1

Writing anything; the SSE event stream; storing history; energy and water
stats; notifications; settings; an MCP server. Each is a later, separate
decision, and none of them is blocked by this design.

## Findings from the live probe (26 August 2026)

Run against the real appliance with an `IdentifyAppliance Monitor` token. These
supersede the "open question" section above, which is now answered.

**Authentication works end to end.** Device flow, refresh token in the login
Keychain, silent refresh with rotation written back — all verified. The granted
scope came back as exactly `IdentifyAppliance Monitor`, and the access token
lasts 24 hours.

**The read-only boundary is real, not just documented.** `GET /settings`
returns `403 insufficient_scope`. The token cannot read settings, and by the
same mechanism cannot write anything.

**`/programs/active` returns 404, not 409, when nothing is running.** The error
key is `SDK.Error.NoProgramActive` either way. Code must treat 404 on that path
as the ordinary idle state.

**The consumable and care values are not status keys at all.** Asking for each
one individually returns:

```
Dishcare.Dishwasher.Event.SaltNearlyEmpty        409  SDK.Error.UnsupportedStatus
Dishcare.Dishwasher.Event.RinseAidNearlyEmpty    409  SDK.Error.UnsupportedStatus
Dishcare.Dishwasher.Event.MachineCareReminder    409  SDK.Error.UnsupportedStatus
BSH.Common.Status.OperationState                 200  …OperationState.Ready
```

The control key returning 200 confirms the method was sound: these three are
genuinely unsupported as status, not merely absent because the conditions are
inactive. They exist only as events on the SSE stream.

**Consequence:** an on-demand command cannot answer "does it need salt, rinse
aid, or a cleaning cycle" — the headline feature this tool was wanted for.
Answering it requires something that is already listening. This is a design
decision for the maintainer, not one to resolve by quietly adding a daemon.

**What a poll *can* read** (whole `/status` payload on an idle machine):

```
BSH.Common.Status.OperationState              Ready
BSH.Common.Status.DoorState                   Open
BSH.Common.Status.RemoteControlActive         True
BSH.Common.Status.RemoteControlStartAllowed   True
```

plus, whilst a programme runs, its key and options (remaining time, progress),
and `/programs` and `/programs/available` both return 200.

## Decision taken (26 August 2026)

Build the on-demand CLI **without** consumables and machine care, since a poll
cannot see them. Everything else in this design stands and is verified working.

A background SSE listener is the only way to report salt, rinse aid and machine
care. It is deferred, not rejected: if the missing warnings prove irritating in
daily use, it becomes a phase 2 with its own spec and plan. Do not add one to
this project without that.

## Phase 2, if it earns its place: the event listener

Deferred deliberately, recorded so the reasoning need not be reconstructed.
Build this only if daily use of the CLI shows the gap actually matters.

**Shape.** The same split as `econsult-window-monitor`: a long-running process
that watches, a small local store, and a CLI that only ever reads the store.
`daemon.py` / `store.py` / `cli.py`, with a launchd plist using `KeepAlive`.
Most of that project's scaffolding transfers directly.

**The one real difference, and it is not in the code.** econsult *polls* a page
on a schedule, so a missed run is caught by the next one. The Home Connect
stream is **change-only**: it reports the moment salt goes low and never
mentions it again. Connecting sends nothing — verified live, 25 seconds of
silence on a fresh connection. The store is therefore not a cache that can be
rebuilt from the API; it is the only record that the transition ever happened.

**What follows from that:**

- **Unknown is not the same as ok.** On first run, and after any gap in
  coverage, the store cannot say "salt is fine" — only "nothing has told me
  otherwise". The CLI must render that distinction honestly rather than
  defaulting to reassurance, and `unknown` will be the correct answer more often
  than is comfortable.
- **Uptime becomes correctness**, not merely convenience. A machine that sleeps
  is a poor host. If this is ever built, an always-on host is part of the design,
  not an optimisation.
- **The connection needs tending.** Reconnect with backoff when the stream drops,
  and note the access token expires after 24 hours: the listener must refresh
  and re-establish rather than sitting on a connection that has gone dead
  underneath it.

**The payoff is larger than consumables.** The same stream carries
`BSH.Common.Event.ProgramFinished`, which is the only route to cycle history and
run statistics — the figures the API refuses to expose directly (see the
findings above). Salt warnings and "how often do we actually run this thing"
come from one listener. That, rather than the consumables alone, is the argument
for building it.

**Scope note.** Phase 2 gets its own spec and plan. It must not be folded into
this project, whose whole value is being small enough to trust.

