"""报告深度洞察：每张图表的中文解析 + 按叙事框架的综合结论。

- 统计描述：把 summary 数字转成可供 LLM 理解的中文描述
- 模板兜底：无 LLM 时用规则生成解析与结论
- LLM 优先：OpenAI 兼容接口生成更有深度的解析与建议
"""

from __future__ import annotations

from app.coding.llm_analyzer import OpenAICompatibleAnalyzer
from app.core.models import AnalysisPlan
from app.core.evidence import (
    MIN_DIM_NORMAL,
    MIN_DIM_RATING,
    build_findings,
    filter_llm_findings,
    findings_to_conclusion,
)
from app.core.names import dimension_cn, platform_cn

CHART_IDS = [
    "overall", "platform", "trend", "dimensions", "heatmap", "words",
    "intensity", "radar", "platform_dim", "date_dim", "wordcloud", "cooccurrence",
]


def build_descriptors(summary: dict) -> dict:
    """把统计汇总转成每张图表的中文数字描述。"""
    dist = summary["sentiment_distribution"]
    platforms = summary["platforms"]
    trend = summary["trend"]
    dims = summary["dimensions"]
    intensity = summary.get("intensity_distribution", {})
    pd_data = summary.get("platform_dim", {})
    dd_data = summary.get("date_dim", {})
    cooccurrence = summary.get("cooccurrence", [])

    def pct(key: str) -> str:
        return f"{dist[key]['ratio'] * 100:.1f}%"

    descriptors = {
        "overall": (
            f"共编码 {summary['total_items']} 条文本（帖子 {summary['total_posts']} 条）："
            f"正面 {dist['positive']['count']} 条（{pct('positive')}）、"
            f"负面 {dist['negative']['count']} 条（{pct('negative')}）、"
            f"中性 {dist['neutral']['count']} 条（{pct('neutral')}）；"
            f"平均情感分 {summary['avg_score']}（-1～1），整体倾向“{summary['overall_sentiment']}”。"
        ),
        "platform": (
            "各平台内容量："
            + "；".join(
                f"{platform_cn(pid)} {v['posts']} 条、平均分 {v['avg_score']}"
                for pid, v in platforms.items()
            )
            or "无平台数据"
        ),
        "trend": (
            _trend_descriptor(trend) if trend else "时间范围内无趋势数据"
        ),
        "dimensions": (
            "各维度评价量与负面率："
            + "；".join(
                _dimension_rate_text(did, v)
                for did, v in sorted(dims.items(), key=lambda kv: -kv[1]["count"])
            )
            if dims
            else "未启用维度分析"
        ),
        "heatmap": (
            _heatmap_descriptor(dims) if dims else "未启用维度分析"
        ),
        "words": _words_descriptor(summary),
        "intensity": _intensity_descriptor(intensity),
        "radar": _radar_descriptor(dims),
        "platform_dim": _platform_dim_descriptor(pd_data),
        "date_dim": _date_dim_descriptor(dd_data),
        "wordcloud": _wordcloud_descriptor(summary),
        "cooccurrence": _cooccurrence_descriptor(cooccurrence, summary),
    }
    return descriptors


def _words_descriptor(summary: dict) -> str:
    pos = summary.get("positive_words") or []
    neg = summary.get("negative_words") or []
    parts = []
    if neg:
        parts.append("负面：" + "、".join(f"{w}（{c}）" for w, c in neg[:8]))
    if pos:
        parts.append("正面：" + "、".join(f"{w}（{c}）" for w, c in pos[:8]))
    return "高频情感词（|情感分| 加权）：" + "；".join(parts) if parts else "无明显高频情感词"


def _wordcloud_descriptor(summary: dict) -> str:
    pos = summary.get("positive_wordcloud") or []
    neg = summary.get("negative_wordcloud") or []
    worst = summary.get("worst_dim_wordcloud") or []
    parts = []
    if neg:
        parts.append("负面词云聚焦：" + "、".join(w for w, _ in neg[:8]))
    if pos:
        parts.append("正面词云聚焦：" + "、".join(w for w, _ in pos[:8]))
    if worst:
        parts.append(
            f"「{dimension_cn(summary.get('worst_dim_id', ''))}」维度负面聚焦："
            + "、".join(w for w, _ in worst[:8])
        )
    return "词云（权重=词频×情感强度）：" + "；".join(parts) if parts else "暂无词云数据"


def _cooccurrence_descriptor(edges: list[dict], summary: dict) -> str:
    sources = [
        r
        for r in (summary.get("sentiment_sources") or [])
        if r["negative_rate"] > 0.5
    ]
    if not sources:
        return "无显著负面来源话题（负面占比 >50% 且讨论量≥5 的话题不足）"
    parts = []
    for r in sources[:6]:
        total = r["positive"] + r["negative"]
        parts.append(
            f"{r['word']}：负面 {r['negative_rate'] * 100:.0f}%（{total} 条）"
        )
    return "负面情绪来源话题 Top：" + "；".join(parts)


