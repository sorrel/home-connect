# Next steps

State as of 26 August 2026. Both the status CLI and the event recorder are
built, reviewed, published and running. 181 tests pass; CI is green.

## Do this first

The recorder currently running was started before the timestamped-logging
change. Pick it up with:

```bash
launchd/install.sh      # unloads and reloads; the event log is untouched
: > data/recorder.err   # clear the stale RVM noise from the old shell-based plist
```

After that, every line in `recorder.log` and `recorder.err` carries a UTC
instant, and the log records when the recorder starts and stops — so it shows
signs of life instead of staying empty.

## What to expect over the first weeks

- **`Cycles: 0` until a wash actually runs.** The log records transitions, not
  readings; an uneventful day produces no lines. Silence is correct.
- **Consumables stay `unknown` for a long time.** They appear only when the
  appliance reports a change, so salt reads `unknown` until the day it runs low.
  That is the honest answer, but it does mean the feature this was wanted for
  proves itself only on the day it matters.
- **Coverage gaps will accumulate** every time the machine sleeps. That is the
  design working, not a fault. `history` reports anything whose last news
  predates the most recent gap as `unknown` rather than `ok`.

Worth checking after a week: that cycles are being paired correctly against
washes you remember running, and that gap markers line up with the machine
having been closed.

## Residual minors, in the order worth fixing

None blocks use. All were found by the final whole-branch review and judged
acceptable to defer.

1. **An unnamed appliance would log its serial.** `appliances.list_appliances`
   falls back to `name = ha_id`, so an appliance with no friendly name would
   have its serial written into the event log as the label — contradicting the
   deliberate promise in `label_for` that the haId never reaches the log. The
   dishwasher is named, so this does not currently trigger. A one-line fix, and
   the one worth taking first.
2. **Installed non-editably, the data directory resolves oddly.**
   `default_data_dir()` anchors on `Path(__file__).parents[2]`, which is right
   for a repository checkout but points inside the interpreter's tree for a
   non-editable install. `HOMECONNECT_DATA_DIR` overrides it and must be set
   before import, since `DATA_DIR` is module-level.
3. **A locked Keychain retries every minute.** The recorder exits 4 and launchd
   restarts it after its 60-second throttle, appending guidance to
   `recorder.err` each time. Bounded, but noisier than it needs to be.
4. **Two cycles beginning in the same second** could attribute a programme to
   the wrong one. Not a real scenario for a dishwasher.

## Possible future work

**Notifications.** The recorder already knows the moment a cycle finishes and
the moment salt runs low. Wiring either to a notification is small and is the
most likely thing to be actually wanted.

**Statistics over the log.** Once a few weeks of cycles exist, `history` could
report averages, most-used programmes, and time-of-day patterns. Deliberately
not built yet: there was no data to design against, and guessing at the shape of
a report before seeing real data is how reports get built that nobody reads.

**More appliances.** The plumbing is appliance-agnostic — `api`, `auth`,
`appliances` and the recorder need no changes. A new appliance type needs one
renderer in `present.py` and, if it has its own event-only keys, an addition to
`store.EVENT_ONLY_KEYS`.

**`history --since` and `-a`.** Described in an early draft of the spec and
never built. The spec has since been corrected to say so.

## Things deliberately not possible

Recorded here so they are not rediscovered.

- **Energy, water and cycle counts do not exist in the API.** The Home Connect
  app shows them; no documented endpoint exposes them. The recorder's own
  history is the only substitute.
- **Salt, rinse aid and machine care cannot be polled.** The API answers
  `SDK.Error.UnsupportedStatus`; they exist only as events on a change-only
  stream, which is why the recorder exists at all.
- **The tool cannot change the appliance**, by design. The OAuth scope is
  read-only and no write verb exists in the codebase.
