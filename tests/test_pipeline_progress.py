"""进度回调边界测试：任何阶段的进度都必须在 [0, 1] 内（修复 150% 问题）。"""

from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coding.llm_analyzer import LLMConfig, MockAnalyzer, OpenAICompatibleAnalyzer
from app.core.pipeline import TaskRunner
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

    def _request_batch(self, texts):
        time.sleep(0.02)
        return [
            {"sentiment": "negative", "score": -0.7, "confidence": 0.9, "keywords": []}
            for _ in texts
        ]

    def _request_narrative_batch(self, texts):
        time.sleep(0.01)
        return [
            {"narrative": "attribution", "attribution": "enterprise"}
            for _ in texts
        ]

    def generate_insights(self, descriptors):
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
        }


class InvalidNarrativeLLM(FakeLLM):
    """模拟模型把 unclear 误填进 narrative 字段。"""

    def _request_narrative_batch(self, texts):
        return [
            {"narrative": "unclear", "attribution": "enterprise"}
            for _ in texts
        ]


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
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_progress_never_exceeds_100()
    test_llm_and_narrative_progress()
    test_invalid_narrative_does_not_crash()
    print("进度边界测试全部通过 ✅")
