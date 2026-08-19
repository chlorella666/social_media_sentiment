"""进度回调边界测试：任何阶段的进度都必须在 [0, 1] 内（修复 150% 问题）。"""

from __future__ import annotations

import datetime as dt
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.channels.base import ChannelAdapter
from app.coding.llm_analyzer import LLMConfig, MockAnalyzer, OpenAICompatibleAnalyzer
import app.core.pipeline as pipeline_mod
from app.core.models import ChannelResult, Post
from app.core.pipeline import TaskRunner, _reconcile_channel_posts
from app.core.planner import build_plan, generate_keyword_groups
from app.domains.loader import load_domain


class FakeLLM(OpenAICompatibleAnalyzer):
    """模拟可用的 DeepSeek：快速返回情感 + 叙事结果。"""

    def __init__(self):
        super().__init__(
            LLMConfig(api_key="sk-x", base_url="http://127.0.0.1:1", model="deepseek-chat"),
            max_workers=2,
            batch_size=3,
        )

    def ping(self, timeout=20):
        return True, "ok"

    def _request_batch(self, texts, dimension_schema=None, subject=None):
        time.sleep(0.02)
        return [
            {"sentiment": "negative", "score": -0.7, "confidence": 0.9,
             "keywords": [], "dimension_sentiments": {"monetization": "negative"}}
            for _ in texts
        ]

    def _request_narrative_batch(self, texts):
        time.sleep(0.01)
        return [
            {"narrative": "attribution", "attribution": "enterprise"}
            for _ in texts
        ]

    def generate_insights(self, descriptors, evidence=None, summary=None):
        return {
            "chart_insights": {
                cid: f"{cid} 图表解析（测试）"
                for cid in [
                    "overall", "platform", "trend", "dimensions", "heatmap", "words",
                    "intensity", "radar", "platform_dim", "date_dim", "wordcloud",
                    "cooccurrence",
                ]
            },
            "conclusion": "测试深度结论：按责任归因与冲突框架给出行动建议。",
            "findings": [
                {
                    "id": "F1",
                    "claim": "测试发现：负面集中在价格维度（n=10）",
                    "evidence_refs": [e["id"] for e in (evidence or [])][:1],
                    "action": "建议品牌方在微博渠道核对价格相关反馈，详见 F1。",
                }
            ],
        }


class InvalidNarrativeLLM(FakeLLM):
    """模拟模型把 unclear 误填进 narrative 字段。"""

    def _request_narrative_batch(self, texts):
        return [
            {"narrative": "unclear", "attribution": "enterprise"}
            for _ in texts
        ]


class CoordinatedChannel(ChannelAdapter):
    """模拟渠道：先报 90% 并等待放行，再报 100% 完成。

    用于复现"多渠道并行、某渠道完成时其他渠道仍在高位进度"的交错场景。
    """

    def __init__(self, cid: str, name: str, reported: threading.Event, start: threading.Event):
        self.id = cid
        self.name = name
        self._reported = reported
        self._start = start

    def collect(self, plan, on_progress=None, cancel_event=None, skip_urls=None):
        if on_progress:
            on_progress(f"{self.name}：90%", 0.9)
        self._reported.set()
        if not self._start.wait(timeout=15):
            raise RuntimeError("协调事件超时")
        if on_progress:
            on_progress(f"{self.name}：完成", 1.0)
        return ChannelResult(channel_id=self.id, ok=True, posts=[])


