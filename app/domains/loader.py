"""领域 schema 加载器（预置 JSON + 缓存目录）。"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.models import DomainSchema

DOMAINS_DIR = Path(__file__).resolve().parent
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "domain_schemas"


def _load_file(path: Path) -> DomainSchema:
    with open(path, "r", encoding="utf-8") as f:
        return DomainSchema.model_validate(json.load(f))


def load_domain(domain_id: str) -> DomainSchema:
    """优先加载缓存 schema，否则加载预置 schema。"""
    cache_path = CACHE_DIR / f"{domain_id}.json"
    if cache_path.exists():
        return _load_file(cache_path)
    preset = DOMAINS_DIR / f"{domain_id}.json"
    if not preset.exists():
        raise FileNotFoundError(f"未找到领域 schema: {domain_id}")
    return _load_file(preset)


def list_domains() -> list[dict]:
    domains = []
    for path in sorted(DOMAINS_DIR.glob("*.json")):
        schema = _load_file(path)
        domains.append({"id": schema.domain_id, "name": schema.domain_name})
    return domains


def save_cached_schema(schema: DomainSchema) -> Path:
    """保存用户编辑后的 schema 到缓存目录（用户可继续修改）。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{schema.domain_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(schema.model_dump(), f, ensure_ascii=False, indent=2)
    return path
