from __future__ import annotations

from urllib.parse import urlparse

from httporchestrator import ConditionalStep, RequestStep, Response

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import (
    Mode,
    format_size,
    format_timestamp,
    should_download,
    status_is,
    variable_is,
)


class FilemailFetcher(BaseFetcher):
    """
    has download notification: Yes, for the first download only
    has downloads count: Yes
    note: This fetcher bypasses the downloads counter and download notifications
    """

    NAME = "Filemail"
    BASE_URL = "https://api.filemail.com"

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        is_filemail_host = host == "filemail.com" or host.endswith(".filemail.com")
        return is_filemail_host and parsed.path.startswith("/d/")

    def __init__(
        self,
        link: str,
        password: str | None = None,
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: No valid Filemail URL provided")

        self.link = link
        self.password = password
        self.headers = {
            **self.initial_headers(),
            "x-api-source": "WebApp",
            "x-api-version": "2.0",
        }

        super().__init__()

    def build_info_steps(self) -> list:
        return [
            RequestStep("resolve transfer")
            .post("/transfer/find")
            .headers(**self.headers)
            .json(lambda vars: self.build_lookup_payload())
            .after(
                lambda response, vars: {
                    "transfer": self.extract_transfer_data(response)
                }
            )
            .after(lambda response, vars: self.build_transfer_state(vars["transfer"]))
            .after(
                lambda response, vars: self.log_fetch_state(
                    vars["metadata"],
                    vars["downloads_count"],
                    vars["filename"],
                    vars["primary_file"],
                    vars["transfer"],
                )
            )
            .check(status_is(200), "expected 200 response")
            .check(variable_is("available", True), "expected file to be available")
        ]

    def build_fetch_steps(self) -> list:
        return [
            ConditionalStep(
                RequestStep("download")
                .get(lambda vars: vars["direct_link"])
                .headers(**self.headers)
                .check(
                    lambda response, vars: (
                        vars.get("metadata", {}).get("is_expired") is False
                    ),
                    "Error: Filemail file expired",
                )
                .check(status_is(200), "expected 200 response")
                .after(
                    lambda response, vars: self.save_file(response, vars["filename"])
                )
            ).run_when(
                lambda vars: should_download(
                    self.mode,
                    1,  # This script bypass the downloads count, so we use 1 as a placeholder to indicate that the file can be downloaded
                )
            )
        ]

    def log_fetch_state(
        self,
        metadata: dict,
        downloads_count=None,
        filename=None,
        primary_file=None,
        transfer=None,
    ):
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "filename": filename or (primary_file or {}).get("filename"),
                "downloads_count": downloads_count,
                "uploader_email": metadata.get("email") or metadata.get("from"),
                "upload_date": format_timestamp(transfer.get("sentdate", None)),
                "expires_at": format_timestamp(transfer.get("expiredate", None)),
                "size": format_size(
                    metadata.get("size") or (primary_file or {}).get("filesize")
                ),
                "url": metadata.get("url"),
            },
            details={
                "metadata": metadata,
                "primary_file": primary_file,
                "transfer": transfer,
            },
        )

    def build_lookup_payload(self) -> dict:
        payload = {"url": self.link}
        if self.password:
            payload["password"] = self.password
        return payload

    def extract_transfer_data(self, response: Response) -> dict:
        data = response.json().get("data")
        if not data:
            raise ValueError("Error: Filemail transfer data not found")
        return data

    def build_transfer_state(self, transfer: dict) -> dict:
        files = transfer.get("files") or []

        primary_file = files[0] if files else {}
        metadata = {
            "id": transfer.get("id"),
            "url": transfer.get("url"),
            "status": transfer.get("status"),
            "subject": transfer.get("subject"),
            "message": transfer.get("message"),
            "size": transfer.get("size"),
            "from": transfer.get("from"),
            "number_of_files": transfer.get("numberoffiles"),
            "number_of_downloads": transfer.get("numberofdownloads"),
            "is_expired": transfer.get("isexpired"),
            "password_protected": transfer.get("passwordprotected"),
            "block_downloads": transfer.get("blockdownloads"),
            "infected": transfer.get("infected"),
            "compressed_file_url": transfer.get("compressedfileurl"),
        }

        number_of_files = metadata.get("number_of_files") or 0
        if number_of_files > 1 and metadata.get("compressed_file_url"):
            filename = f"{metadata.get('subject') or metadata.get('id') or 'filemail-transfer'}.zip"
            base_url = metadata["compressed_file_url"]
        else:
            filename = primary_file.get("filename") or None
            base_url = primary_file.get("downloadurl")

        direct_link = None
        if base_url:
            separator = "&" if "?" in base_url else "?"
            direct_link = f"{base_url}{separator}skipcheck=true&skipreg=true"

        return {
            "metadata": metadata,
            "primary_file": primary_file,
            "filename": filename,
            "direct_link": direct_link,
            "downloads_count": metadata.get("number_of_downloads") or 0,
            "available": (
                metadata.get("status") == "STATUS_COMPLETE"
                and metadata.get("block_downloads") is False
            ),
        }