class TopupChannel(ChannelAdapter):
    """模拟触发补采：首采含可保留与丢弃内容，补采返回新内容并记录 skip_urls。"""

    def __init__(self, first_kept: int = 1, topup_new: int = 3):
        self.id = "topup_fake"
        self.name = "补采渠道"
        self.calls = 0
        self.last_skip_urls: set[str] | None = None
        self._first_kept = first_kept
        self._topup_new = topup_new

    def collect(self, plan, on_progress=None, cancel_event=None, skip_urls=None):
        self.calls += 1
        if on_progress:
            on_progress("采集完成", 1.0)
        if self.calls == 1:
            posts = [
                Post(id=f"d{i}", platform=self.id, keyword="测试 评价",
                     title="", url=f"https://t/d{i}", content=f"加载中{i}")
                for i in range(2)
            ]
            for i in range(self._first_kept):
                posts.append(
                    Post(id=f"k0_{i}", platform=self.id, keyword="测试 评价",
                         title="", url=f"https://t/k0_{i}",
                         content=f"测试 评价 很好用，续航和画质都很出色，首轮{i}")
                )
            return ChannelResult(
                channel_id=self.id, ok=True,
                posts=posts,
            )
        self.last_skip_urls = set(skip_urls or ())
        if on_progress:
            on_progress("补采中 50%", 0.5)
            on_progress("补采完成", 1.0)
        return ChannelResult(
            channel_id=self.id, ok=True,
            posts=[
                Post(id=f"k{i}", platform=self.id, keyword="测试 评价",
                     title="", url=f"https://t/k{i}",
                     content=f"测试 评价 很好用，续航和画质都很出色，补采{i}")
                for i in range(self._topup_new)
            ],
        )


def test_progress_never_exceeds_100() -> None:
    schema = load_domain("game")
    groups = generate_keyword_groups("原神", schema, [d.id for d in schema.dimensions])
    plan = build_plan(
        subject="原神",
        domain_id="game",
        dimension_ids=[d.id for d in schema.dimensions],
        keyword_groups=groups,
        manual_keywords=None,
        channel_ids=["demo"],
        date_start=dt.date.today() - dt.timedelta(days=30),
        date_end=dt.date.today(),
        comments_enabled=True,
        comments_per_post=5,
        llm_enabled=True,  # 传入 Mock，验证即使启用 LLM 进度也不越界
        narrative_enabled=True,
    )
    values: list[tuple[str, float]] = []
    last_snapshot: dict = {}

    def on_progress(status, message, progress, snapshot=None):
        nonlocal last_snapshot
        values.append((message, progress))
        if snapshot:
            last_snapshot = snapshot

    runner = TaskRunner(plan, on_progress=on_progress)
    runner.run(analyzer=MockAnalyzer())

    assert values, "应有进度回调"
    assert {"collect", "clean", "lexicon", "llm", "narrative", "report"} <= set(
        last_snapshot["steps"]
    ), "任务清单缺少步骤"
    assert last_snapshot["steps"]["collect"]["state"] == "done"
    assert last_snapshot["steps"]["llm"]["state"] in ("done", "skipped")
    out_of_range = [(m, p) for m, p in values if not (0.0 <= p <= 1.0)]
    assert not out_of_range, f"进度越界: {out_of_range}"
    assert max(p for _, p in values) == 1.0
    # 进度必须单调不回退（词典预筛 → LLM → 报告）
    for prev, cur in zip(values, values[1:]):
        assert cur[1] >= prev[1], f"进度回退: {prev[0]} {prev[1]} -> {cur[0]} {cur[1]}"
    print(f"✓ 共 {len(values)} 次进度回调，全部在 [0,1] 内且单调递增（修复 150% 问题）")


def test_phase_weights_ordered() -> None:
    """阶段权重区间必须递增衔接，单调兜底才不会把进度卡死。"""
    plan = build_plan(
        subject="测试", domain_id=None, dimension_ids=[], keyword_groups=[],
        manual_keywords=["测试"], channel_ids=["demo"], date_start=None,
        date_end=None, llm_enabled=False,
    )
    runner = TaskRunner(plan)
    segments = list(runner._phase_weights.values())
    starts = [s for s, _ in segments]
    ends = [e for _, e in segments]
    assert starts[0] == 0.0, "首阶段必须从 0 开始"
    assert ends[-1] == 1.0, "末阶段必须到 100%"
    assert starts == sorted(starts) and ends == sorted(ends), "区间必须递增"
    assert all(e == nxt for e, nxt in zip(ends, starts[1:])), "区间必须无缝衔接"
    print("✓ 阶段权重区间递增衔接（0-50-60-90-100）")


