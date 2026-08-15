# -*- coding: utf-8 -*-
"""边界样本专项集标注配套库（2.3 阶段 1）。

职责（docs/边界样本专项集方案.md §四/§六 复用主集规范）：
    - 标注工作表列契约：情感/强度列禁止预填（独立标注），子集列为抽样偏置提示；
    - 双 AI 结果加载与取值校验（枚举、必填联动）；
    - 一致性计算：整条情感一致率 + Cohen's kappa（手写，无第三方依赖）+
      维度级一致率 + 相关一致率 + 强度精确/±1；
    - 复核表生成：一致性核对表（随机 20% + 高歧义 100%）与人工抽检校准表（60 条，
      seed 可复现）；
    - 定版辅助：人工判定优先 + 未决分歧入争议集（finalize_rows 供 apply_edge_review 使用）。

入口脚本：
    tests/finalize_edge_set.py   --worksheets / --compare
    tests/apply_edge_review.py
"""

from __future__ import annotations

import csv
import json
import os
import random
import re
from pathlib import Path

from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[1]
DATASETS = Path(os.environ.get("EDGE_DATASETS_DIR") or (ROOT / "data" / "datasets"))
FIXTURES = Path(os.environ.get("EDGE_FIXTURES_DIR") or (ROOT / "tests" / "fixtures"))

EDGE_ANNOT_XLSX = DATASETS / "annotation_edge_v1.xlsx"
EDGE_ANNOT_CSV = DATASETS / "annotation_edge_v1.csv"
EDGE_PRIMARY_XLSX = DATASETS / "annotation_edge_v1_workbuddy.xlsx"
EDGE_SECONDARY_XLSX = DATASETS / "annotation_edge_v1_trae.xlsx"
EDGE_PRIMARY_CSV = DATASETS / "annotation_edge_v1_workbuddy.csv"
EDGE_SECONDARY_CSV = DATASETS / "annotation_edge_v1_trae.csv"

EDGE_SET_CSV = DATASETS / "edge_set_v1.csv"
EDGE_SET_FIXTURE = FIXTURES / "edge_set_v1.csv"
EDGE_DISPUTED_CSV = DATASETS / "edge_set_v1_disputed.csv"
EDGE_CONSISTENCY_REPORT = DATASETS / "edge_consistency_report_v1.json"
EDGE_REVIEW_CONSISTENCY_XLSX = DATASETS / "edge_review_consistency_v1.xlsx"
EDGE_REVIEW_CALIBRATION_XLSX = DATASETS / "edge_review_calibration_v1.xlsx"

VALID_SENTIMENTS = {"positive", "negative", "neutral", "mixed"}
VALID_INTENSITY = {"1", "2", "3", "4", "5"}
VALID_RELEVANT = {"yes", "no"}
VALID_FLAGS = {"反讽", "黑话", "方言", "官方", "广告", "emoji主导", "无观点"}

SENTIMENT_COL = "情感(整条)"
INTENSITY_COL = "强度(1-5)"
SUBSET_COL = "子集"
FLAG_COL = "语言现象"
RELEVANT_COL = "是否相关"

# 子集中文标签 → 英文键（抽样偏置组；与 sample_golden_set.EDGE_SUBSET_CN 镜像）
SUB_CN_TO_EN = {
    "反讽": "irony",
    "黑话": "jargon",
    "方言": "dialect",
    "长文本": "long",
    "emoji主导": "emoji",
    "低置信": "lowconf",
}
SUB_EN_TO_CN = {v: k for k, v in SUB_CN_TO_EN.items()}
SUBSET_ORDER = ("irony", "jargon", "dialect", "long", "emoji", "lowconf")

# 现象样本「品牌/主题」识别（2026-08-13 元数据修复，方案 A）
# 反讽/方言/emoji 子集由"现象信号词"采集（唔该/巴适/😡😡 太坑了 等），这些词不是真实
# 品牌；命中则标注/定版时「品牌/主题」列置空（关键词列保留真实搜索词），避免"是否与
# 品牌/主题相关"的判定口径失效。extra_keywords 可补充采集脚本的关键词表。
DIALECT_SIGNALS = ("唔该", "咁", "冇", "巴适", "安逸", "要得", "唠嗑", "咋整",
                   "好正", "好鬼正", "得顶")
