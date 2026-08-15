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
    """列出全部领域：预置 + 用户自定义缓存（缓存优先，与 load_domain 口径一致）。

    2.5 起新领域经提案确认后写入 data/domain_schemas/ 缓存，UI 下拉与评测
    均通过本函数/load_domain 加载；domain_templates.json 是模板库，不是领域。
    """
    domains = []
    for path in sorted(p for p in DOMAINS_DIR.glob("*.json")
                       if p.name != "domain_templates.json"):
        schema = _load_file(path)
        domains.append({
            "id": schema.domain_id,
            "name": schema.domain_name,
            "template_id": schema.template_id,
        })
    seen = {d["id"] for d in domains}
    if CACHE_DIR.is_dir():
        for path in sorted(CACHE_DIR.glob("*.json")):
            try:
                schema = _load_file(path)
            except Exception:
                continue
            entry = {
                "id": schema.domain_id,
                "name": schema.domain_name,
                "template_id": schema.template_id,
                "cached": True,
            }
            if schema.domain_id in seen:
                # 缓存版覆盖同名预置（load_domain 缓存优先，保持一致）
                for i, d in enumerate(domains):
                    if d["id"] == schema.domain_id:
                        domains[i] = entry
                        break
            else:
                domains.append(entry)
                seen.add(schema.domain_id)
    return domains


def save_cached_schema(schema: DomainSchema, cache_dir: Path | None = None) -> Path:
    """保存用户编辑后的 schema 到缓存目录（用户可继续修改）。"""
    target = cache_dir or CACHE_DIR
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{schema.domain_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(schema.model_dump(), f, ensure_ascii=False, indent=2)
    return path
