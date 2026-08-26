"""F-011（2026-08-26）：配置中心只读状态判定（LLM / 微博 / Node / opencli）。

只读复用 secrets / health，不新增写路径（避免依赖环 G6）；
sidebar / ②渠道页 / ④确认页 / 结果页共用同一判定函数，避免口径漂移。
"""

from __future__ import annotations

import os
import subprocess

from app.channels import health
from app.core.secrets import load_api_key, load_cookie


def _masked(key: str) -> str:
    key = (key or "").strip()
    if len(key) <= 4:
        return "****"
    return "****" + key[-4:]


def llm_status() -> dict:
    """LLM API Key 状态：{level, text, has_key}（尾号脱敏）。"""
    key = load_api_key(allow_env=False)
    if key:
        return {
            "level": "ok",
            "text": f"✅ LLM API Key：已配置（尾号 {_masked(key)}）",
            "has_key": True,
        }
    env_key = os.environ.get("OPENAI_API_KEY", "")
    if env_key:
        return {
            "level": "ok",
            "text": "✅ LLM API Key：已检测到环境变量配置（OPENAI_API_KEY，开发场景）",
            "has_key": True,
        }
    return {
        "level": "warn",
        "text": "⚠️ LLM API Key：未配置 —— 开启 LLM 将自动降级词典模式（免费离线）",
        "has_key": False,
    }


def weibo_status() -> dict:
    """微博 Cookie 状态（只读 secrets，不触发网络探测）。"""
    cookie = load_cookie("weibo")
    if cookie:
        return {"level": "ok", "text": "✅ 微博 Cookie：已配置", "has_key": True}
    return {
        "level": "warn",
        "text": "⚠️ 微博 Cookie：未配置 —— 选择微博渠道将采集失败",
        "has_key": False,
    }


def node_status() -> dict:
    """Node.js 状态（F-012 联动；PATH + 常见安装位置 + 版本自检）。"""
    version = ""
    try:
        r = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=15
        )
        version = (r.stdout or r.stderr or "").strip()
    except Exception:
        pass
    if health.node_ready():
        return {
            "level": "ok",
            "text": f"✅ Node.js：已安装（{version or '版本未知'}）",
            "has_key": True,
            "version": version,
        }
    return {
        "level": "warn",
        "text": "⚠️ Node.js：未安装 —— 小红书 opencli 无法安装/使用",
        "has_key": False,
        "version": "",
    }


def opencli_status() -> dict:
    """opencli 状态（F-012 联动）。"""
    ok, msg = health.opencli_status()
    return {
        "level": "ok" if ok else "warn",
        "text": ("✅ " if ok else "⚠️ ") + msg,
        "has_key": ok,
    }
