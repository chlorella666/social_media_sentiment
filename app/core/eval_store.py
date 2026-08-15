# -*- coding: utf-8 -*-
"""黄金集评测数据层（阶段 2.1）：历史归档 + 自动对比 + 冻结基线。

职责：
- runs/<时间戳>/report.json：每次评测完整明细（含细分/错误样本/对比结果）；
- history.jsonl：追加式摘要（仪表盘趋势与对比的输入，schema 带版本号）；
- golden 指纹守卫：黄金集内容变更后禁止跨版本直接对比；
- 对比数学：Δpp、状态（改善/持平/回退/噪声内）、小样本（n<30）自动标"参考"；
  V3 噪声区间（2.6）：细分/整体带 95% Wilson CI；Δ 判定公式 = √(ci₁²+ci₂²)，
  |Δ| 超出该宽度才标改善/回退，否则「噪声内（±X pp）」；n<30 保持点估计口径；
- 冻结基线：tests/fixtures/baseline_lexicon.json（词典）/
  baseline_hybrid.json（混合）。

目录可通过环境变量 SMS_BENCHMARK_DIR 覆盖（测试隔离用）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(
    os.environ.get("SMS_FIXTURES_DIR") or (ROOT / "tests" / "fixtures")
)

SCHEMA_VERSION = 1

# 细分样本数低于该值时仅作参考（小样本噪声，不作判断依据）
REF_N = 30
# |Δpp| < 该值视为"持平"（与 1.6 回归容差 1pp 口径一致）
DELTA_PP_THRESHOLD = 1.0
# V3 噪声区间（2026-08-15）：95% Wilson 区间 + Δ 判定宽度
Z95 = 1.96

SEGMENT_GROUPS = (
    "by_channel",
    "by_domain",
    "by_kind",
    "by_flag",
    "by_gold_sentiment",
    "by_subset",
)


def _benchmark_dir() -> Path:
    override = os.environ.get("SMS_BENCHMARK_DIR")
    return Path(override) if override else (ROOT / "data" / "benchmark")


def runs_dir() -> Path:
    return _benchmark_dir() / "runs"


def history_path() -> Path:
    return _benchmark_dir() / "history.jsonl"


def golden_fingerprint(path: str | Path) -> str:
    """golden 内容指纹：sha256 前 16 位 + csv 数据行数。

    黄金集内容或版本变更后指纹变化，仪表盘据此拦截跨版本对比。
    """
    p = Path(path)
    digest = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    rows = 0
    try:
        import csv

        with open(p, encoding="utf-8-sig") as f:
            rows = sum(1 for _ in csv.DictReader(f))
    except (OSError, csv.Error):
        rows = -1
    return f"sha256:{digest}|rows={rows}"


def wilson_ci(p: float, n: int, z: float = Z95) -> tuple[float | None, float | None]:
    """Wilson score interval（比例 0~1）。n<=0 返回 (None, None)。

    小样本下比正态近似更稳（两端不越界）；边界值 p=0/1 时区间仍有效。
    """
    if n <= 0:
        return (None, None)
    p = max(0.0, min(1.0, float(p)))
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def ci_half_pp(acc: float | None, n: int, z: float = Z95) -> float | None:
    """95% CI 半宽（百分点），取 Wilson 区间两侧中较大者（保守）。

    用于单次估计的报告标注；Δ 判定宽度由 delta_ci_pp 组合两侧。
    """
    if acc is None or n <= 0:
        return None
    lo, hi = wilson_ci(acc, n, z)
    acc_pp = float(acc) * 100
    return round(max(acc_pp - lo * 100, hi * 100 - acc_pp), 2)


def delta_ci_pp(ci_cur: float | None, ci_prev: float | None) -> float | None:
    """Δ 的 95% CI 宽度（百分点）：√(ci_cur² + ci_prev²)。"""
    if ci_cur is None or ci_prev is None:
        return None
    return round(math.hypot(ci_cur, ci_prev), 2)


def _status(
    delta_pp: float | None,
    n_cur: int | None = None,
    n_prev: int | None = None,
    ci_cur: float | None = None,
    ci_prev: float | None = None,
) -> str:
    """对比状态：n<30 保持点估计口径（改善/持平/回退）；
    两侧 n≥30 且 |Δ| 未超 CI 宽度 → 「噪声内（±X pp）」。
    """
    if delta_pp is None:
        return "新增"
    if abs(delta_pp) < DELTA_PP_THRESHOLD:
        return "持平"
    if (n_cur or 0) >= REF_N and (n_prev or 0) >= REF_N:
        width = delta_ci_pp(ci_cur, ci_prev)
        if width is not None and abs(delta_pp) <= width:
            return f"噪声内(±{width:.1f}pp)"
    return "改善" if delta_pp > 0 else "回退"


def build_summary(report: dict) -> dict:
    """从完整评测报告中抽取历史摘要（仪表盘/对比的输入）。"""
    scope = report.get("scope") or {}
    summary = {
        "schema": SCHEMA_VERSION,
        "ts": report.get("generated_at") or datetime.now().isoformat(timespec="seconds"),
        "mode_key": report.get("mode_key", "lexicon"),
        "mode": report.get("mode", ""),
        "golden": report.get("golden", ""),
        "golden_fp": report.get("golden_fingerprint", ""),
        "n_main": scope.get("main_n") if scope.get("main_n") is not None else report.get("golden_rows"),
        "n_irrelevant": scope.get("irrelevant_excluded_n", 0),
        "accuracy": report.get("sentiment_accuracy"),
        "groups": {g: report.get(g, {}) for g in SEGMENT_GROUPS},
        "by_sentiment_class": report.get("by_sentiment_class", {}),
        "confusion_matrix": report.get("confusion_matrix", {}),
        "routing": report.get("routing", {}),
        "dimension": report.get("dimension", {}),
        # 2.4 口径标记：sentiment = 维度级情感（新）；mention = 提及识别（2.4 之前）
        "dimension_metric": ("sentiment" if report.get("dimension", {}).get("mention") else "mention"),
        "llm_usage": report.get("llm_usage"),
        "summary_only": bool(report.get("summary_only")),
        "note": report.get("note"),
    }
    return summary


def compare_segments(cur: dict, prev: dict | None) -> dict:
    """逐细分对比：Δpp + 状态 + 小样本参考标记。"""
    prev = prev or {}
    out: dict = {}
    for group in SEGMENT_GROUPS:
        cur_g = (cur.get("groups") or {}).get(group, {})
        prev_g = (prev.get("groups") or {}).get(group, {})
        seg_out: dict = {}
        for key, v in sorted(cur_g.items()):
            pv = prev_g.get(key)
            delta = None
            if pv and v.get("accuracy") is not None and pv.get("accuracy") is not None:
                delta = round((v["accuracy"] - pv["accuracy"]) * 100, 2)
            ci_cur = ci_half_pp(v.get("accuracy"), v.get("n") or 0)
            ci_prev = ci_half_pp(pv.get("accuracy"), pv.get("n") or 0) if pv else None
            seg_out[key] = {
                "n": v.get("n"),
                "accuracy": v.get("accuracy"),
                "prev_accuracy": pv.get("accuracy") if pv else None,
                "delta_pp": delta,
                "ci_pp": ci_cur,
                "ci_prev_pp": ci_prev,
                "delta_ci_pp": delta_ci_pp(ci_cur, ci_prev),
                "status": (
                    _status(delta, v.get("n"), pv.get("n"), ci_cur, ci_prev)
                    if pv else "新增"
                ),
                "ref": (v.get("n") or 0) < REF_N,
            }
        out[group] = seg_out
    return out


def compare_runs(cur: dict, prev: dict | None) -> dict:
    """整体 + 细分对比（cur 为本次、prev 为上次，均为摘要结构）。"""
    prev = prev or {}
    cur_acc = cur.get("accuracy")
    prev_acc = prev.get("accuracy")
    delta = None
    if cur_acc is not None and prev_acc is not None:
        delta = round((cur_acc - prev_acc) * 100, 2)
    ci_cur = ci_half_pp(cur_acc, cur.get("n_main") or 0)
    ci_prev = ci_half_pp(prev_acc, prev.get("n_main") or 0) if prev else None
    return {
        "prev_run_id": prev.get("run_id"),
        "prev_ts": prev.get("ts"),
        "prev_summary_only": bool(prev.get("summary_only")),
        "overall_delta_pp": delta,
        "overall_ci_pp": ci_cur,
        "overall_ci_prev_pp": ci_prev,
        "overall_delta_ci_pp": delta_ci_pp(ci_cur, ci_prev),
        "status": (
            _status(delta, cur.get("n_main"), prev.get("n_main"), ci_cur, ci_prev)
            if prev else "新增"
        ),
        "segments": compare_segments(cur, prev),
    }


def compare_baseline(cur: dict, baseline: dict | None) -> dict:
    """与冻结基线对比（整体准确率口径）。

    指纹守卫（2026-08-14 补）：基线文件与本次运行都带 golden 指纹时，指纹不一致
    说明数据集内容已变更（如 edge_set 升级），对比无效——直接提示重冻结基线，
    避免"跨版本对比"产生误导。
    """
    if not baseline:
        return {"baseline": None, "note": "无冻结基线文件"}
    base_acc = baseline.get("accuracy")
    cur_acc = cur.get("accuracy")
    base_fp = baseline.get("golden_fingerprint")
    cur_fp = cur.get("golden_fp")
    if base_fp and cur_fp and base_fp != cur_fp:
        return {
            "baseline": base_acc,
            "baseline_file": baseline.get("_source"),
            "delta_pp": None,
            "status": "指纹不匹配",
            "note": "数据集内容已变更（基线指纹≠本次运行指纹），对比无效；请重冻结基线",
        }
    delta = None
    if cur_acc is not None and base_acc is not None:
        delta = round((cur_acc - base_acc) * 100, 2)
    n_cur = cur.get("n_main")
    n_base = baseline.get("n")
    ci_cur = ci_half_pp(cur_acc, n_cur or 0)
    ci_base = ci_half_pp(base_acc, n_base or 0)
    return {
        "baseline": base_acc,
        "baseline_file": baseline.get("_source"),
        "delta_pp": delta,
        "ci_pp": ci_cur,
        "ci_prev_pp": ci_base,
        "delta_ci_pp": delta_ci_pp(ci_cur, ci_base),
        "status": (
            _status(delta, n_cur, n_base, ci_cur, ci_base)
            if delta is not None else "不可比"
        ),
        "n": n_cur,
    }


def find_previous(cur: dict, history: list[dict]) -> dict | None:
    """同一模式 + 同一 golden 指纹下的上一跑（按 ts/run_id 排序）。"""
    mode = cur.get("mode_key")
    fp = cur.get("golden_fp")
    cur_ts = cur.get("ts") or ""
    same = [
        h for h in history
        if h.get("mode_key") == mode and h.get("golden_fp") == fp
    ]
    same.sort(key=lambda h: (h.get("ts") or "", h.get("run_id") or ""))
    before = [h for h in same if (h.get("ts") or "") < cur_ts or
              ((h.get("ts") or "") == cur_ts and (h.get("run_id") or "") < (cur.get("run_id") or ""))]
    return before[-1] if before else None


def load_frozen_baseline(mode_key: str) -> dict | None:
    name = {
        "hybrid": "baseline_hybrid.json",
        "lexicon": "baseline_lexicon.json",
        "edge_hybrid": "baseline_edge_hybrid.json",
        "edge_lexicon": "baseline_edge_lexicon.json",
    }.get(mode_key) or f"baseline_{mode_key}.json"
    # 自定义 mode_key（2.5 新领域）→ 独立的 baseline_{mode_key}.json；
    # 不存在则无冻结基线（仅记录），绝不回落到主集基线造成误对比。
    p = FIXTURES_DIR / name
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data["_source"] = str(p)
        return data
    except (OSError, ValueError):
        return None


def _run_id_from_ts(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%Y%m%d_%H%M%S")
    except ValueError:
        return datetime.now().strftime("%Y%m%d_%H%M%S")


def append_history(summary: dict, history_file: Path | None = None) -> None:
    """追加摘要；失败仅告警，不阻塞评测本身。"""
    target = history_file or history_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[eval_store] 历史写入失败（不影响评测）：{exc}", file=sys.stderr)


def write_run(
    report: dict,
    previous: dict | None = None,
    baseline: dict | None = None,
    runs_dir_path: Path | None = None,
    history_file: Path | None = None,
) -> dict:
    """落完整明细 + 自动对比 + 追加历史摘要。返回 run_id/摘要/对比。"""
    summary = build_summary(report)
    comparison: dict = {}
    if previous is not None:
        comparison["vs_previous"] = compare_runs(summary, previous)
    if baseline is not None:
        comparison["vs_baseline"] = compare_baseline(summary, baseline)

    run_id = _run_id_from_ts(summary["ts"])
    rd = runs_dir_path or runs_dir()
    run_dir = rd / run_id
    suffix = 2
    while run_dir.exists():
        run_dir = rd / f"{run_id}-{suffix}"
        suffix += 1

    stored = dict(report)
    stored["run_id"] = run_id
    stored["comparison"] = comparison
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(
        json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if comparison:
        (run_dir / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    summary["run_id"] = run_id
    append_history(summary, history_file)
    return {"run_id": run_id, "run_dir": run_dir, "summary": summary,
            "comparison": comparison}


def load_history(history_file: Path | None = None) -> list[dict]:
    """读取历史摘要；跳过损坏行（告警）。"""
    p = history_file or history_path()
    out: list[dict] = []
    if not p.exists():
        return out
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except ValueError as exc:
            print(f"[eval_store] 历史行解析失败已跳过：{exc}", file=sys.stderr)
    return out


def load_run(run_id: str, runs_dir_path: Path | None = None) -> dict | None:
    rd = runs_dir_path or runs_dir()
    p = rd / run_id / "report.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def seed_history(history_file: Path | None = None) -> int:
    """从冻结基线回填摘要级历史条目（仅整体指标，幂等）。"""
    target = history_file or history_path()
    existing = {h.get("run_id") for h in load_history(target)}
    added = 0
    for fixture in ("baseline_lexicon.json", "baseline_hybrid.json"):
        p = FIXTURES_DIR / fixture
        if not p.exists():
            continue
        try:
            b = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        mode_key = b.get("mode_key") or (
            "lexicon" if "lexicon" in fixture else "hybrid"
        )
        run_id = f"baseline_v1.1_{mode_key}"
        if run_id in existing:
            continue
        fp = ""
        golden_rel = b.get("golden") or "tests/fixtures/golden_set_v1.csv"
        golden_abs = ROOT / golden_rel
        if golden_abs.exists():
            fp = golden_fingerprint(golden_abs)
        entry = {
            "schema": SCHEMA_VERSION,
            "run_id": run_id,
            "ts": b.get("created_at") or "2026-08-07T00:00:00+08:00",
            "mode_key": mode_key,
            "mode": b.get("mode") or mode_key,
            "golden": golden_rel,
            "golden_fp": fp,
            "n_main": b.get("n"),
            "n_irrelevant": 0,
            "accuracy": b.get("accuracy"),
            "groups": {g: {} for g in SEGMENT_GROUPS},
            "by_sentiment_class": {},
            "confusion_matrix": {},
            "routing": {},
            "dimension": {},
            "llm_usage": None,
            "summary_only": True,
            "note": "冻结基线回填（仅整体指标，无细分）",
        }
        append_history(entry, target)
        existing.add(run_id)
        added += 1
    return added


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import argparse

    ap = argparse.ArgumentParser(description="评测历史数据层工具")
    ap.add_argument("--seed-history", action="store_true",
                    help="从冻结基线回填摘要级历史（幂等）")
    ap.add_argument("--list", action="store_true", help="列出历史摘要")
    args = ap.parse_args()
    if args.seed_history:
        n = seed_history()
        print(f"已回填 {n} 条冻结基线历史")
    if args.list:
        for h in load_history():
            print(h.get("ts"), h.get("mode_key"), h.get("run_id"),
                  h.get("accuracy"), "summary_only" if h.get("summary_only") else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