IRONY_SIGNALS = ("呵呵", "太棒了", "真的会谢", "笑死", "绝了", "无语")
EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]")


def is_phenomenon_brand(brand: str, extra_keywords: frozenset = frozenset()) -> bool:
    """是否为现象采集关键词（非真实品牌）。"""
    b = norm(brand)
    if not b:
        return False
    if b in extra_keywords:
        return True
    if EMOJI_RE.search(b):
        return True
    if any(w in b for w in DIALECT_SIGNALS):
        return True
    if any(w in b for w in IRONY_SIGNALS):
        return True
    return False


def brand_display(brand: str) -> str:
    """复核表/定版展示：现象样本品牌置空后显示为「现象样本」。"""
    return norm(brand) or "现象样本"


# 定版 edge_set_v1.csv 列顺序（与 golden_set_v1 同构，额外加 subset）
EDGE_COLUMNS = [
    "text_id", "text", "platform", "domain", "brand", "keyword", "kind", "is_reply",
    "subset", "sentiment", "intensity", "dimension_sentiments", "language_flags",
    "relevant", "remark", "annotator", "annotated_at", "url", "time", "likes",
    "batch", "finalize_note", "gold_revised_by_model",
]

# 复核表列（一致性核对表 / 人工抽检校准表共用）
REVIEW_COLUMNS = [
    "序号", "text_id", "原文", "平台", "领域", "品牌/主题", "子集",
    "主标情感", "副标情感", "主标强度", "副标强度",
    "主标维度", "副标维度", "主标相关", "副标相关",
    "主标语言现象", "副标语言现象", "分歧点",
    "人工判定情感", "人工判定强度", "人工判定维度",
    "人工判定相关", "人工判定语言现象", "是否参考过模型判定",
    "备注", "复核人", "复核日期",
]

# 模型错误人工验收表（edge_2.3_acceptance_review 样式，V2 验证纪律）：
# 「是否参考过模型判定」用于 gold 修订敏感性复算；--blind 时省略模型判定列。
ACCEPTANCE_COLUMNS = [
    "序号", "text_id", "原文", "平台", "领域", "子集", "gold情感",
    "模型判定情感", "LLM修正(是/否)",
    "人工判定情感", "是否参考过模型判定", "备注", "复核人", "复核日期",
]
ACCEPTANCE_MODEL_COLS = ("模型判定情感", "LLM修正(是/否)")


def norm(v) -> str:
    return "" if v is None else str(v).strip()


def norm_sent(v) -> str:
    return norm(v).lower()


def norm_relevant(v) -> str:
    raw = norm(v).lower()
    return {"是": "yes", "否": "no", "y": "yes", "n": "no", "true": "yes",
            "false": "no"}.get(raw, raw)


def parse_flags(raw) -> list[str]:
    """语言现象多选解析（兼容中文逗号/顿号）。"""
    out = []
    for x in str(raw or "").replace("，", ",").replace("、", ",").split(","):
        x = x.strip()
        if x and x not in out:
            out.append(x)
    return out


def parse_dims_dict(raw) -> dict:
    """维度情感解析：JSON dict 优先；兼容 '名称:值' / '名称=值' 对。"""
    raw = norm(raw)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {norm(k): norm_sent(v) for k, v in data.items() if norm(k) and norm_sent(v)}
    except (ValueError, TypeError):
        pass
    out = {}
    for pair in raw.replace("；", ";").replace("，", ",").split(";"):
        for sep in (":", "=", "："):
            if sep in pair:
                k, v = pair.split(sep, 1)
                k, v = norm(k), norm_sent(v)
                if k and v:
                    out[k] = v
                break
    return out


