"""报告证据链测试：证据级联确定性、n 守卫、降噪、need_review/ad 口径、零数据、词典禁语。"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.evidence import (
    build_evidence,
    build_findings,
    filter_llm_findings,
    findings_to_conclusion,
    validate_llm_findings,
)
from app.core.models import AnalysisPlan, CodedItem, SentimentLabel
from app.core.pipeline import TaskRunner


def _summary(neg: int = 10, pos: int = 5, neu: int = 2) -> dict:
    total = neg + pos + neu
    return {
        "total_items": total,
        "total_posts": total,
        "sentiment_distribution": {
            "negative": {"count": neg, "ratio": neg / total if total else 0},
            "positive": {"count": pos, "ratio": pos / total if total else 0},
            "neutral": {"count": neu, "ratio": neu / total if total else 0},
        },
        "dimensions": {},
        "sentiment_sources": [],
        "top_words": [],
    }


def _item(
    tid: str,
    text: str,
    dims: list[str] | None = None,
    dim_sents: dict[str, str] | None = None,
    sent: str = "negative",
    method: str = "lexicon",
    score: float = -0.5,
    intensity: int = 3,
    date: str = "2026-08-12",
    platform: str = "weibo",
    need_review: bool = False,
    ad: bool = False,
) -> CodedItem:
    return CodedItem(
        text_id=tid,
        text=text,
        platform=platform,
        keyword="品牌",
        pub_date=date,
        dimensions=dims or [],
        dimension_sentiments=dim_sents or {},
        sentiment=SentimentLabel(sent),
        intensity=intensity,
        sentiment_score=score,
        method=method,
        need_review=need_review,
        ad_flag=ad,
    )


def test_evidence_cascade_deterministic() -> None:
    """证据级联确定性：负面优先、n 与桶一致、两次结果相同。"""
    items = [
        _item("n1", "价格太贵了", dim_sents={"price_value": "negative"}, score=-0.9, intensity=5),
        _item("n2", "价格虚高", dim_sents={"price_value": "negative"}, score=-0.7, intensity=4),
        _item("n3", "价格离谱", dim_sents={"price_value": "negative"}, score=-0.6, intensity=3),
        _item("n4", "价格不行", dim_sents={"price_value": "negative"}, score=-0.5, intensity=2),
        _item("n5", "价格偏高", dim_sents={"price_value": "negative"}, score=-0.4, intensity=1),
        _item("p1", "质量很好", dim_sents={"product_quality": "positive"}, sent="positive", score=0.8),
        _item("p2", "质量不错", dim_sents={"product_quality": "positive"}, sent="positive", score=0.6),
        _item("o1", "整体太差了", sent="negative", score=-0.8, intensity=4),
    ]
    ev1 = build_evidence(items, _summary(neg=6, pos=2))
    ev2 = build_evidence(items, _summary(neg=6, pos=2))
    assert [c["id"] for c in ev1] == [c["id"] for c in ev2]
    assert [
        c["text_id"] for c in ev1 if c.get("kind") != "stat"
    ] == [
        c["text_id"] for c in ev2 if c.get("kind") != "stat"
    ]
    assert any(c.get("kind") == "stat" for c in ev1), "应包含统计卡"
    price_neg = [
        c for c in ev1
        if c["dimension"] == "price_value" and c.get("kind") != "stat"
    ]
    quality_pos = [
        c for c in ev1
        if c["dimension"] == "product_quality" and c.get("kind") != "stat"
    ]
    assert len(price_neg) == 3, "每维度最多 3 条"
    assert all(c["n"] == 5 for c in price_neg), "n = 该维度×该判定样本数"
    assert all(c["n"] == 2 for c in quality_pos)
    # 负面桶排最前
    assert ev1[0]["sentiment"] == "negative"
    assert ev1[0]["dimension"] == "price_value"
    # 排序：负面优先 → 情感分降序
    scores = [c["score"] for c in price_neg]
    assert scores == sorted(scores, reverse=True)
    print("✓ 证据级联确定性通过")


def test_two_tier_n_guard() -> None:
    """两档 n 守卫：n<3 不评级；3~9 样本有限且不排前；n>=10 正常。"""
    items = [
        _item(f"s{i}", "质量差", dim_sents={"product_quality": "negative"}, score=-0.5)
        for i in range(12)
    ] + [
        _item(f"pr{i}", "价格贵", dim_sents={"price_value": "negative"}, score=-0.5)
        for i in range(4)
    ] + [
        _item(f"ch{i}", "渠道差", dim_sents={"channel_service": "negative"}, score=-0.5)
        for i in range(2)
    ]
    findings = build_findings(
        build_evidence(items, _summary(neg=18)), _summary(neg=18), mode="lexicon"
    )
    claims = [f["claim"] for f in findings]
    assert "产品质量" in "".join(claims)
    assert not any("渠道服务" in c for c in claims), "n=2 维度不得评级"
    price_claim = next(c for c in claims if "价格价值" in c)
    assert "样本有限" in price_claim
    quality_claim = next(c for c in claims if "产品质量" in c)
    assert "样本有限" not in quality_claim
    # 样本有限维度不能排第一（F1 是整体发现，F2 是 n>=10 维度）
    assert "价格价值" not in findings[0]["claim"]
    assert findings[1]["claim"].find("产品质量") >= 0
    print("✓ 两档 n 守卫通过")


def test_denoise_profanity_pii_truncate() -> None:
    """降噪：脏话替换、PII 脱敏、截断 ≤80 字。"""
    items = [
        _item("p", "客服电话13812345678 联系@官方号 太垃圾了 傻逼"),
        _item("t", "这是一个非常长的文本" * 15),
    ]
    ev = build_evidence(items, _summary(neg=2))
    by_tid = {c["text_id"]: c for c in ev if c.get("kind") != "stat"}
    assert by_tid["p"]["text"] == "原文含粗口，已略"
    t = by_tid["t"]["text"]
    assert len(t) <= 81 and t.endswith("…")
    print("✓ 降噪（脏话/PII/截断）通过")


def test_need_review_and_ad_population() -> None:
    """口径：need_review 保留并标注；ad 剔除跟随 exclude_ad。"""
    items = [
        _item("r", "可疑文本", dim_sents={"price_value": "negative"}, need_review=True),
        _item("a", "广告文本", dim_sents={"price_value": "negative"}, ad=True),
        _item("x", "正常文本", dim_sents={"price_value": "negative"}),
    ]
    ev_keep_ad = build_evidence(items, _summary(neg=3), exclude_ad=False)
    tids_keep = {c["text_id"] for c in ev_keep_ad if c.get("kind") != "stat"}
    assert {"r", "a", "x"} <= tids_keep
    r = next(c for c in ev_keep_ad if c["text_id"] == "r")
    assert r["need_review"] is True
    ev_drop_ad = build_evidence(items, _summary(neg=3), exclude_ad=True)
    tids_drop = {c["text_id"] for c in ev_drop_ad if c.get("kind") != "stat"}
    assert "a" not in tids_drop and {"r", "x"} <= tids_drop
    print("✓ need_review/ad 口径通过")


def test_zero_data_short_circuit() -> None:
    """零数据：证据与 findings 为空；TaskRunner 全程不生成推断。"""
    assert build_evidence([], _summary(neg=0, pos=0, neu=0)) == []
    assert build_findings([], _summary(neg=0, pos=0, neu=0)) == []
    plan = AnalysisPlan(subject="空数据", keywords=["空数据"], channels=[])
    bundle = TaskRunner(plan).run()
    assert bundle.insight_mode == "no_data"
    assert bundle.findings == [] and bundle.evidence == []
    assert "无法生成情感结论" in bundle.report_text
    assert "推断" not in bundle.report_text and "建议开启" not in bundle.report_text
    print("✓ 零数据短路通过")


def test_lexicon_findings_guardrails() -> None:
    """词典模式禁语：无强动作建议；负面>正面绝不出"中性/正面为主"。"""
    items = [_item(f"n{i}", "价格太贵了", dim_sents={"price_value": "negative"}) for i in range(10)]
    findings = build_findings(
        build_evidence(items, _summary(neg=10, pos=3)), _summary(neg=10, pos=3), mode="lexicon"
    )
    text = findings_to_conclusion(findings) + "".join(f["claim"] for f in findings)
    assert "整体舆论以中性/正面为主" not in text
    assert "负面占比高于正面" in text
    for banned in ("危机公关", "质检报告", "促销策略", "立即响应"):
        assert banned not in text, f"词典模式不得出现强动作建议: {banned}"
    assert all("n=" in f["claim"] for f in findings)
    assert all("开启 LLM 精分析" in f["action"] for f in findings)
    assert "价格价值" in findings_to_conclusion(findings)
    print("✓ 词典模式结论禁语通过")


def test_validate_llm_findings() -> None:
    """LLM findings 结构校验：引用必须存在、action 必须有具体渠道、≤5 条。"""
    ev = [
        {
            "id": "E1", "text": "t1",
            "dimension": "brand_image", "dimension_name": "品牌形象",
            "platform": "weibo",
        },
        {
            "id": "E2", "text": "t2",
            "dimension": "price_value", "dimension_name": "价格价值",
            "platform": "bilibili",
        },
        {
            "id": "S1", "kind": "stat", "text": "维度「品牌形象」：n=10，负面率 80%",
            "dimension": "brand_image", "dimension_name": "品牌形象", "platform": "",
        },
    ]
    good = [
        {
            "id": "F1",
            "claim": "负面集中在价格",
            "evidence_refs": ["E1"],
            "action": "建议品牌方在微博渠道核对价格反馈，详见 F1",
        }
    ]
    assert validate_llm_findings(good, ev)
    assert not validate_llm_findings(
        [{"id": "F1", "claim": "c", "evidence_refs": ["E99"], "action": "详见 F1"}], ev
    )
    assert not validate_llm_findings(
        [{"id": "F1", "claim": "c", "evidence_refs": [], "action": "泛泛而谈"}], ev
    )
    assert not validate_llm_findings(
        [
            {
                "id": "F1",
                "claim": "c",
                "evidence_refs": ["E1"],
                "action": "建议加强服务质量管理，提升整体口碑表现",
            }
        ],
        ev,
    )
    # 回归：action 含具体渠道但未把 F#/E# 写进正文 → 应判有效
    # （罗技/OPPO 验收样本被误杀的原因）
    channel_only = [
        {
            "id": "F1",
            "claim": "负面集中在品牌形象",
            "evidence_refs": ["E1"],
            "action": "由公关部联合市场部在微博、B站、小红书发布改进公告并跟进回复。",
        }
    ]
    assert validate_llm_findings(channel_only, ev)
    # 结论提到维度 → 引用必须含该维度卡（统计卡或原文卡）
    assert validate_llm_findings(
        [
            {
                "id": "F1",
                "claim": "品牌形象维度负面率高达80%",
                "evidence_refs": ["S1"],
                "action": "由市场部在微博渠道跟进处理（对应F1）。",
            }
        ],
        ev,
    )
    assert not validate_llm_findings(
        [
            {
                "id": "F1",
                "claim": "品牌形象维度负面率高达80%",
                "evidence_refs": ["E2"],
                "action": "由市场部在微博渠道跟进处理（对应F1）。",
            }
        ],
        ev,
    )
    # 结论提到平台 → 引用必须含该平台卡
    assert validate_llm_findings(
        [
            {
                "id": "F1",
                "claim": "微博平台负面情绪最重",
                "evidence_refs": ["E1"],
                "action": "由客服部在微博渠道跟进处理（对应F1）。",
            }
        ],
        ev,
    )
    assert not validate_llm_findings(
        [
            {
                "id": "F1",
                "claim": "微博平台负面情绪最重",
                "evidence_refs": ["E2"],
                "action": "由客服部在微博渠道跟进处理（对应F1）。",
            }
        ],
        ev,
    )
    assert not validate_llm_findings([], ev)
    assert not validate_llm_findings(good * 6, ev)
    print("✓ LLM findings 校验通过")


def test_filter_llm_findings_drops_bad_keeps_good() -> None:
    """按条过滤：坏条只丢自己，不连累整组降级。"""
    ev = [{"id": f"E{i}", "text": "t"} for i in range(1, 10)]
    fs = [
        {"id": "F1", "claim": "c1", "evidence_refs": ["E1"],
         "action": "由客服部在微博发布处理公告（对应F1）。"},
        {"id": "F2", "claim": "c2", "evidence_refs": ["E2"],
         "action": "由市场部在B站发布说明（对应F2）。"},
        {"id": "F3", "claim": "c3", "evidence_refs": ["E3"],
         "action": "由产品部在知乎发布说明（对应F3）。"},
        {"id": "F4", "claim": "c4", "evidence_refs": ["E4"],
         "action": "无渠道无编号的空泛建议"},  # 非法：无渠道标记
        {"id": "F5", "claim": "c5", "evidence_refs": ["E99"],
         "action": "由客服部在微博发布说明（对应F5）。"},  # 非法：引用不存在
    ]
    kept, dropped = filter_llm_findings(fs, ev)
    assert dropped == 2, (kept, dropped)
    assert [f["id"] for f in kept] == ["F1", "F2", "F3"]
    print("✓ filter_llm_findings 按条过滤通过")


def test_build_report_content_llm_partial_fallback() -> None:
    """build_report_content：部分坏条不整组降级；全坏才回退规则 findings。"""
    from app.coding.insights import build_report_content
    from app.coding.llm_analyzer import LLMConfig, OpenAICompatibleAnalyzer
    from app.core.planner import build_plan, generate_keyword_groups
    from app.domains.loader import load_domain

    class FakeInsights(OpenAICompatibleAnalyzer):
        def __init__(self, payload):
            super().__init__(LLMConfig(api_key="sk-x"))
            self._payload = payload

        def generate_insights(self, descriptors, evidence=None, summary=None):
            return self._payload

    schema = load_domain("consumer")
    groups = generate_keyword_groups("欧莱雅", schema, [d.id for d in schema.dimensions])
    plan = build_plan(
        subject="欧莱雅", domain_id="consumer",
        dimension_ids=[d.id for d in schema.dimensions],
        keyword_groups=groups, manual_keywords=None, channel_ids=["demo"],
        date_start=dt.date.today() - dt.timedelta(days=30), date_end=dt.date.today(),
        comments_enabled=True, comments_per_post=3, llm_enabled=True,
        narrative_enabled=False,
    )
    bundle = TaskRunner(plan).run()  # 词典 bundle，仅复用其 summary/evidence
    ev = bundle.evidence
    valid = [
        {
            "id": f"F{i}",
            "claim": f"发现{i}",
            "evidence_refs": [c["id"]],
            "action": f"由客服部在微博渠道跟进处理（对应F{i}）。",
        }
        for i, c in enumerate(ev[:3], start=1)
    ]
    invalid = [
        {
            "id": "F9",
            "claim": "坏条",
            "evidence_refs": [],
            "action": "无渠道建议",
        }
    ]
    # 部分坏 → llm 模式，保留 3 条
    content = build_report_content(
        FakeInsights({"chart_insights": {"overall": "x"}, "findings": valid + invalid}),
        plan, bundle.summary, ev,
    )
    assert content["insight_mode"] == "llm"
    assert len(content["findings"]) == 3
    assert "F9" not in [f["id"] for f in content["findings"]]
    # 全坏 → 规则兜底
    content2 = build_report_content(
        FakeInsights({"chart_insights": {"overall": "x"}, "findings": invalid}),
        plan, bundle.summary, ev,
    )
    assert content2["insight_mode"] == "template_fallback"
    assert content2["findings"]
    print("✓ build_report_content 部分/整组降级通过")


def test_build_report_content_mock_mode() -> None:
    """build_report_content：Mock（词典）模式走规则 findings，12 段图表解析。"""
    from app.coding.insights import build_report_content
    from app.coding.llm_analyzer import MockAnalyzer
    from app.core.planner import build_plan, generate_keyword_groups
    from app.domains.loader import load_domain

    schema = load_domain("consumer")
    groups = generate_keyword_groups("欧莱雅", schema, [d.id for d in schema.dimensions])
    plan = build_plan(
        subject="欧莱雅",
        domain_id="consumer",
        dimension_ids=[d.id for d in schema.dimensions],
        keyword_groups=groups,
        manual_keywords=None,
        channel_ids=["demo"],
        date_start=dt.date.today() - dt.timedelta(days=30),
        date_end=dt.date.today(),
        comments_enabled=True,
        comments_per_post=3,
        llm_enabled=False,
        narrative_enabled=False,
    )
    bundle = TaskRunner(plan).run()
    content = build_report_content(
        MockAnalyzer(), plan, bundle.summary, bundle.evidence
    )
    assert content["insight_mode"] == "lexicon"
    assert set(content["chart_insights"]) == {
        "overall", "platform", "trend", "dimensions", "heatmap", "words",
        "intensity", "radar", "platform_dim", "date_dim", "wordcloud",
        "cooccurrence",
    }
    # F-018（2026-08-26，修订版）：词典模式单区合并——findings 置空、conclusion 空，
    # structured_summary 唯一承担统计结论与解读
    assert content["findings"] == [], "词典模式 findings 应置空"
    assert not content["conclusion"], "词典模式 conclusion 应为空"
    assert content["structured_summary"] and content["structured_summary"].get("overall")
    assert content["structured_summary_source"] == "rule"
    print("✓ 词典模式统一入口通过（F-018 单区合并）")


def test_coder_dimensions_sync() -> None:
    """F-032：LLM 判出新维度 → dimensions 同步回填（新任务维度不错位）。"""
    from unittest import mock

    import app.coding.coder as coder_mod
    from app.coding.coder import Coder
    from app.core.models import AnalysisPlan, Post
    from app.core.planner import build_plan

    class FakeLLM:
        def analyze_batch(self, texts, on_batch_progress=None, dimension_schema=None, **kw):
            return [
                {
                    "sentiment": "negative",
                    "score": -0.6,
                    "confidence": 0.9,
                    "keywords": ["卡顿"],
                    "dimension_sentiments": {"performance": "negative"},
                }
                for _ in texts
            ]

        def analyze_narrative(self, texts, **kw):
            return [{"narrative": None, "attribution": None} for _ in texts]

        @property
        def errors(self):
            return []

    plan = build_plan(
        subject="测试机", domain_id=None, dimension_ids=[],
        keyword_groups=[], manual_keywords=["测试机"], channel_ids=["bilibili"],
        date_start=None, date_end=None, comments_enabled=False,
        comments_per_post=0, llm_enabled=True, narrative_enabled=False,
    )
    posts = [
        Post(
            id="p1", platform="bilibili", keyword="测试机", author="u",
            title="t", content="卡顿严重", url="http://x/p1",
            timestamp="2026-08-27", likes=0, reposts=0, comments_count=0,
            comments=[], platform_specific={},
        )
    ]
    with mock.patch.object(coder_mod, "OpenAICompatibleAnalyzer", FakeLLM):
        coder = Coder(FakeLLM(), schema=None)
        items = coder.code_posts(posts, plan)
    assert items and items[0].method == "llm"
    assert items[0].dimension_sentiments.get("performance") == "negative"
    assert "performance" in items[0].dimensions, (
        f"LLM 判出新维度应同步回填 dimensions，实际 {items[0].dimensions}"
    )
    print("✓ F-032 LLM 维度同步：dimensions 并集回填 通过")


def test_display_finding_helpers() -> None:
    """展示层：内部 F1 → "发现 1"；action 里的（对应F1）→（对应发现1），不误伤 E#/S#。"""
    from app.core.evidence import (
        display_action,
        display_finding_id,
        findings_section_title,
    )

    assert display_finding_id("F1") == "发现 1"
    assert display_finding_id("F12") == "发现 12"
    assert display_finding_id("") == ""
    assert display_finding_id("E1") == "E1"
    assert findings_section_title("review_refresh") == "数据发现（已按复核结果刷新）"
    assert (
        display_action("由客服部在微博渠道跟进处理（对应F1）。")
        == "由客服部在微博渠道跟进处理（对应发现1）。"
    )
    assert display_action("详见F2与E1，统计S2均支持") == "详见发现2与E1，统计S2均支持"
    assert display_action("证据E1与统计S2均支持") == "证据E1与统计S2均支持"
    print("✓ 发现编号展示 helper 通过")


