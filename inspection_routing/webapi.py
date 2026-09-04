"""Minimal authenticated client for Raleigh's EnerGov WebAPI."""

from __future__ import annotations

from http.cookiejar import CookieJar
import json
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError
from urllib.parse import unquote, urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


WEBAPI_ENVIRONMENTS = {
    "prod": "https://raleighnc-energovapi.tylerhost.net/Apps/EnerGovWebAPI",
    "train": (
        "https://raleighnctrain-energovapi.tylerhost.net/"
        "Apps/EnerGovWebAPI"
    ),
    "test": (
        "https://raleighnctest-energovapi.tylerhost.net/"
        "Apps/EnerGovWebAPI"
    ),
}


class EnerGovWebApiError(RuntimeError):
    """Raised when the WebAPI rejects a request or login."""


class EnerGovWebApiClient:
    """Small cookie-authenticated client for the routes used by this repo."""

    def __init__(self, base_url: str, *, timeout: float = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._cookie_jar = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._cookie_jar))

    @staticmethod
    def environment_url(environment: str = "prod") -> str:
        selected = environment.strip().casefold()
        selected = {"production": "prod", "training": "train"}.get(
            selected, selected
        )
        try:
            return WEBAPI_ENVIRONMENTS[selected]
        except KeyError as error:
            choices = ", ".join(WEBAPI_ENVIRONMENTS)
            raise ValueError(
                f"Unknown WebAPI environment {environment!r}; use {choices}"
            ) from error

    @classmethod
    def from_credentials(
        cls,
        username: str,
        password: str,
        *,
        environment: str = "prod",
        timeout: float = 60,
    ) -> "EnerGovWebApiClient":
        client = cls(cls.environment_url(environment), timeout=timeout)
        try:
            client.login(username, password)
        except Exception:
            client.close()
            raise
        return client

    def __enter__(self) -> "EnerGovWebApiClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        """Retained for context-manager compatibility."""

    def _authentication_headers(self) -> dict[str, str]:
        cookies = {cookie.name: cookie.value for cookie in self._cookie_jar}
        if not cookies:
            raise EnerGovWebApiError(
                "The WebAPI session has no authentication cookie"
            )
        headers = {
            "Cookie": "; ".join(
                f"{name}={value}" for name, value in cookies.items()
            )
        }
        xsrf = cookies.get("XSRF-TOKEN")
        if xsrf:
            headers["X-XSRF-TOKEN"] = unquote(xsrf)
        current = cookies.get("tyler-energov-current-session")
        if current:
            try:
                session: Any = unquote(current)
                for _ in range(2):
                    if isinstance(session, str):
                        session = json.loads(session)
                if isinstance(session, Mapping) and session.get("sessionId"):
                    headers["egcurrentsession"] = str(session["sessionId"])
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return headers

    @staticmethod
    def _check_envelope(data: Mapping[str, Any], operation: str) -> None:
        if data.get("Success", data.get("success")) is False:
            message = (
                data.get("ErrorMessage")
                or data.get("errorMessage")
                or data.get("ValidationErrorMessage")
                or data.get("validationErrorMessage")
                or f"{operation} failed"
            )
            raise EnerGovWebApiError(str(message))

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | Sequence[Any] | None = None,
        *,
        query: Mapping[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url += "?" + urlencode(query, doseq=True)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json;charset=UTF-8",
            "eg-case": "camel",
            "energov-perf": "false",
        }
        if authenticated:
            headers.update(self._authentication_headers())
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            url, data=body, headers=headers, method=method.upper()
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                content = response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            content = error.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(content)
                message = (
                    detail.get("ErrorMessage")
                    or detail.get("errorMessage")
                    or detail.get("Message")
                    or content
                )
            except (ValueError, AttributeError):
                message = content
            raise EnerGovWebApiError(
                f"{method.upper()} {path} returned HTTP {error.code}: "
                f"{str(message)[:500]}"
            ) from error
        if not content.strip():
            return None
        data = json.loads(content)
        if isinstance(data, Mapping):
            self._check_envelope(data, f"{method.upper()} {path}")
        return data

    def login(self, username: str, password: str) -> Any:
        username = username.strip()
        if not username or not password:
            raise ValueError("WebAPI username and password are required")
        try:
            result = self._request(
                "GET",
                "/api/login/login",
                query={
                    "userName": username,
                    "password": password,
                    "isCap": "false",
                    "isOutputSuppressed": "false",
                },
                authenticated=False,
            )
        except Exception:
            # Do not expose the credential-bearing login URL in a traceback.
            raise EnerGovWebApiError(
                "WebAPI credential login request failed"
            ) from None
        if not list(self._cookie_jar):
            raise EnerGovWebApiError(
                "WebAPI processed the login but issued no session cookie"
            )
        try:
            self.call("GET", "/api/identity/currentuser")
        except Exception:
            raise EnerGovWebApiError(
                "WebAPI issued cookies but could not validate the session"
            ) from None
        return result

    def call(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | Sequence[Any] | None = None,
        *,
        query: Mapping[str, Any] | None = None,
    ) -> Any:
        return self._request(method, path, payload, query=query)

    def get_inspection(self, inspection_id: str) -> Any:
        if not inspection_id:
            raise ValueError("inspection_id is required")
        return self.call("GET", f"/api/inspections/{inspection_id}")
