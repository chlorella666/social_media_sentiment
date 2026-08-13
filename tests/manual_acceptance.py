# -*- coding: utf-8 -*-
"""阶段一验收脚本（手动一键复跑，真实采集 + LLM + 清洗）。

用法：
  $env:WB_SUB="<微博Cookie>"; python tests/manual_acceptance.py
  python tests/manual_acceptance.py --days 90 --limit 3 --xhs-limit 2
  python tests/manual_acceptance.py --brands 恋与深空,大疆 --skip-llm

前置：API Key（应用侧边栏保存到 DPAPI，或 env OPENAI_API_KEY）、
      微博 Cookie（env WB_SUB）、Chrome 已登录小红书。
输出：data/reports/验收试跑manual_<时间戳>/<品牌>/（Excel 5 sheet + result.json）
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.channels.health import check_channel_health
from app.core.models import AnalysisPlan, ChannelConfig
from app.core.pipeline import TaskRunner, bundle_to_json
from app.coding.llm_analyzer import create_analyzer
from app.output.excel_writer import build_excel

DEFAULT_BRANDS = [
    ("恋与深空", "game", "papegames.cn,infoldgames.com"),
    ("华润万家", "consumer", "crv.com.cn"),
    ("大疆", "consumer", "dji.com"),
]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="阶段一真实渠道验收")
    parser.add_argument("--brands", default="", help="逗号分隔的品牌名（默认三品牌）")
    parser.add_argument("--days", type=int, default=90, help="时间范围（天）")
    parser.add_argument("--limit", type=int, default=3, help="每关键词条数上限")
    parser.add_argument("--xhs-limit", type=int, default=2, help="小红书条数上限")
    parser.add_argument("--skip-llm", action="store_true", help="跳过 LLM（词典模式）")
    parser.add_argument("--relevance", action="store_true", help="启用 LLM 相关性复核")
    parser.add_argument(
        "--keyword-check", action="store_true",
        help="跑完后用关键词效果数据层（2.2）输出每查询串采集漏斗与有效供给率",
    )
    args = parser.parse_args()

    if args.brands:
        names = [b.strip() for b in args.brands.split(",") if b.strip()]
        domain_map = {b: d for b, _, d in DEFAULT_BRANDS}
        official_map = {b: o for b, _, o in DEFAULT_BRANDS}
        brands = [
            (name, domain_map.get(name, "consumer"), official_map.get(name, ""))
            for name in names
        ]
    else:
        brands = DEFAULT_BRANDS

    api_key = ""
    if not args.skip_llm:
        from app.core.secrets import ensure_legacy_key_migrated, load_api_key

        # 旧明文 Key 一次性迁移（幂等；导入并验证后删除明文文件）
        ensure_legacy_key_migrated()
        # 开发脚本通道：DPAPI → OPENAI_API_KEY（1.5 决策，env 仅限开发用途）
        api_key = load_api_key(allow_env=True)
        if not api_key:
            print("⚠ 未找到 API Key（应用侧边栏保存或设置 OPENAI_API_KEY），将使用词典模式")
            args.skip_llm = True

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_root = ROOT / "data" / "reports" / f"验收试跑manual_{ts}"
    out_root.mkdir(parents=True, exist_ok=True)
    date_start = date.today() - timedelta(days=args.days)
    date_end = date.today()

    print(f"=== 渠道体检 ===")
    cookie = os.environ.get("WB_SUB", "")
    for ch_id in ("bilibili", "websearch", "websearch_zhihu", "websearch_tieba",
                  "websearch_taptap", "weibo", "xiaohongshu"):
        ok, msg = check_channel_health(ch_id, {"cookie": cookie})
        print(f"  {'✅' if ok else '⚠️'} {ch_id}: {msg}")

    for brand, domain, official in brands:
        print(f"\n{'='*20} {brand}（{domain}，近{args.days}天）{'='*20}")
        plan = AnalysisPlan(
            subject=brand,
            domain_id=domain,
            keywords=[brand],
            channels=[
                ChannelConfig(channel_id="bilibili", params={"limit": args.limit}),
                *[
                    ChannelConfig(
                        channel_id=cid,
                        params={"limit": args.limit, "official_domains": official},
                    )
                    for cid in (
                        "websearch",
                        "websearch_zhihu",
                        "websearch_tieba",
                        "websearch_taptap",
                    )
                ],
                ChannelConfig(
                    channel_id="weibo",
                    params={"cookie": cookie, "limit": args.limit},
                ),
                ChannelConfig(
                    channel_id="xiaohongshu", params={"limit": args.xhs_limit}
                ),
            ],
            per_keyword_limit=args.limit,
            comments_enabled=True,
            comments_per_post=3,
            llm_enabled=not args.skip_llm,
            narrative_enabled=not args.skip_llm,
            relevance_check_enabled=args.relevance,
            date_start=date_start,
            date_end=date_end,
        )
        analyzer = (
            create_analyzer(
                api_key=api_key,
                base_url="https://api.deepseek.com",
                model="deepseek-chat",
            )
            if api_key
            else None
        )
        runner = TaskRunner(plan)
        t0 = time.time()
        bundle = runner.run(analyzer=analyzer)

        stats: dict[str, dict] = defaultdict(
            lambda: {"posts": 0, "comments": 0, "dropped": 0}
        )
        for ch in bundle.channel_results:
            stats[ch.channel_id]["posts"] += len(ch.posts)
            stats[ch.channel_id]["comments"] += sum(len(p.comments) for p in ch.posts)
            stats[ch.channel_id]["dropped"] += len(ch.dropped)
        for cid, s in sorted(stats.items()):
            coll = s["posts"] + s["dropped"]
            rate = f"{s['dropped']/coll:.0%}" if coll else "-"
            print(f"  {cid}: 保留 {s['posts']} / 评论 {s['comments']} / 丢弃 {s['dropped']}（{rate}）")
        print(
            "  LLM:", bundle.llm_usage,
            "| 修正词典判定:", bundle.summary.get("llm_corrected", 0),
        )
        reasons = Counter(
            str(d.get("reason")) for ch in bundle.channel_results for d in ch.dropped
        )
        if reasons:
            print("  丢弃原因:", "、".join(f"{k}:{v}" for k, v in reasons.most_common(5)))

        out = out_root / brand
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{brand}.xlsx").write_bytes(build_excel(bundle).getvalue())
        bundle_to_json(bundle, out / "result.json")
        if args.keyword_check:
            from app.core import keyword_effects as ke

            payload = ke.scan_report(out / "result.json")
            if payload:
                ws = [r for r in payload["funnel"]
                      if str(r["channel"]).startswith("websearch")]
                for r in payload["funnel"]:
                    eff = f"{r['effective_rate']:.0%}" if r["effective_rate"] is not None else "-"
                    print(f"    kw[{r['channel']}] {r['query']}: "
                          f"采集{r['collected']} 保留{r['kept']} 丢弃{r['dropped']} "
                          f"有效供给率{eff}")
                if ws:
                    total_coll = sum(r["collected"] for r in ws)
                    total_kept = sum(r["kept"] for r in ws)
                    rate = total_kept / total_coll if total_coll else None
                    print(f"  websearch 关键词效果（2.2 验收口径）："
                          f"有效供给率 {rate:.0%}（{total_kept}/{total_coll}）"
                          if rate is not None else
                          "  websearch 关键词效果：无可统计样本")
        print(f"  saved: {out} | 耗时 {time.time()-t0:.0f}s")

    print("\n输出目录:", out_root.resolve())


if __name__ == "__main__":
    main()
