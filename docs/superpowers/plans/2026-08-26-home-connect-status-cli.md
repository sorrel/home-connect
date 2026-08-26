# Home Connect Status CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only CLI that answers "what is the dishwasher doing, and does it need anything?" in one line, generalising to other Home Connect appliances.

**Architecture:** A GET-only HTTP client over the Home Connect API, authenticated with an OAuth2 `Monitor`-scoped token obtained once via device flow and refreshed from the macOS Keychain. Appliance data is fetched into plain dataclasses that know nothing about formatting; a registry of renderers keyed on appliance type turns them into text, with a generic fallback for unrecognised appliances.

**Tech Stack:** Python >= 3.12, uv, Click (CLI), requests (HTTP), keyring (Keychain), python-dotenv (1Password-mounted `.env`), pytest.

**Spec:** `docs/superpowers/specs/2026-08-26-home-connect-status-cli-design.md`

**Status (26 August 2026):** Nothing implemented yet — no code, no git repo.
Execution mode agreed: **subagent-driven** (one fresh subagent per task, review
between tasks). Tasks 1 and 2 are unblocked and can start immediately. The Device Flow
application is registered: **`hc-machine-status`**, Access: Private, OAuth Flow:
Device Flow, One Time Token Mode off. Task 3 therefore needs only the client ID
mounted to `.env` via the 1Password "Home Connect" environment — do not register anything again, and never write the client ID into a file
that git tracks.
Task 4 is the pivot and must not be run without the maintainer's explicit
go-ahead.


## Global Constraints

- **Read-only, enforced twice.** OAuth scope is `Monitor` and nothing else. No POST, PUT, PATCH or DELETE call may exist anywhere in the codebase.
- **Never request the `Settings` scope.** It grants read *and* modify; there is no read-only settings scope. Settings are out of scope entirely.
- **No live API calls in the test suite.** Tests run against recorded JSON fixtures. Task 4 is the single, explicitly gated exception and is run by hand, never by pytest.
- **Tests must pass on a fresh clone.** Never assert that a gitignored runtime directory exists.
- **British English throughout** — code, comments, output, docs, commit messages. `colour`, `authorise`, `programme` (the household noun) — but keep API key names verbatim as the API spells them (`ProgramProgress`, `Dishcare.Dishwasher.Program.Eco50`).
- **Never commit to `main`.** Feature branches only.
- **Terminal alignment uses `display_width()`, never `len()`.** Warning glyphs are double-width.
- **Python >= 3.12.** Run everything via `uv run`.
- **No credentials, real email addresses, hostnames, developer-portal user IDs, or `/Users/<name>/…` paths in committed file contents.** This repository is intended to become public.
- **Neither the client ID nor the client secret is committed.** The registered
  application is a *confidential* client: it has a real client secret, which must
  never be written to a tracked file, echoed in output, or formatted into an
  exception message. The client ID is less sensitive — it names the consent screen
  and carries the quota rather than granting access — but it stays out of the repo
  too. Both live only in the 1Password-mounted `.env`, which is gitignored.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `README.md`, `CLAUDE.md`
- Create: `src/homeconnect/__init__.py`
- Create: `.github/workflows/tests.yml`
- Create: `tests/test_structure.py`
- Move: `../hcsdk-production.yaml` → `docs/hcsdk-production.yaml`

**Interfaces:**
- Consumes: nothing.
- Produces: an installable package `homeconnect` with console script `homeconnect`, and a green test suite.

- [ ] **Step 1: Initialise the repo on a feature branch**

