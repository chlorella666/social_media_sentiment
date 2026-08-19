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
        "dimension": {
            "micro_f1": 0.2,
            "exact_match_rate": 0.4,
            "dimension_n": 5,
            "per_dimension": {
                "产品质量": {"n_gold": 3, "precision": 0.8, "recall": 0.6, "f1": 0.69},
                "价格价值": {"n_gold": 2, "precision": 0.4, "recall": 0.4, "f1": 0.4},
            },
            "dimension_sentiment_dist": {
                "产品质量": {"gold_positive": 2, "gold_negative": 1,
                             "pred_positive": 2, "pred_negative": 0},
            },
            "mention": {"micro_f1": 0.5},
        },
        "dimension_errors": [
            {"text_id": "d1", "text": "维度错误原文", "gold": {"产品质量": "negative"},
             "pred": {"产品质量": "positive"}, "platform": "weibo", "domain": "game"},
        ],
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
        labels = [t.label for t in tabs]
        for want in ("📊 概览", "🧩 维度情感", "🎛 关键词策略配置", "🕘 运行历史"):
            assert want in labels, f"缺页签 {want}：{labels}"
    subs = [s.value for s in at.subheader]
    assert any("维度级情感" in s for s in subs), "维度情感视图未渲染"
    print("✓ 有数据状态（概览/表/趋势/维度情感/历史） 通过")


def test_dashboard_module_mode() -> None:
    """P1-1：模块模式从 history 动态发现并可选。"""
    mod_dir = Path(tempfile.mkdtemp(prefix="sms_dash_mod_")) / "benchmark"
    rd = mod_dir / "runs"
    hf = mod_dir / "history.jsonl"
    eval_store.write_run(_report("2026-08-17T10:00:00", 0.85, mode="module_content_hybrid"),
                         runs_dir_path=rd, history_file=hf)
    eval_store.write_run(_report("2026-08-17T11:00:00", 0.87, mode="module_content_hybrid"),
                         runs_dir_path=rd, history_file=hf)
    old = os.environ.get("SMS_BENCHMARK_DIR")
    os.environ["SMS_BENCHMARK_DIR"] = str(mod_dir)
    try:
        at = AppTest.from_file(str(DASH), default_timeout=30).run()
        assert not at.exception, [str(e) for e in at.exception]
        opts = at.radio(key="eval_mode").options or []
        assert "数字产品·混合流水线" in opts, f"模块模式不可见: {opts}"
        at.radio(key="eval_mode").set_value("数字产品·混合流水线").run()
        assert not at.exception, [str(e) for e in at.exception]
        subs = [s.value for s in at.subheader]
        assert any("维度级情感" in s for s in subs), "模块模式维度情感视图未渲染"
    finally:
        if old is None:
            os.environ.pop("SMS_BENCHMARK_DIR", None)
        else:
            os.environ["SMS_BENCHMARK_DIR"] = old
    print("✓ 模块模式（动态发现/可选/维度情感渲染）通过")


def test_helpers() -> None:
    import app.eval_dashboard as d

    assert d._flags_contain("反讽,黑话", ["反讽"])
    assert not d._flags_contain("反讽,黑话", ["方言"])
    assert d._flags_contain("", [])
    opts = d._mode_options([
        {"mode_key": "module_content_hybrid"},
        {"mode_key": "hybrid"},
        {"mode_key": "module_content_hybrid"},
    ])
    assert opts == ["lexicon", "hybrid", "edge_lexicon", "edge_hybrid",
                    "module_content_hybrid"]
    assert d._mode_label("module_content_hybrid") == "数字产品·混合流水线"
    assert d._mode_label("holdout_digital3c_r4_hybrid") == "3C·hold-out r4·混合"
    assert d._mode_label("未知_mode_key") == "未知·mode·key"
    assert d._is_holdout_mode("holdout_digital3c_r4_hybrid")
    assert d._is_holdout_mode("module_service_holdout_hybrid")
    assert not d._is_holdout_mode("module_content_hybrid")
    assert d._is_lexicon_gate("lexicon") and d._is_lexicon_gate("edge_lexicon")
    assert not d._is_lexicon_gate("hybrid")
    assert d._mode_option_label("lexicon").startswith("门槛")
    assert "留出卷（≤2 次）" in d._mode_option_label("holdout_digital3c_r4_hybrid")
    assert d._mode_option_label("module_content_hybrid") == "数字产品·混合流水线"
    opts2 = d._mode_options([
        {"mode_key": "holdout_digital3c_r4_hybrid"},
        {"mode_key": "module_content_hybrid"},
    ])
    assert (opts2.index("module_content_hybrid")
            < opts2.index("holdout_digital3c_r4_hybrid")), "hold-out 卷应归组到末尾"
    print("✓ helper（by_flag 拆分匹配 / 模式动态发现与标签）通过")


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
        assert (at.radio
                and "边界集·混合流水线（词典+LLM）" in (at.radio(key="eval_mode").options or []))
        at.radio(key="eval_mode").set_value("边界集·混合流水线（词典+LLM）").run()
        assert not at.exception, [str(e) for e in at.exception]
        text_all = " ".join(str(getattr(w, "value", "")) for w in at.dataframe)
        assert "irony" in text_all, "子集分组应渲染 irony"
    finally:
        if old is None:
            os.environ.pop("SMS_BENCHMARK_DIR", None)
        else:
            os.environ["SMS_BENCHMARK_DIR"] = old
    print("✓ 边界集模式（模式选择/子集分组/错误下钻）通过")


