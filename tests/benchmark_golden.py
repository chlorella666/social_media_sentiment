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
DEFAULT_GOLDEN = ROOT / "data" / "datasets" / "golden_set_v1.csv"
REPORT_JSON = ROOT / "data" / "datasets" / "benchmark_report.json"
RECORD_DOC = ROOT / "docs" / "评测记录.md"
CLEANING_GOLDEN = ROOT / "data" / "datasets" / "golden_set_v1_cleaning.csv"
FIXTURE_GOLDEN = ROOT / "tests" / "fixtures" / "golden_set_v1.csv"
# 优先使用入库版（tests/fixtures，跨机器可复现）；缺失时回退 data/ 工作版
DEFAULT_GOLDEN = FIXTURE_GOLDEN if FIXTURE_GOLDEN.exists() else DEFAULT_GOLDEN

VALID_SENTIMENTS = {"positive", "negative", "neutral"}


def load_golden(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            r["dimension_sentiments"] = json.loads(r["dimension_sentiments"] or "{}")
            rows.append(r)
    return rows


def load_schema(domain: str) -> dict[str, dict]:
    path = ROOT / "app" / "domains" / f"{domain}.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    return {
        dim["id"]: {"name": dim["name"], "keywords": dim.get("keywords", [])}
        for dim in schema.get("dimensions", [])
    }


