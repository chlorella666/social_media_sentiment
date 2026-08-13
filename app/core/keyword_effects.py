# -*- coding: utf-8 -*-
"""关键词效果数据层与候选反推（阶段 2.2）。

目标：把"关键词采集质量"变成可量化、可反推、可受控使用的资产：
- 从任务 result.json 提取每查询串全漏斗：采集/丢弃（按原因）/保留/编码/有效供给率；
- 落 data/keyword_effects/runs/<run_id>.json + history.jsonl（按文件内容哈希去重）；
- 反推候选（只出建议，人工确认后才启用）：后缀效果统计、品牌词高频搭配、
  品牌别称（如 DJI）；输出 candidates.csv；
- 关键词策略配置 app/channels/keyword_strategy.json（synonyms / extra_queries /
  suffix_pool），由 WebSearch 渠道读取，缺失时回退旧行为。

目录可用 SMS_KEYWORD_EFFECTS_DIR 覆盖（测试隔离用）。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_STRATEGY = ROOT / "app" / "channels" / "keyword_strategy.json"

REF_N = 30  # 证据样本数低于该值仅作参考
MAX_ALIASES = 2      # 每品牌同义词上限（含品牌名共 ≤3 查询）
MAX_EXTRA = 5        # 每品牌额外查询候选上限
MAX_SUFFIX_POOL = 5  # 后缀池上限

ALIAS_RE = re.compile(r"[A-Za-z][A-Za-z0-9._\-]{1,}")
SKIP_ALIASES = {"https", "http", "www", "com", "cn", "html", "htm"}
SKIP_ALIAS_SUFFIX = {".docx", ".pdf", ".jpg", ".png", ".zip", ".rar", ".exe"}
HINT_TOKENS = ("知乎", "贴吧", "TapTap")
# 站点/页面壳/平台词：对"用户讨论"无信息量，反推候选时排除
NOISE_TOKENS = {
    "百度", "知乎", "综合", "反馈", "推荐", "视频", "论坛", "攻略", "下载",
    "官网", "首页", "官方", "游民", "星空", "九游", "单机", "头条", "搜狐",
    "爱奇艺", "豆瓣", "抖音", "微博", "贴吧", "哔哩哔哩", "游戏", "角色",
    "英雄", "介绍", "大全", "相关", "什么", "值得", "好喝", "怎么", "如何",
    "测评", "评价", "怎么样", "吐槽", "测试", "版本",
}


def _data_dir() -> Path:
    override = os.environ.get("SMS_KEYWORD_EFFECTS_DIR")
    return Path(override) if override else (ROOT / "data" / "keyword_effects")


def runs_dir() -> Path:
    return _data_dir() / "runs"


def history_path() -> Path:
    return _data_dir() / "history.jsonl"


def candidates_path() -> Path:
    return _data_dir() / "candidates.csv"


def _iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 关键词策略配置（WebSearch 渠道复用）
# ---------------------------------------------------------------------------

def load_keyword_strategy(path: str | Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_STRATEGY
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_keyword_strategy(strategy: dict, path: str | Path | None = None) -> Path:
    p = Path(path) if path else DEFAULT_STRATEGY
    strategy.setdefault("schema", 1)
    strategy["updated_at"] = _iso()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(strategy, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def apply_candidates(
    selected: list[dict],
    path: str | Path | None = None,
    strategy: dict | None = None,
) -> dict:
    """人工确认后把候选写入策略配置（带数量上限与去重）。返回更新后的策略。"""
    backup_strategy(path)
    strat = dict(strategy or load_keyword_strategy(path))
    strat.setdefault("synonyms", {})
    strat.setdefault("extra_queries", {})
    strat.setdefault("suffix_pool", ["评价"])
    for row in selected:
        typ = str(row.get("类型") or row.get("type") or "")
        cand = str(row.get("候选") or row.get("candidate") or "").strip()
        subject = str(row.get("主题") or row.get("subject") or "").strip()
        if not cand:
            continue
        if typ == "后缀":
            pool = strat["suffix_pool"]
            if cand not in pool:
                pool.append(cand)
            strat["suffix_pool"] = pool[:MAX_SUFFIX_POOL]
        elif typ == "同义词" and subject:
            aliases = strat["synonyms"].setdefault(subject, [])
            if cand not in aliases:
                aliases.append(cand)
            strat["synonyms"][subject] = aliases[:MAX_ALIASES]
        elif typ == "查询候选" and subject:
            extras = strat["extra_queries"].setdefault(subject, [])
            if cand not in extras:
                extras.append(cand)
            strat["extra_queries"][subject] = extras[:MAX_EXTRA]
    save_keyword_strategy(strat, path)
    append_strategy_history(
        "写入候选",
        json.dumps(
            {
                "synonyms": strat.get("synonyms"),
                "extra_queries": strat.get("extra_queries"),
                "suffix_pool": strat.get("suffix_pool"),
            },
            ensure_ascii=False,
        )[:500],
    )
    return strat


def backups_dir() -> Path:
    return _data_dir() / "backups"


def backup_strategy(path: str | Path | None = None) -> Path | None:
    """保存前备份当前策略配置；返回备份文件路径（无配置则 None）。"""
    target = Path(path) if path else DEFAULT_STRATEGY
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    bdir = backups_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = bdir / f"keyword_strategy_{ts}.json"
    n = 2
    while backup.exists():
        backup = bdir / f"keyword_strategy_{ts}-{n}.json"
        n += 1
    backup.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return backup


def list_backups() -> list[dict]:
    bdir = backups_dir()
    out: list[dict] = []
    if not bdir.exists():
        return out
    for p in sorted(
        bdir.glob("keyword_strategy_*.json"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    ):
        out.append({
            "name": p.name,
            "path": str(p),
            "ts": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
        })
    return out[:20]


def restore_backup(
    backup_path: str | Path,
    path: str | Path | None = None,
) -> dict:
    """从备份恢复策略配置；备份内容非法则抛 ValueError。"""
    target = Path(path) if path else DEFAULT_STRATEGY
    data = json.loads(Path(backup_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("备份内容不是有效的策略配置")
    save_keyword_strategy(data, target)
    append_strategy_history(
        "还原备份",
        f"从 {Path(backup_path).name} 恢复",
    )
    return data


def strategy_history_path() -> Path:
    return _data_dir() / "strategy_history.jsonl"


def append_strategy_history(
    action: str,
    detail: str = "",
    history_file: str | Path | None = None,
) -> None:
    """追加策略变更历史；失败不阻断（best-effort）。"""
    p = Path(history_file) if history_file else strategy_history_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"ts": _iso(), "action": action, "detail": detail},
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass


def load_strategy_history(
    limit: int = 20,
    path: str | Path | None = None,
) -> list[dict]:
    p = Path(path) if path else strategy_history_path()
    out: list[dict] = []
    if not p.exists():
        return out
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out[-limit:]


def save_strategy_edit(
    *,
    synonyms_updates: dict[str, list[str]] | None = None,
    extra_queries_updates: dict[str, list[str]] | None = None,
    suffix_pool: list[str] | None = None,
    path: str | Path | None = None,
) -> dict:
    """结构化保存策略配置（按品牌合并，空列表=删除该品牌）。

    超限不静默截断，而是返回 warnings 提示（WebSearch 读取时仍按上限截断）。
    """
    target = Path(path) if path else DEFAULT_STRATEGY
    current = load_keyword_strategy(target)
    backup = backup_strategy(target)
    warnings: list[str] = []

    if synonyms_updates is not None:
        syn = dict(current.get("synonyms") or {})
        for brand, words in synonyms_updates.items():
            ws = [w.strip() for w in words if w and w.strip()]
            if len(ws) > MAX_ALIASES:
                warnings.append(
                    f"同义词「{brand}」共 {len(ws)} 个，超过上限 {MAX_ALIASES}，"
                    f"WebSearch 只会用前 {MAX_ALIASES} 个"
                )
            if ws:
                syn[brand] = ws
            else:
                syn.pop(brand, None)
        current["synonyms"] = syn

    if extra_queries_updates is not None:
        ex = dict(current.get("extra_queries") or {})
        for brand, words in extra_queries_updates.items():
            ws = [w.strip() for w in words if w and w.strip()]
            if len(ws) > MAX_EXTRA:
                warnings.append(
                    f"额外查询「{brand}」共 {len(ws)} 个，超过上限 {MAX_EXTRA}，"
                    f"WebSearch 只会用前 {MAX_EXTRA} 个"
                )
            if ws:
                ex[brand] = ws
            else:
                ex.pop(brand, None)
        current["extra_queries"] = ex

    if suffix_pool is not None:
        pool = [w.strip() for w in suffix_pool if w and w.strip()]
        if len(pool) > MAX_SUFFIX_POOL:
            warnings.append(
                f"后缀池共 {len(pool)} 个，超过上限 {MAX_SUFFIX_POOL}，"
                f"WebSearch 只会用前 {MAX_SUFFIX_POOL} 个"
            )
        current["suffix_pool"] = pool

    save_keyword_strategy(current, target)
    append_strategy_history(
        "编辑配置",
        json.dumps(
            {
                "synonyms": current.get("synonyms"),
                "extra_queries": current.get("extra_queries"),
                "suffix_pool": current.get("suffix_pool"),
            },
            ensure_ascii=False,
        )[:500],
    )
    return {
        "saved": True,
        "backup": str(backup) if backup else None,
        "warnings": warnings,
        "strategy": current,
    }


# ---------------------------------------------------------------------------
# 漏斗解析
# ---------------------------------------------------------------------------

def _suffix_of(keyword: str, query: str) -> str:
    q = query or keyword
    for h in HINT_TOKENS:
        q = q.replace(h, "")
    q = q.replace(keyword, "")
    return " ".join(q.split())


def extract_funnel(report: dict) -> dict:
    """从 result.json 提取每 (渠道, 关键词, 查询串) 漏斗与候选提示。"""
    plan = report.get("plan") or {}
    subject = plan.get("subject", "")
    channel_results = report.get("channel_results") or []
    coded_items = report.get("coded_items") or []
    llm = report.get("llm_usage") or {}

    coded = Counter()
    neg = Counter()
    for it in coded_items:
        key = (it.get("platform", ""), it.get("keyword", ""))
        coded[key] += 1
        if it.get("sentiment") == "negative":
            neg[key] += 1

    rows: list[dict] = []
    urls: dict[str, dict] = {}
    unattributed = 0
    for ch in channel_results:
        if not ch.get("ok"):
            continue
        cid = ch.get("channel_id", "")
        posts = ch.get("posts") or []
        drops = ch.get("dropped") or []
        kept_by: dict[tuple, int] = defaultdict(int)
        queries_by_kw: dict[tuple, set] = defaultdict(set)
        for p in posts:
            ps = p.get("platform_specific") or {}
            kw = p.get("keyword", "")
            q = ps.get("query") or kw
            kept_by[(cid, kw, q)] += 1
            queries_by_kw[(cid, kw)].add(q)
            key = f"{cid}\x1f{q}"
            urls.setdefault(key, {"kept": [], "dropped": []})["kept"].append(
                p.get("url", "")
            )
        # 旧报告丢弃记录无 query：若该关键词在渠道内只有一种查询串则按它归因
        query_fallback: dict[tuple, str] = {}
        for k, qs in queries_by_kw.items():
            if len(qs) == 1:
                query_fallback[k] = next(iter(qs))
        drop_by: dict[tuple, Counter] = defaultdict(Counter)
        for d in drops:
            kw = d.get("keyword", "")
            if not kw:
                unattributed += 1
                continue
            q = d.get("query") or query_fallback.get((cid, kw), kw)
            drop_by[(cid, kw, q)][d.get("reason", "")] += 1
            key = f"{cid}\x1f{q}"
            urls.setdefault(key, {"kept": [], "dropped": []})["dropped"].append(
                {"url": d.get("url", ""), "reason": d.get("reason", "")}
            )
        for key in sorted(set(kept_by) | set(drop_by)):
            cid_, kw, q = key
            kept = kept_by[key]
            reasons = dict(drop_by[key])
            dropped_n = sum(reasons.values())
            coded_n = coded.get((cid_, kw), 0)
            neg_n = neg.get((cid_, kw), 0)
            total = kept + dropped_n
            rows.append({
                "channel": cid_,
                "keyword": kw,
                "query": q,
                "collected": total,
                "kept": kept,
                "dropped": dropped_n,
                "drop_reasons": reasons,
                "coded": coded_n,
                "negative": neg_n,
                "effective_rate": round(kept / total, 4) if total else None,
                "negative_rate": round(neg_n / coded_n, 4) if coded_n else None,
            })

    return {
        "subject": subject,
        "channels": [ch.get("channel_id", "") for ch in channel_results],
        "funnel": rows,
        "unattributed_dropped": unattributed,
        "llm_cost": llm.get("estimated_cost"),
        "hints": _extract_hints(subject, rows, channel_results),
        "urls": urls,
    }


def _extract_hints(subject: str, rows: list[dict], channel_results: list[dict]) -> dict:
    """候选提示：后缀效果统计 + 品牌词高频搭配 + 品牌别称（限 websearch）。"""
    suffix_stat: dict[str, dict] = {}
    for r in rows:
        if not str(r["channel"]).startswith("websearch"):
            continue
        suf = _suffix_of(r["keyword"], r["query"])
        if not suf:
            continue
        st = suffix_stat.setdefault(
            suf, {"n_queries": 0, "collected": 0, "kept": 0, "effective_rate": None}
        )
        st["n_queries"] += 1
        st["collected"] += r["collected"]
        st["kept"] += r["kept"]
    for st in suffix_stat.values():
        st["effective_rate"] = round(st["kept"] / st["collected"], 4) if st["collected"] else None

    co_texts: list[str] = []
    alias_texts: list[str] = []
    for ch in channel_results:
        if not ch.get("ok") or not str(ch.get("channel_id", "")).startswith("websearch"):
            continue
        for p in ch.get("posts") or []:
            text = f"{p.get('title', '')} {p.get('content', '')}".strip()
            if not text:
                continue
            alias_texts.append(text)
            if subject and subject in text:
                co_texts.append(text)

    from app.coding.tokenizer import GENERIC_NOUNS, segment

    co_count: Counter = Counter()
    co_examples: dict[str, list[str]] = defaultdict(list)
    for text in co_texts:
        for w in set(segment(text, extra_stopwords=GENERIC_NOUNS)):
            if (
                len(w) < 2
                or w == subject
                or subject in w
                or w in subject
                or w in NOISE_TOKENS
            ):
                continue
            co_count[w] += 1
            if len(co_examples[w]) < 2:
                co_examples[w].append(text[:60])
    cooccur = {
        f"{subject}|{w}": {
            "subject": subject,
            "token": w,
            "n": n,
            "examples": co_examples[w],
        }
        for w, n in co_count.most_common(10)
        if n >= 2
    }

    alias_count: Counter = Counter()
    alias_examples: dict[str, list[str]] = defaultdict(list)
    for text in alias_texts:
        for m in set(ALIAS_RE.findall(text)):
            tok = m.strip("._-")
            if len(tok) < 2 or not any(c.isalpha() for c in tok):
                continue
            low = tok.lower()
            if (
                low in SKIP_ALIASES
                or "." in tok
                or any(tok.lower().endswith(sfx) for sfx in SKIP_ALIAS_SUFFIX)
            ):
                continue
            alias_count[tok] += 1
            if len(alias_examples[tok]) < 2:
                alias_examples[tok].append(text[:60])
    # 中文短别称反推（2.2 增强）：品牌名首/尾两字，在"不含全称"的保留文本中出现即算证据
    subj = (subject or "").strip()
    cn_candidates: set[str] = set()
    if len(subj) >= 3:
        cn_candidates = {subj[:2], subj[-2:]}
    for cand in cn_candidates:
        if cand in NOISE_TOKENS or cand == subj or len(cand) < 2:
            continue
        for text in alias_texts:
            if subj and subj in text:
                continue  # 含全称的文本不算短称证据
            if cand in text:
                alias_count[cand] += 1
                if len(alias_examples[cand]) < 2:
                    alias_examples[cand].append(text[:60])
    alias = {
        w: {"n": n, "examples": alias_examples[w], "subject": subject}
        for w, n in alias_count.most_common(5)
        if n >= 2
    }
    return {"suffix_stats": suffix_stat, "cooccur": cooccur, "alias": alias}


# ---------------------------------------------------------------------------
# 历史归档 / 聚合 / 候选输出
# ---------------------------------------------------------------------------

def load_history(history_file: Path | None = None) -> list[dict]:
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
        except ValueError:
            continue
    return out


def _append_history(entry: dict, history_file: Path | None = None) -> None:
    target = history_file or history_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[keyword_effects] 历史写入失败：{exc}", file=sys.stderr)


def scan_report(
    path: Path,
    history_file: Path | None = None,
    force: bool = False,
) -> dict | None:
    """解析单个 result.json 并入历史（按内容哈希去重）。

    force=True：即使 run 已存在也重写 run JSON（用于补齐 URL 明细等），不重复追加历史。
    返回 None 表示重复且未 force。
    """
    raw = path.read_bytes()
    run_id = "sha256:" + hashlib.sha256(raw).hexdigest()[:12]
    existing = {h.get("run_id") for h in load_history(history_file)}
    is_new = run_id not in existing
    if not is_new and not force:
        return None
    report = json.loads(raw.decode("utf-8"))
    payload = extract_funnel(report)
    payload.update(
        run_id=run_id,
        source=str(path),
        scanned_at=_iso(),
        created_at=report.get("created_at", ""),
    )
    rd = runs_dir()
    rd.mkdir(parents=True, exist_ok=True)
    (rd / f"{run_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if is_new:
        _append_history(payload, history_file)
    return payload


def scan_path(path: Path, history_file: Path | None = None) -> list[dict]:
    """扫描 result.json 文件或目录（递归），返回新增的解析结果。"""
    files: list[Path] = []
    if path.is_file():
        if path.name == "result.json":
            files = [path]
    elif path.is_dir():
        files = sorted(path.rglob("result.json"))
    added = []
    for f in files:
        try:
            payload = scan_report(f, history_file)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"跳过 {f}：{exc}", file=sys.stderr)
            payload = None
        if payload:
            added.append(payload)
    return added


def aggregate(history: list[dict]) -> dict:
    """跨任务聚合：按 (渠道, 查询串) 求和漏斗 + 合并候选提示。"""
    agg: dict[tuple, dict] = defaultdict(
        lambda: {"collected": 0, "kept": 0, "dropped": 0, "coded": 0,
                 "negative": 0, "reasons": Counter()}
    )
    suffix_stat: dict[str, dict] = {}
    cooccur: dict[str, dict] = {}
    alias: dict[str, dict] = {}
    llm_cost = 0.0
    unattributed = 0
    for h in history:
        for r in h.get("funnel") or []:
            key = (r["channel"], r["query"])
            a = agg[key]
            a["collected"] += r["collected"]
            a["kept"] += r["kept"]
            a["dropped"] += r["dropped"]
            a["coded"] += r["coded"]
            a["negative"] += r["negative"]
            a["reasons"].update(r.get("drop_reasons") or {})
            a["keyword"] = r["keyword"]
        for suf, st in (h.get("hints") or {}).get("suffix_stats", {}).items():
            s = suffix_stat.setdefault(suf, {"n_queries": 0, "collected": 0, "kept": 0})
            s["n_queries"] += st["n_queries"]
            s["collected"] += st["collected"]
            s["kept"] += st["kept"]
        for k, v in (h.get("hints") or {}).get("cooccur", {}).items():
            c = cooccur.setdefault(k, {"subject": v.get("subject", ""),
                                       "token": v.get("token", ""), "n": 0,
                                       "examples": []})
            c["n"] += v["n"]
            if not c["examples"]:
                c["examples"] = v.get("examples", [])
        for k, v in (h.get("hints") or {}).get("alias", {}).items():
            a2 = alias.setdefault(k, {"n": 0, "examples": []})
            a2["n"] += v["n"]
            if not a2["examples"]:
                a2["examples"] = v.get("examples", [])
            if not a2.get("subject"):
                a2["subject"] = v.get("subject", "")
        llm_cost += h.get("llm_cost") or 0
        unattributed += h.get("unattributed_dropped") or 0

    funnel = []
    for (channel, query), a in sorted(
        agg.items(), key=lambda kv: -kv[1]["collected"]
    ):
        total = a["collected"]
        funnel.append({
            "channel": channel,
            "query": query,
            "keyword": a["keyword"],
            "collected": total,
            "kept": a["kept"],
            "dropped": a["dropped"],
            "drop_reasons": dict(a["reasons"].most_common()),
            "coded": a["coded"],
            "negative": a["negative"],
            "effective_rate": round(a["kept"] / total, 4) if total else None,
            "negative_rate": round(a["negative"] / a["coded"], 4) if a["coded"] else None,
        })
    for st in suffix_stat.values():
        st["effective_rate"] = round(st["kept"] / st["collected"], 4) if st["collected"] else None
    return {
        "tasks": len(history),
        "funnel": funnel,
        "suffix_stats": dict(sorted(suffix_stat.items(), key=lambda kv: -kv[1]["collected"])),
        "cooccur": cooccur,
        "alias": alias,
        "llm_cost": round(llm_cost, 4),
        "unattributed_dropped": unattributed,
    }


def _run_payload(run_id: str, runs_dir_path: Path | None = None) -> dict | None:
    rd = runs_dir_path or runs_dir()
    p = rd / f"{run_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def aggregate_unique(
    history: list[dict],
    runs_dir_path: Path | None = None,
) -> dict:
    """跨任务按 URL 去重后的聚合（判定口径 2026-08-13 修订）。

    同一 URL 只计一次：任一任务中保留 → 保留；从未保留但被丢弃 → 丢弃（取首次原因）。
    重复测量只能验证比率稳定，不能放大样本——n 与有效供给率必须基于去重后统计。
    缺 URL 明细的旧任务不参与去重统计，列入 dedup_missing（运行 --rescan 补齐）。
    候选提示（后缀/共现/别称）仍为任务次数累计，仅作参考。
    """
    kept: dict[tuple, set] = defaultdict(set)
    dropped: dict[tuple, dict] = defaultdict(dict)  # (channel, query) -> url -> reason
    missing: list[str] = []
    for h in history:
        run = _run_payload(h.get("run_id"), runs_dir_path)
        if run is None or "urls" not in run:
            missing.append(str(h.get("run_id") or ""))
            continue
        urls = run.get("urls") or {}
        for key, v in urls.items():
            channel, _, query = key.partition("\x1f")
            k = (channel, query)
            kept[k].update(v.get("kept") or [])
            for d in v.get("dropped") or []:
                u = str(d.get("url") or "")
                if u:
                    dropped[k].setdefault(u, str(d.get("reason") or ""))

    funnel: list[dict] = []
    for (channel, query) in sorted(
        set(kept) | set(dropped),
        key=lambda k: -len(kept.get(k, ())) - len(dropped.get(k, {})),
    ):
        kept_set = kept.get((channel, query), set())
        dropped_map = {
            u: r for u, r in dropped.get((channel, query), {}).items()
            if u not in kept_set
        }
        kept_n, dropped_n = len(kept_set), len(dropped_map)
        total = kept_n + dropped_n
        funnel.append({
            "channel": channel,
            "query": query,
            "keyword": "",
            "collected": total,
            "kept": kept_n,
            "dropped": dropped_n,
            "drop_reasons": dict(Counter(dropped_map.values()).most_common()),
            "coded": 0,
            "negative": 0,
            "effective_rate": round(kept_n / total, 4) if total else None,
            "negative_rate": None,
        })

    suffix_stat: dict[str, dict] = {}
    cooccur: dict[str, dict] = {}
    alias: dict[str, dict] = {}
    llm_cost = 0.0
    unattributed = 0
    for h in history:
        for suf, st in (h.get("hints") or {}).get("suffix_stats", {}).items():
            s = suffix_stat.setdefault(suf, {"n_queries": 0, "collected": 0, "kept": 0})
            s["n_queries"] += st["n_queries"]
            s["collected"] += st["collected"]
            s["kept"] += st["kept"]
        for k, v in (h.get("hints") or {}).get("cooccur", {}).items():
            c = cooccur.setdefault(k, {"subject": v.get("subject", ""),
                                       "token": v.get("token", ""), "n": 0,
                                       "examples": []})
            c["n"] += v["n"]
            if not c["examples"]:
                c["examples"] = v.get("examples", [])
        for k, v in (h.get("hints") or {}).get("alias", {}).items():
            a2 = alias.setdefault(k, {"n": 0, "examples": []})
            a2["n"] += v["n"]
            if not a2["examples"]:
                a2["examples"] = v.get("examples", [])
        llm_cost += h.get("llm_cost") or 0
        unattributed += h.get("unattributed_dropped") or 0
    for st in suffix_stat.values():
        st["effective_rate"] = round(st["kept"] / st["collected"], 4) if st["collected"] else None
    return {
        "tasks": len(history),
        "dedup": "url",
        "dedup_missing": missing,
        "funnel": funnel,
        "suffix_stats": dict(sorted(suffix_stat.items(), key=lambda kv: -kv[1]["collected"])),
        "cooccur": cooccur,
        "alias": alias,
        "llm_cost": round(llm_cost, 4),
        "unattributed_dropped": unattributed,
    }


SUFFIX_STRIP_WORDS = ("评价", "怎么样", "吐槽", "测评")


def _strip_query(query: str) -> str:
    """去掉后缀与子渠道提示，还原为"策略词"（用于低效词聚合）。"""
    q = query or ""
    for t in HINT_TOKENS:
        q = q.replace(t, "")
    for s in SUFFIX_STRIP_WORDS:
        q = q.replace(s, "")
    return " ".join(q.split()).strip()


def _websearch_rows(entry: dict) -> list[dict]:
    return [
        r for r in (entry.get("funnel") or [])
        if str(r.get("channel", "")).startswith("websearch")
    ]


def _agg_rates(rows: list[dict]) -> dict:
    col = sum(r.get("collected", 0) for r in rows)
    kept = sum(r.get("kept", 0) for r in rows)
    return {
        "collected": col,
        "kept": kept,
        "effective_rate": (kept / col) if col else None,
    }


def compare_runs_metrics(
    base: dict,
    cur: dict,
    strategy: dict | None = None,
    agg_rows: list[dict] | None = None,
) -> dict:
    """策略效果判定：选两次任务（基准/本次），输出有效率、纯品牌词、新增词、低效词。

    口径（与产品验收一致）：
    - 有效供给率 = 保留/采集（仅 WebSearch），达标要求较基准提升 ≥ +5pp；
    - 纯品牌词 = 确认关键词 == subject 的那组查询（含其展开串），丢弃率 <15% 达标；
    - 低效词 = 按去掉后缀/子渠道提示后的词聚合：n≥30 且 <50% 强建议删除；
      n≥10 且 <40% 弱建议（样本偏少，仅供参考）；
      n 默认取本次任务，传 agg_rows（该品牌全部历史任务**跨任务按 URL 去重后**的
      聚合漏斗，即 aggregate_unique(history)["funnel"]）时取累计样本。
    """
    subject = cur.get("subject") or base.get("subject") or ""
    ws_base = _websearch_rows(base)
    ws_cur = _websearch_rows(cur)
    b = _agg_rates(ws_base)
    c = _agg_rates(ws_cur)

    delta_pp = None
    if b["effective_rate"] is not None and c["effective_rate"] is not None:
        delta_pp = round((c["effective_rate"] - b["effective_rate"]) * 100, 1)
    if delta_pp is None:
        eff_verdict = "无法判定（缺少有效率数据）"
    elif delta_pp >= 5:
        eff_verdict = "达标（≥+5pp）"
    elif delta_pp > 0:
        eff_verdict = "未达标（提升不足 5pp）"
    else:
        eff_verdict = "下降"

    def _pure(rows: list[dict]) -> dict:
        if not subject:
            return {"present": False}
        group = [r for r in rows if r.get("keyword") == subject]
        if not group:
            return {"present": False}
        col = sum(r.get("collected", 0) for r in group)
        drop = sum(r.get("dropped", 0) for r in group)
        return {
            "present": True,
            "collected": col,
            "dropped": drop,
            "drop_rate": (drop / col * 100) if col else 0.0,
        }

    pb = _pure(ws_base)
    pc = _pure(ws_cur)
    pure: dict = {"present": False}
    if not pb["present"] and not pc["present"]:
        pure = {
            "present": False,
            "note": "两次任务均无「确认关键词=品牌名」的查询，该指标无法判定",
        }
    else:
        before_drop = pb.get("drop_rate") if pb["present"] else None
        after_drop = pc.get("drop_rate") if pc["present"] else None
        if after_drop is None:
            verdict = "无法判定（本次无品牌名关键词）"
        else:
            verdict = "达标（<15%）" if after_drop < 15 else "未达标（≥15%）"
        pure = {
            "present": True,
            "before_drop_pct": round(before_drop, 1) if before_drop is not None else None,
            "after_drop_pct": round(after_drop, 1) if after_drop is not None else None,
            "verdict": verdict,
        }

    fm_base = {(r.get("channel"), r.get("query")): r for r in ws_base}
    fm_cur = {(r.get("channel"), r.get("query")): r for r in ws_cur}
    new_rows = [r for k, r in fm_cur.items() if k not in fm_base]
    new_agg = _agg_rates(new_rows)

    channels = sorted({r.get("channel") for r in ws_base} | {r.get("channel") for r in ws_cur})
    per_channel = []
    for ch in channels:
        a = _agg_rates([r for r in ws_base if r.get("channel") == ch])
        d = _agg_rates([r for r in ws_cur if r.get("channel") == ch])
        dp = None
        if a["effective_rate"] is not None and d["effective_rate"] is not None:
            dp = round((d["effective_rate"] - a["effective_rate"]) * 100, 1)
        per_channel.append({
            "channel": ch,
            "before_rate": a["effective_rate"],
            "after_rate": d["effective_rate"],
            "before_n": a["collected"],
            "after_n": d["collected"],
            "delta_pp": dp,
        })

    strat = strategy if strategy is not None else load_keyword_strategy()
    low_source = (
        [r for r in agg_rows if str(r.get("channel", "")).startswith("websearch")]
        if agg_rows is not None else ws_cur
    )
    word_rows: dict[str, list[dict]] = defaultdict(list)
    for r in low_source:
        w = _strip_query(r.get("query", ""))
        if not w or w == subject:
            continue
        word_rows[w].append(r)
    low: list[dict] = []
    for w, rows in word_rows.items():
        a = _agg_rates(rows)
        eff = a["effective_rate"]
        col = a["collected"]
        if eff is None:
            continue
        if col >= 30 and eff < 0.50:
            level, reason = "强建议删除", f"样本 {col}≥30 且有效供给率 {eff:.0%}<50%"
        elif col >= 10 and eff < 0.40:
            level, reason = "弱建议（参考）", f"样本 {col}≥10 且有效供给率 {eff:.0%}<40%，样本偏少"
        else:
            continue
        extra = (strat.get("extra_queries") or {}).get(subject, [])
        pool = strat.get("suffix_pool") or []
        low.append({
            "word": w,
            "collected": col,
            "kept": a["kept"],
            "effective_rate": round(eff * 100, 1),
            "channels": "、".join(sorted({r.get("channel") for r in rows})),
            "level": level,
            "reason": reason,
            "in_strategy": w in extra or w in pool,
            "in_extra": w in extra,
            "in_suffix": w in pool,
        })
    low.sort(key=lambda x: (x["level"] != "强建议删除", x["effective_rate"]))

    strong_n = sum(1 for x in low if x["level"] == "强建议删除")
    weak_n = len(low) - strong_n
    btxt = f"{b['effective_rate'] * 100:.1f}%" if b["effective_rate"] is not None else "—"
    ctxt = f"{c['effective_rate'] * 100:.1f}%" if c["effective_rate"] is not None else "—"
    parts = [
        f"有效供给率 {btxt}（n={b['collected']}）→ {ctxt}（n={c['collected']}），{eff_verdict}。"
    ]
    if pure.get("present"):
        btxt = f"{pure.get('before_drop_pct')}%" if pure.get("before_drop_pct") is not None else "—"
        atxt = f"{pure.get('after_drop_pct')}%" if pure.get("after_drop_pct") is not None else "—"
        parts.append(
            f"纯品牌词丢弃率 {btxt} → {atxt}（{pure['verdict']}）。"
        )
    else:
        parts.append(pure.get("note", "") + "。")
    if new_agg["collected"]:
        parts.append(
            f"新增 {len(new_rows)} 个查询串，采集 {new_agg['collected']} 保留 "
            f"{new_agg['kept']}（有效供给率 "
            f"{new_agg['effective_rate'] * 100:.1f}%）。"
        )
    else:
        parts.append("无新增查询串。")
    if low:
        parts.append(f"建议删除 {strong_n} 个、参考删除 {weak_n} 个低效词。")
    else:
        parts.append("未发现明显低效词。")

    return {
        "effective": {
            "before_pct": round(b["effective_rate"] * 100, 1) if b["effective_rate"] is not None else None,
            "after_pct": round(c["effective_rate"] * 100, 1) if c["effective_rate"] is not None else None,
            "before_n": b["collected"],
            "after_n": c["collected"],
            "delta_pp": delta_pp,
            "verdict": eff_verdict,
        },
        "pure_brand": pure,
        "new_queries": {
            "count": len(new_rows),
            "collected": new_agg["collected"],
            "kept": new_agg["kept"],
            "effective_pct": round(new_agg["effective_rate"] * 100, 1)
            if new_agg["effective_rate"] is not None else None,
        },
        "per_channel": per_channel,
        "low_efficiency": low,
        "conclusion": " ".join(parts),
    }


def write_candidates(agg: dict, out: Path | None = None) -> Path:
    """把聚合后的候选提示写成 CSV（人工确认表）。"""
    out = out or candidates_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for suf, st in agg.get("suffix_stats", {}).items():
        ref = "参考（证据<30）" if st["collected"] < REF_N else ""
        rows.append({
            "类型": "后缀", "主题": "", "候选": suf,
            "证据n": st["n_queries"], "有效供给率": st["effective_rate"],
            "示例1": "", "示例2": "", "备注": ref,
        })
    for v in agg.get("cooccur", {}).values():
        ex = v.get("examples") or []
        rows.append({
            "类型": "查询候选", "主题": v["subject"],
            "候选": f"{v['subject']} {v['token']}",
            "证据n": v["n"], "有效供给率": "",
            "示例1": ex[0] if len(ex) > 0 else "",
            "示例2": ex[1] if len(ex) > 1 else "", "备注": "",
        })
    for tok, v in agg.get("alias", {}).items():
        ex = v.get("examples") or []
        rows.append({
            "类型": "同义词", "主题": v.get("subject", ""),
            "候选": tok, "证据n": v["n"], "有效供给率": "",
            "示例1": ex[0] if len(ex) > 0 else "",
            "示例2": ex[1] if len(ex) > 1 else "", "备注": "",
        })
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "类型", "主题", "候选", "证据n", "有效供给率", "示例1", "示例2", "备注",
        ])
        w.writeheader()
        w.writerows(rows)
    return out


def load_candidates(path: Path | None = None) -> list[dict]:
    p = path or candidates_path()
    if not p.exists():
        return []
    with open(p, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import argparse

    ap = argparse.ArgumentParser(description="关键词效果数据层与候选反推（2.2）")
    ap.add_argument("--scan", type=Path, default=None,
                    help="扫描 result.json 文件或目录（递归），入历史（按内容去重）")
    ap.add_argument("--scan-dir", type=Path, default=None,
                    help="等价 --scan 指向目录（默认 data/reports）")
    ap.add_argument("--report", action="store_true", help="打印聚合漏斗摘要")
    ap.add_argument("--candidates", action="store_true",
                    help="根据历史生成候选确认 CSV")
    ap.add_argument("--out", type=Path, default=None, help="候选 CSV 输出路径")
    ap.add_argument("--rescan", action="store_true",
                    help="按历史 source 重扫全部任务（force 重写 run JSON 补齐 URL 明细）")
    args = ap.parse_args()

    if args.rescan:
        hist = load_history()
        rewritten = 0
        for h in hist:
            src = h.get("source")
            if src and Path(src).exists():
                try:
                    if scan_report(Path(src), force=True) is not None:
                        rewritten += 1
                except (OSError, ValueError):
                    continue
        print(f"重扫 {len(hist)} 个历史任务，重写 {rewritten} 个 run JSON"
              f"（补齐 URL 明细，不重复追加历史）")
    if args.scan or args.scan_dir:
        target = args.scan or args.scan_dir
        added = scan_path(target)
        print(f"扫描 {target}：新增 {len(added)} 个任务（历史共 "
              f"{len(load_history())} 条）")
        for p in added:
            print(f"  + {p.get('source')} subject={p.get('subject')} "
                  f"funnel={len(p.get('funnel') or [])} 行 "
                  f"llm_cost={p.get('llm_cost') or 0}")
    if args.report:
        agg = aggregate_unique(load_history())
        print(f"\n聚合（{agg['tasks']} 个任务）：LLM 费用合计 "
              f"{agg['llm_cost']} 元，未归属丢弃 {agg['unattributed_dropped']} 条")
        if agg.get("dedup_missing"):
            print(f"⚠ 去重口径：{len(agg['dedup_missing'])} 个旧任务无 URL 明细已排除"
                  f"（运行 --rescan 补齐）")
        print(f"{'查询串':<32}{'渠道':<14}{'采集':>6}{'保留':>6}{'丢弃':>6}"
              f"{'编码':>6}{'有效供给率':>10}{'负面率':>8}")
        for r in agg["funnel"][:20]:
            eff = f"{r['effective_rate']:.1%}" if r["effective_rate"] is not None else "-"
            neg = f"{r['negative_rate']:.1%}" if r["negative_rate"] is not None else "-"
            print(f"{r['query'][:30]:<32}{r['channel'][:12]:<14}"
                  f"{r['collected']:>6}{r['kept']:>6}{r['dropped']:>6}"
                  f"{r['coded']:>6}{eff:>10}{neg:>8}")
    if args.candidates:
        out = write_candidates(aggregate(load_history()), args.out)
        print(f"候选清单：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
