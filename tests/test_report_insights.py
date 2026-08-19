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
    assert bundle.conclusion
    # 报告证据链：findings 结构 + n 守卫 + 词典模式措辞
    assert bundle.insight_mode == "lexicon"
    assert bundle.findings, "词典模式应生成规则 findings"
    for f in bundle.findings:
        assert f["id"].startswith("F")
        assert f["claim"]
        assert isinstance(f["evidence_refs"], list)
        assert f["action"], "词典模式应含方向性提示"
    assert bundle.evidence, "应有证据卡"
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
    # 病灶 E 回归：负面>正面时不得出现"中性/正面为主"
    dist = bundle.summary["sentiment_distribution"]
    if dist["negative"]["count"] > dist["positive"]["count"]:
        assert "中性/正面为主" not in bundle.conclusion
    # 病灶 A 回归：不得出现万能句
    assert "扩大采集范围后复测" not in bundle.report_text
    assert "扩大采集范围后复测" not in bundle.conclusion

    html = build_html(bundle)
    assert "数据发现（词典模式）" in html
    assert "本次为词典模式，维度情感与结论仅供参考" in html
    assert "核心发现" in html or "数据发现" in html
    assert "报告统计" in html
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
    print("报告洞察测试通过 ✅")
