import pytest

from homeconnect import auth

CREDENTIALS = auth.Credentials(client_id="client-1", client_secret="secret-1")


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class UnreadableResponse:
    """A 200 whose body is not JSON at all — a truncated or proxied reply."""

    status_code = 200

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append((url, data))
        return self._responses.pop(0)


class FakeKeyring:
    def __init__(self, stored=None):
        self.store = dict(stored or {})

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password


def test_scope_is_read_only_and_can_enumerate():
    """Read-only is enforced by the token, so this constant must not drift.

    `Monitor` alone is not enough: /homeappliances requires `IdentifyAppliance`
    and returns 403 without it. Both scopes are read-only. Verified against the
    live API on 26 August 2026.
    """
    assert auth.SCOPE == "IdentifyAppliance Monitor"
    assert "Control" not in auth.SCOPE
    assert "Settings" not in auth.SCOPE


def test_as_form_includes_secret_when_present():
    assert CREDENTIALS.as_form() == {
        "client_id": "client-1",
        "client_secret": "secret-1",
    }


def test_as_form_omits_secret_when_absent():
    """A device-flow client need not have a secret; sending an empty one fails."""
    assert auth.Credentials(client_id="client-1").as_form() == {"client_id": "client-1"}


def test_begin_device_authorisation_returns_user_code():
    session = FakeSession([
        FakeResponse(200, {
            "device_code": "dev-123",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://api.home-connect.com/security/oauth/device_verify",
            "interval": 5,
            "expires_in": 600,
        })
    ])

    code = auth.begin_device_authorisation(CREDENTIALS, session=session)

    assert code.user_code == "ABCD-EFGH"
    assert code.device_code == "dev-123"
    assert code.interval == 5

    _url, data = session.calls[0]
    assert data["scope"] == "IdentifyAppliance Monitor"
    assert data["client_id"] == "client-1"
    assert data["client_secret"] == "secret-1"


def test_redeem_device_code_stores_refresh_token():
    session = FakeSession([
        FakeResponse(200, {"access_token": "at-1", "refresh_token": "rt-1"})
    ])
    fake_keyring = FakeKeyring()

    result = auth.redeem_device_code(
        CREDENTIALS, "dev-123", session=session, keyring_module=fake_keyring
    )

    assert result == "rt-1"
    assert fake_keyring.store[(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME)] == "rt-1"

    _url, data = session.calls[0]
    assert data["client_secret"] == "secret-1"
    assert data["grant_type"] == "device_code"


def test_access_token_refreshes_and_rotates_stored_token():
    """Home Connect rotates the refresh token; the new one must be written back."""
    session = FakeSession([
        FakeResponse(200, {"access_token": "at-2", "refresh_token": "rt-2"})
    ])
    fake_keyring = FakeKeyring({(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME): "rt-1"})

    token = auth.access_token(CREDENTIALS, session=session, keyring_module=fake_keyring)

    assert token == "at-2"
    assert fake_keyring.store[(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME)] == "rt-2"


def test_access_token_keeps_old_refresh_token_if_none_returned():
    """Never blank the stored credential on a response that omits a new one."""
    session = FakeSession([FakeResponse(200, {"access_token": "at-2"})])
    fake_keyring = FakeKeyring({(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME): "rt-1"})

    auth.access_token(CREDENTIALS, session=session, keyring_module=fake_keyring)

    assert fake_keyring.store[(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME)] == "rt-1"


def test_access_token_without_stored_token_raises():
    fake_keyring = FakeKeyring()

    with pytest.raises(auth.NotAuthenticated):
        auth.access_token(CREDENTIALS, session=FakeSession([]), keyring_module=fake_keyring)


def test_load_credentials_missing_client_id_raises(monkeypatch):
    monkeypatch.delenv("HOMECONNECT_CLIENT_ID", raising=False)
    monkeypatch.setattr(auth, "load_dotenv_bounded", lambda *a, **k: True)

    with pytest.raises(auth.MissingCredentials):
        auth.load_credentials()


