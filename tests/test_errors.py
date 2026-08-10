"""错误分类与排查建议单元测试（1.3）。

运行：python tests/test_errors.py
覆盖：规则分类（cookie/llm/ratelimit/no_data/worker/output/渠道降级/兜底）、
      建议排序去重、failed_step 兜底类别、步骤中文名。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import errors  # noqa: E402


def make_task(error: str = "", warnings: list[str] | None = None, failed_step: str | None = None) -> dict:
    return {"error": error, "warnings": warnings or [], "failed_step": failed_step}


def test_classify_rules() -> None:
    cases = [
        ("微博渠道需要 Cookie：请在设置中粘贴已登录的微博 Cookie", "cookie"),
        ("LLM 连接失败：网络连接失败或超时，请检查 Base URL、代理与网络", "llm"),
        ("[微博] 频率限制：请求过快，请稍后重试", "ratelimit"),
        ("未采集到任何有效数据，请调整关键词或渠道后重试", "no_data"),
        ("后台执行进程未运行：新任务将排队等待", "worker"),
        ("输出落盘失败：[Errno 28] No space left on device", "output"),
        ("[B站] 采集异常: requests.exceptions.ConnectionError", "degraded"),
        ("某个完全未知的错误文本", "unknown"),
        ("", "unknown"),
    ]
    for text, expected in cases:
        assert errors.classify_error(text) == expected, (text, errors.classify_error(text))
    print("✓ 规则分类 9 例通过")


def test_suggest_ordering_and_dedup() -> None:
    task = make_task(
        error="微博渠道需要 Cookie",
        warnings=["[微博] 频率限制：请求过快", "LLM 连接失败：网络超时"],
    )
    fixes = errors.suggest_fixes(task)
    # cookie 高确定性在前，且每类只出现一次
    assert fixes[0].startswith("可能原因：微博登录态")
    assert len(fixes) == len(set(fixes))
    assert any("测试连接" in f for f in fixes)
    assert any("留出间隔" in f for f in fixes)
    print("✓ 建议排序与去重通过")


def test_failed_step_fallback_category() -> None:
    llm = errors.suggest_fixes(make_task(error="执行异常：boom", failed_step="llm"))
    assert any("测试连接" in f for f in llm), llm
    output = errors.suggest_fixes(make_task(error="执行异常：boom", failed_step="report"))
    assert any("磁盘空间" in f for f in output), output
    worker = errors.suggest_fixes(make_task(error="", failed_step="worker"))
    assert any("run.bat" in f for f in worker), worker
    print("✓ failed_step 兜底类别通过")


def test_fallback_unknown() -> None:
    fixes = errors.suggest_fixes(make_task(error="完全未知的错误"))
    assert len(fixes) == 1
    assert "暂时无法自动判断原因" in fixes[0]
    assert "一键重跑" in fixes[0]
    print("✓ 兜底建议通过")


def test_step_labels() -> None:
    assert errors.step_label("llm") == "LLM 精分析"
    assert errors.step_label("worker") == "后台进程中断"
    assert errors.step_label(None) == "未知步骤"
    assert errors.step_label("collect") == "采集数据"
    print("✓ 步骤中文名通过")


def test_is_ratelimit_wrapper() -> None:
    assert errors.is_ratelimit("风控：频率限制，请稍后")
    assert errors.is_ratelimit("429 Too Many Requests")
    assert not errors.is_ratelimit("微博 Cookie 无效或已过期")
    print("✓ is_ratelimit 薄封装通过")


def test_channel_unavailable_suggestion() -> None:
    fixes = errors.suggest_fixes(
        make_task(error="所选渠道均不可用：weibo（渠道已暂停）")
    )
    assert any("渠道安全设置" in f for f in fixes), fixes
    print("✓ 渠道不可用建议通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_classify_rules()
    test_suggest_ordering_and_dedup()
    test_failed_step_fallback_category()
    test_fallback_unknown()
    test_step_labels()
    test_is_ratelimit_wrapper()
    test_channel_unavailable_suggestion()
    print("错误建议测试全部通过 ✅")


if __name__ == "__main__":
    main()
