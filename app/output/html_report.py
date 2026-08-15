"""HTML 交互报告：Plotly 图表（离线嵌入）+ Jinja2 模板。"""

from __future__ import annotations

import base64
import io
import re
from collections import Counter
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from jinja2 import Environment, FileSystemLoader
from plotly.io import to_html

from app.core.models import ReportBundle
from app.core.names import dimension_cn, platform_cn
from app.core.keyword_effects import extract_funnel

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
    pnames = [platform_cn(p) for p in platforms.keys()]
    fig = go.Figure(
        [
            go.Bar(name="正面", x=pnames, y=[v["positive"] for v in platforms.values()], marker_color="#16a34a"),
            go.Bar(name="负面", x=pnames, y=[v["negative"] for v in platforms.values()], marker_color="#dc2626"),
            go.Bar(name="中性", x=pnames, y=[v["neutral"] for v in platforms.values()], marker_color="#94a3b8"),
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
    cnames = [dimension_cn(n) for n in names]
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Bar(x=cnames, y=[dims[n]["count"] for n in names], name="讨论量", marker_color="#93c5fd"),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(x=cnames, y=[dims[n]["negative_rate"] for n in names], name="负面率", mode="lines+markers", line=dict(color="#dc2626")),
        secondary_y=True,
    )
    fig.update_layout(title="维度评价量与负面率", height=380,
                      margin=dict(l=20, r=20, t=50, b=20))
    fig.update_yaxes(title_text="讨论量", secondary_y=False)
    fig.update_yaxes(title_text="负面率", secondary_y=True, tickformat=".0%")
    return fig


def heatmap_fig(s: dict) -> go.Figure | None:
    dims = s["dimensions"]
    if not dims:
        return None
    names = list(dims.keys())
    cnames = [dimension_cn(n) for n in names]
    rates_row = []
    texts_row = []
    for d in names:
        v = dims[d]
        if v["count"] >= 3:
            rates_row.append(v["negative_rate"])
            texts_row.append(f"{v['negative_rate'] * 100:.0f}%")
        else:
            rates_row.append(None)
            texts_row.append(f"n={v['count']}")
    fig = go.Figure(
        go.Heatmap(
            z=[rates_row],
            x=cnames,
            y=["负面率"],
            colorscale="Reds",
            zmin=0,
            zmax=1,
            text=[texts_row],
            texttemplate="%{text}",
            showscale=False,
        )
    )
    fig.update_xaxes(side="top")
    fig.update_layout(
        title="维度负面率（n<3 样本不评级）",
        height=360,
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


def words_fig(s: dict) -> go.Figure:
    """高频情感词：按词典极性分"正面 Top / 负面 Top"展示（|情感分| 加权）。"""
    from plotly.subplots import make_subplots

    pos = s.get("positive_words") or []
    neg = s.get("negative_words") or []
    if not pos and not neg:
        return go.Figure().update_layout(title="暂无高频情感词")
    fig = make_subplots(
        rows=2,
        subplot_titles=("正面情感词 Top 10", "负面情感词 Top 10"),
        vertical_spacing=0.22,
    )
    if pos:
        fig.add_trace(
            go.Bar(
                x=[c for _, c in reversed(pos)],
                y=[w for w, _ in reversed(pos)],
                orientation="h",
                marker_color="#16a34a",
                name="正面",
            ),
            row=1,
            col=1,
        )
    if neg:
        fig.add_trace(
            go.Bar(
                x=[c for _, c in reversed(neg)],
                y=[w for w, _ in reversed(neg)],
                orientation="h",
                marker_color="#dc2626",
                name="负面",
            ),
            row=2,
            col=1,
        )
    fig.update_layout(
        title="高频情感词（情感分加权，按正负分列）",
        height=540,
        showlegend=False,
        margin=dict(l=20, r=20, t=60, b=20),
    )
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
    cnames = [dimension_cn(d) for d in names]
    neg_rates = [dims[d]["negative_rate"] for d in names]
    avg_scores = [max(0.0, (dims[d]["avg_score"] + 1) / 2) for d in names]  # -1~1 → 0~1
    fig = go.Figure()
    fig.add_trace(
        go.Scatterpolar(
            r=neg_rates + [neg_rates[0]],
            theta=cnames + [cnames[0]],
            fill="toself",
            name="负面率（越高越差）",
            line_color="#dc2626",
        )
    )
    fig.add_trace(
        go.Scatterpolar(
            r=avg_scores + [avg_scores[0]],
            theta=cnames + [cnames[0]],
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
    cdim_names = [dimension_cn(d) for d in dim_names]
    fig = go.Figure()
    colors = px.colors.qualitative.Set2
    for i, (platform, dmap) in enumerate(pd_data.items()):
        fig.add_trace(
            go.Bar(
                name=platform_cn(platform),
                x=cdim_names,
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


def wordcloud_png_bytes(s: dict, which: str = "positive") -> bytes | None:
    """按情感/维度生成词云 PNG（无依赖或数据为空时返回 None）。

    which: positive（正面词云）/ negative（负面词云）/ worst_dim（负面率最高维度词云）。
    """
    key = {
        "positive": "positive_wordcloud",
        "negative": "negative_wordcloud",
        "worst_dim": "worst_dim_wordcloud",
    }.get(which, "positive_wordcloud")
    words = dict(s.get(key, [])[:60])
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


def _wordcloud_data_uri(s: dict, which: str = "positive") -> str:
    data = wordcloud_png_bytes(s, which)
    if not data:
        return ""
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def cooccurrence_fig(s: dict) -> go.Figure | None:
    """关键词共现网络图（PMI 关联；节点颜色=情感，大小=度数，悬停看维度）。"""
    edges = s.get("cooccurrence", [])
    if not edges:
        return None
    try:
        import networkx as nx
    except ImportError:
        return None
    g = nx.Graph()
    for e in edges:
        g.add_edge(
            e["source"], e["target"],
            weight=float(e.get("weight", 0)),
            count=e.get("count", 0),
        )
    if not g.nodes:
        return None
    pos = nx.spring_layout(g, seed=42, k=0.6)
    wmin = min((d["weight"] for _, _, d in g.edges(data=True)), default=0.0)
    wmax = max((d["weight"] for _, _, d in g.edges(data=True)), default=1.0)
    wspan = (wmax - wmin) or 1.0
    word_dims = s.get("word_dims") or {}
    node_neg = s.get("node_negative_rate") or {}
    degree = dict(g.degree())
    max_deg = max(degree.values()) or 1

    def _node_color(n: str) -> str:
        share = node_neg.get(n)
        if share is None:
            return "#94a3b8"  # 样本不足/中性
        if share >= 0.6:
            return "#dc2626"  # 偏负面
        if share <= 0.4:
            return "#16a34a"  # 偏正面
        return "#94a3b8"  # 中性

    node_colors = [_node_color(n) for n in g.nodes]
    hover_texts = [
        (
            f"<b>{n}</b><br>负面倾向 {node_neg.get(n, 0.0) * 100:.0f}%"
            f"<br>主要维度：{word_dims.get(n, '未标注')}"
        )
        for n in g.nodes
    ]
    fig = go.Figure()
    for a, b, d in g.edges(data=True):
        wnorm = (d["weight"] - wmin) / wspan
        fig.add_trace(
            go.Scatter(
                x=[pos[a][0], pos[b][0], None],
                y=[pos[a][1], pos[b][1], None],
                mode="lines",
                line=dict(width=1 + wnorm * 6, color="#cbd5e1"),
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
            textfont=dict(size=10),
            hovertext=hover_texts,
            hoverinfo="text",
            marker=dict(
                size=[600 + degree[n] / max_deg * 2400 for n in g.nodes],
                color=node_colors,
                line=dict(width=1, color="#64748b"),
                opacity=0.9,
            ),
            name="关键词",
        )
    )
    fig.update_layout(
        title="讨论话题共现网络（颜色=话题负面倾向：红负面 / 绿正面 / 灰中性）",
        height=560,
        showlegend=False,
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        margin=dict(l=10, r=10, t=60, b=10),
    )
    return fig


def sentiment_sources_fig(s: dict) -> go.Figure | None:
    """负面情绪来源话题榜：负面占比 >50% 且讨论量 ≥5 的话题词。"""
    sources = [
        r
        for r in (s.get("sentiment_sources") or [])
        if r["negative_rate"] > 0.5
    ]
    if not sources:
        return None
    rows = sorted(sources, key=lambda r: r["negative_rate"])[:10]
    labels = [f"{r['word']}（{r['positive'] + r['negative']} 条）" for r in rows]
    rates = [r["negative_rate"] for r in rows]
    fig = go.Figure(
        go.Bar(
            x=rates,
            y=labels,
            orientation="h",
            marker_color="#dc2626",
            text=[f"{r * 100:.0f}%" for r in rates],
            textposition="outside",
        )
    )
    fig.update_layout(
        title="负面情绪来源话题榜（负面占比，讨论量≥5）",
        height=360,
        xaxis_title="负面占比",
        xaxis_tickformat=".0%",
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


def date_dim_heatmap_fig(s: dict) -> go.Figure | None:
    """日期 × 维度负面率热力图（n<3 不评级；自适应文字色；日期轴）。"""
    dd = s.get("date_dim", {})
    dims = s.get("dimensions", {})
    if not dd or not dims:
        return None
    dates = sorted({d for d in dd.keys() if re.match(r"^\d{4}-\d{2}-\d{2}$", d)})
    if not dates:
        return None
    dim_names = list(dims.keys())
    cdim_names = [dimension_cn(d) for d in dim_names]
    z = []
    for d in dates:
        row = []
        for dim in dim_names:
            v = dd[d].get(dim)
            if v and v["count"] >= 3:
                row.append(v["negative_rate"])
            else:
                row.append(None)
        z.append(row)
    fig = go.Figure(
        go.Heatmap(
            z=z,
            x=cdim_names,
            y=dates,
            colorscale="Reds",
            zmin=0,
            zmax=1,
            hovertemplate="%{x}<br>%{y}<br>负面率 %{z:.0%}<extra></extra>",
        )
    )
    # 逐格标注：自适应黑白文字；小样本只显示 n（灰）
    for i, d in enumerate(dates):
        for j, dim in enumerate(dim_names):
            v = dd[d].get(dim)
            if not v or v["count"] <= 0:
                continue
            if v["count"] >= 3:
                rate = v["negative_rate"]
                txt = f"{rate * 100:.0f}%"
                color = "white" if rate > 0.55 else "#333333"
            else:
                txt = f"n={v['count']}"
                color = "#9ca3af"
            fig.add_annotation(
                x=cdim_names[j],
                y=d,
                text=txt,
                showarrow=False,
                font=dict(color=color, size=10),
            )
    fig.update_layout(
        title="日期 × 维度 负面率热力图（n<3 样本不评级）",
        height=max(360, 22 * len(dates) + 140),
        yaxis=dict(type="date", autorange="reversed"),
        margin=dict(l=20, r=20, t=60, b=40),
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


def _chart_sources(s: dict) -> str:
    fig = sentiment_sources_fig(s)
    return to_html(fig, full_html=False, include_plotlyjs=False) if fig else ""


def _chart_date_dim(s: dict) -> str:
    fig = date_dim_heatmap_fig(s)
    return to_html(fig, full_html=False, include_plotlyjs=False) if fig else ""


def keyword_rows(bundle: ReportBundle) -> tuple[list[dict], int]:
    """按确认关键词聚合采集漏斗与情感统计（HTML/Word 报告共用）。

    采集 = 保留帖子 + 该关键词的丢弃记录；有效供给率 = 保留 / 采集。
    丢弃记录在 2.2 埋点后才带 keyword，旧数据未归属的单独返回数量。
    """
    s = bundle.summary
    kw_stats = s.get("keyword_stats") or {}
    posts_by_kw: Counter[str] = Counter()
    dropped_by_kw: Counter[str] = Counter()
    unattributed = 0
    for ch in bundle.channel_results:
        if not ch.ok:
            continue
        for p in ch.posts:
            posts_by_kw[p.keyword or ""] += 1
        for d in ch.dropped or []:
            kw = d.get("keyword") or ""
            if kw:
                dropped_by_kw[kw] += 1
            else:
                unattributed += 1
    keys = sorted(set(kw_stats) | set(posts_by_kw) | set(dropped_by_kw))
    rows: list[dict] = []
    for kw in keys:
        stats = kw_stats.get(kw) or {}
        collected = posts_by_kw.get(kw, 0) + dropped_by_kw.get(kw, 0)
        kept = posts_by_kw.get(kw, 0)
        nr = stats.get("negative_rate")
        rows.append({
            "keyword": kw or "（未标记）",
            "collected": collected,
            "kept": kept,
            "dropped": dropped_by_kw.get(kw, 0),
            "effective_rate": f"{kept / collected * 100:.1f}%" if collected else "—",
            "coded": stats.get("coded", 0),
            "positive": stats.get("positive", 0),
            "negative": stats.get("negative", 0),
            "neutral": stats.get("neutral", 0),
            "negative_rate": f"{nr * 100:.1f}%" if nr is not None else "—",
        })
    return rows, unattributed


def query_rows(bundle: ReportBundle) -> tuple[list[dict], int]:
    """实际查询串粒度漏斗（仅 WebSearch，与评测中心口径一致）。

    采集/保留/丢弃按 (渠道, 关键词, 查询串) 聚合；编码与负面数按
    (渠道, 关键词) 归因（与评测中心相同口径）；旧数据丢弃无 query 时
    按该关键词在渠道内的唯一查询串兜底。
    """
    report = bundle.model_dump(mode="json")
    rows = [
        r for r in extract_funnel(report).get("funnel", [])
        if str(r.get("channel", "")).startswith("websearch")
    ]
    display = []
    for r in rows:
        er = r.get("effective_rate")
        nr = r.get("negative_rate")
        display.append({
            "query": r.get("query", ""),
            "channel": r.get("channel", ""),
            "collected": r.get("collected", 0),
            "kept": r.get("kept", 0),
            "dropped": r.get("dropped", 0),
            "effective_rate": f"{er * 100:.1f}%" if er is not None else "—",
            "coded": r.get("coded", 0),
            "negative": r.get("negative", 0),
            "negative_rate": f"{nr * 100:.1f}%" if nr is not None else "—",
        })
    unattr_ws = sum(
        1
        for ch in bundle.channel_results
        if ch.ok and str(ch.channel_id).startswith("websearch")
        for d in (ch.dropped or [])
        if not d.get("keyword")
    )
    return display, unattr_ws


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
    dimension_note = (
        "⚠ 本次为词典模式：维度情感为词典轻量估算，仅供参考；"
        "开启 LLM 精分析后维度情感更准确。"
        if llm_count == 0
        else ""
    )
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
    keyword_rows_out, unattributed_dropped = keyword_rows(bundle)
    query_rows_out, unattributed_query_dropped = query_rows(bundle)
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
        dimension_note=dimension_note,
        chart_heatmap=_chart_heatmap(s),
        chart_words=_chart_words(s),
        chart_intensity=_chart_intensity(s),
        chart_radar=_chart_radar(s),
        chart_platform_dim=_chart_platform_dim(s),
        chart_wordcloud_pos=_wordcloud_data_uri(s, "positive"),
        chart_wordcloud_neg=_wordcloud_data_uri(s, "negative"),
        chart_wordcloud_worst=_wordcloud_data_uri(s, "worst_dim"),
        worst_dim_name=dimension_cn(s.get("worst_dim_id", "")),
        chart_cooccurrence=_chart_cooccurrence(s),
        chart_sources=_chart_sources(s),
        chart_date_dim=_chart_date_dim(s),
        top_words=s["top_words"],
        report_text=bundle.report_text,
        chart_insights=bundle.chart_insights,
        conclusion=bundle.conclusion,
        narrative_rows=narrative_rows,
        trust=trust,
        keyword_rows=keyword_rows_out,
        unattributed_dropped=unattributed_dropped,
        query_rows=query_rows_out,
        unattributed_query_dropped=unattributed_query_dropped,
        method_counts={
            "llm": sum(1 for it in bundle.coded_items if it.method == "llm"),
            "lexicon": sum(1 for it in bundle.coded_items if it.method == "lexicon"),
        },
    )
