# -*- coding: utf-8 -*-
"""2.4 维度级情感：词典轻量版维度情感 + 评测口径（维度情感 P/R/F1）。

覆盖：
  - match_dimension_sentiments：转折句逐维拆解 / 否定 / 无观点留空 /
    正负冲突留空 / use_names 中文名键；
  - match_dimensions：提及行为与旧实现一致；
  - dimension_report：维度情感微平均 P/R/F1、精确命中率、提及子报告
    的手工验算；幻觉维度 / 情感标错 / 漏标的计数。
"""

from __future__ import annotations

import sys

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = __file__.replace("\\", "/").rsplit("/", 1)[0].rsplit("/", 1)[0]
sys.path.insert(0, ROOT)

from app.coding.dimensions import match_dimension_sentiments, match_dimensions

GAME_SCHEMA = {
    "art": {"name": "美术反馈", "keywords": ["美术", "画面", "画风", "UI"]},
    "monetization": {"name": "氪金体验", "keywords": ["氪金", "抽卡", "价格", "贵"]},
    "gameplay": {"name": "玩法体验", "keywords": ["玩法", "战斗", "操作"]},
}
CONSUMER_SCHEMA = {
    "product_quality": {"name": "产品质量", "keywords": ["质量", "材质", "做工"]},
    "price_value": {"name": "价格价值", "keywords": ["价格", "贵", "便宜", "性价比"]},
}


def test_turn_sentence_per_dimension():
    """转折句逐维拆解：'画面好但价格贵'→ 美术 positive、氪金 negative。"""
    got = match_dimension_sentiments("画面好但价格贵", GAME_SCHEMA)
    assert got == {"art": "positive", "monetization": "negative"}, got


def test_negation_attribute():
    """否定归属：'质量不好'→ 产品质量 negative。"""
    got = match_dimension_sentiments("质量不好", CONSUMER_SCHEMA)
    assert got == {"product_quality": "negative"}, got


def test_no_dimension_mention():
    assert match_dimension_sentiments("今天天气不错", GAME_SCHEMA) == {}


def test_mention_without_sentiment():
    """提到维度但无褒贬 → 提及有、情感无（口径：无明确褒贬留空）。"""
    text = "这个游戏的玩法设定一共有三种"
    dims = match_dimensions(text, GAME_SCHEMA)
    assert dims == ["gameplay"], dims
    assert match_dimension_sentiments(text, GAME_SCHEMA) == {}


def test_conflicting_votes_skipped():
    """同一维度正负冲突 → 不标（无法定主倾向，对齐标注规范）。"""
    got = match_dimension_sentiments("画面好，但画面也很差", GAME_SCHEMA)
    assert got == {}, got


def test_use_names():
    got = match_dimension_sentiments("画面好", GAME_SCHEMA, use_names=True)
    assert got == {"美术反馈": "positive"}, got


def _dim_report(rows, preds):
    from tests.benchmark_golden import dimension_report

    return dimension_report(rows, preds)


def test_dimension_report_hand_calc():
    rows = [
        {"dimension_sentiments": {"A": "positive"}},
        {"dimension_sentiments": {"B": "negative"}},
        {"dimension_sentiments": {}},
        {"dimension_sentiments": {"A": "negative", "B": "positive"}},
    ]
    preds = [
        {"dimension_sentiments": {"A": "positive"}},
        {"dimension_sentiments": {"B": "positive"}},  # 情感标错：FP + FN
        {"dimension_sentiments": {}},
        {"dimension_sentiments": {"A": "negative", "B": "positive"}},
    ]
    rep = _dim_report(rows, preds)
    # 情感对：A 2 TP / B 1 TP + 1 错值（1 FP + 1 FN）→ P=R=F1=0.75
    assert rep["micro_precision"] == 0.75
    assert rep["micro_recall"] == 0.75
    assert rep["micro_f1"] == 0.75
    # 精确命中：gold 非空 3 行（r0/r1/r3），r1 值不同 → 2/3
    assert rep["exact_match_n"] == 3
    assert round(rep["exact_match_rate"], 4) == round(2 / 3, 4)
    # 提及子报告：全部命中 → 1.0
    assert rep["mention"]["micro_precision"] == 1.0
    assert rep["mention"]["micro_f1"] == 1.0
    # 每维情感 P/R/F1
    assert rep["per_dimension"]["A"]["f1"] == 1.0
    assert rep["per_dimension"]["B"]["precision"] == 0.5
    assert rep["per_dimension"]["B"]["recall"] == 0.5
    assert rep["per_dimension"]["B"]["n_gold"] == 2
    # 兼容旧字段
    assert "exact_match_rate" in rep and "micro_f1" in rep and "per_dimension" in rep


def test_dimension_report_hallucination():
    rows = [{"dimension_sentiments": {"A": "positive"}}]
    preds = [{"dimension_sentiments": {"A": "positive", "X": "negative"}}]
    rep = _dim_report(rows, preds)
    # A TP，X FP → P=1/2, R=1, F1=2/3；整条不精确命中
    assert rep["micro_precision"] == 0.5
    assert rep["micro_recall"] == 1.0
    assert round(rep["micro_f1"], 4) == round(2 / 3, 4)
    assert rep["exact_match_rate"] == 0.0


def test_dimension_report_empty_gold():
    rows = [{"dimension_sentiments": {}}]
    preds = [{"dimension_sentiments": {"A": "positive"}}]
    rep = _dim_report(rows, preds)
    assert rep["exact_match_n"] == 0
    assert rep["micro_precision"] == 0.0
    assert rep["micro_recall"] == 0.0
    assert rep["mention"]["micro_precision"] == 0.0


def test_schema_dimensions_normalization():
    """LLM v3.0 维度清单归一：DomainSchema / dict 两种形态均可。"""
    from app.core.models import Dimension, DomainSchema
    from app.coding.llm_analyzer import schema_dimensions

    schema = DomainSchema(
        domain_id="x", domain_name="X",
        dimensions=[Dimension(id="art", name="美术反馈", keywords=["画面"], description="视觉评价")],
    )
    out = schema_dimensions(schema)
    assert out == [{"id": "art", "name": "美术反馈", "keywords": ["画面"], "description": "视觉评价"}]
    out2 = schema_dimensions({"price": {"name": "价格", "keywords": ["贵"]}})
    assert out2[0]["id"] == "price" and out2[0]["name"] == "价格"
    assert out2[0]["description"] == ""
    assert schema_dimensions(None) == []


def test_sanitize_dimension_sentiments():
    """LLM 维度情感清洗：非法维度/取值剔除、大小写归一、非 dict 置空。"""
    from app.coding.llm_analyzer import sanitize_dimension_sentiments

    items = [
        {"dimension_sentiments": {
            "art": "positive", "fake": "positive", "price": "NEGATIVE",
            "x": "weird", "none": None,
        }},
    ]
    sanitize_dimension_sentiments(items, {"art", "price"})
    assert items[0]["dimension_sentiments"] == {"art": "positive", "price": "negative"}
    items2 = [{"dimension_sentiments": "bad"}, {"dimension_sentiments": None}]
    sanitize_dimension_sentiments(items2, {"art"})
    assert items2[0]["dimension_sentiments"] == {}
    assert items2[1]["dimension_sentiments"] == {}


def main() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"✓ {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"✗ {fn.__name__}: {exc}")
    print(f"\n维度级情感测试：{len(fns) - failed}/{len(fns)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