def dimension_columns(headers: list[str]) -> list[str]:
    """维度列 = 「子集」之后、「语言现象」之前的列（动态取，防列漂移）。"""
    try:
        i0 = list(headers).index(SUBSET_COL) + 1
        i1 = list(headers).index(FLAG_COL)
    except ValueError:
        return []
    return [h for h in headers[i0:i1] if h]


def parse_annotation_row(r: dict, dim_cols: list[str]) -> dict:
    """把一行标注（CSV dict 或 xlsx 行 zip 后的 dict）解析为规范化结构。"""
    flags = parse_flags(r.get(FLAG_COL))
    dims = {}
    for c in dim_cols:
        v = norm_sent(r.get(c))
        if v:
            dims[c] = v
    return {
        "text_id": norm(r.get("text_id")),
        "text": norm(r.get("原文")),
        "platform": norm(r.get("平台")),
        "domain": norm(r.get("领域")),
        "brand": norm(r.get("品牌/主题")),
        "keyword": norm(r.get("关键词")),
        "kind": norm(r.get("文本类型")),
        "is_reply": norm(r.get("是否楼中楼")),
        "subset_cn": norm(r.get(SUBSET_COL)),
        "sentiment": norm_sent(r.get(SENTIMENT_COL)),
        "intensity": norm(r.get(INTENSITY_COL)),
        "dims": dims,
        "flags": flags,
        "relevant": norm_relevant(r.get(RELEVANT_COL)),
        "remark": norm(r.get("备注")),
        "annotator": norm(r.get("标注人")),
        "annotated_at": norm(r.get("标注日期")),
        "url": norm(r.get("链接")),
        "time": norm(r.get("发布时间")),
        "likes": norm(r.get("点赞数")),
        "batch": norm(r.get("采集批次")),
    }


def load_annotation_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        dim_cols = dimension_columns(reader.fieldnames or [])
        for r in reader:
            if not norm(r.get("text_id")):
                continue
            rows.append(parse_annotation_row(r, dim_cols))
    return rows


def load_annotation_xlsx(path: Path) -> list[dict]:
    wb = load_workbook(path, read_only=True, data_only=True)
    rows = []
    try:
        for sheet in ("游戏标注", "消费品标注"):
            if sheet not in wb.sheetnames:
                continue
            ws = wb[sheet]
            it = ws.iter_rows(values_only=True)
            hdr = list(next(it, []))
            dim_cols = dimension_columns([str(h) if h else "" for h in hdr])
            for r in it:
                if not r or not norm(r[0] if r else None):
                    continue
                rec = {str(h) if h else "": (v if v is not None else "") for h, v in zip(hdr, r)}
                rows.append(parse_annotation_row(rec, dim_cols))
    finally:
        wb.close()
    return rows


def load_annotation(path: Path) -> list[dict]:
    if path.suffix.lower() == ".xlsx":
        return load_annotation_xlsx(path)
    return load_annotation_csv(path)


def pick_annotated_input(xlsx: Path, csv_path: Path) -> Path:
    """主标/副标输入选择：优先已填情感最多的文件。

    同一样本会同时生成 xlsx（带下拉校验）与 CSV 副本，标注者可能只填其中一种；
    固定优先 xlsx 会误读空白副本，故按实际填写量选择。
    """
    def filled(p: Path) -> int:
        if not p.exists():
            return -1
        try:
            return sum(1 for r in load_annotation(p) if r["sentiment"])
        except Exception:
            return -1

    return xlsx if filled(xlsx) >= filled(csv_path) else csv_path