def test_load_credentials_reads_environment(monkeypatch):
    monkeypatch.setenv("HOMECONNECT_CLIENT_ID", "client-1")
    monkeypatch.setenv("HOMECONNECT_CLIENT_SECRET", "secret-1")
    monkeypatch.setattr(auth, "load_dotenv_bounded", lambda *a, **k: True)

    credentials = auth.load_credentials()

    assert credentials.client_id == "client-1"
    assert credentials.client_secret == "secret-1"


def test_missing_credentials_message_never_leaks_the_secret(monkeypatch):
    monkeypatch.delenv("HOMECONNECT_CLIENT_ID", raising=False)
    monkeypatch.setenv("HOMECONNECT_CLIENT_SECRET", "super-secret-value")
    monkeypatch.setattr(auth, "load_dotenv_bounded", lambda *a, **k: True)

    with pytest.raises(auth.MissingCredentials) as caught:
        auth.load_credentials()

    assert "super-secret-value" not in str(caught.value)


def test_begin_device_authorisation_rejects_a_non_json_200():
    """A truncated or proxied 200 must be guidance, not a JSONDecodeError."""
    session = FakeSession([UnreadableResponse()])

    with pytest.raises(auth.NotAuthenticated) as caught:
        auth.begin_device_authorisation(CREDENTIALS, session=session)

    assert "not understood" in str(caught.value)


def test_begin_device_authorisation_rejects_a_200_missing_a_field():
    session = FakeSession([FakeResponse(200, {"user_code": "ABCD-EFGH"})])

    with pytest.raises(auth.NotAuthenticated) as caught:
        auth.begin_device_authorisation(CREDENTIALS, session=session)

    assert "device_code" in str(caught.value)


def test_access_token_rejects_a_non_json_200():
    fake_keyring = FakeKeyring({(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME): "rt-1"})

    with pytest.raises(auth.NotAuthenticated):
        auth.access_token(
            CREDENTIALS,
            session=FakeSession([UnreadableResponse()]),
            keyring_module=fake_keyring,
        )


def test_access_token_rejects_a_200_without_an_access_token():
    fake_keyring = FakeKeyring({(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME): "rt-1"})

    with pytest.raises(auth.NotAuthenticated) as caught:
        auth.access_token(
            CREDENTIALS,
            session=FakeSession([FakeResponse(200, {"token_type": "Bearer"})]),
            keyring_module=fake_keyring,
        )

    assert "access_token" in str(caught.value)


def test_unreadable_response_message_never_leaks_the_body():
    """The body can carry a token, so none of it may reach the terminal."""

    class LeakyResponse:
        status_code = 200

        def json(self):
            return {"unexpected": "super-secret-token-value"}

    with pytest.raises(auth.NotAuthenticated) as caught:
        auth.redeem_device_code(
            CREDENTIALS,
            "dev-123",
            session=FakeSession([LeakyResponse()]),
            keyring_module=FakeKeyring(),
        )

    assert "super-secret-token-value" not in str(caught.value)


def test_redeem_device_code_pending_raises_authorisation_pending():
    """Still waiting is not a failure — the loop must be able to tell."""
    session = FakeSession([FakeResponse(400, {"error": "authorization_pending"})])

    with pytest.raises(auth.AuthorisationPending) as caught:
        auth.redeem_device_code(
            CREDENTIALS, "dev-123", session=session, keyring_module=FakeKeyring()
        )

    assert caught.value.slow_down is False
    # Subclassing matters: anything catching the broader type must still catch.
    assert isinstance(caught.value, auth.NotAuthenticated)


def test_redeem_device_code_slow_down_asks_for_a_longer_interval():
    session = FakeSession([FakeResponse(400, {"error": "slow_down"})])

    with pytest.raises(auth.AuthorisationPending) as caught:
        auth.redeem_device_code(
            CREDENTIALS, "dev-123", session=session, keyring_module=FakeKeyring()
        )

    assert caught.value.slow_down is True


def test_redeem_device_code_access_denied_is_fatal_not_pending():
    """A decline must stop the loop, not be mistaken for "not approved yet"."""
    session = FakeSession([FakeResponse(400, {"error": "access_denied"})])

    with pytest.raises(auth.NotAuthenticated) as caught:
        auth.redeem_device_code(
            CREDENTIALS, "dev-123", session=session, keyring_module=FakeKeyring()
        )

    assert not isinstance(caught.value, auth.AuthorisationPending)
    assert "declined" in str(caught.value)


