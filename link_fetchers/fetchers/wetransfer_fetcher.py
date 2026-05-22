from __future__ import annotations

import datetime
from urllib.parse import urlparse

from httporchestrator import ConditionalStep, RequestStep, Response

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import Mode, format_size, format_timestamp, status_is


class WeTransferFetcher(BaseFetcher):
    """
    has download notification: Yes, for the first download only
    has downloads count: Yes
    note: Authorized only links are supported, allowing specific recipients to download and it tracks every download.
    Downloading authorized links may require WeTransfer email verification before the download API can be used.
    """

    NAME = "WeTransfer"
    BASE_URL = "https://wetransfer.com"

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        is_wetransfer_host = host == "wetransfer.com" or host.endswith(
            ".wetransfer.com"
        )
        is_short_host = host == "we.tl" or host.endswith(".we.tl")
        return (is_wetransfer_host and parsed.path.startswith("/downloads/")) or (
            is_short_host and parsed.path not in {"", "/"}
        )

    @staticmethod
    def parse_downloads_url(url: str) -> dict:
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.rstrip("/").split("/")[2:]
        if len(path_parts) == 2:
            return {"transfer_id": path_parts[0], "security_hash": path_parts[1]}
        if len(path_parts) == 3:
            return {
                "transfer_id": path_parts[0],
                # Recipient-specific links are routed as:
                # /downloads/{transfer_id}/{recipient_id}/{security_hash}
                "recipient_id": path_parts[1],
                "security_hash": path_parts[2],
            }
        raise ValueError(
            f"Error: Unable to parse WeTransfer downloads URL: {parsed_url}"
        )

    def __init__(
        self,
        link: str,
        password: str | None = None,
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: No valid WeTransfer URL provided")

        self.link = link
        self.password = password
        self.short_url = self._is_short_url(link)

        super().__init__()

    def build_info_steps(self) -> list:
        steps = []
        if self.short_url:
            steps.append(
                RequestStep("resolve short url")
                .get(self.link)
                .headers(**self.headers)
                .capture("download_url", lambda response, vars: str(response.url))
                .check(status_is(200), "expected 200 response")
            )

        steps.append(
            RequestStep("check transfer status")
            .state(
                download_url=(
                    (lambda vars: vars["download_url"]) if self.short_url else self.link
                )
            )
            .before(lambda vars: self.parse_downloads_url(vars["download_url"]))
            .post(
                lambda vars: f"/api/v4/transfers/{vars['transfer_id']}/prepare-download"
            )
            .headers(**{**self.headers, "x-requested-with": "XMLHttpRequest"})
            .json(
                lambda vars: self.build_download_payload(
                    vars["security_hash"], vars.get("recipient_id")
                )
            )
            .after(lambda response, vars: {"metadata": self.parse_metadata(response)})
            .after(
                lambda response, vars: self.log_fetch_state(
                    vars["metadata"], vars["metadata"].get("number_of_downloads", 0)
                )
            )
            .check(status_is(200), "expected 200 response")
            .check(
                lambda response, vars: (
                    vars["metadata"].get("downloader_email_verification", "anonymous")
                    == "anonymous"
                ),
                "WeTransfer recipient link requires email verification before download",
            )
            .check(
                lambda response, vars: self.is_downloadable(vars["metadata"]),
                "expected transfer to be downloadable",
            )
        )
        return steps

    def build_fetch_steps(self) -> list:
        return [
            ConditionalStep(
                RequestStep("create direct link")
                .state(
                    download_url=(
                        (lambda vars: vars["download_url"])
                        if self.short_url
                        else self.link
                    )
                )
                .before(lambda vars: self.parse_downloads_url(vars["download_url"]))
                .post(lambda vars: f"/api/v4/transfers/{vars['transfer_id']}/download")
                .headers(**{**self.headers, "x-requested-with": "XMLHttpRequest"})
                .json(
                    lambda vars: self.build_download_payload(
                        vars["security_hash"], vars.get("recipient_id")
                    )
                )
                .capture(
                    "direct_link",
                    lambda response, vars: response.json().get("direct_link"),
                )
                .check(status_is(200), "expected 200 response")
            ).run_when(self.should_fetch),
            self.download_step(
                url_key="direct_link",
                filename_key=lambda vars: vars["metadata"]["recommended_filename"],
            ),
        ]

    def log_fetch_state(self, metadata: dict, downloads_count: int):
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "filename": metadata.get("recommended_filename"),
                "downloads_count": downloads_count,
                "upload_date": format_timestamp(metadata.get("uploaded_at")),
                "expires_at": format_timestamp(metadata.get("expires_at")),
                "size": format_size(metadata.get("size")),
                "from_email": metadata.get("from_email"),
                "state": metadata.get("state"),
                "downloader_email_verification": metadata.get(
                    "downloader_email_verification"
                ),
            },
            details={"metadata": metadata},
        )

    def parse_metadata(self, response: Response) -> dict:
        payload = response.json()
        return {
            "state": payload.get("state"),
            "uploaded_at": payload.get("uploaded_at"),
            "expires_at": payload.get("expires_at"),
            "deleted_at": payload.get("deleted_at"),
            "download_limit": payload.get("download_limit"),
            "number_of_downloads": payload.get("number_of_downloads"),
            "recommended_filename": payload.get("recommended_filename"),
            "size": payload.get("size"),
            "from_email": payload.get("creator", {}).get("email"),
            "password_protected": payload.get("password_protected"),
            "paid": payload.get("paid"),
            "downloader_email_verification": payload.get(
                "downloader_email_verification"
            ),
            "user_state": payload.get("user_state"),
        }

    def is_downloadable(self, metadata: dict) -> bool:
        if metadata.get("downloader_email_verification") != "anonymous":
            return False

        download_limit = metadata.get("download_limit")
        if (
            download_limit is not None
            and metadata.get("number_of_downloads", 0) >= download_limit
        ):
            return False

        expires_at = metadata.get("expires_at")
        if expires_at:
            expiry_date = datetime.datetime.fromisoformat(
                expires_at.replace("Z", "+00:00")
            )
            if datetime.datetime.now(datetime.timezone.utc) > expiry_date:
                return False

        return (
            metadata.get("state") == "downloadable"
            and metadata.get("deleted_at") is None
        )

    def build_download_payload(
        self, security_hash: str, recipient_id: str | None = None
    ) -> dict:
        payload = {"intent": "entire_transfer", "security_hash": security_hash}
        if recipient_id:
            payload["recipient_id"] = recipient_id
        return payload

    @staticmethod
    def _is_short_url(link: str) -> bool:
        host = (urlparse(link).hostname or "").lower()
        return host == "we.tl" or host.endswith(".we.tl")