def validate_rows(rows: list[dict]) -> list[str]:
    """取值校验：枚举值、情感/强度必填联动、维度取值。返回错误清单。"""
    errors = []
    for r in rows:
        tid = r["text_id"]
        if r["sentiment"] and r["sentiment"] not in VALID_SENTIMENTS:
            errors.append(f"{tid} 情感取值非法：{r['sentiment']}")
        if r["sentiment"] and not r["intensity"]:
            errors.append(f"{tid} 情感已填但强度为空")
        if r["intensity"] and r["intensity"] not in VALID_INTENSITY:
            errors.append(f"{tid} 强度取值非法：{r['intensity']}")
        if r["relevant"] and r["relevant"] not in VALID_RELEVANT:
            errors.append(f"{tid} 是否相关取值非法：{r['relevant']}")
        for k, v in r["dims"].items():
            if v not in ("positive", "negative"):
                errors.append(f"{tid} 维度[{k}]取值非法：{v}")
        for f in r["flags"]:
            if f not in VALID_FLAGS:
                errors.append(f"{tid} 语言现象取值非法：{f}")
    return errors


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    """Cohen's kappa（手写实现，无第三方依赖）。"""
    if not a or len(a) != len(b):
        return None
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    labels = set(a) | set(b)
    pe = sum((a.count(l) / n) * (b.count(l) / n) for l in labels)
    if pe == 1:
        return 1.0 if po == 1 else 0.0
    return (po - pe) / (1 - pe)


def _subset_key(row: dict) -> str:
    return SUB_CN_TO_EN.get(row["subset_cn"], row["subset_cn"] or "")


def merge_compare(primary: list[dict], secondary: list[dict]) -> dict:
    """双 AI 合并与一致性计算（全量 300 条两两比对）。"""
    p = {r["text_id"]: r for r in primary}
    s = {r["text_id"]: r for r in secondary}
    ids = [r["text_id"] for r in primary if r["text_id"] in s]

    sent_pairs = [(p[i]["sentiment"], s[i]["sentiment"]) for i in ids]
    rel_pairs = [(p[i]["relevant"], s[i]["relevant"]) for i in ids]
    int_pairs = [(p[i]["intensity"], s[i]["intensity"]) for i in ids]

    dim_exact = dim_n = 0
    for i in ids:
        da, db = p[i]["dims"], s[i]["dims"]
        if da or db:
            dim_n += 1
            if da == db:
                dim_exact += 1

    sent_agree = sum(a == b for a, b in sent_pairs)
    rel_agree = sum(a == b for a, b in rel_pairs)
    int_exact = sum(a == b for a, b in int_pairs)
    int_within1 = sum(
        a == b or (a.isdigit() and b.isdigit() and abs(int(a) - int(b)) <= 1)
        for a, b in int_pairs
    )

    by_subset: dict[str, dict] = {}
    for i in ids:
        key = _subset_key(p[i]) or _subset_key(s[i])
        by_subset.setdefault(key, {"n": 0, "agree": 0})
        by_subset[key]["n"] += 1
        if p[i]["sentiment"] == s[i]["sentiment"]:
            by_subset[key]["agree"] += 1

    high_ambiguity: list[dict] = []
    disputes: list[dict] = []
    for i in ids:
        a, b = p[i], s[i]
        reasons = []
        if _subset_key(a) == "irony" or _subset_key(b) == "irony":
            reasons.append("反讽子集")
        if "mixed" in (a["sentiment"], b["sentiment"]):
            reasons.append("mixed")
        if "no" in (a["relevant"], b["relevant"]):
            reasons.append("relevant=no")
        if reasons:
            high_ambiguity.append({
                "text_id": i, "subset": _subset_key(a) or _subset_key(b),
                "reasons": reasons,
            })

        diff = []
        if a["sentiment"] != b["sentiment"]:
            diff.append(f"情感 {a['sentiment'] or '空'}≠{b['sentiment'] or '空'}")
        if a["relevant"] != b["relevant"]:
            diff.append(f"相关 {a['relevant'] or '空'}≠{b['relevant'] or '空'}")
        if a["intensity"] != b["intensity"]:
            diff.append(f"强度 {a['intensity'] or '空'}≠{b['intensity'] or '空'}")
        if a["dims"] != b["dims"]:
            diff.append(f"维度 {a['dims'] or '空'}≠{b['dims'] or '空'}")
        if a["flags"] != b["flags"]:
            diff.append(f"语言现象 {a['flags'] or '空'}≠{b['flags'] or '空'}")
        if diff:
            disputes.append({"text_id": i, "diff": diff})

    n = len(ids)
    sent_a = [x[0] for x in sent_pairs]
    sent_b = [x[1] for x in sent_pairs]
    return {
        "n": n,
        "missing_primary": sorted(set(s) - set(p)),
        "missing_secondary": sorted(set(p) - set(s)),
        "sentiment_agreement": round(sent_agree / n, 4) if n else None,
        "kappa": round(cohen_kappa(sent_a, sent_b), 4) if n else None,
        "relevance_agreement": round(rel_agree / n, 4) if n else None,
        "intensity_exact": round(int_exact / n, 4) if n else None,
        "intensity_within1": round(int_within1 / n, 4) if n else None,
        "dimension_agreement": round(dim_exact / dim_n, 4) if dim_n else None,
        "dimension_n": dim_n,
        "by_subset": {
            k: {"n": v["n"], "agreement": round(v["agree"] / v["n"], 4) if v["n"] else None}
            for k, v in sorted(by_subset.items())
        },
        "high_ambiguity": high_ambiguity,
        "disputes": disputes,
        "达标线": {
            "整条情感一致率": {"value": round(sent_agree / n, 4) if n else None, "target": 0.8},
            "Cohen's kappa": {"value": round(cohen_kappa(sent_a, sent_b), 4) if n else None, "target": 0.6},
            "维度级一致率": {"value": round(dim_exact / dim_n, 4) if dim_n else None, "target": 0.75},
        },
    }


