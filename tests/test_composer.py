# -*- coding: utf-8 -*-
"""模块组合 schema 合成器单元测试（模块化分类 2026-08-17）。

覆盖：单模块原样模板、组合去重合并命名、组合维度上限约束、
按勾选裁剪物化、模块领域全量 LLM 路由判定。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.domains.composer import (  # noqa: E402
    MAX_DIMENSIONS,
    MERGE_SPEC,
    compose_schema,
    filter_schema_dims,
    load_templates,
    module_name,
    templates_fingerprint,
)
from app.domains import loader


def test_templates_keywords() -> None:
    tpl = load_templates()
    total = 0
    for t in tpl["templates"]:
        for d in t["dimensions"]:
            assert d.get("keywords"), f"{t['id']}.{d['id']} 缺关键词"
            total += 1
    print(f"✓ 模板维度关键词齐备：{total} 个维度")


def test_single_module_schema() -> None:
    cases = {
        "content": (8, "数字产品"),
        "physical": (8, "有形实物"),
        "service": (7, "服务内容"),
    }
    for mid, (n, cn) in cases.items():
        s = compose_schema([mid])
        assert s.domain_id == f"modules_{mid}"
        assert s.domain_name == cn
        assert len(s.dimensions) == n
        # 单模块不触发合并
        assert all(d.origin != "template:merged" for d in s.dimensions)
    print("✓ 单模块 schema：模板原样（8/8/7 维，名称不变）")


def test_merge_rules_physical_service() -> None:
    """组合合并规则（2026-08-20 定稿）：physical+service 价格/品牌/售后合并，
    content 的 monetization/performance 不参与本组合。"""
    s = compose_schema(["physical", "service"])
    ids = [d.id for d in s.dimensions]
    assert "monetization" not in ids and "performance" not in ids
    assert "effectiveness" in ids
    assert "price" not in ids
    assert "brand_trust" not in ids
    assert "after_sales" not in ids
    merged = {d.id: d for d in s.dimensions if d.origin == "template:merged"}
    assert "price_value" in merged and merged["price_value"].name == "价格价值"
    assert "channel_service" in merged and merged["channel_service"].name == "渠道与售后"
    assert "brand_image" in merged and merged["brand_image"].name == "品牌与营销"
    assert merged["effectiveness"].name == "功能与稳定性"
    # 关键词应吸收（service.price 的会员/套餐等并入 price_value）
    for kw in ("会员", "套餐"):
        assert kw in merged["price_value"].keywords, f"price_value 缺合并关键词 {kw}"
    assert len(s.dimensions) == 12
    print(f"✓ 组合合并规则：physical+service → {len(s.dimensions)} 维，三类合并生效")


def test_template_fingerprint_invalidation() -> None:
    """模板改动后 modules_* 旧缓存自动失效：指纹不匹配 → 用当前模板重合成。"""
    fresh = compose_schema(["physical", "service"])
    assert fresh.template_fingerprint == templates_fingerprint()
    # 写一个陈旧缓存（错误指纹 + 残缺维度），loader 应忽略并重合成完整集
    stale = fresh.model_copy(update={
        "dimensions": fresh.dimensions[:3],
        "template_fingerprint": "stale" * 4,
    })
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        loader.save_cached_schema(stale, cache_dir=tmp_p)
        with mock.patch.object(loader, "CACHE_DIR", tmp_p):
            loaded = loader.load_domain("modules_physical_service")
        assert len(loaded.dimensions) == len(fresh.dimensions), "陈旧缓存未失效"
        assert loaded.template_fingerprint == templates_fingerprint()
        # 指纹一致的缓存正常复用
        loader.save_cached_schema(fresh, cache_dir=tmp_p)
        with mock.patch.object(loader, "CACHE_DIR", tmp_p):
            loaded2 = loader.load_domain("modules_physical_service")
        assert loaded2.dimensions == fresh.dimensions
    print("✓ 模板指纹：缓存失效自动重合成 / 一致时复用")


def test_combo_merge_rules() -> None:
    # 内容 + 实物：monetization→price_value、performance→effectiveness 合并 → 14 维
    s = compose_schema(["content", "physical"])
    assert s.domain_id == "modules_content_physical"
    assert len(s.dimensions) == 14
    names = {d.id: d.name for d in s.dimensions}
    assert names["price_value"] == "价格价值"
    assert names["effectiveness"] == "功能与稳定性"
    assert names["channel_service"] == "渠道与售后"
    assert names["brand_image"] == "品牌与营销"
    assert "monetization" not in names and "performance" not in names
    # 实物 + 服务：12 维（超出上限，由 UI 裁剪）
    s2 = compose_schema(["physical", "service"])
    assert s2.domain_id == "modules_physical_service"
    assert len(s2.dimensions) == 12
    assert len(s2.dimensions) > MAX_DIMENSIONS
    print("✓ 组合去重合并：价格/品牌/渠道售后/功能稳定按规则合并，命名正确")


def test_filter_schema_dims() -> None:
    s = compose_schema(["physical", "service"])
    picked = [d.id for d in s.dimensions][:MAX_DIMENSIONS]
    f = filter_schema_dims(s, picked)
    assert len(f.dimensions) == MAX_DIMENSIONS
    assert f.domain_id == s.domain_id
    assert [d.id for d in f.dimensions] == picked
    print("✓ 按勾选裁剪物化：组合 12 维裁剪到 10 维")


def test_module_names() -> None:
    assert module_name("content") == "数字产品"
    assert module_name("physical") == "有形实物"
    assert module_name("service") == "服务内容"
    print("✓ 模块用户侧命名：数字产品/有形实物/服务内容")


def test_always_llm_routing() -> None:
    from app.coding.llm_analyzer import is_always_llm_domain

    assert is_always_llm_domain("digital3c")
    assert is_always_llm_domain("game")
    assert is_always_llm_domain("consumer")
    assert is_always_llm_domain("modules_physical_service")
    assert not is_always_llm_domain(None)
    assert not is_always_llm_domain("")
    print("✓ 全量 LLM 路由：已评测领域 + modules_* 全部命中，空领域不命中")


def test_subject_domain_routing() -> None:
    from app.coding.llm_analyzer import uses_subject_domain

    assert uses_subject_domain("digital3c")
    assert uses_subject_domain("modules_physical")
    assert uses_subject_domain("modules_service")
    assert uses_subject_domain("modules_physical_service")
    # 纯数字内容产品沿用主集游戏口径（文本自身），不锚定品牌
    assert not uses_subject_domain("modules_content")
    assert not uses_subject_domain("game")
    assert not uses_subject_domain("consumer")
    assert not uses_subject_domain(None)
    assert not uses_subject_domain("")
    print("✓ 锚定品牌口径路由：digital3c/实物/服务命中；内容模块与 game/consumer 文本自身")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_templates_keywords()
    test_single_module_schema()
    test_merge_rules_physical_service()
    test_template_fingerprint_invalidation()
    test_combo_merge_rules()
    test_filter_schema_dims()
    test_module_names()
    test_always_llm_routing()
    test_subject_domain_routing()
    print("模块组合 schema 全部通过 ✅")


if __name__ == "__main__":
    main()
