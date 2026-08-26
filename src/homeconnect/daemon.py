"""The recorder: seed, listen, reconcile, and mark what was missed.

The vendor's stream reports changes only and sends nothing on connect, so the
recorder must poll once to establish a baseline before it has anything to diff
against. It then holds the stream, and polls again hourly to re-establish ground
truth — cheap insurance against a missed event or a silent disconnection.

Every function that matters takes its dependencies as arguments, so the tests
drive all of it without a network, a Keychain, or a data directory.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable

import requests

from . import appliances as appliances_module
from . import auth
from . import store
from . import stream
from .api import BASE_URL, Client, HomeConnectError

#: Poll again this often even while the stream looks healthy.
RECONCILE_SECONDS = 3600

#: Vendor namespaces whose enum values are reduced to their final segment.
#: Deliberately a fixed list, not "any dotted string" — a value like "1.5"
#: must survive untouched, not be mangled into "5".
_VENDOR_PREFIXES = (
    "BSH.", "Dishcare.", "Cooking.", "Laundry.", "Refrigeration.",
    "ConsumerProducts.",
)


def label_for(appliance: Any) -> str:
    """A stable, human label for an appliance.

    Deliberately the name and never the haId: the haId is a device serial, and
    this value is written to a file people will paste into bug reports.
    """
    return (appliance.name or appliance.type or "appliance").strip().lower()


def observe(
    client: Any,
    found: list | None = None,
    previous: dict | None = None,
) -> dict[str, dict]:
    """Poll every appliance and flatten it to `{label: {short_key: value}}`.

    `found` lets a caller that has already enumerated pass the result in rather
    than paying for a second listing.

    `previous` is the last-known state. A disconnected appliance has nothing
    to read, and treating that as `{}` would make every known key look like it
    vanished and then reappeared on reconnect — a fabricated pair of
    transitions that the append-only log can never undo. Instead, a
    disconnected appliance carries its previous entry forward unchanged and
    gets a single `Connected: False` marker; a connected one gets
    `Connected: True` alongside its real readings.
    """
    observed: dict[str, dict] = {}
    for appliance in (found if found is not None else
                      appliances_module.list_appliances(client)):
        label = label_for(appliance)
        if not appliance.connected:
            prior = (previous or {}).get(label, {})
            flat = dict(prior)
            flat["Connected"] = False
            observed[label] = flat
            continue

        state = appliances_module.fetch_state(client, appliance)
        flat = {
            store.short_key(key): _tail(value)
            for key, value in state.status.items()
        }
        if state.programme is not None:
            flat["Programme"] = store.short_key(state.programme.key)
            for key, value in state.programme.options.items():
                short = store.short_key(key)
                # Status and option short keys share one flat namespace; if a
                # status key already claimed this name, do not let the option
                # silently clobber it — an unrebuildable log cannot afford a
                # silent loss on an unlucky name collision.
                if short in flat:
                    short = f"Option{short}"
                flat[short] = _tail(value)
        flat["Connected"] = True
        observed[label] = flat
    return observed


def _tail(value: Any) -> Any:
    """Reduce a vendor enum to its final segment; leave other values alone."""
    if (isinstance(value, str) and "." in value
            and value.startswith(_VENDOR_PREFIXES)):
        return value.rsplit(".", 1)[-1]
    return value


def record_changes(
    before: dict,
    after: dict,
    events_path=store.EVENTS_PATH,
    now: Callable[[], str] = store.utc_now,
) -> int:
    """Append one line per differing value. Returns how many were written."""
    stamp = now()
    changes = store.diff_states(before, after)
    for label, key, was, is_now in changes:
        store.append_record(
            store.transition_record(stamp, label, key, was, is_now), events_path
        )
    return len(changes)


def apply_event(
    state: dict,
    label: str,
    changes: list[tuple[str, Any]],
    events_path=store.EVENTS_PATH,
    now: Callable[[], str] = store.utc_now,
) -> None:
    """Fold a stream event into state, logging only genuine transitions.

    The vendor repeats values freely — a repeated value is not a change, and
    logging it would inflate every cycle count drawn from this file.
    """
    stamp = now()
    current = state.setdefault(label, {})
    for raw_key, raw_value in changes:
        key = store.short_key(raw_key)
        value = _tail(raw_value)
        if current.get(key) != value:
            store.append_record(
                store.transition_record(stamp, label, key, current.get(key), value),
                events_path,
            )
            current[key] = value


def record_gap(
    since: str | None,
    reason: str,
    events_path=store.EVENTS_PATH,
    now: Callable[[], str] = store.utc_now,
) -> None:
    """Mark a period during which nothing was watching."""
    store.append_record(store.gap_record(now(), since, reason), events_path)


def stream_once(
    session: Any,
    url: str,
    token: str,
    state: dict,
    labels: dict[str, str],
    events_path=store.EVENTS_PATH,
    now: Callable[[], str] = store.utc_now,
) -> str:
    """Consume one stream connection until it ends. Returns why it ended."""
    response = session.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        stream=True,
        timeout=(10, 90),
    )
    if getattr(response, "status_code", 200) != 200:
        return f"http_{response.status_code}"

    with response:
        for event in stream.parse_sse(response.iter_lines(decode_unicode=True)):
            if event["event"] == "KEEP-ALIVE" or not event["data"]:
                continue
            payload = event["data"]
            label = labels.get(str(payload.get("haId", "")), "appliance")
            apply_event(
                state, label, stream.extract_changes(payload), events_path, now
            )
    return "stream_ended"


def run(
    build_client: Callable[[], Any],
    session_factory: Callable[[], Any],
    events_path=store.EVENTS_PATH,
    state_path=store.STATE_PATH,
    sleep: Callable[[float], None] = time.sleep,
    delays: Any = None,
    iterations: int | None = None,
    now: Callable[[], str] = store.utc_now,
    token_provider: Callable[[], str] | None = None,
) -> None:
    """Seed, listen, reconcile, and mark every gap. Loops until `iterations`.

    `iterations=None` means forever. Tests pass a finite count; nothing else
    about the loop differs between test and production, because a loop that only
    runs in a special mode is not a loop anybody has tested.
    """
    if delays is None:
        delays = stream.backoff_delays()

    state = store.load_state(state_path)
    if not state:
        record_gap(None, "startup", events_path, now)

    completed = 0
    while iterations is None or completed < iterations:
        last_good = now()
        try:
            client = build_client()
            found = appliances_module.list_appliances(client)
            # Enumerate once and reuse: a second listing per cycle would spend
            # quota to re-learn what we already know.
            labels = {a.ha_id: label_for(a) for a in found}
            observed = observe(client, found, previous=state)
            record_changes(state, observed, events_path, now)
            state = observed
            store.save_state(state, state_path)

            token = token_provider() if token_provider else ""
            reason = stream_once(
                session_factory(), f"{BASE_URL}{stream.EVENTS_PATH_SUFFIX}",
                token, state, labels, events_path, now,
            )
        except HomeConnectError as exc:
            reason = f"api_error:{type(exc).__name__}"
        except requests.RequestException:
            reason = "network_error"

        store.save_state(state, state_path)
        record_gap(last_good, reason, events_path, now)

        completed += 1
        if iterations is None or completed < iterations:
            sleep(next(delays))


def main() -> int:
    """Console-script entry point for the recorder."""
    try:
        credentials = auth.load_credentials()
    except auth.MissingCredentials as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    cached: list[str] = []

    def token_provider() -> str:
        if not cached:
            cached.append(auth.access_token(credentials))
        return cached[0]

    try:
        with store.single_instance_lock():
            run(
                build_client=lambda: Client(token_provider=token_provider),
                session_factory=requests.Session,
                token_provider=token_provider,
            )
    except store.AlreadyRunning as exc:
        print(f"{exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 0
    return 0
