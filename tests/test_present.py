from homeconnect.appliances import Appliance, ApplianceState, Programme
from homeconnect.present import display_width, render

DISHWASHER = Appliance(
    ha_id="BOSCH-TEST-0001", name="Dishwasher", type="Dishwasher",
    brand="Bosch", vib="SMV6ZCX01G", enumber="SMV6ZCX01G/40", connected=True,
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
