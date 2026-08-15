# -*- coding: utf-8 -*-
"""边界集人工验收修订（V2 验证纪律：gold 修订独立于模型）。

两种模式：
1) 通用模式（2.6 起，推荐）：--sheet <验收表.xlsx>——读取
   finalize_edge_set.py --acceptance-review 生成的验收表（含「人工判定情感」与
   「是否参考过模型判定」列），对人工修订行应用 gold 变更并写入 gold_revised_by_model。
2) 历史模式（2.3 收尾 v1.1，2026-08-14 已执行）：默认无参数，沿用内置 REVISIONS
   修订 6 条 gold；该模式修订发生在人工看过模型错误之后，gold_revised_by_model 标「是」。

处理顺序：apply_edge_review.py 定版 v1.0 → 本脚本修订 → 重新跑分并重冻结基线
（edge_set 指纹变化，旧基线对比被指纹守卫拦截）。

用法：
    python tests/apply_edge_acceptance_revision.py
    python tests/apply_edge_acceptance_revision.py --sheet <验收表.xlsx>
"""

from __future__ import annotations

import csv
import sys
import argparse
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
DATASETS = ROOT / "data" / "datasets"
FIXTURES = ROOT / "tests" / "fixtures"
WORK = DATASETS / "edge_set_v1.csv"
FIXTURE = FIXTURES / "edge_set_v1.csv"
VALID = {"positive", "negative", "neutral", "mixed"}
# 人工修订只允许主评分情感（mixed 不在 benchmark 主评分枚举内，禁止修订为 mixed）
REVISION_VALID = {"positive", "negative", "neutral"}

# text_id -> 人工判定情感（用户 2026-08-14 验收反馈，人工=模型判定，gold 修订）
REVISIONS = {
    "run_P100": "neutral",    # emoji：二创分享无观点
    "run_P1591": "negative",  # emoji：小心乐子，警示性内容
    "run_P1466": "neutral",   # irony：笑死娱乐语境，无明确褒贬
    "v2_C102": "negative",    # long：批评两家预热力竭
    "run_C781": "negative",   # lowconf：疑似反讽/负面
    "run_P942": "neutral",    # lowconf：瑞幸品牌介绍（官方口径与主集对齐）
}


def _load_rows(work: Path) -> tuple[list[dict], list[str]]:
    with open(work, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    cols = list(rows[0].keys())
    if "gold_revised_by_model" not in cols:
        cols.append("gold_revised_by_model")
        for r in rows:
            r["gold_revised_by_model"] = r.get("gold_revised_by_model", "")
    return rows, cols


def _write(path: Path, rows: list[dict], cols: list[str], keep_src: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out_cols = cols if keep_src else [c for c in cols if c not in ("url", "likes", "time")]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=out_cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in out_cols})


def apply_sheet(sheet: Path) -> int:
    """通用模式：按验收表人工判定修订 gold，并写入「是否参考过模型判定」披露。"""
    from tests import edge_annotation as ea

    if not WORK.exists():
        print(f"缺失：{WORK}（先运行 tests/apply_edge_review.py）")
        return 1
    rows, cols = _load_rows(WORK)
    accept = {a["text_id"]: a for a in ea.load_acceptance_xlsx(sheet) if a["text_id"]}
    if not accept:
        print(f"验收表没有有效行：{sheet}")
        return 1
    changed, undisclosed = 0, []
    for r in rows:
        a = accept.get(r["text_id"])
        if not a or not a["human_sentiment"]:
            continue
        new = a["human_sentiment"]
        old = r["sentiment"]
        if new == old:
            continue  # 人工确认 gold（模型错），不修订
        assert new in REVISION_VALID, (
            f"{r['text_id']} 人工修订仅允许 {sorted(REVISION_VALID)}（主评分不含 mixed）")
        r["sentiment"] = new
        if new == "neutral":
            r["intensity"] = "1"
        elif not r.get("intensity") or r["intensity"] not in {"1", "2", "3", "4", "5"}:
            r["intensity"] = "3"
        note = (r.get("finalize_note") or "").strip()
        add = f"人工验收修订:情感 {old}→{new}"
        r["finalize_note"] = (note + "；" if note else "") + add
        flag = (a["gold_revised_by_model"] or "").strip() or "未知"
        r["gold_revised_by_model"] = flag
        if flag == "未知":
            undisclosed.append(r["text_id"])
        changed += 1
        print(f"  {r['text_id']}: {old} → {new}（{add}；参考模型判定={flag}）")
    if undisclosed:
        print(f"[警告] {len(undisclosed)} 条修订未披露是否参考模型判定 → 标「未知」"
              f"（{', '.join(undisclosed[:10])}）")
    _write(WORK, rows, cols, True)
    _write(FIXTURE, rows, cols, False)
    print(f"\n已修订 {changed} 条 → {WORK.name}")
    return 0 if changed else 1


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="边界集人工验收修订（V2 验证纪律）")
    ap.add_argument("--sheet", type=Path, help="验收表 xlsx（finalize_edge_set.py "
                                                "--acceptance-review 生成）")
    args = ap.parse_args()
    if args.sheet:
        return apply_sheet(args.sheet)

    # 历史模式（2.3 收尾 v1.1）：内置 REVISIONS
    if not WORK.exists():
        print(f"缺失：{WORK}（先运行 tests/apply_edge_review.py）")
        return 1
    rows, cols = _load_rows(WORK)
    changed = 0
    for r in rows:
        tid = r["text_id"]
        if tid in REVISIONS:
            new = REVISIONS[tid]
            old = r["sentiment"]
            if new == old:
                print(f"  {tid} 已是 {new}，跳过")
                continue
            assert new in VALID, f"{tid} 非法情感 {new}"
            r["sentiment"] = new
            if new == "neutral":
                r["intensity"] = "1"
            elif not r.get("intensity") or r["intensity"] not in {"1", "2", "3", "4", "5"}:
                r["intensity"] = "3"
            note = (r.get("finalize_note") or "").strip()
            add = f"v1.1人工验收修订:情感 {old}→{new}"
            r["finalize_note"] = (note + "；" if note else "") + add
            r["gold_revised_by_model"] = "是"  # 历史模式：修订发生在看过模型错误之后
            changed += 1
            print(f"  {tid}: {old} → {new}（{add}）")
    _write(WORK, rows, cols, True)
    _write(FIXTURE, rows, cols, False)

    # 校验：仅 6 条情感变化；neutral 强度=1；relevant=no 必须 neutral
    bad_neutral = [r["text_id"] for r in rows
                   if r["sentiment"] == "neutral" and r.get("intensity") != "1"]
    bad_rel = [r["text_id"] for r in rows
               if r.get("relevant") == "no" and r["sentiment"] != "neutral"]
    print(f"\n已修订 {changed}/{len(REVISIONS)} 条 → edge_set v1.1")
    print("情感分布:", dict(Counter(r["sentiment"] for r in rows)))
    print("子集分布:", dict(Counter(r["subset"] for r in rows)))
    print("校验：neutral 强度≠1:", len(bad_neutral), "| relevant=no 且非 neutral:", len(bad_rel))
    return 0 if (changed == len(REVISIONS) and not bad_neutral and not bad_rel) else 1


if __name__ == "__main__":
    raise SystemExit(main())