```bash
# Run from the repository root.
git init
git checkout -b feature/scaffold
git mv ../hcsdk-production.yaml docs/hcsdk-production.yaml 2>/dev/null || mv ../hcsdk-production.yaml docs/hcsdk-production.yaml
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "home-connect"
version = "0.1.0"
description = "Read-only status CLI for Home Connect appliances"
requires-python = ">=3.12"
license = "MIT"
dependencies = [
    "click>=8.0.0",
    "requests>=2.28.0",
    "keyring>=25.0.0",
    "python-dotenv>=1.0.0",
]

[project.scripts]
homeconnect = "homeconnect.cli:cli"

[dependency-groups]
dev = ["pytest>=8.0"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.hatch.build.targets.wheel]
packages = ["src/homeconnect"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 3: Write `.gitignore`**

```
.venv/
__pycache__/
*.pyc
.env
.pytest_cache/
```

- [ ] **Step 4: Copy the CI workflow from a sibling, verbatim except the comment**

```bash
mkdir -p .github/workflows
cp ../econsult-window-monitor/.github/workflows/tests.yml .github/workflows/tests.yml
```

Then replace only the leading comment block with:

```yaml
# Runs the test suite on every push to main and every pull request.
#
# The suite is fully mocked — it never calls the Home Connect API. Keep it that
# way: CI runs on every push, and the API quota is roughly 1000 calls a day.
```

Delete the `uv sync --dev` comment about the `capture` extra, which does not apply here. Leave the pinned action SHAs exactly as they are.

- [ ] **Step 5: Write the failing structure test**

`tests/test_structure.py`:

```python
"""The package must be importable and expose its version."""
from pathlib import Path

import homeconnect


def test_package_imports():
    assert homeconnect.__version__ == "0.1.0"


def test_src_layout():
    """The shippable package lives under src/, per the workspace convention."""
    root = Path(__file__).resolve().parent.parent
    assert (root / "src" / "homeconnect" / "__init__.py").is_file()
```

- [ ] **Step 6: Run the test to verify it fails**

Run: `uv run pytest tests/test_structure.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'homeconnect'`

- [ ] **Step 7: Create the package**

`src/homeconnect/__init__.py`:

```python
"""Read-only status CLI for Home Connect appliances."""

__version__ = "0.1.0"
```

- [ ] **Step 8: Run the test to verify it passes**

Run: `uv run pytest -q`
Expected: 2 passed

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "chore: scaffold read-only Home Connect status CLI"
```

---

### Task 2: GET-only API client

**Files:**
- Create: `src/homeconnect/api.py`
- Create: `tests/test_api.py`
- Create: `tests/fixtures/__init__.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `BASE_URL: str`
  - `class HomeConnectError(Exception)`
  - `class NotAuthorised(HomeConnectError)`
  - `class QuotaExceeded(HomeConnectError)`
  - `class NoProgrammeActive(HomeConnectError)`
  - `class Client` with `__init__(self, token_provider: Callable[[], str], session: requests.Session | None = None, base_url: str = BASE_URL)` and `get(self, path: str) -> dict` returning the parsed `data` payload.

**Design note:** `token_provider` is a zero-argument callable returning an access token, not a token string. That keeps `api.py` ignorant of Keychain and refresh logic, and lets tests pass `lambda: "test-token"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_api.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'homeconnect.api'`

- [ ] **Step 3: Write `src/homeconnect/api.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/homeconnect/api.py tests/test_api.py
git commit -m "feat: add GET-only Home Connect API client"
```

---

### Task 3: Device-flow authentication with Keychain storage

**Files:**
- Create: `src/homeconnect/auth.py`
- Create: `tests/test_auth.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `SCOPE: str` (the literal `"IdentifyAppliance Monitor"`)
  - `KEYRING_SERVICE: str`, `KEYRING_USERNAME: str`
  - `@dataclass(frozen=True) Credentials` with `client_id: str`, `client_secret: str | None = None`, and method `as_form(self) -> dict[str, str]`
  - `@dataclass(frozen=True) DeviceCode` with `device_code: str`, `user_code: str`, `verification_uri: str`, `interval: int`, `expires_in: int`
  - `class MissingCredentials(Exception)`
  - `class NotAuthenticated(Exception)`
  - `def load_credentials() -> Credentials`
  - `def begin_device_authorisation(credentials: Credentials, session=None) -> DeviceCode`
  - `def redeem_device_code(credentials: Credentials, device_code: str, session=None, keyring_module=None) -> str`
  - `def access_token(credentials: Credentials, session=None, keyring_module=None) -> str`

