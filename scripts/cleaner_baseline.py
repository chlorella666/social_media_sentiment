# -*- coding: utf-8 -*-
"""clean_posts 行为基线：fixture -> 保留/丢弃/去重 + 词典编码分布（F-030 阶段 0）。

真实采集数据无人工标签，故对比指标为「保留/丢弃集合 + 词典情感分布 + need_review 数」
（黄金集仅作全局门禁，见 docs/F-030专项执行方案.md 〇之一修正 8）。

用法：
    python scripts/cleaner_baseline.py              # 生成/刷新 data/datasets/cleaner_baseline_v1.json
    python scripts/cleaner_baseline.py --compare    # 与已存基线对比，不一致退出码 1
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.models import AnalysisPlan, Comment, Post  # noqa: E402
from app.coding.cleaner import clean_posts  # noqa: E402
from app.coding.coder import Coder  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "cleaner_baseline_posts.json"
OUT = ROOT / "data" / "datasets" / "cleaner_baseline_v1.json"


def load_posts() -> tuple[list[Post], dict]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    posts: list[Post] = []
    for p in data["posts"]:
        posts.append(
            Post(
                id=p.get("id") or p["url"],
                platform=p.get("platform", ""),
                keyword=p.get("keyword", ""),
                author=p.get("author", ""),
                title=p.get("title", ""),
                content=p.get("content", ""),
                url=p.get("url", ""),
                timestamp=p.get("timestamp", ""),
                likes=int(p.get("likes") or 0),
                comments=[Comment(**c) for c in p.get("comments") or []],
                platform_specific={}
                if p.get("llm_relevant") is None
                else {"llm_relevant": p["llm_relevant"]},
            )
        )
    return posts, data


def lexicon_distribution(posts: list[Post], subject: str) -> dict:
    plan = AnalysisPlan(subject=subject, keywords=[subject])
    items = Coder(None).code_posts(posts, plan)
    return {
        "total": len(items),
        "distribution": dict(Counter(it.sentiment.value for it in items)),
        "need_review": sum(1 for it in items if it.need_review),
        "methods": dict(Counter(it.method for it in items)),
    }


def build_record() -> dict:
    posts, data = load_posts()
    kept, dropped = clean_posts(posts, subject=data["subject"], keywords=[data["subject"]])
    by_reason: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    for d in dropped:
        for r in (d.get("reason") or "").split("；"):
            r = r.strip()
            if r:
                by_reason[r] += 1
        by_kind[d.get("kind", "quality")] += 1
    return {
        "fixture": FIXTURE.name,
        "source_task": data.get("source_task"),
        "subject": data["subject"],
        "input_n": len(posts),
        "kept_n": len(kept),
        "dropped_n": len(dropped),
        "dropped_by_reason": dict(by_reason),
        "dropped_by_kind": dict(by_kind),
        "kept_urls": sorted(p.url for p in kept),
        "dropped_urls": sorted(d.get("url", "") for d in dropped),
        "lexicon": lexicon_distribution(kept, data["subject"]),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true", help="与已存基线对比")
    args = ap.parse_args()
    rec = build_record()
    if args.compare:
        if not OUT.exists():
            print(f"基线不存在：{OUT}，请先运行 python scripts/cleaner_baseline.py")
            return 1
        prev = json.loads(OUT.read_text(encoding="utf-8"))
        diffs = []
        for key in ("input_n", "kept_n", "dropped_n", "dropped_by_reason", "dropped_by_kind"):
            if rec[key] != prev.get(key):
                diffs.append(f"{key}: {prev.get(key)} -> {rec[key]}")
        if rec["kept_urls"] != prev.get("kept_urls"):
            diffs.append("kept_urls 集合变化")
        if rec["dropped_urls"] != prev.get("dropped_urls"):
            diffs.append("dropped_urls 集合变化")
        if rec["lexicon"] != prev.get("lexicon"):
            diffs.append(f"lexicon: {prev.get('lexicon')} -> {rec['lexicon']}")
        if diffs:
            print("与基线不一致：")
            for d in diffs:
                print("  -", d)
            return 1
        print("与基线一致 ✅")
        return 0
    OUT.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"OK: {OUT}\n"
        f"  input={rec['input_n']} kept={rec['kept_n']} dropped={rec['dropped_n']}\n"
        f"  dropped_by_reason={json.dumps(rec['dropped_by_reason'], ensure_ascii=False)}\n"
        f"  lexicon={json.dumps(rec['lexicon'], ensure_ascii=False)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
