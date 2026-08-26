import json

import pytest
import requests

from homeconnect.api import (
    BASE_URL,
    Client,
    NoProgrammeActive,
    NotAuthorised,
    QuotaExceeded,
)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    """Records requests and replays queued responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        return self._responses.pop(0)


def test_get_returns_data_payload():
    session = FakeSession([FakeResponse(200, {"data": {"haId": "BOSCH-1"}})])
    client = Client(token_provider=lambda: "tok", session=session)

    assert client.get("/homeappliances/BOSCH-1") == {"haId": "BOSCH-1"}


def test_get_sends_bearer_token_and_json_accept():
    session = FakeSession([FakeResponse(200, {"data": {}})])
    client = Client(token_provider=lambda: "tok", session=session)

    client.get("/homeappliances")

    url, headers = session.calls[0]
    assert url == f"{BASE_URL}/homeappliances"
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Accept"] == "application/vnd.bsh.sdk.v1+json"


@pytest.mark.parametrize("status_code", [404, 409])
def test_no_program_active_is_its_own_error(status_code):
    """An idle appliance reports NoProgramActive on /programs/active.

    Observed live as **404** on 26 August 2026, though the documentation implies
    409. Both are accepted: the error key is what carries the meaning, and
    treating either as a fault is the classic bug in third-party clients.
    """
    session = FakeSession([
        FakeResponse(status_code, {"error": {"key": "SDK.Error.NoProgramActive"}}),
    ])
    client = Client(token_provider=lambda: "tok", session=session)

    with pytest.raises(NoProgrammeActive):
        client.get("/homeappliances/BOSCH-1/programs/active")


def test_401_raises_not_authorised():
    session = FakeSession([FakeResponse(401, {"error": {"key": "invalid_token"}})])
    client = Client(token_provider=lambda: "tok", session=session)

    with pytest.raises(NotAuthorised):
        client.get("/homeappliances")


def test_429_raises_quota_exceeded():
    session = FakeSession([FakeResponse(429, {"error": {"key": "429"}})])
    client = Client(token_provider=lambda: "tok", session=session)

    with pytest.raises(QuotaExceeded):
        client.get("/homeappliances")


def test_client_has_no_write_methods():
    """Read-only is a property of the code, not just of the token scope."""
    for verb in ("post", "put", "patch", "delete"):
        assert not hasattr(Client, verb)
