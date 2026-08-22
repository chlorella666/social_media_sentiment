# -*- coding: utf-8 -*-
"""渠道诊断测试（体检 × 一键探针融合，docs/archive/渠道诊断融合方案.md，无网络）。

覆盖：WebSearch 风控页不再误报可用（修复"体检骗人"）、降级→warn、
多渠道并行 + 系统状态合并、demo 跳过、小红书环境分支、微博凭据、旧接口兼容。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.channels import health  # noqa: E402


def _probe(status: str, items: int = 3, message: str = "", risk: list | None = None) -> dict:
    return {
        "engine": "360", "status": status, "items": items,
        "message": message or status, "risk": risk or [], "http": 200,
    }


def test_websearch_captcha_page_not_ok() -> None:
    """核心用例：HTTP 200 但页面含验证码 → risk → 判定不可用（不再骗人）。"""
    with mock.patch.object(health, "lightweight_probe",
                           return_value=_probe("risk", items=0, risk=["验证码"])):
        r = health.check_websearch_rich("大疆 评价")
        assert r["ok"] is False and r["level"] == "error"
        assert "风控" in r["msg"]
    with mock.patch.object(health, "lightweight_probe", return_value=_probe("degraded", items=5)):
        r2 = health.check_websearch_rich("大疆 评价")
        assert r2["ok"] is False and r2["level"] == "warn"
    with mock.patch.object(health, "lightweight_probe", return_value=_probe("ok")):
        r3 = health.check_websearch_rich("大疆 评价")
        assert r3["ok"] is True and r3["level"] == "ok"
    with mock.patch.object(health, "lightweight_probe", return_value=_probe("error", items=0)):
        r4 = health.check_websearch_rich("大疆 评价")
        assert r4["level"] == "error"
    with mock.patch.object(health, "lightweight_probe", return_value=_probe("empty", items=0)):
        r5 = health.check_websearch_rich("大疆 评价")
        assert r5["level"] == "warn"
    print("✓ WebSearch 体检：风控/降级/正常/失败/空 状态映射 通过")


def test_check_channels_parallel_and_system() -> None:
    """多渠道并行（<0.45s）+ 系统状态（暂停/配额）合并。"""
    def fake_rich(cid, params=None):
        if cid == "demo":
            return {"ok": True, "level": "ok", "msg": "演示渠道无需检查", "detail": {}}
        time.sleep(0.2)
        return {"ok": True, "level": "ok", "msg": f"{cid} ok", "detail": {}}

    def fake_allowed(cid):
        return (cid != "weibo", "渠道已暂停" if cid == "weibo" else "")

    with mock.patch.object(health, "check_channel_rich", side_effect=fake_rich), \
         mock.patch.object(health.jobs, "check_channel_allowed", side_effect=fake_allowed), \
         mock.patch.object(health.jobs, "channel_state", return_value={
             "quota_limit": 24, "quota_used": 12, "paused": 0, "cool_until": None,
         }):
        t0 = time.monotonic()
        out = health.check_channels(["bilibili", "weibo", "demo", "websearch"], {})
        dt = time.monotonic() - t0
    assert set(out) == {"bilibili", "weibo", "demo", "websearch"}
    assert dt < 0.45, f"并行失败：{dt:.2f}s（3 通道各睡 0.2s）"
    assert out["demo"]["system"]["ok"] is True
    assert out["weibo"]["system"]["ok"] is False
    assert "暂停" in out["weibo"]["system"]["text"]
    assert out["websearch"]["system"]["text"] == "今日配额 12/24"
    print("✓ 多渠道并行（<0.45s）+ 系统状态合并 通过")


def test_demo_skip_and_xhs_env() -> None:
    assert health.check_channel_rich("demo")["level"] == "ok"
    with mock.patch.object(health, "_opencli_ready",
                           return_value=(True, "opencli 就绪")):
        assert health.check_xiaohongshu_rich()["level"] == "ok"
    with mock.patch.object(health, "_opencli_ready",
                           return_value=(False, "未安装 opencli/Node")):
        r = health.check_xiaohongshu_rich()
        assert r["level"] == "warn" and "opencli" in r["msg"]
    print("✓ demo 跳过 + 小红书环境分支 通过")


def test_weibo_cookie_warn_and_ok() -> None:
    r = health.check_weibo_rich("")
    assert r["ok"] is False and r["level"] == "warn"
    with mock.patch.object(health.requests, "get") as m:
        m.return_value.json.return_value = {"ok": 1}
        r2 = health.check_weibo_rich("SUB=x")
        assert r2["ok"] is True and r2["level"] == "ok"
    print("✓ 微博凭据缺省 warn / 有效 ok 通过")


def test_backward_compat_tuple() -> None:
    with mock.patch.object(health, "lightweight_probe", return_value=_probe("ok")):
        ok, msg = health.check_channel_health("websearch", {"query": "大疆 评价"})
        assert ok is True and "360" in msg
    print("✓ 旧 check_channel_health tuple 接口兼容 通过")




def test_opencli_installer() -> None:
    """一键安装 opencli：命令组装（镜像/非镜像）+ 成功/缺 npm 分支。"""
    with mock.patch.object(health.shutil, "which", side_effect=lambda n: f"C:/tools/{n}.exe"):
        assert health.node_ready() is True
        assert health._npm_cmd() == "C:/tools/npm.exe"

    calls: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = iter(["added 1 package"])

        def wait(self, timeout=None) -> None:
            return None

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return FakeProc()

    with mock.patch.object(health.subprocess, "Popen", side_effect=fake_popen):
        r = health.install_opencli(use_mirror=True)
        assert r["ok"] is True and "安装完成" in r["message"]
        assert calls[-1][-2:] == ["--registry", health.NPM_MIRROR_REGISTRY]
        r2 = health.install_opencli(use_mirror=False)
        assert r2["ok"] is True
        assert calls[-1][-1] == "@jackwener/opencli"

    with mock.patch.object(health, "_npm_cmd", return_value=None):
        r3 = health.install_opencli()
        assert r3["ok"] is False and "npm" in r3["message"]
    print("✓ opencli 一键安装：命令组装/镜像/缺 npm 分支 通过")


def test_install_node_winget() -> None:
    """一键安装 Node：winget 命令与缺 winget 分支。"""
    calls: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = iter(["installed"])

        def wait(self, timeout=None) -> None:
            return None

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return FakeProc()

    with mock.patch.object(health.shutil, "which", return_value="C:/Windows/System32/winget.exe"), \
         mock.patch.object(health.subprocess, "Popen", side_effect=fake_popen):
        r = health.install_node_winget()
    assert r["ok"] is True and "重启应用" in r["message"]
    assert calls[0][0].endswith("winget.exe")
    assert "OpenJS.NodeJS.LTS" in calls[0]
    with mock.patch.object(health.shutil, "which", return_value=None):
        r2 = health.install_node_winget()
    assert r2["ok"] is False and "winget" in r2["message"]
    print("✓ 一键安装 Node（winget）：命令与缺 winget 分支 通过")
def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_websearch_captcha_page_not_ok()
    test_check_channels_parallel_and_system()
    test_demo_skip_and_xhs_env()
    test_weibo_cookie_warn_and_ok()
    test_backward_compat_tuple()
    test_opencli_installer()
    test_install_node_winget()
    print("渠道诊断测试全部通过 ✅")


if __name__ == "__main__":
    main()
