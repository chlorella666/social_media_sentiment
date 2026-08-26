"""报告洞察回归测试：每张图表有解析文字，报告含叙事框架深度结论。"""

from __future__ import annotations

import datetime as dt
import sys
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.pipeline import TaskRunner
from app.core.pipeline import build_summary
from app.core.planner import build_plan, generate_keyword_groups
from app.core.models import (
    AnalysisPlan,
    ChannelResult,
    CodedItem,
    NarrativeFrame,
    Post,
    ReportBundle,
    SentimentLabel,
)
from app.domains.loader import load_domain
from docx import Document as DocxDocument
from app.output.html_report import (
    build_html,
    cooccurrence_plan,
    narrative_insight_text,
    query_rows,
    topic_cluster_rows,
)
from app.output.word_report import build_word


def _plan():
    schema = load_domain("consumer")
    groups = generate_keyword_groups("欧莱雅", schema, [d.id for d in schema.dimensions])
    return build_plan(
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


def test_report_contains_chart_insights_and_conclusion() -> None:
    bundle = TaskRunner(_plan()).run()
    assert len(bundle.chart_insights) == 12
    assert bundle.chart_insights.get("overall")
    assert bundle.chart_insights.get("intensity")
    assert bundle.chart_insights.get("cooccurrence")
    # F-018（2026-08-26，修订版）：词典模式单区合并——findings 置空、
    # conclusion 空（structured_summary 唯一承担统计结论与解读）
    assert bundle.insight_mode == "lexicon"
    assert bundle.findings == [], "词典模式 findings 应置空（单区合并）"
    assert not bundle.conclusion, "词典模式 conclusion 应为空（解读区承担）"
    assert bundle.structured_summary and bundle.structured_summary.get("overall"), (
        "词典模式 structured_summary 应非空"
    )
    assert bundle.structured_summary_source == "rule"
    assert bundle.evidence, "应有证据卡（解读区「查看证据与原文」支撑）"
    for c in bundle.evidence:
        assert c["id"].startswith("E") or c["id"].startswith("S")
        assert c["text"]
        if c.get("kind") == "stat":
            assert c["judge"] == "stat"
        else:
            assert len(c["text"]) <= 81
            assert c["platform"] and c["judge"] in ("llm", "lexicon")
            assert c["n"] >= 1
    assert any(c.get("kind") == "stat" for c in bundle.evidence)
    # 病灶 E 回归：负面>正面时 structured_summary 不得出现"中性/正面为主"
    dist = bundle.summary["sentiment_distribution"]
    if dist["negative"]["count"] > dist["positive"]["count"]:
        assert "中性/正面为主" not in bundle.structured_summary.get("overall", "")
    # 病灶 A 回归：不得出现万能句
    assert "扩大采集范围后复测" not in bundle.report_text
    assert "扩大采集范围后复测" not in bundle.structured_summary.get("overall", "")

    html = build_html(bundle)
    assert "解读与建议" in html
    assert "本次为词典模式，维度情感与结论仅供参考" in html
    assert "查看证据与原文（支撑材料）" in html
    assert "支撑材料" in html
    assert 'class="insight"' in html
    assert "情感与分布" in html
    assert "情绪强度" in html
    assert "情绪来源与讨论结构" in html
    assert "附录：关键词效果与采集明细" in html
    # 2026-08-19：关键词效果/实际查询串移到"方法与数据说明"之后（附录）
    assert html.find("方法与数据说明") < html.find("附录：关键词效果与采集明细")
    assert "有效供给率" in html

    word = build_word(bundle).getvalue()
    assert len(word) > 1000
    doc = DocxDocument(BytesIO(word))
    assert len(doc.inline_shapes) >= 5, f"Word 报告应嵌入图表图片，实际 {len(doc.inline_shapes)} 张"
    word_parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            word_parts.extend(cell.text for cell in row.cells)
    word_text = "\n".join(word_parts)
    assert "附录：关键词效果与采集明细" in word_text
    assert "有效供给率" in word_text
    assert "数据发现（词典模式）" in word_text
    assert "本次为词典模式，维度情感与结论仅供参考" in word_text
    assert "负面原文 Top 3" in word_text
    print(f"✓ HTML/Word 报告包含 12 段图表解析、证据链（findings/evidence/banner），Word 嵌入 {len(doc.inline_shapes)} 张图表图片")


def test_old_report_without_evidence_degrades() -> None:
    """旧 result.json（无 findings/evidence 字段）降级显示不报错。"""
    from app.core.pipeline import generate_report_text

    bundle = TaskRunner(_plan()).run()
    old = bundle.model_copy(
        update={
            "findings": [],
            "evidence": [],
            "insight_mode": "",
            "report_text": generate_report_text(bundle.plan, bundle.summary, []),
        }
    )
    html = build_html(old)
    assert "深度结论与建议（按叙事框架）" in html
    assert "核心发现" not in html
    word = build_word(old).getvalue()
    doc = DocxDocument(BytesIO(word))
    assert "深度结论与建议" in "\n".join(p.text for p in doc.paragraphs)
    print("✓ 旧报告（无新字段）三端降级不报错")


def test_query_rows_websearch_funnel() -> None:
    """实际查询串粒度：只统计 WebSearch，采集/保留/丢弃按查询串聚合。"""
    plan = AnalysisPlan(subject="恋与深空", keywords=["恋与深空"])
    ch = ChannelResult(
        channel_id="websearch", ok=True,
        posts=[
            Post(
                id="u1", platform="websearch", keyword="恋与深空",
                title="t1", content="c1",
                platform_specific={"query": "恋与深空 评价"},
            ),
        ],
        dropped=[
            {"platform": "websearch", "keyword": "恋与深空",
             "query": "恋与深空 评价", "reason": "不相关"},
        ],
    )
    coded = [
        CodedItem(text_id="c1", text="c1", platform="websearch",
                  keyword="恋与深空", sentiment=SentimentLabel.negative),
    ]
    bundle = ReportBundle(plan=plan, channel_results=[ch], coded_items=coded, summary={})
    rows, unattr = query_rows(bundle)
    assert unattr == 0
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["query"] == "恋与深空 评价"
    assert r["channel"] == "websearch"
    assert r["collected"] == 2 and r["kept"] == 1 and r["dropped"] == 1
    assert r["effective_rate"] == "50.0%"
    assert r["coded"] == 1 and r["negative"] == 1 and r["negative_rate"] == "100.0%"
    print("✓ 实际查询串漏斗（报告粒度，仅 WebSearch）通过")


def _narr_item(
    tid: str,
    sent: SentimentLabel,
    narrative: NarrativeFrame | None = None,
    attribution: str | None = None,
    method: str = "llm",
) -> CodedItem:
    return CodedItem(
        text_id=tid, text=tid, platform="weibo", keyword="测试",
        sentiment=sent, method=method,
        narrative=narrative, attribution=attribution,
    )


def test_narrative_stats_aggregation() -> None:
    """叙事聚合口径：样本=LLM且叙事字段非空；分母 gave_attr/gave_frame；
    unclear 固定最后；交叉一致（frame_actor 求和 ≤ by_frame）。"""
    plan = AnalysisPlan(subject="测试", keywords=["测试"], narrative_enabled=True)
    items = [
        _narr_item("a", SentimentLabel.negative, NarrativeFrame.conflict, "enterprise"),
        _narr_item("b", SentimentLabel.negative, NarrativeFrame.conflict, "enterprise"),
        _narr_item("c", SentimentLabel.positive, NarrativeFrame.conflict, "enterprise"),
        _narr_item("d", SentimentLabel.negative, NarrativeFrame.attribution, "individual"),
        _narr_item("e", SentimentLabel.negative, None, "unclear"),
        _narr_item("f", SentimentLabel.negative, NarrativeFrame.morality, None),
        _narr_item("g", SentimentLabel.neutral, method="lexicon"),  # 词典不计入
        _narr_item("h", SentimentLabel.negative, NarrativeFrame.conflict, "enterprise"),
    ]
    ns = build_summary(plan, items, [])["narrative_stats"]
    assert ns["total"] == 7 and ns["gave_attr"] == 6 and ns["gave_frame"] == 6
    assert ns["unclear"] == 1
    actors = [r["actor"] for r in ns["by_actor"]]
    assert actors == ["enterprise", "individual", "unclear"], actors
    ent = ns["by_actor"][0]
    assert ent["count"] == 4 and ent["negative"] == 3 and ent["negative_rate"] == 0.75
    assert ns["by_actor"][1]["ref"] is True  # individual n=1 <10
    frames = [r["frame"] for r in ns["by_frame"]]
    assert frames == ["conflict", "attribution", "morality"], frames
    # 交叉一致性：frame_actor 求和 ≤ by_frame；本样本 conflict 全带归因故相等
    assert sum(c["count"] for c in ns["frame_actor"]["conflict"].values()) == 4
    for f, cells in ns["frame_actor"].items():
        fb = next(r for r in ns["by_frame"] if r["frame"] == f)
        assert sum(c["count"] for c in cells.values()) <= fb["count"]
    # 罗技式部分数据：有归因无框架不互相污染
    print("✓ 叙事聚合：样本/分母/unclear/排序/交叉一致 通过")


def test_narrative_report_rendering() -> None:
    """叙事区块渲染守卫：total≥10 出两图；0<total<10 只出提示；缺失不渲染。"""
    bundle = TaskRunner(_plan()).run()
    stats_ok = {
        "total": 12, "gave_attr": 12, "gave_frame": 12, "unclear": 1,
        "by_actor": [
            {"actor": "enterprise", "count": 10, "positive": 2, "neutral": 2,
             "negative": 6, "negative_rate": 0.6, "ref": False},
            {"actor": "unclear", "count": 2, "positive": 0, "neutral": 2,
             "negative": 0, "negative_rate": 0.0, "ref": True},
        ],
        "by_frame": [
            {"frame": "conflict", "count": 12, "positive": 2, "neutral": 4,
             "negative": 6, "negative_rate": 0.5},
        ],
        "frame_actor": {
            "conflict": {"enterprise": {"count": 10, "negative": 6,
                                        "negative_rate": 0.6}},
        },
    }
    bundle.summary["narrative_stats"] = stats_ok
    html = build_html(bundle)
    assert "叙事框架与归因" in html
    assert "归因主体分布" in html and "叙事框架 × 归因主体负面率" in html
    bundle.summary["narrative_stats"] = {**stats_ok, "total": 5}
    html_small = build_html(bundle)
    assert "样本不足" in html_small and "归因主体分布" not in html_small
    # total≥10 但归因/框架全空 → 兜底提示，不出空图
    bundle.summary["narrative_stats"] = {
        **stats_ok, "by_actor": [], "by_frame": [], "frame_actor": {},
    }
    html_empty = build_html(bundle)
    assert "归因/框架样本不足" in html_empty
    assert "归因主体分布" not in html_empty
    bundle.summary.pop("narrative_stats", None)
    html_none = build_html(bundle)
    assert "叙事框架与归因" not in html_none
    print("✓ 叙事区块守卫：≥10 出图 / <10 提示 / 缺失不渲染 通过")


def test_narrative_insight_rules() -> None:
    """解读规则：负面率<50% 不断言"问题归因"；≥50% 才断言；
    unclear 占比>30% 追加复核提示。"""
    base = {"total": 30, "gave_attr": 30, "gave_frame": 30, "unclear": 0,
            "by_frame": [], "frame_actor": {}}
    weak = {
        **base,
        "by_actor": [{"actor": "enterprise", "count": 20, "positive": 12,
                      "neutral": 0, "negative": 8, "negative_rate": 0.4,
                      "ref": False}],
    }
    lines = narrative_insight_text({"total_items": 30, "narrative_stats": weak})
    assert not any("问题归因" in l for l in lines)
    assert any("讨论量最高的归因主体【企业】" in l for l in lines)
    strong = {
        **base,
        "by_actor": [{"actor": "enterprise", "count": 20, "positive": 2,
                      "neutral": 4, "negative": 14, "negative_rate": 0.7,
                      "ref": False}],
    }
    lines2 = narrative_insight_text({"total_items": 30, "narrative_stats": strong})
    assert any("用户主要将问题归因于【企业】" in l for l in lines2)
    unclear = {
        **base, "unclear": 12,
        "by_actor": [{"actor": "enterprise", "count": 20, "positive": 2,
                      "neutral": 4, "negative": 14, "negative_rate": 0.7,
                      "ref": False},
                     {"actor": "unclear", "count": 12, "positive": 0,
                      "neutral": 12, "negative": 0, "negative_rate": 0.0,
                      "ref": True}],
    }
    lines3 = narrative_insight_text({"total_items": 30, "narrative_stats": unclear})
    assert any("归因不明确" in l for l in lines3)
    print("✓ 叙事解读规则：负面率下限 / unclear 提示 通过")


def _big_graph_summary() -> dict:
    """双星+桥：16 节点 15 边，Louvain 应分出 2 簇（单簇占比<90%）。
    满足网络图门槛（节点≥15 且 边≥12）。"""
    edges = []
    for n in "bcdefg":
        edges.append({"source": "a", "target": n, "weight": 1.5, "count": 5})
    for n in "ijklmn":
        edges.append({"source": "h", "target": n, "weight": 1.4, "count": 5})
    edges.append({"source": "a", "target": "p", "weight": 1.2, "count": 4})
    edges.append({"source": "h", "target": "o", "weight": 1.1, "count": 4})
    edges.append({"source": "d", "target": "h", "weight": 0.6, "count": 3})
    nodes = {x for e in edges for x in (e["source"], e["target"])}
    node_count = {n: (10 if n in ("a", "h") else 4) for n in nodes}
    return {
        "total_items": 60,
        "cooccurrence": edges,
        "node_count": node_count,
        "node_negative_count": {n: 2 for n in nodes},
        "node_positive_count": {n: 3 for n in nodes},
        "node_negative_rate": {n: 0.4 for n in nodes},
    }


def _small_graph_summary() -> dict:
    """OPPO 式小图：9 节点 7 边 → 词对榜（节点<12，不聚类）。"""
    edges = [
        {"source": "a", "target": "b", "weight": 1.5, "count": 5},
        {"source": "a", "target": "c", "weight": 1.2, "count": 4},
        {"source": "a", "target": "d", "weight": 1.1, "count": 4},
        {"source": "d", "target": "e", "weight": 0.8, "count": 3},
        {"source": "e", "target": "f", "weight": 1.4, "count": 5},
        {"source": "e", "target": "g", "weight": 1.3, "count": 4},
        {"source": "e", "target": "h", "weight": 1.0, "count": 3},
        {"source": "e", "target": "i", "weight": 0.9, "count": 3},
    ]
    node_count = {n: {"a": 8, "b": 5, "c": 4, "d": 4, "e": 7, "f": 5,
                      "g": 4, "h": 3, "i": 3}[n] for n in
                  {x for e in edges for x in (e["source"], e["target"])}}
    return {
        "total_items": 60,
        "cooccurrence": edges,
        "node_count": node_count,
        "node_negative_count": {n: 2 for n in node_count},
        "node_positive_count": {n: 3 for n in node_count},
        "node_negative_rate": {n: 0.4 for n in node_count},
    }


def test_cooccurrence_plan_branches() -> None:
    """共现降级三分支：network / pairs（小图）/ skip（样本不足）。"""
    s = _big_graph_summary()
    assert cooccurrence_plan(s)["kind"] == "network"
    assert cooccurrence_plan({**s, "total_items": 19})["kind"] == "skip"
    small = _small_graph_summary()
    assert cooccurrence_plan(small)["kind"] == "pairs"
    sparse_edges = s["cooccurrence"][:5]
    assert cooccurrence_plan({**s, "cooccurrence": sparse_edges})["kind"] == "pairs"
    print("✓ cooccurrence_plan 三分支 通过")


def test_cooccurrence_plan_networkx_missing() -> None:
    """networkx 缺失 → kind=pairs 词对榜（不聚类也能展示证据）。"""
    import sys

    saved = sys.modules.get("networkx")
    sys.modules["networkx"] = None
    try:
        kind = cooccurrence_plan(_big_graph_summary())["kind"]
    finally:
        if saved is None:
            sys.modules.pop("networkx", None)
        else:
            sys.modules["networkx"] = saved
    assert kind == "pairs"
    print("✓ networkx 缺失降级 通过")


def test_topic_pairs_rows() -> None:
    """话题词对榜：按共现文本数降序，保留 PMI。"""
    from app.output.html_report import topic_pairs

    rows = topic_pairs(_small_graph_summary())
    assert rows and rows[0]["count"] >= rows[-1]["count"]
    assert all({"source", "target", "count", "pmi"} <= set(r) for r in rows)
    print("✓ 话题词对榜（共现数降序 + PMI） 通过")


def test_report_discussion_structure_branches() -> None:
    """HTML 讨论结构三分支：network→网络图+簇榜单；pairs→词对榜；skip→无词对表。"""
    bundle = TaskRunner(_plan()).run()
    s_pairs = dict(bundle.summary)
    s_pairs["topic_clusters"] = {
        "kind": "pairs", "reason": "共现边不足（4 < 10），已显示话题词对",
        "pairs": [{"source": "光电", "target": "华星", "count": 3, "pmi": 2.6}],
        "clusters": [], "node_cluster": {},
    }
    s_pairs["cooccurrence"] = [
        {"source": "光电", "target": "华星", "count": 3, "weight": 2.6}
    ]
    html_pairs = build_html(bundle.model_copy(update={"summary": s_pairs}))
    assert "话题词对榜" in html_pairs and "光电 — 华星" in html_pairs
    s_net = dict(bundle.summary)
    s_net["topic_clusters"] = {
        "kind": "network", "reason": "",
        "clusters": [{
            "members": ["数码", "推荐"], "name": "数码", "words": ["数码", "推荐"],
            "doc_count": 12, "negative_docs": 3, "total_sent": 6,
            "negative_rate": 0.5,
        }],
        "node_cluster": {"数码": 0, "推荐": 0},
    }
    s_net["cooccurrence"] = [
        {"source": "数码", "target": "推荐", "count": 6, "weight": 2.4}
    ]
    html_net = build_html(bundle.model_copy(update={"summary": s_net}))
    assert "话题簇榜单" in html_net
    assert "讨论话题共现网络（颜色=话题簇" in html_net
    s_skip = dict(bundle.summary)
    s_skip["topic_clusters"] = {
        "kind": "skip", "reason": "有效文本不足（10 < 20）",
        "clusters": [], "node_cluster": {},
    }
    s_skip["cooccurrence"] = []
    html_skip = build_html(bundle.model_copy(update={"summary": s_skip}))
    assert "话题词对榜" not in html_skip
    print("✓ HTML 讨论结构三分支渲染 通过")


def test_cluster_rows_deterministic() -> None:
    """话题簇榜单：两次渲染簇划分一致（Louvain 固定 seed）。"""
    s = _big_graph_summary()
    rows1 = topic_cluster_rows(s)
    rows2 = topic_cluster_rows(s)
    assert rows1 and rows1 == rows2
    assert all(r["doc_count"] >= 0 and r["negative_rate"] for r in rows1)
    print("✓ 话题簇榜单确定性 通过")


def test_topic_cluster_union_doc_count() -> None:
    """簇文档数 = 至少含簇内任一节点词的独立文本数（并集），非词频求和。
    回归：OPPO"数码"簇求和 29 vs 独立文本 13（2026-08-19 用户质疑）。"""
    from app.core.topics import build_topic_clusters

    edges = [
        {"source": "数码", "target": "推荐", "weight": 1.5, "count": 4},
        {"source": "数码", "target": "选购指南", "weight": 1.2, "count": 3},
        {"source": "推荐", "target": "选购指南", "weight": 1.1, "count": 3},
        {"source": "视频", "target": "微博", "weight": 1.4, "count": 4},
        {"source": "华为", "target": "荣耀", "weight": 1.3, "count": 4},
        {"source": "苹果", "target": "华为", "weight": 1.0, "count": 3},
        {"source": "苹果", "target": "荣耀", "weight": 0.9, "count": 3},
        {"source": "外观", "target": "手感", "weight": 1.2, "count": 4},
        {"source": "手感", "target": "屏幕", "weight": 1.1, "count": 4},
        {"source": "屏幕", "target": "电池", "weight": 1.0, "count": 3},
        {"source": "电池", "target": "外观", "weight": 0.8, "count": 3},
        {"source": "续航", "target": "充电", "weight": 1.1, "count": 4},
        {"source": "充电", "target": "快充", "weight": 1.0, "count": 3},
        {"source": "快充", "target": "续航", "weight": 0.9, "count": 3},
    ]
    node_count = {"数码": 4, "推荐": 3, "选购指南": 2, "视频": 5,
                  "微博": 3, "华为": 4, "荣耀": 3, "苹果": 3,
                  "外观": 3, "手感": 3, "屏幕": 3, "电池": 3,
                  "续航": 3, "充电": 3, "快充": 3}
    texts = [
        "数码 推荐 选购指南",   # 含 3 个簇词 → 只计 1
        "数码 推荐",            # 含 2 个簇词 → 只计 1
        "数码",
        "视频 微博",
        "视频",
        "微博",
        "华为 荣耀",
        "苹果 华为",
        "完全无关的内容",
        "完全无关的内容二",
    ] * 6  # 60 条文本，簇并集计数应 ×6 而非逐词求和 ×6
    sentiments = (["negative", "negative", "positive", "neutral", "neutral",
                   "neutral", "negative", "negative", "neutral", "neutral"]) * 6
    res = build_topic_clusters(edges, node_count, texts, sentiments)
    assert res["kind"] == "network"
    by_name = {c["name"]: c for c in res["clusters"]}
    assert "数码" in by_name
    assert by_name["数码"]["doc_count"] == 18  # 3 种独立文本 × 6 轮
    assert by_name["数码"]["doc_count"] < sum(
        node_count[w] for w in by_name["数码"]["members"]
    ) * 6  # 求和口径会明显虚高
    assert by_name["数码"]["negative_docs"] == 12  # 前两条 negative × 6
    assert by_name["数码"]["negative_rate"] == round(12 / 18, 3)  # 0.667
    # 覆盖率：node_cluster 覆盖全部节点
    assert set(res["node_cluster"]) == set(node_count)
    print("✓ 话题簇并集口径：独立文本数/负面率 通过")

def test_template_dimension_ids_have_cn_names():
    """模块模板维度 id 应能映射中文名（2026-08-24：修复英文 id 显示）。"""
    import json

    from app.core.names import dimension_cn

    tpl = json.loads(
        (ROOT / "app" / "domains" / "domain_templates.json").read_text(encoding="utf-8")
    )
    bad = []
    for t in tpl["templates"]:
        for dim in t["dimensions"]:
            if dimension_cn(dim["id"]) == dim["id"]:
                bad.append(dim["id"])
    assert not bad, f"模板维度无中文名: {bad}"
    print("✓ 模板维度中文名映射（effectiveness 等） 通过")


def test_trend_weekly_aggregation():
    """趋势图：日期跨度>90 天按周聚合、≤90 天保持逐日（2026-08-24 阈值调整）。"""
    import pandas as pd

    from app.output.html_report import _trend_weekly

    def _df(periods):
        dates = pd.date_range("2026-01-01", periods=periods)
        return pd.DataFrame(
            {
                "日期": [d.strftime("%Y-%m-%d") for d in dates],
                "内容量": [1] * periods,
                "平均评分": [0.5] * periods,
                "负面数": [0] * periods,
            }
        )

    long_df = _df(120)  # 跨度 119 天 > 90 → 按周
    out = _trend_weekly(long_df)
    assert len(out) < len(long_df), ">90 天跨度应聚合成周"
    assert out["内容量"].sum() == 120
    assert abs(out["平均评分"].iloc[0] - 0.5) < 1e-9
    mid_df = _df(60)  # 跨度 59 天 ≤ 90 → 保持逐日
    assert len(_trend_weekly(mid_df)) == 60, "≤90 天跨度保持逐日"
    assert len(_trend_weekly(long_df.head(3))) == 3, "极短跨度保持逐日"
    print("✓ 趋势图日期跨度>90 天按周聚合 通过")


def test_structured_summary_lexicon_discipline() -> None:
    """F-010：规则解读措辞纪律 / 小样本不推测 / 确定性。"""
    from app.coding.rule_insights import (
        FORBIDDEN_STRONG,
        build_structured_summary,
        dimension_interpretation,
    )

    # n<3 不推测
    d1 = dimension_interpretation("价格", 0.8, 2, has_refs=False)
    assert d1 == {"cause": "", "direction": ""}, d1
    # 措辞黑名单：原因必须带"可能"，禁强动作词
    d2 = dimension_interpretation("价格", 0.7, 20, has_refs=True)
    assert d2["cause"] and "可能" in d2["cause"], d2
    for w in FORBIDDEN_STRONG:
        assert w not in d2["cause"] and w not in d2["direction"], w
    # 负面率<50% 不断言"问题归因"
    d3 = dimension_interpretation("服务", 0.3, 20, has_refs=True)
    assert "问题归因" not in d3["cause"], d3
    # 小样本 degrade：不生成 top_issues
    summary = {
        "total_items": 5,
        "sentiment_distribution": {
            "positive": {"count": 1},
            "neutral": {"count": 2},
            "negative": {"count": 2},
        },
        "dimensions": {"price": {"count": 5, "negative_rate": 0.8}},
    }
    ss = build_structured_summary(summary)
    assert ss["degraded"] is True and not ss["top_issues"], ss
    # 确定性：同输入两次输出一致，且≥50% 负面率给出问题归因
    summary2 = {
        "total_items": 30,
        "sentiment_distribution": {
            "positive": {"count": 20},
            "neutral": {"count": 5},
            "negative": {"count": 5},
        },
        "dimensions": {"performance": {"count": 12, "negative_rate": 0.6}},
    }
    a = build_structured_summary(summary2)
    b = build_structured_summary(summary2)
    assert a == b, "确定性失败"
    assert a["top_issues"] and a["top_issues"][0]["cause"], a
    # F-016（2026-08-26）：top_issues 维度显示中文名，不含英文 id
    for _iss in a["top_issues"]:
        assert _iss["name"] != _iss.get("dimension"), f"维度仍为英文 id: {_iss}"
        assert not _iss["name"].isascii(), f"维度名疑似英文: {_iss['name']}"
    print("✓ F-010 规则解读：措辞纪律 / 小样本 / 确定性 / 维度中文名 通过")


def test_structured_contract_four_states() -> None:
    """F-010/F-015：LLM 结构化总结契约四态（解析成功/失败/字段缺失/数字越界）+ 混合架构。"""
    from app.coding.structured_contract import (
        merge_structured_summary,
        parse_llm_structured_summary,
        validate_llm_structured_summary,
    )

    # 态 1：解析成功 + 数字越界字段被丢弃（只留文案）
    ok_json = '{"chart_insights":{},"structured_summary":{"overall":"整体以正面为主","ratio":999,"count":999,"top_issues":[{"name":"乱填","count":99,"rate":9.9,"cause":"可能因 X 相关","direction":"建议关注 Y"}],"improvements":["改进 A","改进 B"]}}'
    raw = parse_llm_structured_summary(ok_json)
    assert raw is not None and raw.get("overall") == "整体以正面为主"
    clean = validate_llm_structured_summary(raw)
    assert clean is not None and clean["overall"] == "整体以正面为主"
    assert "ratio" not in clean and "count" not in clean, "数字字段应被丢弃"
    assert clean["top_issues"][0]["cause"] and clean["top_issues"][0]["direction"]
    # 态 2：解析失败（非法 JSON / 缺 structured_summary 键）
    assert parse_llm_structured_summary("not json") is None
    assert parse_llm_structured_summary('{"chart_insights":{}}') is None
    assert parse_llm_structured_summary("") is None
    # 态 3：字段缺失（overall 为空 / top_issues 非列表）→ 校验失败
    assert validate_llm_structured_summary({"overall": ""}) is None
    assert validate_llm_structured_summary({"overall": "x", "top_issues": "bad"}) is None
    # 态 4：数字越界由系统覆盖——merge 后 ratio/count/rate 来自 summary 而非 LLM
    summary = {
        "total_items": 30,
        "sentiment_distribution": {
            "positive": {"count": 20}, "neutral": {"count": 5}, "negative": {"count": 5},
        },
        "dimensions": {"quality": {"count": 12, "negative_rate": 0.6}},
        "top_phrases": {},
    }
    ss, source = merge_structured_summary(clean, summary)
    assert source == "llm"
    assert abs(ss["positive"]["ratio"] - round(20 / 30, 4)) < 1e-9, "正面占比应由系统计算"
    assert ss["top_issues"] and abs(ss["top_issues"][0]["rate"] - 0.6) < 1e-9
    assert ss["top_issues"][0]["count"] == 12
    # 回退：LLM 缺失 → rule
    ss2, source2 = merge_structured_summary(None, summary)
    assert source2 == "rule" and ss2["overall"]
    print("✓ LLM 结构化总结契约：四态 + 混合架构（数字系统覆盖）通过")


def test_lexicon_single_zone_merge() -> None:
    """F-018（修订版）：词典模式合并为单一「解读与建议」区——findings 置空、
    structured_summary 非空且 source=rule（统计结论由 structured_summary 唯一承担）。"""
    from app.coding.insights import build_report_content
    from app.coding.llm_analyzer import MockAnalyzer

    summary = {
        "total_items": 30,
        "total_posts": 10,
        "sentiment_distribution": {
            "positive": {"count": 20, "ratio": 20 / 30},
            "neutral": {"count": 5, "ratio": 5 / 30},
            "negative": {"count": 5, "ratio": 5 / 30},
        },
        "avg_score": 0.5,
        "overall_sentiment": "正面",
        "platforms": {},
        "trend": {},
        "dimensions": {"quality": {"count": 12, "negative": 5, "negative_rate": 0.6}},
        "intensity_distribution": {},
        "platform_dim": {},
        "date_dim": {},
        "cooccurrence": [],
        "top_phrases": {},
        "topics": [],
    }
    rc = build_report_content(MockAnalyzer(), None, summary, [])
    assert rc["insight_mode"] == "lexicon"
    assert rc["findings"] == [], "词典模式 findings 应置空（单区合并）"
    assert rc["structured_summary"] and rc["structured_summary"].get("overall"), "structured_summary 应非空"
    assert rc["structured_summary_source"] == "rule"
    print("✓ F-018 词典单区合并：findings 置空 + structured_summary 唯一承担 通过")


def test_coding_workflow_contract() -> None:
    """F-021：LLM 编码工作流契约四态（解析/校验/系统反算）。"""
    from app.coding.coding_workflow import (
        merge_topics,
        parse_coding_output,
        validate_coding_output,
    )

    # 解析成功
    ok = '{"topics":[{"name":"续航","type":"pain","attribution":"产品","text_ids":["T1","T2"],"insight":"续航焦虑"}]}'
    raw = parse_coding_output(ok)
    assert raw and raw[0]["name"] == "续航", raw
    # 解析失败
    assert parse_coding_output("not json") is None
    assert parse_coding_output('{"x":1}') is None
    assert parse_coding_output("") is None
    # 校验：非法 type 归一、数字字段丢弃、空 name 剔除
    clean = validate_coding_output([
        {"name": "续航", "type": "bad", "text_ids": ["T1"], "count": 99, "rate": 9.9},
        {"name": "", "text_ids": ["T2"]},
        {"name": "屏幕", "text_ids": []},
    ])
    assert clean and clean[0]["type"] == "neutral", clean
    assert "count" not in clean[0] and "rate" not in clean[0], "数字字段应丢弃"
    assert len(clean) == 1, "空 name/空 text_ids 应剔除"
    # 系统反算：count 去重 + 过滤不存在 id + 情感/极性由系统计算
    class _S:
        def __init__(self, v): self.value = v
    class _It:
        def __init__(self, tid, text, sent):
            self.text_id = tid; self.text = text; self.sentiment = _S(sent)
    items_by_id = {
        "T1": _It("T1", "电池掉电快", "negative"),
        "T2": _It("T2", "续航不行", "negative"),
        "T3": _It("T3", "屏幕易碎", "negative"),
    }
    topics = merge_topics(
        [{"name": "续航", "type": "pain", "text_ids": ["T1", "T2", "T9", "T1"]}],
        items_by_id,
    )
    assert topics and topics[0]["count"] == 2, "text_ids 去重 + 过滤不存在 id"
    assert topics[0]["polarity"] == "negative"
    assert abs(topics[0]["sentiment_weights"]["negative"] - 1.0) < 1e-9
def test_findings_structured_contract() -> None:
    """F-027：核心发现结构化契约四态（claim/scope/detail/action + conclusion_text）。"""
    from app.coding.findings_contract import (
        build_lead,
        parse_insights_output,
        validate_conclusion_text,
        validate_findings,
    )

    ok = ('{"conclusion_text":"整体情绪中性偏负，负面集中在运营与付费","chart_insights":{},'
          '"findings":[{"id":"F1","claim":"运营负面集中","scope":"维度","detail":"负面率 90.3%（n=72），可能与非及时响应相关","evidence_refs":["S1"],"action":"客服部在微博公开流程"}]}')
    data = parse_insights_output(ok)
    assert data and data.get("conclusion_text")
    assert parse_insights_output("not json") is None
    assert parse_insights_output("") is None
    assert validate_conclusion_text(" 总结 ") == "总结"
    assert validate_conclusion_text("") == ""
    assert validate_conclusion_text(None) == ""
    assert validate_conclusion_text(123) == ""
    findings = validate_findings([
        {"id": "F1", "claim": "A", "scope": "维度", "detail": "d1", "action": "a1"},
        {"id": "F2", "claim": "", "scope": "整体"},
        {"id": "F3", "claim": "B", "scope": "乱填", "evidence_refs": ["S1"]},
    ])
    assert len(findings) == 2, findings
    assert findings[0]["scope"] == "维度" and findings[0]["detail"] == "d1"
    assert findings[1]["scope"] == "其他" and "detail" not in findings[1]
    assert build_lead("LLM 总结", {"overall": "规则总结"}) == "LLM 总结"
    assert build_lead("", {"overall": "规则总结"}) == "规则总结"
    assert build_lead("", None) == ""
    print("✓ F-027 核心发现结构化契约四态（claim/scope/detail/action + conclusion_text）通过")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_report_contains_chart_insights_and_conclusion()
    test_query_rows_websearch_funnel()
    test_narrative_stats_aggregation()
    test_narrative_report_rendering()
    test_narrative_insight_rules()
    test_cooccurrence_plan_branches()
    test_cooccurrence_plan_networkx_missing()
    test_topic_pairs_rows()
    test_report_discussion_structure_branches()
    test_cluster_rows_deterministic()
    test_topic_cluster_union_doc_count()
    test_template_dimension_ids_have_cn_names()
    test_trend_weekly_aggregation()
    test_structured_summary_lexicon_discipline()
    test_structured_contract_four_states()
    test_lexicon_single_zone_merge()
    test_coding_workflow_contract()
    test_findings_structured_contract()
    print("报告洞察测试通过 ✅")
