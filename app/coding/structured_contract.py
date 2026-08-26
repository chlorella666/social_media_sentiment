"""F-010/F-015 LLM 接入（2026-08-26）：结构化总结呈现契约。

混合架构：LLM 只写解读文案（overall / cause / direction / improvements），
所有 ratio/count/phrases/rate 由系统从 summary 计算填充或校验覆盖
（机制上消除数字幻觉）。解析/校验/回退为纯函数，供单测覆盖四态：
解析成功 / 解析失败 / 字段缺失 / 数字越界。
"""

from __future__ import annotations

import json


def parse_llm_structured_summary(content: str) -> dict | None:
    """从 LLM JSON 输出解析 structured_summary 文案块；失败返回 None。"""
    if not content or not isinstance(content, str):
        return None
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    ss = data.get("structured_summary")
    if not isinstance(ss, dict):
        return None
    return ss


def validate_llm_structured_summary(raw: dict) -> dict | None:
    """校验 LLM 文案块：overall 非空 str；top_issues/improvements 为列表。

    数字字段（ratio/count/rate/n）一律丢弃（由系统覆盖填充）；只保留文案。
    返回清洗后 dict 或 None（整体不可用）。
    """
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    overall = raw.get("overall")
    if not isinstance(overall, str) or not overall.strip():
        return None
    out["overall"] = overall.strip()
    issues = raw.get("top_issues")
    if issues is not None:
        if not isinstance(issues, list):
            return None
        clean_issues = []
        for it in issues:
            if not isinstance(it, dict):
                continue
            entry: dict = {}
            cause = it.get("cause")
            direction = it.get("direction")
            if isinstance(cause, str) and cause.strip():
                entry["cause"] = cause.strip()
            if isinstance(direction, str) and direction.strip():
                entry["direction"] = direction.strip()
            if entry:
                clean_issues.append(entry)
        if clean_issues:
            out["top_issues"] = clean_issues
    imps = raw.get("improvements")
    if imps is not None:
        if not isinstance(imps, list):
            return None
        clean_imps = [s for s in imps if isinstance(s, str) and s.strip()]
        if clean_imps:
            out["improvements"] = clean_imps
    return out


def merge_structured_summary(
    llm_text: dict | None,
    summary: dict,
    evidence: list[dict] | None = None,
) -> tuple[dict, str]:
    """混合架构：LLM 文案 + 系统数字 → (structured_summary, source)。

    - llm_text 有效：source="llm"；top_issues 的 name/count/rate 由系统从
      summary 维度统计填充（负面率降序、n>=3），LLM 只提供 cause/direction
      （按序配对，缺失补规则 `dimension_interpretation`）；
    - llm_text 无效/缺失：回退 `rule_insights.build_structured_summary`，
      source="rule"（保证非空不报错）。
    """
    from app.coding.rule_insights import build_structured_summary, dimension_interpretation

    rule_ss = build_structured_summary(summary, evidence, summary.get("top_phrases"))
    if not llm_text:
        return rule_ss, "rule"

    dist = summary.get("sentiment_distribution") or {}
    total = int(summary.get("total_items") or 0)
    pos_n = int(dist.get("positive", {}).get("count") or 0)
    neg_n = int(dist.get("negative", {}).get("count") or 0)

    def _ratio(n: int) -> float:
        return round(n / total, 4) if total else 0.0

    def _phrase_block(key: str, subset_n: int) -> list[dict]:
        items = ((summary.get("top_phrases") or {}).get(key) or [])[:3]
        return [
            {
                "phrase": p.get("phrase", ""),
                "count": p.get("count", 0),
                "ratio": round(p.get("count", 0) / subset_n, 4) if subset_n else 0.0,
            }
            for p in items
        ]

    dims = summary.get("dimensions") or {}
    rows = []
    for dim, v in dims.items():
        n = int(v.get("count") or 0)
        rate = float(v.get("negative_rate") or 0.0)
        if n < 3:
            continue
        rows.append({"dimension": dim, "name": dim, "count": n, "rate": rate})
    rows.sort(key=lambda r: (-r["rate"], r["dimension"]))

    llm_issues = llm_text.get("top_issues") or []
    top_issues = []
    for i, r in enumerate(rows[:3]):
        entry = {
            "dimension": r["dimension"],
            "name": r["name"],
            "count": r["count"],
            "rate": round(r["rate"], 4),
        }
        interp = dimension_interpretation(r["name"], r["rate"], r["count"])
        entry["cause"] = (
            llm_issues[i]["cause"]
            if i < len(llm_issues) and llm_issues[i].get("cause")
            else interp["cause"]
        )
        entry["direction"] = (
            llm_issues[i]["direction"]
            if i < len(llm_issues) and llm_issues[i].get("direction")
            else interp["direction"]
        )
        top_issues.append(entry)

    improvements = llm_text.get("improvements") or rule_ss.get("improvements") or []
    return {
        "overall": llm_text.get("overall") or rule_ss.get("overall", ""),
        "positive": {"ratio": _ratio(pos_n), "phrases": _phrase_block("positive", pos_n)},
        "negative": {"ratio": _ratio(neg_n), "phrases": _phrase_block("negative", neg_n)},
        "top_issues": top_issues,
        "improvements": improvements,
        "degraded": bool(rule_ss.get("degraded")),
    }, "llm"
