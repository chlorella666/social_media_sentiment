"""领域 schema 加载器（预置 JSON + 缓存目录）。"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.models import Dimension, DomainSchema

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


def list_domains(include_modules: bool = False) -> list[dict]:
    """列出全部领域：预置 + 用户自定义缓存（缓存优先，与 load_domain 口径一致）。

    2.5 起新领域经提案确认后写入 data/domain_schemas/ 缓存，UI 下拉与评测
    均通过本函数/load_domain 加载；domain_templates.json 是模板库，不是领域。
    模块组合 schema（modules_*，模块化分类 2026-08-17）默认不列入领域注册表，
    避免污染领域下拉/评测筛选；评测模块卷时用 --golden + --mode-key 直接指定。
    """
    domains = []
    for path in sorted(p for p in DOMAINS_DIR.glob("*.json")
                       if p.name != "domain_templates.json"):
        if not include_modules and path.stem.startswith("modules_"):
            continue
        schema = _load_file(path)
        domains.append({
            "id": schema.domain_id,
            "name": schema.domain_name,
            "template_id": schema.template_id,
        })
    seen = {d["id"] for d in domains}
    if CACHE_DIR.is_dir():
        for path in sorted(CACHE_DIR.glob("*.json")):
            if not include_modules and path.stem.startswith("modules_"):
                continue
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


def task_schema(plan) -> DomainSchema | None:
    """按计划组装分析 schema：预置/模块 schema + 任务级自定义维度（2.8）。

    - 预置/模块维度照常加载（domain_id）；
    - `plan.custom_dimensions` 合并进维度清单（id 已带 custom_ 前缀防冲突，
      显示名追加「（自定义）」标记，供 LLM 提示词/报告/Excel 共用）；
    - 未选模块时 schema = 仅自定义维度（domain_id="custom"）。
    """
    base = None
    if getattr(plan, "domain_id", None):
        try:
            base = load_domain(plan.domain_id)
        except FileNotFoundError:
            base = None
    dims = list(base.dimensions) if base else []
    existing = {d.id for d in dims}
    for cd in getattr(plan, "custom_dimensions", None) or []:
        if cd.id in existing:
            continue
        dims.append(cd.model_copy(update={"name": f"{cd.name}（自定义）"}))
        existing.add(cd.id)
    if not dims:
        return None
    return DomainSchema(
        domain_id=base.domain_id if base else "custom",
        domain_name=base.domain_name if base else "自定义维度",
        dimensions=dims,
        version=getattr(base, "version", "1.0") if base else "1.0",
        template_id=getattr(base, "template_id", None),
    )
