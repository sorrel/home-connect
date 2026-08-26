import json

import requests
from click.testing import CliRunner

from homeconnect import cli as cli_module
from homeconnect.appliances import Appliance, ApplianceState, Programme

DISHWASHER = Appliance(
    ha_id="BOSCH-TEST-0001", name="Dishwasher", type="Dishwasher",
    brand="Bosch", vib="SMV6ZCX01G", enumber="SMV6ZCX01G/40", connected=True,
)

STATE = ApplianceState(
    appliance=DISHWASHER,
    status={
        "BSH.Common.Status.OperationState":
            "BSH.Common.EnumType.OperationState.Run",
    },
    programme=Programme(
        key="Dishcare.Dishwasher.Program.Eco50",
        options={"BSH.Common.Option.RemainingProgramTime": 2820},
    ),
)


def patch_backend(monkeypatch, states=(STATE,)):
    credentials = cli_module.auth.Credentials(client_id="client-1", client_secret="s")
    monkeypatch.setattr(cli_module.auth, "load_credentials", lambda: credentials)
    monkeypatch.setattr(cli_module.auth, "access_token", lambda creds: "tok")
    monkeypatch.setattr(cli_module.api, "Client", lambda **kwargs: object())
    monkeypatch.setattr(
        cli_module.appliances, "list_appliances", lambda client: [s.appliance for s in states]
    )
    monkeypatch.setattr(
        cli_module.appliances,
        "fetch_state",
        lambda client, appliance: next(s for s in states if s.appliance is appliance),
    )


def test_bare_invocation_prints_one_liner(monkeypatch):
    patch_backend(monkeypatch)

    result = CliRunner().invoke(cli_module.cli, [])

    assert result.exit_code == 0
    assert "Eco 50" in result.output


def test_json_flag_emits_parseable_json(monkeypatch):
    patch_backend(monkeypatch)

    result = CliRunner().invoke(cli_module.cli, ["--json"])

    payload = json.loads(result.output)
    assert payload[0]["name"] == "Dishwasher"
    assert payload[0]["programme"] == "Dishcare.Dishwasher.Program.Eco50"


def test_appliance_filter_matches_name_or_type(monkeypatch):
    other = Appliance(
        ha_id="X", name="Oven", type="Oven", brand="Bosch",
        vib="X", enumber="X/01", connected=True,
    )
    other_state = ApplianceState(appliance=other, status={}, programme=None)
    patch_backend(monkeypatch, states=(STATE, other_state))

    result = CliRunner().invoke(cli_module.cli, ["-a", "oven"])

    assert "Oven" in result.output
    assert "Dishwasher" not in result.output


def test_missing_credentials_gives_guidance_not_a_traceback(monkeypatch):
    def raise_missing():
        raise cli_module.auth.MissingCredentials("HOMECONNECT_CLIENT_ID is not set")

    monkeypatch.setattr(cli_module.auth, "load_credentials", raise_missing)

    result = CliRunner().invoke(cli_module.cli, [])

    assert result.exit_code != 0
    assert "HOMECONNECT_CLIENT_ID" in result.output
    assert "Traceback" not in result.output


def test_not_authorised_points_at_the_auth_command(monkeypatch):
    """A rejected token must produce guidance, not a traceback.

    Note this drives the failure through `list_appliances` rather than through
    `auth.access_token`: the token is fetched lazily inside `token_provider`,
    which is only called when a request is actually issued. Patching
    `access_token` alone would leave it never invoked and the test would pass
    for the wrong reason.
    """
    patch_backend(monkeypatch)

    def raise_not_authorised(client):
        raise cli_module.api.NotAuthorised("invalid_token")

    monkeypatch.setattr(cli_module.appliances, "list_appliances", raise_not_authorised)

    result = CliRunner().invoke(cli_module.cli, [])

    assert result.exit_code != 0
    assert "homeconnect auth" in result.output
    assert "Traceback" not in result.output


def test_fetch_state_quota_exceeded_gives_guidance_not_a_traceback(monkeypatch):
    patch_backend(monkeypatch)

    def raise_quota_exceeded(client, appliance):
        raise cli_module.api.QuotaExceeded("daily limit reached")

    monkeypatch.setattr(cli_module.appliances, "fetch_state", raise_quota_exceeded)

    result = CliRunner().invoke(cli_module.cli, [])

    assert result.exit_code != 0
    assert "1000" in result.output
    assert "Traceback" not in result.output


def test_fetch_state_base_error_gives_guidance_not_a_traceback(monkeypatch):
    patch_backend(monkeypatch)

    def raise_base_error(client, appliance):
        raise cli_module.api.HomeConnectError("something went wrong")

    monkeypatch.setattr(cli_module.appliances, "fetch_state", raise_base_error)

    result = CliRunner().invoke(cli_module.cli, [])

    assert result.exit_code != 0
    assert "something went wrong" in result.output
    assert "Traceback" not in result.output


