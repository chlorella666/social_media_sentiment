# -*- coding: utf-8 -*-
"""渠道风控即停测试（2026-08-16，2.6 收口项）。

覆盖：小红书/微博检测到风控（验证码/频繁）→ 立即停止该渠道、保留已采部分、
标记 risk；其他渠道不受影响（渠道隔离由 pipeline 并行保证，回归覆盖）。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from app.core.planner import build_plan  # noqa: E402


def _plan(channel_id: str, keywords: list[str], params: dict | None = None) -> object:
    return build_plan(
        subject="OPPO", domain_id="digital3c", dimension_ids=[],
        keyword_groups=[], manual_keywords=keywords,
        channel_ids=[channel_id],
        date_start=date(2026, 8, 15), date_end=date(2026, 8, 16),
        per_keyword_limit=5, comments_enabled=False, comments_per_post=0,
        llm_enabled=False, channel_params={channel_id: {"limit": 5, **(params or {})}},
    )


def test_xiaohongshu_risk_stop_keeps_collected() -> None:
    """小红书：搜索命中验证码 → 立即停止，保留已采笔记，标记 risk。"""
    from app.channels import xiaohongshu as xhs

    item = {
        "url": "https://www.xiaohongshu.com/explore/abc",
        "title": "笔记一", "published_at": "2026-08-16",
        "author": "a", "likes": "100",
    }
    calls = {"n": 0}

    def fake_search(keyword):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("触发验证码，请稍后再试")
        return [item]

    with mock.patch.object(xhs, "_opencli_base", return_value=None), \
         mock.patch.object(xhs, "_search_notes", side_effect=fake_search), \
         mock.patch.object(xhs, "_note_detail", return_value={}):
        res = xhs.XiaohongshuChannel().collect(_plan("xiaohongshu", ["k1", "k2", "k3"]))
    assert res.ok is False
    assert res.risk is True
    assert "风控" in res.error
    assert len(res.posts) == 1, "风控停止应保留已采部分"
    assert calls["n"] == 2, "命中风控后不应再请求后续关键词"
    print("✓ 小红书风控即停（保留已采 + 不再请求后续） 通过")


class _FakeResp:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"ok": 1, "data": {"cards": [{
            "card_type": 9,
            "mblog": {"bid": "bid1", "text": "微博内容", "created_at": "刚刚", "ad_marked": 0},
        }]}}


class _FakeSession:
    def __init__(self) -> None:
        self.n = 0

    def get(self, *a, **k):
        self.n += 1
        if self.n >= 2:
            raise requests.RequestException("触发验证码，请稍后再试")
        return _FakeResp()


def test_weibo_risk_stop_keeps_collected() -> None:
    """微博：请求命中验证码 → 立即停止，保留已采微博，标记 risk。"""
    from app.channels import weibo as wb

    with mock.patch.object(wb, "_build_headers", return_value={}), \
         mock.patch.object(wb.requests, "Session", lambda: _FakeSession()):
        res = wb.WeiboChannel().collect(
            _plan("weibo", ["k1", "k2"], {"cookie": "SUB=x"}))
    assert res.ok is False
    assert res.risk is True
    assert "风控" in res.error
    assert len(res.posts) == 1, "风控停止应保留已采部分"
    print("✓ 微博风控即停（保留已采 + 不再请求后续） 通过")


def test_jittered_sleep_range() -> None:
    """请求间隔抖动：落在均值±ratio 区间内、均值≈base、不固定。"""
    from app.channels.base import jittered_sleep

    seen: list[float] = []
    with mock.patch(
        "app.channels.base._time.sleep", side_effect=lambda s: seen.append(s)
    ):
        for _ in range(300):
            jittered_sleep(1.0, 0.3)
    assert seen, "应产生休眠"
    assert all(0.7 <= s <= 1.3 for s in seen), "间隔应落在 [0.7, 1.3] 内"
    mean = sum(seen) / len(seen)
    assert 0.95 <= mean <= 1.05, f"均值应≈1.0，实际 {mean:.3f}"
    assert len({round(s, 3) for s in seen}) > 5, "间隔不应是固定值"
    print("✓ 请求间隔抖动在区间内且均值稳定（打破固定节奏）")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_xiaohongshu_risk_stop_keeps_collected()
    test_weibo_risk_stop_keeps_collected()
    test_jittered_sleep_range()
    print("渠道风控即停测试全部通过 ✅")


if __name__ == "__main__":
    main()
