"""Turn appliance state into text.

Renderers are registered by appliance type. Anything unrecognised falls back to
a generic renderer that shows only the `BSH.Common.*` keys every appliance
shares, so a newly added oven degrades to a useful summary rather than crashing.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Callable

import click

from .appliances import ApplianceState

#: Human labels for the programme keys we expect to meet.
PROGRAMME_NAMES = {
    "Dishcare.Dishwasher.Program.Auto1": "Auto 35–45",
    "Dishcare.Dishwasher.Program.Auto2": "Auto 45–65",
    "Dishcare.Dishwasher.Program.Auto3": "Auto 65–75",
    "Dishcare.Dishwasher.Program.Eco50": "Eco 50",
    "Dishcare.Dishwasher.Program.Quick45": "Quick 45",
    "Dishcare.Dishwasher.Program.Intensiv70": "Intensive 70",
    "Dishcare.Dishwasher.Program.NightWash": "Night wash",
    "Dishcare.Dishwasher.Program.Glas40": "Glass 40",
    "Dishcare.Dishwasher.Program.PreRinse": "Pre-rinse",
    "Dishcare.Dishwasher.Program.MachineCare": "Machine care",
    "LaundryCare.Washer.Program.Cotton": "Cotton",
    "LaundryCare.Washer.Program.Cotton.Eco4060": "Cotton Eco 40-60",
    "LaundryCare.Washer.Program.EasyCare": "Easy care",
    "LaundryCare.Washer.Program.Mix": "Mix",
    "LaundryCare.Washer.Program.DelicatesSilk": "Delicates/silk",
    "LaundryCare.Washer.Program.Wool": "Wool",
    "LaundryCare.Washer.Program.RinseSpinDrain": "Rinse, spin, drain",
    "LaundryCare.Washer.Program.DarkWash": "Dark wash",
    "LaundryCare.Washer.Program.Sensitive": "Sensitive",
    "LaundryCare.Washer.Program.Outdoor": "Outdoor",
    "LaundryCare.Washer.Program.Shirts": "Shirts",
}

# Deliberately absent: salt, rinse aid and machine-care reporting. Those values
# are not status keys — the API returns SDK.Error.UnsupportedStatus for each —
# and exist only as SSE events on a change-only stream, so an on-demand render
# of a single fetch cannot show them. They are reported by `homeconnect
# history` instead, from what the recorder observed while it was listening.


def display_width(text: str) -> int:
    """Width of `text` in terminal columns.

    `len()` is wrong here: warning glyphs and most emoji occupy two columns but
    count as one character, which silently misaligns every column after them.
    """
    width = 0
    for char in text:
        if unicodedata.east_asian_width(char) in ("W", "F") or ord(char) > 0x1F300:
            width += 2
        elif char == "⚠":
            width += 2
        else:
            width += 1
    return width


def _enum_tail(value: Any) -> str:
    """`BSH.Common.EnumType.OperationState.Run` -> `Run`."""
    if isinstance(value, str) and "." in value:
        return value.rsplit(".", 1)[-1]
    return str(value)


def _programme_name(key: str) -> str:
    return PROGRAMME_NAMES.get(key, _enum_tail(key))


def _remaining(state: ApplianceState) -> str | None:
    if state.programme is None:
        return None
    seconds = state.programme.options.get("BSH.Common.Option.RemainingProgramTime")
    if not isinstance(seconds, int):
        return None
    return f"{seconds // 60} min left"


def _render_cyclic_appliance(
    state: ApplianceState, verbose: bool, *, finished_message: str
) -> str:
    """Shared shape for any appliance that runs one programme to completion.

    Dishwashers and washing machines report the same three things a poll can
    actually see — operation state, active programme, door state — and differ
    only in what "done" should be called.
    """
    operation = _enum_tail(
        state.status.get("BSH.Common.Status.OperationState", "Unknown")
    )

    if verbose:
        return _render_verbose(state)

    parts = [state.appliance.name]

    if operation == "Run" and state.programme is not None:
        parts.append(f"running {_programme_name(state.programme.key)}")
        remaining = _remaining(state)
        if remaining:
            parts.append(remaining)
    elif operation == "Finished":
        parts.append(finished_message)
    elif operation in ("Inactive", "Ready"):
        parts.append("idle")
    else:
        parts.append(operation.lower())

    # Worth surfacing: a door left open is the usual reason a machine that
    # looks ready has not actually started.
    if _enum_tail(state.status.get("BSH.Common.Status.DoorState", "")) == "Open":
        parts.append("door open")

    return "  ".join(parts)


def _render_dishwasher(state: ApplianceState, verbose: bool) -> str:
    return _render_cyclic_appliance(
        state, verbose, finished_message="finished — ready to empty"
    )


def _render_washer(state: ApplianceState, verbose: bool) -> str:
    return _render_cyclic_appliance(
        state, verbose, finished_message="finished — ready to unload"
    )


def _render_generic(state: ApplianceState, verbose: bool) -> str:
    """Fallback for appliance types we have no dedicated renderer for."""
    if verbose:
        return _render_verbose(state)

    operation = _enum_tail(
        state.status.get("BSH.Common.Status.OperationState", "Unknown")
    )
    return f"{state.appliance.name}  {operation}"


RENDERERS: dict[str, Callable[[ApplianceState, bool], str]] = {
    "Dishwasher": _render_dishwasher,
    "Washer": _render_washer,
}


# --- the verbose view ------------------------------------------------------
#
# Styled to match `homeconnect history`, and like it, assumes a dark
# terminal. Every width is measured on the plain text and the colour applied
# afterwards: an ANSI escape occupies no columns, so styling first would
# misalign every column. Raw API names stay whole, since this view exists for
# debugging, but their vendor prefixes are dimmed so the part that varies is
# the part the eye lands on.

_NAME = dict(fg="bright_cyan", bold=True)
_SECTION = dict(fg="bright_white", bold=True)
_QUIET = dict(fg="bright_black")
_TAIL = dict(fg="bright_white")
_NUMBER = dict(fg="cyan")
_TRUE = dict(fg="green")
_CAVEAT = dict(fg="yellow")

#: The identifiers `--verbose` shows, in order, as (label, API attribute,
#: key in the local details file). The model sits in the heading instead; the
#: E-number comes from the API, and the rest appear only in the Home Connect
#: app and are copied into the file.
_IDENTIFIERS = (
    ("E-number", "enumber", "enumber"),
    ("Serial number", None, "serial"),
    ("FD number", None, "fd"),
    ("Z-number", None, "z"),
    ("Version", None, "version"),
    ("SW version", None, "sw_version"),
)


def _dim_prefix(text: str) -> str:
    """`BSH.Common.Status.DoorState` with everything up to the tail dimmed."""
    prefix, dot, tail = text.rpartition(".")
    if not dot:
        return click.style(text, **_TAIL)
    return click.style(prefix + dot, **_QUIET) + click.style(tail, **_TAIL)


def _styled_value(value: Any) -> str:
    if isinstance(value, bool):
        return click.style(str(value), **(_TRUE if value else _QUIET))
    if isinstance(value, (int, float)):
        return click.style(str(value), **_NUMBER)
    if isinstance(value, str) and "." in value and " " not in value:
        return _dim_prefix(value)
    return str(value)


def _section(title: str, rows: list[tuple[str, str]]) -> list[str]:
    """A titled block of aligned rows; `rows` are (plain label, styled value)."""
    if not rows:
        return []
    width = max(display_width(label) for label, _ in rows)
    lines = ["", "  " + click.style(title, **_SECTION)]
    for label, value in rows:
        padding = " " * (width - display_width(label))
        lines.append(f"    {label}{padding}  {value}")
    return lines


def _keyed_section(title: str, items: list[tuple[str, Any]]) -> list[str]:
    """As `_section`, for raw API keys, whose prefixes are dimmed."""
    if not items:
        return []
    width = max(display_width(key) for key, _ in items)
    lines = ["", "  " + click.style(title, **_SECTION)]
    for key, value in items:
        padding = " " * (width - display_width(key))
        lines.append(f"    {_dim_prefix(key)}{padding}  {_styled_value(value)}")
    return lines


def _render_verbose(state: ApplianceState,
                    details: dict[str, str] | None = None) -> str:
    """Every key we hold, with its raw API name, for debugging."""
    appliance = state.appliance
    heading = click.style(appliance.name, **_NAME)
    model = " ".join(part for part in (appliance.brand, appliance.vib) if part)
    if model:
        heading += "  " + click.style(model, **_QUIET)
    if not appliance.connected:
        heading += "  " + click.style("offline", **_CAVEAT)
    lines = [heading]

    identity = []
    for label, attribute, key in _IDENTIFIERS:
        value = getattr(appliance, attribute) if attribute else None
        value = value or ((details or {}).get(key) if key else None)
        if value:
            identity.append((label, click.style(value, **_TAIL)))
    lines += _section("Identity", identity)

    if state.programme is not None:
        lines += _keyed_section(
            "Programme",
            [("Programme", state.programme.key)]
            + sorted(state.programme.options.items()))

    lines += _keyed_section("Status", sorted(state.status.items()))
    return "\n".join(lines)


def render(state: ApplianceState, verbose: bool = False,
           details: dict[str, str] | None = None) -> str:
    """Render one appliance's state as text.

    `details` are the identifiers from the local `data/appliances.json`; they
    appear only in the verbose view, which also covers an offline appliance.
    """
    if verbose:
        return _render_verbose(state, details)
    if not state.appliance.connected:
        return f"{state.appliance.name}  offline"
    renderer = RENDERERS.get(state.appliance.type, _render_generic)
    return renderer(state, verbose)
