from __future__ import annotations

import hashlib
import re
import time

from httporchestrator import RequestStep, Response

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import format_size, format_timestamp, status_is, variable_is


class GoFileFetcher(BaseFetcher):
    """
    has download notification: No
    has downloads count: No
    """

    NAME = "GoFile"
    BASE_URL = "https://api.gofile.io"
    _XWT_SALT = "5d4f7g8sd45fsd"
    _XBL = "en"
    URL_PATTERN = re.compile(r"(?:https?://)?(?:www\.)?gofile\.io/d/([A-Za-z0-9]+)")

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        return bool(cls.URL_PATTERN.search(url))

    def __init__(
        self,
        link: str,
        password: str | None = None,
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: Invalid GoFile URL provided")

        self.link = link
        self.password = password
        self.content_id = self.URL_PATTERN.search(link).group(1)
        self.user_agent = None

        super().__init__()

    def build_info_steps(self) -> list:
        return [
            RequestStep("create guest account")
            .post("/accounts")
            .headers(**self.headers)
            .json({})
            # Safe capture: returns None on non-200 so the status check below gives the real error
            .capture(
                "guest_token", lambda r, v: (r.json().get("data") or {}).get("token")
            )
            .after(lambda r, v: self._capture_user_agent(r))
            .check(
                lambda r, v: r.status_code != 429,
                "Error: GoFile rate-limited account creation; wait a moment and retry",
            )
            .check(status_is(200), "expected 200 response from /accounts")
            .check(
                lambda r, v: bool(v.get("guest_token")),
                "Error: GoFile did not return an account token",
            ),
            RequestStep("get content")
            .get(self._content_url())
            .headers(
                **self.headers,
                Authorization=lambda v: f"Bearer {v['guest_token']}",
                **{
                    "X-Website-Token": lambda v: self._compute_website_token(
                        v["guest_token"]
                    ),
                    "X-BL": self._XBL,
                },
            )
            .after(lambda r, v: self._extract_content_state(r))
            .after(lambda r, v: self._log_fetch_state(v["metadata"]))
            .check(status_is(200), "expected 200 response")
            .check(
                lambda r, v: (r.json().get("status") or "") != "error-passwordRequired",
                "Error: GoFile content is password-protected; pass password= to create_fetcher",
            )
            .check(
                lambda r, v: (r.json().get("status") or "") == "ok",
                "expected GoFile API status: ok",
            )
            .check(variable_is("available", True), "expected content to be available"),
        ]

    def build_fetch_steps(self) -> list:
        return [
            self.download_step(
                url_key="direct_link",
                filename_key="filename",
                downloads_count=1,
                when=lambda v: v.get("available"),
                headers={
                    **self.headers,
                    "Authorization": lambda v: f"Bearer {v['guest_token']}",
                    "Cookie": lambda v: f"accountToken={v['guest_token']}",
                    "X-Website-Token": lambda v: self._compute_website_token(
                        v["guest_token"]
                    ),
                },
            )
        ]

    def _capture_user_agent(self, response) -> dict:
        self.user_agent = response.request.headers.get("user-agent") or ""
        return {}

    def _compute_website_token(self, token: str) -> str:
        time_window = str(int(time.time()) // 14400)
        seed = (
            f"{self.user_agent}::{self._XBL}::{token}::{time_window}::{self._XWT_SALT}"
        )
        return hashlib.sha256(seed.encode()).hexdigest()

    def _content_url(self) -> str:
        url = f"/contents/{self.content_id}?cache=true&sortField=createTime&sortDirection=1"
        if self.password:
            hashed = hashlib.sha256(self.password.encode()).hexdigest()
            url = f"{url}&password={hashed}"
        return url

    def _extract_content_state(self, response: Response) -> dict:
        data = response.json().get("data", {})
        children = data.get("children") or data.get("contents") or {}

        files = [
            child
            for child in (children.values() if isinstance(children, dict) else [])
            if isinstance(child, dict) and child.get("type") == "file"
        ]
        if not files and isinstance(children, dict):
            files = [v for v in children.values() if isinstance(v, dict)]

        primary = files[0] if files else {}
        metadata = {
            "id": data.get("id"),
            "folder_name": data.get("name"),
            "type": data.get("type"),
            "filename": primary.get("name"),
            "size": primary.get("size"),
            "mimetype": primary.get("mimetype"),
            "download_url": primary.get("link"),
            "created_at": format_timestamp(primary.get("createTime", 0)),
            "file_count": len(files),
            "password_protected": bool(self.password),
            "downloads_count": primary.get("totalDownloadCount"),
        }
        return {
            "available": bool(primary),
            "filename": primary.get("name") or f"gofile-{self.content_id}",
            "direct_link": primary.get("link"),
            "metadata": metadata,
        }

    def _log_fetch_state(self, metadata: dict) -> None:
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "filename": metadata.get("filename"),
                "size": format_size(metadata.get("size")),
                "mimetype": metadata.get("mimetype"),
                "file_count": metadata.get("file_count"),
                "created_at": format_timestamp(metadata.get("created_at")),
            },
            details={"metadata": metadata},
        )
