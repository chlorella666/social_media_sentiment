# -*- coding: utf-8 -*-
"""广告/官方内容规则预标 + 情感统计剔除（2.6，2026-08-16）。

覆盖：规则命中/不误伤、build_summary 的 ads 区块与"仅情感统计剔除"
（漏斗/关键词效果保留并注明）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coding import ad_rules  # noqa: E402
from app.core.models import AnalysisPlan, CodedItem, Post, SentimentLabel  # noqa: E402
from app.core.pipeline import build_summary  # noqa: E402


def test_ad_rule_hits_and_misses() -> None:
    assert ad_rules.is_ad("新品首发，限时优惠，快来预约！")
    assert ad_rules.is_ad("转发抽奖，抽 3 台手机", "标题")
    assert ad_rules.is_ad("", "华为官网 服务与支持")  # 官方页标题
    assert ad_rules.ad_reason("开售了，冲！") == "开售"
    assert not ad_rules.is_ad("这款手机续航真不错，用了两周很满意")
    assert not ad_rules.is_ad("求推荐 4000 左右的手机，纠结 vivo 和小米")
    print("✓ 广告规则命中/不误伤 通过")


def _plan(exclude_ad: bool) -> AnalysisPlan:
    return AnalysisPlan(
        subject="测试品牌", keywords=["测试"], keyword_groups=[], channels=[],
        exclude_ad_enabled=exclude_ad,
    )


def _fixture() -> tuple[list[CodedItem], list[Post]]:
    items = [
        CodedItem(text_id="p1:post", text="很喜欢，很满意", platform="weibo",
                  keyword="测试", sentiment=SentimentLabel.positive,
                  sentiment_score=0.8),
        CodedItem(text_id="p2:post", text="质量太差，翻车", platform="weibo",
                  keyword="测试", sentiment=SentimentLabel.negative,
                  sentiment_score=-0.8),
        CodedItem(text_id="p3:post", text="新品首发限时优惠，快来预约",
                  platform="bilibili", keyword="测试",
                  sentiment=SentimentLabel.neutral, sentiment_score=0.0,
                  ad_flag=True),
    ]
    posts = [
        Post(id="p1", platform="weibo", keyword="测试", title="", content="很喜欢"),
        Post(id="p2", platform="weibo", keyword="测试", title="", content="质量差"),
        Post(id="p3", platform="bilibili", keyword="测试", title="", content="新品首发"),
    ]
    return items, posts


def test_build_summary_ad_included_by_default() -> None:
    items, posts = _fixture()
    s = build_summary(_plan(exclude_ad=False), items, posts)
    assert s["total_items"] == 3
    assert s["ads"]["count"] == 1 and s["ads"]["excluded"] is False
    assert s["sentiment_distribution"]["neutral"]["count"] == 1  # 广告计入
    assert s["keyword_stats"]["测试"]["coded"] == 3
    print("✓ 默认计入：广告/官方内容参与情感统计 通过")


def test_build_summary_ad_excluded_sentiment_only() -> None:
    items, posts = _fixture()
    s = build_summary(_plan(exclude_ad=True), items, posts)
    assert s["total_items"] == 3          # 漏斗保留
    assert s["ads"]["count"] == 1 and s["ads"]["excluded"] is True
    assert s["ads"]["stat_n"] == 2
    assert s["sentiment_distribution"]["neutral"]["count"] == 0  # 广告不计入情感
    assert s["sentiment_distribution"]["positive"]["count"] == 1
    # 关键词效果保留：coded 仍为 3，情感计数为 2
    assert s["keyword_stats"]["测试"]["coded"] == 3
    assert s["keyword_stats"]["测试"]["stat"] == 2
    assert s["platforms"]["weibo"]["posts"] == 2  # 漏斗保留（含广告帖所在平台计数）
    print("✓ 剔除模式：仅情感统计剔除，漏斗/关键词效果保留 通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_ad_rule_hits_and_misses()
    test_build_summary_ad_included_by_default()
    test_build_summary_ad_excluded_sentiment_only()
    print("广告/官方内容规则与统计剔除测试全部通过 ✅")


if __name__ == "__main__":
    main()