def test_collection_progress_monotonic_multichannel() -> None:
    """多渠道并行：渠道完成时不得用"完成数/渠道数"重算导致整体倒退。"""
    ids = ["fake_a", "fake_b", "fake_c", "fake_d"]
    reported = {cid: threading.Event() for cid in ids}
    start = threading.Event()
    fakes = {
        cid: CoordinatedChannel(cid, f"渠道{idx}", reported[cid], start)
        for idx, cid in enumerate(ids)
    }
    orig_get_channel = pipeline_mod.get_channel
    pipeline_mod.get_channel = lambda cid: fakes[cid]
    plan = build_plan(
        subject="测试", domain_id=None, dimension_ids=[], keyword_groups=[],
        manual_keywords=["测试 评价"], channel_ids=ids, date_start=None,
        date_end=None, per_keyword_limit=3, comments_enabled=False,
        comments_per_post=0, llm_enabled=False, channel_params={},
    )
    values: list[tuple[str, float]] = []

    def on_progress(status, message, progress, snapshot=None):
        values.append((message, progress))

    runner = TaskRunner(plan, on_progress=on_progress)

    def release() -> None:
        for ev in reported.values():
            if not ev.wait(timeout=15):
                return
        start.set()

    coord = threading.Thread(target=release, daemon=True)
    coord.start()
    try:
        runner.collect_and_clean(analyzer=MockAnalyzer())
    finally:
        pipeline_mod.get_channel = orig_get_channel
    coord.join(timeout=5)

    assert values, "应有进度回调"
    for prev, cur in zip(values, values[1:]):
        assert cur[1] >= prev[1], f"进度回退: {prev[0]} {prev[1]} -> {cur[0]} {cur[1]}"
    assert max(p for _, p in values) >= 0.5, "采集阶段应推进到 50%"
    print("✓ 多渠道交错采集进度单调不回退（渠道完成统一按均值口径）")


def test_topup_progress_stays_in_cleaning_band() -> None:
    """补采进度归位：触发补采时整体在清洗段 55%~60% 内，且采集步骤不回退为运行中。"""
    fake = TopupChannel()
    orig_get_channel = pipeline_mod.get_channel
    pipeline_mod.get_channel = lambda cid: fake
    plan = build_plan(
        subject="测试", domain_id=None, dimension_ids=[], keyword_groups=[],
        manual_keywords=["测试 评价"], channel_ids=["topup_fake"], date_start=None,
        date_end=None, per_keyword_limit=3, comments_enabled=False,
        comments_per_post=0, llm_enabled=False, channel_params={},
    )
    values: list[tuple[str, float, dict]] = []

    def on_progress(status, message, progress, snapshot=None):
        values.append((message, progress, snapshot or {}))

    runner = TaskRunner(plan, on_progress=on_progress)
    try:
        runner.collect_and_clean(analyzer=MockAnalyzer())
    finally:
        pipeline_mod.get_channel = orig_get_channel

    topup = [(m, p) for m, p, _ in values if "补采" in m]
    assert topup, "应触发补采"
    assert fake.calls >= 2, "补采应发起第二次采集"
    for m, p in topup:
        assert 0.55 <= p <= 0.60, f"补采进度越出清洗段 55%~60%: {m} {p}"
    for prev, cur in zip(values, values[1:]):
        assert cur[1] >= prev[1], f"进度回退: {prev[0]} {prev[1]} -> {cur[0]} {cur[1]}"
    for m, _, snap in values:
        if "补采" in m:
            assert snap.get("steps", {}).get("collect", {}).get("state") == "done", (
                "补采不应把'采集数据'步骤改回运行中"
            )
    print("✓ 补采进度归位到清洗段 55%~60%，采集步骤状态保持完成")


