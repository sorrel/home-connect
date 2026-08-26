"""The recorder: seed, listen, reconcile, and mark what was missed.

The vendor's stream reports changes only and sends nothing on connect, so the
recorder must poll once to establish a baseline before it has anything to diff
against. It then holds the stream, and polls again hourly to re-establish ground
truth — cheap insurance against a missed event or a silent disconnection.

Every function that matters takes its dependencies as arguments, so the tests
drive all of it without a network, a Keychain, or a data directory.
"""

from __future__ import annotations

from typing import Any, Callable

from . import appliances as appliances_module
from . import store

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
