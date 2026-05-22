from __future__ import annotations

import os
import re
import zipfile
from urllib.parse import urlparse

from httporchestrator import ConditionalStep, RequestStep, Response
from loguru import logger

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import (
    Mode,
    format_size,
    format_timestamp,
    status_is,
    variable_is,
)


class TransferXLFetcher(BaseFetcher):
    """
    has download notification: Yes, but only for the first download
    has downloads count: No
    note: Using the management link still triggers download notifications, but it also sign you in to the uploader account
    """

    NAME = "TransferXL"
    BASE_URL = "https://api.transferxl.com/api/v2"
    URL_PATTERN = re.compile(r"/download/([0-9A-Fa-f]{2}[a-zA-Z0-9]+)")

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        is_transferxl_host = host == "transferxl.com" or host.endswith(
            ".transferxl.com"
        )
        return is_transferxl_host and bool(cls.URL_PATTERN.search(parsed.path))

    def __init__(
        self,
        link: str,
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: No valid TransferXL URL provided")

        self.link = link
        self.transfer_id = self.URL_PATTERN.search(urlparse(link).path).group(1)

        super().__init__()

    def build_info_steps(self) -> list:
        return [
            RequestStep("load transfer metadata")
            .get("/history/download")
            .headers(**self.headers)
            .params(
                shortUrl=self.transfer_id, perFilePendingStatus="true", language="en"
            )
            .after(lambda response, vars: {"metadata": self.parse_metadata(response)})
            .after(
                lambda response, vars: {
                    "filename": vars["metadata"].get("filename")
                    or f"TransferXL-{self.transfer_id}.zip",
                    "downloads_count": vars["metadata"].get("download_count"),
                    "available": vars["metadata"].get("state") == "available",
                }
            )
            .after(
                lambda response, vars: self.log_fetch_state(
                    vars["metadata"], vars["downloads_count"]
                )
            )
            .check(status_is(200), "expected 200 response")
            .check(variable_is("available", True), "expected transfer to be available")
        ]

    def build_fetch_steps(self) -> list:
        return [
            ConditionalStep(
                RequestStep("create download token")
                .post("/download/getToken")
                .headers(**self.headers)
                .json({"shortUrl": self.transfer_id})
                .capture(
                    "direct_link",
                    lambda response, vars: self.extract_direct_link(
                        vars["metadata"], response
                    ),
                )
                .check(status_is(200), "expected 200 response")
            ).run_when(
                lambda vars: self.should_fetch(
                    vars, when=lambda values: values.get("available")
                )
            ),
            self.download_step(when=lambda vars: vars.get("available")),
        ]

    def log_fetch_state(self, metadata: dict, downloads_count: int | None):
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "transfer_id": self.transfer_id,
                "filename": metadata.get("filename"),
                "file_count": metadata.get("file_count"),
                "size": format_size(metadata.get("size")),
                "from_email": metadata.get("from_email"),
                "to_email": metadata.get("to_email"),
                "available_until": format_timestamp(metadata.get("available_until")),
                "downloads_count": downloads_count,
                "state": metadata.get("state"),
                "download_url": metadata.get("download_url"),
            },
            details={"metadata": metadata},
        )

    def parse_metadata(self, response: Response) -> dict:
        data = response.json() or {}
        files = data.get("files") or []
        primary_file = files[0] if files else {}
        status = data.get("status")
        result = data.get("result")
        return {
            "id": data.get("id") or self.transfer_id,
            "transfer_id": data.get("id") or self.transfer_id,
            "share_url": data.get("shareUrl") or self.link,
            "region": data.get("region"),
            "download_url": data.get("url"),
            "transfer": data.get("transfer"),
            "message": data.get("message"),
            "file_count": data.get("fileCount") or len(files),
            "size": data.get("size"),
            "from_email": data.get("from"),
            "to_email": data.get("to_email"),
            "status": status,
            "type": data.get("type"),
            "created_at": data.get("createdAt"),
            "encrypted": data.get("encrypted"),
            "is_pending": data.get("isPending"),
            "available_until": data.get("availableUntil"),
            "download_count": data.get("downloadCount"),
            "files": files,
            "filename": primary_file.get("name")
            or f"TransferXL-{self.transfer_id}.zip",
            "primary_file": primary_file,
            "state": (
                "available"
                if result == "ok" and status == "AVAILABLE"
                else "unavailable"
            ),
            "url": self.link,
        }

    def extract_direct_link(self, metadata: dict, response: Response) -> str:
        token = (response.json() or {}).get("downloadToken")
        base_url = metadata.get("download_url")
        if not token or not base_url:
            raise ValueError("Error: TransferXL download token not found")
        return f"{base_url}?downloadToken={token}"

    def save_file(self, response: object, fallback_filename: str) -> dict:
        path = super().save_file(response, fallback_filename).get("local_file_path")
        if not path:
            return {}
        if not zipfile.is_zipfile(path):
            return {"local_file_path": path}
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if len(names) != 1:
                return {"local_file_path": path}
            inner_name = names[0]
            inner_bytes = zf.read(inner_name)
        destination = os.path.join(os.path.dirname(path), os.path.basename(inner_name))
        with open(destination, "wb") as after:
            after.write(inner_bytes)
        os.remove(path)
        logger.success(
            "[{}] extracted {} from ZIP ({} bytes)",
            self.NAME,
            destination,
            len(inner_bytes),
        )
        return {"local_file_path": destination}
