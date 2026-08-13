# -*- coding: utf-8 -*-
"""受控对照聚合确认工具（2.2）：同一 WebSearch 配置跑 N 次并跨任务聚合判定。

用途：受控对照的"判定查询串 n≥30"可通过跨任务聚合达成——同一查询串多次跑分
后累计样本，再用 docs/评测记录.md 的分档口径复核（提升/扩容验证）。

用法：
    python tests/run_keyword_confirm.py --times 3 --limit 13 \
        --date-start 2026-07-14 --date-end 2026-08-13
    python tests/run_keyword_confirm.py --brand 恋与深空 --dry-run

说明：默认品牌为 小象超市（当前验收品牌）；每轮任务只跑 WebSearch、不调用 LLM
（词典模式，无费用）；结果落 data/reports/受控对照_聚合确认_<ts>/ 并扫描入
keyword_effects 历史，随后打印聚合判定。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import keyword_effects as ke  # noqa: E402
from app.core.models import AnalysisPlan, ChannelConfig  # noqa: E402
from app.core.pipeline import TaskRunner, bundle_to_json  # noqa: E402


def run_once(
    brand: str,
    limit: int,
    date_start: date,
    date_end: date,
    out_dir: Path,
    run_i: int,
) -> Path:
    plan = AnalysisPlan(
        subject=brand,
        domain_id="consumer",
        keywords=[brand],
        channels=[ChannelConfig(channel_id="websearch", params={"limit": limit})],
        per_keyword_limit=limit,
        comments_enabled=False,
        comments_per_post=0,
        llm_enabled=False,
        narrative_enabled=False,
        relevance_check_enabled=False,
        date_start=date_start,
        date_end=date_end,
    )
    runner = TaskRunner(plan)
    bundle = runner.run(analyzer=None)  # llm 关闭 → MockAnalyzer，无费用
    run_dir = out_dir / f"run{run_i}"
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "result.json"
    bundle_to_json(bundle, result_path)
    return result_path


def report_aggregate(brand: str) -> None:
    hist = ke.load_history()
    agg = ke.aggregate_unique(hist)
    rows = [
        r for r in agg["funnel"]
        if str(r["channel"]).startswith("websearch") and brand in r["query"]
    ]
    if not rows:
        print("  （历史中无该品牌查询串）")
        return
    print(f"\n=== {brand} 聚合判定（跨任务累计，共 {agg['tasks']} 个任务入库）===")
    if agg.get("dedup_missing"):
        print(f"⚠ 去重口径：{len(agg['dedup_missing'])} 个旧任务无 URL 明细已排除"
              f"（运行 python app/core/keyword_effects.py --rescan 补齐）")
    print("（已按 URL 跨任务去重：同一内容只计一次，重复测量不放大样本）")
    print(f"{'查询串':<22}{'采集':>5}{'保留':>5}{'丢弃':>5}{'有效供给率':>9}  样本")
    bare = []
    extras = []
    for r in sorted(rows, key=lambda x: -x["collected"]):
        eff = f"{r['effective_rate']:.1%}" if r["effective_rate"] is not None else "-"
        ref = "参考" if r["collected"] < ke.REF_N else "OK"
        print(f"{r['query'][:20]:<22}{r['collected']:>5}{r['kept']:>5}"
              f"{r['dropped']:>5}{eff:>9}  {ref}")
        if r["query"].strip() == f"{brand} 评价":
            bare.append(r)
        else:
            extras.append(r)
    if bare:
        b = bare[0]
        print(f"\n同查询串「{brand} 评价」：采集 {b['collected']} / 保留 {b['kept']} / "
              f"有效供给率 {b['effective_rate']:.1%} / 丢弃率 "
              f"{b['dropped'] / b['collected']:.1%}"
              f"{'（n≥30，可用）' if b['collected'] >= ke.REF_N else '（n<30 参考）'}")
    if extras:
        c = sum(r["collected"] for r in extras)
        k = sum(r["kept"] for r in extras)
        print(f"新增查询串合计：采集 {c} / 保留 {k} / 有效供给率 {k / c:.1%}"
              f"{'（n≥30，可用）' if c >= ke.REF_N else '（n<30 参考）'}")
        below = [r for r in extras
                 if r["effective_rate"] is not None and r["effective_rate"] < 0.80]
        if below:
            print("  未达 80% 门槛的新增词：",
                  "、".join(f"{r['query']}({r['effective_rate']:.0%})" for r in below))
        low = [r for r in extras if r["collected"] < ke.REF_N]
        if low:
            print("  仍为参考级的单个新增词：",
                  "、".join(f"{r['query']}(n={r['collected']})" for r in low))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="受控对照聚合确认（2.2）")
    ap.add_argument("--brand", default="小象超市", help="品牌名（默认小象超市）")
    ap.add_argument("--times", type=int, default=3, help="跑几轮（默认 3）")
    ap.add_argument("--limit", type=int, default=13, help="每关键词上限（默认 13）")
    ap.add_argument("--date-start", default="", help="开始日期 YYYY-MM-DD")
    ap.add_argument("--date-end", default="", help="结束日期 YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true",
                    help="不发起网络请求，只打印当前聚合现状")
    args = ap.parse_args()

    date_start = (
        date.fromisoformat(args.date_start)
        if args.date_start else date.today().replace(day=1)
    )
    date_end = date.fromisoformat(args.date_end) if args.date_end else date.today()

    if args.dry_run:
        print(f"dry-run：品牌={args.brand} times={args.times} limit={args.limit} "
              f"时间段={date_start}~{date_end}")
        report_aggregate(args.brand)
        return 0

    # 流程约束（2026-08-13）：聚合确认是重活，限制频率防再触发风控
    from datetime import datetime, timedelta

    now = datetime.now()
    runs: list[datetime] = []
    for p in (ROOT / "data" / "reports").glob("受控对照_聚合确认_*"):
        if not p.is_dir():
            continue
        try:
            ts = p.name.rsplit("_", 1)[1]
            runs.append(datetime.strptime(ts, "%Y%m%d_%H%M%S"))
        except (IndexError, ValueError):
            continue
    today_runs = [t for t in runs if t.date() == now.date()]
    if len(today_runs) >= 2:
        print("频率约束：聚合确认每天最多 2 轮（今天已完成 2 轮），请明日再跑。")
        return 0
    if today_runs and (now - max(today_runs)) < timedelta(minutes=10):
        print("频率约束：与上一轮间隔不足 10 分钟，请稍后再跑。")
        return 0

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_root = ROOT / "data" / "reports" / f"受控对照_聚合确认_{ts}"
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"开始跑 {args.times} 轮 {args.brand}（WebSearch 仅，limit={args.limit}，"
          f"{date_start}~{date_end}）…")
    for i in range(1, args.times + 1):
        p = run_once(args.brand, args.limit, date_start, date_end, out_root, i)
        payload = ke.scan_report(p)
        n_ws = len([x for x in (payload or {}).get("funnel") or []
                    if str(x.get("channel", "")).startswith("websearch")])
        print(f"  run{i}: 完成，查询串 {n_ws} 个 → {p}")
        time.sleep(1.0)
    report_aggregate(args.brand)
    print("输出目录:", out_root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
