# -*- coding: utf-8 -*-
"""维度匹配与维度级情感（2.4 Aspect-based 的词典侧实现）。

- match_dimensions：维度提及（schema 关键词命中，保持 2.3 及之前的旧行为）；
- match_dimension_sentiments：维度级情感（词典轻量版，确定性、可回归）。
  分句打分：把文本按标点/转折词切分，用词典给每个分句打整句情感，
  把分句情感归给"分句内命中的维度"。用于：
    1) 评测层（benchmark_golden.py）词典模式的维度情感预测；
    2) 生产链路词典兜底（离线 / 低置信未送 LLM 时报告的维度情感来源）。

口径说明：这是"轻量极性聚合"兜底，质量预期低于 LLM 维度情感，
只保证报告有数、结果可复现；2.4 主口径以 LLM 维度情感为准。
"""

from __future__ import annotations

import re
from collections import defaultdict

from app.coding import lexicon_v2

# 分句切分：标点 + 常见转折词（"画面好但价格贵"→ 两个分句）
CLAUSE_SPLIT_RE = re.compile(
    r"[，。！？；、,.!?;:：\n\r]|但|但是|不过|然而|可是|却|而"
)


def _iter_dims(schema) -> list[tuple[str, object]]:
    """兼容两种 schema 形态，返回 [(dim_id, dim)]：
    - DomainSchema 对象（d.id / d.name / d.keywords）；
    - 原始 dict：{"dimensions": [...]}（id 在元素内）或 {id: {...}}（id 是键）。
    """
    if schema is None:
        return []
    if hasattr(schema, "dimensions"):
        return [(d.id, d) for d in schema.dimensions]
    if isinstance(schema, dict):
        if "dimensions" in schema:
            return [(d.get("id", ""), d) for d in schema["dimensions"]]
        return [(k, v) for k, v in schema.items()]
    if isinstance(schema, (list, tuple)):
        return [(d.get("id", ""), d) for d in schema]
    return []


def _dim_name(d) -> str:
    return d.name if hasattr(d, "name") else d.get("name", "")


def _dim_keywords(d) -> list[str]:
    kws = d.keywords if hasattr(d, "keywords") else d.get("keywords", [])
    return [k for k in (kws or []) if k]


def match_dimensions(text: str, schema) -> list[str]:
    """维度提及：schema 关键词命中返回维度 id 列表（旧行为，供兼容）。"""
    if not text:
        return []
    lowered = text.lower()
    return [
        dim_id
        for dim_id, d in _iter_dims(schema)
        if any(kw.lower() in lowered for kw in _dim_keywords(d))
    ]


def match_dimension_sentiments(
    text: str, schema, use_names: bool = False
) -> dict[str, str]:
    """维度级情感（词典轻量版）：{维度键: positive|negative}。

    规则：
      - 分句后每句用词典打整句情感，neutral 分句不贡献；
      - 分句内命中的维度获得该分句的情感（"画面好但价格贵"→美术 positive、价格 negative）；
      - 同一维度多分句情感冲突（正负都有）→ 不标（无明确褒贬留空，对齐标注规范 §五）；
      - use_names=True 时键为维度中文名（评测层 gold 用中文名），默认维度 id。
    """
    if not text:
        return {}
    dims = list(_iter_dims(schema))
    if not dims:
        return {}
    votes: dict[str, list[str]] = defaultdict(list)
    for clause in CLAUSE_SPLIT_RE.split(text):
        clause = clause.strip()
        if not clause:
            continue
        clause_sent = lexicon_v2.score_text(clause)["sentiment"]
        if clause_sent == "neutral":
            continue
        lowered = clause.lower()
        for dim_id, d in dims:
            if any(kw.lower() in lowered for kw in _dim_keywords(d)):
                votes[_dim_name(d) if use_names else dim_id].append(clause_sent)
    out: dict[str, str] = {}
    for dim_key, sents in votes.items():
        pos = sents.count("positive")
        neg = sents.count("negative")
        if pos and not neg:
            out[dim_key] = "positive"
        elif neg and not pos:
            out[dim_key] = "negative"
    return out