def test_change_type_recommend_and_apply() -> None:
    """P2-7：按改动类型推荐数据集组合，hold-out/门槛标注，一键应用切换模式。"""
    reco_dir = Path(tempfile.mkdtemp(prefix="sms_dash_reco_")) / "benchmark"
    rd = reco_dir / "runs"
    hf = reco_dir / "history.jsonl"
    eval_store.write_run(_report("2026-08-17T10:00:00", 0.85, mode="module_content_hybrid"),
                         runs_dir_path=rd, history_file=hf)
    eval_store.write_run(_report("2026-08-17T11:00:00", 0.87, mode="module_content_hybrid"),
                         runs_dir_path=rd, history_file=hf)
    old = os.environ.get("SMS_BENCHMARK_DIR")
    os.environ["SMS_BENCHMARK_DIR"] = str(reco_dir)
    try:
        at = AppTest.from_file(str(DASH), default_timeout=30).run()
        assert not at.exception, [str(e) for e in at.exception]
        # 默认「通用规则」：推荐组合含主集 hybrid 与三模块卷
        reco_ms = at.multiselect(key="eval_reco_通用规则")
        assert "混合流水线（词典+LLM）" in reco_ms.options
        assert "数字产品·混合流水线" in reco_ms.options
        # 一键应用 → 当前模式切到第一个推荐项（hybrid）
        at.button(key="eval_reco_apply").click().run()
        assert not at.exception, [str(e) for e in at.exception]
        assert at.radio(key="eval_mode").value == "hybrid"
        # 切「模块级」→ 推荐组合换成模块卷 + 模块 hold-out（分组标注）
        at.radio(key="eval_change_type").set_value("模块级").run()
        assert not at.exception, [str(e) for e in at.exception]
        labels = list(at.multiselect(key="eval_reco_模块级").options)
        assert "有形实物·混合流水线" in labels
        assert any("留出卷（≤2 次）" in x for x in labels), labels
        # 一键应用模块级 → 模式切到 module_content_hybrid（有历史，正常渲染）
        at.button(key="eval_reco_apply").click().run()
        assert not at.exception, [str(e) for e in at.exception]
        assert at.radio(key="eval_mode").value == "module_content_hybrid"
        subs = [s.value for s in at.subheader]
        assert any("维度级情感" in s for s in subs), "应用后模块模式应正常渲染"
    finally:
        if old is None:
            os.environ.pop("SMS_BENCHMARK_DIR", None)
        else:
            os.environ["SMS_BENCHMARK_DIR"] = old
    print("✓ 改动类型引导（推荐组合/分组标注/一键应用）通过")


def test_change_type_collection_hint() -> None:
    """P2-7：采集清洗类不勾选数据集，仅提示看关键词效果。"""
    _seed()
    at = AppTest.from_file(str(DASH), default_timeout=30).run()
    assert not at.exception, [str(e) for e in at.exception]
    at.radio(key="eval_change_type").set_value("采集清洗").run()
    assert not at.exception, [str(e) for e in at.exception]
    assert not any(m.key == "eval_reco_采集清洗" for m in at.multiselect), \
        "采集清洗类不应展示数据集多选"
    captions = " ".join(getattr(w, "value", "") for w in at.caption)
    assert "不跑准确率" in captions
    print("✓ 改动类型引导（采集清洗提示）通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_dashboard_empty_state()
    test_dashboard_with_data()
    test_dashboard_edge_mode()
    test_dashboard_module_mode()
    test_change_type_recommend_and_apply()
    test_change_type_collection_hint()
    test_helpers()
    print("评测仪表盘冒烟测试全部通过 ✅")


if __name__ == "__main__":
    main()
