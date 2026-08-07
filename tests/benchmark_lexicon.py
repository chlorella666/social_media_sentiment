# -*- coding: utf-8 -*-
"""词典基准评测：V1（41+41 手工词） vs V2（精选网络词 + ECSD 电商词）。

数据：data/datasets/online_shopping_10_cats.csv（10 类商品评论，6 万条，
label 1=正面 0=负面）。每类抽样最多 SAMPLE_PER_CLASS 条正 + 负。
除直接准确率外，模拟产品混合流水线：置信度 < 0.8 的文本交给 LLM
（按 LLM_ACC 假设准确率），衡量"词典直判 + LLM 补位"的整体效果。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd

from app.coding import lexicon as v1
from app.coding import lexicon_v2 as v2
from app.coding.llm_analyzer import CONFIDENCE_THRESHOLD

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_CSV = PROJECT_ROOT / "data" / "datasets" / "online_shopping_10_cats.csv"
SAMPLE_PER_CLASS = 100
LLM_ACC = 0.9  # 路由给 LLM 的文本假设准确率（DeepSeek 实测可调）


def load_sample() -> pd.DataFrame:
    df = pd.read_csv(DATA_CSV, encoding="utf-8-sig")
    df.columns = [c.strip().lower() for c in df.columns]
    parts = []
    for cat, group in df.groupby("cat"):
        for label in (1, 0):
            sub = group[group["label"] == label]
            parts.append(sub.sample(min(len(sub), SAMPLE_PER_CLASS), random_state=42))
    return pd.concat(parts, ignore_index=True)


def evaluate(scorer, df: pd.DataFrame) -> dict:
    t0 = time.time()
    correct = correct_binary = preds_binary = neutral = total = 0
    routed = routed_correct = 0
    per_cat: dict[str, dict] = {}
    for _, row in df.iterrows():
        total += 1
        true_label = 1 if row["label"] == 1 else 0
        cat = row["cat"]
        res = scorer.score_text(str(row["review"]))
        pred = (
            1
            if res["sentiment"] == "positive"
            else 0
            if res["sentiment"] == "negative"
            else None
        )
        c = per_cat.setdefault(cat, {"correct": 0, "total": 0, "neutral": 0})
        c["total"] += 1
        if pred is None:
            neutral += 1
            c["neutral"] += 1
        else:
            preds_binary += 1
            if pred == true_label:
                correct_binary += 1
                c["correct"] += 1
        if res["confidence"] < CONFIDENCE_THRESHOLD:
            routed += 1
            routed_correct += LLM_ACC
        elif pred == true_label:
            correct += 1
    return {
        "耗时_s": round(time.time() - t0, 1),
        "样本": total,
        "中性判定": neutral,
        "判定覆盖率": f"{preds_binary / total:.1%}",
        "二分类准确率": f"{correct_binary / preds_binary:.1%}" if preds_binary else "-",
        "硬准确率(中性算错)": f"{correct / total:.1%}",
        "路由LLM占比": f"{routed / total:.1%}",
        "混合流水线准确率": f"{(correct + routed_correct) / total:.1%}",
        "per_cat": per_cat,
    }


def main() -> None:
    df = load_sample()
    print(f"样本量: {len(df)}（10 类 × 正负各 {SAMPLE_PER_CLASS}）\n")
    print("词典加载中...")
    t0 = time.time()
    v2._word_scores()
    print(f"V2 词典加载耗时: {time.time() - t0:.1f}s，词数: {len(v2._word_scores())}\n")

    r1 = evaluate(v1, df)
    r2 = evaluate(v2, df)

    rows = [
        ("指标", "V1 小词典", "V2 大规模"),
        ("耗时(秒)", str(r1["耗时_s"]), str(r2["耗时_s"])),
        ("中性判定条数", str(r1["中性判定"]), str(r2["中性判定"])),
        ("判定覆盖率", r1["判定覆盖率"], r2["判定覆盖率"]),
        ("二分类准确率(已判定)", r1["二分类准确率"], r2["二分类准确率"]),
        ("词典直判准确率", r1["硬准确率(中性算错)"], r2["硬准确率(中性算错)"]),
        ("路由LLM占比(<0.8)", r1["路由LLM占比"], r2["路由LLM占比"]),
        ("混合流水线准确率", r1["混合流水线准确率"], r2["混合流水线准确率"]),
    ]
    for row in rows:
        print(f"{row[0]:<18} {row[1]:>12} {row[2]:>14}")

    print("\n=== 分品类硬准确率（中性算错） ===")
    print(f"{'品类':<8} {'V1':>10} {'V2':>10}")
    for cat in sorted(r1["per_cat"]):
        c1 = r1["per_cat"][cat]
        c2 = r2["per_cat"][cat]
        a1 = f"{c1['correct'] / c1['total']:.1%}"
        a2 = f"{c2['correct'] / c2['total']:.1%}"
        print(f"{cat:<8} {a1:>10} {a2:>10}")


if __name__ == "__main__":
    main()
