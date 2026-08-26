# Home Connect status CLI

A small, local, **read-only** command-line tool that answers one question on
demand: what is the dishwasher doing, and does it need anything? One command,
one glance, no daemon, no background process.

It is built to generalise to other Home Connect appliances over time — the
plumbing is appliance-agnostic, and only the presentation layer knows what a
dishwasher is — but a dishwasher is the only appliance it has been used
against so far.

## What this is not

- **Not a controller.** There is no way to start, stop, pause or otherwise
  change an appliance, and no such command is planned. This is enforced
  twice: the authorisation token is requested with only the `IdentifyAppliance`
  and `Monitor` scopes — both read-only — so a write request would be refused
  by the server regardless of what the code did, and on top of that, no
  POST/PUT/DELETE call exists anywhere in the codebase.
- **Not a settings viewer.** The `Settings` scope is deliberately never
  requested. Home Connect has no read-only settings scope — `Settings` grants
  read *and* modify together — so reading things like power state or child
  lock is not worth holding a token that could also change them.
- **Not an energy or water monitor, and never will be from this API.** The
  Home Connect app shows energy use, water use, cycle counts and run history
  under "Easy Access", but no documented endpoint exposes any of it, for any
  appliance. This was confirmed by probing the live API, not assumed from the
  documentation. Do not go looking for it — it is not there to find.
- **Not able to report salt, rinse aid or machine-care warnings.** These read
  like they should be ordinary status fields, but they are not: asking the
  API for any of `Dishcare.Dishwasher.Event.SaltNearlyEmpty`,
  `...RinseAidNearlyEmpty` or `...MachineCareReminder` as a status returns
  `409 SDK.Error.UnsupportedStatus`. They exist only as change-only events on
  the server-sent-events stream, which nothing in this tool subscribes to. A
  command that runs once and exits cannot see them — this was the headline
  feature the tool was originally wanted for, and it turned out not to be
  obtainable on demand. Seeing it would need a long-running listener, which is
  a deliberately separate piece of work (see "Deferred: an event listener"
  below), not something to bolt on quietly.

## What it does show

- Running / idle / finished / error, from `BSH.Common.Status.OperationState`
- Door open, closed or locked
- Which programme is active, its options, remaining time and progress
- Whether the appliance is online at all

## Setup

This follows the same pattern as the other tools in this workspace:
credentials live in a 1Password Environment, never on disk as plaintext.

1. **Register an application** at the Home Connect developer portal. Set
   **OAuth Flow to Device Flow** — the portal states this cannot be changed
   afterwards, so get it right at creation — and leave **One Time Token
   Mode** off.
2. **Create a 1Password Environment** (e.g. named "Home Connect") holding two
   variables: `HOMECONNECT_CLIENT_ID` and `HOMECONNECT_CLIENT_SECRET` (the
   secret is optional — only set it if the registered application has one).
3. **Mount the environment's local `.env` destination** to this repository's
   root as `.env` (gitignored, never committed).
4. **Run the one-off authorisation:**

   ```bash
   uv run homeconnect auth
   ```

   This shows a short code and a URL. Open the URL in a browser, sign in
   with the Home Connect account the appliance is paired to, and enter the
   code. Once approved, the refresh token is stored in the macOS Keychain.
   Access tokens are then refreshed silently on every run — you should not
   need to repeat this step unless the stored credential is revoked.

**Note on the registered application:** it stays in *development* state on
the developer portal, which binds it to the single Home Connect account used
to authorise it. That is fine for personal use. Serving more than one account
would require going through the portal's production-approval process, which
this project has not done and has no plan to do.

## Usage

```bash
# One-line verdict for every appliance on the account
uv run homeconnect

# Every field, with raw API key names — useful for debugging
uv run homeconnect --verbose

# Machine-readable output
uv run homeconnect --json

# Only appliances matching this name or type (case-insensitive substring)
uv run homeconnect --appliance dishwasher

# One-off authorisation (see Setup)
uv run homeconnect auth
```

Example output, bare:

```
$ homeconnect
Dishwasher  running Eco 50  47 min left
```

Example output, verbose:

```
$ homeconnect --verbose
Dishwasher (Bosch SMV6ZCX01G)
  Programme                               Dishcare.Dishwasher.Program.Eco50
  BSH.Common.Option.ProgramProgress       38
  BSH.Common.Option.RemainingProgramTime  2820
  BSH.Common.Status.DoorState             BSH.Common.EnumType.DoorState.Closed
  BSH.Common.Status.OperationState        BSH.Common.EnumType.OperationState.Run
  BSH.Common.Status.RemoteControlActive   True
```

Verbose deliberately prints the **raw API key names**, not friendly labels:
its job is debugging, and the key is what you would search the API
documentation for. Values are raw too — `RemainingProgramTime` is in seconds.
`--verbose` has no effect alongside `--json`, which always emits the full
payload.

An idle appliance is shown as such, not as an error — a `404` on the active
programme is the ordinary resting state of a machine with nothing running,
not a fault, even though older Home Connect documentation says to expect
`409` there.

An unrecognised appliance type still produces useful output: it falls back to
a generic view of the shared `BSH.Common.*` fields rather than failing.

## Development

```bash
uv sync
uv run pytest
```

The test suite runs entirely against recorded JSON fixtures under
`tests/fixtures/` — it never makes a live API call, and running it does not
touch the real appliance or spend API quota.

## Deferred: an event listener

Salt, rinse aid, machine care, and any kind of run history or usage
statistics all require something that is already listening when the change
happens — a background process subscribed to the SSE stream, with its own
small local store, quite different in shape from this on-demand CLI. That is
a genuinely separate project with its own design questions (an always-on
host, reconnect-with-backoff, "unknown" as a distinct state from "fine"), and
is deliberately not part of this one. If it is ever built, it gets its own
specification rather than growing quietly out of this tool.
