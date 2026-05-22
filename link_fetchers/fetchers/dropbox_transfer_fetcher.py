from __future__ import annotations

import json
import mimetypes
import os
import re
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse, urlunparse

from httporchestrator import ConditionalStep, RequestStep, Response

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import Mode, resolve_filename


class DropboxTransferFetcher(BaseFetcher):
    """
    has download notification: No
    has downloads count: No
    note: some Dropbox shared-content links are recipient-gated and require auth
    """

    NAME = "DropboxTransfer"
    BASE_URL = "https://www.dropbox.com"
    DOWNLOAD_PATH = "/2/sharing/get_shared_link_file"
    AUTHENTICATED_DOWNLOAD_URL = (
        "https://content.dropboxapi.com/2/sharing/get_shared_link_file"
    )

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host == "dropbox.com" or host.endswith(".dropbox.com")

    def __init__(
        self,
        link: str,
        *,
        access_token: str | None = None,
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: No valid Dropbox Transfer URL provided")

        self.link = link
        self.access_token = (access_token or "").strip()
        self.request_headers = self.initial_headers()

        super().__init__()

    def build_info_steps(self) -> list:
        return [
            RequestStep("resolve shared link")
            .get(self.link)
            .headers(**self.request_headers)
            .after(lambda response, vars: self.capture_shared_link_context(response))
            .check(
                lambda response, vars: response.status_code == 200,
                "expected 200 response",
            ),
            ConditionalStep(self.build_probe_step()).run_when(
                lambda vars: vars.get("probe_required", False)
            ),
        ]

    def build_fetch_steps(self) -> list:
        return [
            ConditionalStep(
                RequestStep("download")
                .post(self.download_endpoint())
                .headers(**self.api_headers(include_range=False))
                .data(b"")
                .after(
                    lambda response, vars: {
                        "path": self.save_file(response, vars["filename"])
                    }
                )
                .after(lambda response, vars: {"downloaded": bool(vars.get("path"))})
                .check(
                    lambda response, vars: response.status_code == 200,
                    "expected 200 response",
                )
            ).run_when(
                lambda vars: self.should_fetch(
                    vars,
                    downloads_count=1,
                    when=lambda values: values.get("available"),
                )
            )
        ]

    def build_probe_step(self) -> RequestStep:
        return (
            RequestStep("probe shared link")
            .post(self.download_endpoint())
            .headers(**self.api_headers(include_range=True))
            .data(b"")
            .after(lambda response, vars: self.extract_file_state(response, vars))
            .after(lambda response, vars: self.log_fetch_state(vars["metadata"], None))
            .check(
                lambda response, vars: response.status_code in {200, 206, 409},
                "expected Dropbox probe response",
            )
        )

    def api_headers(self, *, include_range: bool) -> dict:
        headers = {
            **self.request_headers,
            "Dropbox-API-Arg": lambda vars: json.dumps(
                {"url": vars["resolved_url"]}, separators=(",", ":")
            ),
            "Referer": lambda vars: vars["resolved_url"],
        }
        if include_range:
            headers["Range"] = "bytes=0-0"
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
            return headers
        headers["X-CSRF-Token"] = lambda vars: vars["csrf_token"]
        headers["X-Dropbox-Uid"] = "-1"
        return headers

    def download_endpoint(self) -> str:
        return (
            self.AUTHENTICATED_DOWNLOAD_URL if self.access_token else self.DOWNLOAD_PATH
        )

    def capture_shared_link_context(self, response: Response) -> dict:
        resolved_url = str(response.url)
        filename = self.extract_filename_from_url(resolved_url)

        if self.is_recipient_gated(resolved_url, response.text):
            metadata = self.build_metadata(
                resolved_url,
                filename,
                content_type=mimetypes.guess_type(filename)[0],
                state="recipient_gated",
                provider_state="shared_link_access_denied",
                access_required=True,
                restriction="invited_account_required",
                message="Dropbox requires an invited account session or access token for this shared link",
            )
            self.log_fetch_state(metadata, None)
            return {
                "metadata": metadata,
                "filename": filename,
                "available": False,
                "probe_required": False,
            }

        return {
            "resolved_url": resolved_url,
            "fallback_filename": filename,
            "csrf_token": self.extract_csrf_token(response),
            "probe_required": True,
        }

    def is_recipient_gated(self, resolved_url: str, html: str) -> bool:
        if self.access_token:
            return False

        original_path = urlparse(self.link).path.rstrip("/")
        resolved = urlparse(resolved_url)
        query = parse_qs(resolved.query)

        return original_path.startswith("/l/scl/") or (
            resolved.path.startswith("/scl/fi/")
            and "r" in query
            and "rlkey" not in query
        )

    def extract_csrf_token(self, response: Response) -> str:
        raw_responses = [*getattr(response.raw, "history", []), response.raw]
        for raw in raw_responses:
            token = raw.cookies.get("__Host-js_csrf") or raw.cookies.get("t")
            if token:
                return token

        for raw in raw_responses:
            set_cookie = raw.headers.get("set-cookie", "")
            match = re.search(r"(?:__Host-js_csrf|t)=([^;,\s]+)", set_cookie)
            if match:
                return match.group(1)

        raise ValueError("Error: Dropbox shared link did not provide a CSRF token")

    def extract_metadata(self, response: Response) -> dict:
        resolved_url = str(response.url)
        fallback_filename = self.extract_filename_from_url(resolved_url)
        variables = {
            "resolved_url": resolved_url,
            "fallback_filename": fallback_filename,
        }

        if "text/html" in response.headers.get("content-type", "").lower():
            metadata = self.build_metadata(
                resolved_url,
                resolve_filename(response.headers, fallback_filename),
                content_type=response.headers.get("content-type"),
                state="preview_only",
                download_url=None,
            )
            return {
                "metadata": metadata,
                "filename": metadata["filename"],
                "available": False,
            }

        state = self.extract_file_state(response, variables)
        state["metadata"].setdefault(
            "download_url", self.direct_link if state["available"] else None
        )
        return state

    def extract_file_state(self, response: Response, variables: dict) -> dict:
        resolved_url = variables.get("resolved_url") or self.link
        fallback_filename = variables.get(
            "fallback_filename"
        ) or self.extract_filename_from_url(resolved_url)

        if response.status_code in {200, 206}:
            payload = self.extract_api_result(response)
            filename = payload.get("name") or resolve_filename(
                response.headers, fallback_filename
            )
            content_length = response.headers.get("content-length")
            size = (
                payload.get("size") or int(content_length) if content_length else None
            )

            metadata = self.build_metadata(
                resolved_url,
                filename,
                content_type=response.headers.get("content-type")
                or mimetypes.guess_type(filename)[0],
                size=size,
                state="available",
                raw_metadata=payload or None,
            )
            return {
                "metadata": metadata,
                "filename": filename,
                "available": True,
            }

        error, error_tag = self.extract_error(response)
        metadata = self.build_metadata(
            resolved_url,
            fallback_filename,
            content_type=mimetypes.guess_type(fallback_filename)[0],
            state=(
                "access_restricted"
                if error_tag == "shared_link_access_denied"
                else error_tag
            ),
            provider_state=error_tag,
            access_required=error_tag == "shared_link_access_denied",
            restriction=(
                "shared_link_access_denied"
                if error_tag == "shared_link_access_denied"
                else None
            ),
            error=error,
        )
        self.log_json("fetch error", {"resolved_url": resolved_url, "error": error})
        return {
            "metadata": metadata,
            "filename": fallback_filename,
            "available": False,
            "error": error,
        }

    def extract_api_result(self, response: Response) -> dict:
        raw_metadata = response.headers.get(
            "dropbox-api-result"
        ) or response.headers.get("x-dropbox-metadata")
        if not raw_metadata:
            return {}
        try:
            return json.loads(raw_metadata)
        except ValueError:
            return {}

    def extract_error(self, response: Response) -> tuple[dict, str]:
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text.strip()}
        payload["status_code"] = response.status_code
        error_tag = (
            payload.get("error", {}).get(".tag")
            or str(payload.get("error_summary", "")).split("/", 1)[0]
            or "unavailable"
        )
        return payload, error_tag

    def build_metadata(
        self,
        resolved_url: str,
        filename: str,
        *,
        content_type: str | None = None,
        size: int | None = None,
        state: str = "unavailable",
        **extra,
    ) -> dict:
        return {
            "filename": filename,
            "content_type": content_type,
            "size": size,
            "path_display": urlparse(resolved_url).path,
            "state": state,
            "url": resolved_url,
            **extra,
        }

    def log_fetch_state(self, metadata: dict, downloads_count: int | None):
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "filename": metadata.get("filename"),
                "downloads_count": downloads_count,
                "content_type": metadata.get("content_type"),
                "state": metadata.get("state"),
                "provider_state": metadata.get("provider_state"),
                "access_required": metadata.get("access_required"),
                "restriction": metadata.get("restriction"),
                "resolved_url": metadata.get("url"),
            },
            details={"metadata": metadata},
        )

    @property
    def direct_link(self) -> str:
        parsed = urlparse(self.link)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["dl"] = "1"
        return urlunparse(parsed._replace(query=urlencode(query)))

    def extract_filename_from_url(self, url: str) -> str:
        filename = os.path.basename(urlparse(url).path.rstrip("/"))
        return unquote(filename) or "dropbox-download"
