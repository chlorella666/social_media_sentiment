"""敏感信息存储单元测试（1.5）：DPAPI 往返、旧明文迁移、env 通道口径。

运行：python tests/test_secrets.py
DPAPI 用例仅 Windows 可跑（非 Windows 自动跳过）；存储目录经 SMS_SECRETS_DIR
隔离到临时目录，不触碰真实 data/secrets/。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sms_secrets_test_"))
os.environ["SMS_SECRETS_DIR"] = str(_TMP / "secrets")

from app.core import secrets  # noqa: E402


def _is_windows() -> bool:
    return os.name == "nt"


def _dpapi_available() -> bool:
    """探测当前会话 DPAPI 是否可用（受限会话/服务可能不可用）。"""
    try:
        secrets.save_cookie("__probe__", "x")
        ok = secrets.load_cookie("__probe__") == "x"
        secrets.clear_cookie("__probe__")
        return ok
    except Exception:
        return False


_DPAPI = _dpapi_available()


def test_secrets_dir_override() -> None:
    assert secrets.secrets_dir() == _TMP / "secrets"
    print("✓ SMS_SECRETS_DIR 隔离生效")


def test_api_key_roundtrip() -> None:
    if not (_is_windows() and _DPAPI):
        print("⏭ DPAPI 不可用（非 Windows 或受限会话），跳过往返测试")
        return
    secrets.clear_api_key()
    assert secrets.load_api_key(allow_env=False) == ""
    secrets.save_api_key("sk-test-123")
    assert secrets.load_api_key(allow_env=False) == "sk-test-123"
    # 保存为密文：文件内容不应包含明文 Key
    blob = (secrets.secrets_dir() / "llm_api_key.bin").read_bytes()
    assert b"sk-test-123" not in blob, "DPAPI 密文不应包含明文"
    secrets.clear_api_key()
    assert secrets.load_api_key(allow_env=False) == ""
    print("✓ API Key DPAPI 往返（明文不入文件） 通过")


def test_load_api_key_env_override() -> None:
    secrets.clear_api_key()
    old = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "sk-env-key"
    try:
        assert secrets.load_api_key(allow_env=True) == "sk-env-key"
        assert secrets.load_api_key(allow_env=False) == "", "应用链路不应读 env"
    finally:
        if old is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = old
    print("✓ env 仅开发通道：allow_env=True 生效、应用链路不读 通过")


def test_migrate_legacy_key_idempotent() -> None:
    if not (_is_windows() and _DPAPI):
        print("⏭ DPAPI 不可用，跳过迁移测试")
        return
    legacy = _TMP / "legacy_key.txt"
    legacy.write_text("deepseek：\nsk-migrated-abc", encoding="utf-8")
    secrets.clear_api_key()
    assert secrets.migrate_legacy_key(legacy_file=legacy) is True
    assert secrets.load_api_key(allow_env=False) == "sk-migrated-abc"
    assert secrets.migrate_legacy_key(legacy_file=legacy) is False, "重复导入应幂等"
    print("✓ 旧明文迁移（sk- 行解析 + 幂等） 通过")


def test_ensure_legacy_migrated_deletes_file() -> None:
    if not (_is_windows() and _DPAPI):
        print("⏭ DPAPI 不可用，跳过迁移清理测试")
        return
    legacy = _TMP / "legacy_delete.txt"
    legacy.write_text("deepseek：\nsk-delete-me", encoding="utf-8")
    secrets.clear_api_key()
    assert secrets.ensure_legacy_key_migrated(legacy_file=legacy) is True
    assert not legacy.exists(), "导入验证通过后应删除明文文件"
    assert secrets.load_api_key(allow_env=False) == "sk-delete-me"
    assert secrets.ensure_legacy_key_migrated(legacy_file=legacy) is False
    print("✓ 迁移后删除明文文件 通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_secrets_dir_override()
    test_api_key_roundtrip()
    test_load_api_key_env_override()
    test_migrate_legacy_key_idempotent()
    test_ensure_legacy_migrated_deletes_file()
    print("全部敏感信息存储测试通过 ✅")


if __name__ == "__main__":
    main()
