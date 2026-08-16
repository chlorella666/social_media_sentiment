# -*- coding: utf-8 -*-
"""WebSearch 鲁棒性测试（2.2 风控/兜底，无网络）。

运行：python tests/test_websearch_robust.py
覆盖：风控特征词识别、诊断摘要、360 异常/空结果 → cn.bing 兜底、
      双引擎空结果返回 diag（可诊断/可风控识别）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.channels import websearch as ws  # noqa: E402


def test_risk_info() -> None:
    assert "验证码" in ws._risk_info("请输入验证码后重试")
    assert "captcha" in ws._risk_info("captcha challenge")
    assert ws._risk_info("正常搜索结果页面") == []
    assert ws._risk_info("访问过于频繁，请稍后再试") == ["访问过于频繁"]
    print("✓ 风控特征词识别 通过")


def test_diag_compact() -> None:
    diag = [
        {"engine": "360", "status": 200, "items": 0, "error": "", "risk": ["验证码"]},
        {"engine": "cn.bing", "status": 0, "items": 0, "error": "timeout", "risk": []},
    ]
    s = ws._diag_compact(diag)
    assert "360" in s and "验证码" in s
    assert "cn.bing" in s and "timeout" in s
    print("✓ 诊断摘要（状态码/结果数/错误/风控特征） 通过")


def test_engine_fallback_on_360_error() -> None:
    orig = (ws._fetch, ws._parse_360, ws._parse_bing, ws._quality_check)

    def fake_fetch(session, url, referer=""):
        if "so.com" in url:
            raise RuntimeError("360 段包异常")
        return "<html>bing ok</html>", 200, ""

    ws._fetch = fake_fetch
    ws._parse_360 = lambda html: []
    ws._parse_bing = lambda html: [
        {"title": "大疆 无人机", "url": "https://x", "snippet": ""}
    ]
    ws._quality_check = lambda kw, items: True
    try:
        engine, items, diag = ws._search_engine(None, "大疆", 1)
        assert engine == "cn.bing" and len(items) == 1
        assert diag[0]["engine"] == "360" and diag[0]["error"]
    finally:
        ws._fetch, ws._parse_360, ws._parse_bing, ws._quality_check = orig
    print("✓ 360 异常 → 自动尝试 cn.bing 兜底 通过")


def test_engine_empty_diag_both_fail() -> None:
    orig = (ws._fetch, ws._parse_360, ws._parse_bing, ws._quality_check)

    def fake_fetch(session, url, referer=""):
        return "", 200, ""

    ws._fetch = fake_fetch
    ws._parse_360 = lambda html: []
    ws._parse_bing = lambda html: []
    ws._quality_check = lambda kw, items: True
    try:
        engine, items, diag = ws._search_engine(None, "大疆", 1)
        assert engine == "" and items == [] and len(diag) == 3  # 360/bing/quark
        assert all(d["items"] == 0 for d in diag)
    finally:
        ws._fetch, ws._parse_360, ws._parse_bing, ws._quality_check = orig
    print("✓ 双引擎空结果返回 diag（可诊断/可风控识别） 通过")


def test_hard_risk_stops_no_second_engine() -> None:
    """验证码即停：360 命中验证码 → 不再请求 bing/夸克，并加入会话封禁。"""
    orig = (ws._fetch, ws._parse_360, ws._parse_bing, ws._quality_check)
    calls: list[str] = []

    def fake_fetch(session, url, referer=""):
        calls.append(url)
        return "<html>请输入验证码</html>", 200, ""

    ws._fetch = fake_fetch
    ws._parse_360 = lambda html: []
    ws._parse_bing = lambda html: []
    ws._quality_check = lambda kw, items: True
    blocked: set[str] = set()
    try:
        engine, items, diag = ws._search_engine(None, "大疆", 1, blocked)
        assert engine == "" and items == []
        assert len(calls) == 1, "命中验证码后不应再请求后续引擎"
        assert "360" in blocked
        assert _has_risk(diag, "验证码")
    finally:
        ws._fetch, ws._parse_360, ws._parse_bing, ws._quality_check = orig
    print("✓ 验证码即停（不再请求后续引擎 + 会话内封禁） 通过")


def test_blocked_engine_skipped() -> None:
    """会话内已封禁的引擎不再请求，直接跳转到下一个。"""
    orig = (ws._fetch, ws._parse_360, ws._parse_bing, ws._quality_check)
    calls: list[str] = []

    def fake_fetch(session, url, referer=""):
        calls.append(url)
        return "<html>bing ok</html>", 200, ""

    ws._fetch = fake_fetch
    ws._parse_360 = lambda html: []
    ws._parse_bing = lambda html: [
        {"title": "大疆 无人机", "url": "https://x", "snippet": ""}
    ]
    ws._quality_check = lambda kw, items: True
    try:
        engine, items, diag = ws._search_engine(
            None, "大疆", 1, blocked={"360"}
        )
        assert engine == "cn.bing" and len(items) == 1
        assert not any("so.com" in u for u in calls)
        assert any(d.get("status") == "skip" for d in diag)
    finally:
        ws._fetch, ws._parse_360, ws._parse_bing, ws._quality_check = orig
    print("✓ 会话内封禁引擎跳过（减少无效请求） 通过")


def test_parse_generic_quark_fallback() -> None:
    html_text = (
        '<div class="item"><h3><a href="https://example.com/a">'
        "大疆 无人机 评测</a></h3></div>"
    )
    items = ws._parse_generic(html_text)
    assert len(items) == 1
    assert items[0]["url"] == "https://example.com/a"
    assert "大疆" in items[0]["title"]
    print("✓ 夸克等未定结构引擎的通用解析兜底 通过")


def test_day_used_counter() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sms_ws_day_"))
    os.environ["SMS_STATE_DIR"] = str(tmp)
    try:
        assert ws._day_used() == 0
        ws._add_day_used(3)
        assert ws._day_used() == 3
        ws._add_day_used(5)
        assert ws._day_used() == 8
    finally:
        os.environ.pop("SMS_STATE_DIR", None)
    print("✓ 每日关键词总量计数 通过")


def test_probe_engines_status() -> None:
    """一键探针：三引擎独立状态（风控/正常/失败），互不影响。"""
    orig = (ws._fetch, ws._parse_360, ws._parse_bing, ws._quality_check)

    def fake_fetch(session, url, referer=""):
        if "so.com" in url:
            return "<html>请输入验证码</html>", 200, ""
        if "quark" in url:
            raise RuntimeError("network down")
        return "<html>bing results</html>", 200, ""

    ws._fetch = fake_fetch
    ws._parse_360 = lambda html: []
    ws._parse_bing = lambda html: [
        {"title": "大疆 无人机", "url": "https://x", "snippet": ""}
    ]
    ws._quality_check = lambda kw, items: True
    try:
        results = ws.probe_engines("大疆 评价")
        by = {r["engine"]: r["status"] for r in results}
        assert len(results) == 3
        assert by["360"] == "risk"
        assert by["cn.bing"] == "ok"
        assert by["quark"] == "error"
    finally:
        ws._fetch, ws._parse_360, ws._parse_bing, ws._quality_check = orig
    print("✓ 一键探针三引擎状态（风控/正常/失败） 通过")


def test_lightweight_probe_status() -> None:
    """轻量体检探测（渠道诊断用）：只打 360 单引擎，风控/正常/降级/空识别。"""
    orig = (ws._fetch, ws._parse_360)
    holder: dict = {"html": "", "items": []}

    def fake_fetch(session, url, referer=""):
        holder["url"] = url
        return holder["html"], 200, ""

    ws._fetch = fake_fetch
    ws._parse_360 = lambda html: holder["items"]
    try:
        holder["html"] = "<html>请输入验证码</html>"
        holder["items"] = []
        r = ws.lightweight_probe("大疆 评价")
        assert r["status"] == "risk" and "验证码" in r["message"]
        assert "so.com" in holder["url"], "轻量探测应只打 360 主引擎"

        holder["html"] = "<html>results</html>"
        holder["items"] = [{"title": "大疆 无人机", "url": "https://x", "snippet": ""}]
        assert ws.lightweight_probe("大疆 评价")["status"] == "ok"

        holder["items"] = [{"title": "无关内容", "url": "https://y", "snippet": ""}]
        assert ws.lightweight_probe("大疆 评价")["status"] == "degraded"

        holder["html"] = ""
        holder["items"] = []
        assert ws.lightweight_probe("大疆 评价")["status"] == "empty"
    finally:
        ws._fetch, ws._parse_360 = orig
    print("✓ 轻量体检探测（360 单引擎：风控/正常/降级/空） 通过")


def _has_risk(diag: list[dict], marker: str) -> bool:
    return any(marker in (d.get("risk") or []) for d in diag)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_risk_info()
    test_diag_compact()
    test_engine_fallback_on_360_error()
    test_engine_empty_diag_both_fail()
    test_hard_risk_stops_no_second_engine()
    test_blocked_engine_skipped()
    test_parse_generic_quark_fallback()
    test_day_used_counter()
    test_probe_engines_status()
    test_lightweight_probe_status()
    print("WebSearch 鲁棒性测试全部通过 ✅")


if __name__ == "__main__":
    main()
