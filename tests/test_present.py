from homeconnect.appliances import Appliance, ApplianceState, Programme
from homeconnect.present import display_width, render

DISHWASHER = Appliance(
    ha_id="BOSCH-TEST-0001", name="Dishwasher", type="Dishwasher",
    brand="Bosch", vib="SMV6ZCX01G", enumber="SMV6ZCX01G/40", connected=True,
)

WASHER = Appliance(
    ha_id="BOSCH-TEST-0002", name="Washing machine", type="Washer",
    brand="Bosch", vib="WGB256090", enumber="WGB256090/44", connected=True,
)


def running_state(**status_extra):
    status = {
        "BSH.Common.Status.OperationState": "BSH.Common.EnumType.OperationState.Run",
        "BSH.Common.Status.DoorState": "BSH.Common.EnumType.DoorState.Closed",
    }
    status.update(status_extra)
    return ApplianceState(
        appliance=DISHWASHER,
        status=status,
        programme=Programme(
            key="Dishcare.Dishwasher.Program.Eco50",
            options={
                "BSH.Common.Option.RemainingProgramTime": 2820,
                "BSH.Common.Option.ProgramProgress": 38,
            },
        ),
    )


def test_display_width_counts_wide_glyphs_as_two():
    """len() would say 1 and misalign every column that follows."""
    assert display_width("⚠") == 2
    assert display_width("abc") == 3


def test_running_dishwasher_reports_programme_and_time():
    line = render(running_state())

    assert "Eco 50" in line
    assert "47 min" in line


def test_idle_dishwasher_reports_idle():
    state = ApplianceState(
        appliance=DISHWASHER,
        status={
            "BSH.Common.Status.OperationState":
                "BSH.Common.EnumType.OperationState.Inactive"
        },
        programme=None,
    )

    assert "idle" in render(state).lower()


def test_door_open_is_reported_when_idle():
    """Door state is one of the few things a poll can actually see."""
    state = ApplianceState(
        appliance=DISHWASHER,
        status={
            "BSH.Common.Status.OperationState":
                "BSH.Common.EnumType.OperationState.Ready",
            "BSH.Common.Status.DoorState":
                "BSH.Common.EnumType.DoorState.Open",
        },
        programme=None,
    )

    assert "door open" in render(state).lower()


def test_running_washer_reports_programme_and_time():
    state = ApplianceState(
        appliance=WASHER,
        status={
            "BSH.Common.Status.OperationState":
                "BSH.Common.EnumType.OperationState.Run",
            "BSH.Common.Status.DoorState":
                "BSH.Common.EnumType.DoorState.Closed",
        },
        programme=Programme(
            key="LaundryCare.Washer.Program.Cotton.Eco4060",
            options={"BSH.Common.Option.RemainingProgramTime": 5400},
        ),
    )

    line = render(state)

    assert "Cotton Eco 40-60" in line
    assert "90 min" in line


def test_finished_washer_reports_ready_to_unload():
    state = ApplianceState(
        appliance=WASHER,
        status={
            "BSH.Common.Status.OperationState":
                "BSH.Common.EnumType.OperationState.Finished"
        },
        programme=None,
    )

    assert "ready to unload" in render(state).lower()


def test_offline_appliance_says_so():
    offline = Appliance(
        ha_id="X", name="Oven", type="Oven", brand="Bosch",
        vib="X", enumber="X/01", connected=False,
    )
    state = ApplianceState(appliance=offline, status={}, programme=None)

    assert "offline" in render(state).lower()


def test_unknown_appliance_type_falls_back_without_crashing():
    unknown = Appliance(
        ha_id="X", name="Coffee machine", type="CoffeeMaker", brand="Bosch",
        vib="X", enumber="X/01", connected=True,
    )
    state = ApplianceState(
        appliance=unknown,
        status={
            "BSH.Common.Status.OperationState":
                "BSH.Common.EnumType.OperationState.Ready"
        },
        programme=None,
    )

    output = render(state)

    assert "Coffee machine" in output
    assert "Ready" in output


def test_verbose_shows_raw_api_keys():
    output = render(running_state(), verbose=True)

    assert "BSH.Common.Status.DoorState" in output


# --- Local appliance details ------------------------------------------------

DETAILS = {"serial": "000000000000000001", "fd": "0000", "z": "00000",
           "version": "0.0-0.0.0.0 (0.0)", "sw_version": "0000000"}


def test_verbose_shows_the_identifiers_from_the_local_file():
    output = render(running_state(), verbose=True, details=DETAILS)

    assert "Serial number" in output and "000000000000000001" in output
    assert "FD number" in output and "Z-number" in output
    assert "E-number" in output and "SMV6ZCX01G/40" in output
    assert "Model" in output and "SMV6ZCX01G" in output
    assert "0.0-0.0.0.0 (0.0)" in output and "SW version" in output


def test_the_file_supplies_the_e_number_when_the_api_has_none():
    from dataclasses import replace
    state = running_state()
    bare = replace(state, appliance=replace(state.appliance, enumber=""))

    output = render(bare, verbose=True, details={"enumber": "SMV000000/00"})

    assert "SMV000000/00" in output


def test_verbose_shows_the_e_number_even_without_a_local_file():
    output = render(running_state(), verbose=True)
    assert "SMV6ZCX01G/40" in output
    assert "Serial number" not in output


def test_the_ordinary_view_never_shows_the_identifiers():
    output = render(running_state(), details=DETAILS)
    assert "000000000000000001" not in output


def test_an_offline_appliance_still_shows_its_identifiers_when_verbose():
    from dataclasses import replace
    state = running_state()
    offline = replace(state, appliance=replace(state.appliance, connected=False))

    output = render(offline, verbose=True, details=DETAILS)

    assert "offline" in output
    assert "000000000000000001" in output
