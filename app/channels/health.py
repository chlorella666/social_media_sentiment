"""渠道体检：启动分析前快速检查各渠道可用性（只读探测，不采集数据）。"""

from __future__ import annotations

import shutil
from pathlib import Path

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def check_bilibili() -> tuple[bool, str]:
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        session.get("https://www.bilibili.com/", timeout=8)
        r = session.get(
            "https://api.bilibili.com/x/web-interface/search/type",
            params={"search_type": "video", "keyword": "测试", "page": 1, "page_size": 1},
            timeout=10,
        )
        data = r.json()
        if r.status_code == 200 and data.get("code") == 0:
            return True, "公开搜索 API 正常"
        return False, f"搜索 API 异常 code={data.get('code')}"
    except Exception as exc:
        return False, str(exc)[:60]


def check_websearch() -> tuple[bool, str]:
    try:
        r = requests.get(
            "https://www.so.com/s?q=test",
            headers={"User-Agent": UA},
            timeout=10,
        )
        if r.status_code == 200:
            return True, "360 搜索可达（HTTP 200）"
        return False, f"360 返回 HTTP {r.status_code}"
    except Exception as exc:
        return False, str(exc)[:60]


def check_weibo(cookie: str = "") -> tuple[bool, str]:
    if not cookie:
        return False, "未配置 Cookie（需登录 m.weibo.cn 后复制）"
    try:
        r = requests.get(
            "https://m.weibo.cn/api/container/getIndex",
            params={"containerid": "100103type=1&q=测试", "page_type": "searchall"},
            headers={
                "User-Agent": UA.replace("Safari/537.36", "Mobile Safari/537.36"),
                "Cookie": cookie if cookie.startswith("SUB=") else f"SUB={cookie}",
                "Referer": "https://m.weibo.cn/",
            },
            timeout=10,
        )
        data = r.json()
        if data.get("ok") == 1:
            return True, "登录态有效"
        return False, "Cookie 无效或已过期，请重新粘贴"
    except Exception as exc:
        return False, str(exc)[:60]


def check_xiaohongshu() -> tuple[bool, str]:
    main_js = (
        Path.home()
        / ".nodejs"
        / "node_modules"
        / "@jackwener"
        / "opencli"
        / "dist"
        / "src"
        / "main.js"
    )
    node = shutil.which("node") or str(Path.home() / ".nodejs" / "node.exe")
    if main_js.exists() and Path(node).exists():
        return True, "opencli 就绪（需 Chrome 已登录小红书，体检不主动探测会话）"
    return False, "未安装 opencli/Node（npm install -g @jackwener/opencli）"


def check_channel_health(channel_id: str, params: dict | None = None) -> tuple[bool, str]:
    """返回 (是否可用, 说明)。演示渠道不发起网络探测。"""
    params = params or {}
    if channel_id == "bilibili":
        return check_bilibili()
    if channel_id.startswith("websearch"):
        return check_websearch()
    if channel_id == "weibo":
        return check_weibo(str(params.get("cookie") or ""))
    if channel_id == "xiaohongshu":
        return check_xiaohongshu()
    return True, "演示渠道无需检查"
