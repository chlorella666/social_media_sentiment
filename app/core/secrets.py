"""本地敏感信息存储：Windows DPAPI 加密（仅当前 Windows 用户可解密）。

用于可选持久化微博 Cookie 与 LLM API Key。data/ 目录已在 .gitignore 中，
绝不入库、不上传。应用主链路（worker）只从 DPAPI 读取 Key，环境变量
仅作为开发/评测脚本的显式通道（见 1.5 决策）。

非 Windows 平台下 DPAPI 函数安全降级：不崩溃、不落盘，仅环境变量可用。
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SECRETS_DIR = PROJECT_ROOT / "data" / "secrets"
LEGACY_KEY_FILE = PROJECT_ROOT / "llm_apikey.txt"


def secrets_dir() -> Path:
    """加密文件目录；测试可用环境变量 SMS_SECRETS_DIR 覆盖。"""
    return Path(os.environ.get("SMS_SECRETS_DIR", str(DEFAULT_SECRETS_DIR)))


def _is_windows() -> bool:
    return os.name == "nt"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _protect(data: bytes) -> bytes:
    if not _is_windows():
        raise OSError("DPAPI 仅支持 Windows")
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
    if not _is_windows():
        raise OSError("DPAPI 仅支持 Windows")
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
    """保存一项敏感值：Windows 用 DPAPI 加密；macOS/Linux 降级为
    仅当前用户可读（0600）的本地文件（弱于 Windows 加密，建议不存敏感凭据）。"""
    secrets_dir().mkdir(parents=True, exist_ok=True)
    path = secrets_dir() / f"{key}.bin"
    if _is_windows():
        path.write_bytes(_protect(value.encode("utf-8")))
        return
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_cookie(key: str) -> str:
    """读取保存值；不存在或解密失败返回空串（不抛异常）。"""
    path = secrets_dir() / f"{key}.bin"
    if not path.exists():
        return ""
    try:
        if _is_windows():
            return _unprotect(path.read_bytes()).decode("utf-8")
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""
def clear_cookie(key: str) -> None:
    path = secrets_dir() / f"{key}.bin"
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# LLM API Key 语义化封装（与 Cookie 同机制，便于统一清理）
# ---------------------------------------------------------------------------

def save_api_key(value: str) -> None:
    save_cookie("llm_api_key", value)


def load_api_key(allow_env: bool = True) -> str:
    """读取 API Key：

    - allow_env=False：应用主链路，只读 DPAPI（worker 用）；
    - allow_env=True：开发/评测脚本，DPAPI → OPENAI_API_KEY 显式覆盖。
    """
    key = load_cookie("llm_api_key")
    if key:
        return key
    if allow_env:
        return os.environ.get("OPENAI_API_KEY", "")
    return ""


def clear_api_key() -> None:
    clear_cookie("llm_api_key")


def migrate_legacy_key(legacy_file: Path | None = None) -> bool:
    """幂等迁移：仅当 DPAPI 无 Key 且存在旧明文文件时导入一次。

    返回是否执行了导入；不删除旧文件（删除由 ensure_legacy_key_migrated
    在验证通过后显式执行，避免导入失败丢 Key）。
    """
    if not _is_windows():
        return False
    if load_cookie("llm_api_key"):
        return False
    key_file = legacy_file or LEGACY_KEY_FILE
    if not key_file.exists():
        return False
    lines = [
        ln.strip()
        for ln in key_file.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    if not lines:
        return False
    key = next((ln for ln in lines if ln.startswith("sk-")), lines[-1])
    if not key:
        return False
    save_api_key(key)
    return True


def remove_legacy_key_file(legacy_file: Path | None = None) -> bool:
    """删除旧明文 Key 文件（迁移验证后调用）。返回是否实际删除。"""
    key_file = legacy_file or LEGACY_KEY_FILE
    if key_file.exists():
        key_file.unlink()
        return True
    return False


def ensure_legacy_key_migrated(legacy_file: Path | None = None) -> bool:
    """迁移旧明文 Key：导入后立即回读验证，成功才删除明文文件。

    任何失败（含 DPAPI 不可用）都不抛异常、不删文件，留待下次重试。
    """
    try:
        if not migrate_legacy_key(legacy_file=legacy_file):
            return False
        if load_api_key(allow_env=False):
            remove_legacy_key_file(legacy_file=legacy_file)
            return True
    except Exception:
        return False
    return False
