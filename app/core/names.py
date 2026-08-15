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
    return _dimension_map().get(dim_id, dim_id)


def platform_cn(pid: str) -> str:
    return PLATFORM_CN.get(pid, pid)
