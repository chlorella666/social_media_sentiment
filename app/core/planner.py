"""采集计划生成：品牌名 → 维度驱动关键词建议 → AnalysisPlan。"""

from __future__ import annotations

import re
from datetime import date

from app.core.models import AnalysisPlan, ChannelConfig, Dimension, DomainSchema, KeywordGroup

# 2.8 自定义维度约束（2026-08-18 定版）
MAX_CUSTOM_DIMENSIONS = 4
MAX_CUSTOM_NAME_LEN = 8
MIN_CUSTOM_KEYWORDS = 2
MAX_CUSTOM_KEYWORDS = 5


def parse_custom_dimensions(text: str) -> tuple[list[Dimension], list[str]]:
    """解析「维度名：关键词1,关键词2,…」（每行一个维度，2.8 定版）。

    返回 (维度列表, 错误列表)；id 为 custom_<序号>（任务级，防与预置维度冲突）。
    约束：≤4 个维度、名称 1~8 字、关键词 2~5 个（去重）。
    """
    dims: list[Dimension] = []
    errors: list[str] = []
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if "：" in line:
            name, kw_part = line.split("：", 1)
        elif ":" in line:
            name, kw_part = line.split(":", 1)
        else:
            errors.append(f"第 {i} 行缺少「：」分隔，格式应为「维度名：关键词1,关键词2,…」")
            continue
        name = name.strip()
        kws = [k.strip() for k in re.split(r"[,，、\s]+", kw_part) if k.strip()]
        seen: set[str] = set()
        kws_uniq = [k for k in kws if not (k in seen or seen.add(k))]
        problems: list[str] = []
        if not name:
            problems.append("维度名为空")
        elif len(name) > MAX_CUSTOM_NAME_LEN:
            problems.append(f"维度名超过 {MAX_CUSTOM_NAME_LEN} 字（当前 {len(name)} 字）")
        if len(kws_uniq) < MIN_CUSTOM_KEYWORDS:
            problems.append(f"关键词少于 {MIN_CUSTOM_KEYWORDS} 个（当前 {len(kws_uniq)} 个）")
        elif len(kws_uniq) > MAX_CUSTOM_KEYWORDS:
            problems.append(f"关键词超过 {MAX_CUSTOM_KEYWORDS} 个（当前 {len(kws_uniq)} 个）")
        if problems:
            errors.append(f"第 {i} 行「{name or line[:12]}」：" + "；".join(problems))
            continue
        dims.append(Dimension(
            id=f"custom_{i:02d}",
            name=name,
            description=f"自定义维度（{name}）",
            source="custom",
            keywords=kws_uniq,
            origin="custom",
        ))
    if len(dims) > MAX_CUSTOM_DIMENSIONS:
        errors.append(f"自定义维度最多 {MAX_CUSTOM_DIMENSIONS} 个（当前 {len(dims)} 个）")
        dims = dims[:MAX_CUSTOM_DIMENSIONS]
    return dims, errors


def generate_keyword_groups(subject: str, schema: DomainSchema, dimension_ids: list[str]) -> list[KeywordGroup]:
    """按选定维度生成关键词建议（借鉴 SocialBrandSentiment 维度驱动策略）。

    每个维度生成 3 类词：
    - 短词：品牌名 + 维度核心词
    - 中词：品牌名 + 维度 + 常见讨论场景词
    - 长词：品牌名 + 维度 + 用户视角表达
    """
    groups: list[KeywordGroup] = []
    for dim in schema.dimensions:
        if dimension_ids and dim.id not in dimension_ids:
            continue
        base_words = dim.keywords[:6] if dim.keywords else [dim.name]
        keywords = [
            f"{subject} {base_words[0]}",
            f"{subject} {dim.name}",
            f"{subject} {base_words[1] if len(base_words) > 1 else base_words[0]} 评价",
        ]
        if len(base_words) >= 3:
            keywords.append(f"{subject} {base_words[2]} 怎么样")
        # 去重并保序
        seen: set[str] = set()
        deduped = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                deduped.append(kw)
        groups.append(KeywordGroup(dimension_id=dim.id, dimension_name=dim.name, keywords=deduped))
    return groups


def flatten_keywords(groups: list[KeywordGroup]) -> list[str]:
    """平铺关键词列表（供展示与采集）。"""
    keywords: list[str] = []
    for group in groups:
        keywords.extend(group.keywords)
    return keywords


def build_plan(
    *,
    subject: str,
    domain_id: str | None,
    dimension_ids: list[str],
    keyword_groups: list[KeywordGroup],
    manual_keywords: list[str] | None = None,
    channel_ids: list[str],
    date_start: date | None,
    date_end: date | None,
    per_keyword_limit: int = 50,
    comments_enabled: bool = True,
    comments_per_post: int = 20,
    llm_enabled: bool = False,
    llm_base_url: str = "",
    llm_model: str = "",
    custom_dimensions: list[Dimension] | None = None,
    narrative_enabled: bool = False,
    relevance_check_enabled: bool = False,
    channel_params: dict[str, dict] | None = None,
    exclude_words: list[str] | None = None,
    review_enabled: bool = False,
    exclude_ad_enabled: bool = False,
) -> AnalysisPlan:
    """组装采集计划。manual_keywords 非空时以手动关键词为准。"""
    if manual_keywords:
        keywords = [k.strip() for k in manual_keywords if k.strip()]
        groups = [
            KeywordGroup(dimension_id="manual", dimension_name="手动关键词", keywords=keywords)
        ]
    else:
        groups = keyword_groups
        keywords = flatten_keywords(groups)

    channels = [
        ChannelConfig(
            channel_id=cid,
            enabled=True,
            params=(channel_params or {}).get(cid, {}),
        )
        for cid in channel_ids
    ]
    return AnalysisPlan(
        subject=subject,
        domain_id=domain_id,
        dimensions=dimension_ids,
        custom_dimensions=custom_dimensions or [],
        keyword_groups=groups,
        keywords=keywords,
        channels=channels,
        date_start=date_start,
        date_end=date_end,
        per_keyword_limit=per_keyword_limit,
        comments_enabled=comments_enabled,
        comments_per_post=comments_per_post,
        llm_enabled=llm_enabled,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        narrative_enabled=narrative_enabled,
        relevance_check_enabled=relevance_check_enabled,
        exclude_words=exclude_words or [],
        review_enabled=review_enabled,
        exclude_ad_enabled=exclude_ad_enabled,
    )


def save_plan(plan: AnalysisPlan, path) -> None:
    """落盘采集计划（作为向导与执行层之间的协议文件）。"""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
