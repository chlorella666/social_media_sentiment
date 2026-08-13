# -*- coding: utf-8 -*-
"""边界样本专项集抽样测试（2.3 步骤 2）。

运行：python tests/test_edge_sampling.py
覆盖：方言/emoji 主导识别、子集映射、渠道配额、跨子集去重、种子可复现、
      样本池不足短报、低置信得分区间。
"""

from __future__ import annotations

import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tests import sample_golden_set as sgs  # noqa: E402


def make_sample(tid: str, platform: str, text: str, domain: str = "game") -> dict:
    return {
        "text_id": tid, "platform": platform, "domain": domain,
        "brand": "", "keyword": "", "kind": "post", "text": text,
        "title": "", "author": "", "url": f"https://x/{tid}", "time": "",
        "likes": "", "batch": "test", "post_url": f"https://x/{tid}",
        "flags": sgs.edge_flags(text, domain, {"game": {}, "consumer": {}}),
    }


def test_flags_dialect_and_emoji() -> None:
    assert "dialect" in sgs.edge_flags("哩个嘢真係好正 唔该", "consumer", {})
    assert "dialect" not in sgs.edge_flags("这个东西真好", "consumer", {})
    assert "emoji_dominant" in sgs.edge_flags("😍😍😍 太好看了", "game", {})
    assert "emoji_dominant" not in sgs.edge_flags("一般般 😀", "game", {})
    print("✓ 方言/emoji 主导识别 通过")


def test_edge_subset_of() -> None:
    assert sgs.edge_subset_of({"flags": {"irony_hint"}}) == {"irony"}
    assert sgs.edge_subset_of({"flags": {"slang", "dialect"}}) == {"jargon", "dialect"}
    assert sgs.edge_subset_of({"flags": set()}) == set()
    print("✓ 子集映射 通过")


def _build_pool() -> list[dict]:
    pool: list[dict] = []
    for ch in ("weibo", "websearch"):
        for i in range(30):
            pool.append(make_sample(f"d_{ch}_{i}", ch, "哩个嘢真係好正 唔该"))
    for ch in ("weibo", "xiaohongshu", "websearch"):
        for i in range(20):
            pool.append(make_sample(f"i_{ch}_{i}", ch, "呵呵 太棒了 真是谢谢"))
    for ch in ("weibo", "xiaohongshu", "websearch"):
        for i in range(20):
            pool.append(make_sample(f"j_{ch}_{i}", ch, "yyds 绝绝子 绷不住"))
    for ch in ("weibo", "xiaohongshu", "websearch"):
        for i in range(20):
            pool.append(make_sample(f"e_{ch}_{i}", ch, "😍😍😍 太好看了 真的"))
    long_text = "这游戏剧情" + "非常精彩" * 30
    for ch in ("weibo", "websearch", "bilibili"):
        for i in range(20):
            pool.append(make_sample(f"l_{ch}_{i}", ch, long_text))
    return pool


def test_sample_edge_quotas_and_disjoint() -> None:
    pool = _build_pool()
    selected, stats = sgs.sample_edge(pool, random.Random(42), conf_fn=lambda t: 0.7)
    assert len(selected) == 300, len(selected)
    cnt = Counter(s["edge_subset"] for s in selected)
    for sub in ("dialect", "irony", "jargon", "emoji", "long", "lowconf"):
        assert cnt[sub] == 50, (sub, cnt[sub])
    ids = [s["text_id"] for s in selected]
    assert len(ids) == len(set(ids)), "跨子集不允许重复样本"
    d = [s for s in selected if s["edge_subset"] == "dialect"]
    assert Counter(s["platform"] for s in d) == {"weibo": 25, "websearch": 25}
    assert all(st["shortfall"] == 0 for st in stats.values())
    print("✓ 渠道配额 + 跨子集去重 + 300 条总量 通过")


def test_sample_edge_deterministic() -> None:
    pool = _build_pool()
    a, _ = sgs.sample_edge(pool, random.Random(7), conf_fn=lambda t: 0.7)
    b, _ = sgs.sample_edge(pool, random.Random(7), conf_fn=lambda t: 0.7)
    assert [s["text_id"] for s in a] == [s["text_id"] for s in b]
    print("✓ 固定种子可复现 通过")


def test_sample_edge_shortfall() -> None:
    pool = [make_sample("d0", "weibo", "哩个嘢真係好正")]
    selected, stats = sgs.sample_edge(pool, random.Random(1), conf_fn=lambda t: 0.7)
    assert stats["dialect"]["achieved"] == 1
    assert stats["dialect"]["shortfall"] == 49
    assert selected and selected[0]["edge_subset"] == "dialect"
    print("✓ 样本池不足短报 通过")


def test_lowconf_range() -> None:
    pool = [make_sample(f"c{i}", "weibo", f"这是一段用于测试的文本{i}") for i in range(10)]

    def conf(text: str) -> float:
        return int(text[-1]) / 10  # c6/c7/c8/c9 → 0.6~0.9 在区间内

    selected, stats = sgs.sample_edge(pool, random.Random(3), conf_fn=conf)
    low = [s for s in selected if s["edge_subset"] == "lowconf"]
    assert stats["lowconf"]["achieved"] == 4, stats["lowconf"]
    assert {s["text_id"] for s in low} == {"c6", "c7", "c8", "c9"}
    print("✓ 低置信得分区间 [0.55, 0.95] 通过")


def test_edge_row_column_contract(tmp_path) -> None:
    """标注表列契约：情感/强度列为空（独立标注），子集列填抽样偏置组。

    2026-08-13 曾发现 _edge_row 把子集标签误写入「情感(整条)」列（诱导标注），
    此测试为防回归契约。
    """
    headers = sgs._base_columns() + ["子集"] + ["角色偏好", "剧情评价"] + \
        ["语言现象", "是否相关", "备注", "标注人", "标注日期"]
    s = make_sample("e1", "weibo", "呵呵 太棒了")
    s["edge_subset"] = "irony"
    row = sgs._edge_row(1, s, ["角色偏好", "剧情评价"])
    assert len(row) == len(headers), (len(row), len(headers))
    assert row[headers.index("情感(整条)")] == ""
    assert row[headers.index("强度(1-5)")] == ""
    assert row[headers.index("子集")] == "反讽"
    for h in ("角色偏好", "剧情评价", "语言现象", "是否相关", "备注", "标注人", "标注日期"):
        assert row[headers.index(h)] == ""

    import csv as _csv

    out = tmp_path / "edge_col_contract.csv"
    sgs.write_edge_csv([s], headers, ["角色偏好", "剧情评价"], out)
    with open(out, encoding="utf-8-sig", newline="") as f:
        r = next(_csv.DictReader(f))
    assert r["情感(整条)"] == "" and r["强度(1-5)"] == ""
    assert r["子集"] == "反讽"
    print("✓ 标注表列契约（情感/强度留空、子集预填）通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path

        test_flags_dialect_and_emoji()
        test_edge_subset_of()
        test_sample_edge_quotas_and_disjoint()
        test_sample_edge_deterministic()
        test_sample_edge_shortfall()
        test_lowconf_range()
        test_edge_row_column_contract(Path(td))
    print("边界样本专项集抽样测试全部通过 ✅")


if __name__ == "__main__":
    main()
