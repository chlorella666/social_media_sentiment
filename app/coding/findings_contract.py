"""F-027（2026-08-27）：核心发现结构化契约 + conclusion_text。

- LLM 输出 conclusion_text（一句话情感凝练总结：谁、什么情绪、集中在哪、风险在哪）
  与 findings 4 字段：claim（≤30 字一句话）/ scope（整体/维度/趋势/主题/平台…）/
  detail（数据支撑+可能原因，用「可能/或与…相关」、禁强动作词）/ action（对象+渠道）；
- 数字必须引用「统计描述」真实值，不得编造；
- 解析/校验/回退为纯函数，单测覆盖四态（解析成功/失败/字段缺失/scope 非法）。
"""

from __future__ import annotations

import json

VALID_SCOPES = {"整体", "维度", "趋势", "主题", "平台", "其他"}


def parse_insights_output(content: str) -> dict | None:
    """解析 LLM JSON 输出 → {conclusion_text, chart_insights, findings}；失败 None。"""
    if not content or not isinstance(content, str):
        return None
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def validate_conclusion_text(raw: object) -> str:
    """conclusion_text 校验：非空 str → 去空白；否则空串（调用方回退）。"""
    if isinstance(raw, str):
        t = raw.strip()
        if t:
            return t
    return ""


def validate_findings(raw: object) -> list[dict]:
    """findings 校验：每条保留 id/claim/scope/detail/action/evidence_refs/narrative_label。

    - claim 非空 str 才保留；
    - scope 非法值归一为「其他」；
    - detail/action 缺失允许（渲染层回退或留空）。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        claim = f.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            continue
        scope = f.get("scope")
        if not isinstance(scope, str) or scope not in VALID_SCOPES:
            scope = "其他"
        entry: dict = {"id": f.get("id") or f"F{len(out) + 1}", "claim": claim.strip(), "scope": scope}
        detail = f.get("detail")
        if isinstance(detail, str) and detail.strip():
            entry["detail"] = detail.strip()
        action = f.get("action")
        if isinstance(action, str) and action.strip():
            entry["action"] = action.strip()
        refs = f.get("evidence_refs")
        if isinstance(refs, list):
            entry["evidence_refs"] = [x for x in refs if isinstance(x, str)]
        else:
            entry["evidence_refs"] = []
        nl = f.get("narrative_label")
        if isinstance(nl, str) and nl.strip():
            entry["narrative_label"] = nl.strip()
        out.append(entry)
    return out


def build_lead(conclusion_text: str, structured_summary: dict | None = None) -> str:
    """一句话总结：LLM conclusion_text 优先；缺失回退词典 structured_summary.overall。"""
    if conclusion_text:
        return conclusion_text
    if structured_summary and structured_summary.get("overall"):
        return str(structured_summary["overall"])
    return ""
