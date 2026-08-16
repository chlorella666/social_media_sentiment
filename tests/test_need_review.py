# -*- coding: utf-8 -*-
"""2.11 需复核闭环测试：低置信/疑似反讽自动标记。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.models import Post  # noqa: E402
from app.core.planner import build_plan  # noqa: E402
from app.coding.coder import Coder, need_review_reason  # noqa: E402
from app.coding.llm_analyzer import LLMConfig, OpenAICompatibleAnalyzer  # noqa: E402


class FakeLLM(OpenAICompatibleAnalyzer):
    def __init__(self):
        super().__init__(LLMConfig(
            api_key="sk-x", base_url="http://127.0.0.1:1", model="deepseek-chat"))

    def analyze_batch(self, texts, on_batch_progress=None, dimension_schema=None, subject=None):
        return [{"sentiment": "neutral", "score": 0.0, "confidence": 0.3, "keywords": []}
                for _ in texts]

    def analyze_narrative(self, texts, on_batch_progress=None):
        return [{"narrative": "conflict", "attribution": "enterprise"} for _ in texts]


def test_need_review_reason_logic() -> None:
    assert "低置信" in need_review_reason("随便一句话", 0.3)
    assert "反讽" in need_review_reason("真是好厉害啊，像XX一样", 0.9)
    assert need_review_reason("这是一段完全普通的文本内容", 0.9) == ""
    assert "黑话" in need_review_reason("这波水军带节奏，挤牙膏", 0.9)
    assert "词典直判" in need_review_reason(
        "这是一段完全普通的文本内容", 0.9, direct=True, domain="digital3c")
    # 领域分权：问句/短句文本信号仅 digital3c 生效
    assert "短句" in need_review_reason("依旧", 0.9, domain="digital3c")
    assert "问句" in need_review_reason("这个值得买吗？", 0.9, domain="digital3c")
    assert "问句" not in need_review_reason("这个值得买吗？", 0.9, domain="other")
    print("✓ need_review 原因判定（低置信/反讽/黑话/词典直判/领域分权）通过")


def test_coder_marks_low_confidence() -> None:
    plan = build_plan(
        subject="OPPO", domain_id="digital3c", dimension_ids=[],
        keyword_groups=[], manual_keywords=["OPPO"],
        channel_ids=["demo"], date_start=date(2026, 8, 15),
        date_end=date(2026, 8, 16), per_keyword_limit=5,
        comments_enabled=False, comments_per_post=0, llm_enabled=True,
    )
    posts = [
        Post(id="p1", platform="weibo", keyword="OPPO", title="", content="纯介绍内容无情感词",
             url="https://x/1", timestamp="2026-08-16"),
        Post(id="p2", platform="weibo", keyword="OPPO", title="", content="真是好厉害啊，像XX一样",
             url="https://x/2", timestamp="2026-08-16"),
    ]
    items = Coder(FakeLLM(), None).code_posts(posts, plan)
    nr = [it for it in items if it.need_review]
    assert len(nr) == len(items) == 2, "低置信/疑似反讽样本应全部标记 need_review"
    assert all("低置信" in it.need_review_reason for it in nr)
    assert all(it.reviewed_by == "" for it in nr)
    print("✓ Coder 自动标记需复核（低置信/反讽）通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_need_review_reason_logic()
    test_coder_marks_low_confidence()
    print("2.11 需复核闭环测试全部通过 ✅")


if __name__ == "__main__":
    main()
