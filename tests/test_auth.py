import pytest

from homeconnect import auth

CREDENTIALS = auth.Credentials(client_id="client-1", client_secret="secret-1")


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


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
    monkeypatch.setattr(auth, "load_dotenv", lambda *a, **k: None)

    with pytest.raises(auth.MissingCredentials):
        auth.load_credentials()


def test_load_credentials_reads_environment(monkeypatch):
    monkeypatch.setenv("HOMECONNECT_CLIENT_ID", "client-1")
    monkeypatch.setenv("HOMECONNECT_CLIENT_SECRET", "secret-1")
    monkeypatch.setattr(auth, "load_dotenv", lambda *a, **k: None)

    credentials = auth.load_credentials()

    assert credentials.client_id == "client-1"
    assert credentials.client_secret == "secret-1"


def test_missing_credentials_message_never_leaks_the_secret(monkeypatch):
    monkeypatch.delenv("HOMECONNECT_CLIENT_ID", raising=False)
    monkeypatch.setenv("HOMECONNECT_CLIENT_SECRET", "super-secret-value")
    monkeypatch.setattr(auth, "load_dotenv", lambda *a, **k: None)

    with pytest.raises(auth.MissingCredentials) as caught:
        auth.load_credentials()

    assert "super-secret-value" not in str(caught.value)