def test_dimension_evidence_label() -> None:
    """维度负面原文标题后缀：n 与判定来源聚合到维度级（不再逐条重复）。"""
    from app.core.evidence import dimension_evidence_label

    llm_cards = [
        {"kind": "text", "judge": "llm", "n": 14},
        {"kind": "text", "judge": "llm", "n": 14},
    ]
    assert dimension_evidence_label(llm_cards) == "负面样本 n=14 · LLM 判定"
    lex_cards = [{"kind": "text", "judge": "lexicon", "n": 3}]
    assert dimension_evidence_label(lex_cards) == "负面样本 n=3 · 词典判定 · 仅供参考"
    mix_cards = [
        {"kind": "text", "judge": "llm", "n": 14},
        {"kind": "text", "judge": "lexicon", "n": 14},
    ]
    assert dimension_evidence_label(mix_cards) == "负面样本 n=14 · 判定混合（lexicon / llm）"
    assert dimension_evidence_label([]) == "负面样本 n=0"
    print("✓ 维度级 n/判定聚合 label 通过")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_evidence_cascade_deterministic()
    test_two_tier_n_guard()
    test_denoise_profanity_pii_truncate()
    test_need_review_and_ad_population()
    test_zero_data_short_circuit()
    test_lexicon_findings_guardrails()
    test_validate_llm_findings()
    test_build_report_content_mock_mode()
    test_coder_dimensions_sync()
    test_display_finding_helpers()
    test_dimension_evidence_label()
    print("证据链测试全部通过 ✅")
