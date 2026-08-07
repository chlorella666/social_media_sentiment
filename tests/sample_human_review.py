# -*- coding: utf-8 -*-
"""人工抽检校准表生成：从黄金集 v1 抽取 40 条供人工复核。

口径（2026-08-07 定版，见 docs/抽样与标注规范.md §十一）：
    - 高歧义全量：relevant=no（6 条）+ 语言现象含"反讽"（定稿后 2 条），去重后 8 条
    - 其余按情感分层随机抽取 32 条（seed=42，可复现）

用法：
    python tests/sample_human_review.py

输出（data/ 工作区，gitignore）：
    data/datasets/human_review_worksheet_v1.csv
    data/datasets/human_review_worksheet_v1.xlsx

复核方式：人工填写"人工-*"列与"是否同意AI整条情感"，回传后计算
与 AI 标签的一致率/kappa，据此更新黄金集或记录校准结论。
"""

from __future__ import annotations

import csv
import random
import sys
from collections import Counter
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "data" / "datasets" / "golden_set_v1.csv"
OUT_CSV = ROOT / "data" / "datasets" / "human_review_worksheet_v1.csv"
OUT_XLSX = ROOT / "data" / "datasets" / "human_review_worksheet_v1.xlsx"
TARGET = 40


def load_rows() -> list[dict]:
    with open(GOLDEN, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def pick(rows: list[dict], rng: random.Random) -> list[dict]:
    """9 条高歧义全量 + 31 条按情感分层随机，seed 固定可复现。"""
    high = [r for r in rows if r.get("relevant") == "no" or "反讽" in (r.get("language_flags") or "")]
    high_ids = {r["text_id"] for r in high}
    rest = [r for r in rows if r["text_id"] not in high_ids]

    buckets: dict[str, list[dict]] = {}
    for r in rest:
        buckets.setdefault(r.get("sentiment") or "neutral", []).append(r)
    for b in buckets.values():
        rng.shuffle(b)

    need = TARGET - len(high)
    share = {s: len(v) / len(rest) for s, v in buckets.items()}
    want = {s: int(round(need * p)) for s, p in share.items()}
    diff = need - sum(want.values())
    for s in sorted(want, key=lambda k: -want[k]):
        if diff <= 0:
            break
        want[s] += 1
        diff -= 1

    selected = list(high)
    for s, k in want.items():
        selected.extend(buckets[s][:k])

    # 分层不足时（某类样本少）从剩余补齐
    used = {r["text_id"] for r in selected}
    leftover = [r for b in buckets.values() for r in b]
    for r in leftover:
        if len(selected) >= TARGET:
            break
        if r["text_id"] not in used:
            selected.append(r)

    rng.shuffle(selected)
    return selected


def build_rows(selected: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(selected, 1):
        out.append({
            "序号": i,
            "text_id": r["text_id"],
            "原文": r.get("text") or "",
            "平台": r.get("platform") or "",
            "领域": r.get("domain") or "",
            "文本类型": r.get("kind") or "",
            "AI-整条情感": r.get("sentiment") or "",
            "AI-强度": r.get("intensity") or "",
            "AI-维度情感": r.get("dimension_sentiments") or "{}",
            "AI-语言现象": r.get("language_flags") or "",
            "AI-是否相关": r.get("relevant") or "",
            "人工-整条情感(positive/negative/neutral/mixed)": "",
            "人工-强度(1-5)": "",
            "人工-维度情感(可空，格式:维度=情感,如 价格=negative)": "",
            "人工-是否相关(yes/no)": "",
            "是否同意AI整条情感(yes/no)": "",
            "备注": "",
        })
    return out


def write_csv(rows: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_xlsx(rows: list[dict], path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "抽检表"
    cols = list(rows[0].keys())
    ws.append(cols)

    header_fill = PatternFill("solid", fgColor="D9E1F2")
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")

    for r in rows:
        ws.append([r[c] for c in cols])

    widths = {
        "序号": 6, "text_id": 10, "原文": 60, "平台": 10, "领域": 10, "文本类型": 10,
        "AI-整条情感": 12, "AI-强度": 8, "AI-维度情感": 24, "AI-语言现象": 12, "AI-是否相关": 10,
        "人工-整条情感(positive/negative/neutral/mixed)": 18,
        "人工-强度(1-5)": 10,
        "人工-维度情感(可空，格式:维度=情感,如 价格=negative)": 30,
        "人工-是否相关(yes/no)": 14,
        "是否同意AI整条情感(yes/no)": 18,
        "备注": 30,
    }
    for j, c in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(j)].width = widths.get(c, 14)
    for row in ws.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=cell.column == 2)
    ws.freeze_panes = "A2"

    guide = wb.create_sheet("填写说明")
    guide.append(["人工抽检校准填写说明（2026-08-07 定版口径）"])
    guide.append([])
    guide.append(["1. 只依据原文判断，不要参考 AI 列（独立标注原则，规范 §五 规则 10）。"])
    guide.append(["2. 必填：人工-整条情感、人工-是否相关、是否同意AI整条情感。强度与维度情感可选。"])
    guide.append(["3. 情感取值：positive/negative/neutral/mixed；mixed 仅用于无法拆解又定不了主倾向的文本。"])
    guide.append(["4. 整条情感口径：先读全文定综合结论，再拆维度（规范 §五 规则 1-2）。"])
    guide.append(["5. 反讽=表面正向/中性、实际负向；直接批评不算反讽（规则 3）。"])
    guide.append(["6. 官方公告/纯转发/无观点 → neutral，维度留空（规则 5）。"])
    guide.append(["7. 与品牌/主题无关 → 人工-是否相关=no，情感标 neutral（规则 8）。"])
    guide.append(["8. 填写完成后回传（保存 xlsx 或另存 CSV），我们会计算一致率/kappa 并更新校准结论。"])
    guide.column_dimensions["A"].width = 100

    wb.save(path)


def main() -> None:
    rows = load_rows()
    rng = random.Random(42)
    selected = pick(rows, rng)
    out = build_rows(selected)
    write_csv(out, OUT_CSV)
    write_xlsx(out, OUT_XLSX)

    print(f"抽检表已生成：{OUT_XLSX.name} / {OUT_CSV.name}，共 {len(out)} 条")
    print("组成：高歧义全量", sum(
        1 for r in selected if r.get("relevant") == "no" or "反讽" in (r.get("language_flags") or "")),
        "条 + 分层随机", len(out) - sum(
            1 for r in selected if r.get("relevant") == "no" or "反讽" in (r.get("language_flags") or "")), "条")
    print("AI 情感分布:", dict(Counter(r["sentiment"] for r in selected)))
    print("按渠道:", dict(Counter(r["platform"] for r in selected)))
    print("按领域:", dict(Counter(r["domain"] for r in selected)))


if __name__ == "__main__":
    main()