def test_topup_skips_already_collected_and_merges() -> None:
    """补采 A+B：跳过已采内容、合并首轮保留帖，净新增达标才继续。"""
    fake = TopupChannel(first_kept=1, topup_new=3)
    orig_get_channel = pipeline_mod.get_channel
    pipeline_mod.get_channel = lambda cid: fake
    plan = build_plan(
        subject="测试", domain_id=None, dimension_ids=[], keyword_groups=[],
        manual_keywords=["测试 评价"], channel_ids=["topup_fake"], date_start=None,
        date_end=None, per_keyword_limit=3, comments_enabled=False,
        comments_per_post=0, llm_enabled=False, channel_params={},
    )
    runner = TaskRunner(plan)
    try:
        res = runner.collect_and_clean(analyzer=MockAnalyzer())
    finally:
        pipeline_mod.get_channel = orig_get_channel

    assert fake.calls == 2, "应恰好补采一次"
    assert fake.last_skip_urls is not None
    assert "https://t/k0_0" in fake.last_skip_urls, "补采应跳过已保留内容"
    urls = {p.url for p in res["posts"]}
    assert "https://t/k0_0" in urls, "首轮保留帖不应因补采丢失"
    assert all(f"https://t/k{i}" in urls for i in range(3)), "补采新帖应进入结果"
    assert not any("补采收益低" in w for w in res["warnings"])
    print("✓ 补采跳过已采内容 + 首轮保留帖合并 + 净新增达标继续")


def test_topup_low_yield_stops() -> None:
    """补采 B：净新增低于阈值即停，并给出可解释警告。"""
    fake = TopupChannel(first_kept=0, topup_new=1)
    orig_get_channel = pipeline_mod.get_channel
    pipeline_mod.get_channel = lambda cid: fake
    plan = build_plan(
        subject="测试", domain_id=None, dimension_ids=[], keyword_groups=[],
        manual_keywords=["测试 评价"], channel_ids=["topup_fake"], date_start=None,
        date_end=None, per_keyword_limit=3, comments_enabled=False,
        comments_per_post=0, llm_enabled=False, channel_params={},
    )
    runner = TaskRunner(plan)
    try:
        res = runner.collect_and_clean(analyzer=MockAnalyzer())
    finally:
        pipeline_mod.get_channel = orig_get_channel

    assert any("补采收益低" in w for w in res["warnings"]), "应有收益低警告"
    assert fake.calls == 2, "低收益后不应继续第三轮"
    print("✓ 补采收益低即停并警告（不再无谓追加请求）")


def test_llm_and_narrative_progress() -> None:
    """启用 LLM + 叙事/归因时，进度仍单调且两阶段都产出结果。"""
    schema = load_domain("game")
    groups = generate_keyword_groups("原神", schema, [d.id for d in schema.dimensions])
    plan = build_plan(
        subject="原神",
        domain_id="game",
        dimension_ids=[d.id for d in schema.dimensions],
        keyword_groups=groups,
        manual_keywords=None,
        channel_ids=["demo"],
        date_start=dt.date.today() - dt.timedelta(days=30),
        date_end=dt.date.today(),
        comments_enabled=True,
        comments_per_post=5,
        llm_enabled=True,
        narrative_enabled=True,
    )
    values: list[tuple[str, float]] = []
    last_snapshot: dict = {}

    def on_progress(status, message, progress, snapshot=None):
        nonlocal last_snapshot
        values.append((message, progress))
        if snapshot:
            last_snapshot = snapshot

    runner = TaskRunner(plan, on_progress=on_progress)
    bundle = runner.run(analyzer=FakeLLM())

    assert values
    for prev, cur in zip(values, values[1:]):
        assert cur[1] >= prev[1], f"进度回退: {prev[0]} {prev[1]} -> {cur[0]} {cur[1]}"
    assert max(p for _, p in values) == 1.0
    assert any("LLM 精分析" in m for m, _ in values), "缺少 LLM 阶段进度"
    assert any("叙事/归因" in m for m, _ in values), "缺少叙事阶段进度"
    assert last_snapshot["steps"]["llm"]["state"] == "done"
    assert last_snapshot["steps"]["narrative"]["state"] == "done"
    assert last_snapshot["steps"]["lexicon"]["state"] == "done"
    assert any(it.method == "llm" for it in bundle.coded_items), "缺少 LLM 编码结果"
    assert any(it.narrative for it in bundle.coded_items), "缺少叙事分析结果"
    assert bundle.chart_insights and len(bundle.chart_insights) == 12, "缺少图表解析"
    assert bundle.conclusion, "缺少深度结论"
    assert bundle.findings, "LLM 模式应产出 findings"
    assert bundle.insight_mode == "llm", f"LLM 模式 insight_mode 应为 llm，实际 {bundle.insight_mode}"
    assert bundle.evidence, "LLM 模式应有证据卡供 findings 引用"
    print("✓ LLM + 叙事/归因双阶段进度单调，图表解析与深度结论完整")


