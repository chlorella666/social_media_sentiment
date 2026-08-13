# -*- coding: utf-8 -*-
"""边界集 v1.1 人工验收修订（2.3 收尾，2026-08-14）。

依据：用户对 49 条错误样本的人工重判（edge_2.3_acceptance_review.xlsx，
Sheet「错误样本验收」人工结论列）。其中 6 条"人工=模型判定≠gold"按人工判定
修订 gold；41 条"人工=gold"确认模型错（记入已知失败模式，不改）；
2 条三方歧义保留原 gold（备注记录）。

处理顺序：apply_edge_review.py 定版 v1.0 → 本脚本修订 → 重新跑分并重冻结基线
（edge_set 指纹变化，旧基线对比被指纹守卫拦截）。

用法：
    python tests/apply_edge_acceptance_revision.py
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASETS = ROOT / "data" / "datasets"
FIXTURES = ROOT / "tests" / "fixtures"
WORK = DATASETS / "edge_set_v1.csv"
FIXTURE = FIXTURES / "edge_set_v1.csv"

# text_id -> 人工判定情感（用户 2026-08-14 验收反馈，人工=模型判定，gold 修订）
REVISIONS = {
    "run_P100": "neutral",    # emoji：二创分享无观点
    "run_P1591": "negative",  # emoji：小心乐子，警示性内容
    "run_P1466": "neutral",   # irony：笑死娱乐语境，无明确褒贬
    "v2_C102": "negative",    # long：批评两家预热力竭
    "run_C781": "negative",   # lowconf：疑似反讽/负面
    "run_P942": "neutral",    # lowconf：瑞幸品牌介绍（官方口径与主集对齐）
}
VALID = {"positive", "negative", "neutral", "mixed"}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not WORK.exists():
        print(f"缺失：{WORK}（先运行 tests/apply_edge_review.py）")
        return 1
    with open(WORK, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    cols = list(rows[0].keys())
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
            changed += 1
            print(f"  {tid}: {old} → {new}（{add}）")

    def write(path: Path, keep_src: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        out_cols = cols if keep_src else [c for c in cols if c not in ("url", "likes", "time")]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=out_cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in out_cols})

    write(WORK, True)
    write(FIXTURE, False)

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
