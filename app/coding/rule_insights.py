"""F-010（2026-08-26）：词典模式规则解读层——确定性、零 LLM 成本。

措辞纪律：原因推测必须带「可能/或与…相关」；负面率 >=50% 才用「问题归因」；
禁止强动作词（必须/立即/停止/务必/一定）；小样本不写推测。
"""

from __future__ import annotations

MIN_DIM_RATING = 3
FORBIDDEN_STRONG = ("必须", "立即", "停止", "务必", "一定")


def _fmt_rate(rate: float) -> str:
    return f"{rate * 100:.0f}%"


def dimension_interpretation(
    name_cn: str, negative_rate: float, n: int, has_refs: bool = True
) -> dict:
    """按「维度 × 负面率分档」给出可能原因与方向建议（保守措辞）。"""
    if n < MIN_DIM_RATING:
        return {"cause": "", "direction": ""}
    if negative_rate >= 0.6:
        band_txt = "耐用性、质量或使用体验相关的讨论（或与品控、设计、服务环节相关）"
    elif negative_rate >= 0.4:
        band_txt = "价格、服务或体验细节的讨论（或与预期落差相关）"
    else:
        band_txt = "个别场景或细节体验的讨论（样本有限，仅供参考）"
    if negative_rate >= 0.5:
        head = f"「{name_cn}」负面率 {_fmt_rate(negative_rate)}（n={n}），问题归因可能集中在"
    else:
        head = f"「{name_cn}」负面率 {_fmt_rate(negative_rate)}（n={n}），值得关注的方向可能是"
    direction = (
        "建议优先查看该维度负面原文 Top3 验证方向，再结合规模与频次判断优先级；"
        "必要时扩大关键词/渠道补足样本。"
    )
    if not has_refs:
        direction = "建议结合报告「维度明细」进一步查看。"
    return {"cause": head + band_txt + "。", "direction": direction}


def build_structured_summary(
    summary: dict, evidence: list[dict] | None = None, top_phrases: dict | None = None
) -> dict:
    """结构化总结：整体/正面/负面/重点问题归因/改进建议（全部由 summary 推导）。

    极端数据降级：total<10、全中性、无负面证据时仅输出统计层，不含推测。
    """
    evidence = evidence or []
    top_phrases = top_phrases or {}
    total = int(summary.get("total_items") or 0)
    dist = summary.get("sentiment_distribution") or {}
    pos_n = int(dist.get("positive", {}).get("count") or 0)
    neg_n = int(dist.get("negative", {}).get("count") or 0)
    neu_n = int(dist.get("neutral", {}).get("count") or 0)

    def _ratio(n: int) -> float:
        return round(n / total, 4) if total else 0.0

    overall = (
        f"整体讨论以{'正面' if pos_n > neg_n else ('负面' if neg_n > pos_n else '中性')}为主"
        f"（正面 {_ratio(pos_n):.1%} / 中性 {_ratio(neu_n):.1%} / "
        f"负面 {_ratio(neg_n):.1%}，共 {total} 条）。"
    )
    degrade = total < 10 or (pos_n + neg_n) == 0

    def _phrase_block(key: str, label: str, subset_n: int) -> list[dict]:
        # P1（2026-08-26）：占比分母=所属情感子集（如"占负面讨论 17%"），
        # 而非全部文本（否则热门短语仅 1~3% 造成感知差）；count 为主、% 为辅。
        items = (top_phrases.get(key) or [])[:3]
        return [
            {
                "phrase": p.get("phrase", ""),
                "count": p.get("count", 0),
                "ratio": round(p.get("count", 0) / subset_n, 4) if subset_n else 0.0,
            }
            for p in items
        ]

    positive = {"ratio": _ratio(pos_n), "phrases": _phrase_block("positive", "正面", pos_n)}
    negative = {"ratio": _ratio(neg_n), "phrases": _phrase_block("negative", "负面", neg_n)}

    top_issues: list[dict] = []
    if not degrade:
        dims = summary.get("dimensions") or {}
        rows = []
        for dim, v in dims.items():
            n = int(v.get("count") or 0)
            rate = float(v.get("negative_rate") or 0.0)
            if n < MIN_DIM_RATING:
                continue
            rows.append({
                "dimension": dim,
                "name": dim,
                "count": n,
                "rate": rate,
                "has_refs": any(
                    c.get("dimension") == dim and c.get("sentiment") == "negative"
                    for c in evidence
                ),
            })
        rows.sort(key=lambda r: (-r["rate"], r["dimension"]))
        for r in rows[:3]:
            interp = dimension_interpretation(
                r["name"], r["rate"], r["count"], r["has_refs"]
            )
            top_issues.append({
                "dimension": r["dimension"],
                "name": r["name"],
                "count": r["count"],
                "rate": round(r["rate"], 4),
                "cause": interp["cause"],
                "direction": interp["direction"],
            })

    improvements = [
        "优先排查负面率最高的维度（见上），结合负面原文 Top3 验证方向；",
        "扩大关键词或时间窗口，确认问题规模与代表性；",
        "开启 LLM 精分析，可获得可归因的深度结论与行动建议。",
    ]
    if not top_issues:
        improvements = improvements[1:]

    return {
        "overall": overall,
        "positive": positive,
        "negative": negative,
        "top_issues": top_issues,
        "improvements": improvements,
        "degraded": degrade,
    }