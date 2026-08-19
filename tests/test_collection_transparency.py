# -*- coding: utf-8 -*-
"""采集透明度（2026-08-18）：
ChannelResult.collection_stats 缺省、is_quality_drop 决策口径、
_collection_notes 缺口说明（含 HTML 渲染）。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.models import AnalysisPlan, ChannelConfig, ChannelResult, ReportBundle  # noqa: E402
from app.core.pipeline import is_quality_drop  # noqa: E402
from app.output.html_report import _collection_notes, build_html  # noqa: E402


def test_channel_result_stats_default() -> None:
    r = ChannelResult(channel_id="weibo", ok=True)
    assert r.collection_stats == {}
    print("✓ ChannelResult.collection_stats 缺省为空（向后兼容）")


def test_is_quality_drop() -> None:
    # kind 优先：结构化字段权威，reason 仅供展示
    assert is_quality_drop({"kind": "quality", "reason": "重复（相同正文）"})
    assert not is_quality_drop({"kind": "collection", "reason": "与品牌/关键词不相关"})
    assert not is_quality_drop({"kind": "duplicate", "reason": "文本过短"})
    # 旧数据兜底（无 kind）：按原因文案判定
    assert is_quality_drop({"reason": "与品牌/关键词不相关"})
    assert is_quality_drop({"reason": "文本过短"})
    assert is_quality_drop({"reason": "LLM 相关性复核：不相关"})
    assert is_quality_drop({"reason": "样板/页面壳文本"})
    assert is_quality_drop({"reason": "官方页面"})
    assert not is_quality_drop({"reason": "重复（相同正文）"})
    assert not is_quality_drop({"reason": "重复返回（同帖）"})
    assert not is_quality_drop({"reason": "超出时间范围"})
    assert not is_quality_drop({"reason": "广告（ad_marked）"})
    assert not is_quality_drop({})
    # 补采一致性清洗包装前缀：剥前缀后按采集层原因判定（2026-08-19 修复）
    assert not is_quality_drop({"reason": "补采一致性清洗:重复（相同正文）"})
    assert not is_quality_drop({"reason": "补采一致性清洗:广告（ad_marked）"})
    assert not is_quality_drop({"reason": "补采一致性清洗:超出时间范围"})
    assert is_quality_drop({"reason": "补采一致性清洗:与品牌/关键词不相关"})
    # 多原因拼接（纯质量原因，已核实不与去重原因混合）
    assert is_quality_drop({"reason": "文本过短；与品牌/关键词不相关"})
    # 未知 kind（拼写错误）：回落文案判定，不静默豁免
    assert not is_quality_drop({"kind": "qualtiy", "reason": "重复（相同正文）"})
    assert is_quality_drop({"kind": "qualtiy", "reason": "与品牌/关键词不相关"})
    print("✓ is_quality_drop：kind 优先 / 旧数据兜底 / 包装前缀修复 / 多原因")


# 已知原因 → kind 契约全表（2026-08-19 口径结构化；新增原因须同步本表）
KNOWN_KIND: dict[str, str] = {
    # 采集层（channels）
    "重复返回（同帖）": "collection",
    "广告（ad_marked）": "collection",
    "超出时间范围": "collection",
    "缺少链接（无法去重/拉取详情）": "collection",
    "官网域名黑名单": "quality",
    # 清洗层（cleaner）
    "正文为空": "quality",
    "样板/页面壳文本": "quality",
    "官方页面": "quality",
    "文本过短": "quality",
    "与品牌/关键词不相关": "quality",
    "重复（相同ID）": "duplicate",
    "重复（相同链接）": "duplicate",
    "重复（相同标题）": "duplicate",
    "重复（相同正文）": "duplicate",
    # pipeline / worker
    "LLM 相关性复核：不相关": "quality",
    "人工筛选：帖子不相关（3 条评论随帖剔除）": "quality",
    "人工筛选：评论不相关（剔除 2 条）": "quality",
}


def test_kind_contract_table() -> None:
    """已知原因 → kind 全表：错标/漏标即回归红；兜底与 kind 语义必须一致。"""
    for reason, kind in KNOWN_KIND.items():
        assert is_quality_drop({"reason": reason, "kind": kind}) == (
            kind == "quality"
        ), f"kind 不一致：{reason} -> {kind}"
        assert is_quality_drop({"reason": reason}) == (
            kind == "quality"
        ), f"兜底与 kind 漂移：{reason}"
        wrapped = {"reason": f"补采一致性清洗:{reason}", "kind": kind}
        assert is_quality_drop(wrapped) == (
            kind == "quality"
        ), f"包装前缀改变判定：{reason}"
    print("✓ kind 契约全表：已知原因分类 + 兜底一致 + 包装前缀保留")


def test_drop_kind_serialization_roundtrip() -> None:
    """kind 在 dump/load/JSON 直读路径不丢；旧数据无 kind 兼容。"""
    import json
    from app.core.models import ChannelResult

    ch = ChannelResult(channel_id="weibo", ok=True, dropped=[
        {"platform": "weibo", "url": "u1", "reason": "超出时间范围",
         "kind": "collection"},
        {"platform": "weibo", "url": "u2", "reason": "与品牌/关键词不相关",
         "kind": "quality"},
    ])
    dumped = ch.model_dump()
    assert dumped["dropped"][0]["kind"] == "collection"
    reloaded = ChannelResult.model_validate(dumped)
    assert reloaded.dropped[1]["kind"] == "quality"
    raw = json.loads(json.dumps(dumped, ensure_ascii=False))
    assert raw["dropped"][0]["kind"] == "collection"
    old = ChannelResult.model_validate({
        "channel_id": "weibo", "ok": True,
        "dropped": [{"reason": "超出时间范围"}],
    })
    assert old.dropped[0].get("kind", "") == ""
    print("✓ kind 序列化往返：dump/load/JSON 直读/旧数据兼容")


def test_keyword_effects_quality_rate() -> None:
    """质量口径 vs 全漏斗：采集层丢弃（超窗/广告/重复）不计入决策分母。"""
    from app.core.keyword_effects import extract_funnel

    report = {
        "plan": {"subject": "测试", "keywords": ["测试"]},
        "channel_results": [{
            "channel_id": "weibo", "ok": True,
            "posts": [
                {"id": f"p{i}", "platform": "weibo", "keyword": "测试",
                 "title": "t", "content": "c", "url": f"https://m.weibo.cn/detail/{i}",
                 "platform_specific": {"query": "测试"}}
                for i in range(3)
            ],
            "dropped": [
                {"platform": "weibo", "url": "d1", "keyword": "测试",
                 "query": "测试", "reason": "超出时间范围", "kind": "collection"},
                {"platform": "weibo", "url": "d2", "keyword": "测试",
                 "query": "测试", "reason": "广告（ad_marked）", "kind": "collection"},
                {"platform": "weibo", "url": "d3", "keyword": "测试",
                 "query": "测试", "reason": "与品牌/关键词不相关", "kind": "quality"},
            ],
        }],
        "coded_items": [],
        "llm_usage": {},
    }
    r = extract_funnel(report)["funnel"][0]
    assert r["collected"] == 6 and r["kept"] == 3 and r["dropped"] == 3
    assert r["quality_dropped"] == 1
    assert r["effective_rate"] == 0.5           # 全漏斗 3/6
    assert r["quality_effective_rate"] == 0.75  # 质量口径 3/(3+1)
    assert r["quality_effective_rate"] > r["effective_rate"]
    print("✓ keyword_effects 质量口径：采集层丢弃不计入，质量率 > 全漏斗率")


def _bundle_with_stats(stats: dict | None) -> ReportBundle:
    plan = AnalysisPlan(subject="测试", domain_id=None, dimensions=[],
                        keyword_groups=[], keywords=["测试"],
                        channels=[ChannelConfig(channel_id="weibo")])
    ch = ChannelResult(channel_id="weibo", ok=True)
    if stats:
        ch.collection_stats = stats
    return ReportBundle(
        plan=plan, summary={"total_items": 1, "total_posts": 1, "ads": {"count": 0},
                            "sentiment_distribution": {"positive": {"ratio": 0.5},
                                                      "negative": {"ratio": 0.3},
                                                      "neutral": {"ratio": 0.2}},
                            "dimensions": {}, "platforms": {}, "avg_score": 0.1,
                            "overall_sentiment": "正面"},
        channel_results=[ch],
        coded_items=[],
        insight_mode="lexicon",
    )


def test_collection_notes_gap_and_full() -> None:
    gap = {
        "requested_limit": 20, "kept": 6, "api_returned_cards": 17,
        "mblog_cards": 14, "skipped_other_type": 3, "skipped_dup": 0,
        "skipped_ad": 0, "skipped_out_of_range": 9,
    }
    notes = _collection_notes(_bundle_with_stats(gap))
    assert len(notes) == 1
    n = notes[0]
    assert n["requested"] == 20 and n["kept"] == 6
    assert any("超出所选时间范围" in r for r in n["reasons"])
    assert any("放宽时间窗" in t for t in n["tips"])
    # 采满不展示
    assert _collection_notes(_bundle_with_stats({**gap, "kept": 20})) == []
    # 无 stats 不展示（旧任务兼容）
    assert _collection_notes(_bundle_with_stats(None)) == []
    print("✓ _collection_notes：缺口展示/采满不展示/无 stats 兜底")


def test_collection_notes_websearch_reasons() -> None:
    """WebSearch 专用计数（官网黑名单/子渠道过滤/空查询/风控）进入采集说明。"""
    gap = {
        "requested_limit": 13, "kept": 6, "api_returned_cards": 42,
        "skipped_dup": 3, "skipped_official": 4,
        "skipped_domain_filter": 20, "empty_queries": 2, "risk_stop": 1,
    }
    notes = _collection_notes(_bundle_with_stats(gap))
    assert len(notes) == 1
    text = "；".join(notes[0]["reasons"]) + "；".join(notes[0]["tips"])
    assert "官网域名黑名单" in text
    assert "非目标子渠道域名" in text
    assert "无有效结果" in text
    assert "风控信号" in text
    print("✓ _collection_notes：WebSearch 专用原因/建议自动覆盖")


def test_html_renders_collection_note() -> None:
    from datetime import date as _date
    from app.core.planner import build_plan
    from app.core.pipeline import TaskRunner

    plan = build_plan(
        subject="测试", domain_id=None, dimension_ids=[],
        keyword_groups=[], manual_keywords=["测试"],
        channel_ids=["demo"], date_start=_date(2026, 8, 1), date_end=_date(2026, 8, 18),
        per_keyword_limit=20, comments_enabled=False, comments_per_post=0,
        llm_enabled=False,
    )
    gap = {
        "requested_limit": 20, "kept": 6, "api_returned_cards": 17,
        "mblog_cards": 14, "skipped_other_type": 3, "skipped_dup": 0,
        "skipped_ad": 0, "skipped_out_of_range": 9,
    }
    bundle = TaskRunner(plan).run(plan)
    bundle.channel_results[0].collection_stats = gap
    html = build_html(bundle)
    assert "采集说明" in html and "为什么没采满" in html
    assert "超出所选时间范围" in html
    # 采满时不渲染采集说明
    bundle_full = TaskRunner(plan).run(plan)
    bundle_full.channel_results[0].collection_stats = {**gap, "kept": 20}
    html_full = build_html(bundle_full)
    assert "为什么没采满" not in html_full
    print("✓ HTML 采集说明：缺口渲染 / 采满不渲染")


def test_review_snapshot_restores_collection_stats() -> None:
    """人工筛选快照路径：重建 channel_results 必须保留 collection_stats
    （2026-08-19 修复：review 流程丢 stats，导致"采集说明"不渲染）。"""
    from app.worker import _apply_exclusions

    stats = {
        "requested_limit": 20, "kept": 6, "api_returned_cards": 17,
        "mblog_cards": 14, "skipped_other_type": 3, "skipped_dup": 0,
        "skipped_ad": 0, "skipped_out_of_range": 9,
    }
    snapshot = {
        "posts": [{
            "url": "https://m.weibo.cn/detail/1", "platform": "weibo",
            "keyword": "测试", "title": "t", "content": "c",
            "timestamp": "2026-08-10", "likes": 0, "comments": [],
        }],
        "drops": [{
            "platform": "weibo", "url": "https://m.weibo.cn/detail/x",
            "reason": "超出时间范围", "kind": "collection",
        }],
        "collection_stats": {"weibo": stats},
        "warnings": [],
    }
    _, channel_results, _ = _apply_exclusions(snapshot, {})
    assert len(channel_results) == 1
    assert channel_results[0].collection_stats == stats
    assert channel_results[0].dropped[0]["kind"] == "collection"
    # 旧快照无 collection_stats 时兜底为空（向后兼容）
    _, old_results, _ = _apply_exclusions(
        {**snapshot, "collection_stats": {}}, {}
    )
    assert old_results[0].collection_stats == {}
    print("✓ 人工筛选快照重建保留 collection_stats（采集说明可渲染）")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_channel_result_stats_default()
    test_is_quality_drop()
    test_kind_contract_table()
    test_drop_kind_serialization_roundtrip()
    test_keyword_effects_quality_rate()
    test_collection_notes_gap_and_full()
    test_collection_notes_websearch_reasons()
    test_html_renders_collection_note()
    test_review_snapshot_restores_collection_stats()
    print("采集透明度测试全部通过 ✅")


if __name__ == "__main__":
    main()