def _intensity_descriptor(intensity: dict) -> str:
    counts = {int(k): int(v) for k, v in intensity.items()}
    total = sum(counts.values()) or 1
    strong = counts.get(4, 0) + counts.get(5, 0)
    return (
        f"情绪强度分布：1 级 {counts.get(1, 0)} 条、2 级 {counts.get(2, 0)} 条、"
        f"3 级 {counts.get(3, 0)} 条、4 级 {counts.get(4, 0)} 条、5 级 {counts.get(5, 0)} 条；"
        f"强情绪（4～5 级）共 {strong} 条，占比 {strong / total * 100:.1f}%"
    )


def _dimension_rate_text(did: str, v: dict) -> str:
    """n 守卫（两档）：n<3 不评级；3≤n<10 标样本有限。"""
    count = v["count"]
    if count < MIN_DIM_RATING:
        return f"{dimension_cn(did)} 讨论 {count} 条（样本不足，不评级）"
    rate = v["negative_rate"] * 100
    marker = "" if count >= MIN_DIM_NORMAL else "，样本有限"
    return f"{dimension_cn(did)} 讨论 {count} 条、负面率 {rate:.1f}%{marker}"


def _valid_dims(dims: dict) -> list[tuple[str, dict]]:
    """n>=MIN_DIM_RATING 的维度（用于"最差/最高"评选）。"""
    return [(d, v) for d, v in dims.items() if v["count"] >= MIN_DIM_RATING]


def _radar_descriptor(dims: dict) -> str:
    if not dims:
        return "无维度数据"
    valid = _valid_dims(dims)
    if not valid:
        return "各维度样本均不足（n<3），负面率最高/最低未评级"
    worst = max(valid, key=lambda kv: kv[1]["negative_rate"])
    best = min(valid, key=lambda kv: kv[1]["negative_rate"])
    return (
        f"维度负面率雷达显示：负面率最高为「{dimension_cn(worst[0])}」"
        f"（{worst[1]['negative_rate'] * 100:.1f}%），"
        f"最低为「{dimension_cn(best[0])}」（{best[1]['negative_rate'] * 100:.1f}%），"
        f"共 {len(valid)} 个可评级维度（n≥3），"
        f"分布{'失衡' if worst[1]['negative_rate'] > 0.5 else '相对均衡'}"
    )


def _platform_dim_descriptor(pd_data: dict) -> str:
    if not pd_data:
        return "无平台×维度数据"
    rows = []
    for platform, dims in pd_data.items():
        valid = [(d, v) for d, v in dims.items() if v["count"] >= MIN_DIM_RATING]
        if valid:
            d, v = max(valid, key=lambda kv: kv[1]["negative_rate"])
            rows.append(
                f"{platform_cn(platform)} 的「{dimension_cn(d)}」"
                f"负面率 {v['negative_rate'] * 100:.1f}%"
            )
    return "各平台负面率最高维度：" + "；".join(rows[:5]) if rows else "各平台×维度样本均不足（n<3），未评级"


def _date_dim_descriptor(dd_data: dict) -> str:
    if not dd_data:
        return "无日期×维度数据"
    cells = [
        (d, dim, v)
        for d, dims in dd_data.items()
        for dim, v in dims.items()
        if v["count"] >= MIN_DIM_RATING
    ]
    if not cells:
        return "日期×维度样本均不足（n<3），未评级"
    cells.sort(key=lambda c: -c[2]["negative_rate"])
    top = cells[:3]
    return "负面率最高的日期×维度：" + "；".join(
        f"{d} 的「{dimension_cn(dim)}」{v['negative_rate'] * 100:.1f}%"
        for d, dim, v in top
    )


def _trend_descriptor(trend: dict) -> str:
    dates = sorted(trend.keys())
    if len(dates) < 2:
        first = dates[0]
        return f"趋势数据集中在 {first}（{trend[first]['count']} 条，均分 {trend[first]['avg_score']}）"
    half = len(dates) // 2
    first_half = list(dates[:half])
    second_half = list(dates[half:])

    def avg(keys: list[str]) -> float:
        scores = [trend[k]["avg_score"] for k in keys]
        return round(sum(scores) / len(scores), 3) if scores else 0.0

    a, b = avg(first_half), avg(second_half)
    direction = "上升" if b > a + 0.05 else ("下降" if b < a - 0.05 else "平稳")
    peak = max(dates, key=lambda d: trend[d]["count"])
    worst = max(dates, key=lambda d: trend[d]["negative"])
    return (
        f"情感走势整体{direction}：前半段均分 {a}，后半段均分 {b}；"
        f"讨论高峰在 {peak}（{trend[peak]['count']} 条），负面量峰值在 {worst}（{trend[worst]['negative']} 条）"
    )


def _heatmap_descriptor(dims: dict) -> str:
    valid = _valid_dims(dims)
    if not valid:
        return "维度样本均不足（n<3），负面率最高维度未评级"
    worst = max(valid, key=lambda kv: kv[1]["negative_rate"])
    return (
        f"负面率最高维度为「{dimension_cn(worst[0])}」"
        f"（{worst[1]['negative_rate'] * 100:.1f}%），"
        f"共讨论 {worst[1]['count']} 条"
    )


