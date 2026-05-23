from __future__ import annotations

import base64
import json
import os
import struct
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
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


class WormholeFetcher(BaseFetcher):
    """
    has download notification: No
    has downloads count: No
    """

    NAME = "Wormhole"
    BASE_URL = "https://wormhole.app"
    _B2_BUCKET = "socket-dev-prod"

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return host in {"wormhole.app", "www.wormhole.app"} and bool(parsed.fragment)

    def __init__(self, link: str):
        if not self.is_relevant_url(link):
            raise ValueError(
                "Error: Invalid Wormhole URL — must include key in fragment (#)"
            )

        self.link = link
        parsed = urlparse(link)
        self.room_id = parsed.path.lstrip("/")
        self.main_key = self._b64url_decode(parsed.fragment)

        super().__init__()

    def build_info_steps(self) -> list:
        return [
            RequestStep("get salt")
            .get(f"/api/room/{self.room_id}/salt")
            .headers(**self.headers)
            .capture("salt_b64", lambda r, v: r.json()["salt"])
            .check(status_is(200), "expected 200 response"),
            RequestStep("get room metadata")
            .get(f"/api/room/{self.room_id}")
            .headers(
                **self.headers,
                Authorization=lambda v: self._auth_header(v["salt_b64"]),
            )
            .after(lambda r, v: self._parse_encrypted_metadata(r, v["salt_b64"]))
            .after(lambda r, v: self._log_fetch_state(v["metadata"]))
            .check(status_is(200), "expected 200 response")
            .check(
                variable_is("available", True), "expected Wormhole room to be available"
            ),
        ]

    def build_fetch_steps(self) -> list:
        def _is_cloud_uploaded(v: dict) -> bool:
            return v.get("cloud_state") == "uploaded"

        def _has_remaining_downloads(v: dict) -> bool:
            return v.get("remaining_downloads", 0) > (
                0 if self.mode == Mode.FORCE_FETCH else 1
            )

        def _should_download(v: dict) -> bool:
            return _is_cloud_uploaded(v) and _has_remaining_downloads(v)

        return [
            RequestStep("get B2 download auth")
            .post(f"/api/room/{self.room_id}/b2/auth-download")
            .headers(
                **self.headers,
                Authorization=lambda v: self._auth_header(v["salt_b64"]),
            )
            .capture(
                "b2_download_url", lambda r, v: r.json().get("downloadUrl") or ""
            )
            .capture(
                "b2_auth_token",
                lambda r, v: r.json().get("authorizationToken") or "",
            )
            .after(lambda _r, _v: {"chunks": [], "piece_index": 0})
            .check(status_is(200), "expected 200 from B2 auth-download")
            .check(
                lambda r, v: bool(v.get("b2_download_url")),
                "Error: Wormhole B2 auth did not return a download URL",
            )
            .when(lambda v: self.should_fetch(v, downloads_count=1, when=_should_download)),
            ConditionalStep(
                RequestStep("download piece")
                .get(
                    lambda v: (
                        f"{v['b2_download_url']}/file/{self._B2_BUCKET}"
                        f"/{self.room_id}/{v['piece_index']}"
                    )
                )
                .headers(Authorization=lambda v: v["b2_auth_token"])
                .after(lambda r, v: self._after_piece(r, v))
                .check(status_is(200), "expected 200 from B2")
                .while_(lambda v: v.get("piece_index", 0) < v.get("piece_count", 1)),
            ).run_when(
                lambda v: self.should_fetch(
                    v, downloads_count_key="remaining_downloads", when=_should_download
                )
            ),
        ]

    # ── crypto helpers ──────────────────────────────────────────────────────

    def _hkdf_derive(self, salt: bytes, info: bytes, length: int = 16) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=length,
            salt=salt,
            info=info,
        ).derive(self.main_key)

    def _auth_header(self, salt_b64: str) -> str:
        salt = base64.b64decode(salt_b64)
        token = self._hkdf_derive(salt, b"authentication", 16)
        return f"Bearer sync-v1 {base64.b64encode(token).decode()}"

    def _decrypt_meta_bytes(self, encrypted: bytes, salt_b64: str) -> bytes:
        salt = base64.b64decode(salt_b64)
        meta_key = self._hkdf_derive(salt, b"metadata", 16)
        iv, ciphertext = encrypted[:16], encrypted[16:]
        return AESGCM(meta_key).decrypt(iv, ciphertext, None)

    # ── metadata parsing ────────────────────────────────────────────────────

    def _parse_encrypted_metadata(self, response: Response, salt_b64: str) -> dict:
        try:
            enc_b64 = response.json()["encryptedTorrentFile"]
            raw = base64.b64decode(enc_b64)
            plaintext = self._decrypt_meta_bytes(raw, salt_b64)
        except Exception as exc:
            raise ValueError(
                f"Error: Wormhole metadata decryption failed: {exc}"
            ) from exc

        body = response.json()
        meta = self._decode_metadata(plaintext)
        filename = (
            meta.get("name") or meta.get("filename") or f"wormhole-{self.room_id}"
        )
        size = meta.get("size") or meta.get("fileSize") or meta.get("length")
        download_url = (
            meta.get("downloadUrl") or meta.get("url") or meta.get("download_url")
        )
        cloud_state = body.get("cloudState")
        piece_count = meta.get("piece_count", 1)

        remaining_downloads = body.get("remainingDownloads")
        return {
            "available": True,
            "filename": filename,
            "download_url": download_url,
            "cloud_state": cloud_state,
            "piece_count": piece_count,
            "remaining_downloads": remaining_downloads,
            "metadata": {
                "filename": filename,
                "size": size,
                "cloud_state": cloud_state,
                "remaining_downloads": remaining_downloads,
                "expires_at": format_timestamp(body.get("expiresAtTimestampMs", 0)),
                "download_url": download_url,
            },
        }

    def _decode_metadata(self, plaintext: bytes) -> dict:
        try:
            return json.loads(plaintext)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        try:
            torrent, _ = _bencode_decode(plaintext)
            return _extract_torrent_fields(torrent)
        except Exception:
            pass
        raise ValueError("Error: Unable to decode Wormhole metadata as JSON or torrent")

    def _log_fetch_state(self, metadata: dict) -> None:
        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "filename": metadata.get("filename"),
                "size": format_size(metadata.get("size")),
                "cloud_state": metadata.get("cloud_state"),
                "remaining_downloads": metadata.get("remaining_downloads"),
                "expires_at": metadata.get("expires_at"),
            },
            details={"metadata": metadata},
        )

    # ── B2 download ─────────────────────────────────────────────────────────

    def _after_piece(self, response, variables: dict) -> dict:
        chunks: list[bytes] = list(variables.get("chunks", []))
        chunks.append(response.body)
        piece_index: int = variables.get("piece_index", 0)
        piece_count: int = variables.get("piece_count", 1)
        updates: dict = {"chunks": chunks, "piece_index": piece_index + 1}
        if piece_index + 1 >= piece_count:
            updates.update(self._assemble_and_save(chunks, variables))
        return updates

    def _assemble_and_save(self, chunks: list[bytes], variables: dict) -> dict:
        decrypted = self._ece_decrypt(b"".join(chunks))
        path = self.resolve_save_path(variables["filename"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(decrypted)
        logger.success(
            "[{}] downloaded file saved to {} ({} bytes)",
            self.NAME,
            path,
            len(decrypted),
        )
        return {"local_file_path": path}

    # ── ECE (RFC 8188) decryption ───────────────────────────────────────────

    def _ece_decrypt(self, data: bytes) -> bytes:
        """Decrypt an ECE (RFC 8188 / aes128gcm) encrypted byte stream."""
        if len(data) < 21:
            raise ValueError("Error: ECE data too short to contain header")

        salt = data[:16]
        record_size = struct.unpack(">I", data[16:20])[0]
        idlen = data[20]
        payload_start = 21 + idlen

        content_key = self._hkdf_derive(salt, b"Content-Encoding: aes128gcm\x00", 16)
        nonce_base = bytearray(
            self._hkdf_derive(salt, b"Content-Encoding: nonce\x00", 12)
        )

        plaintext_parts: list[bytes] = []
        offset = payload_start
        seq = 0

        while offset < len(data):
            chunk_end = min(offset + record_size, len(data))
            record = data[offset:chunk_end]

            nonce = bytearray(nonce_base)
            seq_bytes = struct.pack(">I", seq)
            for i in range(4):
                nonce[8 + i] ^= seq_bytes[i]

            padded = AESGCM(content_key).decrypt(bytes(nonce), record, None)

            # Strip padding: scan back for delimiter byte (non-zero)
            delim_idx = len(padded) - 1
            while delim_idx > 0 and padded[delim_idx] == 0:
                delim_idx -= 1

            is_final = padded[delim_idx] == 2
            plaintext_parts.append(padded[:delim_idx])

            offset = chunk_end
            seq += 1
            if is_final:
                break

        return b"".join(plaintext_parts)

    @staticmethod
    def _b64url_decode(s: str) -> bytes:
        s = s.rstrip("=")
        padding = (4 - len(s) % 4) % 4
        return base64.urlsafe_b64decode(s + "=" * padding)


# ── minimal bencode decoder ─────────────────────────────────────────────────


def _bencode_decode(data: bytes, offset: int = 0):
    tag = data[offset : offset + 1]
    if tag == b"d":
        offset += 1
        result: dict = {}
        while data[offset : offset + 1] != b"e":
            key, offset = _bencode_decode(data, offset)
            val, offset = _bencode_decode(data, offset)
            result[key] = val
        return result, offset + 1
    if tag == b"l":
        offset += 1
        result_list: list = []
        while data[offset : offset + 1] != b"e":
            val, offset = _bencode_decode(data, offset)
            result_list.append(val)
        return result_list, offset + 1
    if tag == b"i":
        end = data.index(b"e", offset + 1)
        return int(data[offset + 1 : end]), end + 1
    # byte string
    colon = data.index(b":", offset)
    length = int(data[offset:colon])
    start = colon + 1
    return data[start : start + length], start + length


def _extract_torrent_fields(torrent: dict) -> dict:
    info = torrent.get(b"info", {})
    name_raw = info.get(b"name", b"")
    name = (
        name_raw.decode("utf-8", errors="replace")
        if isinstance(name_raw, bytes)
        else str(name_raw)
    )
    size = info.get(b"length", 0)
    if not size:
        files = info.get(b"files", [])
        size = sum(f.get(b"length", 0) for f in files if isinstance(f, dict))

    pieces_raw = info.get(b"pieces", b"")
    piece_count = max(1, len(pieces_raw) // 20)

    url_list = torrent.get(b"url-list", [])
    if isinstance(url_list, bytes):
        url_list = [url_list]
    download_url = (
        url_list[0].decode() if url_list and isinstance(url_list[0], bytes) else None
    )

    return {
        "name": name,
        "size": size,
        "piece_count": piece_count,
        "downloadUrl": download_url,
    }
