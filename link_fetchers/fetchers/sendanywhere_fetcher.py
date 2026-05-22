from __future__ import annotations

import base64
import json as json_mod
import re
from urllib.parse import parse_qs, urlparse

from httporchestrator import ConditionalStep, RequestStep, Response

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import Mode, format_timestamp, status_is


class SendAnywhereFetcher(BaseFetcher):
    """
    has download notification: No
    has downloads count: Yes, but not visible in the website
    """

    NAME = "SendAnywhere"
    BASE_URL = "https://send-anywhere.com"
    KEY_PATTERN = re.compile(r"^/web/(?:downloads|s)/([A-Za-z0-9]+)")

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        is_sendanywhere_host = host == "send-anywhere.com" or host.endswith(
            ".send-anywhere.com"
        )
        if is_sendanywhere_host:
            return bool(cls.KEY_PATTERN.search(parsed.path))
        if host == "sendanywhe.re" and parsed.path not in {"", "/"}:
            return True
        if host == "mandrillapp.com" and "sendanywhe.re" in url:
            return cls.extract_key_from_tracking(url) is not None
        return False

    def __init__(
        self,
        link: str,
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: No valid Send Anywhere URL provided")

        self.link = link
        self.key = self.extract_key(link)
        self.resolved_url = f"https://send-anywhere.com/web/downloads/{self.key}"

        super().__init__()

    def build_info_steps(self) -> list:
        return [
            RequestStep("register device")
            .post("/web/device")
            .json({"os_type": "web"})
            .check(status_is(200), "expected 200 response"),
            RequestStep("get key data")
            .get(f"/web/key/data/{self.key}")
            .after(lambda response, vars: self.extract_key_state(response))
            .after(
                lambda response, vars: self.log_fetch_state(
                    vars["metadata"], vars["downloads_count"]
                )
            )
            .check(status_is(200), "expected 200 response"),
        ]

    def build_fetch_steps(self) -> list:
        return [
            ConditionalStep(
                RequestStep("get relay file info")
                .post(f"/web/key/search/{self.key}")
                .json({})
                .after(
                    lambda response, vars: {
                        "metadata": self.update_metadata_from_search(
                            vars["metadata"], response
                        )
                    }
                )
                .capture(
                    "weblink", lambda response, vars: self.extract_weblink(response)
                )
                .check(status_is(200), "expected 200 response")
            ).run_when(
                lambda vars: self.should_fetch(
                    vars,
                    when=lambda values: values.get("is_relay") is True,
                )
            ),
            self.download_step(
                name="download relay file",
                url_key="weblink",
                filename_key=lambda _vars: f"SendAnywhere-{self.key}",
                capture_filename="filename",
                when=lambda vars: (
                    vars.get("is_relay") is True and bool(vars.get("weblink"))
                ),
            ),
            ConditionalStep(
                RequestStep("prepare download")
                .post(f"/web/key/download/prepare/{self.key}")
                .json(lambda vars: {"files": vars["file_uuids"]})
                .capture(
                    "secret_key",
                    lambda response, vars: self.extract_s3_secret(response),
                )
                .check(status_is(200), "expected 200 response")
            ).run_when(
                lambda vars: self.should_fetch(
                    vars,
                    when=lambda values: values.get("is_relay") is False,
                )
            ),
            ConditionalStep(
                RequestStep("get download URL")
                .post(f"/web/key/download/url/{self.key}")
                .json(
                    lambda vars: {
                        "files": vars["file_uuids"],
                        "secret_key": vars["secret_key"],
                    }
                )
                .capture(
                    "download_url",
                    lambda response, vars: self.extract_s3_download_url(response),
                )
                .check(status_is(200), "expected 200 response")
            ).run_when(
                lambda vars: self.should_fetch(
                    vars,
                    when=lambda values: values.get("is_relay") is False,
                )
            ),
            self.download_step(
                name="download file",
                url_key="download_url",
                filename_key=lambda _vars: f"SendAnywhere-{self.key}",
                capture_filename="filename",
                when=lambda vars: (
                    vars.get("is_relay") is False and bool(vars.get("download_url"))
                ),
            ),
        ]

    def log_fetch_state(self, metadata: dict, downloads_count: int | None):
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "key": self.key,
                "downloads_count": downloads_count,
                "state": metadata.get("state"),
                "created_time": format_timestamp(metadata.get("created_time")),
                "expires_time": format_timestamp(metadata.get("expires_time")),
            },
            details={"metadata": metadata},
        )

    def extract_key_state(self, response: Response) -> dict:
        data = response.json() or {}
        files = data.get("files") or []
        key_data = {
            "key": data.get("key", self.key),
            "server": data.get("server"),
            "link": data.get("link"),
            "device_id": data.get("device_id"),
            "created_time": data.get("created_time"),
            "expires_time": data.get("expires_time"),
            "download_count": data.get("download_count"),
            "use_storage": data.get("use_storage"),
            "is_relay": isinstance(data.get("key"), str)
            and isinstance(data.get("server"), str),
            "file_uuids": [
                item.get("file_uuid") for item in files if item.get("file_uuid")
            ],
            "state": "available",
            "url": self.resolved_url,
        }
        return {
            "metadata": {
                "key": key_data.get("key", self.key),
                "downloads_count": key_data.get("download_count"),
                "state": key_data.get("state", "available"),
                "created_time": key_data.get("created_time"),
                "expires_time": key_data.get("expires_time"),
                "url": self.resolved_url,
                "is_relay": key_data.get("is_relay"),
            },
            "downloads_count": key_data.get("download_count"),
            "is_relay": key_data.get("is_relay", False),
            "file_uuids": key_data.get("file_uuids") or [],
        }

    def update_metadata_from_search(self, metadata: dict, response: Response) -> dict:
        payload = response.json() or {}
        updated = dict(metadata)
        updated["file_count"] = payload.get("file_count", updated.get("file_count"))
        updated["total_size"] = payload.get("file_size", updated.get("total_size"))
        return updated

    def extract_weblink(self, response: Response) -> str:
        weblink = (response.json() or {}).get("weblink", "")
        if not weblink:
            raise ValueError("Error: No weblink in search response")
        return weblink

    def extract_s3_secret(self, response: Response) -> str:
        secret = (response.json() or {}).get("secret_key", "")
        if not secret:
            raise ValueError("Error: No secret_key in prepare response")
        return secret

    def extract_s3_download_url(self, response: Response) -> str:
        payload = response.json() or []
        if isinstance(payload, list) and payload:
            return payload[0].get("url", "")
        raise ValueError("Error: No download URL in response")

    def extract_key(self, link: str) -> str:
        parsed = urlparse(link)
        host = (parsed.hostname or "").lower()
        is_sendanywhere_host = host == "send-anywhere.com" or host.endswith(
            ".send-anywhere.com"
        )

        if is_sendanywhere_host:
            match = self.KEY_PATTERN.search(parsed.path)
            if match:
                return match.group(1)

        if host == "sendanywhe.re" and parsed.path not in {"", "/"}:
            return parsed.path.strip("/")

        key = self.extract_key_from_tracking(link)
        if key:
            return key
        raise ValueError("Error: No valid Send Anywhere URL provided")

    @staticmethod
    def extract_key_from_tracking(link: str) -> str | None:
        parsed = urlparse(link)
        p_values = parse_qs(parsed.query).get("p", [])
        if not p_values:
            return None
        try:
            raw = p_values[0] + ("=" * (-len(p_values[0]) % 4))
            payload = json_mod.loads(base64.b64decode(raw))
            inner = json_mod.loads(payload.get("p", "{}"))
            url = inner.get("url", "")
            return urlparse(url).path.strip("/") or None
        except Exception:
            return None
