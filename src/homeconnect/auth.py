"""OAuth2 device-flow authentication, with the refresh token in the Keychain.

The token is requested with the `Monitor` scope and nothing else. Home Connect
defines `Monitor` as read access to everything except images and settings, so a
token issued here is structurally incapable of changing the appliance.

Deliberately absent: the `Settings` scope. There is no read-only settings
scope — `Settings` grants read *and* modify — and reading PowerState is not
worth a token that can change it.

The client secret is a real secret. It is read from the 1Password-mounted
`.env`, held only in memory, and never logged, printed, or formatted into an
exception message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests
from dotenv import load_dotenv

DEVICE_AUTHORISATION_URL = (
    "https://api.home-connect.com/security/oauth/device_authorization"
)
TOKEN_URL = "https://api.home-connect.com/security/oauth/token"

#: Read-only. Never widen this without an explicit, separate decision.
#:
#: Two scopes, not one. `Monitor` grants reading status and programmes on an
#: appliance whose haId is already known; *listing* appliances is a separate
#: right, `IdentifyAppliance`. With `Monitor` alone, GET /homeappliances returns
#: 403 — confirmed against the live API, and pinned per-endpoint in the OpenAPI
#: spec at docs/hcsdk-production.yaml. Both scopes are read-only.
SCOPE = "IdentifyAppliance Monitor"

KEYRING_SERVICE = "home-connect"
KEYRING_USERNAME = "refresh_token"

_TIMEOUT = 30


class MissingCredentials(Exception):
    """The client ID is not present in the environment."""


class NotAuthenticated(Exception):
    """No usable refresh token; the one-off consent has not been done."""


@dataclass(frozen=True)
class Credentials:
    """Application credentials for the registered Home Connect client."""

    client_id: str
    client_secret: str | None = None

    def as_form(self) -> dict[str, str]:
        """The client fields every OAuth request to Home Connect carries.

        The secret is omitted rather than sent empty when absent: a device-flow
        client need not be confidential, and an empty `client_secret` is
        rejected where a missing one is accepted.
        """
        form = {"client_id": self.client_id}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        return form

    def __repr__(self) -> str:  # pragma: no cover - defensive
        """Never render the secret, so it cannot reach a log or a traceback."""
        held = "set" if self.client_secret else "unset"
        return f"Credentials(client_id={self.client_id!r}, client_secret=<{held}>)"


@dataclass(frozen=True)
class DeviceCode:
    """The codes returned when device authorisation begins."""

    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


def load_credentials() -> Credentials:
    """Read the application credentials from the 1Password-mounted `.env`."""
    load_dotenv()
    client_id = os.environ.get("HOMECONNECT_CLIENT_ID")
    if not client_id:
        raise MissingCredentials(
            "HOMECONNECT_CLIENT_ID is not set. Mount the 1Password 'Home Connect' "
            "environment to .env in this directory."
        )
    return Credentials(
        client_id=client_id,
        client_secret=os.environ.get("HOMECONNECT_CLIENT_SECRET") or None,
    )


def _session(session: Any | None) -> Any:
    return session if session is not None else requests.Session()


def _keyring(keyring_module: Any | None) -> Any:
    if keyring_module is not None:
        return keyring_module
    import keyring

    return keyring


def begin_device_authorisation(
    credentials: Credentials, session: Any | None = None
) -> DeviceCode:
    """Ask Home Connect for a user code to show the person at the keyboard."""
    response = _session(session).post(
        DEVICE_AUTHORISATION_URL,
        data={**credentials.as_form(), "scope": SCOPE},
        timeout=_TIMEOUT,
    )
    if response.status_code != 200:
        raise NotAuthenticated(
            f"device authorisation failed: HTTP {response.status_code}"
        )

    payload = response.json()
    return DeviceCode(
        device_code=payload["device_code"],
        user_code=payload["user_code"],
        verification_uri=payload["verification_uri"],
        interval=int(payload.get("interval", 5)),
        expires_in=int(payload.get("expires_in", 600)),
    )


def redeem_device_code(
    credentials: Credentials,
    device_code: str,
    session: Any | None = None,
    keyring_module: Any | None = None,
) -> str:
    """Exchange an approved device code for tokens; store and return the refresh token."""
    response = _session(session).post(
        TOKEN_URL,
        data={
            **credentials.as_form(),
            "grant_type": "device_code",
            "device_code": device_code,
        },
        timeout=_TIMEOUT,
    )
    if response.status_code != 200:
        raise NotAuthenticated(f"token exchange failed: HTTP {response.status_code}")

    refresh_token = response.json()["refresh_token"]
    _keyring(keyring_module).set_password(
        KEYRING_SERVICE, KEYRING_USERNAME, refresh_token
    )
    return refresh_token


def access_token(
    credentials: Credentials,
    session: Any | None = None,
    keyring_module: Any | None = None,
) -> str:
    """Return a fresh access token, rotating the stored refresh token.

    Home Connect issues a new refresh token on every refresh and invalidates the
    old one, so the replacement must be written back or the next run will fail.
    A response that omits one leaves the stored token untouched — blanking it
    would strand the tool with no way back except re-consent.
    """
    store = _keyring(keyring_module)
    refresh_token = store.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    if not refresh_token:
        raise NotAuthenticated("Not authenticated. Run `homeconnect auth` first.")

    response = _session(session).post(
        TOKEN_URL,
        data={
            **credentials.as_form(),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=_TIMEOUT,
    )
    if response.status_code != 200:
        raise NotAuthenticated(
            "Stored credential rejected. Run `homeconnect auth` again."
        )

    payload = response.json()
    if payload.get("refresh_token"):
        store.set_password(KEYRING_SERVICE, KEYRING_USERNAME, payload["refresh_token"])
    return payload["access_token"]
