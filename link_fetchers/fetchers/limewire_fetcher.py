from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap
from httporchestrator import ConditionalStep, RepeatableStep, RequestStep, Response
from loguru import logger

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import (
    Mode,
    format_size,
    format_timestamp,
    status_is,
    variable_is,
)

_STREAM_RE = re.compile(r'streamController\.enqueue\("((?:[^"\\]|\\.)*)"\)')
_SHARING_PASSPHRASE_SALT_B64 = "wvsoOvbI854RHQMiSiPmnw=="
_CONTENT_MAIN_FILE_IV_B64 = "C8aZG384/qPpBzg="


def decode_turbo_stream(flat: list):
    cache: dict = {}

    def resolve(index):
        if not isinstance(index, int) or isinstance(index, bool):
            return index
        if index < 0:
            return None
        if index in cache:
            return cache[index]
        value = flat[index]
        if isinstance(value, dict):
            result: dict = {}
            cache[index] = result
            for key, value_index in value.items():
                actual_key = resolve(int(key.lstrip("_")))
                actual_value = resolve(value_index)
                if actual_key is not None:
                    result[actual_key] = actual_value
            return result
        if isinstance(value, list):
            if len(value) == 2 and value[0] == "D":
                return value[1]
            result_list: list = []
            cache[index] = result_list
            result_list.extend(resolve(item) for item in value)
            return result_list
        return value

    return resolve(0)


def extract_turbo_data(html: str) -> tuple[dict, dict] | None:
    if not isinstance(html, str):
        return None
    match = _STREAM_RE.search(html)
    if not match:
        return None
    raw = match.group(1)
    try:
        decoded_str = json.loads('"' + raw + '"')
        decoded = decode_turbo_stream(json.loads(decoded_str))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    loader_data = decoded.get("loaderData", {})
    return loader_data.get("routes/__root/d/$id", {}), loader_data.get(
        "routes/__root", {}
    )


def parse_sharing_url_info(url: str) -> dict:
    parsed = urlparse(url)
    sharing_id = (parsed.path.rstrip("/").split("/")[-1]) or ""
    return {"sharing_id": sharing_id, "decryption_info": parsed.fragment or ""}


def select_primary_file_key(file_keys: list, primary_key_id: str | None) -> dict:
    if not file_keys:
        return {}
    if primary_key_id:
        return next(
            (entry for entry in file_keys if entry.get("id") == primary_key_id),
            file_keys[0],
        )
    return file_keys[0]


def build_turbo_metadata(
    route_data: dict, root_data: dict, content_id: str, link: str
) -> dict:
    bucket_wrap = route_data.get("sharingBucketContentData", {})
    bucket_value = bucket_wrap.get("value", {}) if bucket_wrap.get("ok") else {}
    bucket = bucket_value.get("sharingBucket", {})
    content_items = bucket_value.get("contentItemList", [])
    first_item = content_items[0] if content_items else {}
    file_key = select_primary_file_key(
        bucket_value.get("fileEncryptionKeys", []), bucket.get("primaryEncryptionKeyId")
    )
    status = bucket.get("sharingStatus", "")
    file_keys = bucket_value.get("fileEncryptionKeys", [])
    return {
        "id": bucket.get("id") or content_id,
        "filename": bucket.get("name") or f"limewire-{content_id}",
        "size": bucket.get("totalFileSize"),
        "file_type": first_item.get("mediaType"),
        "file_count": len(content_items),
        "downloads_count": bucket.get("downloadCounter"),
        "creator_id": bucket.get("ownerId"),
        "created_at": format_timestamp(bucket.get("createdDate")),
        "expires_at": format_timestamp(bucket.get("expiresAt")),
        "state": "available" if status == "SHARED" else "unavailable",
        "url": link,
        "item_type": first_item.get("itemType"),
        "sharing_id": route_data.get("sharingId"),
        "file_url": None,
        "self_csrf": root_data.get("selfCsrf", ""),
        "content_item": first_item,
        "content_items": content_items,
        "file_encryption_key": file_key,
        "file_encryption_keys": {
            entry.get("id"): entry for entry in file_keys if entry.get("id")
        },
        "sharing_url_info": parse_sharing_url_info(link),
    }


def decode_jwt_payload(token: str) -> dict:
    if not token or "." not in token:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


def urlsafe_b64decode(data: str) -> bytes:
    normalized = (data or "").replace("-", "+").replace("_", "/")
    if len(normalized) % 4:
        normalized += "=" * (4 - (len(normalized) % 4))
    return base64.b64decode(normalized)


def derive_wrapping_key_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000)
    return kdf.derive(passphrase.encode("utf-8"))


def build_p256_private_key_from_scalar(raw_scalar: bytes) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(raw_scalar, "big"), ec.SECP256R1())


def derive_aes_key_from_ecdh(
    private_key: ec.EllipticCurvePrivateKey, peer_public_key_bytes: bytes
) -> bytes:
    peer_public_key = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), peer_public_key_bytes
    )
    return private_key.exchange(ec.ECDH(), peer_public_key)


