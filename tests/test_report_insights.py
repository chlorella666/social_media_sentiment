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
from app.domains.loader import load_domain
from docx import Document as DocxDocument
from app.output.html_report import build_html
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
    assert "关键词共现网络" in html

    word = build_word(bundle).getvalue()
    assert len(word) > 1000
    doc = DocxDocument(BytesIO(word))
    assert len(doc.inline_shapes) >= 5, f"Word 报告应嵌入图表图片，实际 {len(doc.inline_shapes)} 张"
    print(f"✓ HTML/Word 报告包含 12 段图表解析、叙事框架深度结论，Word 嵌入 {len(doc.inline_shapes)} 张图表图片")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_report_contains_chart_insights_and_conclusion()
    print("报告洞察测试通过 ✅")
