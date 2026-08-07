"""LLM 分析器单元测试。

运行：python tests/test_llm_analyzer.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coding.llm_analyzer import LLMConfig, MockAnalyzer, OpenAICompatibleAnalyzer


class FakeResponse:
    def __init__(self, content: str):
        self.content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self.content}}]}


class SlowLLM(OpenAICompatibleAnalyzer):
    """模拟较慢的 DeepSeek 响应，用于验证并发进度回调。"""

    def __init__(self):
        super().__init__(
            LLMConfig(api_key="sk-x", base_url="http://127.0.0.1:1", model="deepseek-chat"),
            max_workers=2,
            batch_size=3,
        )

    def ping(self, timeout=20):
        return True, "ok"

    def _request_batch(self, texts):
        time.sleep(0.05)
        return [
            {"sentiment": "positive", "score": 0.8, "confidence": 0.9, "keywords": []}
            for _ in texts
        ]

    def _request_narrative_batch(self, texts):
        time.sleep(0.02)
        return [
            {"narrative": "human_interest", "attribution": "enterprise"}
            for _ in texts
        ]


def test_ping_unreachable() -> None:
    cfg = LLMConfig(api_key="sk-test", base_url="http://127.0.0.1:1", model="deepseek-chat")
    analyzer = OpenAICompatibleAnalyzer(cfg)
    ok, msg = analyzer.ping(timeout=5)
    assert not ok
    assert "网络" in msg or "连接" in msg, msg
    print(f"✓ ping 失败提示友好：{msg}")


def test_batch_fallback_records_errors() -> None:
    cfg = LLMConfig(api_key="sk-test", base_url="http://127.0.0.1:1", model="deepseek-chat")
    analyzer = OpenAICompatibleAnalyzer(cfg)
    texts = ["这个产品很好用", "价格太贵了", "随便看看"]
    results = analyzer.analyze_batch(texts)
    assert len(results) == 3
    assert results[0]["sentiment"] == "positive"
    assert results[1]["sentiment"] == "negative"
    assert analyzer.errors, "失败必须记录错误，不能静默"
    print(f"✓ 失败降级并上报错误：{analyzer.errors[0][:60]}...")


def test_parse_batch_with_index() -> None:
    content = (
        '{"items":[{"index":1,"sentiment":"negative","score":-0.8,"confidence":0.9,"keywords":["贵"]},'
        '{"index":0,"sentiment":"positive","score":0.8,"confidence":0.9,"keywords":["好用"]}]}'
    )
    parsed = OpenAICompatibleAnalyzer._parse_batch(content, 2)
    assert parsed[0]["sentiment"] == "positive"
    assert parsed[1]["sentiment"] == "negative"
    print("✓ 批量结果按 index 对齐")


def test_reasoner_omits_unsupported_params() -> None:
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        return FakeResponse('{"items":[]}')

    with mock.patch("app.coding.llm_analyzer.requests.post", side_effect=fake_post):
        cfg = LLMConfig(api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-reasoner")
        analyzer = OpenAICompatibleAnalyzer(cfg)
        analyzer._chat("system", "user", use_json=True)
        assert "temperature" not in captured["json"]
        assert "response_format" not in captured["json"]

        cfg2 = LLMConfig(api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-chat")
        analyzer2 = OpenAICompatibleAnalyzer(cfg2)
        analyzer2._chat("system", "user", use_json=True)
        assert captured["json"]["temperature"] == 0.2
        assert captured["json"]["response_format"] == {"type": "json_object"}
    print("✓ deepseek-chat 带 temperature/response_format，reasoner 自动省略")


def test_mock_analyzer() -> None:
    analyzer = MockAnalyzer()
    assert analyzer.ping() == (True, "词典模式无需连接")
    res = analyzer.analyze_batch(["太垃圾了"])
    assert res[0]["sentiment"] == "negative"
    assert analyzer.errors == []
    insights = analyzer.generate_insights({"overall": "测试数据"})
    assert set(insights["chart_insights"]) == {
        "overall", "platform", "trend", "dimensions", "heatmap", "words",
        "intensity", "radar", "platform_dim", "date_dim", "wordcloud",
        "cooccurrence",
    }
    assert insights["conclusion"]
    print("✓ Mock 分析器（无 Key 降级）正常，模板洞察可用")


def test_batch_progress_updates_per_completion() -> None:
    llm = SlowLLM()
    texts = [f"文本{i}" for i in range(9)]
    ticks: list[tuple[int, int]] = []
    results = llm.analyze_batch(texts, on_batch_progress=lambda d, t: ticks.append((d, t)))
    assert [d for d, _ in ticks] == [3, 6, 9], ticks
    assert len(results) == 9
    assert results[0]["sentiment"] == "positive"
    print("✓ 并发批量进度按完成批次实时更新（修复首批慢时进度不动）")


def test_narrative_batching_progress() -> None:
    llm = SlowLLM()
    texts = [f"文本{i}" for i in range(7)]
    ticks: list[tuple[int, int]] = []
    results = llm.analyze_narrative(texts, on_batch_progress=lambda d, t: ticks.append((d, t)))
    assert ticks[-1] == (7, 7), ticks
    assert results[0]["narrative"] == "human_interest"
    assert results[0]["attribution"] == "enterprise"
    print("✓ 叙事/归因并发批量 + 进度回调正常")


def test_narrative_sanitizes_invalid_values() -> None:
    """模型把 unclear 误填进 narrative 时，自动清洗而不是崩溃。"""
    cfg = LLMConfig(api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-chat")
    analyzer = OpenAICompatibleAnalyzer(cfg)
    fake_body = '{"items":[{"index":0,"narrative":"unclear","attribution":"robot"}]}'
    with mock.patch(
        "app.coding.llm_analyzer.requests.post",
        side_effect=lambda *a, **k: FakeResponse(fake_body),
    ):
        items = analyzer._request_narrative_batch(["某条文本"])
    assert items[0]["narrative"] is None
    assert items[0]["attribution"] is None
    assert any("非法" in e for e in analyzer.errors)
    print("✓ 非法叙事/归因取值自动清洗（修复 unclear 崩溃）")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_ping_unreachable()
    test_batch_fallback_records_errors()
    test_parse_batch_with_index()
    test_reasoner_omits_unsupported_params()
    test_mock_analyzer()
    test_batch_progress_updates_per_completion()
    test_narrative_batching_progress()
    test_narrative_sanitizes_invalid_values()
    print("LLM 分析器测试全部通过 ✅")
