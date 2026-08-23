# -*- coding: utf-8 -*-
"""领域扩展提案引擎（2.5）。

流程：提案生成（模板建议 + 维度候选两件套）→ 人工确认表 → 应用落库（缓存）。

用法：
    python -m app.domains.proposer propose \
        --name "数码3C" --domain-id digital3c \
        --desc "手机/相机/电脑等数字硬件产品" \
        --brands "大疆,影石,联想,苹果,华为" \
        --evidence app/domains/evidence/digital3c.md \
        --out data/datasets/domain_proposal_digital3c.json
    python -m app.domains.proposer confirm-sheet --proposal <json> \
        --out data/datasets/domain_confirm_digital3c.xlsx
    python -m app.domains.proposer apply --sheet <xlsx> [--cache-dir data/domain_schemas]

证据来源：--evidence（agent-reach 调研产物，见 app/domains/evidence/）；
--search 可选（调用 mcporter/Exa 拉取网页证据，见 search_evidence()）。
人工确认是硬门槛：LLM/规则提案永远只是建议，apply 前必须过确认表。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import re
import subprocess
import sys
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

ROOT = Path(__file__).resolve().parents[2]

# 对象类型关键词 → 主导模板（2.5 规则主干；LLM 判断为可选增强）
TEMPLATE_RULES = {
    "physical": ["数码", "3c", "手机", "相机", "电脑", "笔记本", "耳机", "硬件",
                 "智能", "家电", "商品", "产品", "手表", "平板", "无人机"],
    "service": ["餐饮", "酒店", "服务", "门店", "售后", "平台", "出行", "外卖",
                "理发", "健身", "旅游"],
    "content": ["游戏", "影视", "剧", "综艺", "内容", "app", "软件", "阅读",
                "音乐", "视频", "课程"],
}
TEMPLATE_CN = {"content": "内容型", "physical": "实物型", "service": "服务型"}
CONFIRM_COLUMNS = ["保留", "维度id", "维度名", "关键词(逗号分隔)", "说明", "来源"]


def load_templates() -> dict[str, dict]:
    p = ROOT / "app" / "domains" / "domain_templates.json"
    return {t["id"]: t for t in json.loads(p.read_text(encoding="utf-8"))["templates"]}


def suggest_template(name: str, desc: str) -> tuple[str, list[str], float]:
    """按对象类型关键词规则建议主导模板（可选 LLM 判断留接口）。"""
    text = f"{name} {desc}".lower()
    scores = {}
    reasons = []
    for tid, kws in TEMPLATE_RULES.items():
        hit = [k for k in kws if k in text]
        scores[tid] = len(hit)
        if hit:
            reasons.append(f"{TEMPLATE_CN[tid]}：命中 {hit[:4]}")
    best = max(scores, key=scores.get) if any(scores.values()) else "physical"
    total = sum(scores.values())
    confidence = round(scores[best] / total, 2) if total else 0.5
    if not reasons:
        reasons.append("无对象类型关键词命中，默认 physical（实物型），请人工复核")
    return best, reasons, confidence


def parse_evidence(path: Path) -> list[dict]:
    """解析证据 md：合成维度候选表（| id | 候选维度（关键词） | 模板归属 | 证据 |）。"""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4 or cells[0] == "id" or cells[0].startswith("-") or "---" in cells[0]:
            continue
        dim_id, dim_cell, origin_cell, source = cells[0], cells[1], cells[2], cells[3]
        m = re.match(r"^(.+?)（(.*)）$", dim_cell)
        name = m.group(1).strip() if m else dim_cell
        keywords = [k.strip() for k in m.group(2).split("/") if k.strip()] if m else []
        origin = "domain"
        tm = re.search(r"template:(\w+)", origin_cell)
        if tm:
            origin = f"template:{tm.group(1)}"
        rows.append({
            "id": dim_id, "name": name, "keywords": keywords,
            "origin": origin, "source": source or "证据",
        })
    return rows


def build_proposal(name: str, domain_id: str, desc: str, brands: list[str],
                   evidence_rows: list[dict]) -> dict:
    template_id, reasons, confidence = suggest_template(name, desc)
    tpl = load_templates()[template_id]
    dimensions = []
    for d in tpl["dimensions"]:
        dimensions.append({
            "id": d["id"], "name": d["name"],
            "keywords": list(d.get("keywords") or []),
            "origin": f"template:{template_id}",
            "source": f"模板 {TEMPLATE_CN[template_id]}",
        })
    seen = {d["id"] for d in dimensions}
    for ev in evidence_rows:
        if ev["id"] in seen:
            continue
        dimensions.append({
            "id": ev["id"], "name": ev["name"],
            "keywords": ev["keywords"], "origin": ev["origin"],
            "source": ev["source"],
        })
        seen.add(ev["id"])
    return {
        "domain_id": domain_id,
        "name": name,
        "description": desc,
        "brands": brands,
        "template_id": template_id,
        "template_reason": reasons,
        "template_confidence": confidence,
        "dimensions": dimensions,
        "risks": [
            "维度候选来自规则+证据，未过人工确认前不作为正式 schema",
            "模板标准维度仍为草案（回测定稿见 2.5 收尾）",
        ],
    }


def write_confirm_sheet(proposal: dict, out: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "确认"
    ws.append(CONFIRM_COLUMNS)
    for c in ws[1]:
        c.fill = PatternFill("solid", fgColor="E2EFDA")
        c.font = Font(bold=True)
    for d in proposal["dimensions"]:
        ws.append(["是", d["id"], d["name"], "、".join(d["keywords"]),
                   d["source"], d["origin"]])
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    for i, wd in enumerate([8, 16, 18, 50, 20, 24], 1):
        ws.column_dimensions[chr(64 + i)].width = wd
    guide = wb.create_sheet("填写说明")
    for line in [
        "领域配方确认表（2.5）：每行一个候选维度。",
        "「保留」填 是/否：否 = 去掉该维度；是 = 保留。",
        "「维度id」建议英文小写下划线（如 battery），提交前可改。",
        "「关键词」用顿号/逗号分隔，是采集与维度识别的基础，请务必补充。",
        "确认后运行：python -m app.domains.proposer apply --sheet <本文件>",
    ]:
        guide.append([line])
    guide.column_dimensions["A"].width = 100
    wb.save(out)


def apply_sheet(sheet: Path, cache_dir: Path, proposal: dict | None = None) -> Path:
    from app.core.models import Dimension, DomainSchema
    from app.domains.loader import save_cached_schema

    wb = load_workbook(sheet, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    next(it)  # header
    dims = []
    try:
        for rec in it:
            keep = str(rec[0] or "").strip()
            if keep not in ("是", "yes", "y", "true"):
                continue
            dim_id = str(rec[1] or "").strip()
            name = str(rec[2] or "").strip()
            keywords = [k.strip() for k in re.split(r"[、,，]", str(rec[3] or "")) if k.strip()]
            source = str(rec[4] or "").strip()
            origin = str(rec[5] or "").strip() or "domain"
            dims.append(Dimension(id=dim_id, name=name, keywords=keywords,
                                  description=source, origin=origin))
    finally:
        wb.close()
    for d in dims:
        if not (d.id and d.name and d.keywords):
            raise ValueError(f"维度行不完整：{d.id or d.name or '?'}（id/名称/关键词必填）")
    if any(d.origin != "domain" and not d.origin.startswith("template:") for d in dims):
        raise ValueError("维度 origin 非法（只允许 domain / template:<id>）")
    if not dims:
        raise ValueError("确认表没有保留任何维度")
    ids = [d.id for d in dims]
    if len(ids) != len(set(ids)):
        raise ValueError(f"维度 id 重复：{ids}")
    if not (3 <= len(dims) <= 10):
        print(f"[提示] 维度数 {len(dims)} 超出 3~10 建议区间（超出需评审）")
    schema = DomainSchema(
        domain_id=(proposal or {}).get("domain_id", "digital3c"),
        domain_name=(proposal or {}).get("name", "数码3C"),
        version="1.0",
        template_id=(proposal or {}).get("template_id", "physical"),
        dimensions=dims,
    )
    return save_cached_schema(schema, cache_dir=cache_dir)


def search_evidence(query: str, node: str = "", cli: str = "", num: int = 6) -> list[dict]:
    """可选：调用 mcporter/Exa 拉取网页证据（agent-reach 集成入口）。

    返回 [{title,url,text}]；失败返回空并打印告警（不阻塞提案）。
    node/cli 为空时按 PATH / 环境变量（SMS_NODE_BIN、SMS_MCPORTER_CLI）解析。
    """
    node = node or os.environ.get("SMS_NODE_BIN") or shutil.which("node") or "node"
    cli = cli or os.environ.get("SMS_MCPORTER_CLI") or "mcporter"
    payload = json.dumps({"query": query, "numResults": num}, ensure_ascii=False)
    try:
        r = subprocess.run(
            [node, cli, "call", "exa.web_search_exa", "--args", payload],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        data = json.loads(r.stdout or "{}")
        return data.get("results") or data.get("data") or []
    except Exception as exc:
        print(f"[proposer] 搜索证据失败（不影响提案）：{exc}")
        return []


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="领域扩展提案引擎（2.5）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("propose", help="生成领域提案（模板建议 + 维度候选）")
    p.add_argument("--name", required=True)
    p.add_argument("--domain-id", required=True)
    p.add_argument("--desc", default="")
    p.add_argument("--brands", default="")
    p.add_argument("--evidence", type=Path)
    p.add_argument("--out", type=Path, required=True)

    p2 = sub.add_parser("confirm-sheet", help="生成人工确认表")
    p2.add_argument("--proposal", type=Path, required=True)
    p2.add_argument("--out", type=Path, required=True)

    p3 = sub.add_parser("apply", help="应用确认表，写入领域缓存 schema")
    p3.add_argument("--sheet", type=Path, required=True)
    p3.add_argument("--proposal", type=Path)
    p3.add_argument("--cache-dir", type=Path, default=ROOT / "data" / "domain_schemas")

    args = ap.parse_args()
    if args.cmd == "propose":
        ev = parse_evidence(args.evidence) if args.evidence and args.evidence.exists() else []
        proposal = build_proposal(
            args.name, args.domain_id, args.desc,
            [b.strip() for b in args.brands.split(",") if b.strip()], ev,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"提案已生成：{args.out}")
        print(f"  主导模板：{proposal['template_id']}（置信 {proposal['template_confidence']}）")
        print(f"  候选维度：{len(proposal['dimensions'])} 个")
        for d in proposal["dimensions"]:
            print(f"    - {d['id']} {d['name']} [{d['origin']}]")
        return 0
    if args.cmd == "confirm-sheet":
        proposal = json.loads(args.proposal.read_text(encoding="utf-8"))
        write_confirm_sheet(proposal, args.out)
        print(f"确认表已生成：{args.out}")
        return 0
    if args.cmd == "apply":
        proposal = (json.loads(args.proposal.read_text(encoding="utf-8"))
                    if args.proposal and args.proposal.exists() else None)
        path = apply_sheet(args.sheet, args.cache_dir, proposal)
        print(f"领域 schema 已写入缓存：{path}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
