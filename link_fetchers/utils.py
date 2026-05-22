from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum, auto
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

if TYPE_CHECKING:
    from httporchestrator import Response

from loguru import logger


class Mode(Enum):
    INFO = auto()
    FETCH = auto()
    FORCE_FETCH = auto()


def format_size(size_bytes: Any) -> str | None:
    if not isinstance(size_bytes, (int, float, str)):
        return None
    if isinstance(size_bytes, str):
        if size_bytes.isnumeric():
            size_bytes = int(size_bytes)
        else:
            return None

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{int(size_bytes)} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def format_timestamp(epoch: Any) -> str | None:
    """Format an epoch timestamp (seconds or milliseconds) or ISO string to human-readable UTC."""
    if isinstance(epoch, str):
        return epoch  # already formatted (ISO 8601 or other string)
    if not isinstance(epoch, (int, float)):
        return None
    # auto-detect: values > 1e12 are likely milliseconds
    if epoch > 1e12:
        epoch = epoch / 1000
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def should_download(mode: Mode, downloads_count: int | None) -> bool:
    if mode == Mode.FORCE_FETCH:
        return True

    if mode != Mode.FETCH:
        logger.debug(
            "Skipping download because mode is {} (downloads_count={})",
            mode.name,
            downloads_count,
        )
        return False

    if downloads_count is None:
        logger.critical("Skipping download because downloads_count is missing")
        return False

    if downloads_count <= 0:
        logger.critical(
            "Skipping download because downloads_count is {}",
            downloads_count,
        )
        return False

    return True


def parse_cookie_header(cookie_header: str) -> dict[str, str]:
    cookie_map: dict[str, str] = {}
    for pair in (cookie_header or "").split(";"):
        part = pair.strip()
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookie_map[key.strip()] = value.strip()
    return cookie_map


def merge_cookie_header(*cookie_headers: str) -> str:
    merged: dict[str, str] = {}
    for header in cookie_headers:
        merged.update(parse_cookie_header(header))
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def cookies_from_response(response: Response) -> str:
    cookie_map = {k: v for k, v in response.cookies.items() if v}
    return "; ".join(f"{k}={v}" for k, v in cookie_map.items())


def status_is(expected: int):
    return lambda response, _vars: response.status_code == expected


def variable_is(name: str, expected):
    return lambda _response, vars: vars.get(name) == expected


def variable_truthy(name: str):
    return lambda _response, vars: bool(vars.get(name))


def resolve_filename(headers: dict, fallback: str) -> str:
    """Extract filename from Content-Disposition header (case-insensitive lookup)."""
    disposition = ""
    for key, value in headers.items():
        if key.lower() == "content-disposition":
            disposition = value
            break
    if not disposition:
        return fallback
    resolved = fallback
    for item in disposition.split(";"):
        part = item.strip()
        if part.startswith("filename*="):
            encoded = part.split("=", 1)[1].strip('"')
            if "''" in encoded:
                encoded = encoded.split("''", 1)[1]
            return unquote(encoded)
        if part.startswith("filename="):
            resolved = unquote(part.split("=", 1)[1].strip('"'))
    return resolved
