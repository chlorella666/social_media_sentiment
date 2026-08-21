# -*- coding: utf-8 -*-
"""模块组合 schema 合成器（模块化分类：数字产品 / 有形实物 / 服务内容）。

2026-08-17 定版（用户决策）：
- 所有品牌一律纯模块生成维度（不做品牌→领域映射、不预填已评测领域）；
- 模块 = 模板 content/physical/service，品牌可多选；
- 维度 = 所选模块模板维度并集，按概念去重合并；
- 组合维度强制 ≤10（超出由向导引导用户取消勾选后再继续）。

合并规则（组合时生效，单模块原样保留模板维度）：
- 价格类：content.monetization(付费与商业化) / physical.price_value(价格价值) /
  service.price(价格与性价比) → 保留 price_value「价格价值」；
- 品牌类：physical.brand_image(品牌与营销) / service.brand_trust(品牌信任与口碑)
  → 保留 brand_image「品牌与营销」；
- 渠道售后类：physical.channel_service(渠道与售后) / service.after_sales(售后与投诉处理)
  → 保留 channel_service「渠道与售后」；
- 功能稳定类：content.performance(性能稳定性) / physical.effectiveness(功能效果/使用体验)
  → 保留 effectiveness「功能与稳定性」。

物化策略：向导确认时按「选中的维度子集」落盘到 data/domain_schemas/
modules_<ids>.json（worker 按 domain_id 直接 load_domain，无需改计划模型）；
同一组合重复物化以后一次为准（与既有缓存 schema 语义一致）。
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

from app.core.models import DomainSchema
from app.core.models import Dimension

TEMPLATES_FILE = Path(__file__).resolve().parent / "domain_templates.json"


def templates_fingerprint() -> str:
    """模板文件 sha256（模块 schema 缓存失效依据，2026-08-20）。"""
    return hashlib.sha256(
        TEMPLATES_FILE.read_bytes()
    ).hexdigest()[:16]

MODULE_CN = {
    "content": "数字产品",
    "physical": "有形实物",
    "service": "服务内容",
}
MODULE_DESC = {
    "content": "软件应用、游戏、数字内容等；功能与内容体验为主，性能、付费、安全为次",
    "physical": "有形商品（消费品/3C/食品等）；品质、效果与价格为主，渠道、服务与品牌为次",
    "service": "服务过程（餐饮/门店/平台服务等）；过程体验为主，价格与信任为次",
}
MAX_DIMENSIONS = 10

# 合并目标规范：canonical id -> (名称, 描述)
MERGE_SPEC = {
    "price_value": ("价格价值", "定价、付费机制、价格、性价比、促销折扣"),
    "brand_image": ("品牌与营销", "品牌声誉、营销、代言、信任与口碑"),
    "channel_service": ("渠道与售后", "购买渠道、物流、客服、售后与投诉处理"),
    "effectiveness": ("功能与稳定性", "核心功能、使用效果、性能与稳定性"),
}
# 合并映射：源维度 id -> canonical id（仅组合时生效）
MERGE_MAP = {
    "monetization": "price_value",
    "performance": "effectiveness",
    "price": "price_value",
    "brand_trust": "brand_image",
    "after_sales": "channel_service",
}


def module_name(module_id: str) -> str:
    return MODULE_CN.get(module_id, module_id)


def load_templates() -> dict:
    return json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))


def compose_schema(module_ids: list[str]) -> DomainSchema:
    """合成模块组合 schema（完整并集，含合并；调用方按用户勾选再裁剪）。

    仅返回合并后的完整维度集；维度数可能超过 MAX_DIMENSIONS，
    由 UI 引导用户取消勾选到 ≤10 后物化。
    """
    templates = load_templates()
    by_id = {t["id"]: t for t in templates["templates"]}
    ordered = [m for m in ("content", "physical", "service") if m in module_ids]
    if not ordered:
        raise ValueError("至少选择一个模块")
    merged = len(ordered) > 1
    dims: list[dict] = []
    seen: set[str] = set()
    for mid in ordered:
        for d in by_id[mid]["dimensions"]:
            canon = MERGE_MAP.get(d["id"], d["id"]) if merged else d["id"]
            if canon in seen:
                # 合并：吸收后出现的 keywords（首个位置保留）
                for dim in dims:
                    if dim["id"] == canon:
                        for kw in d.get("keywords", []):
                            if kw not in dim["keywords"]:
                                dim["keywords"].append(kw)
                        break
                continue
            seen.add(canon)
            entry = {
                "id": canon,
                "name": d["name"],
                "description": d.get("description", ""),
                "source": d.get("source", ""),
                "keywords": list(d.get("keywords", [])),
                "sub_dimensions": [],
                "origin": f"template:{mid}",
            }
            if merged and canon in MERGE_SPEC:
                entry["name"], entry["description"] = MERGE_SPEC[canon]
                entry["origin"] = "template:merged"
            dims.append(entry)
    return DomainSchema(
        domain_id="modules_" + "_".join(ordered),
        domain_name=" + ".join(module_name(m) for m in ordered),
        dimensions=[
            Dimension(
                id=d["id"],
                name=d["name"],
                description=d["description"],
                source=d["source"],
                keywords=d["keywords"],
                sub_dimensions=[],
                origin=d["origin"],
            )
            for d in dims
        ],
        version="1.0",
        template_id=None,
        template_fingerprint=templates_fingerprint(),
    )


def filter_schema_dims(schema: DomainSchema, dim_ids: list[str]) -> DomainSchema:
    """按用户勾选裁剪 schema（仅保留选中维度，供物化落盘）。"""
    keep = {d.id: d for d in schema.dimensions}
    selected = [keep[i] for i in dim_ids if i in keep]
    return schema.model_copy(update={"dimensions": selected})