def test_redeem_device_code_expired_token_is_fatal_not_pending():
    session = FakeSession([FakeResponse(400, {"error": "expired_token"})])

    with pytest.raises(auth.NotAuthenticated) as caught:
        auth.redeem_device_code(
            CREDENTIALS, "dev-123", session=session, keyring_module=FakeKeyring()
        )

    assert not isinstance(caught.value, auth.AuthorisationPending)


# --- The refresh-token race ------------------------------------------------

def test_access_token_retries_once_with_a_freshly_stored_token():
    """The CLI and the recorder share one entry, and rotation invalidates.

    Whichever loses the race presents a token the other has just replaced.
    That is a lost race, not a lost credential, so it must not cost a browser
    re-consent.
    """
    keyring = FakeKeyring({(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME): "stale"})

    class RacingSession(FakeSession):
        def post(self, url, data=None, timeout=None):
            # The other process rotates the stored token between the two
            # attempts, exactly as it would in the real race.
            keyring.store[(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME)] = "fresh"
            return super().post(url, data, timeout)

    session = RacingSession([
        FakeResponse(400, {"error": "invalid_grant"}),
        FakeResponse(200, {"access_token": "at-2", "refresh_token": "rotated"}),
    ])

    token = auth.access_token(CREDENTIALS, session=session, keyring_module=keyring)

    assert token == "at-2"
    assert session.calls[1][1]["refresh_token"] == "fresh"
    assert keyring.store[(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME)] == "rotated"


def test_access_token_does_not_retry_the_very_same_token():
    """Nothing rotated it, so a second attempt would only waste a request."""
    keyring = FakeKeyring({(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME): "stale"})
    session = FakeSession([FakeResponse(400, {"error": "invalid_grant"})])

    with pytest.raises(auth.NotAuthenticated):
        auth.access_token(CREDENTIALS, session=session, keyring_module=keyring)

    assert len(session.calls) == 1


def test_rejection_message_suggests_retrying_before_re_consent():
    keyring = FakeKeyring({(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME): "stale"})
    session = FakeSession([FakeResponse(400, {"error": "invalid_grant"})])

    with pytest.raises(auth.NotAuthenticated) as raised:
        auth.access_token(CREDENTIALS, session=session, keyring_module=keyring)

    message = str(raised.value)
    assert "try again" in message.lower()
    assert "rotated" in message.lower()


def test_keyring_error_is_exposed_for_callers_to_catch():
    """Both the CLI and the recorder catch it by this name."""
    import keyring.errors

    assert auth.KeyringError is keyring.errors.KeyringError


# --- Bounded .env loading --------------------------------------------------
#
# The `.env` is a 1Password local-env file: a FIFO that yields its contents
# only once 1Password attaches as a writer. A plain blocking `load_dotenv()`
# hangs forever when 1Password is locked, and `load_credentials()` is the very
# first thing the recorder does — so it would hang at boot before writing a
# single log line, with `KeepAlive` unable to help because the process never
# exits. These tests pin the non-blocking behaviour.

import os
import threading
import time as time_module


def _attach_writer(path, text, delay=0.0):
    """Attach as a writer after `delay`, the way 1Password does."""
    def _writer():
        if delay:
            time_module.sleep(delay)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    thread = threading.Thread(target=_writer, daemon=True)
    thread.start()
    return thread


def test_read_env_text_reads_a_regular_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("HOMECONNECT_CLIENT_ID=abc\n", encoding="utf-8")
    assert auth._read_env_text(str(path)) == "HOMECONNECT_CLIENT_ID=abc\n"


def test_read_env_text_returns_none_when_absent(tmp_path):
    assert auth._read_env_text(str(tmp_path / "nope.env")) is None


def test_read_env_text_returns_none_for_an_empty_path():
    # find_dotenv() returns "" when it finds nothing; that must not be stat'ed.
    assert auth._read_env_text("") is None


def test_read_env_text_reads_a_fifo_once_a_writer_attaches(tmp_path):
    path = tmp_path / ".env"
    os.mkfifo(path)
    _attach_writer(path, "HOMECONNECT_CLIENT_ID=from-fifo\n", delay=0.2)

    assert auth._read_env_text(str(path), timeout=5.0) == "HOMECONNECT_CLIENT_ID=from-fifo\n"


