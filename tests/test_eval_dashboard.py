# -*- coding: utf-8 -*-
"""评测仪表盘冒烟测试（2.1）：空历史与有数据两种状态，Streamlit AppTest 无浏览器。

运行：python tests/test_eval_dashboard.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sms_eval_dash_"))
os.environ["SMS_BENCHMARK_DIR"] = str(_TMP / "benchmark")

from streamlit.testing.v1 import AppTest  # noqa: E402

from app.core import eval_store  # noqa: E402

DASH = ROOT / "app" / "eval_dashboard.py"


def _report(ts: str, accuracy: float, mode: str = "lexicon") -> dict:
    edge = mode.startswith("edge_")
    return {
        "generated_at": ts,
        "mode_key": mode,
        "mode": mode,
        "golden": ("tests/fixtures/edge_set_v1.csv" if edge
                   else "tests/fixtures/golden_set_v1.csv"),
        "golden_fingerprint": ("sha256:edge_dash|rows=297" if edge
                               else "sha256:dash|rows=3"),
        "golden_rows": 3,
        "scope": {"main_n": 3, "irrelevant_excluded_n": 0},
        "sentiment_accuracy": accuracy,
        "by_channel": {"weibo": {"n": 2, "accuracy": 0.5}},
        "by_domain": {"game": {"n": 2, "accuracy": 0.5}},
        "by_kind": {"post": {"n": 2, "accuracy": 0.5}},
        "by_flag": {},
        "by_gold_sentiment": {
            "positive": {"n": 1, "accuracy": 1.0},
            "negative": {"n": 1, "accuracy": 0.0},
        },
        "by_subset": ({"irony": {"n": 2, "accuracy": 0.5},
                       "lowconf": {"n": 1, "accuracy": 1.0}} if edge else {}),
        "calibration": ({"0.8-0.9": {"n": 2, "accuracy": 1.0}} if edge else {}),
        "by_sentiment_class": {
            "positive": {"n_gold": 1, "n_pred": 1, "precision": 1.0,
                         "recall": 1.0, "f1": 1.0},
            "negative": {"n_gold": 1, "n_pred": 1, "precision": 0.0,
                         "recall": 0.0, "f1": 0.0},
            "neutral": {"n_gold": 1, "n_pred": 1, "precision": 1.0,
                        "recall": 1.0, "f1": 1.0},
        },
        "confusion_matrix": {"positive->positive": 1, "positive->negative": 1},
        "routing": {"direct_rate": 0.5},
        "dimension": {"micro_f1": 0.2},
        "errors": [{"text_id": "x1", "text": "原文", "gold": "negative",
                    "pred": "positive", "platform": "weibo", "domain": "game",
                    "kind": "post", "flags": "",
                    "subset": "irony" if edge else "", "llm_used": False}],
    }


def _seed() -> None:
    rd = Path(os.environ["SMS_BENCHMARK_DIR"]) / "runs"
    hf = Path(os.environ["SMS_BENCHMARK_DIR"]) / "history.jsonl"
    eval_store.write_run(_report("2026-08-10T10:00:00", 0.35),
                         runs_dir_path=rd, history_file=hf)
    eval_store.write_run(_report("2026-08-10T11:00:00", 0.40),
                         runs_dir_path=rd, history_file=hf)


def test_dashboard_empty_state() -> None:
    at = AppTest.from_file(str(DASH), default_timeout=30).run()
    assert not at.exception, [str(e) for e in at.exception]
    assert at.title[0].value == "🎯 黄金集评测中心"
    assert any("暂无评测历史" in (i.value or "") for i in at.info)
    print("✓ 空历史状态 通过")


def test_dashboard_with_data() -> None:
    _seed()
    at = AppTest.from_file(str(DASH), default_timeout=30).run()
    assert not at.exception, [str(e) for e in at.exception]
    assert at.title[0].value == "🎯 黄金集评测中心"
    assert len(at.metric) >= 6, f"概览指标不足：{len(at.metric)}"
    assert len(at.dataframe) >= 2, "应有细分表/历史表等数据表"
    text_all = " ".join(str(getattr(w, "value", "")) for w in at.dataframe)
    assert "weibo" in text_all
    tabs = getattr(at, "tabs", None)
    if tabs is not None:
        assert len(tabs) == 7, f"应含 7 个页签（含「关键词策略配置」），实际 {len(tabs)}"
    print("✓ 有数据状态（概览/表/趋势/历史） 通过")


def test_dashboard_edge_mode() -> None:
    """边界集模式：模式选择可用、子集分组渲染、错误下钻含 subset。"""
    # 用独立目录，避免污染其他用例
    edge_dir = Path(tempfile.mkdtemp(prefix="sms_dash_edge_")) / "benchmark"
    rd = edge_dir / "runs"
    hf = edge_dir / "history.jsonl"
    eval_store.write_run(_report("2026-08-14T00:00:00", 0.8, mode="edge_hybrid"),
                         runs_dir_path=rd, history_file=hf)
    eval_store.write_run(_report("2026-08-14T01:00:00", 0.82, mode="edge_hybrid"),
                         runs_dir_path=rd, history_file=hf)
    old = os.environ.get("SMS_BENCHMARK_DIR")
    os.environ["SMS_BENCHMARK_DIR"] = str(edge_dir)
    at = AppTest.from_file(str(DASH), default_timeout=30).run()
    try:
        assert not at.exception, [str(e) for e in at.exception]
        assert at.radio and "边界集·混合流水线（词典+LLM）" in (at.radio[0].options or [])
        at.radio[0].set_value("边界集·混合流水线（词典+LLM）").run()
        assert not at.exception, [str(e) for e in at.exception]
        text_all = " ".join(str(getattr(w, "value", "")) for w in at.dataframe)
        assert "irony" in text_all, "子集分组应渲染 irony"
    finally:
        if old is None:
            os.environ.pop("SMS_BENCHMARK_DIR", None)
        else:
            os.environ["SMS_BENCHMARK_DIR"] = old
    print("✓ 边界集模式（模式选择/子集分组/错误下钻）通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_dashboard_empty_state()
    test_dashboard_with_data()
    test_dashboard_edge_mode()
    print("评测仪表盘冒烟测试全部通过 ✅")


if __name__ == "__main__":
    main()