def decrypt_aes_ctr_11byte_iv(ciphertext: bytes, aes_key: bytes, iv_11: bytes) -> bytes:
    if len(iv_11) != 11:
        raise ValueError("Invalid CTR IV length")
    counter_block = iv_11 + b"\x00" * 5
    decryptor = Cipher(algorithms.AES(aes_key), modes.CTR(counter_block)).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def unwrap_file_private_key_raw(sharing_url_info: dict, file_key: dict) -> bytes | None:
    decryption_info = sharing_url_info.get("decryption_info")
    sharing_id = sharing_url_info.get("sharing_id")
    if not isinstance(decryption_info, str) or not decryption_info:
        return None
    wrapped_private_key = file_key.get("passphraseWrappedPrivateKey")
    if len(sharing_id or "") != 36 and wrapped_private_key:
        salt = urlsafe_b64decode(_SHARING_PASSPHRASE_SALT_B64)
        wrapping_key = derive_wrapping_key_from_passphrase(decryption_info, salt)
        return aes_key_unwrap(wrapping_key, urlsafe_b64decode(wrapped_private_key))
    return urlsafe_b64decode(decryption_info)


def decrypt_limewire_file_bytes(encrypted_bytes: bytes, metadata: dict) -> bytes:
    content_item = metadata.get("content_item") or {}
    file_key = metadata.get("file_encryption_key") or {}
    sharing_url_info = metadata.get("sharing_url_info") or {}
    private_key_raw = unwrap_file_private_key_raw(sharing_url_info, file_key)
    if not private_key_raw:
        raise ValueError("Could not resolve file private key")
    base_private_key = build_p256_private_key_from_scalar(private_key_raw)
    ephemeral_public_key = urlsafe_b64decode(content_item.get("ephemeralPublicKey", ""))
    aes_ctr_key = derive_aes_key_from_ecdh(base_private_key, ephemeral_public_key)
    return decrypt_aes_ctr_11byte_iv(
        encrypted_bytes, aes_ctr_key, urlsafe_b64decode(_CONTENT_MAIN_FILE_IV_B64)
    )


