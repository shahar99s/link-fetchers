from __future__ import annotations

import datetime
import os
import re

from httporchestrator import ConditionalStep, RequestStep, Response

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import Mode, status_is, variable_is


class SendgbFetcher(BaseFetcher):
    """
    has download notification: Yes, for the first download only
    has downloads count: Yes
    """

    NAME = "SendGB"
    BASE_URL = "https://www.sendgb.com"
    URL_PATTERN = re.compile(
        r"(?:https?://)(?:www\.)?sendgb\.com/(?:upload/\?utm_source=)?([0-9a-zA-Z]+)"
    )

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        return bool(cls.URL_PATTERN.search(url))

    def __init__(
        self,
        link: str,
        password: str | None = None,
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: Invalid SendGB URL provided")

        self.link = link
        self.password = password
        self.upload_id = self.URL_PATTERN.search(link).group(1)

        super().__init__()

    def build_info_steps(self) -> list:
        return [
            RequestStep("get upload page")
            .get(f"/upload/?utm_source={self.upload_id}")
            .headers(**self.headers)
            .after(lambda response, vars: self.extract_page_state(response))
            .after(
                lambda response, vars: self.log_fetch_state(
                    vars["metadata"], vars["downloads_count"]
                )
            )
            .after(
                lambda response, vars: self.save_if_direct(response, vars["metadata"])
            )
            .check(status_is(200), "expected 200 response")
            .check(
                variable_is("available", True),
                "Error: SendGB transfer expired or deleted",
            )
        ]

    def build_fetch_steps(self) -> list:
        return [
            ConditionalStep(
                RequestStep("create direct link")
                .get(
                    lambda vars: (
                        f"/src/download_one.php?uploadId={self.upload_id}&sc={vars['secret_code']}"
                        f"&file={vars['file']}&private_id={vars['private_id']}"
                    )
                )
                .headers(**self.headers)
                .capture("direct_link", lambda response, vars: response.json()["url"])
                .check(status_is(200), "expected 200 response")
                .check(
                    lambda response, vars: response.json().get("success") is True,
                    "expected direct link success",
                )
            ).run_when(
                lambda vars: self.should_fetch(
                    vars, when=lambda values: not values.get("direct_download")
                )
            ),
            self.download_step(
                filename_key="fallback_filename",
                when=lambda vars: not vars.get("direct_download"),
            ),
        ]

    def log_fetch_state(self, metadata: dict, downloads_count: int | None):
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "filename": metadata.get("filename")
                or metadata.get("file")
                or metadata.get("fallback_filename"),
                "downloads_count": downloads_count,
                "direct_download": metadata.get("direct_download"),
                "expires_at": metadata.get("deletion_date"),
                "state": metadata.get("is_deleted"),
            },
            details={"metadata": metadata},
        )

    def extract_page_state(self, response: Response) -> dict:
        direct_download = any(
            key.lower() == "content-disposition" for key in response.headers
        )
        deletion_date = self.extract_deletion_date(response)
        is_deleted = self.is_expired_page(response)
        if deletion_date:
            parsed_date = datetime.datetime.strptime(deletion_date, "%d.%m.%Y").date()
            is_deleted = is_deleted or datetime.datetime.now().date() > parsed_date

        filename = self.extract_display_filename(response)
        file_attr = self.extract_file_attr(response)
        secret_code = self.extract_secret_code(response)
        private_id = self.extract_private_id(response)
        metadata = {
            "id": self.upload_id,
            "direct_download": direct_download,
            "secret_code": secret_code,
            "file": file_attr,
            "filename": filename,
            "private_id": private_id,
            "deletion_date": deletion_date,
            "is_deleted": is_deleted,
        }
        fallback_filename = self.build_fallback_filename(metadata)
        metadata["fallback_filename"] = fallback_filename
        return {
            "downloads_count": None,
            "direct_download": direct_download,
            "secret_code": secret_code,
            "file": file_attr,
            "private_id": private_id,
            "is_deleted": is_deleted,
            "available": not is_deleted,
            "fallback_filename": fallback_filename,
            "metadata": metadata,
        }

    def is_expired_page(self, response: Response) -> bool:
        expired_markers = (
            "There are currently no files available for download.",
            "This transfer has expired",
            "permanently removed from our servers",
        )
        text = response.text or ""
        return any(marker in text for marker in expired_markers)

    def extract_secret_code(self, response: Response) -> str | None:
        match = re.search(r'id="secret_code" value="([^"]*)"', response.text)
        return match.group(1) if match else None

    def extract_file_attr(self, response: Response) -> str | None:
        match = re.search(r'data-file="([^\"]+)"', response.text)
        return match.group(1) if match else None

    def extract_display_filename(self, response: Response) -> str | None:
        patterns = [
            r'data-filename="([^"]+)"',
            r"<title>\s*([^<]+?)\s*(?:\||-)\s*SendGB",
        ]
        for pattern in patterns:
            match = re.search(pattern, response.text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    def extract_private_id(self, response: Response) -> str:
        match = re.search(r'data-private_id="([^\"]*)"', response.text)
        return match.group(1) if match else None

    def extract_deletion_date(self, response: Response) -> str | None:
        match = re.search(
            r'<div class="fw-bold">Deletion Date</div>\s*([0-9]{2}\.[0-9]{2}\.[0-9]{4})',
            response.text,
        )
        return match.group(1) if match else None

    def build_fallback_filename(self, metadata: dict) -> str:
        for candidate in (metadata.get("filename"), metadata.get("file")):
            normalized = os.path.basename((candidate or "").strip())
            if (
                normalized
                and "." in normalized
                and all(sep not in normalized for sep in ("/", "\\", "?", "&", "="))
            ):
                return normalized
        return f"sendgb-{self.upload_id}.bin"

    def save_if_direct(self, response: Response, metadata: dict):
        if metadata.get("direct_download") and self.should_fetch({}, downloads_count=1):
            fallback_filename = metadata.get(
                "fallback_filename"
            ) or self.build_fallback_filename(metadata)
            self.save_file(response, fallback_filename)
