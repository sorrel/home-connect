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
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

import requests

from . import appliances as appliances_module
from . import auth
from . import store
from . import stream
from .api import BASE_URL, Client, HomeConnectError, NotAuthorised, QuotaExceeded

#: Poll again this often even while the stream looks healthy.
RECONCILE_SECONDS = 3600

#: Below this, an interruption is not worth a `coverage_gap` marker. Every
#: state this log tracks changes on the scale of hours (a wash cycle, a door
#: left open), so a blind spot shorter than this cannot hide a genuine
#: transition — recording it anyway would flood the log with reconnects that
#: nobody needs to read.
GAP_THRESHOLD_SECONDS = 120

#: How long to wait after a 429 before touching the API again. The quota is a
#: daily one, so the ordinary backoff — which tops out at five minutes — would
#: spend the rest of the day re-asking an exhausted quota for permission, which
#: is how a quota stays exhausted. Half an hour, and the stream is unaffected.
QUOTA_BACKOFF_SECONDS = 1800

#: Stream keys that name a concept the poll already has a name for. Folded at
#: the point events are applied, so one concept has exactly one name in the
#: log. Without this the poll writes `Programme` and the stream writes
#: `ActiveProgram`, and `report.cycles` — which matches `Programme` — renders
#: every live-observed cycle with no programme name at all.
_STREAM_KEY_ALIASES = {
    "ActiveProgram": "Programme",
    "SelectedProgram": "SelectedProgramme",
}

#: Vendor namespaces whose enum values are reduced to their final segment.
#: Deliberately a fixed list, not "any dotted string" — a value like "1.5"
#: must survive untouched, not be mangled into "5".
_VENDOR_PREFIXES = (
    "BSH.", "Dishcare.", "Cooking.", "Laundry.", "Refrigeration.",
    "ConsumerProducts.",
)


def log(message: str, *, error: bool = False) -> None:
    """Write one timestamped line to the recorder's log.

    Every line carries a UTC instant. launchd appends to these files and never
    truncates them, so without a timestamp there is no way to tell a line
    written moments ago from one left over by a previous run — which is exactly
    the confusion an error log exists to prevent. UTC for the same reason the
    event log uses it: the files outlive a clock change.
    """
    print(f"{store.utc_now()} {message}", file=sys.stderr if error else sys.stdout, flush=True)


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

    A *connected* appliance needs a narrower version of the same care. A poll
    can only report what the API will answer, and `store.EVENT_ONLY_KEYS` are
    keys it never will — they exist solely as stream events. Rebuilt purely
    from the poll, a salt warning learnt an hour ago would be logged as
    `SaltNearlyEmpty: "Present" -> null`, which the report reads as `ok`. So
    those keys, and only those, are carried forward from `previous` too.
    """
    observed: dict[str, dict] = {}
    for appliance in (found if found is not None else
                      appliances_module.list_appliances(client)):
        label = label_for(appliance)
        prior = (previous or {}).get(label, {}) or {}
        if not appliance.connected:
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
        for key in store.EVENT_ONLY_KEYS:
            if key not in flat and key in prior:
                flat[key] = prior[key]
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

    Stream key names are folded onto the poll's namespace here, via
    `_STREAM_KEY_ALIASES`, so that one concept has one name in the log
    regardless of which of the two sources happened to observe it.
    """
    stamp = now()
    current = state.setdefault(label, {})
    for raw_key, raw_value in changes:
        key = store.short_key(raw_key)
        key = _STREAM_KEY_ALIASES.get(key, key)
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


