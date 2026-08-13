# -*- coding: utf-8 -*-
"""边界样本专项集定向采集（2.3 步骤 3）。

用法：
    python tests/collect_edge_samples.py --channels websearch     # 零登录，先跑
    python tests/collect_edge_samples.py --channels weibo         # 需 WB_SUB 或应用侧边栏已存 Cookie
    python tests/collect_edge_samples.py --channels xiaohongshu   # 需 Chrome 已登录小红书（opencli）
    python tests/collect_edge_samples.py --channels websearch,weibo,xiaohongshu

输出：data/reports/edge_collection_<时间戳>/<子集>/result.json + <子集>.xlsx
（sample_golden_set.py --edge-only 会读取 data/reports 汇总样本池并报告缺口）

说明：只采集样本池缺失的子集（反讽/方言/emoji）；黑话、长文本、低置信现有池已达标。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.models import AnalysisPlan, ChannelConfig  # noqa: E402
from app.core.pipeline import TaskRunner, bundle_to_json  # noqa: E402
from app.output.excel_writer import build_excel  # noqa: E402

SUBJECT = "边界集"
DEFAULT_LIMIT = 6

# 每子集：关键词 + 渠道组合（渠道 = websearch 子渠道提示词/域名过滤）
EDGE_PLAN = {
    "irony": {
        "keywords": ["呵呵", "太棒了", "真的会谢", "笑死", "绝了"],
        "channels": ["websearch", "websearch_zhihu"],
    },
    "dialect": {
        "keywords": ["唔该", "好正", "咁犀利", "巴适", "安逸", "咋整", "唠嗑", "要得",
                     "咁正", "冇得顶", "好鬼正"],
        "channels": ["websearch", "websearch_tieba"],
    },
    "emoji": {
        "keywords": ["😍😍 太好看了", "😡😡 太坑了", "😂😂 笑死",
                     "😭😭 太好哭", "💕💕 好甜", "🤮🤮 太恶心",
                     "😅😅 太尴尬", "🙏🙏 求求了", "😤😤 无语",
                     "🥰🥰 好喜欢", "💔💔 心碎", "🤯🤯 震惊"],
        "channels": ["websearch", "weibo", "xiaohongshu", "bilibili"],
    },
}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="边界样本专项集定向采集")
    ap.add_argument("--channels", default="websearch",
                    help="逗号分隔渠道：websearch / weibo / xiaohongshu（默认 websearch）")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="每关键词条数上限")
    ap.add_argument("--subsets", default="irony,dialect,emoji",
                    help="逗号分隔子集（默认缺口的三个）")
    ap.add_argument("--comments", action="store_true",
                    help="同时抓评论（B站等渠道补充短文本/emoji 样本）")
    args = ap.parse_args()

    channel_ids = [c.strip() for c in args.channels.split(",") if c.strip()]
    subsets = [s.strip() for s in args.subsets.split(",") if s.strip()]
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_root = ROOT / "data" / "reports" / f"edge_collection_{ts}"
    out_root.mkdir(parents=True, exist_ok=True)

    cookie = os.environ.get("WB_SUB", "")
    date_start = date.today() - timedelta(days=180)
    date_end = date.today()

    for subset in subsets:
        cfg = EDGE_PLAN.get(subset)
        if not cfg:
            print(f"跳过未知子集：{subset}")
            continue
        keywords = cfg["keywords"]
        chans = [c for c in cfg["channels"] if c in channel_ids]
        if not chans:
            print(f"跳过 {subset}：无可用渠道（请求 {cfg['channels']}，可用 {channel_ids}）")
            continue
        plan = AnalysisPlan(
            subject=SUBJECT,
            domain_id="consumer",
            keywords=keywords,
            channels=[
                ChannelConfig(
                    channel_id=cid,
                    params={"limit": args.limit,
                            "cookie": cookie if cid == "weibo" else ""},
                )
                for cid in chans
            ],
            per_keyword_limit=args.limit,
            comments_enabled=args.comments,
            llm_enabled=False,
            narrative_enabled=False,
            date_start=date_start,
            date_end=date_end,
        )
        print(f"\n{'='*24} {subset}（{chans}，每关键词 {args.limit} 条）{'='*24}")
        runner = TaskRunner(plan)
        bundle = runner.run(analyzer=None)
        for ch in bundle.channel_results:
            print(f"  {ch.channel_id}: 保留 {len(ch.posts)} / 丢弃 {len(ch.dropped or [])}"
                  + (f"（{ch.error}）" if not ch.ok else ""))
        out = out_root / subset
        out.mkdir(parents=True, exist_ok=True)
        # 安全：结果 JSON 不落 Cookie（计划参数剥离后再保存）
        for cfg in bundle.plan.channels:
            cfg.params.pop("cookie", None)
        (out / f"{subset}.xlsx").write_bytes(build_excel(bundle).getvalue())
        bundle_to_json(bundle, out / "result.json")
        print(f"  saved: {out}")

    print("\n输出目录:", out_root.resolve())
    print("下一步：python tests/sample_golden_set.py --edge-only 重新抽样看缺口")


if __name__ == "__main__":
    main()
