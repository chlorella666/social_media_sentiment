"""展示层名称映射：平台 / 维度 id → 中文名（图表、解析文字共用）。"""

from __future__ import annotations

from functools import lru_cache

from app.domains import loader

PLATFORM_CN = {
    "demo": "演示数据",
    "websearch": "全网搜索",
    "websearch_zhihu": "知乎",
    "websearch_tieba": "贴吧",
    "websearch_taptap": "TapTap",
    "bilibili": "B站",
    "weibo": "微博",
    "xiaohongshu": "小红书",
}

NARRATIVE_CN = {
    "conflict": "冲突",
    "human_interest": "人情味",
    "attribution": "归因",
    "economic": "经济后果",
    "morality": "道德",
}

NARRATIVE_FRAMES = ("conflict", "human_interest", "attribution", "economic", "morality")

ATTRIBUTION_CN = {
    "government": "政府",
    "enterprise": "企业",
    "individual": "个人",
    "system": "制度",
    "technology": "技术",
    "society": "社会",
    "nature": "自然",
    "unclear": "不明确",
}

ATTRIBUTION_ACTORS = (
    "government", "enterprise", "individual", "system",
    "technology", "society", "nature", "unclear",
)

_EXTRA_DIM_CN = {
    "manual": "手动关键词",
    "custom": "自定义",
    "overall": "整体",
}


@lru_cache(maxsize=None)
def _dimension_map() -> dict[str, str]:
    """汇总全部预置 + 缓存领域 schema 的维度 id → 中文名（缓存；2.5 新领域自动纳入）。"""
    m = dict(_EXTRA_DIM_CN)
    for path in sorted(loader.DOMAINS_DIR.glob("*.json")):
        if path.name == "domain_templates.json":
            continue
        try:
            schema = loader.load_domain(path.stem)
        except Exception:
            continue
        for dim in schema.dimensions:
            m.setdefault(dim.id, dim.name)
    if loader.CACHE_DIR.is_dir():
        for path in sorted(loader.CACHE_DIR.glob("*.json")):
            try:
                schema = loader.load_domain(path.stem)
            except Exception:
                continue
            for dim in schema.dimensions:
                m[dim.id] = dim.name  # 缓存领域覆盖预置（load_domain 缓存优先，保持一致）
    return m


def dimension_cn(dim_id: str) -> str:
    # 2.8：任务级自定义维度优先（渲染前由 register_custom_dim_names 注册）
    return _CUSTOM_DIM_NAMES.get(dim_id) or _dimension_map().get(dim_id, dim_id)


_CUSTOM_DIM_NAMES: dict[str, str] = {}


def register_custom_dim_names(plan) -> None:
    """注册当前任务的自定义维度 id → 显示名（含「自定义」标记）。

    任务级（本地单任务渲染），pipeline 与结果页渲染前调用；旧任务无
    custom_dimensions 时清空回退静态映射。
    """
    _CUSTOM_DIM_NAMES.clear()
    for d in getattr(plan, "custom_dimensions", None) or []:
        _CUSTOM_DIM_NAMES[d.id] = f"{d.name}（自定义）"


def platform_cn(pid: str) -> str:
    return PLATFORM_CN.get(pid, pid)
