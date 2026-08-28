# -*- coding: utf-8 -*-
"""重建 clean_posts 基线 fixture（F-030 阶段 0，D8 定稿）。

数据来源：data/reports/<task>/collection_snapshot.json（采集快照）。
快照 posts 为清洗保留集；drops 为丢弃账本——把「保留帖 + 清洗层丢弃帖（quality/
duplicate，排除 collection 采集层跳过）」合并还原为清洗层真实入参（D7/D8 口径）。
按仓库脱敏纪律处理：作者匿名、URL/ID 稳定打码、正文走 desensitize_text。

用法：
    python scripts/build_cleaner_baseline_fixture.py
    python scripts/build_cleaner_baseline_fixture.py --source data/reports/<task>/collection_snapshot.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.coding.cleaner import desensitize_text  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "cleaner_baseline_posts.json"
DEFAULT_SOURCE = ROOT / "data" / "reports" / "20260821_002746" / "collection_snapshot.json"


def _mask_url(seq: int) -> str:
    return f"https://x.example/post/{seq}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    args = ap.parse_args()
    src = Path(args.source)
    raw = json.loads(src.read_text(encoding="utf-8"))
    posts_raw = raw.get("posts") or []
    drops_raw = [
        d for d in (raw.get("drops") or []) if d.get("kind") in ("quality", "duplicate")
    ]
    subject = raw.get("subject") or "恋与深空"
    task_id = raw.get("task_id") or src.parent.name

    author_map: dict[str, str] = {}
    next_author = 1

    def anon_author(name: str) -> str:
        nonlocal next_author
        if not name:
            return ""
        if name not in author_map:
            author_map[name] = f"用户{next_author}"
            next_author += 1
        return author_map[name]

    posts_out: list[dict] = []
    for i, p in enumerate(posts_raw, start=1):
        url = _mask_url(i)
        comments = []
        for j, c in enumerate(p.get("comments") or []):
            comments.append(
                {
                    "id": f"{url}::c{j}",
                    "author": anon_author(c.get("author", "")),
                    "text": desensitize_text(c.get("text", "")),
                    "likes": int(c.get("likes") or 0),
                    "time": c.get("time", ""),
                    "is_reply": bool(c.get("is_reply")),
                    "reply_to": c.get("reply_to", ""),
                    "depth": int(c.get("depth") or 0),
                }
            )
        posts_out.append(
            {
                "id": f"post{i}",
                "platform": p.get("platform", ""),
                "keyword": p.get("keyword", ""),
                "author": anon_author(p.get("author", "")),
                "title": desensitize_text(p.get("title", "")),
                "content": desensitize_text(p.get("content", "")),
                "url": url,
                "timestamp": p.get("timestamp", ""),
                "likes": int(p.get("likes") or 0),
                "comments": comments,
                "llm_relevant": bool(p.get("llm_relevant")),
            }
        )
    for k, d in enumerate(drops_raw, start=len(posts_raw) + 1):
        url = _mask_url(k)
        posts_out.append(
            {
                "id": f"drop{k}",
                "platform": d.get("platform", ""),
                "keyword": d.get("keyword", ""),
                "author": anon_author(d.get("author", "")),
                "title": desensitize_text(d.get("title", "")),
                "content": desensitize_text(d.get("content", "")),
                "url": url,
                "timestamp": "",
                "likes": 0,
                "comments": [],
                "llm_relevant": None,
                "drop_kind": d.get("kind", ""),
                "drop_reason": d.get("reason", ""),
            }
        )

    payload = {
        "source_task": task_id,
        "source_path": str(src),
        "subject": subject,
        "desensitized": True,
        "reconstruction": {
            "kept_posts": len(posts_raw),
            "cleaning_drops": len(drops_raw),
            "excluded_collection_drops": len(raw.get("drops") or []) - len(drops_raw),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "posts": posts_out,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"OK: {OUT}  input_posts={len(posts_out)}  comments="
        f"{sum(len(p['comments']) for p in posts_out)}  subject={subject}  "
        f"source_task={task_id}  (kept={len(posts_raw)}, cleaning_drops={len(drops_raw)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
