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
#: spec at https://api-docs.home-connect.com/ . Both scopes are read-only.
SCOPE = "IdentifyAppliance Monitor"

KEYRING_SERVICE = "home-connect"
KEYRING_USERNAME = "refresh_token"

_TIMEOUT = 30


class MissingCredentials(Exception):
    """The client ID is not present in the environment."""


class NotAuthenticated(Exception):
    """No usable refresh token; the one-off consent has not been done."""


class AuthorisationPending(NotAuthenticated):
    """The device code has been issued but nobody has approved it yet.

    Deliberately a subclass of `NotAuthenticated`, so anything catching the
    broader type keeps behaving exactly as it did. Only the approval loop cares
    about the distinction: this one means "keep waiting", where a plain
    `NotAuthenticated` from the same call means "stop, this will never work".
    """

    def __init__(self, message: str, slow_down: bool = False) -> None:
        super().__init__(message)
        #: The server asked us to poll less often (OAuth `slow_down`).
        self.slow_down = slow_down


#: OAuth error codes we are willing to repeat back to the user. Anything else
#: from the body is dropped rather than echoed: a response body can carry a
#: token, and none of it is trustworthy enough to print.
_SAFE_ERROR_CODES = frozenset({
    "access_denied",
    "authorization_pending",
    "expired_token",
    "invalid_client",
    "invalid_grant",
    "invalid_request",
    "invalid_scope",
    "slow_down",
    "unauthorized_client",
    "unsupported_grant_type",
})

#: Pending, not fatal: keep polling until the deadline.
_PENDING_ERROR_CODES = frozenset({"authorization_pending", "slow_down"})

_UNREADABLE = (
    "The authorisation server's response was not understood. "
    "Try again, and run `homeconnect auth` if it persists."
)


def _decode(response: Any) -> Any:
    """Return the decoded JSON body, or fail with guidance.

    A truncated, empty or non-JSON 200 is a vendor failure, not a bug here, and
    it must not surface as a `JSONDecodeError` traceback. `ValueError` is caught
    rather than a requests-internal type: `JSONDecodeError` subclasses it on
    every JSON backend. The body is never quoted in the message — it can carry
    a token.
    """
    try:
        return response.json()
    except ValueError as exc:
        raise NotAuthenticated(_UNREADABLE) from exc


def _field(payload: Any, name: str) -> Any:
    """Return `payload[name]`, or fail with guidance rather than a `KeyError`.

    Only the field *name* — ours, not the server's data — reaches the message.
    """
    try:
        return payload[name]
    except (KeyError, TypeError) as exc:
        raise NotAuthenticated(f"{_UNREADABLE} (no `{name}` field)") from exc


def _error_code(response: Any) -> str:
    """The OAuth `error` code from a failed response, if it is one we know.

    Returns an empty string when the body is unreadable or the code is
    unrecognised, so nothing unvetted from the body can reach the terminal.
    """
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    code = payload.get("error")
    return code if isinstance(code, str) and code in _SAFE_ERROR_CODES else ""


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

    payload = _decode(response)
    return DeviceCode(
        device_code=_field(payload, "device_code"),
        user_code=_field(payload, "user_code"),
        verification_uri=_field(payload, "verification_uri"),
        interval=int(payload.get("interval", 5)),
        expires_in=int(payload.get("expires_in", 600)),
    )


def redeem_device_code(
    credentials: Credentials,
    device_code: str,
    session: Any | None = None,
    keyring_module: Any | None = None,
) -> str:
    """Exchange an approved device code for tokens; store and return the refresh token.

    Raises `AuthorisationPending` while the person at the browser has neither
    approved nor declined, and a plain `NotAuthenticated` for everything else —
    a decline or an expired code is fatal and must stop the polling loop rather
    than let it run to its deadline in silence.
    """
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
        code = _error_code(response)
        if code in _PENDING_ERROR_CODES:
            raise AuthorisationPending(
                "waiting for approval", slow_down=code == "slow_down"
            )
        if code == "access_denied":
            raise NotAuthenticated("Authorisation was declined in the browser.")
        if code == "expired_token":
            raise NotAuthenticated("The code expired before it was approved.")
        detail = f" ({code})" if code else ""
        raise NotAuthenticated(
            f"token exchange failed: HTTP {response.status_code}{detail}"
        )

    refresh_token = _field(_decode(response), "refresh_token")
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

    payload = _decode(response)
    if isinstance(payload, dict) and payload.get("refresh_token"):
        store.set_password(KEYRING_SERVICE, KEYRING_USERNAME, payload["refresh_token"])
    return _field(payload, "access_token")
