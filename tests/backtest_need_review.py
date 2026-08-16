# -*- coding: utf-8 -*-
"""2.11 need_review 候选扩展回测：主集/迭代集上旧规则(v1) vs 新规则(v2)。

口径：
- 需要复核的代理真值 = 模型判错（pred != gold，主评分 relevant=yes 行）；
- 命中率/召回 = 标记 ∩ 判错 / 判错；
- 误报率 = 标记 ∩ 判对 / 标记（标了但实际判对，会浪费人工）；
- 标记率 = 标记 / 总数。

用法：
    python tests/backtest_need_review.py --golden tests/fixtures/golden_set_v1.csv
    python tests/backtest_need_review.py --golden tests/fixtures/coldstart_digital3c_golden.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.benchmark_golden import get_llm_analyzer, load_golden, predict_all  # noqa: E402
from app.coding.coder import need_review_reason_v1, need_review_reason_v2  # noqa: E402


def evaluate(rows: list[dict], preds: list[dict],
             rules: list[tuple[str, object]] | None = None) -> dict:
    n = len(rows)
    errors = [
        (r["text"], p["sentiment"], r["sentiment"], p["confidence"])
        for r, p in zip(rows, preds)
        if p["sentiment"] != r["sentiment"]
    ]
    confs = [p["confidence"] for p in preds]
    out: dict = {"n": n, "n_errors": len(errors), "accuracy": (n - len(errors)) / n}
    for tag, fn in rules or (
        ("v1_旧(conf<0.5+反讽)", need_review_reason_v1),
        ("v2_新(conf<0.7+反讽/黑话/问句/短句)", need_review_reason_v2),
    ):
        flagged_err = flagged_ok = 0
        reasons: dict[str, int] = {}
        for r, p in zip(rows, preds):
            reason = fn(r["text"], p["confidence"])
            if not reason:
                continue
            if reason in reasons:
                reasons[reason] += 1
            else:
                reasons[reason] = 1
            if p["sentiment"] != r["sentiment"]:
                flagged_err += 1
            else:
                flagged_ok += 1
        flagged = flagged_err + flagged_ok
        out[f"{tag}_命中率"] = round(flagged_err / len(errors), 4) if errors else None
        out[f"{tag}_误报率"] = round(flagged_ok / flagged, 4) if flagged else None
        out[f"{tag}_标记率"] = round(flagged / n, 4)
        out[f"{tag}_标记数"] = flagged
        out[f"{tag}_原因分布"] = reasons
    out["置信度分桶"] = {
        "<0.5": sum(1 for c in confs if c < 0.5),
        "0.5-0.7": sum(1 for c in confs if 0.5 <= c < 0.7),
        ">=0.7": sum(1 for c in confs if c >= 0.7),
    }
    return out


def _predict_and_maybe_save(rows: list[dict], use_llm: bool,
                            save_preds: Path | None) -> list[dict]:
    llm = get_llm_analyzer() if use_llm else None
    preds = predict_all(rows, use_llm, llm)
    if save_preds:
        payload = [
            {"text": r["text"], "sentiment": r["sentiment"],
             "pred": p["sentiment"], "confidence": p["confidence"]}
            for r, p in zip(rows, preds)
        ]
        save_preds.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"预测已缓存：{save_preds}")
    return preds


def _load_preds(path: Path, rows: list[dict]) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if len(payload) != len(rows):
        raise SystemExit(f"缓存条数 {len(payload)} != 可评分 {len(rows)}，请重新 --save-preds")
    preds = []
    for r, p in zip(rows, payload):
        if p["text"] != r["text"]:
            raise SystemExit("缓存文本与 golden 不一致，请重新 --save-preds")
        preds.append({"sentiment": p["pred"], "confidence": p["confidence"]})
    return preds


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", type=Path, required=True)
    ap.add_argument("--no-llm", action="store_true", help="仅词典直判（快速自检用）")
    ap.add_argument("--save-preds", type=Path, default=None, help="缓存预测结果（调参免重复 LLM）")
    ap.add_argument("--load-preds", type=Path, default=None, help="读取预测缓存做规则调参")
    args = ap.parse_args()
    rows = [r for r in load_golden(args.golden) if r.get("relevant", "yes") == "yes"]
    print(f"golden: {args.golden} | 可评分 {len(rows)} 条")
    preds = (
        _load_preds(args.load_preds, rows)
        if args.load_preds
        else _predict_and_maybe_save(rows, use_llm=not args.no_llm, save_preds=args.save_preds)
    )
    res = evaluate(rows, preds)
    print(f"准确率: {res['accuracy']:.1%} | 判错 {res['n_errors']} 条")
    for k, v in res.items():
        if k in ("n", "n_errors", "accuracy"):
            continue
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
