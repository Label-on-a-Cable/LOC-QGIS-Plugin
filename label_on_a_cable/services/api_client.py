"""Core HTTP client for the LOC API.

Wraps ``requests.Session`` with the base URL, default headers,
response handling, and one method per API endpoint.
"""

import json as _json
import os
import logging

import requests

from .exceptions import (
    AuthenticationException,
    LOCAPIException,
    NetworkException,
    OTPRequiredException,
    ServerException,
    ValidationException,
)

BASE_URL = "https://dashboard.loc.store/api"
DEFAULT_TIMEOUT = 30  # seconds
PUSH_TIMEOUT = 600    # seconds (10 min)

_log = logging.getLogger("LOC.api_client")



def _extract_request_id(headers) -> str:
    """Extract a trace/correlation ID from response headers."""
    for key in ("x-request-id", "x-trace-id", "x-correlation-id",
                "request-id", "trace-id"):
        val = headers.get(key, "")
        if val:
            return val
    return ""


class ApiClient:
    """Low-level HTTP client for every LOC API endpoint."""

    def __init__(self, base_url=BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # ------------------------------------------------------------------
    # Auth header management
    # ------------------------------------------------------------------

    def set_token(self, token):
        """Set the Bearer token for all subsequent requests."""
        self.session.headers["Authorization"] = f"Bearer {token}"

    def clear_token(self):
        """Remove the Bearer token."""
        self.session.headers.pop("Authorization", None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path):
        """Build a full URL from a relative path."""
        return f"{self.base_url}/{path.lstrip('/')}"

    def _handle_response(self, response):
        """Inspect status code and body; raise on errors.

        Follows the status-code table from api_contract.md.
        """
        status = response.status_code

        # --- 5xx: server error ---
        if status >= 500:
            body = ""
            try:
                body = response.text[:5000]
            except Exception:
                pass

            elapsed = (
                response.elapsed.total_seconds()
                if response.elapsed else 0
            )
            request_id = _extract_request_id(response.headers)

            _log.error(
                "Server error: URL=%s  status=%d  elapsed=%.1fs  "
                "request-id=%s  body=%s",
                response.url, status, elapsed,
                request_id or "(none)",
                body[:500],
            )

            raise ServerException(
                f"Server error (HTTP {status}): {body[:500]}".strip(),
                status_code=status,
                request_url=str(response.url),
                request_id=request_id,
                elapsed_seconds=elapsed,
            )

        # --- Try to parse JSON body (may be empty) ---
        try:
            data = response.json()
        except ValueError:
            data = {}

        # --- 400: validation ---
        if status == 400:
            msg = data.get("error", data.get("message", "Validation error"))
            raise ValidationException(str(msg), status_code=400)

        # --- 401: auth (with auto-logout hint) ---
        if status == 401:
            # Check if this is actually an OTP prompt (some 401s carry otp:true)
            if data.get("otp"):
                raise OTPRequiredException(
                    data.get("error", "Check your email for the verification code")
                )
            msg = data.get("error", data.get("message", "Authentication failed"))
            raise AuthenticationException(str(msg), status_code=401)

        # --- 403: forbidden ---
        if status == 403:
            msg = data.get("error", data.get("message", "Forbidden"))
            raise AuthenticationException(str(msg), status_code=403)

        # --- 404: not found ---
        if status == 404:
            msg = data.get("error", data.get("message", "Resource not found"))
            raise LOCAPIException(str(msg), status_code=404)

        # --- 2xx: success, but still check for OTP prompt in body ---
        # data may be a list (e.g. locations endpoint returns a JSON array)
        if 200 <= status < 300:
            if isinstance(data, dict) and data.get("otp") is True:
                raise OTPRequiredException(
                    data.get("error", "Check your email for the verification code")
                )
            return data

        # --- Anything else unexpected ---
        raise LOCAPIException(
            f"Unexpected HTTP {status}", status_code=status
        )

    def _get(self, path, params=None, timeout=DEFAULT_TIMEOUT):
        """Perform a GET request and return parsed data."""
        url = self._url(path)
        try:
            resp = self.session.get(url, params=params, timeout=timeout)
        except requests.ConnectionError as exc:
            raise NetworkException(
                f"Connection failed: {url} — {exc}",
            ) from exc
        except requests.Timeout as exc:
            raise NetworkException(
                f"Request timed out after {timeout}s: {url}",
            ) from exc
        return self._handle_response(resp)

    def _post(self, path, json=None, params=None, timeout=DEFAULT_TIMEOUT):
        """Perform a POST request and return parsed data."""
        url = self._url(path)
        try:
            resp = self.session.post(
                url, json=json, params=params, timeout=timeout,
            )
        except requests.ConnectionError as exc:
            raise NetworkException(
                f"Connection failed: {url} — {exc}",
            ) from exc
        except requests.Timeout as exc:
            _log.error(
                "Request timed out: URL=%s  timeout=%ds", url, timeout,
            )
            raise NetworkException(
                f"Request timed out after {timeout}s: {url}",
            ) from exc
        return self._handle_response(resp)

    # ------------------------------------------------------------------
    # Endpoint methods
    # ------------------------------------------------------------------

    # 1. Reachability check (no dedicated health endpoint exists)
    def health_check(self):
        """Check whether the LOC server is reachable.

        There is no /health endpoint.  Instead we POST to the login
        endpoint with empty credentials.  The server will reject the
        request (400/401), but that proves it is up and responding.

        Returns True  — server is reachable.
        Raises NetworkException — DNS failure / connection refused / timeout.
        """
        try:
            self.session.post(
                self._url("auth/app/login/enabled2fa"),
                json={"email": "", "password": ""},
                params={"plugin": "true"},
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.ConnectionError as exc:
            raise NetworkException(f"Connection failed: {exc}") from exc
        except requests.Timeout as exc:
            raise NetworkException(f"Request timed out: {exc}") from exc
        # Any HTTP response (including 400/401) means the server is alive.
        return True

    # 2. Auth: login (with optional OTP)
    def login(self, email, password, otp=None):
        """POST auth/app/login/enabled2fa?plugin=true

        Returns parsed response dict on success.
        Raises OTPRequiredException if 2FA code is needed.
        """
        body = {"email": email, "password": password}
        if otp:
            body["otp"] = otp
        return self._post("auth/app/login/enabled2fa", json=body,
                          params={"plugin": "true"})

    # 3. Locations: list all for user
    def get_all_locations(self):
        """GET locations/all-locations-for-user"""
        return self._get("locations/all-locations-for-user")

    # 4. Locations: single location details
    def get_location(self, location_id):
        """GET locations/{location_id}"""
        return self._get(f"locations/{location_id}")

    # 5. Locations: logs
    def get_location_logs(self, location_id):
        """GET locations/{location_id}/logs"""
        return self._get(f"locations/{location_id}/logs")

    # 6. Categories: all for organization
    def get_categories_by_org(self, org_id):
        """GET organizations/categories?organization={org_id}"""
        return self._get("organizations/categories",
                         params={"organization": org_id})

    # 7. Categories: by location
    def get_categories_by_location(self, location_id):
        """GET organizations/categories?location={location_id}"""
        return self._get("organizations/categories",
                         params={"location": location_id})

    # 8. Categories: single by ID
    def get_category(self, category_id):
        """GET organizations/categories/{category_id}"""
        return self._get(f"organizations/categories/{category_id}")

    # 9. LOCs: fetch all for location
    def get_locs_for_location(self, location_id):
        """POST LOCs/all-locs-for-location"""
        return self._post("LOCs/all-locs-for-location",
                          json={"location_id": location_id})

    # 10. LOCs: push (or preview with stats=true)
    def push_locs(self, payload, preview=False):
        """POST LOCs/plugin-v3   (?stats=true for dry-run preview)"""
        params = {"stats": "true"} if preview else None
        return self._post("LOCs/plugin-v3", json=payload,
                          params=params, timeout=PUSH_TIMEOUT)
