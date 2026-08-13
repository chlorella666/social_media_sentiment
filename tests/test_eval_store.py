# -*- coding: utf-8 -*-
"""评测数据层测试（2.1）：历史归档 / golden 指纹 / 对比数学 / seed。

运行：python tests/test_eval_store.py
覆盖：指纹确定性、run 落盘与历史追加、上一跑查找（同模式同指纹）、
      细分 Δpp/状态/小样本参考、整体对比、冻结基线对比、seed 幂等、
      情感类别 P/R/F1 计算正确性（手工验算）。
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

_TMP = Path(tempfile.mkdtemp(prefix="sms_eval_store_"))
os.environ["SMS_BENCHMARK_DIR"] = str(_TMP / "benchmark")
_TMP_FIX = _TMP / "fixtures"
_TMP_FIX.mkdir()
os.environ["SMS_FIXTURES_DIR"] = str(_TMP_FIX)

from app.core import eval_store  # noqa: E402
from tests import benchmark_golden as bg  # noqa: E402


def make_report(ts: str, accuracy: float, mode: str = "lexicon",
                fp: str = "sha256:test|rows=3") -> dict:
    return {
        "generated_at": ts,
        "mode_key": mode,
        "mode": "词典直判（无 LLM）" if mode == "lexicon" else "混合流水线（词典+LLM）",
        "golden": "tests/fixtures/golden_set_v1.csv",
        "golden_fingerprint": fp,
        "golden_rows": 3,
        "scope": {"main_n": 3, "irrelevant_excluded_n": 0},
        "sentiment_accuracy": accuracy,
        "by_channel": {"a": {"n": 2, "accuracy": 0.5}, "b": {"n": 1, "accuracy": 1.0}},
        "by_domain": {},
        "by_kind": {},
        "by_flag": {},
        "by_gold_sentiment": {
            "positive": {"n": 2, "accuracy": 0.5},
            "negative": {"n": 1, "accuracy": 1.0},
        },
        "by_sentiment_class": {},
        "confusion_matrix": {"positive->positive": 2},
        "routing": {"direct_rate": 0.1},
        "dimension": {"micro_f1": 0.2},
        "errors": [{"text_id": "x1", "text": "原文", "gold": "positive",
                    "pred": "negative", "platform": "weibo", "domain": "game",
                    "kind": "post", "flags": "", "llm_used": False}],
    }


def test_golden_fingerprint_deterministic() -> None:
    p = Path(tempfile.mkdtemp(prefix="sms_fp_")) / "g.csv"
    p.write_text("id,sentiment\n1,positive\n2,negative\n", encoding="utf-8-sig")
    fp1 = eval_store.golden_fingerprint(p)
    fp2 = eval_store.golden_fingerprint(p)
    assert fp1 == fp2
    assert "rows=2" in fp1
    p.write_text("id,sentiment\n1,positive\n2,negative\n3,neutral\n", encoding="utf-8-sig")
    assert eval_store.golden_fingerprint(p) != fp1
    print("✓ golden 指纹确定性 + 内容变更可检测 通过")


def test_write_run_and_history() -> None:
    rd = _TMP / "benchmark" / "runs"
    hf = _TMP / "benchmark" / "history.jsonl"
    r1 = eval_store.write_run(make_report("2026-08-10T12:00:00", 0.35),
                              runs_dir_path=rd, history_file=hf)
    r2 = eval_store.write_run(make_report("2026-08-10T12:05:00", 0.36,
                                          fp="sha256:test|rows=3"),
                              runs_dir_path=rd, history_file=hf)
    assert r1["run_id"] != r2["run_id"]
    assert (rd / r1["run_id"] / "report.json").exists()
    stored = eval_store.load_run(r1["run_id"], rd)
    assert stored["run_id"] == r1["run_id"]
    assert "comparison" in stored
    hist = eval_store.load_history(hf)
    assert len(hist) == 2
    assert hist[0]["run_id"] == r1["run_id"]
    assert hist[0]["groups"]["by_channel"]["a"]["accuracy"] == 0.5
    print("✓ run 落盘 + 历史追加 + 回读 通过")


def test_find_previous() -> None:
    hf = _TMP / "benchmark" / "history_fp.jsonl"
    base = make_report("2026-08-10T12:00:00", 0.35)
    same = make_report("2026-08-10T12:10:00", 0.36)
    other_mode = make_report("2026-08-10T12:20:00", 0.78, mode="hybrid")
    other_fp = make_report("2026-08-10T12:30:00", 0.37, fp="sha256:other|rows=3")
    hist = [eval_store.build_summary(base), eval_store.build_summary(other_mode),
            eval_store.build_summary(other_fp)]
    cur = eval_store.build_summary(same)
    prev = eval_store.find_previous(cur, hist)
    assert prev is not None
    assert prev["ts"] == "2026-08-10T12:00:00"
    assert prev["mode_key"] == "lexicon"
    assert prev["golden_fp"] == "sha256:test|rows=3"
    print("✓ 上一跑查找（同模式同指纹）通过")


def test_compare_segments_and_runs() -> None:
    cur = eval_store.build_summary(make_report("2026-08-10T13:00:00", 0.40))
    prev = eval_store.build_summary(make_report("2026-08-10T12:00:00", 0.37))
    cmp = eval_store.compare_segments(cur, prev)
    ch = cmp["by_channel"]["a"]
    assert ch["delta_pp"] == 0.0 and ch["status"] == "持平"  # 0.50 -> 0.50
    b = cmp["by_channel"]["b"]
    assert b["ref"] is True  # n=1 < 30 → 仅作参考
    assert cmp["by_channel"]["b"]["delta_pp"] == 0.0  # 1.0 -> 1.0
    gs = cmp["by_gold_sentiment"]["positive"]
    assert gs["delta_pp"] == 0.0
    rc = eval_store.compare_runs(cur, prev)
    assert rc["overall_delta_pp"] == 3.0 and rc["status"] == "改善"
    assert rc["segments"]["by_channel"]["a"]["prev_accuracy"] == 0.5
    rc_new = eval_store.compare_runs(cur, None)
    assert rc_new["status"] == "新增" and rc_new["overall_delta_pp"] is None
    print("✓ 细分/整体对比数学 + 小样本参考 通过")


def test_compare_baseline() -> None:
    cur = eval_store.build_summary(make_report("2026-08-10T14:00:00", 0.3643))
    base = {"accuracy": 0.3543, "_source": "tests/fixtures/baseline_lexicon.json"}
    cb = eval_store.compare_baseline(cur, base)
    assert cb["delta_pp"] == 1.0 and cb["status"] == "改善"  # 恰好 +1pp → 改善
    cb2 = eval_store.compare_baseline(cur, None)
    assert cb2["baseline"] is None
    print("✓ 冻结基线对比 通过")


def test_compare_baseline_fingerprint_guard() -> None:
    """基线指纹与运行不一致必须拦截（防跨版本误导对比）。"""
    cur = eval_store.build_summary(make_report("2026-08-14T00:00:00", 0.8,
                                               fp="sha256:edge_v11|rows=297"))
    base = {"accuracy": 0.8182, "golden_fingerprint": "sha256:edge_v10|rows=297",
            "_source": "tests/fixtures/baseline_edge_hybrid.json"}
    cb = eval_store.compare_baseline(cur, base)
    assert cb["status"] == "指纹不匹配" and cb["delta_pp"] is None
    assert "重冻结基线" in cb["note"]
    base_ok = {"accuracy": 0.8182, "golden_fingerprint": "sha256:edge_v11|rows=297",
               "_source": "tests/fixtures/baseline_edge_hybrid.json"}
    cb_ok = eval_store.compare_baseline(cur, base_ok)
    assert cb_ok["delta_pp"] == -1.82 and cb_ok["status"] == "回退"
    # 旧基线无指纹字段 → 不拦截（向后兼容）
    base_legacy = {"accuracy": 0.3543, "_source": "tests/fixtures/baseline_lexicon.json"}
    cb_legacy = eval_store.compare_baseline(
        eval_store.build_summary(make_report("2026-08-14T00:00:00", 0.3643)), base_legacy)
    assert cb_legacy["status"] == "改善"
    print("✓ 冻结基线指纹守卫（拦截/兼容/正常对比）通过")


def test_seed_history_idempotent() -> None:
    hf = _TMP / "benchmark" / "seed.jsonl"
    (_TMP_FIX / "baseline_lexicon.json").write_text(
        '{"accuracy": 0.3543, "created_at": "2026-08-07T00:00:00"}',
        encoding="utf-8")
    (_TMP_FIX / "baseline_hybrid.json").write_text(
        '{"accuracy": 0.7829, "created_at": "2026-08-07T00:00:00"}',
        encoding="utf-8")
    n1 = eval_store.seed_history(hf)
    n2 = eval_store.seed_history(hf)
    assert n1 == 2 and n2 == 0
    hist = eval_store.load_history(hf)
    assert all(h["summary_only"] for h in hist)
    assert {h["mode_key"] for h in hist} == {"lexicon", "hybrid"}
    print("✓ 冻结基线 seed 幂等 通过")


def test_sentiment_class_metrics_hand_checked() -> None:
    preds = ["positive", "positive", "negative", "neutral"]
    golds = ["positive", "negative", "negative", "neutral"]
    m = bg.sentiment_class_metrics(preds, golds)
    assert m["positive"]["n_gold"] == 1 and m["positive"]["n_pred"] == 2
    assert m["positive"]["precision"] == 0.5 and m["positive"]["recall"] == 1.0
    assert m["positive"]["f1"] == round(2 * 0.5 * 1.0 / 1.5, 4)
    assert m["negative"]["precision"] == 1.0 and m["negative"]["recall"] == 0.5
    assert m["neutral"]["precision"] == 1.0 and m["neutral"]["recall"] == 1.0
    print("✓ 情感类别 P/R/F1 手工验算 通过")


def test_frozen_baseline_mode_mapping() -> None:
    """edge 模式必须映射到 baseline_edge_*.json（2.3 评测接入）。"""
    (_TMP_FIX / "baseline_lexicon.json").write_text(
        '{"accuracy": 0.3543, "mode": "lexicon"}', encoding="utf-8")
    (_TMP_FIX / "baseline_hybrid.json").write_text(
        '{"accuracy": 0.7829, "mode": "hybrid"}', encoding="utf-8")
    (_TMP_FIX / "baseline_edge_lexicon.json").write_text(
        '{"accuracy": 0.464, "mode": "edge_lexicon"}', encoding="utf-8")
    (_TMP_FIX / "baseline_edge_hybrid.json").write_text(
        '{"accuracy": 0.5, "mode": "edge_hybrid"}', encoding="utf-8")
    assert eval_store.load_frozen_baseline("lexicon")["accuracy"] == 0.3543
    assert eval_store.load_frozen_baseline("hybrid")["accuracy"] == 0.7829
    assert eval_store.load_frozen_baseline("edge_lexicon")["accuracy"] == 0.464
    assert eval_store.load_frozen_baseline("edge_hybrid")["accuracy"] == 0.5
    assert eval_store.load_frozen_baseline("unknown")["accuracy"] == 0.3543
    # by_subset 已纳入细分分组（edge 报告使用）
    assert "by_subset" in eval_store.SEGMENT_GROUPS
    print("✓ 冻结基线 mode_key 映射（含 edge）+ by_subset 分组 通过")


def test_calibration_report() -> None:
    """边界集置信度分桶校准：越有把握应越准；缺失置信度单独计数。"""
    rows = [{"sentiment": s} for s in ("positive", "negative", "neutral", "positive", "neutral")]
    preds = [
        {"sentiment": "positive", "confidence": 0.95},
        {"sentiment": "negative", "confidence": 0.62},
        {"sentiment": "negative", "confidence": 0.62},
        {"sentiment": "positive", "confidence": 0.95},
        {"sentiment": "neutral", "confidence": None},
    ]
    cal = bg.calibration_report(rows, preds)
    assert cal["0.9-1.0"]["n"] == 2 and cal["0.9-1.0"]["accuracy"] == 1.0
    assert cal["0.6-0.7"]["n"] == 2 and cal["0.6-0.7"]["accuracy"] == 0.5
    assert cal["缺失置信度"]["n"] == 1
    assert cal["0.0-0.5"]["n"] == 0 and cal["0.0-0.5"]["accuracy"] is None
    print("✓ 置信度分桶校准（含缺失置信度）通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_golden_fingerprint_deterministic()
    test_write_run_and_history()
    test_find_previous()
    test_compare_segments_and_runs()
    test_compare_baseline()
    test_compare_baseline_fingerprint_guard()
    test_seed_history_idempotent()
    test_sentiment_class_metrics_hand_checked()
    test_frozen_baseline_mode_mapping()
    test_calibration_report()
    print("评测数据层测试全部通过 ✅")


if __name__ == "__main__":
    main()