def _last_watched_at(state_path) -> str | None:
    """When the recorder last wrote state, as a `utc_now`-shaped stamp.

    The state file is rewritten after every event and every poll, so its
    modification time is the last instant anything was known to be watching —
    which is exactly the `from` a restart's coverage marker needs.
    """
    try:
        modified = state_path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(modified, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _gap_worth_recording(since: str | None, until: str) -> bool:
    """Whether an interruption from `since` to `until` deserves a marker.

    An unreadable pair of stamps is recorded rather than dismissed: this log
    exists to distinguish an uneventful hour from an unwatched one, so where
    the length cannot be established, "unknown" must not silently become
    "too short to matter".
    """
    if since is None:
        return False
    span = store.elapsed_seconds(since, until)
    return span is None or span >= GAP_THRESHOLD_SECONDS


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

    The deadline is checked per *line*, not per parsed event: a vendor
    keep-alive sent as a bare SSE comment (a line starting with `:`) is
    swallowed by `parse_sse` and never surfaces as an event, so a per-event
    check alone would miss it and the cycle would run on until the socket's
    read timeout fired instead. This still cannot help when the stream sends
    nothing at all — `iter_lines()` blocks on the underlying socket read
    regardless of how eagerly a wrapping generator would like to check the
    clock — and that residual is inherent to a blocking read, not something
    worth engineering around here.

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
        # Closed explicitly: a rejected stream is still an open connection, and
        # a 401 every cycle for a fortnight would otherwise leak one apiece.
        response.close()
        return f"http_{response.status_code}"

    deadline = clock() + RECONCILE_SECONDS
    hit_deadline: list[bool] = []

    def lines_until_deadline() -> Iterator[str]:
        for line in response.iter_lines(decode_unicode=True):
            yield line
            if clock() >= deadline:
                hit_deadline.append(True)
                return

    with response:
        for event in stream.parse_sse(lines_until_deadline()):
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

    return "reconcile" if hit_deadline else "stream_ended"


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
    exceeded `GAP_THRESHOLD_SECONDS`, measured on the **wall clock** from the
    `now()` stamps themselves — see `store.elapsed_seconds` for why monotonic
    time is the wrong instrument on a laptop that sleeps. The marker carries
    the diagnosed reason (`stream_lost`, `network_error`, `quota_exceeded`,
    an `api_error:…`), because it is the only diagnostic a later reader has.
    A `"reconcile"` ending is a deliberate, healthy reconnection and never
    starts a gap.

    Every start where prior state exists and time has passed since it was
    written also gets a marker. The commonest interruption by far is not a
    crash but a restart — a sleeping laptop, a logout, `launchctl unload` —
    and after the first run `state.json` always exists, so a `startup` marker
    written only when state is absent would never be written again. The
    state file's modification time is the last instant the recorder is known
    to have been watching.

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
    else:
        last_known_good = _last_watched_at(state_path)
        resumed_at = now()
        if _gap_worth_recording(last_known_good, resumed_at):
            store.append_record(
                store.gap_record(resumed_at, last_known_good, "restart"), events_path
            )

    coverage_lost_at: str | None = None
    coverage_lost_reason: str | None = None
    consecutive_auth_failures = 0

    completed = 0
    while iterations is None or completed < iterations:
        healthy_stream = False
        hit_quota = False
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
                resumed_at = now()
                if _gap_worth_recording(coverage_lost_at, resumed_at):
                    store.append_record(
                        store.gap_record(
                            resumed_at, coverage_lost_at,
                            coverage_lost_reason or "recovered",
                        ),
                        events_path,
                    )
                    # The marker records the length; the log says it out loud,
                    # so that the file someone actually opens when something
                    # looks wrong closes the story the failure line opened.
                    span = store.elapsed_seconds(coverage_lost_at, resumed_at)
                    length = "an unknown period" if span is None else f"{span:.0f}s"
                    log(
                        f"coverage resumed after {length} "
                        f"({coverage_lost_reason or 'recovered'})"
                    )
                coverage_lost_at = None
                coverage_lost_reason = None

            unknown_haid_events: list[None] = []
            token = token_holder.get() if token_holder else ""
            reason = stream_once(
                session_factory(), f"{BASE_URL}{stream.EVENTS_PATH_SUFFIX}",
                token, state, labels, events_path, now, state_path=state_path,
                clock=clock, on_unknown_haid=lambda: unknown_haid_events.append(None),
            )
            if unknown_haid_events:
                log(
                    f"skipped {len(unknown_haid_events)} event(s) this cycle "
                    "for an haId not in the current appliance list",
                    error=True,
                )

            if reason == "http_401":
                consecutive_auth_failures += 1
                if token_holder:
                    token_holder.invalidate()
            else:
                consecutive_auth_failures = 0

            # Only "reconcile" is a healthy ending: it is a deliberate,
            # scheduled reconnection. "stream_ended" is a lost connection —
            # letting it reset the backoff too would mean a server that
            # accepts and immediately drops the connection gets hammered at
            # the shortest delay forever, instead of the backoff escalating
            # as it exists to do.
            healthy_stream = reason == "reconcile"
            if not healthy_stream and coverage_lost_at is None:
                coverage_lost_at = now()
                # "stream_ended" is the loop's internal word for it; the log's
                # word, and the spec's, is "stream_lost".
                coverage_lost_reason = (
                    "stream_lost" if reason == "stream_ended" else reason
                )
        except NotAuthorised:
            consecutive_auth_failures += 1
            if token_holder:
                token_holder.invalidate()
            reason = "api_error:NotAuthorised"
            if coverage_lost_at is None:
                coverage_lost_at = now()
                coverage_lost_reason = reason
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
                coverage_lost_reason = reason
        except auth.KeyringError as exc:
            # A locked, denied or reprompting Keychain. Left uncaught this
            # escapes main() as a traceback and kills a process that was meant
            # to run for weeks, so it is treated as what it is: a failure to
            # authenticate. Two in a row still exit non-zero, which lets
            # launchd retry later rather than spin against a locked Keychain.
            consecutive_auth_failures += 1
            if token_holder:
                token_holder.invalidate()
            reason = f"keychain_error:{type(exc).__name__}"
            log(
                "the macOS Keychain could not be read "
                f"({type(exc).__name__}). Unlock it and allow access, or run "
                "`homeconnect auth` again.",
                error=True,
            )
            if coverage_lost_at is None:
                coverage_lost_at = now()
                coverage_lost_reason = reason
        except QuotaExceeded:
            # The daily quota is spent. Backing off by the ordinary sequence
            # would re-ask an exhausted quota every five minutes for the rest
            # of the day; the stream needs no quota and keeps running.
            consecutive_auth_failures = 0
            hit_quota = True
            reason = "quota_exceeded"
            if coverage_lost_at is None:
                coverage_lost_at = now()
                coverage_lost_reason = reason
        except HomeConnectError as exc:
            consecutive_auth_failures = 0
            reason = f"api_error:{type(exc).__name__}"
            if coverage_lost_at is None:
                coverage_lost_at = now()
                coverage_lost_reason = reason
        except requests.RequestException as exc:
            consecutive_auth_failures = 0
            reason = "network_error"
            if coverage_lost_at is None:
                coverage_lost_at = now()
                coverage_lost_reason = reason
                # Logged only as coverage is lost, never once per retry: the
                # backoff tops out at five minutes, so a long outage is
                # hundreds of cycles, and these files are never rotated. The
                # exception is the only account of *why* — `events.jsonl`
                # records the blind spot but reduces every cause to
                # "network_error" — so if it is not written here it is lost.
                log(
                    f"network error, reconnecting: {type(exc).__name__}: {exc}",
                    error=True,
                )

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
            sleep(QUOTA_BACKOFF_SECONDS if hit_quota else next(delays))


def main() -> int:
    """Console-script entry point for the recorder."""
    try:
        credentials = auth.load_credentials()
    except auth.MissingCredentials as exc:
        log(f"{exc}", error=True)
        return 2

    token_holder = TokenHolder(lambda: auth.access_token(credentials))
    log("recorder starting")

    try:
        with store.single_instance_lock():
            run(
                build_client=lambda: Client(token_provider=token_holder.get),
                session_factory=requests.Session,
                token_holder=token_holder,
            )
    except store.AlreadyRunning as exc:
        log(f"{exc}", error=True)
        return 3
    except AuthenticationExhausted as exc:
        log(f"{exc}", error=True)
        return 4
    except KeyboardInterrupt:
        log("recorder stopped by interrupt")
        return 0
    log("recorder stopped")
    return 0
