# -*- coding: utf-8 -*-
"""2.4 前置：104 条维度标注人工复核工具（边界集维度裁判版 v1.2）。

背景（todolist 2.4 前置项，2026-08-14）：
    边界集维度级一致率 34.7%（n=193，目标 ≥75%）未达标（已决策接受、以主标为准），
    2.4 维度裁判必须先人工复核 edge_set_v1.csv 中全部 104 条带维度标注的样本，
    否则裁判不可信。主集（黄金集）96 条维度标注已有 40 条人工抽检校准，不在本工具范围。

用法：
    python tests/edge_dimension_review.py --gen     # 生成复核表（104 行，预填 2.3 人工判定）
    python tests/edge_dimension_review.py --apply   # 人工判定应用定版 → edge_set_v1.csv（v1.2 维度裁判版）

规则：
    - 复核表内每维一列（按该行领域），只填 positive / negative，留空 = 认可主标；
    - 人工判定优先：填了任意维度列，整条 dimension_sentiments 以人工为准（覆盖主标）；
    - 无品牌现象样本（17 条，2.4 裁判污染治理，2026-08-14）：
        「文本品牌召回(人工填)」：文本里出现真实品牌（如"唔该茶铺""鲜知味"）→ 填回品牌；
        「类别级参考(是/否)」：确无品牌但维度有效（如"佛山探店"）→ 填"是"，维度保留为参考级；
        既无品牌召回又非类别级 → 维度应清空（人工判定维度列留空 + 备注"清空维度"）；
        「领域重判(game/consumer/other)」：领域错配行可重判；领域变更 → 维度清空
        （旧领域维度词汇不迁移，该行退出维度裁判，整条情感裁判保留）；
    - 备注命令（人工判定列无法表达的整行操作）：
        "清空维度"    → 该行维度清空（relevant=no 口径归一用）；
        "改相关=yes/no" → 改是否相关（相关口径调整）；
        "保持主标"    → 明确保留主标（relevant=no 且带维度的行必须显式表态）；
    - 守卫：① relevant=no 却带维度标注 → 必须显式处理（清空 / 改相关 / 保持主标）；
      ② 无品牌且维度非空 → 必须满足（品牌召回非空 或 类别级参考=是 或 显式"保持主标"），
      否则 --apply 拒绝定版（防规范冲突 / 品牌锚缺失静默入库）；
    - 定版后重跑评测重冻结基线（指纹随标注变更而变化，eval_store 守卫）。
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tests import edge_annotation as ea  # noqa: E402

EDGE_SET_CSV = ROOT / "data" / "datasets" / "edge_set_v1.csv"
EDGE_SET_FIXTURE = ROOT / "tests" / "fixtures" / "edge_set_v1.csv"
REVIEW_XLSX = ROOT / "data" / "datasets" / "edge_dimension_review_v1.xlsx"
REVIEW_REPORT = ROOT / "data" / "datasets" / "edge_dimension_review_report_v1.json"

# 维度中文名（与 app/domains/*.json 的 name 一致；读取避免硬编码漂移）
def _domain_dim_names() -> dict[str, list[str]]:
    out = {}
    for domain_id in ("game", "consumer"):
        p = ROOT / "app" / "domains" / f"{domain_id}.json"
        schema = json.loads(p.read_text(encoding="utf-8"))
        out[domain_id] = [d["name"] for d in schema["dimensions"]]
    return out


DOMAIN_DIMS = _domain_dim_names()
VALID_DIM_VALUES = {"positive", "negative"}


def load_edge_rows() -> list[dict]:
    """读取 edge_set_v1.csv，返回 104 条带维度标注的行（dimension_sentiments 非空）。"""
    rows = list(csv.DictReader(open(EDGE_SET_CSV, encoding="utf-8-sig")))
    dim_rows = []
    for r in rows:
        ds = (r.get("dimension_sentiments") or "").strip()
        if ds and ds != "{}":
            dim_rows.append(r)
    return dim_rows


def load_secondary_dims() -> dict[str, dict]:
    """trae 副标维度参考：{text_id: {维度名: 情感}}。"""
    path = ea.pick_annotated_input(ea.EDGE_SECONDARY_XLSX, ea.EDGE_SECONDARY_CSV)
    if not path.exists():
        return {}
    out = {}
    for r in ea.load_annotation(path):
        out[r["text_id"]] = dict(r["dims"])
    return out


def load_human_dims() -> dict[str, dict]:
    """2.3 两张复核表里已有人工维度判定的行（人工优先，预填进新表）。"""
    out: dict[str, dict] = {}
    for path in (ea.EDGE_REVIEW_CONSISTENCY_XLSX, ea.EDGE_REVIEW_CALIBRATION_XLSX):
        if not path.exists():
            continue
        for h in ea.load_review_xlsx(path):
            if h["text_id"] and h["dims"]:
                out.setdefault(h["text_id"], h["dims"])
    return out


REVIEW_HEADERS = (
    "序号", "text_id", "原文", "平台", "领域", "品牌/主题", "子集",
    "是否相关", "整条情感", "强度", "主标维度", "副标维度",
    "2.3复核状态", "2.3人工判定(参考)",
)
REVIEW_EXTRA = (
    "现象样本(无品牌)", "文本品牌召回(人工填)", "类别级参考(是/否)",
    "领域重判(game/consumer/other)",
)


def gen_worksheet() -> int:
    rows = load_edge_rows()
    if len(rows) != 104:
        print(f"预期 104 条带维度标注，实际 {len(rows)} 条——中止，先核对 edge_set_v1.csv")
        return 1
    secondary = load_secondary_dims()
    human = load_human_dims()

    wb = Workbook()
    ws = wb.active
    ws.title = "复核"
    # 全部维度列（游戏 7 + 消费品 7），按行领域只使用对应 7 列，其余留空
    dim_cols = DOMAIN_DIMS["game"] + DOMAIN_DIMS["consumer"]
    headers = list(REVIEW_HEADERS) + dim_cols + list(REVIEW_EXTRA) + [
        "提示", "备注", "复核人", "复核日期"
    ]
    ws.append(headers)
    header_fill = PatternFill("solid", fgColor="E2EFDA")
    warn_fill = PatternFill("solid", fgColor="FFF2CC")
    header_font = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font

    prefilled = 0
    flagged = 0
    for i, r in enumerate(rows, 1):
        tid = r["text_id"]
        domain = r["domain"]
        dims = json.loads(r["dimension_sentiments"] or "{}")
        sec = secondary.get(tid, {})
        hd = human.get(tid, {})
        status = "2.3已人工判定" if hd else "未复核（以主标为准）"
        nobrand = not (r.get("brand") or "").strip()
        note = ""
        if nobrand:
            note = ("⚠ 无品牌现象样本：文本含真实品牌请填「品牌召回」列；"
                    "确无品牌但维度有效请填「类别级参考=是」；否则维度应清空")
        if r.get("relevant", "yes") == "no" and dims:
            rel_note = ("⚠ relevant=no 却带维度标注：请在备注写「清空维度」或「改相关=yes」"
                        "（或「保持主标」显式保留），否则定版会拦截")
            note = rel_note + ("；" + note if note else "")
            flagged += 1
        if hd:
            prefilled += 1
        row = {
            "序号": i,
            "text_id": tid,
            "原文": r["text"],
            "平台": r["platform"],
            "领域": domain,
            "品牌/主题": ea.brand_display(r.get("brand", "")),
            "子集": ea.SUB_EN_TO_CN.get(r.get("subset", ""), r.get("subset", "")),
            "是否相关": r.get("relevant", ""),
            "整条情感": r.get("sentiment", ""),
            "强度": r.get("intensity", ""),
            "主标维度": json.dumps(dims, ensure_ascii=False),
            "副标维度": json.dumps(sec, ensure_ascii=False),
            "2.3复核状态": status,
            "2.3人工判定(参考)": json.dumps(hd, ensure_ascii=False) if hd else "",
            "现象样本(无品牌)": "是" if nobrand else "",
            "文本品牌召回(人工填)": "",
            "类别级参考(是/否)": "",
            "领域重判(game/consumer/other)": "",
            "提示": note,
            "备注": "",
            "复核人": "",
            "复核日期": "",
        }
        vals = [row.get(h, "") for h in headers]
        # 预填人工判定维度列（2.3 已人工判定 → 该值已定版，用户确认或修改）
        for d in DOMAIN_DIMS[domain]:
            vals[headers.index(d)] = hd.get(d, "") if hd else ""
        ws.append(vals)
        row_fill = warn_fill if note else None
        if row_fill:
            for cell in ws[ws.max_row]:
                cell.fill = row_fill
        for cell in ws[ws.max_row]:
            cell.alignment = wrap

    widths = [6, 12, 55, 10, 8, 14, 10, 9, 10, 7, 22, 22, 16, 22]
    for d in dim_cols:
        widths.append(14)
    widths += [16, 22, 14, 18, 40, 24, 10, 10]
    for i, wd in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = wd
    ws.freeze_panes = "D2"

    guide = wb.create_sheet("填写说明")
    lines = [
        "边界集 104 条维度标注人工复核（2.4 前置，维度裁判版 v1.2）",
        "",
        "一、每行按「领域」只填对应 7 个维度列（游戏/消费品），其他领域列留空：",
        "    - 取值只允许 positive / negative；无明确褒贬留空；",
        "    - 全部留空 = 认可主标（当前定版值），无需填写；",
        "    - 填了任意维度列 = 该行 dimension_sentiments 整条以人工为准（覆盖主标）。",
        "二、无品牌现象样本（「现象样本」列=是，黄色高亮，共 17 条）：",
        "    - 文本含真实品牌（如「唔该茶铺」「鲜知味」）→ 在「文本品牌召回」列填回品牌；",
        "    - 确无品牌但维度有效（如「佛山探店」）→ 「类别级参考」填「是」（维度保留为参考级）；",
        "    - 都不适用 → 维度列清空（备注写「清空维度」）；",
        "    - 领域错配（游戏内容标了消费品等）→ 「领域重判」填 game/consumer/other；",
        "      领域变更后该行维度将清空（退出维度裁判，整条情感裁判保留）。",
        "三、relevant=no 却带维度标注（5 条），必须在备注显式表态：",
        '    - 写「清空维度」→ 维度清空；',
        '    - 写「改相关=yes」→ 改为相关并保留维度；',
        '    - 写「保持主标」→ 保留现状（请在备注说明理由）。',
        "    未表态的行，定版脚本会拒绝执行（防规范冲突静默入库）。",
        "四、判例速查（抽样与标注规范 §五）：",
        "    - 转折句逐维拆解：「画面好但价格贵」→ 美术 positive、价格 negative；",
        "    - 「贵是贵但值」→ 价格 negative、体验 positive；",
        "    - 只提到没褒贬 → 留空；反讽按真实意图判；emoji 需结合文字语境。",
        "五、填完运行：python tests/edge_dimension_review.py --apply",
        "    定版产物：edge_set_v1.csv（v1.2 维度裁判版）+ 变更报告 + 争议清单。",
    ]
    for line in lines:
        guide.append([line])
    guide.column_dimensions["A"].width = 120
    guide["A1"].font = Font(bold=True, size=12)
    wb.save(REVIEW_XLSX)
    print(f"已生成：{REVIEW_XLSX}")
    print(f"  104 条维度标注（游戏 {sum(1 for r in rows if r['domain']=='game')} / "
          f"消费品 {sum(1 for r in rows if r['domain']=='consumer')}）")
    print(f"  2.3 已人工判定预填：{prefilled} 条（留空列按主标处理）")
    print(f"  无品牌现象样本：{sum(1 for r in rows if not (r.get('brand') or '').strip())} 条"
          f"（其中 relevant=no 待显式处理：{flagged} 条，黄色高亮）")
    return 0


def _parse_human_row(headers: list[str], rec: tuple, domain: str) -> dict:
    """从复核表一行提取人工判定维度（该领域 7 列 → dict）。"""
    dims = {}
    for d in DOMAIN_DIMS[domain]:
        v = str(rec[headers.index(d)] or "").strip().lower() if headers.index(d) < len(rec) else ""
        if v:
            if v not in VALID_DIM_VALUES:
                raise ValueError(f"维度[{d}]取值非法：{v}（只允许 positive/negative）")
            dims[d] = v
    return dims


def _cell(rec: tuple, headers: list[str], name: str) -> str:
    if name not in headers:
        return ""
    i = headers.index(name)
    return str(rec[i] or "").strip() if i < len(rec) else ""


def apply_review() -> int:
    if not REVIEW_XLSX.exists():
        print(f"复核表不存在：{REVIEW_XLSX}（先运行 --gen 并完成人工填写）")
        return 1
    all_rows = list(csv.DictReader(open(EDGE_SET_CSV, encoding="utf-8-sig")))
    edge_map = {r["text_id"]: r for r in all_rows}
    wb = load_workbook(REVIEW_XLSX, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    headers = [str(h) if h else "" for h in next(it)]
    human_rows: dict[str, dict] = {}
    for rec in it:
        tid = str(rec[headers.index("text_id")] or "").strip()
        if not tid:
            continue
        if tid not in edge_map:
            print(f"复核表出现未知 text_id：{tid}——中止")
            return 1
        domain = edge_map[tid]["domain"]
        dims = _parse_human_row(headers, rec, domain)
        remark = str(rec[headers.index("备注")] or "").strip()
        human_rows[tid] = {
            "dims": dims,
            "remark": remark,
            "brand_recall": _cell(rec, headers, "文本品牌召回(人工填)"),
            "category_level": _cell(rec, headers, "类别级参考(是/否)"),
            "domain_rejudge": _cell(rec, headers, "领域重判(game/consumer/other)"),
        }
    wb.close()

    if len(human_rows) != 104:
        print(f"复核表应含 104 行，实际 {len(human_rows)} 行——中止")
        return 1

    changes: list[dict] = []
    blocked: list[str] = []
    for tid, h in human_rows.items():
        r = edge_map[tid]
        old_dims = json.loads(r["dimension_sentiments"] or "{}")
        old_rel = r.get("relevant", "yes")
        remark = h["remark"]
        note_parts = []
        new_dims = dict(old_dims)
        new_rel = old_rel
        new_brand = (r.get("brand") or "").strip()
        new_domain = r["domain"]

        # 无品牌现象样本治理（2026-08-14）：
        # 品牌召回 / 类别级参考 / 领域重判
        if h["brand_recall"]:
            new_brand = h["brand_recall"]
            note_parts.append(f"品牌召回：{h['brand_recall']}")
        category_level = h["category_level"].lower() in ("是", "yes", "true")
        if category_level:
            note_parts.append("类别级参考")

        if "清空维度" in remark:
            new_dims = {}
            note_parts.append("人工：清空维度")
        elif "改相关" in remark:
            for token in ("改相关=yes", "改相关=no"):
                if token in remark:
                    new_rel = token.split("=")[-1]
                    note_parts.append(f"人工：相关 {old_rel}→{new_rel}")
        if h["dims"]:
            new_dims = h["dims"]
            note_parts.append("人工：维度判定覆盖")
        # 领域重判最后生效：领域变更 → 旧领域维度词汇不迁移，该行退出维度裁判
        if h["domain_rejudge"]:
            if h["domain_rejudge"] not in ("game", "consumer", "other"):
                raise ValueError(f"{tid} 领域重判取值非法：{h['domain_rejudge']}"
                                 "（只允许 game/consumer/other）")
            if h["domain_rejudge"] == "other":
                # other = 非消费品/游戏内容（如社会事件）：不参与维度裁判；
                # 领域字段保持原值（避免破坏评测/加载），仅清空维度并落档
                new_dims = {}
                note_parts.append("领域重判=other（不参与维度裁判），维度清空")
            elif h["domain_rejudge"] != new_domain:
                note_parts.append(f"领域重判：{new_domain}→{h['domain_rejudge']}")
                new_domain = h["domain_rejudge"]
                new_dims = {}
                note_parts.append("维度清空（领域变更，旧维度词汇不迁移）")
        if "保持主标" in remark:
            note_parts.append("人工：保持主标")

        # 规范守卫 ①：relevant=no 不允许带维度（除非显式表态）
        if new_rel == "no" and new_dims:
            blocked.append(
                f"{tid}：relevant=no 仍带维度 {json.dumps(new_dims, ensure_ascii=False)}"
                f"（备注={remark or '空'}）——需写「清空维度」/「改相关=yes」/「保持主标」"
            )
            continue
        # 规范守卫 ②：无品牌行维度非空 → 必须品牌召回/类别级参考/显式表态
        if new_dims and not new_brand:
            if not (category_level or "保持主标" in remark or "类别级参考" in remark):
                blocked.append(
                    f"{tid}：无品牌仍带维度 {json.dumps(new_dims, ensure_ascii=False)}"
                    f"（备注={remark or '空'}）——需品牌召回 / 类别级参考=是 / 保持主标"
                )
                continue
        if (new_dims != old_dims or new_rel != old_rel
                or new_brand != (r.get("brand") or "").strip()
                or new_domain != r["domain"]
                or category_level or h["brand_recall"] or h["domain_rejudge"]):
            changes.append({
                "text_id": tid,
                "old_dimension_sentiments": old_dims,
                "new_dimension_sentiments": new_dims,
                "old_relevant": old_rel,
                "new_relevant": new_rel,
                "old_brand": (r.get("brand") or "").strip(),
                "new_brand": new_brand,
                "old_domain": r["domain"],
                "new_domain": new_domain,
                "note": "；".join(note_parts) or "人工判定",
                "remark": remark,
            })
            r["dimension_sentiments"] = json.dumps(new_dims, ensure_ascii=False)
            r["relevant"] = new_rel
            r["brand"] = new_brand
            r["domain"] = new_domain
            if note_parts:
                prev = (r.get("finalize_note") or "").strip()
                parts = [p for p in note_parts if p not in prev]
                if parts:
                    r["finalize_note"] = (prev + "；" if prev else "") + "；".join(parts)

    if blocked:
        print("以下行被规范守卫拦截（先处理再定版）：")
        for b in blocked:
            print("  -", b)
        return 1

    # 写回工作版（保留全部列；all_rows 的 dict 对象已在上面循环中被原地更新）
    fieldnames = list(all_rows[0].keys())
    with open(EDGE_SET_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in all_rows:
            w.writerow(r)

    # 写回入库版（去 URL/likes/time）
    fixture_cols = [c for c in fieldnames if c not in ("url", "likes", "time")]
    EDGE_SET_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    with open(EDGE_SET_FIXTURE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fixture_cols)
        w.writeheader()
        for r in all_rows:
            w.writerow({c: r.get(c, "") for c in fixture_cols})

    report = {
        "version": "v1.2 维度裁判版",
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "scope": "edge_set_v1.csv 全部 104 条带维度标注样本人工复核",
        "changed": len(changes),
        "kept_primary": 104 - len(changes),
        "changes": changes,
    }
    REVIEW_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"定版：{EDGE_SET_CSV.name}（v1.2 维度裁判版，共 {len(all_rows)} 条）")
    print(f"  维度标注变更：{len(changes)} 条（其余 {104 - len(changes)} 条认可主标）")
    print(f"  变更报告：{REVIEW_REPORT}")
    print(f"  入库版：{EDGE_SET_FIXTURE}")
    print("下一步：python tests/benchmark_golden.py --edge --no-record --no-history 重跑基线，"
          "确认指纹变更后重冻结 baseline_edge_*.json")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import argparse

    ap = argparse.ArgumentParser(description="2.4 前置：104 条维度标注人工复核")
    ap.add_argument("--gen", action="store_true", help="生成复核表（预填 2.3 人工判定）")
    ap.add_argument("--apply", action="store_true", help="应用人工判定，定版 v1.2")
    args = ap.parse_args()
    if not (args.gen or args.apply):
        ap.print_help()
        return 1
    if args.gen:
        return gen_worksheet()
    return apply_review()


if __name__ == "__main__":
    raise SystemExit(main())
