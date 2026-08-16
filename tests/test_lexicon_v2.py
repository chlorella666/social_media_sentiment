# -*- coding: utf-8 -*-
"""词典 V2 单测（2.6 词典第一刀，2026-08-15）。

覆盖：粉丝向/方言黑话正名、精选单字正负词、ECSD 单字噪声不再直判负面、
语境词"一样/差别"不计分、官方/介绍语体压置信送 LLM。断言不依赖 ECSD 数据文件
（CI 无 data/ 也可跑）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coding import lexicon_v2 as lx  # noqa: E402
from app.coding.llm_analyzer import CONFIDENCE_THRESHOLD  # noqa: E402


def _sent(text: str) -> str:
    return lx.score_text(text)["sentiment"]


def _conf(text: str) -> float:
    return lx.score_text(text)["confidence"]


def test_fan_blackwords_positive() -> None:
    assert _sent("开荤了！就是不一样") == "positive"
    assert _sent("甘拜下风啊哥哥！！") == "positive"
    assert _sent("也够游戏闹麻了[星星眼][星星眼]") == "positive"
    assert _sent("大家还是太有梗了") == "positive"
    print("✓ 粉丝向/方言黑话正名（开荤/甘拜下风/闹麻了/有梗/星星眼）通过")


def test_curated_single_chars() -> None:
    assert _sent("太爽了") == "positive"
    assert _sent("很牛") == "positive"
    assert _sent("真坏") == "negative"
    assert _sent("太烦了") == "negative"
    assert _sent("很吵") == "negative"
    # 高碰撞单字（帅/美/强/快等）已移除：不再词典直判，交 LLM
    for text in ("很帅", "太美了", "快试试", "勉强可以"):
        r = lx.score_text(text)
        assert r["sentiment"] != "positive" or r["confidence"] < CONFIDENCE_THRESHOLD, text
    print("✓ 精选单字正负词（爽/牛/坏/烦/吵）+ 高碰撞单字移除 通过")


def test_ecsd_single_char_noise_not_direct_negative() -> None:
    """高碰撞单字（空/暗/涩/旧/乱/疯/苦/痛）不再触发直判负面。"""
    for text in (
        "恋与深空",          # 空
        "暗戳戳逗你",         # 暗
        "酸涩的爱和成长的痛",  # 涩/痛
        "仍旧",              # 旧
        "乱成一锅粥",         # 乱
        "笑疯了",            # 疯
        "苦命的哥",          # 苦
    ):
        r = lx.score_text(text)
        assert r["sentiment"] != "negative" or r["confidence"] < CONFIDENCE_THRESHOLD, text
    print("✓ ECSD 单字噪声不再高置信判负面 通过")


def test_context_word_yiyang() -> None:
    for text in ("和别家一样", "和别家差别不大"):
        r = lx.score_text(text)
        assert "一样" not in r["keywords"] and "差别" not in r["keywords"]
        assert r["sentiment"] == "neutral"
    print("✓ 语境词'一样/差别'不计分 通过")


def test_official_intro_lean_not_direct_positive() -> None:
    """官方/介绍/活动语体：仅正面信号时压置信送 LLM（防词典直判正面）。"""
    for text in (
        "DJI 大疆创新 - 官方网站 致力于成为持续推动人类进步的科技公司",
        "五星思念限时UP许愿「夏意涌向」即将开启！",
        "新手小白看过来！大疆pocket3简易使用教程",
        "《守望先锋》是一款团队第一人称射击游戏，游戏故事发生在未来",
    ):
        r = lx.score_text(text)
        assert r["confidence"] < CONFIDENCE_THRESHOLD, text
    print("✓ 官方/介绍/评测语体压置信送 LLM 通过")


def test_positive_miss_rows_not_direct_negative() -> None:
    """主集正面漏检 4 条词典直判错（2.6 取证样本）：不再被高置信判负面。"""
    rows = [
        "「恋与深空」伸舌头了！开荤了就是不一样哈",
        "这大苹果肌这手段，甘拜下风啊哥哥！！",
        "酸涩的爱和成长的痛。夏以昼仍旧啊。。",
        "lysk评论区给我笑疯了 已乱成一锅粥，大家还是太有梗了",
    ]
    for text in rows:
        r = lx.score_text(text)
        assert r["sentiment"] != "negative" or r["confidence"] < CONFIDENCE_THRESHOLD, text
    print("✓ 主集正面漏检 4 条不再词典直判负面 通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_fan_blackwords_positive()
    test_curated_single_chars()
    test_ecsd_single_char_noise_not_direct_negative()
    test_context_word_yiyang()
    test_official_intro_lean_not_direct_positive()
    test_positive_miss_rows_not_direct_negative()
    print("词典 V2 单测全部通过 ✅")


if __name__ == "__main__":
    main()
