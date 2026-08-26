"""The recorder: seed, listen, reconcile, and mark what was missed.

The vendor's stream reports changes only and sends nothing on connect, so the
recorder must poll once to establish a baseline before it has anything to diff
against. It then holds the stream, and `stream_once` itself ends the connection
and returns `"reconcile"` once `RECONCILE_SECONDS` has elapsed, so the next loop
iteration polls again and re-establishes ground truth — cheap insurance against
a missed event or a silent disconnection.

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
from .api import BASE_URL, Client, HomeConnectError, NotAuthorised

#: Poll again this often even while the stream looks healthy.
RECONCILE_SECONDS = 3600

#: Below this, an interruption is not worth a `coverage_gap` marker. Every
#: state this log tracks changes on the scale of hours (a wash cycle, a door
#: left open), so a blind spot shorter than this cannot hide a genuine
#: transition — recording it anyway would flood the log with reconnects that
#: nobody needs to read.
GAP_THRESHOLD_SECONDS = 120

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


class AuthenticationExhausted(Exception):
    """Two consecutive authentication failures within `run()`.

    A single 401 is ordinary — tokens expire — and is handled by invalidating
    the cached token so the next cycle mints a fresh one. A *second* failure
    right after that means refreshing did not help, so the process exits
    non-zero instead of looping forever: `NotAuthorised` and an HTTP 401 are
    both caught inside the loop and neither would otherwise stop it, which
    would leave a recorder running for weeks writing only gap markers while
    launchd sees a healthy, long-lived process and never restarts it.
    """


class TokenHolder:
    """A refreshable, invalidatable access token cache.

    Injectable so `run()` can force a refresh after an authentication failure
    without touching the real Keychain, and so tests can see how often the
    underlying fetch actually ran.
    """

    def __init__(self, fetch: Callable[[], str]) -> None:
        self._fetch = fetch
        self._token: str | None = None

    def get(self) -> str:
        if self._token is None:
            self._token = self._fetch()
        return self._token

    def invalidate(self) -> None:
        self._token = None


def stream_once(
    session: Any,
    url: str,
    token: str,
    state: dict,
    labels: dict[str, str],
    events_path=store.EVENTS_PATH,
    now: Callable[[], str] = store.utc_now,
    state_path=store.STATE_PATH,
    clock: Callable[[], float] = time.monotonic,
    on_unknown_haid: Callable[[], None] | None = None,
) -> str:
    """Consume one stream connection until it ends. Returns why it ended.

    Three outcomes: the connection closed (`"stream_ended"`), the server
    rejected it (`"http_<code>"`), or `RECONCILE_SECONDS` elapsed without
    either (`"reconcile"`) — the hourly re-poll the module docstring
    promises, so a quiet-but-alive stream is never mistaken for a dead one
    via the socket read timeout alone.

    An event naming an haId absent from `labels` is skipped rather than
    filed under a fallback label: a pseudo-appliance would be written to
    state, would not appear in the next poll's `observe()`, and every one of
    its keys would then be logged as transitioning to `None` — a fabricated
    pair of transitions for a device this recorder never actually
    identified. `on_unknown_haid`, if given, is called once per skipped
    event so a caller can count them.
    """
    response = session.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        stream=True,
        timeout=(10, 90),
    )
    if getattr(response, "status_code", 200) != 200:
        return f"http_{response.status_code}"

    deadline = clock() + RECONCILE_SECONDS
    with response:
        for event in stream.parse_sse(response.iter_lines(decode_unicode=True)):
            if event["event"] != "KEEP-ALIVE" and event["data"]:
                payload = event["data"]
                label = labels.get(str(payload.get("haId", "")))
                if label is None:
                    if on_unknown_haid is not None:
                        on_unknown_haid()
                else:
                    apply_event(
                        state, label, stream.extract_changes(payload),
                        events_path, now,
                    )
                    # Saved after every batch, not just between cycles: a
                    # kill mid-stream must not leave state.json describing an
                    # hour-old snapshot that re-logs already-recorded
                    # transitions the next time it is diffed against reality.
                    store.save_state(state, state_path)
            if clock() >= deadline:
                return "reconcile"
    return "stream_ended"


def run(
    build_client: Callable[[], Any],
    session_factory: Callable[[], Any],
    events_path=store.EVENTS_PATH,
    state_path=store.STATE_PATH,
    sleep: Callable[[float], None] = time.sleep,
    delays: Any = None,
    delays_factory: Callable[[], Any] = stream.backoff_delays,
    iterations: int | None = None,
    now: Callable[[], str] = store.utc_now,
    clock: Callable[[], float] = time.monotonic,
    token_holder: TokenHolder | None = None,
) -> None:
    """Seed, listen, reconcile, and mark every gap that matters. Loops until
    `iterations`.

    `iterations=None` means forever. Tests pass a finite count; nothing else
    about the loop differs between test and production, because a loop that only
    runs in a special mode is not a loop anybody has tested.

    A `coverage_gap` marker brackets a real interruption: the moment
    watching stops (`coverage_lost_at`, stamped when `stream_once` returns
    anything but `"reconcile"`, or when an exception is caught) to the moment
    it resumes (the next successful poll). It is written only if that span
    exceeded `GAP_THRESHOLD_SECONDS` — `clock()` (monotonic, independent of
    `now()`'s wall-clock stamps) measures the span so a fake `now()` in tests
    cannot distort it. A `"reconcile"` ending is a deliberate, healthy
    reconnection and never starts a gap.

    Raises `AuthenticationExhausted` after two consecutive authentication
    failures, so `main()` can exit non-zero and let launchd restart the
    process with a fresh attempt rather than let it run indefinitely on a
    token that cannot be refreshed.
    """
    if delays is None:
        delays = delays_factory()

    state = store.load_state(state_path)
    if not state:
        record_gap(None, "startup", events_path, now)

    coverage_lost_at: str | None = None
    coverage_lost_since: float | None = None
    consecutive_auth_failures = 0

    completed = 0
    while iterations is None or completed < iterations:
        healthy_stream = False
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

            # This poll succeeded, so any tracked interruption ends here.
            if coverage_lost_at is not None:
                if (clock() - coverage_lost_since) >= GAP_THRESHOLD_SECONDS:
                    store.append_record(
                        store.gap_record(now(), coverage_lost_at, "recovered"),
                        events_path,
                    )
                coverage_lost_at = None
                coverage_lost_since = None

            token = token_holder.get() if token_holder else ""
            reason = stream_once(
                session_factory(), f"{BASE_URL}{stream.EVENTS_PATH_SUFFIX}",
                token, state, labels, events_path, now, state_path=state_path,
            )

            if reason == "http_401":
                consecutive_auth_failures += 1
                if token_holder:
                    token_holder.invalidate()
            else:
                consecutive_auth_failures = 0

            if reason == "reconcile":
                healthy_stream = True
            else:
                healthy_stream = reason == "stream_ended"
                if coverage_lost_at is None:
                    coverage_lost_at = now()
                    coverage_lost_since = clock()
        except NotAuthorised:
            consecutive_auth_failures += 1
            if token_holder:
                token_holder.invalidate()
            reason = "api_error:NotAuthorised"
            if coverage_lost_at is None:
                coverage_lost_at = now()
                coverage_lost_since = clock()
        except auth.NotAuthenticated as exc:
            # Not a HomeConnectError — raised by auth.access_token() when the
            # stored refresh token itself is no good, which is exactly the
            # same authentication-failure situation as a 401 from the API.
            consecutive_auth_failures += 1
            if token_holder:
                token_holder.invalidate()
            reason = f"api_error:{type(exc).__name__}"
            if coverage_lost_at is None:
                coverage_lost_at = now()
                coverage_lost_since = clock()
        except HomeConnectError as exc:
            consecutive_auth_failures = 0
            reason = f"api_error:{type(exc).__name__}"
            if coverage_lost_at is None:
                coverage_lost_at = now()
                coverage_lost_since = clock()
        except requests.RequestException:
            consecutive_auth_failures = 0
            reason = "network_error"
            if coverage_lost_at is None:
                coverage_lost_at = now()
                coverage_lost_since = clock()

        if consecutive_auth_failures >= 2:
            raise AuthenticationExhausted(
                "two consecutive authentication failures; refreshing did not help"
            )

        store.save_state(state, state_path)

        completed += 1
        if iterations is None or completed < iterations:
            if healthy_stream:
                # A cycle that streamed successfully earns a clean slate: an
                # already-advanced backoff must not punish a recorder that
                # just proved the connection is fine.
                delays = delays_factory()
            sleep(next(delays))


def main() -> int:
    """Console-script entry point for the recorder."""
    try:
        credentials = auth.load_credentials()
    except auth.MissingCredentials as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    token_holder = TokenHolder(lambda: auth.access_token(credentials))

    try:
        with store.single_instance_lock():
            run(
                build_client=lambda: Client(token_provider=token_holder.get),
                session_factory=requests.Session,
                token_holder=token_holder,
            )
    except store.AlreadyRunning as exc:
        print(f"{exc}", file=sys.stderr)
        return 3
    except AuthenticationExhausted as exc:
        print(f"{exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        return 0
    return 0