def get_llm_analyzer():
    from app.coding.llm_analyzer import LLMConfig, OpenAICompatibleAnalyzer

    import os

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        key_file = ROOT / "llm_apikey.txt"
        if key_file.exists():
            lines = [ln.strip() for ln in key_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            api_key = next((ln for ln in lines if ln.startswith("sk-")), lines[-1] if lines else "")
    if not api_key:
        raise SystemExit("--llm 需要 API Key：设置 OPENAI_API_KEY 或填写 llm_apikey.txt")
    config = LLMConfig(
        api_key=api_key,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
        model=os.environ.get("OPENAI_MODEL", "deepseek-chat"),
    )
    return OpenAICompatibleAnalyzer(config)


def predict_all(rows: list[dict], use_llm: bool, llm=None) -> list[dict]:
    from app.coding import lexicon_v2 as lexicon
    from app.coding.cleaner import clean_text
    from app.coding.llm_analyzer import CONFIDENCE_THRESHOLD

    schemas = {d: load_schema(d) for d in {r["domain"] for r in rows}}
    out = []
    llm_texts, llm_idx = [], []
    for i, r in enumerate(rows):
        text = clean_text(r["text"] or "")
        pre = lexicon.score_text(text)
        direct = pre["confidence"] >= CONFIDENCE_THRESHOLD
        pred = {
            "sentiment": pre["sentiment"],
            "lexicon_sentiment": pre["sentiment"],
            "direct": direct,
            "llm_used": False,
            "dims": [],
        }
        schema = schemas.get(r["domain"], {})
        pred["dims"] = [
            d["name"] for d in schema.values()
            if any(kw.lower() in (r["text"] or "").lower() for kw in d["keywords"])
        ]
        out.append(pred)
        if use_llm and not direct:
            llm_texts.append(text)
            llm_idx.append(i)
    if llm_texts:
        results = llm.analyze_batch(llm_texts)
        for i, res in zip(llm_idx, results):
            if res and res.get("sentiment") in VALID_SENTIMENTS:
                out[i]["sentiment"] = res["sentiment"]
                out[i]["llm_used"] = True
    return out


def accuracy(pred_sents, golden_sents):
    n = len(pred_sents)
    return (sum(a == b for a, b in zip(pred_sents, golden_sents)) / n) if n else 0.0


def group_report(rows, preds, key_fn):
    groups = defaultdict(list)
    for r, p in zip(rows, preds):
        groups[key_fn(r)].append((p["sentiment"], r["sentiment"]))
    return {
        k: {"n": len(v), "accuracy": round(accuracy(*zip(*v)), 4)}
        for k, v in sorted(groups.items())
    }


def dimension_report(rows, preds):
    """维度级：精确命中率 + 每维 precision/recall（微平均）。"""
    exact_t, exact_n = 0, 0
    tp, fp, fn = 0, 0, 0
    all_dims = sorted({d for r in rows for d in r["dimension_sentiments"]} | {d for p in preds for d in p["dims"]})
    per_dim = {}
    for dim in all_dims:
        pt = pf = pn = 0
        for r, p in zip(rows, preds):
            gold = dim in r["dimension_sentiments"]
            pre = dim in p["dims"]
            if pre and gold:
                pt += 1
            elif pre and not gold:
                pf += 1
            elif not pre and gold:
                pn += 1
        precision = pt / (pt + pf) if pt + pf else 0.0
        recall = pt / (pt + pn) if pt + pn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_dim[dim] = {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}
        tp += pt
        fp += pf
        fn += pn
    micro_p = tp / (tp + fp) if tp + fp else 0.0
    micro_r = tp / (tp + fn) if tp + fn else 0.0
    micro_f1 = 2 * micro_p * micro_r / (micro_p + micro_r) if micro_p + micro_r else 0.0
    for r, p in zip(rows, preds):
        if r["dimension_sentiments"]:
            exact_n += 1
            if set(p["dims"]) == set(r["dimension_sentiments"]):
                exact_t += 1
    return {
        "exact_match_rate": round(exact_t / exact_n, 4) if exact_n else None,
        "exact_match_n": exact_n,
        "micro_precision": round(micro_p, 4),
        "micro_recall": round(micro_r, 4),
        "micro_f1": round(micro_f1, 4),
        "per_dimension": per_dim,
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


def main():
    ap = argparse.ArgumentParser(description="黄金集基准评测")
    ap.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    ap.add_argument("--llm", action="store_true", help="混合流水线（词典+LLM），需 API Key")
    ap.add_argument("--record", action="store_true", default=True, help="追加结果到 docs/评测记录.md")
    args = ap.parse_args()

    if not args.golden.exists():
        raise SystemExit(f"黄金集不存在：{args.golden}（先运行 tests/finalize_golden_set.py）")
    rows = load_golden(args.golden)
    relevant_rows = [r for r in rows if r.get("relevant", "yes") == "yes"]
    irrelevant_rows = [r for r in rows if r.get("relevant", "yes") != "yes"]
    if not relevant_rows:
        raise SystemExit("黄金集没有 relevant=yes 样本，无法计算主评分")

    llm = get_llm_analyzer() if args.llm else None
    preds = predict_all(relevant_rows, args.llm, llm)
    mode = "混合流水线（词典+LLM）" if args.llm else "词典直判（无 LLM）"

    overall = accuracy([p["sentiment"] for p in preds], [r["sentiment"] for r in relevant_rows])
    confusion = Counter((p["sentiment"], r["sentiment"]) for p, r in zip(preds, relevant_rows))
    dim_rep = dimension_report(relevant_rows, preds)
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
        "golden": str(args.golden),
        "golden_rows": len(rows),
        "scope": {
            "main_n": len(relevant_rows),
            "irrelevant_excluded_n": len(irrelevant_rows),
            "口径": "主评分仅统计 relevant=yes；relevant=no 单独统计，不计入情感准确率",
        },
        "irrelevant": {
            "n": len(irrelevant_rows),
            "golden_sentiments": dict(Counter(r["sentiment"] for r in irrelevant_rows)),
            "note": "规范 §五 规则 8：不相关内容情感按 neutral 标注，单独统计",
        },
        "mode": mode,
        "sentiment_accuracy": round(overall, 4),
        "confusion_matrix": {f"{a}->{b}": c for (a, b), c in sorted(confusion.items())},
        "by_domain": group_report(relevant_rows, preds, lambda r: r["domain"]),
        "by_channel": group_report(relevant_rows, preds, lambda r: r["platform"]),
        "by_kind": group_report(relevant_rows, preds, lambda r: r["kind"]),
        "by_flag": flag_acc,
        "dimension": dim_rep,
        "routing": {
            "direct_lexicon": direct_n,
            "llm_used": llm_n,
            "llm_corrected": llm_fix,
            "direct_rate": round(direct_n / len(relevant_rows), 4) if relevant_rows else 0,
        },
        "cleaning": cleaning_report(),
    }
    if args.llm and llm is not None:
        report["llm_usage"] = llm.usage

    print(f"黄金集：{len(rows)} 条（主评分 {len(relevant_rows)}，排除 relevant=no {len(irrelevant_rows)}） | 模式：{mode}")
    print(f"整条情感准确率：{overall:.1%}")
    print("  按领域：", {k: f"{v['accuracy']:.1%}({v['n']})" for k, v in report["by_domain"].items()})
    print("  按渠道：", {k: f"{v['accuracy']:.1%}({v['n']})" for k, v in report["by_channel"].items()})
    print(f"维度：精确命中 {dim_rep['exact_match_rate']:.1%}（n={dim_rep['exact_match_n']}），"
          f"微平均 P/R/F1 = {dim_rep['micro_precision']:.2f}/{dim_rep['micro_recall']:.2f}/{dim_rep['micro_f1']:.2f}")
    print("  每维 P/R/F1：", {k: f"{v['precision']:.2f}/{v['recall']:.2f}/{v['f1']:.2f}" for k, v in dim_rep["per_dimension"].items()})
    print("路由：", report["routing"])
    print("语言现象子集准确率：", {k: f"{v['accuracy']:.1%}(n={v['n']})" for k, v in flag_acc.items()})
    cl = report["cleaning"]
    if cl.get("status") == "no_golden":
        print("清洗验证集：", cl["note"])
    else:
        print(f"清洗验证集：{cl['n']} 条，确认应丢弃 {cl['confirmed_drop']} 条"
              f"（一致率 {cl['human_agreement']:.0%}），规则命中率 {cl['rule_hit_rate']:.0%}，"
              f"原因匹配率 {cl['category_match_rate']:.0%}；不可复现 {len(cl['unreproducible'])} 条")

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{REPORT_JSON}")

    if args.record:
        RECORD_DOC.parent.mkdir(parents=True, exist_ok=True)
        if not RECORD_DOC.exists():
            RECORD_DOC.write_text("# 评测记录\n\n黄金集质量评测的历史记录，每次改动后对比上一版基线。\n", encoding="utf-8")
        flag_line = "；".join(f"{k} {v['accuracy']:.1%}(n={v['n']})" for k, v in sorted(flag_acc.items()))
        section = (
            f"\n## {datetime.now():%Y-%m-%d} {mode}（golden_set_v1，主评分 {len(relevant_rows)} 条，"
            f"排除 relevant=no {len(irrelevant_rows)} 条）\n\n"
            f"- 整条情感准确率：**{overall:.1%}**（游戏 {report['by_domain'].get('game', {}).get('accuracy', 0):.1%} / "
            f"消费品 {report['by_domain'].get('consumer', {}).get('accuracy', 0):.1%}）\n"
            f"- 语言现象子集：{flag_line or '无'}\n"
            f"- 维度：精确命中 {dim_rep['exact_match_rate']:.1%}（n={dim_rep['exact_match_n']}），"
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
