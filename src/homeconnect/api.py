"""GET-only HTTP client for the Home Connect API.

This module deliberately exposes no write verb. The OAuth token is requested
with the `Monitor` scope, so the server would refuse a write in any case, but
the absence of a `post`/`put`/`delete` method here means a write cannot be
issued by accident either.
"""

from __future__ import annotations

from typing import Any, Callable

import requests

BASE_URL = "https://api.home-connect.com/api"

#: The API is versioned through the Accept header, not the URL.
ACCEPT = "application/vnd.bsh.sdk.v1+json"

_TIMEOUT = 30


class HomeConnectError(Exception):
    """Base class for every failure this client reports."""


class NotAuthorised(HomeConnectError):
    """The access token is missing, expired, or lacks the required scope."""


class QuotaExceeded(HomeConnectError):
    """The daily or per-minute call quota has been used up."""


class NoProgrammeActive(HomeConnectError):
    """No programme is running.

    The API signals this with a 409. It is the ordinary state of an idle
    appliance, so callers are expected to catch it rather than treat it as a
    fault. Mistaking this for an error is the classic bug in third-party
    Home Connect clients.
    """


class ApplianceOffline(HomeConnectError):
    """The appliance is registered but not currently reachable."""


def _error_key(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("key", ""))
    return ""


class Client:
    """A read-only Home Connect API client."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        session: Any | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self._token_provider = token_provider
        self._session = session if session is not None else requests.Session()
        self._base_url = base_url

    def get(self, path: str) -> Any:
        """Fetch `path` and return its `data` payload.

        Raises one of the module's exceptions on any non-200 response.
        """
        response = self._session.get(
            f"{self._base_url}{path}",
            headers={
                "Authorization": f"Bearer {self._token_provider()}",
                "Accept": ACCEPT,
            },
            timeout=_TIMEOUT,
        )

        if response.status_code == 200:
            return response.json().get("data")

        payload = response.json()
        key = _error_key(payload)

        # Checked before the status code: an idle appliance was observed
        # returning 404 here where the documentation implies 409, so the error
        # key is the reliable signal, not the status.
        if "NoProgramActive" in key or "NoProgramSelected" in key:
            raise NoProgrammeActive(key)

        if response.status_code == 401:
            raise NotAuthorised(key or "unauthorised")
        if response.status_code == 429:
            raise QuotaExceeded(key or "quota exceeded")
        if response.status_code == 409:
            if "Offline" in key or "NotConnected" in key:
                raise ApplianceOffline(key)
            raise HomeConnectError(key or "conflict")

        raise HomeConnectError(f"HTTP {response.status_code}: {key}")
