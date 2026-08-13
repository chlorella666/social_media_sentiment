"""报告洞察回归测试：每张图表有解析文字，报告含叙事框架深度结论。"""

from __future__ import annotations

import datetime as dt
import sys
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.pipeline import TaskRunner
from app.core.planner import build_plan, generate_keyword_groups
from app.core.models import (
    AnalysisPlan,
    ChannelResult,
    CodedItem,
    Post,
    ReportBundle,
    SentimentLabel,
)
from app.domains.loader import load_domain
from docx import Document as DocxDocument
from app.output.html_report import build_html, query_rows
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
    assert "责任归因" in bundle.conclusion

    html = build_html(bundle)
    assert "深度结论与建议" in html
    assert 'class="insight"' in html
    assert "情感与分布" in html
    assert "情绪强度" in html
    assert "情绪来源与讨论结构" in html
    assert "关键词效果（按搜索关键词）" in html
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
    assert "关键词效果（按搜索关键词）" in word_text
    assert "有效供给率" in word_text
    print(f"✓ HTML/Word 报告包含 12 段图表解析、叙事框架深度结论，Word 嵌入 {len(doc.inline_shapes)} 张图表图片")


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


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_report_contains_chart_insights_and_conclusion()
    test_query_rows_websearch_funnel()
    print("报告洞察测试通过 ✅")
