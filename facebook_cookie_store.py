"""Secure storage helpers for the shared Facebook cookies file."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


MAX_COOKIE_FILE_BYTES = 5 * 1024 * 1024
CONTAINER_COOKIE_PATH = "/app/cookies/cookies.json"


def cookie_upload_path() -> Path:
    directory = os.getenv("FACEBOOK_COOKIES_DIR", "data/facebook-cookies")
    return Path(directory) / "cookies.json"


def cookie_file_available() -> bool:
    path = cookie_upload_path()
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def runner_cookie_path(source_cookie_file: str | None) -> str:
    configured = (source_cookie_file or "").strip()
    if configured:
        return configured
    return CONTAINER_COOKIE_PATH if cookie_file_available() else ""


def _cookie_entries(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
        return payload["cookies"]
    raise ValueError("Cookie 檔案格式不正確，需為 JSON 陣列或包含 cookies 陣列的物件")


def save_cookie_upload(raw: bytes) -> int:
    if len(raw) > MAX_COOKIE_FILE_BYTES:
        raise ValueError("Cookie 檔案不可超過 5 MB")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Cookie 檔案不是有效的 UTF-8 JSON") from exc

    entries = _cookie_entries(payload)
    valid_count = sum(
        1
        for item in entries
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item.get("name")
        and isinstance(item.get("value"), str)
    )
    if valid_count == 0:
        raise ValueError("Cookie 檔案沒有可使用的 Cookie 項目")

    destination = cookie_upload_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=".cookies-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(raw)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, destination)
        os.chmod(destination, 0o600)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)
    return valid_count
