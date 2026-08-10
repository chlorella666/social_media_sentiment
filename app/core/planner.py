"""采集计划生成：品牌名 → 维度驱动关键词建议 → AnalysisPlan。"""

from __future__ import annotations

from datetime import date

from app.core.models import AnalysisPlan, ChannelConfig, DomainSchema, KeywordGroup


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
    narrative_enabled: bool = False,
    relevance_check_enabled: bool = False,
    channel_params: dict[str, dict] | None = None,
    exclude_words: list[str] | None = None,
    review_enabled: bool = False,
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
    )


def save_plan(plan: AnalysisPlan, path) -> None:
    """落盘采集计划（作为向导与执行层之间的协议文件）。"""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