**PRECONDITION — the application registration must be right before this task runs:**

- The registered application is **`hc-machine-status`** (Access: Private, OAuth
  Flow: Device Flow, One Time Token Mode off). Do not register
  anything again.
- Do **not** use the auto-generated *API Web Client* from the portal front page.
  Its redirect URI is pinned to `https://apiclient.home-connect.com/o2c.html`,
  so the OAuth redirect lands on a Bosch web page and a CLI can never complete
  the flow with it.
- This application **has a client secret**, so it is a confidential client and
  every token request must carry `client_secret`. The code nonetheless treats
  the secret as optional, because a device-flow client is not obliged to have
  one and the tool should not break if the application is ever replaced by one
  without.

**Security note — the secret is genuinely secret,** unlike the client ID. It
must never be written to a tracked file, echoed in output, or included in an
exception message. Note that `MissingCredentials` below names the *variable*
that is absent and never its value, and no code path formats credentials into a
message.

**Design note:** `keyring` and the HTTP session are injected so tests never
touch the real Keychain or the network.

**Operational note — `.env` is a named pipe, not a file.** 1Password mounts it
as a FIFO (`prw-------`), which is why the credentials never touch the disk. The
consequence is that reading it *blocks* rather than failing when nothing is
writing: if 1Password is not running, or the environment has been unmounted,
`load_dotenv()` hangs indefinitely with no error. Tests must therefore never
call the real `load_dotenv` — the tests above monkeypatch it for exactly this
reason, and a test that forgets will hang CI rather than fail it.

- [ ] **Step 1: Write the failing tests**

`tests/test_auth.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'homeconnect.auth'`

- [ ] **Step 3: Write `src/homeconnect/auth.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auth.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/homeconnect/auth.py tests/test_auth.py
git commit -m "feat: add device-flow auth with Keychain-stored refresh token"
```

---

### Task 4: RESOLVED — no work required

The live probe this task existed to run was carried out on 26 August 2026,
before implementation began. **Do not repeat it and do not make live API calls.**
Full results are in the spec's "Findings from the live probe" section. The two
that change the code:

1. **The consumable and care values are not status keys.** Asking for each
   individually returns `409 SDK.Error.UnsupportedStatus`, whilst
   `BSH.Common.Status.OperationState` asked the same way returns 200. They exist
   only as SSE events, and the stream was confirmed to send nothing on connect —
   it pushes on change only. An on-demand command therefore cannot report salt,
   rinse aid, or machine care.
   **Decision taken:** build the CLI without them. A listener is a possible later
   phase with its own spec. Do not add one as part of this plan.
2. **`/programs/active` returns 404 when idle**, not the documented 409. Task 2
   already matches on the error key rather than the status code.

- [ ] **Step 1: Create the fixtures from the recorded live responses**

`tests/fixtures/appliances.json` — one connected dishwasher, haId anonymised:

```json
{
  "data": {
    "homeappliances": [
      {
        "haId": "BOSCH-SMV000000-000000000000",
        "name": "Dishwasher",
        "type": "Dishwasher",
        "brand": "Bosch",
        "vib": "SMV000000",
        "enumber": "SMV000000/00",
        "connected": true
      }
    ]
  }
}
```

`tests/fixtures/status_idle.json` — the complete `/status` payload observed on
an idle machine. Note there are only four keys; this is everything a poll sees:

```json
{
  "data": {
    "status": [
      {"key": "BSH.Common.Status.RemoteControlStartAllowed", "value": true},
      {"key": "BSH.Common.Status.RemoteControlActive", "value": true},
      {"key": "BSH.Common.Status.DoorState",
       "value": "BSH.Common.EnumType.DoorState.Open"},
      {"key": "BSH.Common.Status.OperationState",
       "value": "BSH.Common.EnumType.OperationState.Ready"}
    ]
  }
}
```

