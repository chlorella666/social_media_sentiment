# -*- coding: utf-8 -*-
"""F-020（2026-08-26）：演示数据脱敏 → app/demo_source.json（随源码打包，进公开仓库）。"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = ROOT / "data" / "state" / "demo_report" / "demo_source.json"
DST = ROOT / "app" / "demo_source.json"

from app.coding.cleaner import desensitize_text


def _mask_url(url: str, url_map: dict, counter: list[int]) -> str:
    if url in url_map:
        return url_map[url]
    m = re.match(r"(https?://[^/]+)(/.*)?$", url or "")
    host = m.group(1) if m else (url or "")
    counter[0] += 1
    masked = f"{host}/[链接{counter[0]}]"
    url_map[url] = masked
    return masked


def _mask_text_id(tid: str, url_map: dict, url_counter: list[int]) -> str:
    m = re.match(r"^(.*?)(:post|:comment(?::?\d+)?)$", tid or "")
    if not m:
        return tid or ""
    return _mask_url(m.group(1), url_map, url_counter) + m.group(2)


def _mask_comment_id(cid: str, url_map: dict, url_counter: list[int]) -> str:
    m = re.match(r"^(.*?)(::\d+)$", cid or "")
    if not m:
        return _mask_url(cid or "", url_map, url_counter)
    return _mask_url(m.group(1), url_map, url_counter) + m.group(2)


def _anon_author(author: str, author_map: dict, counter: list[int]) -> str:
    a = (author or "").strip()
    if not a:
        return ""
    if a not in author_map:
        counter[0] += 1
        author_map[a] = f"用户{counter[0]}"
    return author_map[a]


def _mask_ts(ts: str) -> str:
    return str(ts or "")[:10]


def _mask_specific(spec: dict) -> dict:
    if not spec:
        return spec
    return {k: desensitize_text(str(v)) for k, v in spec.items()}


def main() -> None:
    data = json.load(io.open(SRC, "r", encoding="utf-8"))
    url_map: dict[str, str] = {}
    url_counter = [0]
    author_map: dict[str, str] = {}
    author_counter = [0]

    for ch in data.get("channel_results") or []:
        for p in ch.get("posts") or []:
            p["author"] = _anon_author(p.get("author"), author_map, author_counter)
            if p.get("url"):
                p["url"] = _mask_url(p["url"], url_map, url_counter)
            p["title"] = desensitize_text(p.get("title") or "")
            p["content"] = desensitize_text(p.get("content") or "")
            p["timestamp"] = _mask_ts(p.get("timestamp"))
            p["platform_specific"] = _mask_specific(p.get("platform_specific") or {})
            for c in p.get("comments") or []:
                c["author"] = _anon_author(c.get("author"), author_map, author_counter)
                if c.get("id"):
                    c["id"] = _mask_comment_id(c["id"], url_map, url_counter)
                c["text"] = desensitize_text(c.get("text") or "")
                c["time"] = _mask_ts(c.get("time"))

    for it in data.get("coded_items") or []:
        if it.get("text_id"):
            it["text_id"] = _mask_text_id(it["text_id"], url_map, url_counter)
        it["text"] = desensitize_text(it.get("text") or "")

    # F-018 口径：LLM 模式无 structured_summary（演示报告为 LLM 模式）
    data["structured_summary"] = {}
    data["structured_summary_source"] = "rule"
    n_posts = sum(len(ch.get("posts") or []) for ch in data.get("channel_results") or [])
    n_items = len(data.get("coded_items") or [])
    data.setdefault("warnings", [])
    data["_demo_meta"] = {
        "subject": data.get("plan", {}).get("subject", "恋与深空"),
        "posts": n_posts,
        "coded_items": n_items,
        "source": "恋与深空玩家讨论（脱敏样本）",
    }

    DST.parent.mkdir(parents=True, exist_ok=True)
    with io.open(DST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"OK: {DST}（posts={n_posts}, items={n_items}, url_map={len(url_map)}, authors={len(author_map)}）")


if __name__ == "__main__":
    main()