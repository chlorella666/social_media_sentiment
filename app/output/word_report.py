"""Word 静态报告（python-docx）。

MVP 输出文本与统计表格；V1 将图表渲染为 PNG 后嵌入。
"""

from __future__ import annotations

from io import BytesIO

from docx import Document
from docx.shared import Inches, Pt

from app.core.models import ReportBundle
from app.output.html_report import (
    cooccurrence_fig,
    date_dim_heatmap_fig,
    dimensions_fig,
    heatmap_fig,
    intensity_fig,
    overall_fig,
    platform_dim_fig,
    platform_fig,
    radar_fig,
    trend_fig,
    wordcloud_png_bytes,
    words_fig,
)

SENTIMENT_NAMES = {"positive": "正面", "negative": "负面", "neutral": "中性"}

CHART_BUILDERS = [
    ("overall", "整体情感占比", overall_fig),
    ("platform", "各平台情感分布", platform_fig),
    ("trend", "情感趋势（内容量 + 平均评分 + 负面率）", trend_fig),
    ("dimensions", "维度讨论量与负面率", dimensions_fig),
    ("heatmap", "维度负面率热力图", heatmap_fig),
    ("intensity", "情绪强度直方图", intensity_fig),
    ("radar", "各维度负面率/平均分雷达图", radar_fig),
    ("platform_dim", "平台 × 维度负面率", platform_dim_fig),
    ("date_dim", "日期 × 维度负面率热力图", date_dim_heatmap_fig),
    ("words", "高频情感词 Top20", words_fig),
    ("cooccurrence", "关键词共现网络图", cooccurrence_fig),
]

_calc_fig = None


def _get_calc_fig():
    """kaleido v1：启动一次渲染服务，之后所有图表共用（大幅提速）。"""
    global _calc_fig
    if _calc_fig is None:
        try:
            from kaleido import calc_fig_sync, start_sync_server

            start_sync_server(silence_warnings=True)
            _calc_fig = calc_fig_sync
        except Exception:
            _calc_fig = False
    return _calc_fig or None


def _add_chart_image(doc: Document, fig, title: str) -> None:
    """用 kaleido 把 plotly 图渲染成 PNG 嵌入 Word。"""
    calc = _get_calc_fig()
    if calc is None:
        return
    try:
        data = calc(
            fig,
            opts=dict(format="png", width=850, height=480, scale=1.1),
        )
    except Exception:
        return
    doc.add_picture(BytesIO(data), width=Inches(6.2))
    run = doc.add_paragraph().add_run(title)
    run.bold = True


def build_word(bundle: ReportBundle) -> BytesIO:
    doc = Document()
    doc.add_heading(f"社交媒体情感分析报告 — {bundle.plan.subject}", level=0)
    doc.add_paragraph(f"生成时间：{bundle.created_at.strftime('%Y-%m-%d %H:%M')}")

    s = bundle.summary
    doc.add_heading("一、分析概览", level=1)
    overview = doc.add_table(rows=0, cols=2)
    overview.style = "Table Grid"
    for k, v in [
        ("分析对象", bundle.plan.subject),
        ("关键词数", str(len(bundle.plan.keywords))),
        ("帖子数", str(s["total_posts"])),
        ("编码文本数", str(s["total_items"])),
        ("整体倾向", s["overall_sentiment"]),
        ("平均情感分", str(s["avg_score"])),
    ]:
        cells = overview.add_row().cells
        cells[0].text, cells[1].text = k, v

    doc.add_heading("二、情感分布", level=1)
    dist = s["sentiment_distribution"]
    table = doc.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    hdr = table.rows[0].cells
    hdr[0].text, hdr[1].text, hdr[2].text = "情感", "数量", "占比"
    for key, name in [("positive", "正面"), ("negative", "负面"), ("neutral", "中性")]:
        row = table.add_row().cells
        row[0].text = name
        row[1].text = str(dist[key]["count"])
        row[2].text = f"{dist[key]['ratio'] * 100:.1f}%"

    doc.add_heading("三、平台统计", level=1)
    ptable = doc.add_table(rows=1, cols=6)
    ptable.style = "Table Grid"
    hdr = ptable.rows[0].cells
    for i, name in enumerate(["平台", "内容数", "平均评分", "正面", "负面", "中性"]):
        hdr[i].text = name
    for pid, v in s["platforms"].items():
        row = ptable.add_row().cells
        row[0].text = pid
        row[1].text = str(v["posts"])
        row[2].text = str(v["avg_score"])
        row[3].text = str(v["positive"])
        row[4].text = str(v["negative"])
        row[5].text = str(v["neutral"])

    if s["dimensions"]:
        doc.add_heading("四、维度分析", level=1)
        dtable = doc.add_table(rows=1, cols=4)
        dtable.style = "Table Grid"
        hdr = dtable.rows[0].cells
        for i, name in enumerate(["维度", "讨论量", "负面数", "负面率"]):
            hdr[i].text = name
        for did, v in s["dimensions"].items():
            row = dtable.add_row().cells
            row[0].text = did
            row[1].text = str(v["count"])
            row[2].text = str(v["negative"])
            row[3].text = f"{v['negative_rate'] * 100:.1f}%"

    doc.add_heading("五、图表与解析", level=1)
    s = bundle.summary
    for cid, title, builder in CHART_BUILDERS:
        fig = builder(s)
        if fig is None:
            continue
        _add_chart_image(doc, fig, title)
        insight = bundle.chart_insights.get(cid, "")
        if insight:
            p = doc.add_paragraph(insight)
            p.paragraph_format.first_line_indent = Pt(24)
    wc_bytes = wordcloud_png_bytes(s)
    if wc_bytes:
        doc.add_picture(BytesIO(wc_bytes), width=Inches(6.2))
        run = doc.add_paragraph().add_run("内容关键词词云（jieba 分词）")
        run.bold = True
        wc_insight = bundle.chart_insights.get("wordcloud", "")
        if wc_insight:
            p = doc.add_paragraph(wc_insight)
            p.paragraph_format.first_line_indent = Pt(24)

    doc.add_heading("六、深度结论与建议（按叙事框架）", level=1)
    if bundle.conclusion:
        for line in bundle.conclusion.split("\n"):
            if line.strip():
                doc.add_paragraph(line)
    else:
        for line in bundle.report_text.split("\n"):
            p = doc.add_paragraph(line)
            p.paragraph_format.first_line_indent = Pt(24)

    doc.add_heading("七、概览", level=1)
    for line in bundle.report_text.split("\n"):
        p = doc.add_paragraph(line)
        p.paragraph_format.first_line_indent = Pt(24)

    if bundle.warnings:
        doc.add_heading("八、注意事项", level=1)
        for w in bundle.warnings:
            doc.add_paragraph(f"- {w}")

    out = BytesIO()
    doc.save(out)
    out.seek(0)
    return out
