# -*- coding: utf-8 -*-
"""模块组合 schema 合成器单元测试（模块化分类 2026-08-17）。

覆盖：单模块原样模板、组合去重合并命名、组合维度上限约束、
按勾选裁剪物化、模块领域全量 LLM 路由判定。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.domains.composer import (  # noqa: E402
    MAX_DIMENSIONS,
    MERGE_SPEC,
    compose_schema,
    filter_schema_dims,
    load_templates,
    module_name,
)


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
        "content": (5, "数字产品"),
        "physical": (7, "有形实物"),
        "service": (7, "服务内容"),
    }
    for mid, (n, cn) in cases.items():
        s = compose_schema([mid])
        assert s.domain_id == f"modules_{mid}"
        assert s.domain_name == cn
        assert len(s.dimensions) == n
        # 单模块不触发合并
        assert all(d.origin != "template:merged" for d in s.dimensions)
    print("✓ 单模块 schema：模板原样（5/7/7 维，名称不变）")


def test_combo_merge_rules() -> None:
    # 内容 + 实物：价格/品牌/售后/功能稳定合并 → 10 维
    s = compose_schema(["content", "physical"])
    assert s.domain_id == "modules_content_physical"
    assert len(s.dimensions) == 10
    names = {d.id: d.name for d in s.dimensions}
    assert names["price_value"] == "价格价值"
    assert names["effectiveness"] == "功能与稳定性"
    assert names["channel_service"] == "渠道服务与售后"
    assert names["brand_image"] == "品牌形象"
    # 实物 + 服务：11 维（超出上限，由 UI 裁剪）
    s2 = compose_schema(["physical", "service"])
    assert s2.domain_id == "modules_physical_service"
    assert len(s2.dimensions) == 11
    assert len(s2.dimensions) > MAX_DIMENSIONS
    print("✓ 组合去重合并：价格/品牌/渠道售后/功能稳定按规则合并，命名正确")


def test_filter_schema_dims() -> None:
    s = compose_schema(["physical", "service"])
    picked = [d.id for d in s.dimensions][:MAX_DIMENSIONS]
    f = filter_schema_dims(s, picked)
    assert len(f.dimensions) == MAX_DIMENSIONS
    assert f.domain_id == s.domain_id
    assert [d.id for d in f.dimensions] == picked
    print("✓ 按勾选裁剪物化：组合 11 维裁剪到 10 维")


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
    test_combo_merge_rules()
    test_filter_schema_dims()
    test_module_names()
    test_always_llm_routing()
    test_subject_domain_routing()
    print("模块组合 schema 全部通过 ✅")


if __name__ == "__main__":
    main()
