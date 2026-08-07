"""HTML 交互报告：Plotly 图表（离线嵌入）+ Jinja2 模板。"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from jinja2 import Environment, FileSystemLoader
from plotly.io import to_html

from app.core.models import ReportBundle

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
SENTIMENT_COLORS = {"positive": "#16a34a", "negative": "#dc2626", "neutral": "#94a3b8"}
SENTIMENT_NAMES = {"positive": "正面", "negative": "负面", "neutral": "中性"}


def overall_fig(s: dict) -> go.Figure:
    dist = s["sentiment_distribution"]
    fig = go.Figure(
        go.Pie(
            labels=[SENTIMENT_NAMES[k] for k in ["positive", "negative", "neutral"]],
            values=[dist[k]["count"] for k in ["positive", "negative", "neutral"]],
            hole=0.45,
            marker=dict(colors=[SENTIMENT_COLORS[k] for k in ["positive", "negative", "neutral"]]),
            textinfo="label+percent",
        )
    )
    fig.update_layout(title="整体情感占比", height=360, margin=dict(l=20, r=20, t=50, b=20))
    return fig


def platform_fig(s: dict) -> go.Figure:
    platforms = s["platforms"]
    fig = go.Figure(
        [
            go.Bar(name="正面", x=list(platforms.keys()), y=[v["positive"] for v in platforms.values()], marker_color="#16a34a"),
            go.Bar(name="负面", x=list(platforms.keys()), y=[v["negative"] for v in platforms.values()], marker_color="#dc2626"),
            go.Bar(name="中性", x=list(platforms.keys()), y=[v["neutral"] for v in platforms.values()], marker_color="#94a3b8"),
        ]
    )
    fig.update_layout(barmode="stack", title="各平台情感分布", height=360, margin=dict(l=20, r=20, t=50, b=20))
    return fig


def trend_fig(s: dict) -> go.Figure:
    trend = s["trend"]
    if not trend:
        return go.Figure().update_layout(title="暂无趋势数据")
    df = pd.DataFrame(
        [
            {"日期": d, "内容量": v["count"], "平均评分": v["avg_score"], "负面数": v["negative"]}
            for d, v in trend.items()
        ]
    )
    return make_subplots_dual(df)


def make_subplots_dual(df: pd.DataFrame) -> go.Figure:
    from plotly.subplots import make_subplots

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=df["日期"], y=df["内容量"], name="内容量", marker_color="#93c5fd"), secondary_y=False)
    fig.add_trace(go.Scatter(x=df["日期"], y=df["平均评分"], name="平均评分", mode="lines+markers", line=dict(color="#dc2626")), secondary_y=True)
    fig.update_layout(title="情感趋势（内容量 + 平均评分）", height=380, margin=dict(l=20, r=20, t=50, b=20))
    fig.update_yaxes(title_text="内容量", secondary_y=False)
    fig.update_yaxes(title_text="平均评分", secondary_y=True)
    return fig


def dimensions_fig(s: dict) -> go.Figure | None:
    dims = s["dimensions"]
    if not dims:
        return None
    return make_subplots_dual_dim(dims)


def make_subplots_dual_dim(dims: dict) -> go.Figure:
    from plotly.subplots import make_subplots

    names = list(dims.keys())
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Bar(x=names, y=[dims[n]["count"] for n in names], name="讨论量", marker_color="#93c5fd"),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(x=names, y=[dims[n]["negative_rate"] for n in names], name="负面率", mode="lines+markers", line=dict(color="#dc2626")),
        secondary_y=True,
    )
    fig.update_layout(title="维度讨论量与负面率", height=380, margin=dict(l=20, r=20, t=50, b=20))
    fig.update_yaxes(title_text="讨论量", secondary_y=False)
    fig.update_yaxes(title_text="负面率", secondary_y=True, tickformat=".0%")
    return fig


def heatmap_fig(s: dict) -> go.Figure | None:
    dims = s["dimensions"]
    if not dims:
        return None
    # MVP：维度负面率热力（单行矩阵）；平台×维度矩阵在 V1 补充
    names = list(dims.keys())
    rates = [[dims[d]["negative_rate"] for d in names]]
    fig = go.Figure(
        go.Heatmap(
            z=rates,
            x=names,
            y=["负面率"],
            colorscale="Reds",
            zmin=0,
            zmax=1,
            text=[[f"{r * 100:.1f}%" for r in rates[0]]],
            texttemplate="%{text}",
            showscale=False,
        )
    )
    fig.update_xaxes(side="top")
    fig.update_layout(height=360, margin=dict(l=20, r=20, t=50, b=20))
    return fig


def words_fig(s: dict) -> go.Figure:
    words = s["top_words"]
    if not words:
        return go.Figure().update_layout(title="暂无高频词")
    fig = go.Figure(
        go.Bar(
            x=[c for _, c in reversed(words)],
            y=[w for w, _ in reversed(words)],
            orientation="h",
            marker_color="#2563eb",
        )
    )
    fig.update_layout(title="高频情感词 Top 20", height=420, margin=dict(l=20, r=20, t=50, b=20))
    return fig


def intensity_fig(s: dict) -> go.Figure:
    """情绪强度直方图（1~5 级）。"""
    dist = s.get("intensity_distribution", {})
    levels = [str(i) for i in range(1, 6)]
    counts = [dist.get(lv, 0) for lv in levels]
    fig = go.Figure(
        go.Bar(x=levels, y=counts, marker_color=["#93c5fd", "#60a5fa", "#3b82f6", "#2563eb", "#1e3a8a"])
    )
    fig.update_layout(
        title="情绪强度分布（1=微弱 ~ 5=强烈）",
        xaxis_title="强度等级",
        yaxis_title="文本数",
        height=360,
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


def radar_fig(s: dict) -> go.Figure | None:
    """各维度负面率/平均分雷达图。"""
    dims = s.get("dimensions", {})
    if not dims:
        return None
    names = list(dims.keys())
    neg_rates = [dims[d]["negative_rate"] for d in names]
    avg_scores = [max(0.0, (dims[d]["avg_score"] + 1) / 2) for d in names]  # -1~1 → 0~1
    fig = go.Figure()
    fig.add_trace(
        go.Scatterpolar(
            r=neg_rates + [neg_rates[0]],
            theta=names + [names[0]],
            fill="toself",
            name="负面率（越高越差）",
            line_color="#dc2626",
        )
    )
    fig.add_trace(
        go.Scatterpolar(
            r=avg_scores + [avg_scores[0]],
            theta=names + [names[0]],
            fill="toself",
            name="平均分映射（越高越好）",
            line_color="#16a34a",
        )
    )
    fig.update_layout(
        title="各维度负面率 / 平均分雷达图",
        polar=dict(radialaxis=dict(range=[0, 1], tickformat=".0%")),
        height=420,
        margin=dict(l=60, r=60, t=60, b=40),
    )
    return fig


def platform_dim_fig(s: dict) -> go.Figure | None:
    """平台 × 维度负面率分组柱状图。"""
    pd_data = s.get("platform_dim", {})
    dims = s.get("dimensions", {})
    if not pd_data or not dims:
        return None
    dim_names = list(dims.keys())
    fig = go.Figure()
    colors = px.colors.qualitative.Set2
    for i, (platform, dmap) in enumerate(pd_data.items()):
        fig.add_trace(
            go.Bar(
                name=platform,
                x=dim_names,
                y=[dmap.get(d, {}).get("negative_rate", 0) for d in dim_names],
                marker_color=colors[i % len(colors)],
            )
        )
    fig.update_layout(
        barmode="group",
        title="各平台 × 维度负面率对比",
        xaxis_title="维度",
        yaxis_title="负面率",
        yaxis_tickformat=".0%",
        height=420,
        margin=dict(l=20, r=20, t=50, b=40),
    )
    return fig


def _cjk_font() -> str | None:
    for p in [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/msyh.ttf",
    ]:
        if Path(p).exists():
            return p
    return None


def wordcloud_png_bytes(s: dict) -> bytes | None:
    """用 jieba 高频词生成词云 PNG（无依赖可用时返回 None）。"""
    words = dict(s.get("top_content_words", [])[:60])
    if not words:
        return None
    try:
        from wordcloud import WordCloud
    except ImportError:
        return None
    try:
        wc = WordCloud(
            font_path=_cjk_font(),
            width=900,
            height=450,
            background_color="white",
            collocations=False,
            random_state=42,
        )
        img = wc.generate_from_frequencies(words).to_image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _wordcloud_data_uri(s: dict) -> str:
    data = wordcloud_png_bytes(s)
    if not data:
        return ""
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def cooccurrence_fig(s: dict) -> go.Figure | None:
    """关键词共现网络图（networkx 布局 + plotly 渲染）。"""
    edges = s.get("cooccurrence", [])
    if not edges:
        return None
    try:
        import networkx as nx
    except ImportError:
        return None
    g = nx.Graph()
    for e in edges[:25]:
        g.add_edge(e["source"], e["target"], weight=e["weight"])
    if not g.nodes:
        return None
    pos = nx.spring_layout(g, seed=42, k=0.6)
    node_size = [g.degree(n) * 400 + 800 for n in g.nodes]
    fig = go.Figure()
    for a, b, w in g.edges(data=True):
        fig.add_trace(
            go.Scatter(
                x=[pos[a][0], pos[b][0], None],
                y=[pos[a][1], pos[b][1], None],
                mode="lines",
                line=dict(width=1 + w["weight"] * 0.3, color="#cbd5e1"),
                hoverinfo="skip",
                showlegend=False,
            )
        )
    fig.add_trace(
        go.Scatter(
            x=[pos[n][0] for n in g.nodes],
            y=[pos[n][1] for n in g.nodes],
            mode="markers+text",
            text=[n for n in g.nodes],
            textposition="top center",
            marker=dict(size=node_size, color="#3b82f6", opacity=0.75),
            name="关键词",
        )
    )
    fig.update_layout(
        title="关键词共现网络图（前 25 对共现）",
        height=520,
        showlegend=False,
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        margin=dict(l=10, r=10, t=50, b=10),
    )
    return fig


def date_dim_heatmap_fig(s: dict) -> go.Figure | None:
    """日期 × 维度负面率热力图。"""
    dd = s.get("date_dim", {})
    dims = s.get("dimensions", {})
    if not dd or not dims:
        return None
    dates = list(dd.keys())
    dim_names = list(dims.keys())
    z = []
    text = []
    for d in dates:
        row = []
        trow = []
        for dim in dim_names:
            v = dd[d].get(dim)
            if v and v["count"] > 0:
                row.append(v["negative_rate"])
                trow.append(f"{v['negative_rate'] * 100:.0f}%")
            else:
                row.append(None)
                trow.append("")
        z.append(row)
        text.append(trow)
    fig = go.Figure(
        go.Heatmap(
            z=z,
            x=dim_names,
            y=dates,
            colorscale="Reds",
            zmin=0,
            zmax=1,
            text=text,
            texttemplate="%{text}",
            hovertemplate="%{x}<br>%{y}<br>负面率 %{z}<extra></extra>",
        )
    )
    fig.update_layout(
        title="日期 × 维度 负面率热力图",
        height=max(360, 24 * len(dates) + 120),
        yaxis=dict(autorange="reversed"),
        margin=dict(l=20, r=20, t=50, b=40),
    )
    return fig


def _chart_overall(s: dict) -> str:
    return to_html(overall_fig(s), full_html=False, include_plotlyjs=True)


def _chart_platform(s: dict) -> str:
    return to_html(platform_fig(s), full_html=False, include_plotlyjs=False)


def _chart_trend(s: dict) -> str:
    return to_html(trend_fig(s), full_html=False, include_plotlyjs=False)


def _chart_dimensions(s: dict) -> str:
    fig = dimensions_fig(s)
    return to_html(fig, full_html=False, include_plotlyjs=False) if fig else ""


def _chart_heatmap(s: dict) -> str:
    fig = heatmap_fig(s)
    return to_html(fig, full_html=False, include_plotlyjs=False) if fig else ""


def _chart_words(s: dict) -> str:
    return to_html(words_fig(s), full_html=False, include_plotlyjs=False)


def _chart_intensity(s: dict) -> str:
    return to_html(intensity_fig(s), full_html=False, include_plotlyjs=False)


def _chart_radar(s: dict) -> str:
    fig = radar_fig(s)
    return to_html(fig, full_html=False, include_plotlyjs=False) if fig else ""


def _chart_platform_dim(s: dict) -> str:
    fig = platform_dim_fig(s)
    return to_html(fig, full_html=False, include_plotlyjs=False) if fig else ""


def _chart_cooccurrence(s: dict) -> str:
    fig = cooccurrence_fig(s)
    return to_html(fig, full_html=False, include_plotlyjs=False) if fig else ""


def _chart_date_dim(s: dict) -> str:
    fig = date_dim_heatmap_fig(s)
    return to_html(fig, full_html=False, include_plotlyjs=False) if fig else ""


def build_html(bundle: ReportBundle) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=False)
    template = env.get_template("report.html.j2")
    s = bundle.summary
    dist = s["sentiment_distribution"]
    collected = sum(
        len(ch.posts) + len(ch.dropped)
        for ch in bundle.channel_results
        if ch.ok
    )
    dropped_n = sum(len(ch.dropped) for ch in bundle.channel_results)
    comment_n = sum(
        1 for ch in bundle.channel_results for p in ch.posts for _ in p.comments
    )
    llm_count = sum(1 for it in bundle.coded_items if it.method == "llm")
    llm_corrected = int(s.get("llm_corrected") or 0)
    usage = bundle.llm_usage or {}
    trust = {
        "collected": collected,
        "dropped": dropped_n,
        "drop_rate": round(dropped_n / collected * 100, 1) if collected else 0,
        "comments": comment_n,
        "llm_count": llm_count,
        "llm_ratio": round(llm_count / max(s["total_items"], 1) * 100, 1),
        "llm_corrected": llm_corrected,
        "date_range": (
            f"{bundle.plan.date_start} ~ {bundle.plan.date_end}"
            if bundle.plan.date_start or bundle.plan.date_end
            else "不限"
        ),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "estimated_cost": float(usage.get("estimated_cost") or 0),
    }
    narrative_rows = [
        {
            "text": it.text[:100],
            "narrative": it.narrative.value if it.narrative else "",
            "attribution": it.attribution or "",
        }
        for it in bundle.coded_items
        if it.narrative or it.attribution
    ]
    return template.render(
        subject=bundle.plan.subject,
        created_at=bundle.created_at.strftime("%Y-%m-%d %H:%M"),
        keyword_count=len(bundle.plan.keywords),
        channel_count=len(bundle.channel_results),
        total_posts=s["total_posts"],
        total_items=s["total_items"],
        warnings=bundle.warnings,
        overall_sentiment=s["overall_sentiment"],
        avg_score=s["avg_score"],
        pos_ratio=round(dist["positive"]["ratio"] * 100, 1),
        neg_ratio=round(dist["negative"]["ratio"] * 100, 1),
        neu_ratio=round(dist["neutral"]["ratio"] * 100, 1),
        chart_overall=_chart_overall(s),
        chart_platform=_chart_platform(s),
        chart_trend=_chart_trend(s),
        chart_dimensions=_chart_dimensions(s),
        chart_heatmap=_chart_heatmap(s),
        chart_words=_chart_words(s),
        chart_intensity=_chart_intensity(s),
        chart_radar=_chart_radar(s),
        chart_platform_dim=_chart_platform_dim(s),
        chart_wordcloud=_wordcloud_data_uri(s),
        chart_cooccurrence=_chart_cooccurrence(s),
        chart_date_dim=_chart_date_dim(s),
        top_words=s["top_words"],
        report_text=bundle.report_text,
        chart_insights=bundle.chart_insights,
        conclusion=bundle.conclusion,
        narrative_rows=narrative_rows,
        trust=trust,
        method_counts={
            "llm": sum(1 for it in bundle.coded_items if it.method == "llm"),
            "lexicon": sum(1 for it in bundle.coded_items if it.method == "lexicon"),
        },
    )
