from __future__ import annotations

import base64
import json
import re
import struct

from Crypto.Cipher import AES
from httporchestrator import RequestStep, Response

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import Mode, format_size, status_is, variable_is


def _a32_to_str(values) -> bytes:
    return struct.pack(f">{len(values)}I", *values)


def _str_to_a32(data: bytes):
    remainder = len(data) % 4
    if remainder:
        data += b"\0" * (4 - remainder)
    return struct.unpack(f">{len(data) // 4}I", data)


def _base64_url_decode(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _base64_to_a32(data: str):
    return _str_to_a32(_base64_url_decode(data))


def _decrypt_attr(attr: bytes, key):
    cipher = AES.new(_a32_to_str(key), AES.MODE_CBC, b"\0" * 16)
    decrypted = cipher.decrypt(attr).decode("latin-1").rstrip("\0")
    return json.loads(decrypted[4:]) if decrypted[:6] == 'MEGA{"' else False


class MegaFetcher(BaseFetcher):
    """
    has download notification: No
    has downloads count: No
    note: Transfer.it isn't tested
    """

    NAME = "Mega"
    BASE_URL = "https://g.api.mega.co.nz"
    URL_PATTERN = re.compile(
        r"https?://mega\.nz/(?:file/(?P<file_id>[A-Za-z0-9_-]+)#(?P<key>[A-Za-z0-9_-]+)|#!(?P<legacy_file_id>[A-Za-z0-9_-]+)!(?P<legacy_key>[A-Za-z0-9_-]+))"
    )

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        return bool(cls.URL_PATTERN.match(url))

    def __init__(
        self,
        link: str,
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: No valid Mega URL provided")

        self.link = link

        match = self.URL_PATTERN.match(link)
        self.file_id = match.group("file_id") or match.group("legacy_file_id")
        self.file_key = match.group("key") or match.group("legacy_key")

        super().__init__()

    def build_info_steps(self) -> list:
        return [
            RequestStep("get file metadata")
            .post("/cs")
            .params(id=0)
            .headers(**self.headers)
            .json(lambda vars: self.build_file_info_payload())
            .after(
                lambda response, vars: {
                    "api_response": self.extract_api_response(response)
                }
            )
            .after(lambda response, vars: self.extract_file_state(vars["api_response"]))
            .after(
                lambda response, vars: self.log_fetch_state(
                    vars["metadata"], vars["downloads_count"]
                )
            )
            .check(status_is(200), "expected 200 response")
            .check(variable_is("available", True), "expected file to be available")
        ]

    def build_fetch_steps(self) -> list:
        return [self.download_step()]

    def log_fetch_state(self, metadata: dict, downloads_count: int):
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "filename": metadata.get("filename"),
                "downloads_count": downloads_count,
                "size": format_size(metadata.get("size")),
                "url": metadata.get("url"),
                "state": metadata.get("state"),
            },
            details={"metadata": metadata},
        )

    def build_file_info_payload(self) -> list[dict]:
        return [{"a": "g", "g": 1, "p": self.file_id, "ssm": 1}]

    def extract_api_response(self, response: Response) -> dict:
        payload = response.json()
        if isinstance(payload, list):
            payload = payload[0]
        if isinstance(payload, int):
            return {"error_code": payload}
        if not isinstance(payload, dict):
            raise ValueError(f"Error: Unexpected Mega API payload: {payload}")
        return payload

    def _get_decrypted_file_key(self):
        decoded_file_key = _base64_to_a32(self.file_key)
        return (
            decoded_file_key[0] ^ decoded_file_key[4],
            decoded_file_key[1] ^ decoded_file_key[5],
            decoded_file_key[2] ^ decoded_file_key[6],
            decoded_file_key[3] ^ decoded_file_key[7],
        )

    def extract_filename(self, api_response: dict) -> str:
        if "at" not in api_response:
            return f"{self.file_id}.bin"

        attrs = _decrypt_attr(
            _base64_url_decode(api_response["at"]), self._get_decrypted_file_key()
        )
        if not attrs or not attrs.get("n"):
            return f"{self.file_id}.bin"
        return attrs["n"]

    def extract_file_state(self, api_response: dict) -> dict:
        size = api_response.get("s")
        available = bool(
            api_response.get("error_code") is None
            and api_response.get("g")
            and size is not None
        )

        if api_response.get("error_code") is not None:
            metadata = {
                "filename": f"{self.file_id}.bin",
                "size": size,
                "url": self.link,
                "state": "unavailable",
                "error_code": api_response["error_code"],
            }
        else:
            metadata = {
                "filename": self.extract_filename(api_response),
                "size": size,
                "url": self.link,
                "state": "available" if available else "unavailable",
            }

        return {
            "metadata": metadata,
            "filename": metadata["filename"],
            "direct_link": api_response.get("g"),
            "downloads_count": 1,
            "available": available,
        }

    def transform_body(self, body: bytes) -> bytes:
        try:
            from Crypto.Cipher import AES
            from Crypto.Util import Counter
        except ImportError as exc:
            raise ImportError(
                "Mega fetcher requires pycryptodome-compatible AES support at runtime."
            ) from exc

        parsed_file_key = _base64_to_a32(self.file_key)
        decrypted_key = self._get_decrypted_file_key()
        iv = parsed_file_key[4:6] + (0, 0)
        counter = Counter.new(128, initial_value=((iv[0] << 32) + iv[1]) << 64)
        decryptor = AES.new(_a32_to_str(decrypted_key), AES.MODE_CTR, counter=counter)
        return decryptor.decrypt(body)
