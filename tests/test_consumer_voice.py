# -*- coding: utf-8 -*-
"""消费者声音指标（2026-08-18 Phase 0）：
extract_funnel ad_count、voice_tier 分档、demo 任务 summary.consumer_voice。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import keyword_effects as ke  # noqa: E402
from app.core.planner import build_plan  # noqa: E402
from app.core.models import SentimentLabel  # noqa: E402
from app.core.pipeline import TaskRunner, recompute_summary, voice_tier  # noqa: E402
from app.coding.cleaner import clean_posts  # noqa: E402
from app.core.models import Post  # noqa: E402


def _report() -> dict:
    return {
        "plan": {"subject": "瑞幸", "keywords": ["瑞幸"]},
        "channel_results": [{
            "channel_id": "bilibili", "ok": True,
            "posts": [
                {"platform": "bilibili", "keyword": "瑞幸", "url": "u1",
                 "platform_specific": {"query": "瑞幸"}},
                {"platform": "bilibili", "keyword": "瑞幸", "url": "u2",
                 "platform_specific": {"query": "瑞幸"}},
            ],
            "dropped": [
                {"keyword": "瑞幸", "url": "u3", "reason": "不相关"},
            ],
        }],
        "coded_items": [
            {"platform": "bilibili", "keyword": "瑞幸", "sentiment": "positive",
             "ad_flag": False},
            {"platform": "bilibili", "keyword": "瑞幸", "sentiment": "negative",
             "ad_flag": True},
        ],
        "llm_usage": {},
    }


def test_extract_funnel_ad_count() -> None:
    funnel = ke.extract_funnel(_report())["funnel"]
    assert len(funnel) == 1
    r = funnel[0]
    assert r["query"] == "瑞幸" and r["collected"] == 3 and r["kept"] == 2
    assert r["coded"] == 2 and r["ad_count"] == 1
    assert r["kept"] - r["ad_count"] == 1  # E = kept − ad_count
    print("✓ extract_funnel ad_count（E=kept−ad_count）通过")


def test_voice_tier() -> None:
    # 默认比率 0.1：正/负最小条数 = max(1, 10%×E)
    assert voice_tier(120, 200, 30, 30) == "充足"
    assert voice_tier(80, 200, 5, 30) == "够用"      # E≥60% 不足、≥30% 够用，neg 达标
    assert voice_tier(80, 200, 5, 5) == "不足"        # neg 5 < 8（10%×80）
    assert voice_tier(40, 200, 5, 5) == "不足"        # E < 30%×T
    assert voice_tier(0, 0, 0, 0) == "不足"
    # 渠道校准（2026-08-18）：B站 E=18/neg=3/pos=1，比率 0.05 → 充足；
    # 默认 0.1 → pos 1 < 2，降为够用
    assert voice_tier(18, 20, 1, 3, neg_ratio=0.05, pos_ratio=0.05) == "充足"
    assert voice_tier(18, 20, 1, 3) == "够用"
    print("✓ voice_tier 分档（比率校准：默认 0.1 / bilibili 0.05）通过")


def test_demo_summary_consumer_voice() -> None:
    plan = build_plan(
        subject="瑞幸", domain_id=None, dimension_ids=[],
        keyword_groups=[], manual_keywords=["瑞幸"],
        channel_ids=["demo"], date_start=date(2026, 8, 1), date_end=date(2026, 8, 18),
        per_keyword_limit=10, comments_enabled=False, comments_per_post=0,
        llm_enabled=False,
    )
    bundle = TaskRunner(plan).run(plan)
    cv = bundle.summary.get("consumer_voice") or {}
    assert "collected" in cv and "effective" in cv and "ratio" in cv
    assert cv["tier"] in ("充足", "够用", "不足")
    assert 0 <= (cv["ratio"] or 0) <= 1
    print("✓ demo 任务 summary.consumer_voice（E/占比/分档）通过")


def test_clean_posts_content_title_guard() -> None:
    """A4：B站无简介视频 content=title 时不再被「重复（相同正文）」误丢。"""
    posts = [
        Post(id="b1", platform="bilibili", keyword="瑞幸", title="瑞幸泰奶测评：味道到底如何",
             content="瑞幸泰奶测评：味道到底如何", url="https://www.bilibili.com/video/BVA",
             timestamp="2026-08-18"),
        Post(id="b2", platform="bilibili", keyword="瑞幸", title="瑞幸联名翻车：网友怎么看",
             content="瑞幸联名翻车：网友怎么看", url="https://www.bilibili.com/video/BVB",
             timestamp="2026-08-18"),
    ]
    kept, dropped = clean_posts(posts, subject="瑞幸", keywords=["瑞幸"])
    assert len(kept) == 2, f"content=title 的两条不同标题不应判重: kept={len(kept)}"
    assert not any("重复（相同正文）" in d.get("reason", "") for d in dropped)
    # 真实长正文（≥20 字，不同标题）仍判重
    posts2 = [
        Post(id="c1", platform="bilibili", keyword="瑞幸", title="瑞幸咖啡口感分享",
             content="这是一段完全相同的正文内容共二十个字以上用于判重测试",
             url="https://x/1",
             timestamp="2026-08-18"),
        Post(id="c2", platform="bilibili", keyword="瑞幸", title="瑞幸拿铁体验报告",
             content="这是一段完全相同的正文内容共二十个字以上用于判重测试",
             url="https://x/2",
             timestamp="2026-08-18"),
    ]
    kept2, dropped2 = clean_posts(posts2, subject="瑞幸", keywords=["瑞幸"])
    assert len(kept2) == 1 and any("重复（相同正文）" in d.get("reason", "")
                                   for d in dropped2)
    # 短内容（<20 字）不作为正文键：不同标题不误判
    posts3 = [
        Post(id="d1", platform="bilibili", keyword="瑞幸", title="瑞幸咖啡口感分享第一杯",
             content="短内容", url="https://y/1", timestamp="2026-08-18"),
        Post(id="d2", platform="bilibili", keyword="瑞幸", title="瑞幸拿铁体验报告第二杯",
             content="短内容", url="https://y/2", timestamp="2026-08-18"),
    ]
    kept3, _ = clean_posts(posts3, subject="瑞幸", keywords=["瑞幸"])
    assert len(kept3) == 2, "短内容不应作为正文判重键"
    print("✓ clean_posts A4 修复：短/空正文不误丢；长同正文仍判重")


def test_recompute_summary_reflects_review() -> None:
    """2.11 方案 A：复核修改情感后 recompute_summary 反映到分布。"""
    plan = build_plan(
        subject="瑞幸", domain_id=None, dimension_ids=[],
        keyword_groups=[], manual_keywords=["瑞幸"],
        channel_ids=["demo"], date_start=date(2026, 8, 1), date_end=date(2026, 8, 18),
        per_keyword_limit=10, comments_enabled=False, comments_per_post=0,
        llm_enabled=False,
    )
    b1 = TaskRunner(plan).run(plan)
    assert b1.coded_items, "demo 任务应有编码结果"
    target = b1.coded_items[0]
    old = target.sentiment
    new = (SentimentLabel.positive if old != SentimentLabel.positive
           else SentimentLabel.negative)
    target.sentiment = new
    posts = [p for ch in b1.channel_results for p in ch.posts]
    s2 = recompute_summary(b1.plan, b1.coded_items, b1.channel_results, posts)
    assert s2["sentiment_distribution"][new.value]["count"] >= 1
    assert s2["consumer_voice"]["effective"] == len(
        [it for it in b1.coded_items if not it.ad_flag])
    print("✓ recompute_summary（复核后情感变更反映到分布）通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_extract_funnel_ad_count()
    test_voice_tier()
    test_demo_summary_consumer_voice()
    test_clean_posts_content_title_guard()
    test_recompute_summary_reflects_review()
    print("消费者声音指标测试全部通过 ✅")


if __name__ == "__main__":
    main()