class LimewireFetcher(BaseFetcher):
    """
    has download notification: No
    has downloads count: Yes
    """

    NAME = "Limewire"
    BASE_URL = "https://limewire.com"
    URL_PATTERN = re.compile(r"limewire\.com/d/([0-9A-Za-z_-]+)")

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        netloc = parsed.netloc.removeprefix("www.")
        return (netloc == "limewire.com" or netloc.endswith(".limewire.com")) and bool(
            cls.URL_PATTERN.search(url)
        )

    def __init__(
        self,
        link: str,
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: No valid Limewire URL provided")

        self.link = link
        self.content_id = self.URL_PATTERN.search(link).group(1)
        self._last_metadata: dict = {}

        super().__init__()

    def build_info_steps(self) -> list:
        return [
            RequestStep("get content metadata")
            .get(f"/d/{self.content_id}")
            .headers(**self.headers)
            .after(lambda response, vars: self.extract_content_state(response))
            .after(
                lambda response, vars: self.log_fetch_state(
                    vars["metadata"], vars["downloads_count"]
                )
            )
            .check(status_is(200), "expected 200 response")
            .check(variable_is("available", True), "link isn't available")
        ]

    def build_fetch_steps(self) -> list:
        return [
            ConditionalStep(
                RequestStep("get download urls")
                .post(
                    lambda vars: (
                        f"https://api.limewire.com/sharing/download/{vars['bucket_id']}"
                    )
                )
                .headers(
                    **self.headers,
                    Authorization=lambda vars: f"Bearer {vars['access_token']}",
                    **{
                        "x-csrf-token": lambda vars: vars["csrf_token"],
                        "Content-Type": "application/json",
                        "Cookie": lambda vars: (
                            f"production_access_token={vars['access_token']}"
                        ),
                    },
                )
                .json(
                    lambda vars: {
                        "contentItems": [
                            {"id": item["id"]} for item in vars["content_items"]
                        ]
                    }
                )
                .after(
                    lambda response, vars: self.prepare_download_queue(response, vars)
                )
                .check(status_is(200), "expected 200 response")
            ).run_when(
                lambda vars: (
                    self.should_fetch(vars)
                    and vars.get("available") is True
                    and vars.get("bucket_id") is not None
                    and vars.get("content_items")
                    and vars.get("csrf_token")
                    and vars.get("access_token")
                )
            ),
            ConditionalStep(
                RepeatableStep(
                    RequestStep("download content item")
                    .state(
                        current_download=lambda vars: vars["download_queue"][
                            vars["download_index"]
                        ]
                    )
                    .get(lambda vars: vars["current_download"]["download_url"])
                    .headers(**self.headers)
                    .timeout(120)
                    .after(
                        lambda response, vars: self.save_downloaded_item(response, vars)
                    )
                    .check(status_is(200), "expected 200 response")
                ).run_while(
                    lambda vars: (
                        vars.get("download_index", 0)
                        < len(vars.get("download_queue") or [])
                    )
                )
            ).run_when(
                lambda vars: (
                    self.should_fetch(vars) and bool(vars.get("download_queue"))
                )
            ),
        ]

    def log_fetch_state(self, metadata: dict, downloads_count: int | None):
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "content_id": self.content_id,
                "filename": metadata.get("filename"),
                "file_count": metadata.get("file_count"),
                "upload_date": metadata.get("created_at"),
                "expires_at": metadata.get("expires_at"),
                "size": format_size(metadata.get("size")),
                "downloads_count": downloads_count,
                "state": metadata.get("state"),
            },
            details={"metadata": metadata},
        )

    def extract_content_state(self, response: Response) -> dict:
        turbo = extract_turbo_data(response.text)
        if not turbo:
            raise ValueError("Unable to parse LimeWire turbo metadata")
        route_data, root_data = turbo
        metadata = build_turbo_metadata(
            route_data, root_data, self.content_id, self.link
        )
        self._last_metadata = metadata
        return {
            "metadata": metadata,
            "filename": metadata.get("filename") or f"limewire-{self.content_id}",
            "downloads_count": metadata.get("downloads_count"),
            "bucket_id": metadata.get("id"),
            "content_items": metadata.get("content_items") or [],
            "csrf_token": self.resolve_csrf_token(response),
            "access_token": self.resolve_access_token(response) or None,
            "available": metadata.get("state") == "available",
        }

    def resolve_access_token(self, response: Response) -> str:
        for item in getattr(response.raw, "history", []):
            token = dict(item.cookies).get("production_access_token", "")
            if token:
                return token
        return response.cookies.get("production_access_token", "") or ""

    def resolve_csrf_token(self, response: Response) -> str:
        access_token = self.resolve_access_token(response)
        return decode_jwt_payload(access_token).get("csrfToken", "")

    def build_item_filename(
        self, metadata: dict, content_item: dict, index: int
    ) -> str:
        total_items = len(metadata.get("content_items") or [])
        fallback_name = metadata.get("filename") or f"limewire-{self.content_id}"
        stem, original_ext = os.path.splitext(fallback_name)
        media_type = content_item.get("mediaType") or metadata.get("file_type") or ""
        guessed_ext = mimetypes.guess_extension(media_type) or original_ext or ".bin"
        if guessed_ext == ".jpe":
            guessed_ext = ".jpg"
        if total_items <= 1:
            return fallback_name if original_ext else f"{fallback_name}{guessed_ext}"
        base_name = stem or fallback_name or f"limewire-{self.content_id}"
        suffix = "" if index == 0 else f" ({index + 1})"
        return f"{base_name}{suffix}{guessed_ext}"

    def save_bytes(self, payload: bytes, filename: str) -> str:
        resolved_name = os.path.basename(filename)
        path = self.resolve_save_path(resolved_name)
        with open(path, "wb") as file_handle:
            file_handle.write(payload)
        logger.success(
            "[{}] downloaded file saved to {} ({} bytes)", self.NAME, path, len(payload)
        )
        return path

    def prepare_download_queue(self, response: Response, vars: dict) -> dict:
        payload = response.json() or {}
        download_items = payload.get("contentItems") or []
        metadata = vars.get("metadata") or {}
        content_items = {
            item.get("id"): item
            for item in (vars.get("content_items") or [])
            if item.get("id")
        }
        download_queue = []

        for index, download_item in enumerate(download_items):
            item_id = download_item.get("id")
            download_url = download_item.get("downloadUrl")
            if not item_id or not download_url:
                continue
            content_item = content_items.get(item_id)
            if not content_item:
                continue
            download_queue.append(
                {
                    "download_url": download_url,
                    "content_item": content_item,
                    "filename": self.build_item_filename(metadata, content_item, index),
                }
            )

        if not download_queue:
            raise ValueError(
                "Error: LimeWire download URLs were returned, but no files were saved"
            )

        return {
            "download_queue": download_queue,
            "download_index": 0,
            "downloaded_file_paths": [],
        }

    def save_downloaded_item(self, response: Response, vars: dict) -> dict:
        metadata = vars.get("metadata") or {}
        file_keys = metadata.get("file_encryption_keys") or {}
        current_download = vars["current_download"]
        content_item = current_download["content_item"]

        item_metadata = dict(metadata)
        item_metadata["content_item"] = content_item
        item_metadata["file_encryption_key"] = file_keys.get(
            content_item.get("baseFileEncryptionKeyId")
        ) or metadata.get("file_encryption_key")

        decrypted_bytes = decrypt_limewire_file_bytes(response.content, item_metadata)
        path = self.save_bytes(decrypted_bytes, current_download["filename"])
        return {
            "download_index": vars.get("download_index", 0) + 1,
            "downloaded_file_paths": [*vars.get("downloaded_file_paths", []), path],
        }

    def transform_body(self, body: bytes) -> bytes:
        try:
            return decrypt_limewire_file_bytes(body, self._last_metadata)
        except Exception as exc:
            logger.warning(
                "[{}] limewire decrypt failed, saving raw payload: {}", self.NAME, exc
            )
            return body