def template_chart_insights(descriptors: dict) -> dict:
    """无 LLM / 兜底时的图表解析（数字驱动；不出现强动作建议措辞）。"""
    d = lambda key: descriptors.get(key, "暂无数据")  # noqa: E731
    ci = {
        "overall": (
            f"整体分布显示{d('overall')}。"
            "若负面占比明显偏高，说明当前口碑存在集中性问题；若以中性为主，"
            "则话题讨论多处于观望阶段（开启 LLM 精分析可获得更具体归因）。"
        ),
        "platform": (
            f"平台对比显示：{d('platform')}。"
            "内容量大的平台是舆论主阵地，平均分低的平台可作为重点关注方向。"
        ),
        "trend": (
            f"时间趋势显示：{d('trend')}。"
            "若后段情感分下滑或负面量抬升，可结合当时的营销/事件节点排查诱因。"
        ),
        "dimensions": (
            f"维度分布显示：{d('dimensions')}。"
            "评价量高且负面率高的维度是核心风险点，建议作为后续关注重点。"
        ),
        "heatmap": (
            f"负面率热力显示：{d('heatmap')}。"
            "该维度可作为下一阶段舆情跟踪与产品/服务改进的重点方向。"
        ),
        "words": (
            f"高频词显示：{d('words')}。"
            "正面词主导说明口碑基础良好，负面词集中说明存在具体槽点。"
        ),
        "intensity": (
            f"强度分布显示：{d('intensity')}。"
            "强情绪占比高说明讨论带有明确立场，事件烈度高，可关注响应节奏；"
            "以温和情绪为主则更适合长效口碑建设。"
        ),
        "radar": (
            f"雷达图显示：{d('radar')}。"
            "负面率突出的维度是短板，平均分突出的维度可作传播优势点。"
        ),
        "platform_dim": (
            f"平台×维度显示：{d('platform_dim')}。"
            "可结合具体平台与维度的原文样本进一步确认问题场景"
            "（开启 LLM 精分析可获得可归因的结论与建议）。"
        ),
        "date_dim": (
            f"日期×维度显示：{d('date_dim')}。"
            "若负面集中在特定日期，说明与当时的营销/事件节点可能相关，可复盘该节点。"
        ),
        "wordcloud": (
            f"内容高频词显示：{d('wordcloud')}。"
            "主题词集中在产品/体验相关词时，说明讨论围绕实际使用；集中在营销词时说明认知主要来自传播。"
        ),
        "cooccurrence": (
            f"情绪来源与讨论结构显示：{d('cooccurrence')}。"
            "共现密集的词对构成核心讨论主题，可据此提炼用户最关心的话题组合。"
        ),
    }
    return ci


def template_insights(descriptors: dict) -> dict:
    """兼容旧入口：图表解析 + 兜底结论（无 summary/evidence 时使用）。"""
    ci = template_chart_insights(descriptors)
    conclusion = (
        "结论基于词典/规则模板生成，仅供参考；"
        "开启 LLM 精分析可获得可归因的结论与行动建议。"
    )
    return {"chart_insights": ci, "conclusion": conclusion}


def build_report_content(
    analyzer,
    plan: AnalysisPlan,
    summary: dict,
    evidence: list[dict] | None = None,
) -> dict:
    """统一报告内容入口：图表解析 + findings + conclusion + insight_mode。

    - LLM 模式：LLM 在给定证据上写发现（P1.5），结构校验不过则规则兜底；
    - 词典模式：规则 findings（不再尝试 LLM、不再落静态模板）。
    """
    evidence = evidence or []
    descriptors = build_descriptors(summary)
    if isinstance(analyzer, OpenAICompatibleAnalyzer):
        try:
            out = analyzer.generate_insights(descriptors, evidence, summary)
        except TypeError:  # 兼容旧签名（测试 fake / 历史子类）
            try:
                out = analyzer.generate_insights(descriptors, evidence)
            except TypeError:
                out = analyzer.generate_insights(descriptors)
        chart_insights = out.get("chart_insights") or {}
        findings, dropped = filter_llm_findings(out.get("findings") or [], evidence)
        if not findings:
            findings = build_findings(evidence, summary, mode="fallback")
            mode = "template_fallback"
            conclusion = findings_to_conclusion(findings)
        else:
            mode = "llm"
            if dropped:
                conclusion = findings_to_conclusion(findings)
            else:
                conclusion = out.get("conclusion") or findings_to_conclusion(findings)
        return {
            "chart_insights": chart_insights,
            "conclusion": conclusion,
            "findings": findings,
            "insight_mode": mode,
        }
    # 词典模式（MockAnalyzer / 无 Key）：规则 findings，零新增 LLM
    ci = template_chart_insights(descriptors)
    findings = build_findings(evidence, summary, mode="lexicon")
    return {
        "chart_insights": ci,
        "conclusion": findings_to_conclusion(findings),
        "findings": findings,
        "insight_mode": "lexicon",
    }


def build_insights(analyzer, plan: AnalysisPlan, summary: dict) -> dict:
    """兼容壳：旧调用方无 items/evidence 时退化为空证据。"""
    return build_report_content(analyzer, plan, summary, [])
