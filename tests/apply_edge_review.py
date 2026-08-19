# -*- coding: utf-8 -*-
"""边界样本专项集人工复核应用与定版（2.3 阶段 1 配套）。

用法：
    python tests/apply_edge_review.py

输入：
    主标/副标标注表（annotation_edge_v1_workbuddy/trae.xlsx 或 .csv）
    一致性核对表 + 人工抽检校准表（edge_review_consistency_v1.xlsx /
      edge_review_calibration_v1.xlsx，人工判定列已填写）

处理（docs/archive/边界样本专项集方案.md §四/§六）：
    - 人工判定优先（两套复核表合并，以人工为准）；
    - 双 AI 情感/相关分歧且无人工判定 → 争议集 edge_set_v1_disputed.csv；
    - 其余字段以主标为准，人工填写则覆盖。

输出：
    data/datasets/edge_set_v1.csv     工作版（含来源信息）
    tests/fixtures/edge_set_v1.csv    去 URL 入库版（跨机器复现）
    data/datasets/edge_set_v1_disputed.csv（争议样本，提示词调优弹药）
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tests import edge_annotation as ea  # noqa: E402


def _pick_input(primary: bool) -> Path:
    x = ea.EDGE_PRIMARY_XLSX if primary else ea.EDGE_SECONDARY_XLSX
    c = ea.EDGE_PRIMARY_CSV if primary else ea.EDGE_SECONDARY_CSV
    return ea.pick_annotated_input(x, c)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    primary_path = _pick_input(True)
    secondary_path = _pick_input(False)
    missing = [p for p in (primary_path, secondary_path,
                           ea.EDGE_REVIEW_CONSISTENCY_XLSX,
                           ea.EDGE_REVIEW_CALIBRATION_XLSX) if not p.exists()]
    if missing:
        print("缺少输入文件：")
        for p in missing:
            print("  -", p)
        print("先运行 tests/finalize_edge_set.py --compare 并完成人工复核。")
        return 1

    primary = ea.load_annotation(primary_path)
    secondary = ea.load_annotation(secondary_path)
    errs = ea.validate_rows(primary) + ea.validate_rows(secondary)
    if errs:
        print("标注取值校验失败：")
        for e in errs[:30]:
            print("  -", e)
        return 1

    human = {}
    reviewed_ids: set[str] = set()
    for path in (ea.EDGE_REVIEW_CONSISTENCY_XLSX, ea.EDGE_REVIEW_CALIBRATION_XLSX):
        for h in ea.load_review_xlsx(path):
            if h["text_id"]:
                human[h["text_id"]] = h
                reviewed_ids.add(h["text_id"])
    print(f"人工复核：{len(human)} 条已加载（去重后，均在复核表内）")

    final_rows, disputed_rows = ea.finalize_rows(primary, secondary, human, reviewed_ids)
    ea.write_edge_set_csv(final_rows, ea.EDGE_SET_CSV)

    fixture_cols = [c for c in ea.EDGE_COLUMNS if c not in ("url", "likes", "time")]
    ea.EDGE_SET_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    with open(ea.EDGE_SET_FIXTURE, "w", newline="", encoding="utf-8-sig") as f:
        import csv

        w = csv.DictWriter(f, fieldnames=fixture_cols)
        w.writeheader()
        for r in final_rows:
            w.writerow({c: r.get(c, "") for c in fixture_cols})

    if disputed_rows:
        ea.write_disputed_csv(disputed_rows, ea.EDGE_DISPUTED_CSV)
    else:
        ea.EDGE_DISPUTED_CSV.write_text("", encoding="utf-8")

    print(f"\n定版：{ea.EDGE_SET_CSV.name}，共 {len(final_rows)} 条（争议 {len(disputed_rows)} 条）")
    print("子集分布：", dict(Counter(r["subset"] for r in final_rows)))
    print("情感分布：", dict(Counter(r["sentiment"] for r in final_rows)))
    print("相关分布：", dict(Counter(r["relevant"] for r in final_rows)))
    print(f"入库版：{ea.EDGE_SET_FIXTURE}")
    if disputed_rows:
        print(f"争议集：{ea.EDGE_DISPUTED_CSV}")
        for d in disputed_rows:
            print("  -", d["text_id"], d["reason"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
