# -*- coding: utf-8 -*-
"""丢弃记录口径（2026-08-19 结构化）。

dropped 记录创建时标注 kind：
- collection：采集层跳过（API 同帖重复返回 / 超出时间范围 / 广告），补采豁免；
- duplicate：清洗层去重（重复（相同ID/链接/标题/正文）），补采豁免；
- quality：质量丢弃（不相关/过短/样板/官方/正文为空/人工剔除/LLM 复核），计入决策。

is_quality_drop 是「补采触发」与「2.2 有效供给」的决策入口：
- kind 优先（结构化语义，reason 仅供展示）；
- 旧数据无 kind 时兜底：剥「补采一致性清洗:」包装前缀后按已知采集层原因前缀判定。

本模块零依赖 app 内部包，避免 pipeline ↔ keyword_effects 依赖环。
方案见 docs/archive/丢弃口径结构化方案.md。
"""

from __future__ import annotations

PREFIX_WRAP = "补采一致性清洗:"
# 采集层/去重类原因前缀（兜底用；缺链接=小红书无 URL item，补采重返回同一条）
NON_QUALITY_DROP_PREFIXES = ("重复", "超出时间范围", "广告", "缺少链接")
KINDS = {"collection", "duplicate", "quality"}


def is_quality_drop(drop: dict) -> bool:
    """决策入口：结构化 kind 优先；缺失时兜底文案判定。"""
    kind = str((drop or {}).get("kind") or "").strip()
    if kind in KINDS:
        return kind == "quality"
    # 未知 kind（拼写错误/旧版本字段）：回落文案判定，避免静默改变决策
    return _reason_is_quality(str((drop or {}).get("reason") or ""))


def _reason_is_quality(reason: str) -> bool:
    """旧数据兜底：先剥补采一致性包装前缀，再按已知采集层原因前缀判定。"""
    r = (reason or "").strip()
    if r.startswith(PREFIX_WRAP):
        r = r[len(PREFIX_WRAP):]
    if not r:
        return False
    return not r.startswith(NON_QUALITY_DROP_PREFIXES)
