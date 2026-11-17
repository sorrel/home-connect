# home-connect

A read-only command-line tool for Home Connect appliances (Bosch, Siemens, Neff
and other BSH brands). It reports what an appliance is doing, and optionally
records state changes over time.

It cannot start, stop, or configure an appliance. The OAuth token is requested
with read-only scopes, so a write would be refused by the server even if the
code attempted one, and no write verb exists in the codebase.

## What it can and cannot tell you

**Available:** operation state (idle, running, finished), door state, the active
programme and its options, remaining time and progress, and whether an appliance
is reachable.

**Not available, from the API at all:** energy use, water use, cycle counts, and
run history. The Home Connect app displays these, but no documented endpoint
exposes them. The recorder below is the only route to comparable figures, by
observing over time.

**Not available on demand:** salt, rinse aid and machine-care reminders. These
are not status values — the API returns `SDK.Error.UnsupportedStatus` for each —
and exist only as events on a change-only stream. A command that is not already
listening cannot see them.

## Requirements

- Python 3.12 or later, and [uv](https://docs.astral.sh/uv/)
- macOS (the refresh token is stored in the Keychain)
- A Home Connect account with at least one paired appliance

## Setup

1. Register an application at
   [developer.home-connect.com](https://developer.home-connect.com) with **OAuth
   Flow: Device Flow**. The portal states this cannot be changed afterwards.
   Leave One Time Token Mode off.
2. Put the client ID and secret in the environment as `HOMECONNECT_CLIENT_ID`
   and `HOMECONNECT_CLIENT_SECRET`. A `.env` file in the repository root is
   read if present; it is gitignored.
3. Authorise this machine once:

   ```bash
   uv run homeconnect auth
   ```

   You are shown a code to enter in a browser. The resulting refresh token is
   stored in the macOS Keychain and refreshed silently thereafter.

An application registered this way stays in *development* state and works only
with the Home Connect account named on it. Serving other accounts requires the
portal's production-approval process.

## Usage

```bash
uv run homeconnect                 # one-line verdict per appliance
uv run homeconnect --verbose       # every field, with raw API key names
uv run homeconnect --json          # machine-readable
uv run homeconnect -a dishwasher   # filter by name or type
uv run homeconnect history         # what the recorder has observed
```

## The recorder

`homeconnect-recorder` holds the appliance event stream open and appends each
observed state change to `data/events.jsonl`, one JSON object per line. It polls
periodically to re-establish ground truth after any interruption.

It records transitions, not readings, so an uneventful hour produces no lines.

Because it cannot watch while the machine is asleep or offline, every loss of
coverage is written to the log as a marker. `homeconnect history` reports a
value as `unknown` when its most recent information predates a gap, rather than
as `ok` — silence in the log means nobody was watching, not that nothing
happened.

To run it in the background, restarting at login:

```bash
launchd/install.sh      # install and start
launchd/uninstall.sh    # stop and remove
```

### Restarting it, and checking it came back

The recorder loads its code once, when it starts. After pulling a change,
restart it to pick the change up:

```bash
launchctl kickstart -k gui/$(id -u)/com.homeconnect.recorder
```

Then confirm it is running, and look for a fresh `recorder starting` line:

```bash
launchctl print gui/$(id -u)/com.homeconnect.recorder | grep -E '^\s*(state|pid|last exit reason) '
tail -n 3 data/recorder.log data/recorder.err
```

If `state = not running`, read `last exit reason`. A
`OS_REASON_CODESIGNING` means macOS killed the new process as it started. It has
happened once, even though both `uv` and the project's Python passed
`codesign -v`, and starting it once more worked:

```bash
launchctl kickstart gui/$(id -u)/com.homeconnect.recorder
```

Every restart writes a `coverage_gap` record to the log. The gap runs from when
the recorder last saved its state, not from the restart itself, so after a quiet
spell it can be longer than the real outage. That errs on the safe side:
`homeconnect history` counts too much time as unwatched, never too little.

The restart re-reads the 1Password-mounted `.env`. With 1Password locked, the
recorder writes a "Waiting for it" line to `recorder.err` and keeps retrying.
When it gets in, `recorder.log` shows
`credentials read from 1Password after waiting …`. Unlock 1Password if a restart
seems stuck.

`homeconnect history` prints the cycles observed (the ten most recent, and it
says so when there are more), the believed state of salt, rinse aid and machine
care, and the coverage gaps that qualify both.

## Data

Everything stays local. `data/` holds the event log and last-known state, and is
gitignored. It sits beside this README regardless of where you run the commands
from; set `HOMECONNECT_DATA_DIR` to keep it somewhere else. Credentials are never written to disk by this tool: the client ID
and secret come from the environment, and the refresh token lives in the
Keychain.

### Appliance details the API does not give

The API reports each appliance's model and E-number, but not its serial, FD or
Z number, nor its software versions. The Home Connect app shows them all under
the appliance's *Appliance information*. Copy them into `data/appliances.json`,
keyed by the appliance's name as the app shows it:

```json
{
  "Dishwasher": {
    "enumber": "SMS000000/00",
    "serial": "000000000000000000",
    "fd": "0000",
    "z": "00000",
    "version": "0.0-0.0.0.0 (0.0)",
    "sw_version": "0000000"
  }
}
```

Every field is optional. They then appear under each appliance in `--verbose`,
and under `details` in `--json`. The file lives in `data/`, so it is never
committed; if you rename an appliance in the app, rename its entry too.

## Rate limits

The API allows roughly 1000 calls per day. A status command spends about three;
the recorder spends a similar number per reconciliation.

## Tests

```bash
uv run pytest -q
```

The suite is fully mocked and never contacts the API.

## Licence

MIT. See [LICENSE](LICENSE).