`tests/fixtures/programs_active_idle.json` — the 404 body when nothing runs:

```json
{"error": {"key": "SDK.Error.NoProgramActive", "description": "No program active"}}
```

- [ ] **Step 2: Commit**

```bash
git add tests/fixtures
git commit -m "test: add fixtures recorded from the live appliance"
```

---

### Task 5: Appliance models and fetching

**Files:**
- Create: `src/homeconnect/appliances.py`
- Create: `tests/test_appliances.py`

**Interfaces:**
- Consumes: `homeconnect.api.Client`, `homeconnect.api.NoProgrammeActive`.
- Produces:
  - `@dataclass(frozen=True) Appliance` with `ha_id: str`, `name: str`, `type: str`, `brand: str`, `vib: str`, `enumber: str`, `connected: bool`
  - `@dataclass(frozen=True) Programme` with `key: str`, `options: dict[str, Any]`
  - `@dataclass(frozen=True) ApplianceState` with `appliance: Appliance`, `status: dict[str, Any]`, `programme: Programme | None`
  - `def list_appliances(client) -> list[Appliance]`
  - `def fetch_state(client, appliance: Appliance) -> ApplianceState`

**Design note:** `status` is a flat `{key: value}` dict built from the API's
`[{"key": ..., "value": ...}]` array. Keeping raw API key names as the dict keys
means `--verbose` can print them unchanged and new keys need no code change.

- [ ] **Step 1: Write the failing tests**

`tests/test_appliances.py`:

```python
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


def test_fetch_state_skips_calls_for_offline_appliance():
    offline = appliances.Appliance(
        ha_id="BOSCH-TEST-0002", name="Oven", type="Oven", brand="Bosch",
        vib="X", enumber="X/01", connected=False,
    )
    client = FakeClient({})  # any call would raise KeyError

    state = appliances.fetch_state(client, offline)

    assert state.status == {}
    assert state.programme is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_appliances.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'homeconnect.appliances'`

- [ ] **Step 3: Write `src/homeconnect/appliances.py`**

```python
"""Fetch appliance records and state. Knows nothing about formatting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .api import NoProgrammeActive


@dataclass(frozen=True)
class Appliance:
    """A single registered home appliance."""

    ha_id: str
    name: str
    type: str
    brand: str
    vib: str
    enumber: str
    connected: bool


@dataclass(frozen=True)
class Programme:
    """The programme currently running, with its options."""

    key: str
    options: dict[str, Any]


@dataclass(frozen=True)
class ApplianceState:
    """Everything we could read about one appliance, in one go."""

    appliance: Appliance
    status: dict[str, Any]
    programme: Programme | None


def _flatten(entries: Any) -> dict[str, Any]:
    """Turn the API's [{key, value}, …] arrays into a plain dict.

    Raw API key names are kept as the dict keys so `--verbose` can show them
    unchanged, and so a key we have never seen before still gets displayed.
    """
    if not isinstance(entries, list):
        return {}
    return {entry["key"]: entry.get("value") for entry in entries if "key" in entry}


def list_appliances(client: Any) -> list[Appliance]:
    """Enumerate every appliance on the account."""
    payload = client.get("/homeappliances") or {}
    return [
        Appliance(
            ha_id=record["haId"],
            name=record.get("name", record["haId"]),
            type=record.get("type", "Unknown"),
            brand=record.get("brand", ""),
            vib=record.get("vib", ""),
            enumber=record.get("enumber", ""),
            connected=bool(record.get("connected", False)),
        )
        for record in payload.get("homeappliances", [])
    ]


def fetch_state(client: Any, appliance: Appliance) -> ApplianceState:
    """Read status and any active programme for one appliance.

    An offline appliance is reported as such without further calls — there is
    nothing to read and each attempt would spend quota to no purpose.
    """
    if not appliance.connected:
        return ApplianceState(appliance=appliance, status={}, programme=None)

    status_payload = client.get(f"/homeappliances/{appliance.ha_id}/status") or {}
    status = _flatten(status_payload.get("status"))

    try:
        active = client.get(f"/homeappliances/{appliance.ha_id}/programs/active") or {}
        programme = Programme(
            key=active.get("key", ""),
            options=_flatten(active.get("options")),
        )
    except NoProgrammeActive:
        programme = None

    return ApplianceState(appliance=appliance, status=status, programme=programme)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_appliances.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/homeconnect/appliances.py tests/test_appliances.py
git commit -m "feat: add appliance models and state fetching"
```

