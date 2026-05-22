from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

from httporchestrator import ConditionalStep, RequestStep, Response

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import (
    Mode,
    format_size,
    format_timestamp,
    status_is,
    variable_is,
)

"""
NOTE: transfernow saves logs for each download and the IP they came from
Also the first download for each user sends a notification
"""


class TransferNowFetcher(BaseFetcher):
    """
    has download notification: Yes
    has downloads count: Yes
    note: Also save source IP for each download
    """

    NAME = "TransferNow"
    BASE_URL = "https://www.transfernow.net"

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        try:
            cls.parse_link(url)
            return True
        except ValueError:
            return False

    @staticmethod
    def parse_link(link: str) -> tuple[str, str]:
        parsed = urlparse(link)
        host = (parsed.hostname or "").lower()
        is_transfernow_host = host == "transfernow.net" or host.endswith(
            ".transfernow.net"
        )
        if not is_transfernow_host:
            raise ValueError(f"Error: No valid TransferNow URL provided. Got: {link}")

        path_parts = [part for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)

        if len(path_parts) >= 3 and path_parts[-3] == "dl":
            return path_parts[-2], path_parts[-1]
        if len(path_parts) == 2 and path_parts[-2] == "dl":
            return path_parts[-1], ""
        if path_parts and path_parts[-1] == "cld":
            transfer_id = query.get("utm_source", [None])[0]
            secret = query.get("utm_medium", [""])[0] or ""
            if transfer_id:
                return transfer_id, secret
        if len(path_parts) >= 2 and path_parts[-2:] == ["d", "start"]:
            transfer_id = query.get("utm_source", [None])[0]
            secret = query.get("utm_medium", [""])[0] or ""
            if transfer_id:
                return transfer_id, secret
        raise ValueError("Error: Unable to parse TransferNow URL")

    def __init__(
        self,
        link: str,
        sender_secret: str | None = None,
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: No valid TransferNow URL provided")

        self.link = link
        self.sender_secret = sender_secret
        self.transfer_id, self.secret = self.parse_link(link)

        super().__init__()

    def build_info_steps(self) -> list:
        steps = [
            RequestStep("load transfer page")
            .get(
                f"/en/cld?utm_source={self.transfer_id}"
                + (f"&utm_medium={self.secret}" if self.secret else "")
            )
            .headers(**self.headers)
            .after(lambda response, vars: self.extract_transfer_state(response))
            .after(
                lambda response, vars: self.log_fetch_state(
                    vars["metadata"],
                    vars["downloads_count"],
                    None,
                    vars["filename"],
                    vars["primary_file"],
                    None,
                )
            )
            .check(status_is(200), "expected 200 response")
            .check(variable_is("available", True), "expected transfer to be available")
        ]

        if self.sender_secret:
            steps.append(
                RequestStep("load transfer stats")
                .get(f"/api/transfer/v2/transfers/{self.transfer_id}")
                .headers(**self.headers)
                .params(senderSecret=self.sender_secret)
                .after(
                    lambda response, vars: {
                        "downloads_count": self.extract_stats_downloads_count(response),
                        "views_count": self.extract_stats_views_count(response),
                        "download_events": self.extract_download_events(response),
                    }
                )
                .after(
                    lambda response, vars: self.log_fetch_state(
                        vars["metadata"],
                        vars["downloads_count"],
                        vars.get("views_count"),
                        vars["filename"],
                        vars["primary_file"],
                        vars.get("download_events"),
                    )
                )
                .check(status_is(200), "expected 200 response")
            )

        return steps

    def build_fetch_steps(self) -> list:
        return [
            ConditionalStep(
                RequestStep("create direct link")
                .get("/api/transfer/downloads/link")
                .headers(**self.headers)
                .params(
                    transferId=self.transfer_id,
                    userSecret=self.secret,
                    fileId=lambda vars: vars["file_id"],
                )
                .capture("direct_link", lambda response, vars: response.json()["url"])
                .check(status_is(200), "expected 200 response")
            ).run_when(self.should_fetch),
            self.download_step(),
        ]

    def log_fetch_state(
        self,
        metadata: dict,
        downloads_count=None,
        views_count=None,
        filename=None,
        primary_file=None,
        download_events=None,
    ):
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "filename": filename or (primary_file or {}).get("name"),
                "downloads_count": downloads_count,
                "views_count": views_count,
                "from_email": (metadata.get("owner") or {}).get("email")
                or (metadata.get("sender") or {}).get("email"),
                "upload_date": format_timestamp(
                    (metadata.get("validity") or {}).get("from")
                ),
                "expires_at": format_timestamp(
                    (metadata.get("validity") or {}).get("to")
                ),
                "size": format_size(
                    metadata.get("size") or (primary_file or {}).get("size")
                ),
            },
            details={
                "metadata": metadata,
                "primary_file": primary_file,
                "download_events": download_events,
            },
        )

    def extract_next_data(self, response: Response) -> dict:
        content = response.body.decode("utf-8")
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
            content,
            re.DOTALL,
        )
        if not match:
            raise ValueError("Error: TransferNow metadata payload not found")
        return json.loads(match.group(1))

    def extract_transfer_state(self, response: Response) -> dict:
        payload = self.extract_next_data(response)
        transfer_data = (
            payload.get("props", {}).get("pageProps", {}).get("transferData")
        )
        if not transfer_data:
            raise ValueError("Error: TransferNow transfer metadata not found")

        metadata = transfer_data.get("metadata", {})
        files = metadata.get("files") or []
        if not files:
            raise ValueError("Error: TransferNow files metadata not found")

        primary_file = files[0]
        file_id = primary_file.get("id")
        if not file_id:
            raise ValueError("Error: TransferNow file id not found")

        return {
            "downloads_count": None,
            "metadata": metadata,
            "primary_file": primary_file,
            "file_id": file_id,
            "filename": primary_file.get("name") or f"{self.transfer_id}.bin",
            "available": (
                transfer_data.get("available") is True
                and transfer_data.get("locked") is False
                and transfer_data.get("shouldBuy") is False
                and metadata.get("status") == "ENABLED"
            ),
        }

    def extract_stats_downloads_count(self, response: Response) -> int:
        return response.json().get("downloadsCount") or 0

    def extract_stats_views_count(self, response: Response) -> int:
        return response.json().get("viewsCount") or 0

    def extract_download_events(self, response: Response) -> list:
        return response.json().get("downloadEvents") or []
