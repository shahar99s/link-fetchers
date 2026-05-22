from __future__ import annotations

import re
from urllib.parse import urlparse

from httporchestrator import RequestStep

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import format_size, status_is, variable_is


class TurbobitFetcher(BaseFetcher):
    """
    has download notification: No
    has downloads count: No
    """

    NAME = "Turbobit"
    BASE_URL = "https://app.turbobit.net"
    VALID_HOSTS = {"turbobit.net", "www.turbobit.net", "trbt.cc"}
    FILE_ID_PATTERN = re.compile(
        r"(?:https?://)?(?:www\.)?(?:turbobit\.net|trbt\.cc)/([A-Za-z0-9]+)"
    )

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in cls.VALID_HOSTS:
            return False
        return bool(cls.FILE_ID_PATTERN.search(url))

    def __init__(self, link: str):
        if not self.is_relevant_url(link):
            raise ValueError("Error: Invalid Turbobit/trbt.cc URL provided")

        self.link = link
        match = self.FILE_ID_PATTERN.search(link)
        self.file_id = match.group(1)

        super().__init__()

    def build_info_steps(self) -> list:
        return [
            RequestStep("get file info")
            .post("/api/download/info")
            .headers(**self.headers)
            .json(
                {
                    "fileId": self.file_id,
                    "referrer": "",
                    "site": None,
                    "shortDomain": "",
                }
            )
            .after(lambda r, v: self._extract_info_state(r))
            .after(lambda r, v: self._log_fetch_state(v["metadata"]))
            .check(
                lambda r, v: r.status_code != 404,
                "Error: Turbobit file not found or deleted",
            )
            .check(status_is(200), "expected 200 response from /api/download/info")
            .check(variable_is("available", True), "Error: Turbobit file unavailable"),
        ]

    def build_fetch_steps(self) -> list:
        return [
            RequestStep("prepare free download")
            .post("/api/download/free/prepare")
            .headers(**self.headers)
            .json({"fileId": self.file_id})
            .check(
                status_is(200), "expected 200 response from /api/download/free/prepare"
            )
            .check(
                lambda r, v: bool((r.json() or {}).get("success")),
                "Error: Turbobit free download is not ready; captcha or wait gate may be required",
            ),
            RequestStep("start free download")
            .post("/api/download/free/start")
            .headers(**self.headers)
            .json({"fileId": self.file_id})
            .after(lambda r, v: self._extract_download_state(r))
            .check(
                status_is(200), "expected 200 response from /api/download/free/start"
            )
            .check(
                variable_is("download_available", True),
                "Error: Turbobit did not return a download URL",
            ),
            self.download_step(
                url_key="direct_link",
                filename_key="filename",
                downloads_count=1,
                when=lambda v: bool(v.get("download_available")),
                headers=self.headers,
            ),
        ]

    def _extract_info_state(self, response) -> dict:
        if response.status_code != 200:
            return {
                "available": False,
                "filename": f"turbobit-{self.file_id}",
                "metadata": {"file_id": self.file_id, "state": "not_found"},
            }
        data = response.json()
        file_info = data.get("file") or {}
        filename = file_info.get("name")
        size = file_info.get("size")
        metadata = {
            "file_id": self.file_id,
            "filename": filename,
            "size": size,
            "premium_only": data.get("premiumOnlyDownload", False),
            "state": "available",
        }
        return {
            "available": bool(file_info),
            "filename": filename or f"turbobit-{self.file_id}",
            "metadata": metadata,
        }

    def _extract_download_state(self, response) -> dict:
        if response.status_code != 200:
            return {"download_available": False}
        data = response.json() or {}
        return {
            "download_available": bool(data.get("downloadUrl")),
            "direct_link": data.get("downloadUrl"),
        }

    def _log_fetch_state(self, metadata: dict) -> None:
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "filename": metadata.get("filename"),
                "size": format_size(metadata.get("size")),
                "state": metadata.get("state"),
                "premium_only": metadata.get("premium_only"),
            },
            details={"metadata": metadata},
        )