---

### Task 6: Presentation

**Files:**
- Create: `src/homeconnect/present.py`
- Create: `tests/test_present.py`

**Interfaces:**
- Consumes: `homeconnect.appliances.ApplianceState`, `Appliance`, `Programme`.
- Produces:
  - `def display_width(text: str) -> int`
  - `def render(state: ApplianceState, verbose: bool = False) -> str`
  - `RENDERERS: dict[str, Callable[[ApplianceState, bool], str]]`

**Design note:** `render` dispatches on `state.appliance.type`, falling back to a
generic renderer that shows only `BSH.Common.*` keys. Adding an oven later means
adding one function and one registry entry, touching nothing else.

- [ ] **Step 1: Write the failing tests**

`tests/test_present.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_present.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'homeconnect.present'`

- [ ] **Step 3: Write `src/homeconnect/present.py`**

```python
"""Turn appliance state into text.

Renderers are registered by appliance type. Anything unrecognised falls back to
a generic renderer that shows only the `BSH.Common.*` keys every appliance
shares, so a newly added oven degrades to a useful summary rather than crashing.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Callable

from .appliances import ApplianceState

#: Human labels for the programme keys we expect to meet.
PROGRAMME_NAMES = {
    "Dishcare.Dishwasher.Program.Auto1": "Auto 35–45",
    "Dishcare.Dishwasher.Program.Auto2": "Auto 45–65",
    "Dishcare.Dishwasher.Program.Auto3": "Auto 65–75",
    "Dishcare.Dishwasher.Program.Eco50": "Eco 50",
    "Dishcare.Dishwasher.Program.Quick45": "Quick 45",
    "Dishcare.Dishwasher.Program.Intensiv70": "Intensive 70",
    "Dishcare.Dishwasher.Program.NightWash": "Night wash",
    "Dishcare.Dishwasher.Program.Glas40": "Glass 40",
    "Dishcare.Dishwasher.Program.PreRinse": "Pre-rinse",
    "Dishcare.Dishwasher.Program.MachineCare": "Machine care",
}

# Deliberately absent: salt, rinse aid and machine-care reporting. Those values
# are not status keys — the API returns SDK.Error.UnsupportedStatus for each —
# and exist only as SSE events on a change-only stream. Reporting them needs a
# process that is already listening, which this tool is not. See the spec's
# findings section before attempting to add them here.


def display_width(text: str) -> int:
    """Width of `text` in terminal columns.

    `len()` is wrong here: warning glyphs and most emoji occupy two columns but
    count as one character, which silently misaligns every column after them.
    """
    width = 0
    for char in text:
        if unicodedata.east_asian_width(char) in ("W", "F") or ord(char) > 0x1F300:
            width += 2
        elif char == "⚠":
            width += 2
        else:
            width += 1
    return width


def _enum_tail(value: Any) -> str:
    """`BSH.Common.EnumType.OperationState.Run` -> `Run`."""
    if isinstance(value, str) and "." in value:
        return value.rsplit(".", 1)[-1]
    return str(value)


def _programme_name(key: str) -> str:
    return PROGRAMME_NAMES.get(key, _enum_tail(key))


def _remaining(state: ApplianceState) -> str | None:
    if state.programme is None:
        return None
    seconds = state.programme.options.get("BSH.Common.Option.RemainingProgramTime")
    if not isinstance(seconds, int):
        return None
    return f"{seconds // 60} min left"


def _render_dishwasher(state: ApplianceState, verbose: bool) -> str:
    operation = _enum_tail(
        state.status.get("BSH.Common.Status.OperationState", "Unknown")
    )

    if verbose:
        return _render_verbose(state)

    parts = [state.appliance.name]

    if operation == "Run" and state.programme is not None:
        parts.append(f"running {_programme_name(state.programme.key)}")
        remaining = _remaining(state)
        if remaining:
            parts.append(remaining)
    elif operation == "Finished":
        parts.append("finished — ready to empty")
    elif operation in ("Inactive", "Ready"):
        parts.append("idle")
    else:
        parts.append(operation.lower())

    # Worth surfacing: a door left open is the usual reason a machine that
    # looks ready has not actually started.
    if _enum_tail(state.status.get("BSH.Common.Status.DoorState", "")) == "Open":
        parts.append("door open")

    return "  ".join(parts)


def _render_generic(state: ApplianceState, verbose: bool) -> str:
    """Fallback for appliance types we have no dedicated renderer for."""
    if verbose:
        return _render_verbose(state)

    operation = _enum_tail(
        state.status.get("BSH.Common.Status.OperationState", "Unknown")
    )
    return f"{state.appliance.name}  {operation}"


def _render_verbose(state: ApplianceState) -> str:
    """Every key we hold, with its raw API name, for debugging."""
    appliance = state.appliance
    lines = [f"{appliance.name} ({appliance.brand} {appliance.vib})"]

    keys = list(state.status)
    if state.programme is not None:
        keys.extend(state.programme.options)
    label_width = max([display_width(key) for key in keys] + [len("Programme")])

    if state.programme is not None:
        lines.append(f"  {'Programme'.ljust(label_width)}  {state.programme.key}")
        for key, value in sorted(state.programme.options.items()):
            padding = " " * (label_width - display_width(key))
            lines.append(f"  {key}{padding}  {value}")

    for key, value in sorted(state.status.items()):
        padding = " " * (label_width - display_width(key))
        lines.append(f"  {key}{padding}  {value}")

    return "\n".join(lines)


RENDERERS: dict[str, Callable[[ApplianceState, bool], str]] = {
    "Dishwasher": _render_dishwasher,
}


def render(state: ApplianceState, verbose: bool = False) -> str:
    """Render one appliance's state as text."""
    if not state.appliance.connected:
        return f"{state.appliance.name}  offline"

    renderer = RENDERERS.get(state.appliance.type, _render_generic)
    return renderer(state, verbose)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_present.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/homeconnect/present.py tests/test_present.py
git commit -m "feat: add appliance renderers with generic fallback"
```

