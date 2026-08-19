# -*- coding: utf-8 -*-
"""2.8 自定义维度统计 + 全量 LLM 路由测试（2026-08-18）。

覆盖：维度输入解析/校验、task_schema 合并（custom_ 前缀 + 自定义标记）、
Coder 全量 LLM 路由（llm_enabled / custom_dimensions 触发）、
benchmark 混合模式 direct=False 契约。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.models import Post  # noqa: E402
from app.core.planner import (  # noqa: E402
    MAX_CUSTOM_DIMENSIONS,
    build_plan,
    parse_custom_dimensions,
)
from app.coding.coder import Coder  # noqa: E402
from app.coding.llm_analyzer import LLMConfig, OpenAICompatibleAnalyzer  # noqa: E402
from app.domains.loader import task_schema  # noqa: E402


class RecordingLLM(OpenAICompatibleAnalyzer):
    """记录调用次数的假分析器（不发起真实网络请求）。"""

    def __init__(self):
        super().__init__(LLMConfig(
            api_key="sk-x", base_url="http://127.0.0.1:1", model="deepseek-chat"))
        self.calls: list[dict] = []

    def analyze_batch(self, texts, on_batch_progress=None, dimension_schema=None, subject=None):
        self.calls.append({"texts": list(texts), "dims": dimension_schema})
        return [
            {"sentiment": "neutral", "score": 0.0, "confidence": 0.9,
             "keywords": [], "dimension_sentiments": {}}
            for _ in texts
        ]

    def analyze_narrative(self, texts, on_batch_progress=None):
        return [{"narrative": "conflict", "attribution": "enterprise"} for _ in texts]


def _plan(**kw) -> "object":
    defaults = dict(
        subject="华润万家", domain_id="modules_physical", dimension_ids=[],
        keyword_groups=[], manual_keywords=["华润万家"], channel_ids=["demo"],
        date_start=date(2026, 8, 1), date_end=date(2026, 8, 18),
        per_keyword_limit=5, comments_enabled=False, comments_per_post=0,
    )
    defaults.update(kw)
    return build_plan(**defaults)


def _post(cid: str, content: str) -> Post:
    return Post(id=cid, platform="weibo", keyword="华润万家", title="",
                content=content, url=f"https://x/{cid}", timestamp="2026-08-18")


def test_parse_custom_dimensions() -> None:
    dims, errs = parse_custom_dimensions("联名活动：联名,IP,周边\n物流体验：发货,快递,物流,配送")
    assert not errs and len(dims) == 2
    assert dims[0].id == "custom_01" and dims[0].name == "联名活动"
    assert dims[0].keywords == ["联名", "IP", "周边"]
    # 非法行 / 超长名 / 关键词数量越界 / 上限 4
    _, errs = parse_custom_dimensions("没有冒号")
    assert errs
    _, errs = parse_custom_dimensions("超长维度名称测试一下：a,b")
    assert any("超过 8 字" in e for e in errs)
    _, errs = parse_custom_dimensions("单关键词：x")
    assert any("少于 2 个" in e for e in errs)
    dims, errs = parse_custom_dimensions(
        "\n".join(f"维度{i}：k{i}a,k{i}b" for i in range(5)))
    assert len(dims) == MAX_CUSTOM_DIMENSIONS and errs
    print("✓ 自定义维度解析/校验（格式/长度/关键词数/上限）通过")


def test_task_schema_merge() -> None:
    dims, _ = parse_custom_dimensions("联名活动：联名,IP,周边")
    plan = _plan(custom_dimensions=dims)
    schema = task_schema(plan)
    assert schema is not None
    names = {d.id: d.name for d in schema.dimensions}
    assert "custom_01" in names and names["custom_01"] == "联名活动（自定义）"
    # 未选模块：schema = 仅自定义维度
    plan2 = _plan(domain_id=None, custom_dimensions=dims)
    s2 = task_schema(plan2)
    assert s2 is not None and s2.domain_id == "custom"
    assert [d.id for d in s2.dimensions] == ["custom_01"]
    # 无自定义且无领域 → None（不启用维度分析）
    assert task_schema(_plan(domain_id=None)) is None
    print("✓ task_schema 合并（custom_ 前缀/自定义标记/仅自定义维度）通过")


def test_coder_full_llm_routing() -> None:
    # 无领域（整体情感，非强制 LLM 域）+ llm_enabled=True → 高置信文本也全量送 LLM
    llm = RecordingLLM()
    plan = _plan(domain_id=None, llm_enabled=True)
    items = Coder(llm, task_schema(plan)).code_posts(
        [_post("p1", "太棒了，非常满意，强烈推荐！")], plan)
    assert llm.calls and items[0].method == "llm", "llm_enabled 应全量送 LLM"
    # 含自定义维度（llm_enabled=False）→ 强制全量 LLM
    dims, _ = parse_custom_dimensions("联名活动：联名,IP,周边")
    llm2 = RecordingLLM()
    plan2 = _plan(domain_id=None, llm_enabled=False, custom_dimensions=dims)
    items2 = Coder(llm2, task_schema(plan2)).code_posts(
        [_post("p2", "太棒了，非常满意，强烈推荐！")], plan2)
    assert llm2.calls and items2[0].method == "llm", "含自定义维度应强制全量 LLM"
    # 两者皆无 + 高置信 → 词典直判（不送 LLM）
    llm3 = RecordingLLM()
    plan3 = _plan(domain_id=None, llm_enabled=False)
    items3 = Coder(llm3, task_schema(plan3)).code_posts(
        [_post("p3", "太棒了，非常满意，强烈推荐！")], plan3)
    assert not llm3.calls and items3[0].method == "lexicon"
    print("✓ Coder 路由：llm_enabled/自定义维度全量 LLM，高置信无开关仍词典")


def test_benchmark_hybrid_direct_zero() -> None:
    from tests.benchmark_golden import predict_all

    rows = [
        {"text": "太棒了，非常满意，强烈推荐！", "domain": "modules_physical",
         "brand": "华润万家", "dimension_sentiments": "{}"},
        {"text": "随便一句话没有任何情感倾向和立场", "domain": "",
         "brand": "", "dimension_sentiments": "{}"},
    ]
    llm = RecordingLLM()
    preds = predict_all(rows, use_llm=True, llm=llm)
    assert all(p["direct"] is False for p in preds), "混合模式应全量 LLM（direct=False）"
    assert all(p["llm_used"] for p in preds)
    # 词典模式保持阈值口径（高置信直判）
    preds_lex = predict_all(rows, use_llm=False, llm=None)
    assert preds_lex[0]["direct"] is True or preds_lex[0]["llm_used"] is False
    print("✓ benchmark 契约：混合模式 direct_rate=0，词典模式阈值口径不变")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_parse_custom_dimensions()
    test_task_schema_merge()
    test_coder_full_llm_routing()
    test_benchmark_hybrid_direct_zero()
    print("自定义维度 + 全量 LLM 路由测试全部通过 ✅")


if __name__ == "__main__":
    main()
