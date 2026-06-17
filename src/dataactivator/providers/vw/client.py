"""Client for the VW EU Data Act portal: login, session and data delivery."""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from pydantic import BaseModel, ConfigDict

from . import const
from .pages import collect_login_fields, extract_login_error

logger = logging.getLogger(__name__)


class VwApiError(Exception):
    """Portal request failed."""


class VwAuthError(VwApiError):
    """Login failed or was rejected."""


class VwSettings(BaseModel):
    """Provider-specific settings from the config file."""

    model_config = ConfigDict(extra="forbid")

    email: str
    password: str
    country: str = "de"
    language: str = "de"
    brand: str = const.DEFAULT_BRAND
    retry_attempts: int = const.RETRY_ATTEMPTS
    retry_delay: float = const.RETRY_DELAY_SECONDS
    poll_interval: float = const.POLL_INTERVAL_SECONDS


@dataclass
class Observation:
    """One portal HTTP attempt, as seen by an observer callback."""

    endpoint: str
    status: int | None
    latency_ms: float
    portal_date: str | None = None
    error: str | None = None


# Called for every portal HTTP attempt, including each retry.
Observer = Callable[[Observation], None]


class VwPortalClient:
    """Synchronous client; authentication state lives in the cookie jar."""

    def __init__(
        self,
        settings: VwSettings,
        cookies: httpx.Cookies | None = None,
        observer: Observer | None = None,
    ) -> None:
        self.settings = settings
        self.observer = observer
        self._http = httpx.Client(
            cookies=cookies or httpx.Cookies(),
            headers={"User-Agent": const.USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
        )

    @property
    def cookies(self) -> httpx.Cookies:
        return self._http.cookies

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> VwPortalClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- session ------------------------------------------------------------

    def session_valid(self) -> bool:
        """Probe the stored session with a cheap authenticated request.

        An expired session answers 401/403 or redirects to the identity
        provider; a valid one returns JSON from the portal host. The
        access_token JWT expiry is checked first: some data endpoints
        fail on an expired token even while this probe still passes.
        """
        if self._access_token_expired():
            logger.debug("access_token cookie missing or expired")
            return False
        try:
            resp = self._http.get(
                f"{const.PORTAL_BASE}{const.VEHICLES_PATH}",
                params=const.VEHICLES_PARAMS,
            )
        except httpx.HTTPError as exc:
            raise VwApiError(f"network error while probing session: {exc}") from exc
        if resp.status_code in (401, 403):
            return False
        if urlparse(str(resp.url)).netloc != urlparse(const.PORTAL_BASE).netloc:
            return False
        if resp.status_code != 200:
            return False
        try:
            resp.json()
        except ValueError:
            return False
        return True

    def _access_token_expired(self) -> bool:
        """True if the access_token cookie is missing or its JWT expired."""
        for cookie in self._http.cookies.jar:
            if cookie.name == "access_token" and cookie.value:
                try:
                    payload = cookie.value.split(".")[1]
                    payload += "=" * (-len(payload) % 4)
                    claims = json.loads(base64.urlsafe_b64decode(payload))
                    return claims["exp"] <= time.time() + 30
                except (IndexError, KeyError, ValueError):
                    return True
        return True

    # -- login ---------------------------------------------------------------

    def login(self) -> None:
        """Full OIDC login; on success the cookie jar holds the session."""
        try:
            self._do_login()
        except httpx.HTTPError as exc:
            raise VwApiError(f"network error during login: {exc}") from exc

    def _do_login(self) -> None:
        # The portal sets load-balancer/session cookies on first contact
        # that its login callback later relies on.
        try:
            self._http.get(f"{const.PORTAL_BASE}/")
        except httpx.HTTPError as exc:
            logger.debug("priming request failed (ignored): %s", exc)

        # The portal's own authentication redirect endpoint breaks for
        # non-browser clients, so start the flow at the identity provider.
        resp = self._http.get(self._authorize_url())
        signin_url = str(resp.url)
        logger.debug("signin page: %s", signin_url)

        # Step 1: identifier (email).
        fields, action = collect_login_fields(resp.text)
        if "hmac" not in fields or "_csrf" not in fields:
            raise VwAuthError(
                f"could not parse sign-in form (fields: {sorted(fields)})"
            )
        fields["email"] = self.settings.email
        resp = self._http.post(
            urljoin(signin_url, action or ""),
            data=fields,
            headers={"Referer": signin_url},
        )
        authenticate_url = str(resp.url)
        logger.debug("after identifier: HTTP %s %s", resp.status_code, authenticate_url)

        # Step 2: password. This page keeps its fields in the JS model.
        fields, action = collect_login_fields(resp.text)
        if "hmac" not in fields or "_csrf" not in fields:
            raise VwAuthError(
                extract_login_error(resp.text)
                or "identity provider did not return the password form "
                "(wrong email, or the flow changed)"
            )
        fields["email"] = self.settings.email
        fields["password"] = self.settings.password
        if action:
            target = urljoin(authenticate_url, action)
        else:
            # Posting back to a URL that still carries ?relayState= would
            # duplicate the field from the body and be rejected (HTTP 400).
            target = authenticate_url.split("?", 1)[0]
        resp = self._http.post(
            target,
            data=fields,
            headers={"Referer": authenticate_url},
        )
        landing = str(resp.url)
        logger.debug("after credentials: HTTP %s %s", resp.status_code, landing)
        if resp.status_code >= 400:
            raise VwAuthError(
                extract_login_error(resp.text)
                or f"login rejected (HTTP {resp.status_code})"
            )

        # Success means the redirect chain ended back on the portal host;
        # bad credentials re-render the identity sign-in page instead.
        if "signin-service" in landing or "/error" in landing:
            raise VwAuthError("login failed — check email and password")
        if urlparse(landing).netloc != urlparse(const.PORTAL_BASE).netloc:
            raise VwAuthError(f"login did not complete (ended at {landing})")

    def _authorize_url(self) -> str:
        # state encodes country__language__brand; the portal callback
        # uses it to restore the locale.
        params = {
            "client_id": const.CLIENT_ID,
            "response_type": "code",
            "scope": const.SCOPE,
            "state": f"{self.settings.country}__{self.settings.language}__{self.settings.brand}",
            "redirect_uri": const.REDIRECT_URI,
            "prompt": "login",
        }
        return f"{const.AUTHORIZE_URL}?{urlencode(params)}"

    # -- data ----------------------------------------------------------------

    def _observe(self, obs: Observation) -> None:
        if self.observer is not None:
            self.observer(obs)

    def _get_with_retry(
        self,
        url: str,
        *,
        endpoint: str,
        headers: dict[str, str] | None = None,
        attempts: int | None = None,
        retry_delay: float | None = None,
    ) -> httpx.Response:
        """GET with patient retries on HTTP 5xx.

        The euda-apim endpoints fail erratically with 500 (the same
        request succeeds minutes later); bursts make it worse, so the
        delay between attempts is generous on purpose. Every attempt is
        reported to the observer — including the failing ones, which are
        the portal-availability evidence.
        """
        if attempts is None:
            attempts = self.settings.retry_attempts
        if retry_delay is None:
            retry_delay = self.settings.retry_delay
        last_status = 0
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                resp = self._http.get(url, headers=headers)
            except httpx.HTTPError as exc:
                self._observe(Observation(
                    endpoint=endpoint, status=None,
                    latency_ms=round((time.monotonic() - started) * 1000, 1),
                    error=str(exc),
                ))
                raise VwApiError(f"network error for {url}: {exc}") from exc
            self._observe(Observation(
                endpoint=endpoint, status=resp.status_code,
                latency_ms=round((time.monotonic() - started) * 1000, 1),
                portal_date=resp.headers.get("date"),
            ))
            if resp.status_code < 500:
                return resp
            last_status = resp.status_code
            if attempt < attempts:
                logger.info(
                    "HTTP %s from portal (attempt %d/%d), retrying in %.0fs",
                    resp.status_code, attempt, attempts, retry_delay,
                )
                time.sleep(retry_delay)
        raise VwApiError(
            f"portal still answers HTTP {last_status} after {attempts} attempts: {url}"
        )

    def get_data_request(self, vin: str, request_type: str = "partial") -> dict[str, Any]:
        """Metadata of the account's data request for this vehicle.

        The response's ``Identifier`` addresses the delivery endpoints.
        """
        url = const.PORTAL_BASE + const.METADATA_PATH.format(vin=vin, type=request_type)
        resp = self._get_with_retry(url, endpoint="metadata")
        if resp.status_code in (401, 403):
            raise VwAuthError("session expired")
        if resp.status_code != 200:
            raise VwApiError(
                f"metadata/{request_type} for {vin} failed (HTTP {resp.status_code})"
            )
        return resp.json()

    def list_datasets(
        self, vin: str, identifier: str, request_type: str = "partial"
    ) -> list[dict[str, Any]]:
        """Available datasets: [{name, createdOn, size}], newest first."""
        url = const.PORTAL_BASE + const.LIST_PATH.format(vin=vin, identifier=identifier)
        resp = self._get_with_retry(url, endpoint="list", headers={"type": request_type})
        if resp.status_code in (401, 403):
            raise VwAuthError("session expired")
        if resp.status_code != 200:
            raise VwApiError(f"dataset list for {vin} failed (HTTP {resp.status_code})")
        data = resp.json()
        return data if isinstance(data, list) else data.get("files", [])

    def download_dataset(
        self, vin: str, identifier: str, name: str, request_type: str = "partial"
    ) -> bytes:
        """Download one dataset ZIP (raw bytes)."""
        url = const.PORTAL_BASE + const.DOWNLOAD_PATH.format(vin=vin, identifier=identifier)
        resp = self._get_with_retry(
            url, endpoint="download", headers={"filename": name, "type": request_type}
        )
        if resp.status_code in (401, 403):
            raise VwAuthError("session expired")
        if resp.status_code != 200:
            raise VwApiError(f"download of {name} failed (HTTP {resp.status_code})")
        return resp.content

    def list_vehicles(self) -> list[dict[str, Any]]:
        """VINs (plus nickname where present) visible to the account."""
        resp = self._http.get(
            f"{const.PORTAL_BASE}{const.VEHICLES_PATH}",
            params=const.VEHICLES_PARAMS,
        )
        if resp.status_code in (401, 403):
            raise VwAuthError("session expired")
        if resp.status_code != 200:
            raise VwApiError(f"vehicles request failed (HTTP {resp.status_code})")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise VwApiError(f"vehicles endpoint returned invalid JSON: {exc}") from exc
        return _extract_vehicles(payload)


def _extract_vehicles(payload: Any) -> list[dict[str, Any]]:
    """Collect VIN-shaped entries from the undocumented vehicles payload."""
    found: dict[str, dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            vin = node.get("vin") or node.get("vehicleIdentificationNumber")
            if isinstance(vin, str) and len(vin) == 17:
                entry = found.setdefault(vin, {"vin": vin})
                nickname = (
                    node.get("vehicleNickname")
                    or node.get("nickname")
                    or node.get("modelName")
                )
                if nickname:
                    entry["nickname"] = nickname
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return list(found.values())