def test_invalid_narrative_does_not_crash() -> None:
    """模型返回非法叙事值时，分析必须正常完成而不是中断。"""
    schema = load_domain("consumer")
    groups = generate_keyword_groups("欧莱雅", schema, [d.id for d in schema.dimensions])
    plan = build_plan(
        subject="欧莱雅",
        domain_id="consumer",
        dimension_ids=[d.id for d in schema.dimensions],
        keyword_groups=groups,
        manual_keywords=None,
        channel_ids=["demo"],
        date_start=dt.date.today() - dt.timedelta(days=30),
        date_end=dt.date.today(),
        comments_enabled=True,
        comments_per_post=3,
        llm_enabled=True,
        narrative_enabled=True,
    )
    bundle = TaskRunner(plan).run(analyzer=InvalidNarrativeLLM())
    assert bundle.summary["total_items"] > 0
    assert not any(it.narrative for it in bundle.coded_items)
    print("✓ 非法叙事值不中断分析（回归：unclear 崩溃）")


def test_reconcile_channel_posts_consistency() -> None:
    """补采一致性收尾：channel_results 与返回 posts 同源；
    补采原始帖经最终清洗，相关的进入报告、无关的进丢弃明细。"""
    plan = build_plan(
        subject="测试", domain_id=None, dimension_ids=[], keyword_groups=[],
        manual_keywords=["测试 评价"], channel_ids=["demo"], date_start=None,
        date_end=None, per_keyword_limit=3, comments_enabled=False,
        comments_per_post=0, llm_enabled=False, channel_params={},
    )
    ch = ChannelResult(
        channel_id="demo", ok=True,
        posts=[
            Post(id="a", platform="demo", keyword="测试 评价",
                 title="", url="https://demo/a",
                 content="测试 评价 很好用，用了两周非常满意，续航和画质都很出色"),
            Post(id="b", platform="demo", keyword="测试 评价",
                 title="", url="https://demo/b",
                 content="完全无关的文本，不包含关键词"),
        ],
        dropped=[{"platform": "demo", "url": "legacy", "title": "",
                  "reason": "旧轮次丢弃"}],
    )
    warnings: list[str] = []
    kept = _reconcile_channel_posts([ch], plan, warnings)
    assert [p.id for p in kept] == ["a"]
    assert [p.id for p in ch.posts] == ["a"]  # 统计与渠道结果同源
    assert any("补采一致性清洗" in d["reason"] for d in ch.dropped)
    assert any("补采一致性清洗" in w for w in warnings)
    print("✓ 补采一致性收尾（posts 与 channel_results 同源）通过")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_progress_never_exceeds_100()
    test_phase_weights_ordered()
    test_collection_progress_monotonic_multichannel()
    test_topup_progress_stays_in_cleaning_band()
    test_topup_skips_already_collected_and_merges()
    test_topup_low_yield_stops()
    test_llm_and_narrative_progress()
    test_invalid_narrative_does_not_crash()
    test_reconcile_channel_posts_consistency()
    print("进度边界测试全部通过 ✅")
