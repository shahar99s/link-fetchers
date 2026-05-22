from __future__ import annotations

import re
from urllib.parse import urlparse

from httporchestrator import RequestStep, Response

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import status_is


class FourSharedFetcher(BaseFetcher):
    """
    has download notification: No
    has downloads count: No
    """

    NAME = "4shared"
    BASE_URL = "https://www.4shared.com"

    HOSTS = {"4shared.com", "www.4shared.com", "4s.io", "www.4s.io"}
    SHORT_URL_RE = re.compile(r"^/s/([A-Za-z0-9_-]+)$")
    FILE_URL_RE = re.compile(r"^/[^/]+/([A-Za-z0-9_-]+)/.*\.html$")

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in cls.HOSTS:
            return False
        return bool(
            cls.SHORT_URL_RE.match(parsed.path) or cls.FILE_URL_RE.match(parsed.path)
        )

    def __init__(self, link: str):
        if not self.is_relevant_url(link):
            raise ValueError("Error: No valid 4shared URL provided")
        self.link = link
        self.is_short_url = bool(self.SHORT_URL_RE.match(urlparse(link).path))
        super().__init__()

    def build_info_steps(self) -> list:
        steps = []

        if self.is_short_url:
            steps.append(
                RequestStep("resolve short URL")
                .get(self.link)
                .headers(**self.headers)
                .capture("page_url", lambda response, vars: str(response.url))
                .check(status_is(200), "expected 200 response")
            )

        steps.append(
            RequestStep("load file page")
            .get(lambda vars: vars.get("page_url") or self.link)
            .headers(**self.headers)
            .after(lambda response, vars: self.extract_page_state(response))
            .check(status_is(200), "expected 200 response")
            .check(
                lambda response, vars: bool(vars.get("direct_link")),
                "Error: File not found or not available for download on 4shared",
            )
        )
        return steps

    def build_fetch_steps(self) -> list:
        return [
            self.download_step(
                url_key="direct_link",
                filename_key="filename",
                downloads_count=1,
                capture_filename="filename",
            )
        ]

    def extract_page_state(self, response: Response) -> dict:
        html = response.text or ""
        direct_link = self._extract_direct_link(html)
        filename = self._extract_filename(html)
        size = self._extract_size(html)
        available = bool(direct_link) and not self._is_deleted_page(html)

        metadata = {
            "direct_link": direct_link,
            "filename": filename,
            "size": size,
            "available": available,
        }
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "filename": filename,
                "size": size,
                "available": available,
            },
            details={"metadata": metadata},
        )
        return metadata

    def _extract_direct_link(self, html: str) -> str | None:
        match = re.search(r'id="jsDirectDownloadLink"\s+value="([^"]+)"', html)
        if not match:
            match = re.search(r'value="([^"]+)"\s+id="jsDirectDownloadLink"', html)
        return match.group(1) if match else None

    def _extract_filename(self, html: str) -> str:
        match = re.search(r'class="jsFileName"\s+value="([^"]+)"', html)
        if not match:
            match = re.search(r'value="([^"]+)"\s+class="jsFileName"', html)
        if match:
            return match.group(1).strip()
        title_match = re.search(r"<title>([^<]+)</title>", html)
        if title_match:
            title = title_match.group(1).strip()
            title = re.sub(
                r"\s*[-|]\s*(download at|4shared).*$", "", title, flags=re.IGNORECASE
            ).strip()
            return title or "4shared-file"
        return "4shared-file"

    def _extract_size(self, html: str) -> str | None:
        match = re.search(
            r'<div class="file-info-tag"><b>Size</b>\s*([^<]+)</div>', html
        )
        if match:
            return match.group(1).strip()
        match = re.search(
            r'class="fileTagLink">\s*(\d[\d.,]*\s*(?:B|KB|MB|GB|TB))\s*<', html
        )
        if match:
            return match.group(1).strip()
        return None

    def _is_deleted_page(self, html: str) -> bool:
        markers = (
            "This file has been deleted",
            "File has been deleted",
            "not found",
            "không tồn tại",
        )
        return any(marker.lower() in html.lower() for marker in markers)