def _rows_by_subset(rows: list[dict]) -> dict[str, list[str]]:
    out = {k: [] for k in SUBSET_ORDER}
    for r in rows:
        key = SUB_CN_TO_EN.get(r["subset_cn"], "")
        if key:
            out.setdefault(key, []).append(r["text_id"])
    return out


def build_review_sheets(
    primary: list[dict],
    secondary: list[dict],
    merge_report: dict,
    seed: int = 42,
) -> tuple[list[dict], list[dict]]:
    """生成两套人工复核表（仅 text_id 清单，行内容由调用方组装）。

    一致性核对表：每子集 10 条分层随机（共 60）+ 高歧义 100%；
    人工抽检校准表：高歧义优先至 12 条 + 分层随机补足至 60 条（seed 可复现）。
    返回 (consistency_ids, calibration_ids)。
    """
    by_sub = _rows_by_subset(primary)
    high_ids = [h["text_id"] for h in merge_report.get("high_ambiguity", [])]
    high_set = set(high_ids)

    rng1 = random.Random(seed)
    stratified60 = []
    for sub in SUBSET_ORDER:
        cands = [t for t in by_sub.get(sub, []) if t not in high_set]
        rng1.shuffle(cands)
        stratified60.extend(cands[:10])
    consistency_ids = list(dict.fromkeys(stratified60 + high_ids))

    rng2 = random.Random(seed + 1)
    calibration_ids: list[str] = []
    taken: set[str] = set()
    for tid in high_ids[:12]:
        calibration_ids.append(tid)
        taken.add(tid)
    need = 60 - len(calibration_ids)
    per_sub = max(need // len(SUBSET_ORDER), 1)
    for sub in SUBSET_ORDER:
        cands = [t for t in by_sub.get(sub, []) if t not in taken]
        rng2.shuffle(cands)
        add = cands[:per_sub]
        calibration_ids.extend(add)
        taken.update(add)
    if len(calibration_ids) < 60:
        extra = [t for t in high_ids[12:] if t not in taken]
        extra += [t for sub in SUBSET_ORDER for t in by_sub.get(sub, []) if t not in taken]
        for tid in extra:
            if len(calibration_ids) >= 60:
                break
            calibration_ids.append(tid)
            taken.add(tid)
    return consistency_ids, calibration_ids[:60]


def review_row(seq: int, tid: str, primary: dict, secondary: dict) -> dict:
    a = primary.get(tid) or {}
    b = secondary.get(tid) or {}
    diff = []
    if a.get("sentiment") != b.get("sentiment"):
        diff.append("情感")
    if a.get("relevant") != b.get("relevant"):
        diff.append("相关")
    if a.get("intensity") != b.get("intensity"):
        diff.append("强度")
    if a.get("dims") != b.get("dims"):
        diff.append("维度")
    if a.get("flags") != b.get("flags"):
        diff.append("语言现象")
    return {
        "序号": seq,
        "text_id": tid,
        "原文": a.get("text", ""),
        "平台": a.get("platform", ""),
        "领域": a.get("domain", ""),
        "品牌/主题": brand_display(a.get("brand", "")),
        "子集": a.get("subset_cn", ""),
        "主标情感": a.get("sentiment", ""),
        "副标情感": b.get("sentiment", ""),
        "主标强度": a.get("intensity", ""),
        "副标强度": b.get("intensity", ""),
        "主标维度": json.dumps(a.get("dims", {}), ensure_ascii=False),
        "副标维度": json.dumps(b.get("dims", {}), ensure_ascii=False),
        "主标相关": a.get("relevant", ""),
        "副标相关": b.get("relevant", ""),
        "主标语言现象": "、".join(a.get("flags", [])),
        "副标语言现象": "、".join(b.get("flags", [])),
        "分歧点": "、".join(diff),
        "人工判定情感": "",
        "人工判定强度": "",
        "人工判定维度": "",
        "人工判定相关": "",
        "人工判定语言现象": "",
        "是否参考过模型判定": "",
        "备注": "",
        "复核人": "",
        "复核日期": "",
    }


def parse_human_row(r: dict) -> dict:
    """解析复核表一行的「人工判定」列。留空表示认可主标。"""
    return {
        "text_id": norm(r.get("text_id")),
        "sentiment": norm_sent(r.get("人工判定情感")),
        "intensity": norm(r.get("人工判定强度")),
        "dims": parse_dims_dict(r.get("人工判定维度")),
        "relevant": norm_relevant(r.get("人工判定相关")),
        "flags": parse_flags(r.get("人工判定语言现象")),
        "gold_revised_by_model": norm(r.get("是否参考过模型判定")),
        "remark": norm(r.get("备注")),
        "reviewer": norm(r.get("复核人")),
        "reviewed_at": norm(r.get("复核日期")),
    }


def load_review_xlsx(path: Path) -> list[dict]:
    wb = load_workbook(path, read_only=True, data_only=True)
    out = []
    try:
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        hdr = [str(h) if h else "" for h in next(it, [])]
        for r in it:
            if not r or not norm(r[0] if r else None):
                continue
            rec = {h: (v if v is not None else "") for h, v in zip(hdr, r)}
            out.append(parse_human_row(rec))
    finally:
        wb.close()
    return out


def _finalize_one(
    tid: str,
    primary: dict,
    secondary: dict,
    human: dict | None,
    reviewed: bool,
    note_parts: list[str],
):
    """单条定版：人工判定优先；情感/相关未决分歧 → 返回 (None, dispute_reason)。"""
    a, b = primary, secondary
    sent = a.get("sentiment", "")
    relevant = a.get("relevant", "")
    intensity = a.get("intensity", "")
    dims = a.get("dims", {})
    flags = list(a.get("flags", []))
    remark = a.get("remark", "")

    if human:
        if human["sentiment"]:
            if human["sentiment"] != sent:
                note_parts.append(f"情感 {sent or '空'}→{human['sentiment']}")
            sent = human["sentiment"]
        if human["relevant"]:
            if human["relevant"] != relevant:
                note_parts.append(f"相关 {relevant or '空'}→{human['relevant']}")
            relevant = human["relevant"]
        if human["intensity"]:
            if human["intensity"] != intensity:
                note_parts.append(f"强度 {intensity or '空'}→{human['intensity']}")
            intensity = human["intensity"]
        if human["dims"] or human["dims"] == {}:
            if human["dims"] != dims:
                note_parts.append(f"维度 {dims or '{}'}→{human['dims']}")
            dims = human["dims"]
        if human["flags"]:
            flags = human["flags"]
        if human["remark"]:
            remark = (remark + "；" if remark else "") + f"人工复核:{human['remark']}"
        if human["reviewer"]:
            note_parts.append(f"人工:{human['reviewer']}")

    # 未决分歧判定：仅当该行完全没进过复核表（人工没见过）时，双 AI 不一致才入争议集；
    # 复核表里留空 = 按填写说明「留空 = 认可主标」，直接采用主标。
    if not reviewed and (not human or not human["sentiment"]) and b.get("sentiment") != sent:
        return None, f"情感分歧未决：{sent or '空'}≠{b.get('sentiment') or '空'}"
    if not reviewed and (not human or not human["relevant"]) and b.get("relevant") != relevant:
        return None, f"相关分歧未决：{relevant or '空'}≠{b.get('relevant') or '空'}"

    # 口径归一：neutral 强度必须为 1（抽样与标注规范 §四；主集/双 AI 均按此标准化）
    if sent == "neutral" and str(intensity or "") != "1":
        note_parts.append(f"强度 {intensity or '空'}→1（neutral 口径）")
        intensity = "1"

    return {
        "text_id": tid,
        "text": a.get("text", ""),
        "platform": a.get("platform", ""),
        "domain": a.get("domain", ""),
        "brand": a.get("brand", ""),
        "keyword": a.get("keyword", ""),
        "kind": a.get("kind", ""),
        "is_reply": a.get("is_reply", ""),
        "subset": SUB_CN_TO_EN.get(a.get("subset_cn", ""), a.get("subset_cn", "")),
        "sentiment": sent,
        "intensity": intensity,
        "dimension_sentiments": json.dumps(dims, ensure_ascii=False),
        "language_flags": "、".join(flags),
        "relevant": relevant,
        "remark": remark,
        "annotator": a.get("annotator", ""),
        "annotated_at": a.get("annotated_at", ""),
        "url": a.get("url", ""),
        "time": a.get("time", ""),
        "likes": a.get("likes", ""),
        "batch": a.get("batch", ""),
        "finalize_note": "；".join(note_parts),
        "gold_revised_by_model": (
            (human or {}).get("gold_revised_by_model", "")
        ),
    }, None


def finalize_rows(
    primary: list[dict],
    secondary: list[dict],
    human_decisions: dict[str, dict],
    reviewed_ids: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """定版合并：返回 (final_rows, disputed_rows)。

    规则：
    - 人工判定（一致性核对表 + 人工抽检校准表）优先，以人工为准；
    - 双 AI 情感/相关分歧且无人工判定 → 争议集（不进主评分）；
    - 其余字段（强度/维度/语言现象）以主标为准，人工填写则覆盖。
    """
    p = {r["text_id"]: r for r in primary}
    s = {r["text_id"]: r for r in secondary}
    reviewed_ids = reviewed_ids or set()
    final_rows = []
    disputed_rows = []
    for tid in p:
        note_parts = []
        row, reason = _finalize_one(
            tid, p[tid], s.get(tid) or {}, human_decisions.get(tid),
            tid in reviewed_ids, note_parts,
        )
        if row is None:
            disputed_rows.append({
                "text_id": tid,
                "text": p[tid].get("text", ""),
                "primary_sentiment": p[tid].get("sentiment", ""),
                "secondary_sentiment": (s.get(tid) or {}).get("sentiment", ""),
                "primary_relevant": p[tid].get("relevant", ""),
                "secondary_relevant": (s.get(tid) or {}).get("relevant", ""),
                "primary_dims": json.dumps(p[tid].get("dims", {}), ensure_ascii=False),
                "secondary_dims": json.dumps((s.get(tid) or {}).get("dims", {}), ensure_ascii=False),
                "primary_flags": "、".join(p[tid].get("flags", [])),
                "secondary_flags": "、".join((s.get(tid) or {}).get("flags", [])),
                "subset": SUB_CN_TO_EN.get(p[tid].get("subset_cn", ""), p[tid].get("subset_cn", "")),
                "reason": reason or "分歧未决",
            })
            continue
        final_rows.append(row)
    return final_rows, disputed_rows


def write_edge_set_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=EDGE_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in EDGE_COLUMNS})