def test_list_appliances_base_error_gives_guidance_not_a_traceback(monkeypatch):
    patch_backend(monkeypatch)

    def raise_base_error(client):
        raise cli_module.api.HomeConnectError("server error")

    monkeypatch.setattr(cli_module.appliances, "list_appliances", raise_base_error)

    result = CliRunner().invoke(cli_module.cli, [])

    assert result.exit_code != 0
    assert "server error" in result.output
    assert "Traceback" not in result.output


def test_auth_missing_credentials_gives_guidance_not_a_traceback(monkeypatch):
    def raise_missing():
        raise cli_module.auth.MissingCredentials("HOMECONNECT_CLIENT_ID is not set")

    monkeypatch.setattr(cli_module.auth, "load_credentials", raise_missing)

    result = CliRunner().invoke(cli_module.cli, ["auth"])

    assert result.exit_code != 0
    assert "HOMECONNECT_CLIENT_ID" in result.output
    assert "Traceback" not in result.output


def test_auth_not_authenticated_gives_guidance_not_a_traceback(monkeypatch):
    credentials = cli_module.auth.Credentials(client_id="client-1", client_secret="s")
    monkeypatch.setattr(cli_module.auth, "load_credentials", lambda: credentials)

    def raise_not_authenticated(creds):
        raise cli_module.auth.NotAuthenticated("device authorisation request rejected")

    monkeypatch.setattr(cli_module.auth, "begin_device_authorisation", raise_not_authenticated)

    result = CliRunner().invoke(cli_module.cli, ["auth"])

    assert result.exit_code != 0
    assert "device authorisation request rejected" in result.output
    assert "Traceback" not in result.output


def test_network_failure_gives_guidance_not_a_traceback(monkeypatch):
    """Wifi off is the likeliest real failure; it must not print a traceback."""
    patch_backend(monkeypatch)

    def raise_connection_error(client):
        raise requests.ConnectionError("Failed to establish a new connection")

    monkeypatch.setattr(cli_module.appliances, "list_appliances", raise_connection_error)

    result = CliRunner().invoke(cli_module.cli, [])

    assert result.exit_code != 0
    assert "Could not reach the Home Connect API" in result.output
    assert "Traceback" not in result.output


def test_timeout_gives_guidance_not_a_traceback(monkeypatch):
    patch_backend(monkeypatch)

    def raise_timeout(client, appliance):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(cli_module.appliances, "fetch_state", raise_timeout)

    result = CliRunner().invoke(cli_module.cli, [])

    assert result.exit_code != 0
    assert "Could not reach the Home Connect API" in result.output
    assert "Traceback" not in result.output


class _OkResponse:
    status_code = 200

    def json(self):
        return {"data": {}}


class _FakeSession:
    def get(self, url, headers=None, timeout=None):
        return _OkResponse()


def test_access_token_is_fetched_once_per_run(monkeypatch):
    """Every mint rotates the refresh token and writes the Keychain.

    `Client.get` asks the provider on every request, so an unmemoised provider
    would rotate the stored credential once per GET — three times a run — each
    rotation a window in which a crash strands the credential.
    """
    credentials = cli_module.auth.Credentials(client_id="client-1")
    monkeypatch.setattr(cli_module.auth, "load_credentials", lambda: credentials)

    calls = []

    def counting_access_token(creds):
        calls.append(creds)
        return "tok"

    monkeypatch.setattr(cli_module.auth, "access_token", counting_access_token)

    real_client = cli_module.api.Client
    monkeypatch.setattr(
        cli_module.api,
        "Client",
        lambda **kwargs: real_client(session=_FakeSession(), **kwargs),
    )

    client = cli_module._build_client()
    client.get("/homeappliances")
    client.get("/homeappliances/X/status")

    assert len(calls) == 1


DEVICE_CODE_KWARGS = dict(
    device_code="dev-123",
    user_code="ABCD-EFGH",
    verification_uri="https://api.home-connect.com/security/oauth/device_verify",
    interval=0,
    expires_in=600,
)


def patch_auth_flow(monkeypatch, redeem):
    credentials = cli_module.auth.Credentials(client_id="client-1")
    monkeypatch.setattr(cli_module.auth, "load_credentials", lambda: credentials)
    monkeypatch.setattr(
        cli_module.auth,
        "begin_device_authorisation",
        lambda creds: cli_module.auth.DeviceCode(**DEVICE_CODE_KWARGS),
    )
    monkeypatch.setattr(cli_module.auth, "redeem_device_code", redeem)
    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: None)


