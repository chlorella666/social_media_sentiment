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


def _post(platform: str, title: str, content: str, pid: str = "x") -> Post:
    return Post(id=pid, platform=platform, title=title, content=content,
                url=f"https://x.example/{pid}")


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

# ---------------------------------------------------------------------------
# 2026-08-22：容错相关性判定（别名/变体/信息不足降级）+ 小红书详情一致性守卫
# ---------------------------------------------------------------------------


def _dropped_reasons(kept, dropped):
    return [d.get("reason", "") for d in dropped]


def test_relevance_alias_kept() -> None:
    """别名（LYSK/叠纸）命中即保留，不再误杀。"""
    kept, dropped = cleaner.clean_posts(
        [
            _post("websearch_zhihu", "LYSK乙游事件 - 知乎", "关于恋与深空运营的讨论", pid="a"),
            _post("websearch_zhihu", "为什么叠纸一直致力于得罪玩家?", "玩家对恋与深空的不满", pid="b"),
        ],
        subject="恋与深空",
    )
    assert len(kept) == 2, _dropped_reasons(kept, dropped)


def test_relevance_variant_kept() -> None:
    """变体（空格/大小写/简称）经归一化+最长公共子串命中。"""
    kept, dropped = cleaner.clean_posts(
        [
            _post("websearch", "华润超市怎么样", "华润 超市 购物体验", pid="c"),
            _post("websearch", "iPhone 15 评测", "iphone15 拍照体验", pid="d"),
            _post("websearch", "迈从X9机械键盘", "迈从 x9 手感不错", pid="e"),
        ],
        subject="华润万家",
        keywords=["iphone", "迈从X9"],
    )
    assert len(kept) == 3, _dropped_reasons(kept, dropped)


def test_relevance_unrelated_real_content_kept_weak() -> None:
    """有实质正文但未命中：保留并标记 weak（交由 LLM/人工判定），不硬丢。"""
    post = _post("websearch", "今日股市行情", "上证指数收盘上涨", pid="f")
    kept, dropped = cleaner.clean_posts([post], subject="恋与深空")
    assert len(kept) == 1 and not dropped
    assert post.platform_specific.get("relevance") == "weak"


def test_relevance_insufficient_info_reason() -> None:
    """标题不含品牌且正文缺失/过短时，不再标「与品牌/关键词不相关」。"""
    kept, dropped = cleaner.clean_posts(
        [_post("xiaohongshu", "今天天气真好适合出去玩", "今天天气真好适合出去玩", pid="h")],
        subject="恋与深空",
    )
    assert not kept and dropped
    assert "信息不足" in dropped[0]["reason"]
    assert "与品牌/关键词不相关" not in dropped[0]["reason"]
    # 极短内容仍由「文本过短」兜底，也不会误标不相关
    kept2, dropped2 = cleaner.clean_posts(
        [_post("xiaohongshu", "快跑！！！", "快跑！！！", pid="i")],
        subject="恋与深空",
    )
    assert not kept2 and dropped2
    assert "与品牌/关键词不相关" not in dropped2[0]["reason"]


def test_relevance_weak_content_kept_with_flag() -> None:
    """有实质正文但未命中：保留并标记 low relevance，不误杀。"""
    post = _post("xiaohongshu", "isa 你没有心", "刚买的这款耳机真的太难用了，音质很差")
    kept, dropped = cleaner.clean_posts([post], subject="某品牌耳机")
    assert len(kept) == 1 and not dropped
    assert post.platform_specific.get("relevance") == "weak"


def test_drop_record_has_content_and_match() -> None:
    """丢弃记录补全正文摘要与判定依据，供人工核对。"""
    _, dropped = cleaner.clean_posts(
        [_post("xiaohongshu", "快跑！！！", "快跑！！！", pid="g")],
        subject="恋与深空",
    )
    assert dropped
    assert "content" in dropped[0]
    assert "match" in dropped[0]


def test_detail_consistency_guard() -> None:
    """小红书详情一致性守卫：正常配对通过、错配被识别。"""
    from app.channels.xiaohongshu import _detail_is_consistent

    assert _detail_is_consistent("西村力五杀一帅", "#西村力 #西村力晚安")
    assert _detail_is_consistent("恋与深空 七夕", "和哥哥过七夕吧~")
    assert not _detail_is_consistent("全网无代餐极品阴湿男", "一直非常相信全棉时代的生产卫生")
    assert not _detail_is_consistent("北海道温泉旅馆", "#恋与深空 #收谷")
    # 任一为空/过短不阻断
    assert _detail_is_consistent("", "任意详情")
    assert _detail_is_consistent("短", "内容")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_websearch_short_text_not_dropped()
    test_official_page_review_aggregator_not_dropped()
    test_websearch_boilerplate_snippet_not_dropped()
    test_desensitize_text()
    test_relevance_alias_kept()
    test_relevance_variant_kept()
    test_relevance_unrelated_real_content_kept_weak()
    test_relevance_insufficient_info_reason()
    test_relevance_weak_content_kept_with_flag()
    test_drop_record_has_content_and_match()
    test_detail_consistency_guard()
    print("清洗规则测试全部通过 ✅")


if __name__ == "__main__":
    main()
