"""LLM 分析器单元测试。

运行：python tests/test_llm_analyzer.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coding.llm_analyzer import LLMConfig, MockAnalyzer, OpenAICompatibleAnalyzer


class FakeResponse:
    def __init__(self, content: str, finish_reason: str = ""):
        self.content = content
        self.finish_reason = finish_reason

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {
            "choices": [
                {
                    "message": {"content": self.content},
                    "finish_reason": self.finish_reason,
                }
            ]
        }


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

    def _request_batch(self, texts, dimension_schema=None, subject=None):
        time.sleep(0.05)
        return [
            {"sentiment": "positive", "score": 0.8, "confidence": 0.9,
             "keywords": [], "dimension_sentiments": {}}
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


def test_llm_entries_desensitize_before_send() -> None:
    """P1-3：LLM 前脱敏覆盖全部文本入口（相关性复核/情感批量/叙事批量），
    发送给模型的 chunk 不得含手机号/@用户/链接。"""
    from app.coding.cleaner import desensitize_text

    analyzer = OpenAICompatibleAnalyzer(
        LLMConfig(api_key="x", base_url="http://x", model="m")
    )
    pii = "联系 13800138000 或 a@b.com，@某用户 https://x.com 不错"
    captured: dict = {}

    def fake_relevance(chunk, **_kw):
        captured["rel"] = chunk
        return [True] * len(chunk)  # 真实路径 sanitize_fn 在此步内完成

    analyzer._structured_batch = fake_relevance  # type: ignore[method-assign]
    out = analyzer.check_relevance("测试", [pii, "正常文本"])
    assert out == [True, True]
    assert "13800138000" not in captured["rel"][0]
    assert "a@b.com" not in captured["rel"][0]
    assert "@某用户" not in captured["rel"][0]
    assert "x.com" not in captured["rel"][0]

    def fake_sentiment(texts, dimension_schema=None, subject=None):
        captured["sent"] = texts
        return [
            {"sentiment": "positive", "score": 0.5, "confidence": 0.9,
             "keywords": [], "dimension_sentiments": {}}
            for _ in texts
        ]

    analyzer._request_batch = fake_sentiment  # type: ignore[method-assign]
    analyzer.analyze_batch([pii, "正常文本"])
    assert "13800138000" not in captured["sent"][0]
    assert "@某用户" not in captured["sent"][0]

    def fake_narrative(texts):
        captured["narr"] = texts
        return [{"narrative": None, "attribution": None} for _ in texts]

    analyzer._request_narrative_batch = fake_narrative  # type: ignore[method-assign]
    analyzer.analyze_narrative([pii, "正常文本"])
    assert "13800138000" not in captured["narr"][0]
    # 幂等：再脱敏一次结果不变
    assert desensitize_text(captured["sent"][0]) == captured["sent"][0]
    print("✓ P1-3：相关性/情感/叙事三入口发送前均脱敏（幂等） 通过")


def test_truncation_split_retry() -> None:
    """finish_reason=length 触发拆小批重试，整层不丢失。"""
    cfg = LLMConfig(api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-chat")
    analyzer = OpenAICompatibleAnalyzer(cfg)
    calls = {"n": 0}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse('{"items":[{"index":0}]}', finish_reason="length")
        body = kwargs["json"]["messages"][1]["content"]
        texts = json.loads(body)["texts"]
        items = [
            {"index": i, "narrative": "conflict", "attribution": "enterprise"}
            for i in range(len(texts))
        ]
        return FakeResponse(json.dumps({"items": items}, ensure_ascii=False))

    with mock.patch("app.coding.llm_analyzer.requests.post", side_effect=fake_post):
        texts = [f"文本{i}" for i in range(8)]
        results = analyzer._request_narrative_batch(texts)
    assert len(results) == 8
    assert results[0]["narrative"] == "conflict"
    assert calls["n"] == 3, f"应为 1 次截断 + 2 次小批成功，实际 {calls['n']}"
    assert not any("批量请求失败" in e for e in analyzer.errors)
    print("✓ 截断自动拆小批重试（8 条 → 1 次截断 + 2 次小批成功）")


def test_small_batch_fallback_on_truncation() -> None:
    """小批（≤3 条）截断时不再拆分，走兜底并上报错误。"""
    cfg = LLMConfig(api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-chat")
    analyzer = OpenAICompatibleAnalyzer(cfg)
    with mock.patch(
        "app.coding.llm_analyzer.requests.post",
        side_effect=lambda *a, **k: FakeResponse(
            '{"items":[{"index":0}]}', finish_reason="length"
        ),
    ):
        results = analyzer._request_narrative_batch(["a", "b"])
    assert results == [
        {"narrative": None, "attribution": None},
        {"narrative": None, "attribution": None},
    ]
    assert any("批量请求失败" in e for e in analyzer.errors)
    print("✓ 小批截断走兜底并上报错误")


def test_missing_index_completion_recovers() -> None:
    """小批缺失 index（纯格式失败）→ 一次定向补全请求恢复，不降级。"""
    cfg = LLMConfig(api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-chat")
    analyzer = OpenAICompatibleAnalyzer(cfg)
    calls = {"n": 0}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # 缺 index 1（只返回了 index 0）
            return FakeResponse(
                json.dumps(
                    {
                        "items": [
                            {"index": 0, "narrative": "conflict", "attribution": "enterprise"}
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        body = kwargs["json"]["messages"][1]["content"]
        assert "缺少以下 index：1" in body, "补全请求应指明缺失的 index"
        assert '"index": 1' in body, "补全请求应携带缺失条目的原文"
        return FakeResponse(
            json.dumps(
                {
                    "items": [
                        {"index": 1, "narrative": "human_interest", "attribution": "individual"}
                    ]
                },
                ensure_ascii=False,
            )
        )

    with mock.patch("app.coding.llm_analyzer.requests.post", side_effect=fake_post):
        results = analyzer._request_narrative_batch(["a", "b"])
    assert len(results) == 2
    assert results[0]["narrative"] == "conflict"
    assert results[1]["narrative"] == "human_interest"
    assert results[1]["attribution"] == "individual"
    assert calls["n"] == 2
    assert not any("批量请求失败" in e for e in analyzer.errors)
    print("✓ 缺失 index 小批经定向补全恢复（无降级、无错误上报）")


def test_missing_index_completion_fails_falls_back() -> None:
    """补全请求仍失败时，回退兜底并上报错误（不中断主流程）。"""
    cfg = LLMConfig(api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-chat")
    analyzer = OpenAICompatibleAnalyzer(cfg)

    def fake_post(url, **kwargs):
        # 首轮缺 index 1；补全轮返回空 items → 补全失败
        return FakeResponse('{"items":[]}')

    with mock.patch(
        "app.coding.llm_analyzer.requests.post", side_effect=fake_post
    ):
        results = analyzer._request_narrative_batch(["a", "b"])
    assert results == [
        {"narrative": None, "attribution": None},
        {"narrative": None, "attribution": None},
    ]
    assert any("批量请求失败" in e for e in analyzer.errors)
    print("✓ 补全失败回退兜底并上报错误")


def test_sentiment_missing_index_completion_recovers() -> None:
    """情感层同样启用缺失 index 定向补全（词典兜底前先尝试恢复）。"""
    cfg = LLMConfig(api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-chat")
    analyzer = OpenAICompatibleAnalyzer(cfg)
    calls = {"n": 0}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(
                json.dumps(
                    {
                        "items": [
                            {"index": 0, "sentiment": "positive", "score": 0.8,
                             "confidence": 0.9, "keywords": []}
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        body = kwargs["json"]["messages"][1]["content"]
        assert "缺少以下 index：1" in body, "补全请求应指明缺失的 index"
        return FakeResponse(
            json.dumps(
                {
                    "items": [
                        {"index": 1, "sentiment": "negative", "score": -0.6,
                         "confidence": 0.8, "keywords": []}
                    ]
                },
                ensure_ascii=False,
            )
        )

    with mock.patch("app.coding.llm_analyzer.requests.post", side_effect=fake_post):
        results = analyzer._request_batch(["a", "b"])
    assert results[0]["sentiment"] == "positive"
    assert results[1]["sentiment"] == "negative"
    assert not any("批量请求失败" in e for e in analyzer.errors)
    print("✓ 情感层缺失 index 定向补全恢复（未走词典兜底）")


def test_subject_anchor_instruction_and_cache() -> None:
    """subject（跟本品牌口径）：system 注入锚定品牌指令、user 带 subject；
    缓存按 subject 区分（同文本不同品牌不串结果）。"""
    from app.coding.llm_analyzer import SUBJECT_DOMAINS

    assert "digital3c" in SUBJECT_DOMAINS
    cfg = LLMConfig(api_key="sk-test", base_url="http://127.0.0.1:1", model="deepseek-chat")
    analyzer = OpenAICompatibleAnalyzer(cfg)
    calls: list[tuple[str, str]] = []

    def fake_post(system, user, **kwargs):
        calls.append((system, user))
        body = json.dumps({
            "items": [{"index": 0, "sentiment": "negative", "score": -0.6,
                       "confidence": 0.9, "keywords": []}],
        }, ensure_ascii=False)
        return body, ""  # _post_chat 返回 (content, finish_reason)

    with mock.patch.object(analyzer, "_post_chat", side_effect=fake_post):
        r1 = analyzer.analyze_batch(["夸竞品贬本品牌"], subject="影石")
        r2 = analyzer.analyze_batch(["夸竞品贬本品牌"], subject=None)
    assert len(calls) == 2, "同文本不同 subject 不应命中同一缓存"
    sys_with, user_with = calls[0]
    assert "锚定品牌口径" in sys_with and "影石" in sys_with
    assert json.loads(user_with)["subject"] == "影石"
    sys_wo, user_wo = calls[1]
    assert "锚定品牌口径" not in sys_wo
    assert "subject" not in json.loads(user_wo)
    assert r1[0]["sentiment"] == "negative" and r2[0]["sentiment"] == "negative"
    print("✓ subject 锚定品牌指令 + 缓存隔离 通过")


def test_v34_rule_gating() -> None:
    """SMS_V34_RULES 规则开关：置空回到 v3.3+B，子集只含对应规则。"""
    from app.coding import llm_analyzer as la

    cfg = LLMConfig(api_key="sk-test", base_url="http://127.0.0.1:1", model="deepseek-chat")
    analyzer = OpenAICompatibleAnalyzer(cfg)
    captured: dict[str, str] = {}

    def fake_post(system, user, **kwargs):
        captured["system"] = system
        body = json.dumps({"items": [{"index": 0, "sentiment": "neutral",
                                      "score": 0.0, "confidence": 0.5,
                                      "keywords": []}]}, ensure_ascii=False)
        return body, ""

    def run(v34: str, v35: str | None = None) -> str:
        a2 = OpenAICompatibleAnalyzer(cfg)
        env = {"SMS_V34_RULES": v34}
        if v35 is not None:
            env["SMS_V35_RULES"] = v35
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch.object(a2, "_post_chat", side_effect=fake_post):
            a2.analyze_batch(["测试文本"])
        return captured["system"]

    sys_off = run("", "")
    assert "报道/资讯体（v3.4）" not in sys_off
    assert "行动信号与平淡情绪（v3.4）" not in sys_off
    assert "锚定品牌口径" not in sys_off  # 无 subject
    sys_11 = run("11")
    assert "报道/资讯体（v3.4）" in sys_11
    assert "行动信号与平淡情绪（v3.4）" not in sys_11
    sys_all = run(la._V34_RULES_DEFAULT)
    assert la._V34_RULES_DEFAULT == "12,13,14"
    assert "报道/资讯体（v3.4）" not in sys_all  # v11 消融后默认关闭
    assert "行动信号与平淡情绪（v3.4）" in sys_all
    assert "subject 口径强化（v3.4）" in sys_all
    sys_v35 = run(la._V34_RULES_DEFAULT, la._V35_RULES_DEFAULT)
    assert la._V35_RULES_DEFAULT == "15,16,17"
    assert "问句二分（v3.5）" in sys_v35
    assert "报道细分（v3.5）" in sys_v35
    assert "功能/能力陈述的评价色彩（v3.5）" in sys_v35
    sys_v35_off = run(la._V34_RULES_DEFAULT, "")
    assert "问句二分（v3.5）" not in sys_v35_off
    print("✓ v3.4/v3.5 规则开关（SMS_V34_RULES / SMS_V35_RULES 消融） 通过")


def test_v38_rule_gating() -> None:
    """SMS_V38_RULES 规则开关：默认全开、置空回到 v3.7、子集可单规则消融。"""
    from app.coding import llm_analyzer as la

    cfg = LLMConfig(api_key="sk-test", base_url="http://127.0.0.1:1",
                    model="deepseek-chat")
    schema = {
        "dimensions": [
            {"id": "character", "name": "角色偏好", "description": "角色",
             "keywords": ["角色", "人设"]},
            {"id": "brand_image", "name": "品牌形象", "description": "品牌",
             "keywords": ["品牌", "营销"]},
        ]
    }
    captured: dict[str, str] = {}

    def fake_post(system, user, **kwargs):
        captured["system"] = system
        body = json.dumps({"items": [{"index": 0, "sentiment": "neutral",
                                      "score": 0.0, "confidence": 0.5,
                                      "keywords": [],
                                      "dimension_sentiments": {}}]},
                          ensure_ascii=False)
        return body, ""

    def run(v38: str) -> str:
        a2 = OpenAICompatibleAnalyzer(cfg)
        with mock.patch.dict("os.environ", {"SMS_V38_RULES": v38}), \
             mock.patch.object(a2, "_post_chat", side_effect=fake_post):
            a2.analyze_batch(["测试文本"], dimension_schema=schema)
        return captured["system"]

    tags = ("19a. 维度判维双信号", "19b. 长文本分段扫描", "19c. 游戏侧维度边界",
            "19d. 品牌形象语境", "19e. 竞品对比边界", "19f. 渠道服务语境",
            "19g. 字面词去噪", "19h. 页面壳不产生维度情感", "20. emoji 维度依据")
    sys_all = run(la._V38_RULES_DEFAULT)
    # 消融（2026-08-21）：19b 双输移出默认，保留为可选开关
    assert la._V38_RULES_DEFAULT == "19a,19c,19d,19e,19f,19g,19h,20"
    assert all(t in sys_all for t in tags if t != "19b. 长文本分段扫描")
    assert "19b. 长文本分段扫描" not in sys_all, "19b 消融后不应默认注入"
    sys_off = run("")
    assert all(t not in sys_off for t in tags), "置空应回到 v3.7（无 v3.8 规则）"
    sys_only = run("19a")
    assert "19a. 维度判维双信号" in sys_only
    assert "20. emoji 维度依据" not in sys_only
    sys_19b = run("19b")
    assert "19b. 长文本分段扫描" in sys_19b, "19b 仍应可通过开关显式启用"
    print("✓ v3.8 规则开关（SMS_V38_RULES 消融） 通过")


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
    test_llm_entries_desensitize_before_send()
    test_truncation_split_retry()
    test_small_batch_fallback_on_truncation()
    test_missing_index_completion_recovers()
    test_missing_index_completion_fails_falls_back()
    test_sentiment_missing_index_completion_recovers()
    test_subject_anchor_instruction_and_cache()
    test_v34_rule_gating()
    test_v38_rule_gating()
    print("LLM 分析器测试全部通过 ✅")
