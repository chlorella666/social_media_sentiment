"""《使用边界》首次确认状态单元测试（1.5）。

运行：python tests/test_usage_boundary.py
覆盖：默认未确认、确认后放行、版本变化需重新确认、文档缺失兜底文案。
状态目录经 SMS_STATE_DIR 隔离到临时目录。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sms_usage_test_"))
os.environ["SMS_STATE_DIR"] = str(_TMP / "state")

from app.core import usage_boundary  # noqa: E402


def test_default_not_acknowledged() -> None:
    usage_boundary.reset_ack()
    assert not usage_boundary.is_acknowledged()
    print("✓ 默认未确认（首次启动需确认） 通过")


def test_acknowledge_then_acknowledged() -> None:
    usage_boundary.reset_ack()
    usage_boundary.acknowledge()
    assert usage_boundary.is_acknowledged()
    ack = json.loads(usage_boundary.ack_path().read_text(encoding="utf-8"))
    assert ack["version"] == usage_boundary.doc_version()
    assert ack["app_version"], "确认记录应含应用版本"
    assert ack["acked_at"], "确认记录应含确认时间"
    print("✓ 确认落盘（版本 + 时间 + 应用版本） 通过")


def test_version_change_requires_reconfirm() -> None:
    usage_boundary.reset_ack()
    usage_boundary.acknowledge()
    assert usage_boundary.is_acknowledged()
    usage_boundary.ack_path().write_text(
        json.dumps({"version": "0.9", "acked_at": "", "app_version": ""}),
        encoding="utf-8",
    )
    assert not usage_boundary.is_acknowledged(), "文档版本变化应重新确认"
    print("✓ 版本变化 → 重新确认 通过")


def test_doc_missing_fallback() -> None:
    original_path = usage_boundary.USAGE_BOUNDARY_PATH
    usage_boundary.USAGE_BOUNDARY_PATH = _TMP / "missing.md"
    try:
        assert "兜底说明" in usage_boundary.boundary_text()
        assert usage_boundary.doc_version() == usage_boundary.DEFAULT_VERSION
    finally:
        usage_boundary.USAGE_BOUNDARY_PATH = original_path
    print("✓ 文档缺失：内置兜底文案 + 默认版本 通过")


def test_privacy_regression_items() -> None:
    """P1-8 隐私回归项：LLM 出境提示 + 离线模式可用（P1-2）+ 确认可重置（P1-4 联动）。"""
    text = usage_boundary.boundary_text()
    assert any(k in text for k in ("发送给", "出境", "服务商")), "《使用边界》应明示数据出境"
    assert any(k in text for k in ("敏感信息", "勿输入")), "应提示勿输入个人敏感信息"
    # P1-2：无 Key 时默认走词典（Mock）分析器，离线可跑
    from app.coding.llm_analyzer import MockAnalyzer, create_analyzer

    assert isinstance(create_analyzer(api_key=None, allow_env=False), MockAnalyzer)
    # P1-4 联动：确认记录可一键重置（清除后重新弹确认）
    usage_boundary.acknowledge()
    assert usage_boundary.is_acknowledged()
    usage_boundary.reset_ack()
    assert not usage_boundary.is_acknowledged()
    print("✓ P1-8 隐私回归项：出境提示 / 离线默认 / 确认可重置 通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_default_not_acknowledged()
    test_acknowledge_then_acknowledged()
    test_version_change_requires_reconfirm()
    test_doc_missing_fallback()
    test_privacy_regression_items()
    print("全部《使用边界》确认测试通过 ✅")


if __name__ == "__main__":
    main()
