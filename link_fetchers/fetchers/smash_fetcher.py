from __future__ import annotations

import base64
from urllib.parse import parse_qs, quote, urlparse

from httporchestrator import RequestStep, Response

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import Mode, format_size, status_is, variable_is


class FromSmashFetcher(BaseFetcher):
    """
    has download notification: Determined from transfer.notification
    has downloads count: No
    note: This fetcher bypasses the downloads counter and download notifications
    """

    NAME = "FromSmash"
    BASE_URL = "https://fromsmash.com"

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        normalized_path = parsed.path.rstrip("/")
        host = (parsed.hostname or "").lower()
        is_smash_host = host == "fromsmash.com" or host.endswith(".fromsmash.com")
        return is_smash_host and normalized_path not in {"", "/"}

    def __init__(
        self,
        link: str,
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: No valid FromSmash URL provided")

        self.link = link

        parsed = urlparse(link)
        normalized_path = parsed.path.rstrip("/")
        query_params = parse_qs(parsed.query)
        self.identity_token = query_params.get("e", [None])[0]
        self.transfer_id = normalized_path.split("/")[-1]
        self.target_id = f"{parsed.netloc}{normalized_path}"
        self.encoded_target_id = quote(self.target_id, safe="")

        super().__init__()

    def build_info_steps(self) -> list:
        return [
            RequestStep("discover FromSmash public service endpoint")
            .get("https://discovery.fromsmash.co/namespace/public/services")
            .headers(**self.headers)
            .params(version="10-2019")
            .capture("region", lambda response, vars: self.extract_region(response))
            .check(status_is(200), "expected 200 response"),
            RequestStep("create anonymous FromSmash account")
            .post(lambda vars: f"https://iam.{vars['region']}.fromsmash.co/account")
            .headers(**self.headers)
            .json({})
            .capture(
                "account_token",
                lambda response, vars: self.extract_account_token(response),
            )
            .check(status_is(201), "expected 201 response"),
            RequestStep("resolve FromSmash transfer target")
            .get(f"https://link.fromsmash.co/target/{self.encoded_target_id}")
            .headers(
                **self.headers,
                Authorization=lambda vars: f"Bearer {vars['account_token']}",
            )
            .params(version="10-2019")
            .after(lambda response, vars: {"target": self.extract_target(response)})
            .after(
                lambda response, vars: {
                    "transfer_region": self.extract_transfer_region(vars["target"]),
                    "public_transfer_id": self.extract_public_transfer_id(
                        vars["target"]
                    ),
                }
            )
            .check(status_is(200), "expected 200 response"),
            RequestStep("load FromSmash transfer preview")
            .get(
                lambda vars: (
                    f"https://transfer.{vars['transfer_region']}.fromsmash.co/transfer/{vars['public_transfer_id']}/preview"
                )
            )
            .headers(
                **self.headers,
                Authorization=lambda vars: f"Bearer {vars['account_token']}",
            )
            .params(version="01-2024", e=self.identity_token)
            .after(
                lambda response, vars: self.extract_transfer_state(
                    response, vars["target"]
                )
            )
            .check(status_is(200), "expected 200 response"),
            RequestStep("load FromSmash transfer files preview")
            .get(
                lambda vars: (
                    f"https://transfer.{vars['transfer_region']}.fromsmash.co/transfer/{vars['public_transfer_id']}/files/preview"
                )
            )
            .headers(
                **self.headers,
                Authorization=lambda vars: f"Bearer {vars['account_token']}",
            )
            .params(version="01-2024", e=self.identity_token)
            .after(
                lambda response, vars: {
                    "files_metadata": self.extract_files_metadata(response)
                }
            )
            .after(
                lambda response, vars: {
                    "filename": self.extract_filename(
                        vars["files_metadata"], vars["transfer_metadata"]
                    ),
                }
            )
            .after(
                lambda response, vars: self.log_fetch_state(
                    vars["transfer_metadata"],
                    vars["files_metadata"],
                    vars["downloads_count"],
                )
            )
            .check(status_is(200), "expected 200 response")
            .check(variable_is("available", True), "expected transfer to be available"),
        ]

    def build_fetch_steps(self) -> list:
        return [
            self.download_step(
                url_key="download_url",
                downloads_count=1,
            )
        ]

    def log_fetch_state(
        self, transfer_metadata: dict, files_metadata: dict, downloads_count: int | None
    ):
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "filenames": list(files_metadata.keys()),
                "downloads_count": downloads_count,
                "transfer_name": transfer_metadata.get("transfer_name"),
                "transfer_size": format_size(transfer_metadata["transfer_size"]),
                "file_count": transfer_metadata.get("file_count"),
                "state": transfer_metadata.get("state"),
                "expires_at": transfer_metadata.get("availability_end_date"),
                "has_download_notification": transfer_metadata.get(
                    "has_download_notification"
                ),
                "from_email": transfer_metadata.get("identity_email"),
                "notification_channels": transfer_metadata.get("notification_channels"),
            },
            details={
                "transfer_metadata": transfer_metadata,
                "files_metadata": files_metadata,
            },
        )

    def extract_region(self, response: Response) -> str:
        region = (response.json() or {}).get("region")
        if not region:
            raise ValueError("Error: FromSmash discovery region not found")
        return region

    def extract_account_token(self, response: Response) -> str:
        payload = response.json() or {}
        account = payload.get("account") or payload.get("identity") or payload
        token = (account.get("token") or {}).get("token")
        if not token:
            raise ValueError("Error: FromSmash anonymous account token not found")
        return token

    def extract_target(self, response: Response) -> dict:
        target = (response.json() or {}).get("target") or {}
        if not target.get("target"):
            raise ValueError("Error: FromSmash target resolution failed")
        return target

    def extract_transfer_region(self, target: dict) -> str:
        region = (target or {}).get("region")
        if not region:
            raise ValueError("Error: FromSmash transfer region not found")
        return region

    def extract_public_transfer_id(self, target: dict) -> str:
        public_transfer_id = (target or {}).get("target")
        if not public_transfer_id:
            raise ValueError("Error: FromSmash transfer id not found")
        return public_transfer_id

    def decode_identity_email(self, encoded_identity: str | None) -> str | None:
        if not encoded_identity:
            return None
        normalized = encoded_identity.strip()
        normalized += "=" * ((-len(normalized)) % 4)
        try:
            return base64.b64decode(normalized).decode("utf-8")
        except Exception:
            return None

    def extract_notification_channels(self, transfer: dict) -> list[str]:
        notification = transfer.get("notification") or {}
        return [
            channel
            for channel, config in notification.items()
            if isinstance(config, dict) and config.get("enabled")
        ]

    def extract_transfer_state(self, response: Response, target: dict) -> dict:
        transfer = response.json()["transfer"]
        notification_channels = self.extract_notification_channels(transfer)
        download_url = transfer.get("download")
        transfer_metadata = {
            "transfer_id": target.get("target") or self.transfer_id,
            "transfer_name": transfer.get("title"),
            "file_count": transfer.get("filesNumber"),
            "region": target.get("region"),
            "download_url": download_url,
            "availability_start_date": transfer.get("availabilityStartDate"),
            "availability_end_date": transfer.get("availabilityEndDate"),
            "availability_duration": transfer.get("availabilityDuration"),
            "created": transfer.get("created"),
            "notification_channels": notification_channels,
            "has_download_notification": "download" in notification_channels,
            "has_any_notification": bool(notification_channels),
            "notification_safe": "download" not in notification_channels,
            "identity_email": self.decode_identity_email(self.identity_token),
            "state": "available" if download_url else "unavailable",
            "transfer_size": transfer.get("size"),
            "url": self.link,
        }
        return {
            "transfer_metadata": transfer_metadata,
            "downloads_count": None,
            "download_url": download_url,
            "available": bool(download_url),
        }

    def extract_files_metadata(self, response: Response) -> dict:
        return {
            item["name"]: {"size": item["size"]} for item in response.json()["files"]
        }

    def extract_filename(self, files_metadata: dict, transfer_metadata: dict) -> str:
        if len(files_metadata) == 1:
            return next(iter(files_metadata))
        name = transfer_metadata.get("transfer_name") or self.transfer_id
        return f"{name}.zip"
