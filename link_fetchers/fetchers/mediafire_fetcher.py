from __future__ import annotations

import hashlib
import re
from urllib.parse import urlencode, urlparse

from httporchestrator import ConditionalStep, RequestStep, Response
from loguru import logger

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import Mode, format_size, format_timestamp, status_is


class MediaFireFetcher(BaseFetcher):
    """
    has download notification: No
    has downloads count: Yes
    note: By providing credentials, downloads count is bypassed by copying the file into our user account
    """

    NAME = "MediaFire"
    BASE_URL = "https://mediafire.com"
    URL_PATTERN = re.compile(r"^/file/(\w+)/")

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        is_mediafire_host = host == "mediafire.com" or host.endswith(".mediafire.com")
        return is_mediafire_host and bool(cls.URL_PATTERN.search(parsed.path))

    def __init__(
        self,
        link: str,
        email: str = "",
        password: str = "",
        app_id: str = "42511",
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: No valid MediaFire URL provided")

        self.link = link
        self.headers = self.initial_headers()
        self.file_key = self.URL_PATTERN.search(urlparse(link).path).group(1)
        self.email = email.strip()
        self.password = password.strip()
        self.app_id = app_id.strip()
        self.has_credentials = bool(self.email and self.password)
        self.form_headers = {
            **self.headers,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        super().__init__()

    def build_info_steps(self) -> list:
        return [
            RequestStep("info")
            .post("/api/1.5/file/get_info.php")
            .headers(**self.headers)
            .params(recursive="yes", quick_key=self.file_key, response_format="json")
            .after(lambda response, vars: self.extract_file_state(response))
            .after(
                lambda response, vars: self.log_fetch_state(
                    vars["metadata"], vars["downloads_count"]
                )
            )
            .check(status_is(200), "expected 200 response")
            .check(
                lambda response, vars: (
                    response.json()
                    .get("response", {})
                    .get("file_info", {})
                    .get("password_protected")
                    == "no"
                ),
                "expected non-password-protected file",
            )
            .check(
                lambda response, vars: (
                    response.json()
                    .get("response", {})
                    .get("file_info", {})
                    .get("permissions", {})
                    .get("read")
                    == "1"
                ),
                "expected readable file",
            )
            .check(
                lambda response, vars: (
                    response.json().get("response", {}).get("result") == "Success"
                ),
                "expected MediaFire success payload",
            ),
        ]

    def build_fetch_steps(self) -> list:
        return (
            self.build_authenticated_fetch_steps()
            if self.has_credentials
            else self.build_public_fetch_steps()
        )

    def build_authenticated_fetch_steps(self) -> list:
        return [
            ConditionalStep(
                RequestStep("login")
                .post("/api/1.5/user/get_session_token.php")
                .headers(**self.form_headers)
                .data(self.build_login_body())
                .after(lambda response, vars: self.extract_session_state(response))
                .check(status_is(200), "expected 200 response")
                .check(
                    lambda response, vars: (
                        response.json().get("response", {}).get("result") == "Success"
                    ),
                    "expected login success",
                )
            ).run_when(self.should_fetch),
            ConditionalStep(
                RequestStep("copy file to user account")
                .post("/api/1.5/file/copy.php")
                .headers(**self.form_headers)
                .data(
                    lambda vars: self.build_authenticated_api_body(
                        "/api/1.5/file/copy.php",
                        vars,
                        quick_key=self.file_key,
                        folder_key="myfiles",
                    )
                )
                .capture(
                    "copy_quick_key",
                    lambda response, vars: self.extract_copy_quick_key(response),
                )
                .after(
                    lambda response, vars: self.update_authenticated_session_state(
                        response, vars
                    )
                )
                .check(status_is(200), "expected 200 response")
                .check(
                    lambda response, vars: (
                        response.json().get("response", {}).get("result") == "Success"
                    ),
                    "expected copy success",
                )
            ).run_when(self.should_fetch),
            ConditionalStep(
                RequestStep("get copy direct link")
                .post("/api/1.5/file/get_links.php")
                .headers(**self.form_headers)
                .data(
                    lambda vars: self.build_authenticated_api_body(
                        "/api/1.5/file/get_links.php",
                        vars,
                        quick_key=vars["copy_quick_key"],
                        link_type="direct_download",
                    )
                )
                .capture(
                    "direct_download_link",
                    lambda response, vars: self.extract_copy_direct_link(response),
                )
                .check(status_is(200), "expected 200 response")
                .check(
                    lambda response, vars: (
                        response.json().get("response", {}).get("result") == "Success"
                    ),
                    "expected link lookup success",
                )
            ).run_when(self.should_fetch),
            self.download_step(
                name="download (user copy)", url_key="direct_download_link"
            ),
        ]

    def build_public_fetch_steps(self) -> list:
        return [
            ConditionalStep(
                RequestStep("get direct link")
                .get(f"/file/{self.file_key}")
                .headers(**self.headers)
                .capture(
                    "direct_download_link",
                    lambda response, vars: self.extract_direct_download_link(response),
                )
                .check(status_is(200), "expected 200 response")
            ).run_when(self.should_fetch),
            self.download_step(name="download link", url_key="direct_download_link"),
        ]

    def log_fetch_state(self, metadata: dict, downloads_count: int):
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "uploader": metadata.get("owner_name"),
                "filename": metadata.get("filename"),
                "downloads_count": downloads_count,
                "views_count": metadata.get("views") or metadata.get("view"),
                "upload_date": format_timestamp(metadata.get("created")),
                "size": format_size(metadata.get("size")),
                "auth_mode": "user_copy" if self.has_credentials else "public",
            },
            details={"metadata": metadata},
        )

    def extract_file_state(self, response: Response) -> dict:
        metadata = response.json()["response"]["file_info"]
        return {
            "metadata": metadata,
            "downloads_count": 1,
            "filename": metadata.get("filename"),
        }

    def extract_direct_download_link(self, response: Response) -> str:
        body = response.body
        text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
        for line in text.splitlines():
            match = re.search(r'href="((http|https)://download[^\"]+)', line)
            if match:
                return match.groups()[0]
        raise ValueError("Error: No valid direct download link found")

    def build_login_body(self) -> str:
        return urlencode(
            {
                "application_id": self.app_id,
                "email": self.email,
                "password": self.password,
                "response_format": "json",
                "signature": self.build_login_signature(),
                "token_version": "2",
            }
        )

    def extract_session_state(self, response: Response) -> dict:
        payload = response.json().get("response", {})
        token = payload.get("session_token")
        if not token:
            raise ValueError("Error: MediaFire login failed - no session token")
        logger.info("[MediaFire] authenticated session obtained")
        return {
            "session_token": token,
            "session_time": str(payload.get("time") or ""),
            "session_secret_key": str(payload.get("secret_key") or ""),
        }

    def build_login_signature(self) -> str:
        raw = f"{self.email}{self.password}{self.app_id}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def build_authenticated_api_body(self, uri: str, vars: dict, **params) -> str:
        query_params = {
            "response_format": "json",
            "session_token": vars["session_token"],
            **params,
        }
        query = urlencode(sorted(query_params.items()))
        signature = self.build_authenticated_call_signature(
            vars.get("session_secret_key", ""),
            vars.get("session_time", ""),
            uri,
            query,
        )
        return f"{query}&signature={signature}"

    def build_authenticated_call_signature(
        self, secret_key: str, session_time: str, uri: str, query: str
    ) -> str:
        if not secret_key or not session_time:
            raise ValueError("Error: MediaFire authenticated session metadata missing")
        secret_key_mod = int(secret_key) % 256
        raw = f"{secret_key_mod}{session_time}{uri}?{query}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def extract_copy_quick_key(self, response: Response) -> str:
        payload = response.json().get("response", {})
        new_keys = payload.get("new_quickkeys")
        if not new_keys:
            new_keys = payload.get("new_key") or []
        if isinstance(new_keys, list) and new_keys:
            return new_keys[0]
        if isinstance(new_keys, str):
            return new_keys
        raise ValueError("Error: MediaFire file copy failed - no new quick_key")

    def update_authenticated_session_state(
        self, response: Response, vars: dict
    ) -> dict:
        payload = response.json().get("response", {})
        if payload.get("new_key") != "yes":
            return {}
        return {
            "session_secret_key": self.regenerate_secret_key(
                vars.get("session_secret_key", "")
            )
        }

    def regenerate_secret_key(self, secret_key: str) -> str:
        if not secret_key:
            raise ValueError("Error: MediaFire authenticated session metadata missing")
        return str((int(secret_key) * 16807) % 2147483647)

    def extract_copy_direct_link(self, response: Response) -> str:
        payload = response.json().get("response", {})
        direct_link = self.find_download_link(payload.get("links"))
        if direct_link:
            return direct_link

        direct_link = self.find_download_link(payload)
        if direct_link:
            return direct_link

        raise ValueError("Error: MediaFire direct link lookup failed - no download URL")

    def find_download_link(self, payload) -> str:
        if isinstance(payload, str):
            candidate = payload.strip()
            if candidate.startswith("//"):
                return f"https:{candidate}"
            if candidate.startswith(("http://", "https://")):
                return candidate
            return ""

        if isinstance(payload, dict):
            for key in ("direct_download", "normal_download", "link"):
                candidate = self.find_download_link(payload.get(key))
                if candidate:
                    return candidate
            for value in payload.values():
                candidate = self.find_download_link(value)
                if candidate:
                    return candidate
            return ""

        if isinstance(payload, list):
            for item in payload:
                candidate = self.find_download_link(item)
                if candidate:
                    return candidate
            return ""

        return ""
