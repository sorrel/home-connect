# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## What this is

A small, local, read-only CLI that reports the status of Home Connect
appliances (currently just a Bosch dishwasher) on demand — "what is it doing,
does it need anything?" — with no daemon and no write path. See `README.md`
for user-facing setup and usage, and
`docs/superpowers/specs/2026-08-26-home-connect-status-cli-design.md` for the
full design record, including the live-probe findings that shaped it.

## Hard rules (global constraints — do not relax without an explicit, separate decision)

- **Read-only, enforced twice.** The OAuth token requested in `auth.py` is
  `IdentifyAppliance Monitor` and nothing else — both scopes are read-only.
  On top of that, `api.py` exposes only `get()`; there is no `post`, `put` or
  `delete` method anywhere in the codebase, so a write cannot be issued even
  by accident. Do not add one without a separate, explicitly approved change.
- **Never request the `Settings` scope.** There is no read-only settings
  scope in the Home Connect API — `Settings` grants read *and* modify
  together — so it is never requested, and things like `PowerState` or
  `ChildLock` are out of reach by design, not oversight.
- **No live API calls in the test suite.** Tests run only against recorded
  JSON fixtures under `tests/fixtures/`. Never verify behaviour, fix a bug,
  or check anything by invoking the real API or the real CLI — a live run
  reads a 1Password named pipe (a blocking read), can rotate the stored
  refresh token in the Keychain, and spends real, limited daily quota.
- **British English** throughout — code comments, variable and function
  names, CLI output, commit messages, documentation.
- **Never commit directly to `main`.** Always work on a feature branch.
- **No credentials, and no personal identifiers, in anything committed.**
  This repository is intended to become public. That means no client IDs or
  secrets, no real email addresses, no hostnames, no developer-portal user
  IDs, no appliance names or serial numbers, and no absolute
  `/Users/<name>/...` paths in any tracked file.

## Module map

```
src/homeconnect/
  auth.py         OAuth2 device flow; refresh token held in the macOS Keychain
  api.py          GET-only HTTP client; maps API error keys to typed exceptions
  appliances.py   enumerate appliances, fetch per-appliance status + programme
  present.py      renderers keyed by appliance type, with a generic fallback
  cli.py          Click entry point (bare command, `--verbose`, `--json`,
                  `-a/--appliance`, and the `auth` sub-command)
```

- **`auth.py`** — one-off device-flow consent. Client ID/secret come from a
  1Password-mounted `.env` at the repository root (never committed). The
  refresh token is stored in the Keychain and rotates on every use; the
  rotated value is always written back, since Home Connect invalidates the
  previous one.
- **`api.py`** — thin wrapper over `https://api.home-connect.com/api`.
  Exposes `get(path)` only. Distinguishes `NoProgrammeActive` (the ordinary
  idle state — the API returns `404` here, not the documented `409`) from
  genuine errors like `NotAuthorised`, `QuotaExceeded` and `ApplianceOffline`.
- **`appliances.py`** — `GET /homeappliances`, then per-appliance `GET
  /status` and `GET /programs/active`. Returns plain dataclasses
  (`Appliance`, `Programme`, `ApplianceState`); knows nothing about display
  formatting. An offline appliance is reported without further calls.
- **`present.py`** — a registry of renderers keyed on appliance `type`
  (`Dishwasher` is the only one implemented). An unrecognised type falls back
  to a generic renderer showing only the shared `BSH.Common.*` fields, so a
  future oven or other appliance degrades to a useful summary rather than
  crashing. Uses a `display_width()` helper rather than `len()` for column
  alignment, since warning glyphs are double-width.
- **`cli.py`** — Click entry point. Routes every exception that can come from
  the API or the credential store through one `_run()` helper, so failures
  turn into a guidance message (e.g. "run `homeconnect auth`") rather than a
  traceback.

## Registered application state

The Home Connect developer-portal application backing this tool stays in
*development* state, which binds it to the single Home Connect account used
to run `homeconnect auth`. That is intentional and sufficient for personal
use. Serving a second account would require the portal's production-approval
process, which is out of scope and not planned.

## What the API cannot provide (do not go looking for it)

Confirmed by probing the live API, not assumed from documentation:

- **No energy, water, cycle-count or run-history data exists at all**, even
  though the Home Connect app displays all of it. There is no endpoint for
  it, documented or otherwise.
- **Salt, rinse aid and machine-care reminders are not status fields.**
  Requesting any of them as a status returns `409 SDK.Error.UnsupportedStatus`
  — they exist only as change-only events on the SSE stream. An on-demand
  command, by construction, cannot see them.

Both of these were the reason a background event listener was considered and
then deliberately deferred (see the design spec's "Phase 2" section) rather
than folded into this project. Do not add SSE handling, polling loops, or a
local history store to this codebase without a separate spec — the value of
this tool is being small enough to trust.

## Testing

Run with:

```bash
uv run pytest
```

Fixtures live under `tests/fixtures/` (recorded appliance list, idle status,
and an idle `/programs/active` response); other states used in the tests are
built inline as dataclasses. No network access happens in the suite.
