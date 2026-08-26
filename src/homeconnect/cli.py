"""Command-line entry point.

A bare `homeconnect` prints the one-line verdict for every appliance. The
`auth` sub-command performs the one-off device-flow consent.
"""

from __future__ import annotations

import json
import time

import click
import requests

from . import api, appliances, auth, present
from . import help as help_module


def _run(action):
    """Run a zero-argument callable, turning known failures into guidance.

    Every call that can reach the Home Connect API or the credential store
    should be routed through here rather than wrapped in its own try/except,
    so the translation from exception to user-facing message stays in one
    place.
    """
    try:
        return action()
    except api.NotAuthorised as exc:
        raise click.ClickException(
            f"{exc}\nRun `homeconnect auth` to authorise this machine."
        ) from exc
    except api.QuotaExceeded as exc:
        raise click.ClickException(
            f"API quota exhausted ({exc}). The daily limit is about 1000 calls."
        ) from exc
    except api.HomeConnectError as exc:
        raise click.ClickException(str(exc)) from exc
    except auth.AuthorisationPending:
        # Not a failure: the approval loop is waiting for the person at the
        # browser. Let it through untranslated so the loop can retry.
        raise
    except auth.MissingCredentials as exc:
        raise click.ClickException(str(exc)) from exc
    except auth.NotAuthenticated as exc:
        raise click.ClickException(
            f"{exc}\nRun `homeconnect auth` to authorise this machine."
        ) from exc
    except requests.RequestException as exc:
        # Wifi off, DNS down, connection reset, timeout: by some distance the
        # likeliest real failure, and the one that used to print a traceback.
        # A specific, known, external type — not a bare `Exception`.
        raise click.ClickException(
            "Could not reach the Home Connect API. Check your connection "
            f"and try again. ({type(exc).__name__})"
        ) from exc


def _build_client():
    """Return an API client, or exit with guidance rather than a traceback."""
    credentials = _run(auth.load_credentials)

    # A one-slot cache, closed over rather than module-level: the memo must not
    # outlive the run, or a second client would inherit a stale token.
    cached: list[str] = []

    def token_provider() -> str:
        """Return the access token, minting it at most once per process.

        `Client.get` asks on every request, and every mint POSTs to the token
        endpoint, rotates the refresh token and writes the Keychain. A run
        issues three GETs, so an unmemoised provider means three rotations —
        three windows in which a crash between the POST and the Keychain write
        strands the credential and forces a browser re-consent. Access tokens
        last 24 hours, so once is plenty.
        """
        if not cached:
            cached.append(_run(lambda: auth.access_token(credentials)))
        return cached[0]

    return api.Client(token_provider=token_provider)


def _matches(appliance, needle: str) -> bool:
    needle = needle.lower()
    return needle in appliance.name.lower() or needle in appliance.type.lower()


@click.group(cls=help_module.ColouredGroup, invoke_without_command=True)
@click.option("--verbose", "-v", is_flag=True, help="Show every field with raw API key names. Ignored with --json.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--appliance", "-a", default=None, help="Only show appliances matching this name or type.")
@click.pass_context
def cli(ctx, verbose: bool, as_json: bool, appliance: str | None) -> None:
    """Read-only status for Home Connect appliances."""
    if ctx.invoked_subcommand is not None:
        return

    client = _build_client()

    found = _run(lambda: appliances.list_appliances(client))

    if appliance:
        found = [item for item in found if _matches(item, appliance)]

    if not found:
        raise click.ClickException(
            "No matching appliances. Check the account the appliance is paired to."
        )

    states = [
        _run(lambda item=item: appliances.fetch_state(client, item)) for item in found
    ]

    if as_json:
        click.echo(json.dumps([
            {
                "haId": state.appliance.ha_id,
                "name": state.appliance.name,
                "type": state.appliance.type,
                "connected": state.appliance.connected,
                "programme": state.programme.key if state.programme else None,
                "status": state.status,
                "options": state.programme.options if state.programme else {},
            }
            for state in states
        ], indent=2))
        return

    for state in states:
        click.echo(present.render(state, verbose=verbose))


@cli.command(name="auth")
def auth_command() -> None:
    # Named "auth" for the user; the Python identifier differs so it does not
    # shadow the imported `auth` module inside this file.
    """Authorise this machine (one-off)."""
    credentials = _run(auth.load_credentials)
    code = _run(lambda: auth.begin_device_authorisation(credentials))

    click.echo(f"Visit {code.verification_uri}")
    click.echo(f"and enter the code: {code.user_code}")
    click.echo("Waiting for approval…")

    interval = code.interval
    deadline = time.monotonic() + code.expires_in
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            _run(lambda: auth.redeem_device_code(credentials, code.device_code))
        except auth.AuthorisationPending as pending:
            # Only "not approved yet" retries. A decline or an expired code is
            # fatal and propagates, rather than polling on for ten minutes.
            if pending.slow_down:
                interval += 5
            continue
        click.echo("Authorised. The refresh token is stored in your Keychain.")
        return

    raise click.ClickException("Timed out waiting for approval.")


@cli.command(name="history")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def history_command(as_json: bool) -> None:
    """Report on what the recorder has observed."""
    from . import report, store

    records, skipped = store.read_records(store.EVENTS_PATH)

    if as_json:
        click.echo(json.dumps({
            "cycles": [vars(c) for c in report.cycles(records)],
            "consumables": [vars(c) for c in report.consumables(records)],
            "gaps": report.coverage(records)[0],
            "skipped": skipped,
        }, indent=2))
        return

    click.echo(report.render(records, skipped))


cli.add_command(help_module.help_command, name="help")
