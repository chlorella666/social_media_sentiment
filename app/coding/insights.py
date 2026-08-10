"""报告深度洞察：每张图表的中文解析 + 按叙事框架的综合结论。

- 统计描述：把 summary 数字转成可供 LLM 理解的中文描述
- 模板兜底：无 LLM 时用规则生成解析与结论
- LLM 优先：OpenAI 兼容接口生成更有深度的解析与建议
"""

from __future__ import annotations

from app.coding.llm_analyzer import OpenAICompatibleAnalyzer
from app.coding import lexicon_v2
from app.core.models import AnalysisPlan
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
            f"平均情感分 {summary['avg_score']}（-1~1），整体倾向“{summary['overall_sentiment']}”。"
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
            "各维度讨论量与负面率："
            + "；".join(
                f"{dimension_cn(did)} 讨论 {v['count']} 条、"
                f"负面率 {v['negative_rate'] * 100:.1f}%"
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
        f"强情绪（4~5 级）共 {strong} 条，占比 {strong / total * 100:.1f}%"
    )


def _radar_descriptor(dims: dict) -> str:
    if not dims:
        return "无维度数据"
    worst = max(dims.items(), key=lambda kv: kv[1]["negative_rate"])
    best = min(dims.items(), key=lambda kv: kv[1]["negative_rate"])
    return (
        f"维度负面率雷达显示：负面率最高为「{dimension_cn(worst[0])}」"
        f"（{worst[1]['negative_rate'] * 100:.1f}%），"
        f"最低为「{dimension_cn(best[0])}」（{best[1]['negative_rate'] * 100:.1f}%），"
        f"共 {len(dims)} 个维度，分布{'失衡' if worst[1]['negative_rate'] > 0.5 else '相对均衡'}"
    )


def _platform_dim_descriptor(pd_data: dict) -> str:
    if not pd_data:
        return "无平台×维度数据"
    rows = []
    for platform, dims in pd_data.items():
        valid = [(d, v) for d, v in dims.items() if v["count"] > 0]
        if valid:
            d, v = max(valid, key=lambda kv: kv[1]["negative_rate"])
            rows.append(
                f"{platform_cn(platform)} 的「{dimension_cn(d)}」"
                f"负面率 {v['negative_rate'] * 100:.1f}%"
            )
    return "各平台负面率最高维度：" + "；".join(rows[:5]) if rows else "无数据"


def _date_dim_descriptor(dd_data: dict) -> str:
    if not dd_data:
        return "无日期×维度数据"
    cells = [
        (d, dim, v)
        for d, dims in dd_data.items()
        for dim, v in dims.items()
        if v["count"] > 0
    ]
    if not cells:
        return "无数据"
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
    worst = max(dims.items(), key=lambda kv: kv[1]["negative_rate"])
    return (
        f"负面率最高维度为「{dimension_cn(worst[0])}」"
        f"（{worst[1]['negative_rate'] * 100:.1f}%），"
        f"共讨论 {worst[1]['count']} 条"
    )


def template_insights(descriptors: dict) -> dict:
    """无 LLM 时的模板解析与结论（数字驱动，逻辑清晰）。"""
    d = lambda key: descriptors.get(key, "暂无数据")  # noqa: E731
    ci = {
        "overall": (
            f"整体分布显示{d('overall')}。"
            "若负面占比明显偏高，说明当前口碑存在集中性问题；若以中性为主，"
            "则话题讨论多处于观望阶段，需要更多有效内容驱动认知。"
        ),
        "platform": (
            f"平台对比显示：{d('platform')}。"
            "内容量大的平台是舆论主阵地，平均分低的平台应优先排查口碑问题。"
        ),
        "trend": (
            f"时间趋势显示：{d('trend')}。"
            "若后段情感分下滑或负面量抬升，需结合当时的营销/事件节点定位诱因。"
        ),
        "dimensions": (
            f"维度分布显示：{d('dimensions')}。"
            "讨论量高且负面率高的维度是核心风险点，建议优先整改。"
        ),
        "heatmap": (
            f"负面率热力显示：{d('heatmap')}。"
            "该维度应作为下一阶段舆情跟踪与产品/服务改进的重点。"
        ),
        "words": (
            f"高频词显示：{d('words')}。"
            "正面词主导说明口碑基础良好，负面词集中说明存在具体槽点。"
        ),
        "intensity": (
            f"强度分布显示：{d('intensity')}。"
            "强情绪占比高说明讨论带有明确立场，事件烈度高，建议加快响应节奏；"
            "以温和情绪为主则更适合长效口碑建设。"
        ),
        "radar": (
            f"雷达图显示：{d('radar')}。"
            "负面率突出的维度是短板，平均分突出的维度可作传播优势点。"
        ),
        "platform_dim": (
            f"平台×维度显示：{d('platform_dim')}。"
            "定位到具体平台的具体维度槽点后，可交由对应渠道运营团队定向处理。"
        ),
        "date_dim": (
            f"日期×维度显示：{d('date_dim')}。"
            "若负面集中在特定日期，说明与当时的营销/事件节点强相关，建议复盘该节点。"
        ),
        "wordcloud": (
            f"内容高频词显示：{d('wordcloud')}。"
            "主题词集中在产品/体验相关词时，说明讨论围绕实际使用；集中在营销词时说明认知主要来自传播。"
        ),
        "cooccurrence": (
            f"共现网络显示：{d('cooccurrence')}。"
            "共现密集的词对构成核心讨论主题，可据此提炼用户最关心的话题组合。"
        ),
    }
    conclusion = (
        "综合各图表信息：整体舆论以中性/正面为主，但存在负面集中维度，"
        "按叙事框架给出如下结论与建议：\n"
        "1. 责任归因：负面率最高的维度最可能指向企业责任，建议尽快定位具体场景并给出官方回应；\n"
        "2. 冲突框架：若负面评论涉及消费者与品牌对立，应避免争论，用事实与补偿方案化解；\n"
        "3. 人情味框架：正面体验分享是稀缺的 UGC 素材，可筛选真实好评作为传播内容；\n"
        "4. 经济后果：负面讨论集中在价格/价值维度时，需评估定价与促销策略对口碑的影响；\n"
        "5. 道德框架：若出现安全/诚信类关键词（食品安全、虚假宣传等），属于最高优先级危机信号，需立即响应。\n"
        "建议扩大采集范围复测，验证以上结论的稳定性。"
    )
    return {"chart_insights": ci, "conclusion": conclusion}


def build_insights(analyzer, plan: AnalysisPlan, summary: dict) -> dict:
    """生成图表解析 + 深度结论：LLM 优先，模板兜底。"""
    descriptors = build_descriptors(summary)
    if isinstance(analyzer, OpenAICompatibleAnalyzer):
        return analyzer.generate_insights(descriptors)
    return template_insights(descriptors)