def write_disputed_csv(rows: list[dict], path: Path) -> None:
    cols = [
        "text_id", "text", "subset",
        "primary_sentiment", "secondary_sentiment",
        "primary_relevant", "secondary_relevant",
        "primary_dims", "secondary_dims",
        "primary_flags", "secondary_flags", "reason",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def gen_acceptance_review_xlsx(errors: list[dict], out: Path, blind: bool = False) -> None:
    """生成模型错误人工验收表（V2 验证纪律，edge_2.3_acceptance_review 样式）。

    列含「是否参考过模型判定（是/否）」——人工修订 gold 时须披露是否看过模型判定；
    blind=True 省略「模型判定情感/LLM修正」两列（更严格的盲审档）。
    """
    from openpyxl.styles import Alignment, Font, PatternFill

    cols = (
        [c for c in ACCEPTANCE_COLUMNS if c not in ACCEPTANCE_MODEL_COLS]
        if blind else list(ACCEPTANCE_COLUMNS)
    )
    wb = Workbook()
    ws = wb.active
    ws.title = "错误样本验收"
    ws.append(cols)
    for cell in ws[1]:
        cell.fill = PatternFill("solid", fgColor="E2EFDA")
        cell.font = Font(bold=True)
    for i, e in enumerate(errors, 1):
        row = {
            "序号": i,
            "text_id": e.get("text_id", ""),
            "原文": e.get("text", ""),
            "平台": e.get("platform", ""),
            "领域": e.get("domain", ""),
            "子集": e.get("subset", ""),
            "gold情感": e.get("gold", ""),
            "模型判定情感": e.get("pred", ""),
            "LLM修正(是/否)": "是" if e.get("llm_used") else "否",
            "人工判定情感": "",
            "是否参考过模型判定": "",
            "备注": "",
            "复核人": "",
            "复核日期": "",
        }
        ws.append([row.get(c, "") for c in cols])
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    guide = wb.create_sheet("填写说明")
    lines = [
        "模型错误人工验收表（V2 验证纪律）。",
        "一、「人工判定情感」：填写你认为正确的最终情感（positive/negative/neutral）；留空 = 认可 gold。",
        "二、「是否参考过模型判定」：修订/确认时是否看过「模型判定情感」列？看过填「是」，没看填「否」。",
        "    - 该字段用于敏感性复算：gold 修订参考过模型的行可被",
        "      benchmark_golden.py --exclude-gold-revised-by-model 剔除后再统计。",
        "三、盲审更严格：生成时加 --blind 会省略模型判定列，默认应填「否」。",
        "四、填完运行 tests/apply_edge_acceptance_revision.py --sheet <本文件> 应用到 edge_set。",
    ]
    for line in lines:
        guide.append([line])
    guide.column_dimensions["A"].width = 110
    wb.save(out)
    print(f"验收表已生成：{out}（{len(errors)} 条，blind={blind}）")


def load_acceptance_xlsx(path: Path) -> list[dict]:
    """读取模型错误人工验收表：人工判定情感 + 是否参考过模型判定。"""
    wb = load_workbook(path, read_only=True, data_only=True)
    out = []
    try:
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        hdr = [str(h) if h else "" for h in next(it, [])]
        for r in it:
            if not r or not norm(r[0] if r else None):
                continue
            rec = {h: (v if v is not None else "") for h, v in zip(hdr, r)}
            out.append({
                "text_id": norm(rec.get("text_id")),
                "gold": norm_sent(rec.get("gold情感")),
                "pred": norm_sent(rec.get("模型判定情感")),
                "human_sentiment": norm_sent(rec.get("人工判定情感")),
                "gold_revised_by_model": norm(rec.get("是否参考过模型判定")),
                "remark": norm(rec.get("备注")),
                "reviewer": norm(rec.get("复核人")),
                "reviewed_at": norm(rec.get("复核日期")),
            })
    finally:
        wb.close()
    return out
