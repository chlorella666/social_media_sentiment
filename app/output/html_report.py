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
from app.core.names import (
    ATTRIBUTION_ACTORS,
    ATTRIBUTION_CN,
    NARRATIVE_CN,
    NARRATIVE_FRAMES,
    dimension_cn,
    platform_cn,
)
from app.core.keyword_effects import extract_funnel
from app.core.evidence import (
    dimension_evidence_label,
    display_action,
    display_finding_id,
    findings_section_title,
)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
SENTIMENT_COLORS = {
    "positive": "#2E9A6E",
    "negative": "#D94A4A",
    "neutral": "#8C8C8C",
}
SENTIMENT_NAMES = {"positive": "正面", "negative": "负面", "neutral": "中性"}
# UX 5.5 色觉符号：颜色之上叠加 ✓/✗/～，色弱用户不依赖颜色也能区分情感
SENTIMENT_SYMBOL = {"positive": "✓", "negative": "✗", "neutral": "～"}
SENTIMENT_LABEL = {
    k: f"{SENTIMENT_NAMES[k]} {SENTIMENT_SYMBOL[k]}" for k in SENTIMENT_NAMES
}


def _rate_symbol(rate: float) -> str:
    """负面率方向符号：≥0.6 ✗ / ≤0.4 ✓ / 中间 ～（配合颜色使用的第二编码）。"""
    if rate >= 0.6:
        return "✗"
    if rate <= 0.4:
        return "✓"
    return "～"

# 配色令牌（方案 §5.5）：簇色 = brand + sentiment 三色 + border，零新增 hex；
# 颜色不表达情感时可用 brand（簇色表达分组，情感由节点边框表达）。
CLUSTER_PALETTE = ["#2B7BD6", "#2E9A6E", "#D94A4A", "#8C8C8C", "#DEE2E6"]

# P1-4：统一图表主题（去默认 Plotly 蓝紫；值全部来自设计令牌灰阶）
_CHART_THEME = {
    "font": dict(
        family="Microsoft YaHei, PingFang SC, Segoe UI, sans-serif",
        size=13,
        color="#3B414D",  # --g-800
    ),
    "paper_bgcolor": "#FFFFFF",  # --c-card
    "plot_bgcolor": "#FFFFFF",   # --c-card
    "gridcolor": "#E9ECEF",      # --g-200
    "zerolinecolor": "#DEE2E6",  # --g-300
    "linecolor": "#DEE2E6",      # --g-300
    "tickcolor": "#5C6370",      # --g-700
    "legendcolor": "#3B414D",    # --g-800
    "titlecolor": "#1A1D23",     # --g-900
}


def _styled(fn):
    """统一图表主题装饰器：白底 + 灰阶坐标轴 + 品牌字体 + 深色标题/图例。"""
    def wrapper(*args, **kwargs):
        fig = fn(*args, **kwargs)
        if fig is None:
            return None
        try:
            fig.update_layout(
                font=_CHART_THEME["font"],
                paper_bgcolor=_CHART_THEME["paper_bgcolor"],
                plot_bgcolor=_CHART_THEME["plot_bgcolor"],
                legend=dict(font=dict(color=_CHART_THEME["legendcolor"])),
                title_font=dict(
                    color=_CHART_THEME["titlecolor"], size=14, family=_CHART_THEME["font"]["family"]
                ),
            )
            fig.update_xaxes(
                gridcolor=_CHART_THEME["gridcolor"],
                zerolinecolor=_CHART_THEME["zerolinecolor"],
                linecolor=_CHART_THEME["linecolor"],
                tickfont=dict(color=_CHART_THEME["tickcolor"]),
            )
            fig.update_yaxes(
                gridcolor=_CHART_THEME["gridcolor"],
                zerolinecolor=_CHART_THEME["zerolinecolor"],
                linecolor=_CHART_THEME["linecolor"],
                tickfont=dict(color=_CHART_THEME["tickcolor"]),
            )
        except Exception:
            pass
        return fig
    return wrapper


def _negative_heat_colorscale() -> list:
    """负面率离散分档（约束 4：不渐变，用 negative 色透明度 0.5/0.7/0.85/1.0）。"""
    return [
        [0.0, "#FFFFFF"],
        [0.25, "rgba(217, 74, 74, 0.5)"],
        [0.5, "rgba(217, 74, 74, 0.7)"],
        [0.75, "rgba(217, 74, 74, 0.85)"],
        [1.0, "rgba(217, 74, 74, 1.0)"],
    ]


@_styled
def overall_fig(s: dict) -> go.Figure:
    dist = s["sentiment_distribution"]
    fig = go.Figure(
        go.Pie(
            labels=[SENTIMENT_LABEL[k] for k in ["positive", "negative", "neutral"]],
            values=[dist[k]["count"] for k in ["positive", "negative", "neutral"]],
            hole=0.45,
            marker=dict(colors=[SENTIMENT_COLORS[k] for k in ["positive", "negative", "neutral"]]),
            textinfo="label+percent",
        )
    )
    fig.update_layout(title="整体情感占比", height=360, margin=dict(l=20, r=20, t=50, b=20))
    return fig


