"""jieba 分词器测试：停用词过滤、词频统计、共现关系。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coding.tokenizer import build_cooccurrence, build_word_freq, segment


def test_segment_filters_stopwords() -> None:
    words = segment("我觉得这款产品的质量真的非常好，性价比很高")
    assert "觉得" not in words
    assert "真的" not in words
    assert "质量" in words
    assert "性价比" in words
    print("✓ jieba 分词过滤停用词")


def test_word_freq() -> None:
    freq = build_word_freq(["画质很好画质不错", "画质一流"], top_n=10)
    top_word, count = freq[0]
    assert count >= 3
    print(f"✓ 词频统计正常（最高频词 {top_word} × {count}）")


def test_cooccurrence() -> None:
    edges = build_cooccurrence(
        ["抽卡概率太低很坑", "抽卡保底太坑", "抽卡概率暗改"],
        window=3,
        top_n=20,
    )
    assert edges, "应有共现关系"
    words = {edges[0]["source"], edges[0]["target"]}
    assert "抽卡" in words or "概率" in words or "保底" in words
    print(f"✓ 共现网络边示例：{edges[0]['source']}-{edges[0]['target']}（{edges[0]['weight']}）")


def test_extract_phrases_fixture() -> None:
    """F-009：短语完整（比想象中好/质量堪忧/屏幕易碎/电池掉电快/英文产品名），碎片不入选。"""
    from app.coding.tokenizer import extract_phrases
    texts = [
        "比想象中好很多，推荐购买", "比想象中好，性价比高",
        "质量堪忧，后悔入手了", "质量堪忧，不建议",
        "屏幕易碎，要小心", "屏幕易碎，摔一下就坏",
        "电池掉电快，一天两充", "电池掉电快，很麻烦",
        "iPhone 15 Pro Max 拍照很强", "iPhone 15 Pro Max 手感好",
    ]
    sents = ["positive", "positive", "negative", "negative", "negative",
             "negative", "negative", "negative", "positive", "positive"]
    ph = extract_phrases(texts, sentiments=sents, brand="某手机", keywords=["手机"])
    phrases = [p["phrase"] for p in ph]
    for target in ["比想象中好", "质量堪忧", "屏幕易碎", "电池掉电快", "iPhone 15 Pro Max"]:
        assert target in phrases, f"短语未完整入选: {target}"
    assert "想象中" not in phrases, "碎片未剔除: 想象中"
    assert all("sentiment_weights" in p for p in ph), "缺少情感权重"


def test_extract_phrases_p0_filters() -> None:
    """F-015 P0：碎片/泛词/实体 junk 不入选，真实短语保留。"""
    from app.coding.tokenizer import extract_phrases
    texts = [
        "在这种情况下不知道怎么办", "在这种情况下不知道怎么办",
        "2026年发布的新机", "2026年发布的新机",
        "评价总时长大约5分钟", "评价总时长大约5分钟",
        "屏幕易碎很麻烦", "屏幕易碎很麻烦",
        "电池掉电快一天两充", "电池掉电快一天两充",
    ]
    sents = ["neutral", "neutral", "neutral", "neutral",
             "negative", "negative", "negative", "negative",
             "negative", "negative"]
    ph = extract_phrases(texts, sentiments=sents, brand="测试机", keywords=["测试机"])
    phrases = [p["phrase"] for p in ph]
    for bad in ("情况下", "不知道", "2026年", "小时后", "评价总时长"):
        assert bad not in phrases, f"碎片未过滤: {bad}"
    # 完整短语优先（子串剔除）："屏幕易碎很麻烦" 入选即代表观点可读
    assert any("屏幕易碎" in p for p in phrases), "真实短语被误杀"
    assert any("电池掉电快" in p for p in phrases), "真实短语被误杀"
    print("✓ F-015 P0 短语质量：碎片/泛词/实体过滤 通过")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_segment_filters_stopwords()
    test_word_freq()
    test_cooccurrence()
    test_extract_phrases_fixture()
    test_extract_phrases_p0_filters()
    print("jieba 分词器测试全部通过 ✅")
