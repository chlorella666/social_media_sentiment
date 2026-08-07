# -*- coding: utf-8 -*-
"""黄金集抽样脚本：从真实采集报告中抽取标注工作表（含清洗验证集）。

规范依据：docs/抽样与标注规范.md（v0.2）

用法：
    python tests/sample_golden_set.py                # 默认 seed=42，目标 200 条
    python tests/sample_golden_set.py --seed 7 --total 200
    python tests/sample_golden_set.py --trial 20      # 额外生成游戏域试标集（两人对齐口径用）
    python tests/sample_golden_set.py --trial 20 --trial-domain consumer
    python tests/sample_golden_set.py --force         # 覆盖已存在的产物文件（默认跳过，防覆盖标注结果）

产物（data/ 目录不入库）：
    data/datasets/annotation_worksheet_v1_game.csv / _consumer.csv  标注主集
    data/datasets/annotation_worksheet_v1.xlsx                      宽表（含下拉校验与说明页）
    data/datasets/cleaning_worksheet_v1.csv / .xlsx                 清洗验证集
    data/datasets/trial_alignment_v1_game.csv / .xlsx               试标集（--trial 时生成）
    data/datasets/sampling_report_v1.json                           配额达成报告

设计要点：
    - 只读 Excel 的"原始数据/评论明细/丢弃明细"，不含词典/LLM 判定结果
    - 跨多个真实采集批次汇总样本池（跳过 demo 渠道），按批次优先级去重
    - 配额：每渠道 50 × 2 领域各 50%；文本类型 帖子40%/评论50%/楼中楼10%
    - 边缘样本（长文本/黑话/官方/多维度/混合语体/疑似反讽）加权采样 + 定向补齐
    - 同一帖子最多取 3 条评论；固定随机种子保证可复现
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "data" / "reports"
OUT_DIR = ROOT / "data" / "datasets"
DOMAINS_DIR = ROOT / "app" / "domains"

DEFAULT_SEED = 42
DEFAULT_TOTAL = 200
CHANNEL_TARGET = 50          # 每个渠道目标条数
DOMAIN_TARGET = DEFAULT_TOTAL // 2   # 每领域 100
TYPE_TARGET = {"post": int(DEFAULT_TOTAL * 0.40),
               "comment": int(DEFAULT_TOTAL * 0.50),
               "reply": int(DEFAULT_TOTAL * 0.10)}
CELL_TARGET = 25             # 渠道 × 领域 = 8 格，各 25

EDGE_TARGETS = {
    "long": 0.15,          # >100 字长文本
    "slang": 0.10,         # 网络黑话
    "official": 0.10,      # 官方/广告内容
    "multi_dimension": 0.15,  # 多维度文本
    "mixed_script": 0.05,  # 中英/繁体/emoji 混杂
    "irony_hint": 0.10,    # 疑似反讽（启发式，仅供采样偏置）
}

BRAND_DOMAIN = {"恋与深空": "game"}
REPLY_MARK = "楼中楼"
PLATFORM_GROUP = ("websearch", "websearch_zhihu", "websearch_tieba", "websearch_taptap")

SLANG_WORDS = [
    "绝绝子", "yyds", "YYDS", "塌房", "割韭菜", "智商税", "暴雷", "背刺", "离谱",
    "上头", "劝退", "真香", "破防", "绷不住", "冲爆", "氪金", "白嫖", "晒单", "打卡",
    "种草", "拔草", "踩雷", "闭眼入", "狠狠", "雷点", "亮点", "翻车", "退坑", "入坑",
    "麻了", "带节奏", "水军", "控评", "下头", "爹味", "普信", "摆烂", "内卷", "裂开",
    "蚌埠住了", "天花板", "封神", "吹爆", "手慢无", "神仙打架", "巨坑", "无语", "膈应",
]
OFFICIAL_WORDS = [
    "官方", "公告", "宣发", "发布会", "首页", "欢迎您", "声明", "客服", "召回",
    "新品发布", "更新公告", "品牌故事", "免责", "服务协议",
]
IRONY_HINTS = [
    "呵呵", "笑死", "真的会谢", "太棒了", "真棒", "太好了", "绝了", "厉害",
    "服了", "感人", "优秀", "真是谢谢",
]
TRADITIONAL_CHARS = set(
    "這裡臺麼嗎說會個時來為與後點對體們發沒問題錯樂憂國學進過還動開關萬億經濟體驗價格質量品質廣告客戶服務"
)
EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")
LATIN_RE = re.compile(r"[A-Za-z]")


# ---------------------------------------------------------------------------
# 样本池构建
# ---------------------------------------------------------------------------

def run_short_key(run_name: str) -> str:
    m = re.search(r"v(\d)", run_name)
    if m:
        return "v" + m.group(1)
    if "manual" in run_name:
        return "manual"
    if run_name.startswith("验收试跑_") or run_name == "验收试跑":
        return "v1"
    return "run"


def run_priority(run_dir: Path) -> tuple:
    name = run_dir.name
    order = {
        "验收试跑v4_20260806_010815": 0,
        "验收试跑v3_20260806_002303": 1,
        "验收试跑v3_20260806_001943": 1,
        "验收试跑v2_20260805_013458": 2,
        "验收试跑manual_20260806_163955": 3,
        "验收试跑_20260804_190639": 4,
    }
    return (order.get(name, 10), name)


def norm_key(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()[:50]


def read_plan(run_dir: Path, brand_dir: Path | None = None):
    """读取批次计划（用于排除 demo 与识别领域）。"""
    candidates = []
    if brand_dir is not None:
        candidates.append(brand_dir / "result.json")
    candidates.append(run_dir / "result.json")
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8")).get("plan", {})
            except Exception:
                pass
    return {}


def domain_of(plan: dict, keyword: str) -> str:
    if plan.get("domain_id") in ("game", "consumer"):
        return plan["domain_id"]
    for brand, dom in BRAND_DOMAIN.items():
        if brand in (keyword or ""):
            return dom
    return "consumer"


def iter_excel_batches():
    """产出 (run_dir, xlsx_path, batch_label) 的真实采集批次。"""
    if not REPORTS_DIR.exists():
        return
    for run_dir in sorted(REPORTS_DIR.iterdir(), key=run_priority):
        if not run_dir.is_dir() or run_dir.name == "archive":
            continue
        for xlsx in sorted(run_dir.rglob("*.xlsx")):
            yield run_dir, xlsx


def load_domain_keywords() -> dict[str, dict[str, list[str]]]:
    """{domain: {dimension_name: [keywords]}}。"""
    result = {}
    for dom in ("game", "consumer"):
        path = DOMAINS_DIR / f"{dom}.json"
        if not path.exists():
            result[dom] = {}
            continue
        schema = json.loads(path.read_text(encoding="utf-8"))
        result[dom] = {
            dim["name"]: dim.get("keywords", [])
            for dim in schema.get("dimensions", [])
        }
    return result


def dimension_names(domain_keywords: dict) -> dict[str, list[str]]:
    return {dom: list(dims.keys()) for dom, dims in domain_keywords.items()}


def edge_flags(text: str, domain: str, domain_keywords: dict) -> set[str]:
    flags: set[str] = set()
    t = text or ""
    if len(t) > 100:
        flags.add("long")
    if any(w in t for w in SLANG_WORDS):
        flags.add("slang")
    if any(w in t for w in OFFICIAL_WORDS):
        flags.add("official")
    if LATIN_RE.search(t) or any(c in TRADITIONAL_CHARS for c in t) or EMOJI_RE.search(t):
        flags.add("mixed_script")
    if any(w in t for w in IRONY_HINTS):
        flags.add("irony_hint")
    hit_dims = [name for name, kws in domain_keywords.get(domain, {}).items() if any(k in t for k in kws)]
    if len(hit_dims) >= 2:
        flags.add("multi_dimension")
    return flags


def build_pool(domain_keywords: dict):
    """跨批次汇总并去重，返回 (posts, comments, replies)。"""
    posts, comments, replies = [], [], []
    seen_posts: set[str] = set()
    seen_comments: set[tuple[str, str]] = set()
    seen_text: set[str] = set()
    idx = {"post": 0, "comment": 0, "reply": 0}

    for run_dir, xlsx in iter_excel_batches():
        batch_label = str(xlsx.parent.relative_to(REPORTS_DIR))
        run_key = run_short_key(run_dir.name)
        wb = load_workbook(xlsx, read_only=True)
        if "原始数据" not in wb.sheetnames:
            continue
        plan = read_plan(run_dir, xlsx.parent if xlsx.parent != run_dir else None)
        channels = [c.get("channel_id") for c in plan.get("channels", [])]
        if channels and set(channels) <= {"demo"}:
            continue

        def header_map(sheet):
            rows = sheet.iter_rows(values_only=True)
            hdr = next(rows, None)
            return hdr, list(rows), {name: i for i, name in enumerate(hdr or [])}

        # 帖子
        hdr, body, hidx = header_map(wb["原始数据"])
        if hdr:
            for r in body:
                plat = r[hidx.get("平台")] if "平台" in hidx else None
                kw = r[hidx.get("关键词")] if "关键词" in hidx else None
                author = r[hidx.get("作者")] if "作者" in hidx else None
                title = r[hidx.get("标题")] if "标题" in hidx else None
                content = r[hidx.get("正文")] if "正文" in hidx else None
                url = r[hidx.get("链接")] if "链接" in hidx else None
                ts = r[hidx.get("发布时间")] if "发布时间" in hidx else None
                likes = r[hidx.get("点赞数")] if "点赞数" in hidx else None
                if not plat or not url:
                    continue
                text = (title or "").strip()
                if content and str(content).strip() not in ("", "-", text):
                    text = text + "\n" + str(content).strip()
                if not text:
                    continue
                url_key = str(url).strip().rstrip("/").lower()
                if url_key in seen_posts:
                    continue
                if norm_key(text) in seen_text:
                    continue
                group = "websearch" if plat in PLATFORM_GROUP else plat
                dom = domain_of(plan, str(kw or ""))
                idx["post"] += 1
                sample = {
                    "text_id": f"{run_key}_P{idx['post']:03d}",
                    "platform": group,
                    "platform_raw": str(plat),
                    "domain": dom,
                    "brand": str(kw or ""),
                    "keyword": str(kw or ""),
                    "kind": "post",
                    "text": text,
                    "title": (title or "").strip(),
                    "author": str(author or ""),
                    "url": str(url),
                    "time": str(ts or ""),
                    "likes": str(likes or ""),
                    "batch": batch_label,
                    "post_url": str(url),
                    "flags": edge_flags(text, dom, domain_keywords),
                }
                posts.append(sample)
                seen_posts.add(url_key)
                seen_text.add(norm_key(text))

        # 评论与楼中楼
        if "评论明细" in wb.sheetnames:
            hdr, body, hidx = header_map(wb["评论明细"])
            if hdr:
                for r in body:
                    post_url = r[hidx.get("帖子链接")] if "帖子链接" in hidx else None
                    plat = r[hidx.get("平台")] if "平台" in hidx else None
                    kw = r[hidx.get("关键词")] if "关键词" in hidx else None
                    post_author = r[hidx.get("帖子作者")] if "帖子作者" in hidx else None
                    c_author = r[hidx.get("评论作者")] if "评论作者" in hidx else None
                    c_text = r[hidx.get("评论内容")] if "评论内容" in hidx else None
                    likes = r[hidx.get("点赞数")] if "点赞数" in hidx else None
                    c_time = r[hidx.get("评论时间")] if "评论时间" in hidx else None
                    is_reply = r[hidx.get("是否回复")] if "是否回复" in hidx else None
                    if not c_text or not post_url:
                        continue
                    text = str(c_text).strip()
                    if not text:
                        continue
                    url_key = str(post_url).strip().rstrip("/").lower()
                    ckey = (url_key, norm_key(text))
                    if ckey in seen_comments:
                        continue
                    if norm_key(text) in seen_text:
                        continue
                    group = "websearch" if plat in PLATFORM_GROUP else plat
                    dom = domain_of(plan, str(kw or ""))
                    kind = "reply" if REPLY_MARK in str(is_reply or "") else "comment"
                    idx[kind] += 1
                    sample = {
                        "text_id": f"{run_key}_{'R' if kind == 'reply' else 'C'}{idx[kind]:03d}",
                        "platform": group,
                        "platform_raw": str(plat),
                        "domain": dom,
                        "brand": str(kw or ""),
                        "keyword": str(kw or ""),
                        "kind": kind,
                        "text": text,
                        "title": "",
                        "author": str(c_author or ""),
                        "url": str(post_url),
                        "time": str(c_time or ""),
                        "likes": str(likes or ""),
                        "batch": batch_label,
                        "post_author": str(post_author or ""),
                        "post_url": str(post_url),
                        "flags": edge_flags(text, dom, domain_keywords),
                    }
                    (replies if kind == "reply" else comments).append(sample)
                    seen_comments.add(ckey)
                    seen_text.add(norm_key(text))
        wb.close()
    return posts, comments, replies


def build_cleaning_pool():
    """从丢弃明细构建清洗验证候选池。"""
    items = []
    idx = 0
    seen: set[tuple[str, str]] = set()
    for run_dir, xlsx in iter_excel_batches():
        wb = load_workbook(xlsx, read_only=True)
        if "丢弃明细" not in wb.sheetnames:
            wb.close()
            continue
        plan = read_plan(run_dir, xlsx.parent if xlsx.parent != run_dir else None)
        channels = [c.get("channel_id") for c in plan.get("channels", [])]
        if channels and set(channels) <= {"demo"}:
            wb.close()
            continue
        ws = wb["丢弃明细"]
        rows = ws.iter_rows(values_only=True)
        hdr = next(rows, None)
        hidx = {name: i for i, name in enumerate(hdr or [])}
        if not hdr:
            wb.close()
            continue
        for r in rows:
            plat = r[hidx.get("平台")] if "平台" in hidx else None
            url = r[hidx.get("链接")] if "链接" in hidx else None
            title = r[hidx.get("标题")] if "标题" in hidx else None
            reason = r[hidx.get("丢弃原因")] if "丢弃原因" in hidx else None
            if not reason or not url:
                continue
            key = (str(url).strip().lower(), str(title or "").strip()[:30])
            if key in seen:
                continue
            seen.add(key)
            idx += 1
            items.append({
                "text_id": f"drop_{idx:03d}",
                "platform": str(plat or ""),
                "url": str(url),
                "title": str(title or ""),
                "reason_raw": str(reason),
                "batch": str(xlsx.parent.relative_to(REPORTS_DIR)),
            })
        wb.close()
    return items


def normalize_reason(reason: str) -> str:
    if "官网" in reason:
        return "官网域名黑名单"
    if "样板" in reason or "页面壳" in reason:
        return "样板/页面壳"
    if "不相关" in reason:
        return "不相关"
    if "过短" in reason:
        return "文本过短"
    if "重复" in reason:
        return "重复"
    return "其他"


# ---------------------------------------------------------------------------
# 配额抽样
# ---------------------------------------------------------------------------

def weighted_sample(pool: list, k: int, rng: random.Random) -> list:
    """按边缘标记数加权的不重复抽样。"""
    pool = list(pool)
    picked = []
    while picked.__len__() < k and pool:
        weights = [1.0 + len(s["flags"]) for s in pool]
        total = sum(weights)
        r = rng.uniform(0, total)
        acc = 0.0
        for i, w in enumerate(weights):
            acc += w
            if r <= acc:
                picked.append(pool.pop(i))
                break
    return picked


def _takable(pool, selected_ids, post_comment_count, cell, kind):
    cands = [s for s in pool[cell][kind] if s["text_id"] not in selected_ids]
    if kind in ("comment", "reply"):
        # 同一帖子最多 3 条评论/楼中楼
        cands = [s for s in cands if post_comment_count[s["post_url"]] < 3]
    return cands


def sample_main(pool: dict[tuple[str, str], dict[str, list]], rng: random.Random):
    """按渠道×领域×文本类型配额抽样。

    约束优先级：渠道配额（硬）> 文本类型配额（软，优先补齐）> 领域配额（软，仅优先不硬卡）。
    """
    selected: list[dict] = []
    selected_ids: set[str] = set()
    cell_used: Counter = Counter()
    chan_used: Counter = Counter()
    dom_used: Counter = Counter()
    type_used: Counter = Counter()
    post_comment_count: Counter = Counter()

    cells = sorted(pool.keys(), key=lambda c: (len(pool[c]["post"]) + len(pool[c]["comment"]) + len(pool[c]["reply"]), c))
    reply_target = min(TYPE_TARGET["reply"], sum(len(pool[c]["reply"]) for c in pool))

    def space(cell):
        return max(0, CELL_TARGET - cell_used[cell])

    def under_domain(cell):
        return dom_used[cell[1]] < DOMAIN_TARGET

    def take(cell, kind, k):
        cands = _takable(pool, selected_ids, post_comment_count, cell, kind)
        picked = weighted_sample(cands, min(k, len(cands)), rng)
        for s in picked:
            selected.append(s)
            selected_ids.add(s["text_id"])
            cell_used[cell] += 1
            chan_used[s["platform"]] += 1
            dom_used[s["domain"]] += 1
            type_used[s["kind"]] += 1
            if s["kind"] in ("comment", "reply"):
                post_comment_count[s["post_url"]] += 1
        return len(picked)

    # 第一遍：稀缺格优先填满（楼中楼 → 约一半评论 → 其余帖子）
    for c in cells:
        if space(c) <= 0 or chan_used[c[0]] >= CHANNEL_TARGET:
            continue
        take(c, "reply", min(space(c), reply_target - type_used["reply"], 3))
        half = max(0, round(space(c) * 0.5))
        take(c, "comment", min(space(c), TYPE_TARGET["comment"] - type_used["comment"], half))
        take(c, "post", space(c))

    # 第二遍：补齐楼中楼配额（受同帖 3 条上限约束，最稀缺先补）
    while type_used["reply"] < reply_target:
        best_cell, best_key = None, None
        for c in cells:
            if chan_used[c[0]] >= CHANNEL_TARGET:
                continue
            if not _takable(pool, selected_ids, post_comment_count, c, "reply"):
                continue
            key = (0 if under_domain(c) else 1, cell_used[c])
            if best_key is None or key < best_key:
                best_cell, best_key = c, key
        if best_cell is None or take(best_cell, "reply", 1) == 0:
            break

    # 第三遍：补齐全局评论/帖子配额（领域配额为软目标，仅优先不硬卡）
    for kind in ("comment", "post"):
        while type_used[kind] < TYPE_TARGET[kind]:
            best_cell, best_key = None, None
            for c in cells:
                if chan_used[c[0]] >= CHANNEL_TARGET:
                    continue
                if not _takable(pool, selected_ids, post_comment_count, c, kind):
                    continue
                key = (0 if under_domain(c) else 1, cell_used[c])
                if best_key is None or key < best_key:
                    best_cell, best_key = c, key
            if best_cell is None or take(best_cell, kind, 1) == 0:
                break

    # 第四遍：渠道补满（优先缺失最多的文本类型，其次未满领域）
    for chan in sorted({c[0] for c in cells}):
        while chan_used[chan] < CHANNEL_TARGET:
            best_cell, best_kind, best_key = None, None, None
            for dom in ("game", "consumer"):
                c = (chan, dom)
                for kind in ("post", "comment", "reply"):
                    if not _takable(pool, selected_ids, post_comment_count, c, kind):
                        continue
                    deficit = TYPE_TARGET[kind] - type_used[kind]
                    key = (0 if under_domain(c) else 1, -deficit, cell_used[c])
                    if best_key is None or key < best_key:
                        best_cell, best_kind, best_key = c, kind, key
            if best_cell is None or take(best_cell, best_kind, 1) == 0:
                break

    # 第五遍：边缘样本定向补齐（同格同类内替换，保持配额）
    for flag, pct in EDGE_TARGETS.items():
        target = int(round(DEFAULT_TOTAL * pct))
        for _ in range(300):
            flagged = sum(1 for s in selected if flag in s["flags"])
            if flagged >= target:
                break
            swapped = False
            for c in cells:
                for kind in ("post", "comment", "reply"):
                    sel_non = [s for s in selected if s["platform"] == c[0] and s["domain"] == c[1]
                               and s["kind"] == kind and flag not in s["flags"]]
                    avail_flag = [s for s in pool[c][kind] if s["text_id"] not in selected_ids and flag in s["flags"]]
                    if not sel_non or not avail_flag:
                        continue
                    rng.shuffle(avail_flag)
                    rng.shuffle(sel_non)
                    old, new = sel_non[0], avail_flag[0]
                    if new["kind"] in ("comment", "reply") and post_comment_count.get(new["post_url"], 0) >= 3:
                        continue
                    selected.remove(old)
                    selected_ids.discard(old["text_id"])
                    if old["kind"] in ("comment", "reply"):
                        post_comment_count[old["post_url"]] -= 1
                    selected.append(new)
                    selected_ids.add(new["text_id"])
                    if new["kind"] in ("comment", "reply"):
                        post_comment_count[new["post_url"]] += 1
                    swapped = True
                    break
                if swapped:
                    break
            if not swapped:
                break

    return selected


def sample_cleaning(cleaning_pool: list, rng: random.Random, per_reason: int = 20):
    by_reason: dict[str, list] = defaultdict(list)
    for item in cleaning_pool:
        by_reason[normalize_reason(item["reason_raw"])].append(item)
    result = []
    for reason in sorted(by_reason):
        cands = list(by_reason[reason])
        rng.shuffle(cands)
        result.extend(cands[:per_reason])
    return result


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _base_columns() -> list[str]:
    return [
        "序号", "text_id", "平台", "领域", "品牌/主题", "关键词", "文本类型", "是否楼中楼",
        "原文", "帖子标题", "作者", "链接", "发布时间", "点赞数", "采集批次",
        "情感(整条)", "强度(1-5)",
    ]


def _annotate_columns(domain_keywords) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    dims = {dom: list(kws.keys()) for dom, kws in domain_keywords.items()}
    cols = {
        dom: _base_columns() + dims[dom] + ["语言现象", "是否相关", "备注", "标注人", "标注日期"]
        for dom in dims
    }
    return cols, dims


def _sample_row(seq: int, s: dict, dims: list[str]) -> list:
    return [
        seq, s["text_id"], s["platform"], s["domain"], s["brand"], s["keyword"],
        {"post": "帖子正文", "comment": "评论", "reply": "楼中楼"}[s["kind"]],
        "是" if s["kind"] == "reply" else "",
        s["text"], s["title"], s["author"], s["url"], s["time"], s["likes"], s["batch"],
        "", "",
    ] + ["" for _ in dims] + ["", "", "", "", ""]


def write_csv(rows: list[dict], cols: list[str], dims: list[str], path: Path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i, s in enumerate(rows, 1):
            w.writerow(_sample_row(i, s, dims))


def write_xlsx(rows: dict[str, list[dict]], dims: dict[str, list[str]], domain_keywords: dict, path: Path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    header_fill = PatternFill("solid", fgColor="DCE6F1")
    header_font = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")

    for dom, samples in rows.items():
        ws = wb.create_sheet("游戏标注" if dom == "game" else "消费品标注")
        cols = _base_columns() + dims[dom] + ["语言现象", "是否相关", "备注", "标注人", "标注日期"]
        ws.append(cols)
        for i, s in enumerate(samples, 1):
            ws.append(_sample_row(i, s, dims[dom]))
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
        ws.freeze_panes = "C2"
        widths = [6, 14, 10, 8, 14, 14, 10, 10, 60, 30, 12, 34, 14, 8, 24, 12, 10] + [10] * len(dims[dom]) + [12, 10, 20, 10, 12]
        for i, wd in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = wd
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = wrap
        dv_sent = DataValidation(type="list", formula1='"positive,negative,neutral,mixed"', allow_blank=True)
        dv_int = DataValidation(type="list", formula1='"1,2,3,4,5"', allow_blank=True)
        dv_rel = DataValidation(type="list", formula1='"是,否"', allow_blank=True)
        dv_dim = DataValidation(type="list", formula1='"positive,negative"', allow_blank=True)
        ws.add_data_validation(dv_sent); ws.add_data_validation(dv_int)
        ws.add_data_validation(dv_rel); ws.add_data_validation(dv_dim)
        n = len(samples) + 1
        dv_sent.add(f"P2:P{n}")
        dv_int.add(f"Q2:Q{n}")
        dim_cols = [get_column_letter(i) for i in range(len(_base_columns()) + 1, len(_base_columns()) + len(dims[dom]) + 1)]
        for col in dim_cols:
            dv_dim.add(f"{col}2:{col}{n}")
        rel_col = get_column_letter(len(_base_columns()) + len(dims[dom]) + 2)
        dv_rel.add(f"{rel_col}2:{rel_col}{n}")

    ws = wb.create_sheet("说明")
    lines = [
        "黄金集标注说明（口径 v0.3，详见 docs/抽样与标注规范.md）",
        "",
        "一、填写顺序：先读「原文」定整条情感与强度，再对每个「明确带情感」的维度填 positive/negative；未提及或无明确褒贬留空。",
        "二、整条情感：positive / negative / neutral / mixed。mixed 仅用于无法按维度拆解、又定不了主倾向的文本，占比应低于 2%。",
        "三、强度锚点：1=无/轻微、3=明显、5=强烈；两人相差 1 分属正常，不必强改。",
        "四、转折句逐维拆解：「画面好但价格劝退」→ 美术/质量 positive、价格 negative，整条 negative。",
        "五、反讽：只用于「表面正向/中性、实际负向」的文本（「这波操作太棒了」→ negative）；直接批评不算反讽。",
        "六、黑话：指圈内词/网络词（糖豆、骨科、工业糖精、挡枪等），标了就在备注写明含义。",
        "七、疑问句：有明显倾向按倾向（「这价格谁买得起？」→ negative）；纯疑问 → neutral。",
        "八、官方公告/纯转发/无观点：整条 neutral，维度列全部留空；语言现象只标「官方」，不再叠标「无观点」。",
        "九、纯玩家互动（鼓励、社区氛围等）：标「运营与社区」维度，情感按内容标。",
        "十、emoji：情绪明确（😡😍）可作情感依据；emoji 承载情感时标「emoji主导」。",
        "十一、不相关（跑题/引流）→ 是否相关填「否」，整条 neutral，不计入主情感评分。",
        "十二、独立标注：正式标注不要参考程序或词典判定，避免被诱导。",
        "十三、语言现象可多选，逗号分隔：反讽/黑话/方言/官方/广告/emoji主导/无观点；没现象就不填。",
        "",
        "维度关键词参考（命中即标，可多维度同时标）：",
    ]
    for dom, d in domain_keywords.items():
        label = "游戏" if dom == "game" else "消费品"
        parts = [f"{name}[{'、'.join(kws[:12])}]" for name, kws in d.items()]
        lines.append(f" - {label}：{'；'.join(parts)}")
    for line in lines:
        ws.append([line])
    ws.column_dimensions["A"].width = 110
    ws["A1"].font = Font(bold=True, size=12)
    wb.save(path)


def write_cleaning_xlsx(rows: list[dict], path: Path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "清洗验证"
    cols = ["序号", "text_id", "平台", "链接", "标题", "采集批次", "丢弃原因", "是否正确丢弃", "备注", "标注人", "标注日期"]
    ws.append(cols)
    for i, s in enumerate(rows, 1):
        ws.append([i, s["text_id"], s["platform"], s["url"], s["title"], s["batch"], s["reason_raw"], "", "", "", ""])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    dv = DataValidation(type="list", formula1='"应丢弃,误丢弃"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"H2:H{len(rows) + 1}")
    for i, wd in enumerate([6, 12, 12, 36, 40, 22, 18, 12, 20, 10, 12], 1):
        ws.column_dimensions[get_column_letter(i)].width = wd
    wb.save(path)


def write_cleaning_csv(rows: list[dict], path: Path):
    cols = ["序号", "text_id", "平台", "链接", "标题", "采集批次", "丢弃原因", "是否正确丢弃", "备注", "标注人", "标注日期"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i, s in enumerate(rows, 1):
            w.writerow([i, s["text_id"], s["platform"], s["url"], s["title"], s["batch"], s["reason_raw"], "", "", "", ""])


def sample_trial(samples: list[dict], n: int, rng: random.Random) -> list[dict]:
    """试标集：先保证平台×文本类型全覆盖，再按边缘标记加权补齐。"""
    chosen: list[dict] = []
    used: set[str] = set()
    cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in samples:
        cells[(s["platform"], s["kind"])].append(s)
    # 1) 每个平台×文本类型组合至少 1 条（稀缺组合优先）
    for key in sorted(cells, key=lambda k: (len(cells[k]), k)):
        if len(chosen) >= n:
            break
        cands = list(cells[key])
        rng.shuffle(cands)
        s = cands[0]
        if s["text_id"] not in used:
            chosen.append(s)
            used.add(s["text_id"])
    # 2) 剩余按边缘标记加权随机补齐
    remaining = [s for s in samples if s["text_id"] not in used]
    chosen.extend(weighted_sample(remaining, n - len(chosen), rng))
    chosen.sort(key=lambda s: (s["platform"], s["kind"], s["text_id"]))
    return chosen[:n]


def write_trial_csv(rows: list[dict], cols: list[str], dims: list[str], path: Path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i, s in enumerate(rows, 1):
            w.writerow(_sample_row(i, s, dims))


def write_trial_xlsx(rows: list[dict], dims: list[str], path: Path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "游戏试标" if rows and rows[0]["domain"] == "game" else "消费品试标"
    cols = _base_columns() + dims + ["语言现象", "是否相关", "备注", "标注人", "标注日期"]
    ws.append(cols)
    for i, s in enumerate(rows, 1):
        ws.append(_sample_row(i, s, dims))
    header_fill = PatternFill("solid", fgColor="FFF2CC")
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = Font(bold=True)
    ws.freeze_panes = "C2"
    widths = [6, 14, 10, 8, 14, 14, 10, 10, 60, 30, 12, 34, 14, 8, 24, 12, 10] + [10] * len(dims) + [12, 10, 20, 10, 12]
    for i, wd in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = wd
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    dv_sent = DataValidation(type="list", formula1='"positive,negative,neutral,mixed"', allow_blank=True)
    dv_int = DataValidation(type="list", formula1='"1,2,3,4,5"', allow_blank=True)
    dv_rel = DataValidation(type="list", formula1='"是,否"', allow_blank=True)
    dv_dim = DataValidation(type="list", formula1='"positive,negative,neutral"', allow_blank=True)
    ws.add_data_validation(dv_sent)
    ws.add_data_validation(dv_int)
    ws.add_data_validation(dv_rel)
    ws.add_data_validation(dv_dim)
    n = len(rows) + 1
    dv_sent.add(f"P2:P{n}")
    dv_int.add(f"Q2:Q{n}")
    dim_cols = [get_column_letter(i) for i in range(len(_base_columns()) + 1, len(_base_columns()) + len(dims) + 1)]
    for col in dim_cols:
        dv_dim.add(f"{col}2:{col}{n}")
    rel_col = get_column_letter(len(_base_columns()) + len(dims) + 2)
    dv_rel.add(f"{rel_col}2:{rel_col}{n}")

    guide = wb.create_sheet("试标说明")
    lines = [
        "试标目的：两人用这 20 条对齐「整条情感 + 维度级情感」的判定口径，不是正式评分。",
        "",
        "步骤：",
        "1. 两人各自独立标完这 20 条（不要互相看、不要看程序结果）；",
        "2. 对比每条差异，重点讨论：转折句、反讽、无观点、维度留空；",
        "3. 分歧逐条讨论，把统一口径记入「讨论记录」sheet；",
        "4. 整条情感一致率 ≥0.8 且维度级一致率 ≥0.75 后，再开始正式标注。",
        "",
        "口径速查：",
        "- 整条情感：positive / negative / neutral / mixed（mixed 占比应 <2%）；",
        "- 转折句逐维拆解：「画面好但价格劝退」→ 美术 positive、价格 negative、整条 negative；",
        "- 反讽按真实意图标；无观点/官方公告 → 整条 neutral，维度列全留空；",
        "- 维度只有「明确带情感」才填，只提到没评价 → 留空。",
        "详细规则见 docs/抽样与标注规范.md。",
    ]
    for line in lines:
        guide.append([line])
    guide.column_dimensions["A"].width = 100
    guide["A1"].font = Font(bold=True, size=12)

    dsheet = wb.create_sheet("讨论记录")
    dsheet.append(["序号", "text_id", "分歧点", "统一口径"])
    for i, s in enumerate(rows, 1):
        dsheet.append([i, s["text_id"], "", ""])
    dsheet.column_dimensions["A"].width = 6
    dsheet.column_dimensions["B"].width = 14
    dsheet.column_dimensions["C"].width = 40
    dsheet.column_dimensions["D"].width = 40
    wb.save(path)


def summarize(selected: list[dict], pool_stats: dict, pool: dict) -> dict:
    chan = Counter(s["platform"] for s in selected)
    dom = Counter(s["domain"] for s in selected)
    kind = Counter(s["kind"] for s in selected)
    edge = Counter()
    for s in selected:
        for f in s["flags"]:
            edge[f] += 1
    chan_pool: Counter = Counter()
    dom_pool: Counter = Counter()
    kind_pool: Counter = Counter()
    edge_pool: Counter = Counter()
    for c, kinds in pool.items():
        for samples in kinds.values():
            for s in samples:
                chan_pool[s["platform"]] += 1
                dom_pool[s["domain"]] += 1
                kind_pool[s["kind"]] += 1
                for f in s["flags"]:
                    edge_pool[f] += 1
    return {
        "targets": {"total": len(selected), "channel": CHANNEL_TARGET, "domain": DOMAIN_TARGET, "type": TYPE_TARGET},
        "achieved": {
            "total": len(selected),
            "channel": dict(chan),
            "domain": dict(dom),
            "type": dict(kind),
            "edge": {f: {"count": c, "pct": round(c / max(len(selected), 1), 3)} for f, c in edge.items()},
        },
        "pool": pool_stats,
        "pool_max": {
            "channel": dict(chan_pool),
            "domain": dict(dom_pool),
            "type": dict(kind_pool),
            "edge": dict(edge_pool),
        },
        "shortfalls": [],
    }


def main():
    ap = argparse.ArgumentParser(description="黄金集抽样与标注工作表生成")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--total", type=int, default=DEFAULT_TOTAL)
    ap.add_argument("--per-reason", type=int, default=20, help="清洗验证集每类丢弃原因抽样条数")
    ap.add_argument("--trial", type=int, default=0, help="生成试标集条数（默认 0 不生成）")
    ap.add_argument("--trial-domain", default="game", choices=["game", "consumer"])
    ap.add_argument("--force", action="store_true", help="覆盖已存在的产物文件（默认跳过）")
    args = ap.parse_args()

    global DOMAIN_TARGET, TYPE_TARGET
    DOMAIN_TARGET = args.total // 2
    TYPE_TARGET = {
        "post": int(args.total * 0.40),
        "comment": int(args.total * 0.50),
        "reply": int(args.total * 0.10),
    }

    rng = random.Random(args.seed)
    domain_keywords = load_domain_keywords()
    dims = {dom: list(kws.keys()) for dom, kws in domain_keywords.items()}

    posts, comments, replies = build_pool(domain_keywords)
    pool_stats = {
        "posts": {"total": len(posts), "by_channel": dict(Counter(s["platform"] for s in posts)),
                  "by_domain": dict(Counter(s["domain"] for s in posts))},
        "comments": {"total": len(comments), "by_channel": dict(Counter(s["platform"] for s in comments)),
                     "by_domain": dict(Counter(s["domain"] for s in comments))},
        "replies": {"total": len(replies), "by_channel": dict(Counter(s["platform"] for s in replies)),
                    "by_domain": dict(Counter(s["domain"] for s in replies))},
    }
    print("样本池：帖子", len(posts), "评论", len(comments), "楼中楼", len(replies))

    pool: dict[tuple[str, str], dict[str, list]] = defaultdict(lambda: {"post": [], "comment": [], "reply": []})
    for s in posts + comments + replies:
        pool[(s["platform"], s["domain"])][s["kind"]].append(s)

    selected = sample_main(pool, rng)
    report = summarize(selected, pool_stats, pool)

    def add_shortfall(label, achieved, target, pool_max):
        if achieved >= target:
            return
        if pool_max < target:
            report["shortfalls"].append(f"{label} 仅抽到 {achieved}/{target}（样本池不足，池内仅 {pool_max} 条）")
        else:
            report["shortfalls"].append(f"{label} 仅抽到 {achieved}/{target}（配额未达成）")

    # 短报：渠道/领域/文本类型未达标（区分"数据池不足"与"配额未达成"）
    for ch in sorted({s["platform"] for s in posts + comments + replies}):
        add_shortfall(f"渠道 {ch}", report["achieved"]["channel"].get(ch, 0), CHANNEL_TARGET,
                      report["pool_max"]["channel"].get(ch, 0))
    for dom in ("game", "consumer"):
        add_shortfall(f"领域 {dom}", report["achieved"]["domain"].get(dom, 0), DOMAIN_TARGET,
                      report["pool_max"]["domain"].get(dom, 0))
    for kind, tgt in TYPE_TARGET.items():
        pool_t = report["pool_max"]["type"].get(kind, 0)
        if kind == "reply":
            tgt = min(tgt, pool_t)
        achieved_t = report["achieved"]["type"].get(kind, 0)
        if achieved_t < tgt:
            if pool_t >= tgt:
                report["shortfalls"].append(
                    f"文本类型 {kind} 仅抽到 {achieved_t}/{tgt}（受同帖最多 3 条评论上限与渠道配额约束）")
            else:
                report["shortfalls"].append(f"文本类型 {kind} 仅抽到 {achieved_t}/{tgt}（样本池不足，池内仅 {pool_t} 条）")
    for flag, pct in EDGE_TARGETS.items():
        got = report["achieved"]["edge"].get(flag, {}).get("pct", 0)
        pool_f = report["pool_max"]["edge"].get(flag, 0)
        count_f = report["achieved"]["edge"].get(flag, {}).get("count", 0)
        add_shortfall(f"边缘样本 {flag}（覆盖 {round(got * 100)}%）", count_f,
                      int(round(DEFAULT_TOTAL * pct)), pool_f)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cols, dims_out = _annotate_columns(domain_keywords)
    by_domain = {"game": [], "consumer": []}
    for s in sorted(selected, key=lambda x: (x["platform"], x["domain"], x["kind"], x["text_id"])):
        by_domain[s["domain"]].append(s)
    game_path = OUT_DIR / "annotation_worksheet_v1_game.csv"
    consumer_path = OUT_DIR / "annotation_worksheet_v1_consumer.csv"
    xlsx_path = OUT_DIR / "annotation_worksheet_v1.xlsx"

    def safe_write(path: Path, writer, *fargs, **kwargs):
        if path.exists() and not args.force:
            print(f"跳过（已存在，用 --force 覆盖）：{path.name}")
            return
        writer(*fargs, **kwargs)

    safe_write(game_path, write_csv, by_domain["game"], cols["game"], dims_out["game"], game_path)
    safe_write(consumer_path, write_csv, by_domain["consumer"], cols["consumer"], dims_out["consumer"], consumer_path)
    safe_write(xlsx_path, write_xlsx, by_domain, dims_out, domain_keywords, xlsx_path)

    trial_files: list[Path] = []
    if args.trial > 0:
        dom = args.trial_domain
        trial_rows = sample_trial(by_domain[dom], args.trial, rng)
        trial_csv = OUT_DIR / f"trial_alignment_v1_{dom}.csv"
        trial_xlsx = OUT_DIR / f"trial_alignment_v1_{dom}.xlsx"
        safe_write(trial_csv, write_trial_csv, trial_rows, cols[dom], dims_out[dom], trial_csv)
        safe_write(trial_xlsx, write_trial_xlsx, trial_rows, dims_out[dom], trial_xlsx)
        trial_files = [trial_csv, trial_xlsx]

    cleaning = sample_cleaning(build_cleaning_pool(), rng, args.per_reason)
    cleaning_csv = OUT_DIR / "cleaning_worksheet_v1.csv"
    cleaning_xlsx = OUT_DIR / "cleaning_worksheet_v1.xlsx"
    safe_write(cleaning_csv, write_cleaning_csv, cleaning, cleaning_csv)
    safe_write(cleaning_xlsx, write_cleaning_xlsx, cleaning, cleaning_xlsx)

    report["targets"]["type"]["reply"] = min(TYPE_TARGET["reply"], report["pool_max"]["type"].get("reply", 0))
    report["targets"]["total"] = sum(report["achieved"]["channel"].values())
    report["files"] = [str(p) for p in (game_path, consumer_path, xlsx_path, *trial_files, cleaning_csv, cleaning_xlsx)]
    report_path = OUT_DIR / "sampling_report_v1.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n== 配额达成 ==")
    print("渠道:", dict(report["achieved"]["channel"]))
    print("领域:", dict(report["achieved"]["domain"]))
    print("文本类型:", dict(report["achieved"]["type"]))
    print("边缘覆盖:", {f: f"{v['pct']:.0%}" for f, v in report["achieved"]["edge"].items()})
    if report["shortfalls"]:
        print("\n== 未达标说明 ==")
        for line in report["shortfalls"]:
            print(" -", line)
    print("\n产物：")
    for f in report["files"]:
        print(" -", f)
    if trial_files:
        print("\n试标集（先做这个）：", trial_files[0].name, "+", trial_files[1].name)


if __name__ == "__main__":
    main()
