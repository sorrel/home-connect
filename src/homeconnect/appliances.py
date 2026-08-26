"""Fetch appliance records and state. Knows nothing about formatting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .api import NoProgrammeActive


@dataclass(frozen=True)
class Appliance:
    """A single registered home appliance."""

    ha_id: str
    name: str
    type: str
    brand: str
    vib: str
    enumber: str
    connected: bool


@dataclass(frozen=True)
class Programme:
    """The programme currently running, with its options."""

    key: str
    options: dict[str, Any]


@dataclass(frozen=True)
class ApplianceState:
    """Everything we could read about one appliance, in one go."""

    appliance: Appliance
    status: dict[str, Any]
    programme: Programme | None


def _flatten(entries: Any) -> dict[str, Any]:
    """Turn the API's [{key, value}, …] arrays into a plain dict.

    Raw API key names are kept as the dict keys so `--verbose` can show them
    unchanged, and so a key we have never seen before still gets displayed.
    """
    if not isinstance(entries, list):
        return {}
    return {entry["key"]: entry.get("value") for entry in entries if "key" in entry}


def list_appliances(client: Any) -> list[Appliance]:
    """Enumerate every appliance on the account."""
    payload = client.get("/homeappliances") or {}
    return [
        Appliance(
            ha_id=record["haId"],
            name=record.get("name", record["haId"]),
            type=record.get("type", "Unknown"),
            brand=record.get("brand", ""),
            vib=record.get("vib", ""),
            enumber=record.get("enumber", ""),
            connected=bool(record.get("connected", False)),
        )
        for record in payload.get("homeappliances", [])
    ]


def fetch_state(client: Any, appliance: Appliance) -> ApplianceState:
    """Read status and any active programme for one appliance.

    An offline appliance is reported as such without further calls — there is
    nothing to read and each attempt would spend quota to no purpose.
    """
    if not appliance.connected:
        return ApplianceState(appliance=appliance, status={}, programme=None)

    status_payload = client.get(f"/homeappliances/{appliance.ha_id}/status") or {}
    status = _flatten(status_payload.get("status"))

    try:
        active = client.get(f"/homeappliances/{appliance.ha_id}/programs/active") or {}
        programme = Programme(
            key=active.get("key", ""),
            options=_flatten(active.get("options")),
        )
    except NoProgrammeActive:
        programme = None

    return ApplianceState(appliance=appliance, status=status, programme=programme)
