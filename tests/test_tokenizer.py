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


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_segment_filters_stopwords()
    test_word_freq()
    test_cooccurrence()
    print("jieba 分词器测试全部通过 ✅")
