"""本地敏感信息存储：Windows DPAPI 加密（仅当前 Windows 用户可解密）。

用于可选持久化微博 Cookie。data/ 目录已在 .gitignore 中，绝不入库。
"""

from __future__ import annotations

import ctypes
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SECRETS_DIR = PROJECT_ROOT / "data" / "secrets"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _protect(data: bytes) -> bytes:
    blob_in = _DATA_BLOB(len(data), ctypes.cast(data, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("CryptProtectData 失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _unprotect(data: bytes) -> bytes:
    blob_in = _DATA_BLOB(len(data), ctypes.cast(data, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("CryptUnprotectData 失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def save_cookie(key: str, value: str) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    (SECRETS_DIR / f"{key}.bin").write_bytes(_protect(value.encode("utf-8")))


def load_cookie(key: str) -> str:
    path = SECRETS_DIR / f"{key}.bin"
    if not path.exists():
        return ""
    try:
        return _unprotect(path.read_bytes()).decode("utf-8")
    except Exception:
        return ""


def clear_cookie(key: str) -> None:
    path = SECRETS_DIR / f"{key}.bin"
    if path.exists():
        path.unlink()
