"""Turn appliance state into text.

Renderers are registered by appliance type. Anything unrecognised falls back to
a generic renderer that shows only the `BSH.Common.*` keys every appliance
shares, so a newly added oven degrades to a useful summary rather than crashing.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Callable

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


def _render_dishwasher(state: ApplianceState, verbose: bool) -> str:
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
        parts.append("finished — ready to empty")
    elif operation in ("Inactive", "Ready"):
        parts.append("idle")
    else:
        parts.append(operation.lower())

    # Worth surfacing: a door left open is the usual reason a machine that
    # looks ready has not actually started.
    if _enum_tail(state.status.get("BSH.Common.Status.DoorState", "")) == "Open":
        parts.append("door open")

    return "  ".join(parts)


def _render_generic(state: ApplianceState, verbose: bool) -> str:
    """Fallback for appliance types we have no dedicated renderer for."""
    if verbose:
        return _render_verbose(state)

    operation = _enum_tail(
        state.status.get("BSH.Common.Status.OperationState", "Unknown")
    )
    return f"{state.appliance.name}  {operation}"


def _render_verbose(state: ApplianceState) -> str:
    """Every key we hold, with its raw API name, for debugging."""
    appliance = state.appliance
    lines = [f"{appliance.name} ({appliance.brand} {appliance.vib})"]

    keys = list(state.status)
    if state.programme is not None:
        keys.extend(state.programme.options)
    label_width = max([display_width(key) for key in keys] + [len("Programme")])

    if state.programme is not None:
        lines.append(f"  {'Programme'.ljust(label_width)}  {state.programme.key}")
        for key, value in sorted(state.programme.options.items()):
            padding = " " * (label_width - display_width(key))
            lines.append(f"  {key}{padding}  {value}")

    for key, value in sorted(state.status.items()):
        padding = " " * (label_width - display_width(key))
        lines.append(f"  {key}{padding}  {value}")

    return "\n".join(lines)


RENDERERS: dict[str, Callable[[ApplianceState, bool], str]] = {
    "Dishwasher": _render_dishwasher,
}


def render(state: ApplianceState, verbose: bool = False) -> str:
    """Render one appliance's state as text."""
    if not state.appliance.connected:
        return f"{state.appliance.name}  offline"

    renderer = RENDERERS.get(state.appliance.type, _render_generic)
    return renderer(state, verbose)