def test_read_env_text_gives_up_when_no_writer_ever_attaches(tmp_path):
    """The locked-1Password case: must return, not hang."""
    path = tmp_path / ".env"
    os.mkfifo(path)

    started = time_module.monotonic()
    assert auth._read_env_text(str(path), timeout=0.3) is None
    assert time_module.monotonic() - started < 3.0


def test_dotenv_path_is_anchored_to_the_repository_not_the_cwd(tmp_path, monkeypatch):
    """Running the installed command from elsewhere must still find the .env."""
    anchored = tmp_path / ".env"
    anchored.write_text("HOMECONNECT_CLIENT_ID=anchored\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(auth, "_ANCHORED_ENV", anchored)
    monkeypatch.chdir(elsewhere)

    assert auth.dotenv_path() == str(anchored)


def test_dotenv_path_accepts_a_fifo(tmp_path, monkeypatch):
    """The mounted .env is a FIFO, for which isfile() is False."""
    anchored = tmp_path / ".env"
    os.mkfifo(anchored)
    monkeypatch.setattr(auth, "_ANCHORED_ENV", anchored)

    assert auth.dotenv_path() == str(anchored)


def test_dotenv_path_falls_back_to_a_cwd_search_when_the_repository_has_none(
    tmp_path, monkeypatch
):
    """Covers the package installed outside a checkout."""
    monkeypatch.setattr(auth, "_ANCHORED_ENV", tmp_path / "absent" / ".env")
    monkeypatch.setattr(auth, "find_dotenv", lambda *a, **k: "/from/cwd/.env")

    assert auth.dotenv_path() == "/from/cwd/.env"


def test_load_dotenv_bounded_populates_the_environment(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("HOMECONNECT_CLIENT_ID=from-file\n", encoding="utf-8")
    monkeypatch.setattr(auth, "dotenv_path", lambda: str(path))
    monkeypatch.delenv("HOMECONNECT_CLIENT_ID", raising=False)

    assert auth.load_dotenv_bounded() is True
    assert os.environ["HOMECONNECT_CLIENT_ID"] == "from-file"


def test_load_dotenv_bounded_does_not_override_an_existing_value(tmp_path, monkeypatch):
    """Matches load_dotenv()'s default: an explicit export still wins."""
    path = tmp_path / ".env"
    path.write_text("HOMECONNECT_CLIENT_ID=from-file\n", encoding="utf-8")
    monkeypatch.setattr(auth, "dotenv_path", lambda: str(path))
    monkeypatch.setenv("HOMECONNECT_CLIENT_ID", "from-environment")

    assert auth.load_dotenv_bounded() is True
    assert os.environ["HOMECONNECT_CLIENT_ID"] == "from-environment"


def test_load_dotenv_bounded_reports_failure_on_timeout(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    os.mkfifo(path)
    monkeypatch.setattr(auth, "dotenv_path", lambda: str(path))

    assert auth.load_dotenv_bounded(timeout=0.3) is False


def test_load_credentials_says_so_when_the_env_read_times_out(tmp_path, monkeypatch):
    """A locked 1Password must produce guidance, not a hang and not a bare
    'CLIENT_ID is not set' that sends you looking in the wrong place."""
    monkeypatch.delenv("HOMECONNECT_CLIENT_ID", raising=False)
    monkeypatch.setattr(auth, "load_dotenv_bounded", lambda *a, **k: False)

    with pytest.raises(auth.MissingCredentials) as caught:
        auth.load_credentials()

    assert "1Password" in str(caught.value)
    assert "Timed out" in str(caught.value)


def test_load_credentials_timeout_message_never_leaks_the_secret(monkeypatch):
    monkeypatch.delenv("HOMECONNECT_CLIENT_ID", raising=False)
    monkeypatch.setenv("HOMECONNECT_CLIENT_SECRET", "super-secret-value")
    monkeypatch.setattr(auth, "load_dotenv_bounded", lambda *a, **k: False)

    with pytest.raises(auth.MissingCredentials) as caught:
        auth.load_credentials()

    assert "super-secret-value" not in str(caught.value)
