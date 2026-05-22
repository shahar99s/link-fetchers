from __future__ import annotations

import json
import os
import re
import secrets
import socket
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlparse

import httpx
from httporchestrator import RequestStep
from loguru import logger

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import format_size, format_timestamp, status_is, variable_is

_AUTH_URL = "https://account.box.com/api/oauth2/authorize"
_TOKEN_URL = "https://api.box.com/oauth2/token"
_TOKEN_CACHE = os.path.join(os.path.expanduser("~"), ".link-fetchers", "box_token.json")
_OAUTH_TIMEOUT = 120


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BoxFetcher(BaseFetcher):
    """
    has download notification: No
    has downloads count: No
    note: app.box.com/file/{id} URLs authenticate via OAuth2. On first run a
          browser window opens so the user can authorise; the resulting token is
          cached at ~/.link-fetchers/box_token.json and refreshed automatically.
          Supply client_id/client_secret (or BOX_CLIENT_ID/BOX_CLIENT_SECRET env
          vars) from a Box developer app (developer.box.com).
          app.box.com/s/{code} public shared-link URLs work without any token.
    """

    NAME = "Box"
    BASE_URL = "https://api.box.com"

    VALID_HOSTS = {"box.com", "www.box.com", "app.box.com"}
    _FILE_PATTERN = re.compile(r"/file/(\d+)")
    _SHARED_PATTERN = re.compile(r"/s/([A-Za-z0-9_-]+)")

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if host not in cls.VALID_HOSTS:
            return False
        return bool(cls._FILE_PATTERN.search(url) or cls._SHARED_PATTERN.search(url))

    def __init__(
        self,
        link: str,
        access_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: Invalid Box URL provided")

        self.link = link
        self.access_token = access_token
        self.client_id = client_id or os.environ.get("BOX_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("BOX_CLIENT_SECRET")

        file_match = self._FILE_PATTERN.search(link)
        if file_match:
            self.link_type = "file"
            self.file_id = file_match.group(1)
            self.shared_link_url = None
        else:
            shared_match = self._SHARED_PATTERN.search(link)
            self.link_type = "shared"
            self.file_id = None
            self.shared_link_url = f"https://app.box.com/s/{shared_match.group(1)}"

        if self.link_type == "file" and not self.access_token:
            self.access_token = self._get_access_token()

        super().__init__()

    # ── token management ─────────────────────────────────────────────────────

    def _get_access_token(self) -> str | None:
        cached = self._load_cached_token()
        if cached:
            return cached
        if self.client_id and self.client_secret:
            return self._oauth2_flow()
        logger.warning(
            "[Box] no access token — provide client_id/client_secret "
            "or set BOX_CLIENT_ID / BOX_CLIENT_SECRET"
        )
        return None

    def _load_cached_token(self) -> str | None:
        try:
            with open(_TOKEN_CACHE, encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("expires_at", 0) > time.time() + 60:
                logger.debug("[Box] using cached access token")
                return data["access_token"]
            refresh = data.get("refresh_token")
            if refresh and self.client_id and self.client_secret:
                return self._refresh_token(refresh)
        except Exception:
            pass
        return None

    def _save_cached_token(
        self, access_token: str, refresh_token: str, expires_in: int
    ) -> None:
        os.makedirs(os.path.dirname(_TOKEN_CACHE), exist_ok=True)
        with open(_TOKEN_CACHE, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_at": time.time() + expires_in,
                },
                fh,
            )

    def _exchange_code(self, code: str, redirect_uri: str) -> str | None:
        resp = httpx.post(
            _TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": redirect_uri,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(
                "[Box] token exchange failed: {} {}", resp.status_code, resp.text[:200]
            )
            return None
        payload = resp.json()
        self._save_cached_token(
            payload["access_token"],
            payload.get("refresh_token", ""),
            payload.get("expires_in", 3600),
        )
        return payload["access_token"]

    def _refresh_token(self, refresh_token: str) -> str | None:
        resp = httpx.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
        self._save_cached_token(
            payload["access_token"],
            payload.get("refresh_token", refresh_token),
            payload.get("expires_in", 3600),
        )
        logger.debug("[Box] access token refreshed")
        return payload["access_token"]

    def _oauth2_flow(self) -> str | None:
        state = secrets.token_urlsafe(16)
        port = _free_port()
        redirect_uri = f"http://localhost:{port}/callback"
        auth_url = (
            f"{_AUTH_URL}?response_type=code"
            f"&client_id={self.client_id}"
            f"&redirect_uri={quote(redirect_uri, safe='')}"
            f"&state={state}"
        )
        code_holder: list[str | None] = [None]

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                params = parse_qs(urlparse(self.path).query)
                if params.get("state", [None])[0] == state:
                    code_holder[0] = params.get("code", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Box authorization complete.</h2>"
                    b"<p>You can close this tab.</p></body></html>"
                )

            def log_message(self, format, *args):  # noqa: A002
                pass

        server = HTTPServer(("127.0.0.1", port), _Handler)
        server.timeout = 5

        logger.info(
            "[Box] opening browser for authorization ({} s timeout)…", _OAUTH_TIMEOUT
        )
        webbrowser.open(auth_url)

        deadline = time.monotonic() + _OAUTH_TIMEOUT
        while code_holder[0] is None and time.monotonic() < deadline:
            server.handle_request()
        server.server_close()

        if not code_holder[0]:
            logger.warning("[Box] OAuth2 flow timed out or was cancelled")
            return None

        logger.info("[Box] authorization received, exchanging code for token…")
        return self._exchange_code(code_holder[0], redirect_uri)

    # ── step builders ────────────────────────────────────────────────────────

    def build_info_steps(self) -> list:
        if self.link_type == "file":
            return self._file_info_steps()
        return self._shared_info_steps()

    def build_fetch_steps(self) -> list:
        if self.link_type == "file":
            return self._file_fetch_steps()
        return self._shared_fetch_steps()

    # ── /file/ authenticated flow ────────────────────────────────────────────

    def _file_info_steps(self) -> list:
        fields = "name,size,sha1,created_at,modified_at,content_created_at"
        return [
            RequestStep("get file info")
            .get(f"/2.0/files/{self.file_id}?fields={fields}")
            .headers(
                **self.headers,
                Authorization=self._bearer(),
            )
            .after(lambda r, v: self._extract_state_file(r))
            .after(lambda r, v: self._log_fetch_state(v["metadata"]))
            .check(
                lambda r, v: r.status_code != 404,
                "Error: Box file not found",
            )
            .check(
                lambda r, v: r.status_code in (200, 401),
                "expected 200 or 401 response from Box API",
            )
            .check(variable_is("available", True), "Error: Box file unavailable"),
        ]

    def _file_fetch_steps(self) -> list:
        return [
            RequestStep("download")
            .get(f"/2.0/files/{self.file_id}/content")
            .headers(
                **self.headers,
                Authorization=self._bearer(),
            )
            .after(lambda r, v: self._handle_download(r, v))
            .check(
                lambda r, v: r.status_code in (200, 401),
                "expected 200 or 401 response from Box API",
            )
        ]

    def _handle_download(self, response, variables) -> dict:
        if response.status_code != 200:
            logger.warning(
                "[Box] download failed with HTTP {} - file may require authentication",
                response.status_code,
            )
            return {}
        return self.save_file(
            response, variables.get("filename", f"box-{self.file_id}")
        )

    # ── /s/ shared-link flow ─────────────────────────────────────────────────

    def _shared_info_steps(self) -> list:
        return [
            RequestStep("get shared item info")
            .get("/2.0/shared_items")
            .headers(
                **self.headers,
                Authorization=self._bearer(fallback="Bearer none"),
                Boxapi=f"shared_link={self.shared_link_url}",
            )
            .after(lambda r, v: self._extract_state(r))
            .after(lambda r, v: self._log_fetch_state(v["metadata"]))
            .check(
                lambda r, v: r.status_code != 404,
                "Error: Box shared link not found or expired",
            )
            .check(status_is(200), "expected 200 response from Box API")
            .check(variable_is("available", True), "Error: Box file unavailable"),
        ]

    def _shared_fetch_steps(self) -> list:
        return [
            self.download_step(
                url_key=lambda v: (
                    f"https://api.box.com/2.0/files/{v['file_id']}/content"
                ),
                filename_key="filename",
                headers={
                    **self.headers,
                    "Authorization": self._bearer(fallback="Bearer none"),
                    "Boxapi": f"shared_link={self.shared_link_url}",
                },
            )
        ]

    # ── helpers ──────────────────────────────────────────────────────────────

    def _bearer(self, fallback: str = "") -> str:
        token = self.access_token
        if not token:
            existing = self.headers.get("Authorization", "")
            if existing.startswith("Bearer "):
                token = existing[len("Bearer ") :]
        return f"Bearer {token}" if token else fallback

    def _extract_state(self, response) -> dict:
        if response.status_code != 200:
            label = self.file_id or "unknown"
            return {
                "available": False,
                "filename": f"box-{label}",
                "file_id": self.file_id,
                "metadata": {"file_id": self.file_id, "state": "not_found"},
            }
        data = response.json()
        filename = data.get("name")
        file_id = data.get("id") or self.file_id
        metadata = {
            "file_id": file_id,
            "filename": filename,
            "size": data.get("size"),
            "sha1": data.get("sha1"),
            "created_at": format_timestamp(data.get("created_at")),
            "modified_at": format_timestamp(data.get("modified_at")),
        }
        return {
            "available": bool(filename),
            "filename": filename or f"box-{file_id}",
            "file_id": file_id,
            "metadata": metadata,
        }

    def _extract_state_file(self, response) -> dict:
        if response.status_code == 200:
            data = response.json()
            filename = data.get("name")
            file_id = data.get("id") or self.file_id
            metadata = {
                "file_id": file_id,
                "filename": filename,
                "size": data.get("size"),
                "sha1": data.get("sha1"),
                "created_at": format_timestamp(data.get("created_at")),
                "modified_at": format_timestamp(data.get("modified_at")),
            }
            return {
                "available": bool(filename),
                "filename": filename or f"box-{file_id}",
                "file_id": file_id,
                "metadata": metadata,
                "downloads_count": 1,
            }
        elif response.status_code == 401:
            file_id = self.file_id
            filename = f"box-{file_id}"
            metadata = {
                "file_id": file_id,
                "filename": filename,
                "size": None,
                "sha1": None,
                "created_at": None,
                "modified_at": None,
                "note": "metadata unavailable without authentication token",
            }
            return {
                "available": True,
                "filename": filename,
                "file_id": file_id,
                "metadata": metadata,
                "downloads_count": 1,
            }
        else:
            label = self.file_id or "unknown"
            return {
                "available": False,
                "filename": f"box-{label}",
                "file_id": self.file_id,
                "metadata": {"file_id": self.file_id, "state": "not_found"},
                "downloads_count": 0,
            }

    def _log_fetch_state(self, metadata: dict) -> None:
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "filename": metadata.get("filename"),
                "size": format_size(metadata.get("size")),
                "file_id": metadata.get("file_id"),
                "created_at": metadata.get("created_at"),
            },
            details={"metadata": metadata},
        )