@_styled
def platform_fig(s: dict) -> go.Figure:
    platforms = s["platforms"]
    pnames = [platform_cn(p) for p in platforms.keys()]
    fig = go.Figure(
        [
            go.Bar(name=SENTIMENT_LABEL["positive"], x=pnames, y=[v["positive"] for v in platforms.values()], marker_color=SENTIMENT_COLORS["positive"]),
            go.Bar(name=SENTIMENT_LABEL["negative"], x=pnames, y=[v["negative"] for v in platforms.values()], marker_color=SENTIMENT_COLORS["negative"]),
            go.Bar(name=SENTIMENT_LABEL["neutral"], x=pnames, y=[v["neutral"] for v in platforms.values()], marker_color=SENTIMENT_COLORS["neutral"]),
        ]
    )
    fig.update_layout(barmode="stack", title="各平台情感分布", height=360, margin=dict(l=20, r=20, t=50, b=20))
    return fig


@_styled
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
    fig.add_trace(go.Bar(x=df["日期"], y=df["内容量"], name="内容量", marker_color="rgba(43, 123, 214, 0.6)"), secondary_y=False)
    fig.add_trace(go.Scatter(x=df["日期"], y=df["平均评分"], name="平均评分", mode="lines+markers", line=dict(color=SENTIMENT_COLORS["negative"])), secondary_y=True)
    fig.update_layout(title="情感趋势（内容量 + 平均评分）", height=380, margin=dict(l=20, r=20, t=50, b=20))
    fig.update_yaxes(title_text="内容量", secondary_y=False)
    fig.update_yaxes(title_text="平均评分", secondary_y=True)
    return fig


@_styled
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
        go.Bar(x=cnames, y=[dims[n]["count"] for n in names], name="讨论量", marker_color="rgba(43, 123, 214, 0.6)"),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(x=cnames, y=[dims[n]["negative_rate"] for n in names], name="负面率 ✗", mode="lines+markers", line=dict(color=SENTIMENT_COLORS["negative"])),
        secondary_y=True,
    )
    fig.update_layout(title="维度评价量与负面率", height=380,
                      margin=dict(l=20, r=20, t=50, b=20))
    fig.update_yaxes(title_text="讨论量", secondary_y=False)
    fig.update_yaxes(title_text="负面率", secondary_y=True, tickformat=".0%")
    return fig


