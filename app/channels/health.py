"""渠道诊断（体检 × 一键探针融合，docs/渠道诊断融合方案.md，2026-08-15）。

轻量层：check_channels 并行真实探测已选渠道（WebSearch = 360 单引擎出数探测，
含风控/降级识别，不再以 HTTP 200 为准），并合并系统侧状态（暂停/冷却/配额）。
深度层：WebSearch 三引擎探针仍在 websearch.probe_engines，按需触发。
"""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from app.channels.websearch import lightweight_probe
from app.core import jobs

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

LEVELS = ("ok", "warn", "error")


def _allowed_rich(channel_id: str) -> dict:
    """系统侧状态：暂停/冷却/配额，合并成诊断说明与徽标判定。"""
    ok, reason = jobs.check_channel_allowed(channel_id)
    s = jobs.channel_state(channel_id)
    limit = int(s.get("quota_limit") or -1)
    if not ok:
        # 受阻时以 check_channel_allowed 的权威原因（暂停/冷却/配额不足）为准
        text = reason or "系统不可用"
    else:
        parts: list[str] = []
        if limit >= 0:
            parts.append(f"今日配额 {s.get('quota_used', 0)}/{limit}")
        else:
            parts.append("未限配额")
        if s.get("paused"):
            parts.append("已暂停")
        if s.get("cool_until"):
            parts.append(f"冷却中（至 {s['cool_until']}）")
        text = " · ".join(parts)
    return {
        "ok": ok,
        "text": text,
        "reason": reason or "",
    }


def check_bilibili_rich() -> dict:
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
            return {"ok": True, "level": "ok", "msg": "公开搜索 API 正常", "detail": {}}
        return {"ok": False, "level": "error",
                "msg": f"搜索 API 异常 code={data.get('code')}", "detail": {}}
    except Exception as exc:
        return {"ok": False, "level": "error", "msg": str(exc)[:60], "detail": {}}


def check_websearch_rich(query: str = "测试 评价") -> dict:
    """WebSearch 体检：360 主引擎真实出数探测（含风控/降级识别）。"""
    p = lightweight_probe(query)
    st = p["status"]
    if st == "ok":
        return {"ok": True, "level": "ok",
                "msg": f"360 主引擎可达（解析 {p['items']} 条，质量通过）", "detail": p}
    if st == "risk":
        return {"ok": False, "level": "error",
                "msg": f"命中风控（{p['message']}），判定不可用；"
                       "建议换网络/代理或暂不勾选 WebSearch", "detail": p}
    if st == "error":
        return {"ok": False, "level": "error", "msg": p["message"], "detail": p}
    if st == "degraded":
        return {"ok": False, "level": "warn",
                "msg": "可达但结果与查询无关（疑似降级页），建议深度探针确认", "detail": p}
    return {"ok": False, "level": "warn",
            "msg": "360 主引擎无结果，建议深度探针确认", "detail": p}


def check_weibo_rich(cookie: str = "") -> dict:
    if not cookie:
        return {"ok": False, "level": "warn",
                "msg": "未配置 Cookie（需登录 m.weibo.cn 后复制，到侧边栏保存）", "detail": {}}
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
            return {"ok": True, "level": "ok", "msg": "登录态有效", "detail": {}}
        return {"ok": False, "level": "error",
                "msg": "Cookie 无效或已过期，请重新粘贴", "detail": {}}
    except Exception as exc:
        return {"ok": False, "level": "error", "msg": str(exc)[:60], "detail": {}}


def _opencli_ready() -> tuple[bool, str]:
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
        return True, "opencli 就绪（需 Chrome 已登录小红书；体检不主动探测会话，避免触发验证码）"
    return False, "未安装 opencli/Node（npm install -g @jackwener/opencli）"


def check_xiaohongshu_rich() -> dict:
    ok, msg = _opencli_ready()
    return {"ok": ok, "level": "ok" if ok else "warn", "msg": msg, "detail": {}}


def check_channel_rich(channel_id: str, params: dict | None = None) -> dict:
    """单渠道诊断（统一返回 {ok, level, msg, detail}）；demo 不发起网络探测。"""
    params = params or {}
    if channel_id == "bilibili":
        return check_bilibili_rich()
    if channel_id.startswith("websearch"):
        return check_websearch_rich(str(params.get("query") or "测试 评价"))
    if channel_id == "weibo":
        return check_weibo_rich(str(params.get("cookie") or ""))
    if channel_id == "xiaohongshu":
        return check_xiaohongshu_rich()
    return {"ok": True, "level": "ok", "msg": "演示渠道无需检查", "detail": {}}


def check_channels(channels: list[str], params: dict | None = None) -> dict[str, dict]:
    """轻量层：并行探测已选渠道（max_workers=4）并合并系统侧状态。"""
    params = params or {}

    def one(cid: str) -> tuple[str, dict]:
        rich = check_channel_rich(cid, params)
        rich["system"] = _allowed_rich(cid) if cid != "demo" else {
            "ok": True, "text": "无需检查", "reason": "",
        }
        return cid, rich

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for cid, rich in ex.map(one, channels):
            out[cid] = rich
    return out


# ---------------------------------------------------------------------------
# 兼容层：旧 tuple 接口（UI 面板已改用 check_channels / check_channel_rich）
# ---------------------------------------------------------------------------

def check_bilibili() -> tuple[bool, str]:
    r = check_bilibili_rich()
    return r["ok"], r["msg"]


def check_websearch(query: str = "测试 评价") -> tuple[bool, str]:
    r = check_websearch_rich(query)
    return r["ok"], r["msg"]


def check_weibo(cookie: str = "") -> tuple[bool, str]:
    r = check_weibo_rich(cookie)
    return r["ok"], r["msg"]


def check_xiaohongshu() -> tuple[bool, str]:
    r = check_xiaohongshu_rich()
    return r["ok"], r["msg"]


def check_channel_health(channel_id: str, params: dict | None = None) -> tuple[bool, str]:
    """旧签名兼容：返回 (是否可用, 说明)。"""
    r = check_channel_rich(channel_id, params)
    return r["ok"], r["msg"]