def test_auth_loop_retries_while_approval_is_pending(monkeypatch):
    attempts = []

    def redeem(creds, device_code):
        attempts.append(device_code)
        if len(attempts) < 3:
            raise cli_module.auth.AuthorisationPending("waiting for approval")
        return "rt-1"

    patch_auth_flow(monkeypatch, redeem)

    result = CliRunner().invoke(cli_module.cli, ["auth"])

    assert result.exit_code == 0
    assert len(attempts) == 3
    assert "Authorised" in result.output


def test_auth_loop_stops_immediately_when_the_user_declines(monkeypatch):
    """A decline must fail at once, not poll on until the ten-minute deadline."""
    attempts = []

    def redeem(creds, device_code):
        attempts.append(device_code)
        raise cli_module.auth.NotAuthenticated(
            "Authorisation was declined in the browser."
        )

    patch_auth_flow(monkeypatch, redeem)

    result = CliRunner().invoke(cli_module.cli, ["auth"])

    assert result.exit_code != 0
    assert len(attempts) == 1
    assert "declined" in result.output
    assert "Traceback" not in result.output


def test_auth_loop_backs_off_when_told_to_slow_down(monkeypatch):
    slept = []
    attempts = []

    def redeem(creds, device_code):
        attempts.append(device_code)
        if len(attempts) == 1:
            raise cli_module.auth.AuthorisationPending("slow down", slow_down=True)
        return "rt-1"

    patch_auth_flow(monkeypatch, redeem)
    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: slept.append(seconds))

    result = CliRunner().invoke(cli_module.cli, ["auth"])

    assert result.exit_code == 0
    assert slept == [0, 5]


def test_history_reports_an_empty_log(monkeypatch, tmp_path):
    from homeconnect import store

    monkeypatch.setattr(store, "EVENTS_PATH", tmp_path / "absent.jsonl")

    result = CliRunner().invoke(cli_module.cli, ["history"])

    assert result.exit_code == 0
    assert "no events" in result.output.lower()


def test_history_json_is_parseable(monkeypatch, tmp_path):
    from homeconnect import store

    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"ts":"2026-08-26T19:00:00Z","ha":"dishwasher",'
        '"key":"OperationState","from":"Ready","to":"Run"}\n'
    )
    monkeypatch.setattr(store, "EVENTS_PATH", path)

    result = CliRunner().invoke(cli_module.cli, ["history", "--json"])

    payload = json.loads(result.output)
    assert payload["cycles"][0]["started"] == "2026-08-26T19:00:00Z"


def test_help_command_runs():
    result = CliRunner().invoke(cli_module.cli, ["help"])
    assert result.exit_code == 0
    assert "Quick Reference" in result.output


def test_unknown_command_suggests_a_similar_one():
    result = CliRunner().invoke(cli_module.cli, ["histry"])
    assert result.exit_code != 0
    assert "history" in result.output


def test_bare_help_lists_all_subcommands():
    result = CliRunner().invoke(cli_module.cli, ["--help"])

    assert result.exit_code == 0
    assert "Commands" in result.output
    for name in ("auth", "history", "help"):
        assert name in result.output


def test_bare_help_option_descriptions_are_aligned():
    import re

    result = CliRunner().invoke(cli_module.cli, ["--help"])
    stripped = re.sub(r"\x1b\[[0-9;]*m", "", result.output)

    option_lines = [
        line for line in stripped.splitlines()
        if line.strip().startswith("-")
    ]
    assert len(option_lines) >= 2

    from homeconnect.present import display_width

    columns = set()
    for line in option_lines:
        indent = len(line) - len(line.lstrip())
        remainder = line[indent:]
        # Find where the run of at least two spaces separating the option
        # name from its description begins.
        match = re.search(r"  +", remainder)
        assert match, f"no description column found in: {line!r}"
        columns.add(indent + display_width(remainder[: match.end()]))

    assert len(columns) == 1, f"option descriptions are ragged: {columns}"


def test_locked_keychain_gives_guidance_not_a_traceback(monkeypatch):
    """A locked, denied or reprompting Keychain must not print a traceback."""
    patch_backend(monkeypatch)

    def raise_keyring_error(creds):
        raise cli_module.auth.KeyringError("the Keychain is locked")

    # Driven through the token provider, as it is in life: the token is
    # fetched lazily when a request is first issued, so a test that never
    # issues one would pass for the wrong reason.
    monkeypatch.setattr(cli_module.auth, "access_token", raise_keyring_error)
    monkeypatch.setattr(
        cli_module.api, "Client", lambda **kwargs: kwargs["token_provider"]
    )
    monkeypatch.setattr(
        cli_module.appliances, "list_appliances", lambda provider: provider()
    )

    result = CliRunner().invoke(cli_module.cli, [])

    assert result.exit_code != 0
    assert "Keychain" in result.output
    assert "Traceback" not in result.output