@_styled
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
            texts_row.append(
                f"{_rate_symbol(v['negative_rate'])} {v['negative_rate'] * 100:.0f}%"
            )
        else:
            rates_row.append(None)
            texts_row.append(f"n={v['count']}")
    fig = go.Figure(
        go.Heatmap(
            z=[rates_row],
            x=cnames,
            y=["负面率"],
            colorscale=_negative_heat_colorscale(),
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


@_styled
def words_fig(s: dict) -> go.Figure:
    """高频情感词：按词典极性分"正面 Top / 负面 Top"展示（|情感分| 加权）。"""
    from plotly.subplots import make_subplots

    pos = s.get("positive_words") or []
    neg = s.get("negative_words") or []
    if not pos and not neg:
        return go.Figure().update_layout(title="暂无高频情感词")
    fig = make_subplots(
        rows=2,
        subplot_titles=(
            f"正面情感词 {SENTIMENT_SYMBOL['positive']} Top 10",
            f"负面情感词 {SENTIMENT_SYMBOL['negative']} Top 10",
        ),
        vertical_spacing=0.22,
    )
    if pos:
        fig.add_trace(
            go.Bar(
                x=[c for _, c in reversed(pos)],
                y=[w for w, _ in reversed(pos)],
                orientation="h",
                marker_color=SENTIMENT_COLORS["positive"],
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
                marker_color=SENTIMENT_COLORS["negative"],
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


@_styled
def intensity_fig(s: dict) -> go.Figure:
    """情绪强度直方图（1~5 级）。"""
    dist = s.get("intensity_distribution", {})
    levels = [str(i) for i in range(1, 6)]
    counts = [dist.get(lv, 0) for lv in levels]
    # 强度区分只用 brand 同一颜色的透明度（约束 4）
    fig = go.Figure(
        go.Bar(x=levels, y=counts, marker_color=[
            "rgba(43, 123, 214, 0.5)",
            "rgba(43, 123, 214, 0.62)",
            "rgba(43, 123, 214, 0.75)",
            "rgba(43, 123, 214, 0.88)",
            "rgba(43, 123, 214, 1.0)",
        ])
    )
    fig.update_layout(
        title="情绪强度分布（1=微弱 ~ 5=强烈）",
        xaxis_title="强度等级",
        yaxis_title="文本数",
        height=360,
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


@_styled
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
            name=f"负面率 {SENTIMENT_SYMBOL['negative']}（越高越差）",
            line_color=SENTIMENT_COLORS["negative"],
        )
    )
    fig.add_trace(
        go.Scatterpolar(
            r=avg_scores + [avg_scores[0]],
            theta=cnames + [cnames[0]],
            fill="toself",
            name=f"平均分映射 {SENTIMENT_SYMBOL['positive']}（越高越好）",
            line_color=SENTIMENT_COLORS["positive"],
        )
    )
    fig.update_layout(
        title="各维度负面率 / 平均分雷达图",
        polar=dict(radialaxis=dict(range=[0, 1], tickformat=".0%")),
        height=420,
        margin=dict(l=60, r=60, t=60, b=40),
    )
    return fig


@_styled
def platform_dim_fig(s: dict) -> go.Figure | None:
    """平台 × 维度负面率分组柱状图。"""
    pd_data = s.get("platform_dim", {})
    dims = s.get("dimensions", {})
    if not pd_data or not dims:
        return None
    dim_names = list(dims.keys())
    cdim_names = [dimension_cn(d) for d in dim_names]
    fig = go.Figure()
    # 平台色 = 簇色板（令牌派生：brand + sentiment 三色 + border），零新增 hex
    colors = CLUSTER_PALETTE
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
        # 约束 4：词云仅用 sentiment 令牌色（正面=positive 色，负面/最差维度=negative 色）
        _wc_color = (
            SENTIMENT_COLORS["positive"]
            if which == "positive"
            else SENTIMENT_COLORS["negative"]
        )
        wc = WordCloud(
            font_path=_cjk_font(),
            width=900,
            height=450,
            background_color="white",
            collocations=False,
            random_state=42,
            color_func=lambda *_a, **_k: _wc_color,
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


COOCCUR_MIN_TOTAL = 20
COOCCUR_MIN_EDGES = 12
COOCCUR_MIN_NODES = 15
COOCCUR_MAX_CLUSTERS = 5
COOCCUR_PAIRS_MAX = 10


def _louvain_clusters(edges: list[dict], seed: int = 42) -> list[list[str]] | None:
    """Louvain 社区划分（固定 seed 保证确定性；簇数截断为 3~5，
    最小簇合并为"其他"；networkx 不可用返回 None）。"""
    try:
        import networkx as nx
    except Exception:
        return None
    g = nx.Graph()
    for e in edges:
        g.add_edge(
            e["source"], e["target"],
            weight=float(e.get("weight", 0)),
        )
    if g.number_of_nodes() == 0:
        return None
    communities = list(
        nx.community.louvain_communities(g, seed=seed, weight="weight")
    )
    clusters = [sorted(c) for c in communities]
    clusters.sort(key=len, reverse=True)
    if len(clusters) > COOCCUR_MAX_CLUSTERS:
        kept = clusters[: COOCCUR_MAX_CLUSTERS - 1]
        others = [n for c in clusters[COOCCUR_MAX_CLUSTERS - 1:] for n in c]
        kept.append(sorted(others))
        clusters = kept
    return clusters


def cooccurrence_plan(s: dict) -> dict:
    """共现结构判定（三端共用，方案 §4.4）。
    返回 {"kind": "network"|"pairs"|"skip", "reason": str}。"""
    tc = s.get("topic_clusters")
    if isinstance(tc, dict) and tc.get("kind"):
        # 新 summary：判定与簇统计已在 build_summary 层完成（并集口径）
        return {"kind": tc["kind"], "reason": tc.get("reason", "")}
    # 旧 result.json 兜底：无 topic_clusters 时按边/节点/networkx 重判
    total = int(s.get("total_items") or 0)
    if total < COOCCUR_MIN_TOTAL:
        return {"kind": "skip", "reason": f"有效文本不足（{total} < 20）"}
    edges = s.get("cooccurrence") or []
    if not edges:
        return {"kind": "skip", "reason": "无共现数据"}
    if len(edges) < COOCCUR_MIN_EDGES:
        return {"kind": "pairs", "reason": f"共现边不足（{len(edges)} < 12），已显示话题词对"}
    nodes = {e["source"] for e in edges} | {e["target"] for e in edges}
    if len(nodes) < COOCCUR_MIN_NODES:
        return {"kind": "pairs", "reason": f"话题节点不足（{len(nodes)} < 15），已显示话题词对"}
    clusters = _louvain_clusters(edges)
    if clusters is None:
        return {"kind": "pairs", "reason": "networkx 不可用，已显示话题词对"}
    if len(clusters[0]) / len(nodes) > 0.9:
        return {"kind": "pairs", "reason": "讨论未形成明显话题簇，已显示话题词对"}
    return {"kind": "network", "reason": ""}


def topic_pairs(s: dict) -> list[dict]:
    """话题词对榜（方案 Part B 修订 A）：词对 + 共现文本数 + PMI，
    按共现数降序；小图不强行聚类时的诚实证据展示。"""
    tc = s.get("topic_clusters")
    if isinstance(tc, dict) and tc.get("pairs"):
        return tc["pairs"]
    edges = s.get("cooccurrence") or []
    rows = [
        {
            "source": e["source"],
            "target": e["target"],
            "count": int(e.get("count", 0)),
            "pmi": float(e.get("weight", 0)),
        }
        for e in edges
    ]
    rows.sort(key=lambda r: (-r["count"], r["source"], r["target"]))
    return rows[:COOCCUR_PAIRS_MAX]


def _hub_score(s: dict, node: str) -> int:
    """hub 分 = 度数 × 文档频次（标签/代表词排序用）。"""
    deg = sum(
        1
        for e in (s.get("cooccurrence") or [])
        if node in (e["source"], e["target"])
    )
    return deg * int(_effective_node_count(s).get(node, 0))


def _effective_node_count(s: dict) -> dict[str, float]:
    """节点大小：优先 node_count（文档频次）；旧数据无该字段时
    以边权求和估算（方案 §漏洞 4：向后兼容不报错）。"""
    nc = dict(s.get("node_count") or {})
    if nc:
        return nc
    for e in s.get("cooccurrence") or []:
        w = float(e.get("weight", 0))
        nc[e["source"]] = nc.get(e["source"], 0) + w
        nc[e["target"]] = nc.get(e["target"], 0) + w
    return nc


def _cluster_name(s: dict, cluster: list[str]) -> str:
    """簇名 = 簇内 node_count 最高的词。"""
    nc = _effective_node_count(s)
    return max(cluster, key=lambda n: (nc.get(n, 0), n))


def topic_cluster_rows(s: dict) -> list[dict]:
    """话题簇榜单（方案 §4.3）：簇名/代表词/文档数/负面率，按文档数降序。"""
    tc = s.get("topic_clusters")
    if isinstance(tc, dict) and tc.get("clusters"):
        return [
            {
                "name": c["name"],
                "words": "、".join(c["words"]),
                "doc_count": c["doc_count"],
                "negative_rate": (
                    f"{c['negative_rate'] * 100:.0f}%"
                    if c.get("negative_rate") is not None else "—"
                ),
            }
            for c in tc["clusters"]
        ]
    # 旧数据兜底（词级求和口径，仅历史报告重出用）
    plan = cooccurrence_plan(s)
    if plan["kind"] != "network":
        return []
    clusters = _louvain_clusters(s.get("cooccurrence") or [])
    if not clusters:
        return []
    nc = _effective_node_count(s)
    neg_c = s.get("node_negative_count") or {}
    pos_c = s.get("node_positive_count") or {}
    rows = []
    for c in clusters:
        top3 = sorted(c, key=lambda n: (-_hub_score(s, n), n))[:3]
        doc_count = sum(nc.get(n, 0) for n in c)
        neg = sum(neg_c.get(n, 0) for n in c)
        pos = sum(pos_c.get(n, 0) for n in c)
        rate = neg / (neg + pos) if neg + pos else None
        rows.append({
            "name": _cluster_name(s, c),
            "words": "、".join(top3),
            "doc_count": doc_count,
            "negative_rate": f"{rate * 100:.0f}%" if rate is not None else "—",
        })
    rows.sort(key=lambda r: -r["doc_count"])
    return rows


@_styled
def cooccurrence_fig(s: dict) -> go.Figure | None:
    """讨论话题共现网络（方案图 B-1）：颜色=话题簇，大小=讨论量，
    边框=情感倾向；只画显著边；hub 标签避免 Word 静态图糊脸。"""
    plan = cooccurrence_plan(s)
    if plan["kind"] != "network":
        return None
    edges = s.get("cooccurrence") or []
    tc = s.get("topic_clusters")
    if isinstance(tc, dict) and tc.get("node_cluster"):
        node_cluster: dict[str, int] = tc["node_cluster"]
        clusters = [c["members"] for c in tc["clusters"]]
        cluster_names = {
            i: c["name"] for i, c in enumerate(tc["clusters"])
        }
    else:
        clusters = _louvain_clusters(edges)
        node_cluster = {}
        cluster_names = {}
    if not clusters:
        return None
    try:
        import networkx as nx
    except Exception:
        return None
    if not node_cluster:  # 旧数据兜底
        for i, c in enumerate(clusters):
            for n in c:
                node_cluster[n] = i
        cluster_names = {i: _cluster_name(s, c) for i, c in enumerate(clusters)}
    g = nx.Graph()
    for e in edges:
        g.add_edge(
            e["source"], e["target"],
            weight=float(e.get("weight", 0)),
        )
    pos = nx.spring_layout(g, seed=42, k=0.6)
    node_count = _effective_node_count(s)
    node_neg = s.get("node_negative_rate") or {}
    word_dims = s.get("word_dims") or {}
    max_nc = max(node_count.values()) or 1
    wmin = min((d["weight"] for _, _, d in g.edges(data=True)), default=0.0)
    wmax = max((d["weight"] for _, _, d in g.edges(data=True)), default=1.0)
    wspan = (wmax - wmin) or 1.0

    def _cluster_color(i: int) -> str:
        return CLUSTER_PALETTE[i] if i < len(CLUSTER_PALETTE) else "#DEE2E6"

    def _border_color(n: str) -> str:
        share = node_neg.get(n)
        if share is None:
            return SENTIMENT_COLORS["neutral"]  # 样本不足
        if share >= 0.6:
            return SENTIMENT_COLORS["negative"]  # 偏负面
        if share <= 0.4:
            return SENTIMENT_COLORS["positive"]  # 偏正面
        return SENTIMENT_COLORS["neutral"]  # 中性

    fig = go.Figure()
    for a, b, d in g.edges(data=True):
        same = node_cluster[a] == node_cluster[b]
        wnorm = (d["weight"] - wmin) / wspan
        fig.add_trace(
            go.Scatter(
                x=[pos[a][0], pos[b][0], None],
                y=[pos[a][1], pos[b][1], None],
                mode="lines",
                line=dict(
                    width=1 + wnorm * 4,
                    color=_cluster_color(node_cluster[a]) if same else "rgba(140, 140, 140, 0.6)",
                    dash="solid" if same else "dot",
                ),
                opacity=0.35 if same else 0.6,
                hoverinfo="skip",
                showlegend=False,
            )
        )
    hover_texts = [
        (
            f"<b>{n}</b><br>簇：{cluster_names[node_cluster[n]]}"
            f"<br>文档数：{node_count.get(n, 0)}"
            f"<br>负面率：{node_neg[n] * 100:.0f}%"
            if n in node_neg else
            f"<b>{n}</b><br>簇：{cluster_names[node_cluster[n]]}"
            f"<br>文档数：{node_count.get(n, 0)}<br>负面率：—"
        )
        + f"<br>主要维度：{word_dims.get(n, '未标注')}"
        for n in g.nodes
    ]
    fig.add_trace(
        go.Scatter(
            x=[pos[n][0] for n in g.nodes],
            y=[pos[n][1] for n in g.nodes],
            mode="markers",
            marker=dict(
                size=[20 + 46 * (node_count.get(n, 0) / max_nc) ** 0.5 for n in g.nodes],
                color=[_cluster_color(node_cluster[n]) for n in g.nodes],
                line=dict(width=2, color=[_border_color(n) for n in g.nodes]),
                opacity=0.9,
            ),
            hovertext=hover_texts,
            hoverinfo="text",
            showlegend=False,
        )
    )
    degree = dict(g.degree())
    label_nodes = {}
    for i, c in enumerate(clusters):
        top = sorted(
            c,
            key=lambda n: (-degree[n] * node_count.get(n, 0), n),
        )[: min(3, len(c))]
        for n in top:
            label_nodes[n] = n
    if label_nodes:
        fig.add_trace(
            go.Scatter(
                x=[pos[n][0] for n in label_nodes],
                y=[pos[n][1] for n in label_nodes],
                mode="text",
                text=[label_nodes[n] for n in label_nodes],
                textposition="top center",
                textfont=dict(size=11),
                hoverinfo="skip",
                showlegend=False,
            )
        )
    for i, c in enumerate(clusters):
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                marker=dict(size=12, color=_cluster_color(i)),
                name=f"簇 {i + 1}：{cluster_names[i]}",
                showlegend=True,
            )
        )
    fig.update_layout(
        title=(
            "讨论话题共现网络（颜色=话题簇；节点大小=讨论量；"
            "边框=情感：✗红负面 / ✓绿正面 / ～灰中性）"
        ),
        height=560,
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        margin=dict(l=10, r=10, t=60, b=10),
        legend=dict(orientation="h", y=1.08, x=0, xanchor="left"),
    )
    return fig


@_styled
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
            marker_color=SENTIMENT_COLORS["negative"],
            text=[f"{r * 100:.0f}%" for r in rates],
            textposition="outside",
        )
    )
    fig.update_layout(
        title=f"负面情绪来源话题榜 {SENTIMENT_SYMBOL['negative']}（负面占比，讨论量≥5）",
        height=360,
        xaxis_title="负面占比",
        xaxis_tickformat=".0%",
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


def _narrative_stats(s: dict) -> dict:
    return s.get("narrative_stats") or {}


@_styled
def narrative_actor_fig(s: dict) -> go.Figure | None:
    """归因主体分布（方案图 A-1）：横向堆叠条形，颜色=情感；
    count<3 灰化，柱端标注 n 与负面率（仅 count≥3）。"""
    ns = _narrative_stats(s)
    if not ns or ns.get("total", 0) < 10 or not ns.get("by_actor"):
        return None
    actors = ns["by_actor"]
    labels = [ATTRIBUTION_CN.get(r["actor"], r["actor"]) for r in actors]
    fig = go.Figure()
    for sent in ("positive", "neutral", "negative"):
        fig.add_trace(
            go.Bar(
                name=SENTIMENT_LABEL[sent],
                orientation="h",
                y=labels,
                x=[r[sent] for r in actors],
                marker=dict(
                    color=[
                        SENTIMENT_COLORS[sent] if r["count"] >= 3 else "rgba(148,163,184,0.45)"
                        for r in actors
                    ]
                ),
            )
        )
    for i, r in enumerate(actors):
        if r["count"] >= 3:
            fig.add_annotation(
                x=r["count"],
                y=labels[i],
                text=(
                    f"n={r['count']} · 负面率 "
                    f"{_rate_symbol(r['negative_rate'])} {r['negative_rate'] * 100:.0f}%"
                ),
                showarrow=False,
                xanchor="left",
                font=dict(size=11),
            )
    fig.update_layout(
        title="归因主体分布（用户主要把问题归给谁）",
        barmode="stack",
        height=60 + 34 * len(actors),
        xaxis_title="文本数",
        margin=dict(l=20, r=20, t=50, b=20),
        legend_title="情感",
    )
    return fig


@_styled
def narrative_frame_actor_heatmap(s: dict) -> go.Figure | None:
    """叙事框架 × 归因主体负面率热力图（方案图 A-2）。
    格 = 条数（count≥3 显示 n · 负面率），颜色 = 负面率；含总计列/行。"""
    ns = _narrative_stats(s)
    if not ns or ns.get("total", 0) < 10 or not ns.get("frame_actor"):
        return None
    cols = [r["actor"] for r in ns["by_actor"]]
    if not cols:
        return None
    rows = list(NARRATIVE_FRAMES)
    z: list[list[float | None]] = []
    text: list[list[str]] = []
    for f in rows:
        zrow: list[float | None] = []
        trow: list[str] = []
        cells = ns.get("frame_actor", {}).get(f, {})
        for a in cols:
            cell = cells.get(a)
            if cell and cell["count"] >= 3:
                zrow.append(cell["negative_rate"])
                trow.append(
                    f"{cell['count']} · {_rate_symbol(cell['negative_rate'])} "
                    f"{cell['negative_rate'] * 100:.0f}%"
                )
            else:
                zrow.append(None)
                trow.append("—")
        fb = next((r for r in ns["by_frame"] if r["frame"] == f), None)
        if fb:
            zrow.append(fb["negative_rate"])
            trow.append(
                f"{fb['count']} · {_rate_symbol(fb['negative_rate'])} "
                f"{fb['negative_rate'] * 100:.0f}%"
            )
        else:
            zrow.append(None)
            trow.append("—")
        z.append(zrow)
        text.append(trow)
    total_row: list[float | None] = []
    total_text: list[str] = []
    for a in cols:
        ab = next((r for r in ns["by_actor"] if r["actor"] == a), None)
        if ab:
            total_row.append(ab["negative_rate"])
            total_text.append(f"{ab['count']} · {ab['negative_rate'] * 100:.0f}%")
        else:
            total_row.append(None)
            total_text.append("—")
    gave_attr = ns.get("gave_attr") or 0
    if gave_attr:
        neg_all = sum(r["negative"] for r in ns["by_actor"])
        total_row.append(round(neg_all / gave_attr, 3))
        total_text.append(f"{gave_attr} · {neg_all / gave_attr * 100:.0f}%")
    else:
        total_row.append(None)
        total_text.append("—")
    z.append(total_row)
    text.append(total_text)
    x_labels = [ATTRIBUTION_CN.get(a, a) for a in cols] + ["总计"]
    y_labels = [NARRATIVE_CN.get(f, f) for f in rows] + ["合计"]
    fig = go.Figure(
        go.Heatmap(
            z=z,
            x=x_labels,
            y=y_labels,
            zmin=0,
            zmax=1,
            colorscale=_negative_heat_colorscale(),
            colorbar=dict(title="负面率", tickformat=".0%"),
            text=text,
            texttemplate="%{text}",
            hovertemplate="%{y} × %{x}：%{text}<extra>负面率 %{z:.0%}</extra>",
        )
    )
    fig.update_layout(
        title="叙事框架 × 归因主体负面率（颜色=负面率；数字=n · 负面率）",
        height=60 + 34 * len(y_labels),
        margin=dict(l=20, r=20, t=60, b=20),
        yaxis=dict(autorange="reversed"),
    )
    return fig


def narrative_insight_text(s: dict) -> list[str]:
    """叙事/归因规则解读（零 LLM 成本，方案 §3.4）。
    - "问题归因"措辞仅在 negative_rate≥50% 时使用；
    - 风险热点：负面率≥60% 且 count≥10；
    - unclear 占比 >30% 追加人工复核提示。"""
    ns = _narrative_stats(s)
    if not ns or ns.get("total", 0) < 10:
        return []
    lines: list[str] = []
    candidates = [
        r for r in ns["by_actor"]
        if r["actor"] != "unclear"
        and r["negative"] >= 3
        and r["count"] >= 10
        and r["negative_rate"] >= 0.5
    ]
    if candidates:
        top = sorted(candidates, key=lambda r: (-r["negative"], r["actor"]))[:2]
        parts = []
        for i, r in enumerate(top):
            name = ATTRIBUTION_CN.get(r["actor"], r["actor"])
            body = f"（负面率 {r['negative_rate'] * 100:.0f}%，n={r['count']}）"
            parts.append(
                f"用户主要将问题归因于【{name}】{body}" if i == 0
                else f"其次【{name}】{body}"
            )
        lines.append("；".join(parts) + "。")
    else:
        facts = [r for r in ns["by_actor"] if r["actor"] != "unclear"]
        if facts:
            top = max(facts, key=lambda r: (r["count"], r["negative"]))
            name = ATTRIBUTION_CN.get(top["actor"], top["actor"])
            lines.append(
                f"讨论量最高的归因主体【{name}】"
                f"（负面率 {top['negative_rate'] * 100:.0f}%，n={top['count']}）"
            )
    hotspots: list[tuple[str, str, dict]] = []
    for f, cells in ns.get("frame_actor", {}).items():
        for a, cell in cells.items():
            if cell["count"] >= 10 and cell["negative_rate"] >= 0.6:
                hotspots.append((f, a, cell))
    if hotspots:
        hotspots.sort(key=lambda t: (-t[2]["count"], t[0], t[1]))
        parts = [
            f"【{NARRATIVE_CN.get(f, f)} × {ATTRIBUTION_CN.get(a, a)}】类文本"
            f"负面率 {c['negative_rate'] * 100:.0f}%（n={c['count']}）"
            for f, a, c in hotspots[:2]
        ]
        lines.append("；".join(parts) + " 为风险热点。")
    gave_attr = ns.get("gave_attr") or 0
    if gave_attr and ns.get("unclear", 0) / gave_attr > 0.3:
        lines.append("归因不明确的样本占比较高，建议人工复核样本。")
    return lines


@_styled
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
            colorscale=_negative_heat_colorscale(),
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
                color = "white" if rate > 0.55 else "#1A1D23"
            else:
                txt = f"n={v['count']}"
            color = "#8C8C8C"
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


def _chart_narrative_actor(s: dict) -> str:
    fig = narrative_actor_fig(s)
    return to_html(fig, full_html=False, include_plotlyjs=False) if fig else ""


def _chart_narrative_heatmap(s: dict) -> str:
    fig = narrative_frame_actor_heatmap(s)
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
    """实际查询串粒度漏斗（按渠道展示，2026-08-18 起覆盖全部渠道）。

    采集/保留/丢弃按 (渠道, 关键词, 查询串) 聚合；编码与负面数按
    (渠道, 关键词) 归因（与评测中心相同口径）；旧数据丢弃无 query 时
    按该关键词在渠道内的唯一查询串兜底。
    """
    report = bundle.model_dump(mode="json")
    rows = extract_funnel(report).get("funnel", [])
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
    co_plan = cooccurrence_plan(s)
    cluster_rows = topic_cluster_rows(s)
    pairs_rows = topic_pairs(s)
    evidence_by_id = {c["id"]: c for c in bundle.evidence}
    dim_evidence: dict[str, list[dict]] = {}
    for c in bundle.evidence:
        if c.get("dimension") and c.get("sentiment") == "negative":
            dim_evidence.setdefault(c["dimension"], []).append(c)
    # P0-3：整体倾向 key（呈现层派生，供情感标签三件套 ✓/✗/～）
    overall_key = (
        "pos" if s["overall_sentiment"] == "正面"
        else ("neg" if s["overall_sentiment"] == "负面" else "neu")
    )
    # P2：报告编号 + 封面摘要（呈现层派生）
    report_no = f"RPT-{bundle.created_at:%Y%m%d-%H%M}"
    subject_desc = (
        f"覆盖 {len(bundle.channel_results)} 渠道{trust['date_range']}公开讨论，"
        f"共采集 {trust['collected']} 条内容、编码 {s['total_items']} 条文本，"
        f"其中 {trust['llm_ratio']}% 经大模型精分析。"
    )
    return template.render(
          subject=bundle.plan.subject,
          created_at=bundle.created_at.strftime("%Y-%m-%d %H:%M"),
          report_no=report_no,
          subject_desc=subject_desc,
        keyword_count=len(bundle.plan.keywords),
        channel_count=len(bundle.channel_results),
        total_posts=s["total_posts"],
        total_items=s["total_items"],
        ads=s.get("ads") or {},
        warnings=bundle.warnings,
          overall_sentiment=s["overall_sentiment"],
          overall_key=overall_key,
          avg_score=s["avg_score"],
          pos_ratio=round(dist["positive"]["ratio"] * 100, 1),
          neg_ratio=round(dist["negative"]["ratio"] * 100, 1),
          neu_ratio=round(dist["neutral"]["ratio"] * 100, 1),
          pos_count=dist["positive"]["count"],
          neg_count=dist["negative"]["count"],
          neu_count=dist["neutral"]["count"],
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
        cooccurrence_kind=co_plan["kind"],
        cooccurrence_reason=co_plan["reason"],
        cluster_rows=cluster_rows,
        topic_pairs=pairs_rows,
        chart_sources=_chart_sources(s),
        chart_date_dim=_chart_date_dim(s),
        top_words=s["top_words"],
        report_text=bundle.report_text,
        chart_insights=bundle.chart_insights,
        conclusion=bundle.conclusion,
        insight_mode=bundle.insight_mode,
        findings=bundle.findings,
        evidence_by_id=evidence_by_id,
        dim_evidence=dim_evidence,
        conclusion_title=findings_section_title(bundle.insight_mode),
        display_finding_id=display_finding_id,
        display_action=display_action,
        dimension_evidence_label=dimension_evidence_label,
        need_review_n=sum(1 for it in bundle.coded_items if it.need_review),
        narrative_total=(s.get("narrative_stats") or {}).get("total", 0),
        narrative_insight=narrative_insight_text(s),
        chart_narrative_actor=_chart_narrative_actor(s),
        chart_narrative_heatmap=_chart_narrative_heatmap(s),
        trust=trust,
        keyword_rows=keyword_rows_out,
        unattributed_dropped=unattributed_dropped,
        query_rows=query_rows_out,
        unattributed_query_dropped=unattributed_query_dropped,
        method_counts={
            "llm": sum(1 for it in bundle.coded_items if it.method == "llm"),
            "lexicon": sum(1 for it in bundle.coded_items if it.method == "lexicon"),
        },
        collection_notes=_collection_notes(bundle),
    )


def _collection_notes(bundle: ReportBundle) -> list[dict]:
    """采集说明（2026-08-18 采集透明度）：实际保留 < 配置上限的渠道缺口。
    只统计有 collection_stats 的渠道；采满或缺失 stats 不展示。"""
    notes: list[dict] = []
    for ch in bundle.channel_results:
        st = ch.collection_stats or {}
        if not st:
            continue
        requested = st.get("requested_limit")
        kept = st.get("kept") or 0
        if requested is None or kept >= requested:
            continue
        reasons: list[str] = []
        tips: list[str] = []
        oor = st.get("skipped_out_of_range") or 0
        ad = st.get("skipped_ad") or 0
        dup = st.get("skipped_dup") or 0
        official = st.get("skipped_official") or 0
        domain_filter = st.get("skipped_domain_filter") or 0
        empty_q = st.get("empty_queries") or 0
        risk_stop = st.get("risk_stop") or 0
        mblog = st.get("mblog_cards")
        if oor:
            reasons.append(f"{oor} 条超出所选时间范围被过滤")
            tips.append("放宽时间窗可多采")
        if ad:
            reasons.append(f"{ad} 条为广告被过滤")
        if dup:
            reasons.append(f"{dup} 条为重复返回")
        if official:
            reasons.append(f"{official} 条为官网域名黑名单被过滤")
            tips.append("检查关键词策略中的官网域名黑名单")
        if domain_filter:
            reasons.append(f"{domain_filter} 条非目标子渠道域名被过滤")
            tips.append("子渠道仅保留目标站点域名，可换全网渠道采集更多")
        if empty_q:
            reasons.append(f"{empty_q} 个查询词无有效结果")
            tips.append("增加相关关键词扩展信息面")
        if risk_stop:
            reasons.append("检测到风控信号提前结束")
            tips.append("冷却后减少关键词或分次运行")
        if mblog is not None and mblog < requested:
            reasons.append(f"该关键词搜索结果有限（API 正文 {mblog} 条 < 设置 {requested} 条）")
            tips.append("增加相关关键词扩展信息面")
        if not reasons:
            reasons.append("平台搜索结果不足")
        if not tips:
            tips.append("增加相关关键词或更换渠道")
        notes.append({
            "channel": ch.channel_id, "requested": requested, "kept": kept,
            "reasons": reasons, "tips": tips,
        })
    return notes
