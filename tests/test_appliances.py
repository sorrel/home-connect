import pytest

from homeconnect import appliances
from homeconnect.api import NoProgrammeActive


class FakeClient:
    def __init__(self, responses):
        self._responses = dict(responses)

    def get(self, path):
        value = self._responses[path]
        if isinstance(value, Exception):
            raise value
        return value


APPLIANCES = {
    "homeappliances": [
        {
            "haId": "BOSCH-TEST-0001",
            "name": "Dishwasher",
            "type": "Dishwasher",
            "brand": "Bosch",
            "vib": "SMV6ZCX01G",
            "enumber": "SMV6ZCX01G/40",
            "connected": True,
        }
    ]
}


def test_list_appliances_parses_records():
    client = FakeClient({"/homeappliances": APPLIANCES})

    found = appliances.list_appliances(client)

    assert len(found) == 1
    assert found[0].ha_id == "BOSCH-TEST-0001"
    assert found[0].type == "Dishwasher"
    assert found[0].connected is True


def test_fetch_state_flattens_status_array():
    appliance = appliances.list_appliances(
        FakeClient({"/homeappliances": APPLIANCES})
    )[0]
    client = FakeClient({
        "/homeappliances/BOSCH-TEST-0001/status": {
            "status": [
                {"key": "BSH.Common.Status.DoorState",
                 "value": "BSH.Common.EnumType.DoorState.Closed"},
                {"key": "BSH.Common.Status.OperationState",
                 "value": "BSH.Common.EnumType.OperationState.Run"},
            ]
        },
        "/homeappliances/BOSCH-TEST-0001/programs/active": {
            "key": "Dishcare.Dishwasher.Program.Eco50",
            "options": [
                {"key": "BSH.Common.Option.RemainingProgramTime", "value": 2820},
                {"key": "BSH.Common.Option.ProgramProgress", "value": 38},
            ],
        },
    })

    state = appliances.fetch_state(client, appliance)

    assert state.status["BSH.Common.Status.OperationState"].endswith("Run")
    assert state.programme.key == "Dishcare.Dishwasher.Program.Eco50"
    assert state.programme.options["BSH.Common.Option.ProgramProgress"] == 38


def test_fetch_state_treats_idle_409_as_no_programme():
    """An idle appliance must produce a state object, not an exception."""
    appliance = appliances.list_appliances(
        FakeClient({"/homeappliances": APPLIANCES})
    )[0]
    client = FakeClient({
        "/homeappliances/BOSCH-TEST-0001/status": {"status": []},
        "/homeappliances/BOSCH-TEST-0001/programs/active": NoProgrammeActive("idle"),
    })

    state = appliances.fetch_state(client, appliance)

    assert state.programme is None


def test_parses_the_recorded_live_payloads():
    """Guards the hand-written dicts above against drifting from reality.

    These fixtures are the exact responses the real appliance returned on
    26 August 2026 (haId anonymised). If our parsing assumptions are wrong,
    this fails even when every hand-written fixture above still passes.
    """
    import json
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures"
    raw_appliances = json.loads((fixtures / "appliances.json").read_text())
    raw_status = json.loads((fixtures / "status_idle.json").read_text())
    ha_id = raw_appliances["data"]["homeappliances"][0]["haId"]

    client = FakeClient({
        "/homeappliances": raw_appliances["data"],
        f"/homeappliances/{ha_id}/status": raw_status["data"],
        f"/homeappliances/{ha_id}/programs/active": NoProgrammeActive(
            "SDK.Error.NoProgramActive"
        ),
    })

    appliance = appliances.list_appliances(client)[0]
    state = appliances.fetch_state(client, appliance)

    assert appliance.type == "Dishwasher"
    assert appliance.connected is True
    assert state.status["BSH.Common.Status.DoorState"].endswith("Open")
    assert state.programme is None


def test_fetch_state_skips_calls_for_offline_appliance():
    offline = appliances.Appliance(
        ha_id="BOSCH-TEST-0002", name="Oven", type="Oven", brand="Bosch",
        vib="X", enumber="X/01", connected=False,
    )
    client = FakeClient({})  # any call would raise KeyError

    state = appliances.fetch_state(client, offline)

    assert state.status == {}
    assert state.programme is None
