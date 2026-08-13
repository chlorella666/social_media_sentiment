# -*- coding: utf-8 -*-
"""清洗规则测试（2.2 增强）：WebSearch 误杀修复。

运行：python tests/test_cleaner.py
覆盖：文本过短阈值按渠道放宽、官方页面规则不再误杀第三方评价/评分聚合页、
      WebSearch 摘要混入页面壳词时按标题是否有信息量决定是否保留。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coding import cleaner  # noqa: E402
from app.core.models import Post  # noqa: E402


def _post(platform: str, title: str, content: str) -> Post:
    return Post(id="x", platform=platform, title=title, content=content,
                url="https://x.example/1")


def test_websearch_short_text_not_dropped() -> None:
    kept, dropped = cleaner.clean_posts(
        [_post("websearch", "恋与深空 短评", "超好玩")], subject="恋与深空"
    )
    assert len(kept) == 1 and not dropped
    # 非 WebSearch 渠道仍按 10 字阈值
    kept2, dropped2 = cleaner.clean_posts(
        [_post("weibo", "短评", "好")], subject="恋与深空"
    )
    assert not kept2 and dropped2 and "文本过短" in dropped2[0]["reason"]
    print("✓ WebSearch 短评不再因文本过短误杀 通过")


def test_official_page_review_aggregator_not_dropped() -> None:
    assert cleaner.is_official_page("恋与深空的最新评论和评分-应用宝官网") is False
    assert cleaner.is_official_page("恋与深空官网") is True
    assert cleaner.is_official_page("官方网站") is True
    kept, dropped = cleaner.clean_posts(
        [_post("websearch", "恋与深空的最新评论和评分-应用宝官网",
               "应用宝上的评分汇总，用户评价很多")],
        subject="恋与深空",
    )
    assert len(kept) == 1 and not dropped
    print("✓ 官方页面规则不再误杀第三方评价/评分聚合页 通过")


def test_websearch_boilerplate_snippet_not_dropped() -> None:
    kept, dropped = cleaner.clean_posts(
        [_post("websearch", ". 对 恋与深空的评价 - TapTap",
               "查看详细资料 很好玩 强烈推荐这个游戏")],
        subject="恋与深空",
    )
    assert len(kept) == 1 and not dropped
    # 非 WebSearch 渠道仍按严格壳词规则
    kept2, dropped2 = cleaner.clean_posts(
        [_post("weibo", "某帖子", "查看详细资料")], subject="恋与深空"
    )
    assert not kept2 and dropped2 and "样板/页面壳文本" in dropped2[0]["reason"]
    print("✓ WebSearch 摘要混入页面壳词时不误杀有信息量的帖子 通过")


def test_desensitize_text() -> None:
    d = cleaner.desensitize_text
    s = "联系 a@b.com 或 13800138000 或 110101199003071234，@某用户 https://weibo.com/x"
    out = d(s)
    assert "a@b.com" not in out
    assert "13800138000" not in out
    assert "110101199003071234" not in out
    assert "@某用户" not in out
    assert "weibo.com" not in out
    # 保守原则：短数字串（游戏模组码/产品编号）不得被误删
    assert d("模组码:8099410 【红石版】:821226 其余保持原样") == "模组码:8099410 【红石版】:821226 其余保持原样"
    print("✓ LLM 前脱敏（邮箱/手机/身份证/@/链接）且不误伤短数字码 通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_websearch_short_text_not_dropped()
    test_official_page_review_aggregator_not_dropped()
    test_websearch_boilerplate_snippet_not_dropped()
    test_desensitize_text()
    print("清洗规则测试全部通过 ✅")


if __name__ == "__main__":
    main()