---

### Task 7: The CLI

**Files:**
- Create: `src/homeconnect/cli.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: `auth.load_credentials`, `auth.access_token`, `auth.NotAuthenticated`, `auth.MissingCredentials`, `api.Client`, `appliances.list_appliances`, `appliances.fetch_state`, `present.render`.
- Produces: `cli` — the Click group named in `[project.scripts]`.

**Design note:** `cli` is a group with `invoke_without_command=True` so that a
bare `homeconnect` prints status, whilst `homeconnect auth` remains available.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
import json

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'homeconnect.cli'`

- [ ] **Step 3: Write `src/homeconnect/cli.py`**

```python
"""Command-line entry point.

A bare `homeconnect` prints the one-line verdict for every appliance. The
`auth` sub-command performs the one-off device-flow consent.
"""

from __future__ import annotations

import json
import sys
import time

import click

from . import api, appliances, auth, present


def _build_client():
    """Return an API client, or exit with guidance rather than a traceback."""
    try:
        credentials = auth.load_credentials()
    except auth.MissingCredentials as exc:
        raise click.ClickException(str(exc)) from exc

    def token_provider() -> str:
        try:
            return auth.access_token(credentials)
        except auth.NotAuthenticated as exc:
            raise click.ClickException(
                f"{exc}\nRun `homeconnect auth` to authorise this machine."
            ) from exc

    return api.Client(token_provider=token_provider)


def _matches(appliance, needle: str) -> bool:
    needle = needle.lower()
    return needle in appliance.name.lower() or needle in appliance.type.lower()


@click.group(invoke_without_command=True)
@click.option("--verbose", "-v", is_flag=True, help="Show every field with raw API key names.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--appliance", "-a", default=None, help="Only show appliances matching this name or type.")
@click.pass_context
def cli(ctx, verbose: bool, as_json: bool, appliance: str | None) -> None:
    """Read-only status for Home Connect appliances."""
    if ctx.invoked_subcommand is not None:
        return

    client = _build_client()

    try:
        found = appliances.list_appliances(client)
    except api.NotAuthorised as exc:
        raise click.ClickException(
            f"{exc}\nRun `homeconnect auth` to authorise this machine."
        ) from exc
    except api.QuotaExceeded as exc:
        raise click.ClickException(
            f"API quota exhausted ({exc}). The daily limit is about 1000 calls."
        ) from exc

    if appliance:
        found = [item for item in found if _matches(item, appliance)]

    if not found:
        raise click.ClickException(
            "No matching appliances. Check the account the appliance is paired to."
        )

    states = [appliances.fetch_state(client, item) for item in found]

    if as_json:
        click.echo(json.dumps([
            {
                "haId": state.appliance.ha_id,
                "name": state.appliance.name,
                "type": state.appliance.type,
                "connected": state.appliance.connected,
                "programme": state.programme.key if state.programme else None,
                "status": state.status,
                "options": state.programme.options if state.programme else {},
            }
            for state in states
        ], indent=2))
        return

    for state in states:
        click.echo(present.render(state, verbose=verbose))


@cli.command()
def auth_command() -> None:
    """Authorise this machine (one-off)."""
    credentials = auth.load_credentials()
    code = auth.begin_device_authorisation(credentials)

    click.echo(f"Visit {code.verification_uri}")
    click.echo(f"and enter the code: {code.user_code}")
    click.echo("Waiting for approval…")

    deadline = time.monotonic() + code.expires_in
    while time.monotonic() < deadline:
        time.sleep(code.interval)
        try:
            auth.redeem_device_code(credentials, code.device_code)
        except auth.NotAuthenticated:
            continue
        click.echo("Authorised. The refresh token is stored in your Keychain.")
        return

    raise click.ClickException("Timed out waiting for approval.")


# Registered under the name the user types, not the Python identifier.
cli.add_command(auth_command, name="auth")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 5 passed

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: all tests pass (approximately 34)

- [ ] **Step 6: Commit**

```bash
git add src/homeconnect/cli.py tests/test_cli.py
git commit -m "feat: add read-only status CLI"
```

---

### Task 8: Documentation

**Files:**
- Create: `README.md`
- Create: `CLAUDE.md`

- [ ] **Step 1: Write `README.md`**

Cover: what the tool is and is explicitly not (no writes, no stats), the
one-off setup (register at developer.home-connect.com, create the 1Password
"Home Connect" environment with `HOMECONNECT_CLIENT_ID`, mount `.env`, run
`homeconnect auth`), the commands and flags, and the fact that no energy or
water data exists in the API so nobody goes looking for it later.

- [ ] **Step 2: Write `CLAUDE.md`**

Carry over the Global Constraints section of this plan verbatim as the hard
rules, plus the module map and the note that the developer application stays
in *development* state and is bound to a single Home Connect account.

- [ ] **Step 3: Commit and open the pull request**

```bash
git add README.md CLAUDE.md
git commit -m "docs: add README and CLAUDE.md"
git push -u origin feature/scaffold
gh pr create --fill
```
