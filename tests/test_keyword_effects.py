# -*- coding: utf-8 -*-
"""关键词效果数据层与 WebSearch 查询策略测试（2.2）。

运行：python tests/test_keyword_effects.py
覆盖：漏斗解析、扫描去重、后缀/共现/别称候选反推、聚合、
      策略配置读写与上限、build_query 默认行为不回归（无网络）。
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sms_kw_effects_"))
os.environ["SMS_KEYWORD_EFFECTS_DIR"] = str(_TMP / "effects")

from app.channels.websearch import build_query  # noqa: E402
from app.core import keyword_effects as ke  # noqa: E402


def make_report(
    subject: str = "大疆",
    query: str = "大疆 评价",
    keyword: str = "大疆",
    n_kept: int = 3,
    n_dropped: int = 2,
) -> dict:
    posts = [
        {
            "id": f"u{i}", "platform": "websearch", "keyword": keyword,
            "title": f"大疆 {i} 炸机", "content": f"大疆 {i} 续航 不错",
            "url": f"https://x.example/{i}",
            "platform_specific": {"query": query, "source": "websearch"},
        }
        for i in range(n_kept)
    ]
    drops = [
        {"platform": "websearch", "url": f"https://d.example/{i}",
         "title": "大疆官网", "keyword": keyword, "reason": "官网域名黑名单"}
        for i in range(n_dropped)
    ]
    coded = [
        {"platform": "websearch", "keyword": keyword, "sentiment": "negative"}
        for _ in range(2)
    ] + [
        {"platform": "websearch", "keyword": keyword, "sentiment": "positive"}
        for _ in range(n_kept - 2)
    ]
    return {
        "plan": {"subject": subject, "keywords": [subject]},
        "channel_results": [{
            "channel_id": "websearch", "ok": True,
            "posts": posts, "dropped": drops,
        }],
        "coded_items": coded,
        "llm_usage": {"estimated_cost": 0.5},
        "created_at": "2026-08-10T10:00:00",
    }


def test_extract_funnel() -> None:
    payload = ke.extract_funnel(make_report())
    assert payload["subject"] == "大疆"
    assert len(payload["funnel"]) == 1
    r = payload["funnel"][0]
    assert (r["channel"], r["query"]) == ("websearch", "大疆 评价")
    assert r["collected"] == 5 and r["kept"] == 3 and r["dropped"] == 2
    assert r["coded"] == 3 and r["negative"] == 2
    assert r["effective_rate"] == 0.6
    assert r["drop_reasons"] == {"官网域名黑名单": 2}
    assert "urls" in payload
    urls_key = "websearch\x1f大疆 评价"
    assert urls_key in payload["urls"]
    assert len(payload["urls"][urls_key]["kept"]) == 3
    print("✓ 漏斗解析（采集/丢弃/保留/编码/有效供给率） 通过")


def test_scan_dedupe() -> None:
    hf = _TMP / "effects" / "dedupe.jsonl"
    src = _TMP / "report_a.json"
    src.write_text(json.dumps(make_report(), ensure_ascii=False), encoding="utf-8")
    assert ke.scan_report(src, hf) is not None
    assert ke.scan_report(src, hf) is None
    assert len(ke.load_history(hf)) == 1
    print("✓ 扫描按内容哈希去重 通过")


def test_suffix_and_candidate_hints() -> None:
    report = make_report(query="大疆 怎么样", n_kept=4, n_dropped=0)
    report["channel_results"][0]["posts"].append({
        "id": "a", "platform": "websearch", "keyword": "大疆",
        "title": "DJI 客服 太差", "content": "DJI 官方 无回应",
        "url": "https://x.example/a",
        "platform_specific": {"query": "大疆 怎么样", "source": "websearch"},
    })
    report["channel_results"][0]["posts"].append({
        "id": "b", "platform": "websearch", "keyword": "大疆",
        "title": "DJI 售后 太差", "content": "大疆 炸机 三次",
        "url": "https://x.example/b",
        "platform_specific": {"query": "大疆 怎么样", "source": "websearch"},
    })
    payload = ke.extract_funnel(report)
    hints = payload["hints"]
    assert "怎么样" in hints["suffix_stats"]
    assert hints["suffix_stats"]["怎么样"]["kept"] > 0
    co = hints["cooccur"]
    assert any(v["token"] == "炸机" and v["n"] >= 2 for v in co.values())
    assert "DJI" in hints["alias"]
    assert hints["alias"]["DJI"]["n"] >= 2
    print("✓ 后缀/共现/别称候选反推 通过")


def test_aggregate() -> None:
    hf = _TMP / "effects" / "agg.jsonl"
    r1 = make_report(query="大疆 评价", n_kept=3, n_dropped=2)
    r2 = make_report(query="大疆 评价", n_kept=4, n_dropped=1)
    f1 = _TMP / "agg1.json"
    f2 = _TMP / "agg2.json"
    f1.write_text(json.dumps(r1, ensure_ascii=False), encoding="utf-8")
    f2.write_text(json.dumps(r2, ensure_ascii=False), encoding="utf-8")
    ke.scan_report(f1, hf)
    ke.scan_report(f2, hf)
    agg = ke.aggregate(ke.load_history(hf))
    assert agg["tasks"] == 2
    row = agg["funnel"][0]
    assert row["collected"] == 10 and row["kept"] == 7 and row["dropped"] == 3
    assert row["effective_rate"] == 0.7
    assert abs(agg["llm_cost"] - 1.0) < 1e-6
    print("✓ 跨任务聚合 通过")


def test_build_query_default_behavior() -> None:
    assert build_query("大疆") == "大疆 评价"
    assert build_query("大疆", strategy={}) == "大疆 评价"
    assert build_query("大疆", strategy={"suffix_pool": ["怎么样", "评价"]}) == "大疆 怎么样"
    strat = {"suffix_pool": ["评价"],
             "rules": {"websearch": {"prefer_suffix": ["测评"]}}}
    assert build_query("大疆", strategy=strat) == "大疆 测评"
    assert build_query("大疆", hint="知乎") == "大疆 知乎"
    assert build_query("大疆 怎么样") == "大疆 怎么样"
    print("✓ build_query 默认行为保持 + 策略后缀选择 通过")


def test_apply_candidates_caps() -> None:
    strat_path = _TMP / "strategy.json"
    rows = [
        {"类型": "后缀", "主题": "", "候选": "吐槽"},
        {"类型": "同义词", "主题": "大疆", "候选": "DJI"},
        {"类型": "同义词", "主题": "大疆", "候选": "DJI大疆"},
        {"类型": "同义词", "主题": "大疆", "候选": "大江"},  # 超限应被截断
        *[{"类型": "查询候选", "主题": "大疆", "候选": f"大疆 候选{i}"}
          for i in range(6)],  # 超限应被截断
    ]
    ke.apply_candidates(rows, strat_path)
    strat = ke.load_keyword_strategy(strat_path)
    assert "吐槽" in strat["suffix_pool"]
    assert strat["synonyms"]["大疆"] == ["DJI", "DJI大疆"]
    assert len(strat["extra_queries"]["大疆"]) == ke.MAX_EXTRA
    assert ke.load_keyword_strategy(_TMP / "missing.json") == {}
    print("✓ 策略配置写入 + 上限/去重/缺失回退 通过")


def test_strategy_backup_edit_restore_history() -> None:
    strat_path = _TMP / "strategy_edit.json"
    strat_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "synonyms": {"大疆": ["DJI"]},
                "extra_queries": {"大疆": ["大疆 炸机"]},
                "suffix_pool": ["评价"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    hist_path = _TMP / "strategy_history_test.jsonl"

    backup = ke.backup_strategy(strat_path)
    assert backup is not None and Path(backup).exists()

    res = ke.save_strategy_edit(
        synonyms_updates={"大疆": ["DJI", "DJI大疆", "大江"]},  # 超限 → 仅警告不截断
        extra_queries_updates={"新品牌": ["新品牌 词1", "新品牌 词2"]},
        suffix_pool=["评价", "怎么样", "吐槽", "测评", "好用吗", "值得吗"],  # 超限 → 仅警告
        path=strat_path,
    )
    assert res["saved"] and len(res["warnings"]) >= 2
    strat = ke.load_keyword_strategy(strat_path)
    assert strat["synonyms"]["大疆"] == ["DJI", "DJI大疆", "大江"]
    assert strat["extra_queries"]["新品牌"] == ["新品牌 词1", "新品牌 词2"]
    assert len(strat["suffix_pool"]) == 6

    ke.append_strategy_history("测试", "测试详情", history_file=hist_path)
    h = ke.load_strategy_history(path=hist_path)
    assert h and h[-1]["action"] == "测试"

    ke.save_strategy_edit(
        synonyms_updates={"新品牌": []},
        extra_queries_updates={"新品牌": []},
        path=strat_path,
    )
    strat2 = ke.load_keyword_strategy(strat_path)
    assert "新品牌" not in strat2.get("extra_queries", {})

    ke.restore_backup(backup, strat_path)
    strat3 = ke.load_keyword_strategy(strat_path)
    assert strat3["synonyms"]["大疆"] == ["DJI"]
    assert strat3["suffix_pool"] == ["评价"]
    print("✓ 策略配置备份/编辑（超限警告不截断）/历史/删除品牌/回滚 通过")


def _fr(channel: str, keyword: str, query: str, col: int, kept: int) -> dict:
    return {
        "channel": channel, "keyword": keyword, "query": query,
        "collected": col, "kept": kept, "dropped": col - kept,
        "drop_reasons": {}, "coded": 0, "negative": 0,
        "effective_rate": round(kept / col, 4) if col else None,
        "negative_rate": None,
    }


def test_compare_runs_metrics_verdicts() -> None:
    subject = "华润万家"
    base = {"subject": subject, "funnel": [
        _fr("websearch", subject, subject, 60, 24),
        _fr("websearch_zhihu", subject, subject + " 知乎", 10, 4),
        _fr("websearch", "万家", "万家 评价", 20, 20),
    ]}
    cur = {"subject": subject, "funnel": [
        _fr("websearch", subject, subject, 60, 42),
        _fr("websearch_zhihu", subject, subject + " 知乎", 10, 5),
        _fr("websearch", "万家", "万家 评价", 20, 20),
        _fr("websearch", subject, subject + " 购物 评价", 10, 8),
        _fr("websearch", subject, subject + " 连锁 评价", 40, 14),
        _fr("websearch", subject, subject + " 零售 评价", 12, 4),
    ]}
    strategy = {
        "extra_queries": {
            subject: [subject + " 购物", subject + " 连锁", subject + " 零售"]
        },
        "suffix_pool": ["评价"],
    }
    m = ke.compare_runs_metrics(base, cur, strategy=strategy)
    eff = m["effective"]
    assert eff["verdict"] == "达标（≥+5pp）" and eff["delta_pp"] >= 5
    assert m["pure_brand"]["present"] is True
    assert m["pure_brand"]["verdict"] == "未达标（≥15%）"
    assert m["new_queries"]["count"] == 3
    strong = [x["word"] for x in m["low_efficiency"] if x["level"] == "强建议删除"]
    weak = [x["word"] for x in m["low_efficiency"] if x["level"] == "弱建议（参考）"]
    assert subject + " 连锁" in strong
    assert subject + " 零售" in weak
    assert all(x["in_strategy"] for x in m["low_efficiency"])
    assert m["conclusion"]
    print("✓ 策略效果判定（有效率/纯品牌词/新增/低效词） 通过")


def test_compare_runs_metrics_no_pure_brand() -> None:
    base = {"subject": "大疆", "funnel": [
        _fr("websearch", "大疆 无人机", "大疆 无人机 评价", 10, 8)
    ]}
    cur = {"subject": "大疆", "funnel": [
        _fr("websearch", "大疆 无人机", "大疆 无人机 评价", 12, 9)
    ]}
    m = ke.compare_runs_metrics(base, cur, strategy={})
    assert m["pure_brand"]["present"] is False
    assert "无法判定" in m["pure_brand"].get("note", "")
    print("✓ 无品牌名关键词时的纯品牌词判定提示 通过")


def test_compare_runs_metrics_pure_side_missing() -> None:
    """基准/本次其中一侧没有品牌名关键词组时，判定不得崩溃、结论不含 None%。"""
    base = {"subject": "恋与深空", "funnel": [
        _fr("websearch", "恋与深空 剧情", "恋与深空 剧情 评价", 10, 8)
    ]}
    cur = {"subject": "恋与深空", "funnel": [
        _fr("websearch", "恋与深空", "恋与深空 评价", 10, 6)
    ]}
    m = ke.compare_runs_metrics(base, cur, strategy={})
    pb = m["pure_brand"]
    assert pb["present"] is True
    assert pb["before_drop_pct"] is None and pb["after_drop_pct"] == 40.0
    assert "None" not in m["conclusion"]

    base2 = {"subject": "恋与深空", "funnel": [
        _fr("websearch", "恋与深空", "恋与深空 评价", 10, 6)
    ]}
    cur2 = {"subject": "恋与深空", "funnel": [
        _fr("websearch", "恋与深空 剧情", "恋与深空 剧情 评价", 10, 8)
    ]}
    m2 = ke.compare_runs_metrics(base2, cur2, strategy={})
    pb2 = m2["pure_brand"]
    assert pb2["present"] is True and pb2["after_drop_pct"] is None
    assert "无法判定" in pb2["verdict"]
    assert "None" not in m2["conclusion"]
    print("✓ 纯品牌词一侧缺失时判定不崩溃（None 防护） 通过")


def test_compare_runs_metrics_cumulative_low_efficiency() -> None:
    """低效词改用累计样本：单次任务 n 不足不触发，跨任务累计后触发强建议。"""
    subject = "华润万家"
    base = {"subject": subject, "funnel": []}
    cur = {"subject": subject, "funnel": [
        _fr("websearch", subject, subject + " 购物 评价", 12, 3),   # 25%，n<30
        _fr("websearch", subject, subject + " 连锁 评价", 20, 8),   # 40%，n=20
    ]}
    agg_rows = [
        {"channel": "websearch", "query": subject + " 购物 评价",
         "collected": 40, "kept": 14, "effective_rate": 0.35},       # 累计 40 条 → 强建议
        {"channel": "websearch", "query": subject + " 连锁 评价",
         "collected": 35, "kept": 16, "effective_rate": 0.4571},     # 累计 35 条 → 强建议
    ]
    m_cur = ke.compare_runs_metrics(base, cur, strategy={})
    strong_cur = [x["word"] for x in m_cur["low_efficiency"] if x["level"] == "强建议删除"]
    assert subject + " 连锁" not in strong_cur
    m_agg = ke.compare_runs_metrics(base, cur, strategy={}, agg_rows=agg_rows)
    strong_agg = [x["word"] for x in m_agg["low_efficiency"] if x["level"] == "强建议删除"]
    assert subject + " 购物" in strong_agg
    assert subject + " 连锁" in strong_agg
    print("✓ 低效词改用累计样本（跨任务 n 合并） 通过")


def test_chinese_short_alias_hints() -> None:
    """中文短别称反推：品牌首/尾两字，仅用不含全称的保留文本作证据。"""
    subject = "华润万家"
    posts = [
        {"platform": "websearch", "keyword": subject, "title": "万家 超市 评价",
         "content": "万家 的东西不错", "url": "u1"},
        {"platform": "websearch", "keyword": subject, "title": "华润 超市 贴吧",
         "content": "华润 的超市经常打折", "url": "u2"},
        {"platform": "websearch", "keyword": subject, "title": "华润 促销",
         "content": "华润 每周有促销", "url": "u5"},
        {"platform": "websearch", "keyword": subject, "title": "华润万家 评价",
         "content": "华润万家 还不错", "url": "u3"},
        {"platform": "websearch", "keyword": subject, "title": "万家 购物",
         "content": "去万家 买年货", "url": "u4"},
    ]
    ch = {"channel_id": "websearch", "ok": True, "posts": posts, "dropped": []}
    hints = ke._extract_hints(subject, [], [ch])
    alias = hints["alias"]
    assert "万家" in alias and alias["万家"]["n"] >= 2
    assert "华润" in alias and alias["华润"]["n"] >= 2
    assert alias["万家"]["subject"] == subject
    assert "华润万家" not in alias  # 全称不作为别称

    entry = {"subject": subject, "funnel": [],
             "hints": {"suffix_stats": {}, "cooccur": {}, "alias": alias}}
    agg = ke.aggregate([entry])
    out = _TMP / "candidates_cn.csv"
    ke.write_candidates(agg, out)
    cn_rows = [r for r in csv.DictReader(open(out, encoding="utf-8-sig"))
               if r["类型"] == "同义词" and r["候选"] in ("万家", "华润")]
    assert cn_rows and all(r["主题"] == subject for r in cn_rows)

    strat_path = _TMP / "strategy_cn.json"
    ke.apply_candidates([
        {"类型": "同义词", "主题": subject, "候选": "万家"},
        {"类型": "同义词", "主题": subject, "候选": "华润"},
    ], strat_path)
    strat = ke.load_keyword_strategy(strat_path)
    assert strat["synonyms"][subject] == ["万家", "华润"]
    print("✓ 中文短别称反推（万家/华润）+ 归入同义词候选 通过")


def test_bundle_to_json_includes_channel_results() -> None:
    """聚合确认路径：bundle_to_json 必须落 channel_results，否则漏斗无法入库。"""
    from app.core.models import (
        AnalysisPlan,
        ChannelResult,
        Post,
        ReportBundle,
    )
    from app.core.pipeline import bundle_to_json

    plan = AnalysisPlan(subject="大疆", keywords=["大疆"])
    ch = ChannelResult(
        channel_id="websearch",
        ok=True,
        posts=[
            Post(id="u1", platform="websearch", keyword="大疆",
                 title="t", content="c",
                 platform_specific={"query": "大疆 评价"}),
        ],
        dropped=[],
    )
    bundle = ReportBundle(plan=plan, channel_results=[ch],
                          coded_items=[], summary={})
    out = _TMP / "bundle_roundtrip.json"
    bundle_to_json(bundle, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "channel_results" in data and len(data["channel_results"]) == 1
    payload = ke.extract_funnel(data)
    assert any(r["query"] == "大疆 评价" for r in payload["funnel"])
    print("✓ bundle_to_json 含 channel_results（聚合确认路径） 通过")


def test_aggregate_unique_url_dedup() -> None:
    """跨任务去重口径：同一 URL 只计一次，重复测量不放大 n 与有效率。"""
    from app.core.models import (
        AnalysisPlan,
        ChannelResult,
        Post,
        ReportBundle,
    )
    from app.core.pipeline import bundle_to_json

    def make_report(query: str, kept_urls, dropped_urls) -> ReportBundle:
        plan = AnalysisPlan(subject="大疆", keywords=["大疆"])
        posts = [
            Post(id=f"u{i}", platform="websearch", keyword="大疆",
                 title=f"t{i}", content=f"c{i}", url=u,
                 platform_specific={"query": query})
            for i, u in enumerate(kept_urls)
        ]
        drops = [
            {"platform": "websearch", "url": u, "title": "d",
             "keyword": "大疆", "query": query, "reason": "不相关"}
            for u in dropped_urls
        ]
        return ReportBundle(
            plan=plan,
            channel_results=[
                ChannelResult(channel_id="websearch", ok=True,
                              posts=posts, dropped=drops),
            ],
            coded_items=[], summary={},
        )

    hf = _TMP / "ke_dedup_hist.jsonl"
    f1 = _TMP / "dedup_r1.json"
    f2 = _TMP / "dedup_r2.json"
    bundle_to_json(make_report("大疆 评价",
                               ["https://a", "https://b"], ["https://x"]), f1)
    bundle_to_json(make_report("大疆 评价",
                               ["https://a", "https://c"], ["https://y"]), f2)
    ke.scan_report(f1, hf)
    ke.scan_report(f2, hf)
    hist = ke.load_history(hf)
    raw = ke.aggregate(hist)["funnel"][0]
    uniq = ke.aggregate_unique(hist)["funnel"][0]
    assert raw["collected"] == 6 and raw["kept"] == 4  # 重复计数
    assert uniq["collected"] == 5 and uniq["kept"] == 3  # 去重后 a 只计一次
    assert uniq["dropped"] == 2 and uniq["effective_rate"] == 0.6
    stale = ke.aggregate_unique([{"run_id": "sha256:missing", "funnel": []}])
    assert stale["dedup_missing"] == ["sha256:missing"]
    print("✓ 跨任务 URL 去重聚合（重复测量不放大样本） 通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_extract_funnel()
    test_scan_dedupe()
    test_suffix_and_candidate_hints()
    test_aggregate()
    test_build_query_default_behavior()
    test_apply_candidates_caps()
    test_strategy_backup_edit_restore_history()
    test_compare_runs_metrics_verdicts()
    test_compare_runs_metrics_no_pure_brand()
    test_compare_runs_metrics_pure_side_missing()
    test_compare_runs_metrics_cumulative_low_efficiency()
    test_chinese_short_alias_hints()
    test_bundle_to_json_includes_channel_results()
    test_aggregate_unique_url_dedup()
    print("关键词效果测试全部通过 ✅")


if __name__ == "__main__":
    main()
