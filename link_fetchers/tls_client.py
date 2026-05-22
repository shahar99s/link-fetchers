from __future__ import annotations

import httpx
from curl_cffi.requests import Session

_OS_SUFFIX: dict[str, str] = {
    "android": "_android",
    "ios": "_ios",
}


def resolve_impersonate(impersonate: str | dict) -> str:
    if isinstance(impersonate, str):
        return impersonate
    browser = impersonate.get("browser", "chrome").lower()
    version = str(impersonate.get("version", "124")).replace(".", "_")
    os_name = str(impersonate.get("os", "")).lower()
    suffix = _OS_SUFFIX.get(os_name, "")
    return f"{browser}{version}{suffix}"


class CurlImpersonatingClient:
    def __init__(self, impersonate: str | dict = "chrome124"):
        self._session = Session(impersonate=resolve_impersonate(impersonate))
        self.cookies = httpx.Cookies()

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})

        timeout = kwargs.pop("timeout", None)
        if isinstance(timeout, httpx.Timeout):
            timeout = timeout.read or timeout.connect

        content = kwargs.pop("content", None)
        if content is not None:
            kwargs.setdefault("data", content)

        for cookie in self.cookies.jar:
            self._session.cookies.set(
                cookie.name, cookie.value, cookie.domain, cookie.path
            )

        response = self._session.request(
            method=method,
            url=url,
            headers=headers,
            timeout=timeout,
            allow_redirects=kwargs.pop(
                "follow_redirects", kwargs.pop("allow_redirects", True)
            ),
            verify=kwargs.pop("verify", True),
            params=kwargs.pop("params", None),
            data=kwargs.pop("data", None),
            json=kwargs.pop("json", None),
        )

        for cookie in self._session.cookies.jar:
            self.cookies.set(cookie.name, cookie.value, cookie.domain, cookie.path)

        response_headers = dict(response.headers)
        for key in list(response_headers):
            if key.lower() in {
                "content-encoding",
                "content-length",
                "transfer-encoding",
            }:
                del response_headers[key]

        request = httpx.Request(method, str(response.url or url), headers=headers)
        return httpx.Response(
            response.status_code,
            headers=response_headers,
            content=response.content,
            request=request,
        )

    def close(self):
        self._session.close()
