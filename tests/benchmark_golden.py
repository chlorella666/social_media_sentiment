# -*- coding: utf-8 -*-
"""黄金集基准评测：词典直判基线 / 混合流水线（词典 + LLM）。

规范依据：docs/抽样与标注规范.md §八（评测脚本输出口径）

用法：
    python tests/benchmark_golden.py                  # 词典直判（无 Key，确定性，可作基线）
    python tests/benchmark_golden.py --llm            # 混合流水线（低置信度送 LLM，需 API Key）
    python tests/benchmark_golden.py --golden data/datasets/golden_set_v1.csv

输出：
    data/datasets/benchmark_report.json               结构化报告
    docs/评测记录.md                                   追加一段结果（--record，默认开）

口径（2026-08-07 定版）：
    - 主评分只统计 relevant=yes 的样本；relevant=no 单独统计，不计入情感准确率
    - 语言现象子集报告附带 n（样本 <10 的类别仅作参考）
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core import eval_store  # noqa: E402

DEFAULT_GOLDEN = ROOT / "data" / "datasets" / "golden_set_v1.csv"
REPORT_JSON = ROOT / "data" / "datasets" / "benchmark_report.json"
RECORD_DOC = ROOT / "docs" / "评测记录.md"
CLEANING_GOLDEN = ROOT / "data" / "datasets" / "golden_set_v1_cleaning.csv"
FIXTURE_GOLDEN = ROOT / "tests" / "fixtures" / "golden_set_v1.csv"
# 优先使用入库版（tests/fixtures，跨机器可复现）；缺失时回退 data/ 工作版
DEFAULT_GOLDEN = FIXTURE_GOLDEN if FIXTURE_GOLDEN.exists() else DEFAULT_GOLDEN
EDGE_FIXTURE = ROOT / "tests" / "fixtures" / "edge_set_v1.csv"
EDGE_REPORT_JSON = ROOT / "data" / "datasets" / "edge_benchmark_report.json"

VALID_SENTIMENTS = {"positive", "negative", "neutral"}
SENTIMENT_CLASSES = ("positive", "negative", "neutral")
SUBSET_CN = {
    "irony": "反讽", "jargon": "黑话", "dialect": "方言",
    "long": "长文本", "emoji": "emoji主导", "lowconf": "低置信",
}


def load_golden(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            r["dimension_sentiments"] = json.loads(r["dimension_sentiments"] or "{}")
            rows.append(r)
    return rows


REVISED_BY_MODEL_TRUE = {"是", "yes", "true", "1"}


def is_gold_revised_by_model(v) -> bool:
    """gold 修订是否参考过模型判定（V2 敏感性复算的过滤条件）。"""
    return (v or "").strip().lower() in REVISED_BY_MODEL_TRUE


def filter_gold_revised(rows: list[dict], exclude: bool) -> tuple[list[dict], list[dict]]:
    """--exclude-gold-revised-by-model：剔除「修订参考过模型」的 gold 行后复算。"""
    if not exclude:
        return rows, []
    kept, dropped = [], []
    for r in rows:
        (dropped if is_gold_revised_by_model(r.get("gold_revised_by_model")) else kept).append(r)
    return kept, dropped


def load_schema(domain: str) -> dict[str, dict]:
    """领域维度 schema（2.5 起走统一加载：预置 + 缓存，缓存优先）。"""
    from app.domains.loader import load_domain

    try:
        schema = load_domain(domain)
    except Exception:
        return {}
    return {
        dim.id: {"name": dim.name, "keywords": dim.keywords}
        for dim in schema.dimensions
    }


def get_llm_analyzer():
    from app.coding.llm_analyzer import LLMConfig, OpenAICompatibleAnalyzer
    from app.core.secrets import ensure_legacy_key_migrated, load_api_key

    import os

    # 旧明文 Key 一次性迁移（幂等；导入并验证后删除明文文件）
    ensure_legacy_key_migrated()
    # 开发脚本通道：DPAPI → OPENAI_API_KEY（1.5 决策，env 仅限开发用途）
    api_key = load_api_key(allow_env=True)
    if not api_key:
        raise SystemExit(
            "--llm 需要 API Key：应用侧边栏保存（Windows DPAPI）或设置 OPENAI_API_KEY"
        )
    config = LLMConfig(
        api_key=api_key,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
        model=os.environ.get("OPENAI_MODEL", "deepseek-chat"),
    )
    return OpenAICompatibleAnalyzer(config)


def predict_all(rows: list[dict], use_llm: bool, llm=None) -> list[dict]:
    from app.coding import lexicon_v2 as lexicon
    from app.coding.cleaner import clean_text, desensitize_text
    from app.coding.dimensions import match_dimension_sentiments
    from app.coding.llm_analyzer import (
        CONFIDENCE_THRESHOLD,
        is_always_llm_domain,
        uses_subject_domain,
    )

    schemas = {d: load_schema(d) for d in {r["domain"] for r in rows}}
    out = []
    llm_texts, llm_idx = [], []
    for i, r in enumerate(rows):
        text = clean_text(r["text"] or "")
        pre = lexicon.score_text(text)
        force_llm = is_always_llm_domain(r["domain"])
        # 2.8 新口径（2026-08-18）：混合模式全量送 LLM（direct=False），
        # 与生产"llm_enabled → 全量 LLM"一致；词典模式（无 --llm）保持原阈值口径
        direct = (not use_llm) and (not force_llm) and pre["confidence"] >= CONFIDENCE_THRESHOLD
        schema = schemas.get(r["domain"], {})
        # 维度情感（2.4 词典兜底口径）：gold 用维度中文名，预测同样用中文名
        dim_sents = match_dimension_sentiments(r["text"] or "", schema, use_names=True)
        pred = {
            "sentiment": pre["sentiment"],
            "lexicon_sentiment": pre["sentiment"],
            "direct": direct,
            "llm_used": False,
            "confidence": pre["confidence"],
            "dims": [],
            "dimension_sentiments": dim_sents,
        }
        pred["dims"] = [
            d["name"] for d in schema.values()
            if any(kw.lower() in (r["text"] or "").lower() for kw in d["keywords"])
        ]
        out.append(pred)
        if use_llm and not direct:
            # P1-7：发 LLM 前脱敏（黄金集/边界集同口径）；词典预筛仍用清洗后原文
            llm_texts.append(desensitize_text(text))
            llm_idx.append(i)
    if llm_texts:
        # 2.4：LLM v3.0 输出维度情感；按领域分组调用（schema 不同，不能混批）
        from collections import defaultdict

        groups: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
        for pos, i in enumerate(llm_idx):
            groups[(rows[i]["domain"], rows[i].get("brand") or "")].append((pos, i))
        results: list[dict | None] = [None] * len(llm_texts)
        for (domain, brand), idxs in groups.items():
            subject = brand if uses_subject_domain(domain) and brand else None
            part = llm.analyze_batch(
                [llm_texts[pos] for pos, _ in idxs],
                dimension_schema=schemas[domain],
                subject=subject,
            )
            for (pos, _), res in zip(idxs, part):
                results[pos] = res
        for i, res in zip(llm_idx, results):
            if res and res.get("sentiment") in VALID_SENTIMENTS:
                out[i]["sentiment"] = res["sentiment"]
                out[i]["llm_used"] = True
                if res.get("confidence") is not None:
                    out[i]["confidence"] = float(res["confidence"])
            llm_dims = (res or {}).get("dimension_sentiments")
            if isinstance(llm_dims, dict) and llm_dims:
                schema = schemas[rows[i]["domain"]]
                name_map = {d_id: info["name"] for d_id, info in schema.items()}
                out[i]["dimension_sentiments"] = {
                    name_map.get(d_id, d_id): v
                    for d_id, v in llm_dims.items()
                    if v in ("positive", "negative")
                }
    return out


def accuracy(pred_sents, golden_sents):
    n = len(pred_sents)
    return (sum(a == b for a, b in zip(pred_sents, golden_sents)) / n) if n else 0.0


def group_report(rows, preds, key_fn):
    groups = defaultdict(list)
    for r, p in zip(rows, preds):
        groups[key_fn(r)].append((p["sentiment"], r["sentiment"]))
    out = {}
    for k, v in sorted(groups.items()):
        acc = round(accuracy(*zip(*v)), 4)
        out[k] = {"n": len(v), "accuracy": acc,
                  "ci_pp": eval_store.ci_half_pp(acc, len(v))}
    return out


def sentiment_class_metrics(pred_sents, gold_sents):
    """情感类别级 P/R/F1（one-vs-rest）；负面召回是负面监测场景的核心指标。"""
    out = {}
    for cls in SENTIMENT_CLASSES:
        tp = sum(1 for p, g in zip(pred_sents, gold_sents) if p == cls and g == cls)
        fp = sum(1 for p, g in zip(pred_sents, gold_sents) if p == cls and g != cls)
        fn = sum(1 for p, g in zip(pred_sents, gold_sents) if g == cls and p != cls)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        out[cls] = {
            "n_gold": gold_sents.count(cls),
            "n_pred": pred_sents.count(cls),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }
    return out


def dimension_report(rows, preds):
    """维度级情感（2.4 口径）：gold/pred dimension_sentiments 逐维比对。

    主指标：
      - 微平均 P/R/F1：按 (维度, 情感) 对统计——预测值与 gold 完全一致为 TP，
        情感标错或幻觉维度为 FP，漏标为 FN；
      - 精确命中率：整条维度情感集合（含情感值）完全一致的比例；
      - per_dimension：每维情感 P/R/F1（含 n_gold）；
    mention 子报告保留旧口径（维度提及识别，2.3 及之前的指标）供诊断对比。
    """
    exact_t, exact_n = 0, 0
    tp, fp, fn = 0, 0, 0
    mention_tp, mention_fp, mention_fn = 0, 0, 0
    all_dims = sorted(
        {d for r in rows for d in r["dimension_sentiments"]}
        | {d for p in preds for d in p.get("dimension_sentiments") or {}}
        | {d for p in preds for d in (p.get("dims") or [])}
    )
    per_dim: dict = {}
    mention_per: dict = {}
    dist: dict = {}
    for dim in all_dims:
        pt = pf = pn = 0
        mt = mf = mn = 0
        gpos = gneg = ppos = pneg = 0
        for r, p in zip(rows, preds):
            g = r["dimension_sentiments"]
            pr = p.get("dimension_sentiments") or {}
            gold_v = g.get(dim)
            pred_v = pr.get(dim)
            if gold_v == "positive":
                gpos += 1
            elif gold_v == "negative":
                gneg += 1
            if pred_v == "positive":
                ppos += 1
            elif pred_v == "negative":
                pneg += 1
            # 维度情感（2.4）：值一致为 TP；值不一致 = FP + FN；单侧缺失按 FN/FP
            if pred_v and gold_v and pred_v == gold_v:
                pt += 1
            elif pred_v and gold_v:
                pf += 1
                pn += 1
            elif pred_v:
                pf += 1
            elif gold_v:
                pn += 1
            # 提及识别（旧口径，供诊断）
            dim_gold = dim in g
            dim_pred = dim in pr
            if dim_pred and dim_gold:
                mt += 1
            elif dim_pred and not dim_gold:
                mf += 1
            elif not dim_pred and dim_gold:
                mn += 1
        precision = pt / (pt + pf) if pt + pf else 0.0
        recall = pt / (pt + pn) if pt + pn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_dim[dim] = {
            "n_gold": sum(1 for r in rows if dim in r["dimension_sentiments"]),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }
        m_precision = mt / (mt + mf) if mt + mf else 0.0
        m_recall = mt / (mt + mn) if mt + mn else 0.0
        m_f1 = 2 * m_precision * m_recall / (m_precision + m_recall) if m_precision + m_recall else 0.0
        mention_per[dim] = {
            "precision": round(m_precision, 4),
            "recall": round(m_recall, 4),
            "f1": round(m_f1, 4),
        }
        dist[dim] = {
            "gold_positive": gpos, "gold_negative": gneg,
            "pred_positive": ppos, "pred_negative": pneg,
        }
        tp += pt
        fp += pf
        fn += pn
        mention_tp += mt
        mention_fp += mf
        mention_fn += mn
    micro_p = tp / (tp + fp) if tp + fp else 0.0
    micro_r = tp / (tp + fn) if tp + fn else 0.0
    micro_f1 = 2 * micro_p * micro_r / (micro_p + micro_r) if micro_p + micro_r else 0.0
    m_micro_p = mention_tp / (mention_tp + mention_fp) if mention_tp + mention_fp else 0.0
    m_micro_r = mention_tp / (mention_tp + mention_fn) if mention_tp + mention_fn else 0.0
    m_micro_f1 = 2 * m_micro_p * m_micro_r / (m_micro_p + m_micro_r) if m_micro_p + m_micro_r else 0.0
    for r, p in zip(rows, preds):
        if r["dimension_sentiments"]:
            exact_n += 1
            if dict(p.get("dimension_sentiments") or {}) == dict(r["dimension_sentiments"]):
                exact_t += 1
    return {
        "口径": "维度级情感（每维 positive/negative，2.4）",
        "exact_match_rate": round(exact_t / exact_n, 4) if exact_n else None,
        "exact_match_n": exact_n,
        "dimension_n": exact_n,
        "micro_precision": round(micro_p, 4),
        "micro_recall": round(micro_r, 4),
        "micro_f1": round(micro_f1, 4),
        "per_dimension": per_dim,
        "dimension_sentiment_dist": dist,
        "mention": {
            "口径": "维度提及识别（旧口径，2.3 及之前）",
            "micro_precision": round(m_micro_p, 4),
            "micro_recall": round(m_micro_r, 4),
            "micro_f1": round(m_micro_f1, 4),
            "per_dimension": mention_per,
        },
    }


def cleaning_report():
    """清洗验证集：人工确认一致率 + 清洗规则命中率（近似）。

    注意：重复类与官网域名黑名单依赖完整采集上下文（同批去重/URL 黑名单配置），
    无法仅凭单条标题复现，命中率为近似值，不构成误杀率证明。
    """
    if not CLEANING_GOLDEN.exists():
        return {"status": "no_golden", "note": "golden_set_v1_cleaning.csv 不存在，先运行 tests/finalize_golden_set.py"}
    from app.coding import cleaner

    rows = list(csv.DictReader(open(CLEANING_GOLDEN, encoding="utf-8-sig")))
    n = len(rows)
    confirmed = sum(1 for r in rows if r.get("correct_drop") == "应丢弃")
    hits = cat_match = 0
    unreproducible = []
    for r in rows:
        title = r.get("title") or ""
        cat = r.get("reason_category") or ""
        hits_now = []
        if cleaner.is_boilerplate(title):
            hits_now.append("样板/页面壳")
        if cleaner.is_official_page(title):
            hits_now.append("官方页面")
        if len(cleaner.clean_text(title)) < 10:
            hits_now.append("文本过短")
        if cat == "不相关" and (not r.get("brand") or r["brand"] not in title):
            hits_now.append("不相关")
        if cat == "官网域名黑名单" and ("官网" in title or "首页" in title):
            hits_now.append("官方页面")
        if hits_now:
            hits += 1
            if cat in hits_now or (cat == "官网域名黑名单" and "官方页面" in hits_now):
                cat_match += 1
        else:
            unreproducible.append({"text_id": r["text_id"], "reason": cat, "title": title[:40]})
    return {
        "n": n,
        "confirmed_drop": confirmed,
        "human_agreement": round(confirmed / n, 4) if n else None,
        "rule_hit_rate": round(hits / n, 4) if n else None,
        "category_match_rate": round(cat_match / n, 4) if n else None,
        "by_reason": dict(Counter(r.get("reason_category") for r in rows)),
        "unreproducible": unreproducible[:20],
        "note": "重复类与官网域名黑名单需完整采集上下文才能复现，命中率为近似值",
    }


def calibration_report(rows: list[dict], preds: list[dict]) -> dict:
    """按置信度分桶的准确率（2.3 校准检查：越有把握应越准，低置信不单调需校准）。

    词典直判用词典置信度，LLM 判定用 LLM 自报置信度；缺失置信度单独计数。
    """
    buckets = ((0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0001))
    out: dict = {}
    missing: list[tuple[str, str]] = []
    for lo, hi in buckets:
        pairs = [
            (p["sentiment"], r["sentiment"])
            for r, p in zip(rows, preds)
            if p.get("confidence") is not None and lo <= p["confidence"] < hi
        ]
        out[f"{lo:.1f}-{hi:.1f}"] = {
            "n": len(pairs),
            "accuracy": round(accuracy(*zip(*pairs)), 4) if pairs else None,
        }
    for r, p in zip(rows, preds):
        if p.get("confidence") is None:
            missing.append((p["sentiment"], r["sentiment"]))
    out["缺失置信度"] = {
        "n": len(missing),
        "accuracy": round(accuracy(*zip(*missing)), 4) if missing else None,
    }
    return out


def main():
    ap = argparse.ArgumentParser(description="黄金集基准评测")
    ap.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    ap.add_argument("--edge", action="store_true",
                    help="边界集评测模式（默认 tests/fixtures/edge_set_v1.csv，"
                         "按子集/子集×渠道/分桶校准报告，独立 mode_key）")
    ap.add_argument("--mode-key", type=str, default=None,
                    help="自定义评测键（2.5 新领域用，如 domain_digital3c_lexicon；"
                         "独立历史与基线，避免污染主集/边界集）")
    ap.add_argument("--llm", action="store_true", help="混合流水线（词典+LLM），需 API Key")
    ap.add_argument(
        "--record", dest="record", action="store_true", default=True,
        help="追加结果到 docs/评测记录.md",
    )
    ap.add_argument(
        "--no-record", dest="record", action="store_false",
        help="不追加 docs/评测记录.md（回归/CI 用，避免污染文档）",
    )
    ap.add_argument(
        "--report-out", type=Path, default=None,
        help="报告 JSON 输出路径（默认 data/datasets/benchmark_report.json）",
    )
    ap.add_argument(
        "--no-history", dest="history", action="store_false", default=True,
        help="不写评测历史与对比（临时跑分用）",
    )
    ap.add_argument(
        "--exclude-gold-revised-by-model", action="store_true",
        help="V2 敏感性复算：剔除「gold 修订参考过模型判定」的行再统计",
    )
    args = ap.parse_args()

    golden = args.golden
    if args.edge and args.golden == DEFAULT_GOLDEN:
        golden = EDGE_FIXTURE
    if not golden.exists():
        raise SystemExit(f"数据集不存在：{golden}（主集先运行 tests/finalize_golden_set.py；"
                         f"边界集先运行 tests/apply_edge_review.py）")
    rows = load_golden(golden)
    rows, gold_revised_dropped = filter_gold_revised(rows, args.exclude_gold_revised_by_model)
    if gold_revised_dropped:
        print(f"[V2] 已剔除 gold 修订参考过模型判定的行：{len(gold_revised_dropped)} 条"
              f"（{', '.join(r['text_id'] for r in gold_revised_dropped[:10])}）")
    relevant_rows = [r for r in rows if r.get("relevant", "yes") == "yes"]
    irrelevant_rows = [r for r in rows if r.get("relevant", "yes") != "yes"]
    if not relevant_rows:
        raise SystemExit("数据集没有 relevant=yes 样本，无法计算主评分")

    llm = get_llm_analyzer() if args.llm else None
    preds = predict_all(relevant_rows, args.llm, llm)
    if args.edge:
        mode_key = "edge_hybrid" if args.llm else "edge_lexicon"
        mode = "边界集-混合流水线（词典+LLM）" if args.llm else "边界集-词典直判（无 LLM）"
    elif args.mode_key:
        mode_key = args.mode_key
        mode = "自定义领域评测（词典）" if not args.llm else "自定义领域评测（混合）"
    else:
        mode_key = "hybrid" if args.llm else "lexicon"
        mode = "混合流水线（词典+LLM）" if args.llm else "词典直判（无 LLM）"

    overall = accuracy([p["sentiment"] for p in preds], [r["sentiment"] for r in relevant_rows])
    overall_acc = round(overall, 4)
    confusion = Counter((p["sentiment"], r["sentiment"]) for p, r in zip(preds, relevant_rows))
    dim_rep = dimension_report(relevant_rows, preds)
    pred_sents = [p["sentiment"] for p in preds]
    gold_sents = [r["sentiment"] for r in relevant_rows]
    errors = [
        {
            "text_id": r["text_id"],
            "text": r["text"],
            "gold": r["sentiment"],
            "pred": p["sentiment"],
            "platform": r["platform"],
            "domain": r["domain"],
            "kind": r["kind"],
            "flags": r["language_flags"],
            "subset": r.get("subset", ""),
            "llm_used": p["llm_used"],
        }
        for r, p in zip(relevant_rows, preds)
        if p["sentiment"] != r["sentiment"]
    ]
    # 2.4：维度错误样本下钻（gold vs pred dimension_sentiments 不一致；供失败模式分析）
    dimension_errors = [
        {
            "text_id": r["text_id"],
            "text": r["text"],
            "gold": r["dimension_sentiments"],
            "pred": p.get("dimension_sentiments") or {},
            "subset": r.get("subset", ""),
            "platform": r["platform"],
            "domain": r["domain"],
            "llm_used": p["llm_used"],
        }
        for r, p in zip(relevant_rows, preds)
        if (r["dimension_sentiments"] or {}) != (p.get("dimension_sentiments") or {})
    ]
    direct_n = sum(1 for p in preds if p["direct"])
    llm_n = sum(1 for p in preds if p["llm_used"])
    llm_fix = sum(1 for p in preds if p["llm_used"] and p["sentiment"] != p["lexicon_sentiment"])
    flags = defaultdict(list)
    for r, p in zip(relevant_rows, preds):
        for f in (r["language_flags"] or "").split(","):
            if f:
                flags[f].append((p["sentiment"], r["sentiment"]))
    flag_acc = {f: {"n": len(v), "accuracy": round(accuracy(*zip(*v)), 4)} for f, v in sorted(flags.items())}

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode_key": mode_key,
        "golden": str(golden),
        "golden_fingerprint": eval_store.golden_fingerprint(golden),
        "golden_rows": len(rows),
        "scope": {
            "main_n": len(relevant_rows),
            "irrelevant_excluded_n": len(irrelevant_rows),
            "gold_revised_excluded_n": len(gold_revised_dropped),
            "口径": "主评分仅统计 relevant=yes；relevant=no 单独统计，不计入情感准确率",
        },
        "irrelevant": {
            "n": len(irrelevant_rows),
            "golden_sentiments": dict(Counter(r["sentiment"] for r in irrelevant_rows)),
            "note": "规范 §五 规则 8：不相关内容情感按 neutral 标注，单独统计",
        },
        "mode": mode,
        "sentiment_accuracy": overall_acc,
        "accuracy_ci_pp": eval_store.ci_half_pp(overall_acc, len(relevant_rows)),
        "confusion_matrix": {f"{a}->{b}": c for (a, b), c in sorted(confusion.items())},
        "by_domain": group_report(relevant_rows, preds, lambda r: r["domain"]),
        "by_channel": group_report(relevant_rows, preds, lambda r: r["platform"]),
        "by_kind": group_report(relevant_rows, preds, lambda r: r["kind"]),
        "by_flag": flag_acc,
        "by_gold_sentiment": group_report(relevant_rows, preds, lambda r: r["sentiment"]),
        "by_sentiment_class": sentiment_class_metrics(pred_sents, gold_sents),
        "dimension": dim_rep,
        "routing": {
            "direct_lexicon": direct_n,
            "llm_used": llm_n,
            "llm_corrected": llm_fix,
            "direct_rate": round(direct_n / len(relevant_rows), 4) if relevant_rows else 0,
        },
        "cleaning": cleaning_report(),
        "errors": errors,
        "dimension_errors": dimension_errors[:300],
    }
    if args.llm and llm is not None:
        report["llm_usage"] = llm.usage
        from app.coding.llm_analyzer import PROMPT_VERSION

        report["prompt_version"] = PROMPT_VERSION
    if args.edge:
        report["by_subset"] = group_report(relevant_rows, preds, lambda r: r["subset"])
        report["by_subset_channel"] = group_report(
            relevant_rows, preds, lambda r: f"{r['subset']}|{r['platform']}"
        )
        report["calibration"] = calibration_report(relevant_rows, preds)
        report["scope"]["subset_counts"] = dict(
            Counter(r["subset"] for r in relevant_rows)
        )
        report["cleaning"] = {"status": "no_golden",
                              "note": "边界集不参与主集清洗验证集"}

    comparison = None
    if args.history:
        history = eval_store.load_history()
        prev = eval_store.find_previous(eval_store.build_summary(report), history)
        baseline = eval_store.load_frozen_baseline(report["mode_key"])
        run_info = eval_store.write_run(report, previous=prev, baseline=baseline)
        report["comparison"] = run_info["comparison"]
        comparison = run_info["comparison"]
        if golden.name.startswith("holdout_"):
            # V1 纪律：hold-out 每卷 ≤2 次验收（best-effort 记录，失败不阻塞跑分）
            try:
                from tests.coldstart_annotation import record_holdout_use

                res = record_holdout_use(golden)
                if res.get("ok"):
                    print(f"[V1] hold-out 验收已记录：{res['uses']}/{res['max_uses']} 次")
                else:
                    print(f"[V1] hold-out 使用记录：{res.get('error')}")
            except Exception as exc:
                print(f"[V1] hold-out 使用记录失败（不阻塞）：{exc}")

    print(f"{'边界集' if args.edge else '黄金集'}：{len(rows)} 条"
          f"（主评分 {len(relevant_rows)}，排除 relevant=no {len(irrelevant_rows)}） | 模式：{mode}")
    print(f"整条情感准确率：{overall:.1%}"
          f"（95% CI ±{report['accuracy_ci_pp']}pp）")
    if args.edge:
        print("  按子集：", {k: f"{v['accuracy']:.1%}(n={v['n']})" for k, v in report["by_subset"].items()})
        print("  置信度分桶校准：", {
            k: (f"{v['accuracy']:.1%}(n={v['n']})" if v["accuracy"] is not None else "n=0")
            for k, v in report["calibration"].items()
        })
    print("  按领域：", {k: f"{v['accuracy']:.1%}({v['n']})" for k, v in report["by_domain"].items()})
    print("  按渠道：", {k: f"{v['accuracy']:.1%}({v['n']})" for k, v in report["by_channel"].items()})
    emr = dim_rep["exact_match_rate"]
    emr_s = f"{emr:.1%}" if emr is not None else "无维度标注"
    print(f"维度：精确命中 {emr_s}（n={dim_rep['exact_match_n']}），"
          f"微平均 P/R/F1 = {dim_rep['micro_precision']:.2f}/"
          f"{dim_rep['micro_recall']:.2f}/{dim_rep['micro_f1']:.2f}")
    print("  每维 P/R/F1：", {k: f"{v['precision']:.2f}/{v['recall']:.2f}/{v['f1']:.2f}" for k, v in dim_rep["per_dimension"].items()})
    print("路由：", report["routing"])
    print("语言现象子集准确率：", {k: f"{v['accuracy']:.1%}(n={v['n']})" for k, v in flag_acc.items()})
    print("情感类别 P/R/F1：", {k: f"{v['precision']:.2f}/{v['recall']:.2f}/{v['f1']:.2f}"
                               for k, v in report["by_sentiment_class"].items()})
    print(f"错误样本：{len(errors)} 条（明细见运行报告 errors）")
    if comparison:
        vp = comparison.get("vs_previous")
        if vp:
            d = vp.get("overall_delta_pp")
            if d is not None:
                w = vp.get("overall_delta_ci_pp")
                w_s = f"，Δ CI ±{w}pp" if w is not None else ""
                print(f"对比（vs 上次 {vp.get('prev_ts')}）：整体 {d:+.1f}pp"
                      f"{w_s}（{vp.get('status')}）")
            else:
                print(f"对比（vs 上次 {vp.get('prev_ts')}）：无准确率可比")
        vb = comparison.get("vs_baseline")
        if vb and vb.get("delta_pp") is not None:
            w = vb.get("delta_ci_pp")
            w_s = f"，Δ CI ±{w}pp" if w is not None else ""
            print(f"对比（vs 冻结基线 {vb.get('baseline')}）：整体 {vb['delta_pp']:+.1f}pp"
                  f"{w_s}（{vb['status']}）")
    cl = report["cleaning"]
    if cl.get("status") == "no_golden":
        print("清洗验证集：", cl["note"])
    else:
        print(f"清洗验证集：{cl['n']} 条，确认应丢弃 {cl['confirmed_drop']} 条"
              f"（一致率 {cl['human_agreement']:.0%}），规则命中率 {cl['rule_hit_rate']:.0%}，"
              f"原因匹配率 {cl['category_match_rate']:.0%}；不可复现 {len(cl['unreproducible'])} 条")

    report_path = args.report_out or (EDGE_REPORT_JSON if args.edge else REPORT_JSON)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{report_path}")
    if args.history:
        print(f"评测历史：{eval_store.history_path()}")

    if args.record:
        RECORD_DOC.parent.mkdir(parents=True, exist_ok=True)
        if not RECORD_DOC.exists():
            RECORD_DOC.write_text("# 评测记录\n\n黄金集质量评测的历史记录，每次改动后对比上一版基线。\n", encoding="utf-8")
        if args.edge:
            subset_line = "；".join(
                f"{SUBSET_CN.get(k, k)} {v['accuracy']:.1%}(n={v['n']})"
                for k, v in sorted(report["by_subset"].items())
            )
            cal_line = "；".join(
                f"{k} {v['accuracy']:.1%}(n={v['n']})" if v["accuracy"] is not None else f"{k} n=0"
                for k, v in sorted(report["calibration"].items())
            )
            section = (
                f"\n## {datetime.now():%Y-%m-%d} {mode}（edge_set_v1，主评分 {len(relevant_rows)} 条，"
                f"排除 relevant=no {len(irrelevant_rows)} 条）\n\n"
                f"- 整条情感准确率：**{overall:.1%}**\n"
                f"- 按子集（n<30 仅参考）：{subset_line or '无'}\n"
                f"- 置信度分桶校准：{cal_line}\n"
                f"- 路由：词典直判 {direct_n} 条（{report['routing']['direct_rate']:.0%}）"
                + (f"，LLM 精分析 {llm_n} 条、修正 {llm_fix} 条\n" if args.llm else "\n")
            )
        else:
            flag_line = "；".join(f"{k} {v['accuracy']:.1%}(n={v['n']})" for k, v in sorted(flag_acc.items()))
            domain_line = ""
            if report["by_domain"]:
                domain_line = "（按领域：" + "；".join(
                    f"{k} {v['accuracy']:.1%}(n={v['n']})"
                    for k, v in sorted(report["by_domain"].items())
                ) + "）"
            section = (
                f"\n## {datetime.now():%Y-%m-%d} {mode}（{golden.name}，主评分 {len(relevant_rows)} 条，"
                f"排除 relevant=no {len(irrelevant_rows)} 条）\n\n"
                f"- 整条情感准确率：**{overall:.1%}**{domain_line}\n"
                f"- 语言现象子集：{flag_line or '无'}\n"
                f"- 维度：精确命中 "
                + (f"{dim_rep['exact_match_rate']:.1%}" if dim_rep["exact_match_rate"] is not None else "无维度标注")
                + f"（n={dim_rep['exact_match_n']}），"
                  f"微平均 P/R/F1 = {dim_rep['micro_precision']:.2f}/{dim_rep['micro_recall']:.2f}/{dim_rep['micro_f1']:.2f}\n"
                f"- 路由：词典直判 {direct_n} 条（{report['routing']['direct_rate']:.0%}）"
                + (f"，LLM 精分析 {llm_n} 条、修正 {llm_fix} 条\n" if args.llm else "\n")
                + (f"- 清洗验证集：{cl['n']} 条确认应丢弃（一致率 {cl['human_agreement']:.0%}），"
                   f"规则命中率 {cl['rule_hit_rate']:.0%}\n" if cl.get("status") != "no_golden" else "")
            )
        with open(RECORD_DOC, "a", encoding="utf-8") as f:
            f.write(section)
        print(f"已记录：{RECORD_DOC}")


if __name__ == "__main__":
    main()
