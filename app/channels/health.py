"""渠道诊断（体检 × 一键探针融合，docs/archive/渠道诊断融合方案.md，2026-08-15）。

轻量层：check_channels 并行真实探测已选渠道（WebSearch = 360 单引擎出数探测，
含风控/降级识别，不再以 HTTP 200 为准），并合并系统侧状态（暂停/冷却/配额）。
深度层：WebSearch 三引擎探针仍在 websearch.probe_engines，按需触发。
"""

from __future__ import annotations

import shutil
import os
import re
import subprocess
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




# ---------------------------------------------------------------------------
# opencli 一键安装（2026-08-22，小白友好：应用内按钮直接安装，无需手动命令）
# ---------------------------------------------------------------------------

NPM_MIRROR_REGISTRY = "https://registry.npmmirror.com"
_NODE_CANDIDATES = (
    Path.home() / ".nodejs" / "node.exe",
    Path("C:/Program Files/nodejs/node.exe"),
)
_NPM_CANDIDATES = (
    Path.home() / ".nodejs" / "npm.cmd",
    Path("C:/Program Files/nodejs/npm.cmd"),
)


def node_ready() -> bool:
    """Node.js 是否可用（PATH 或常见安装位置）。"""
    return bool(shutil.which("node") or any(p.exists() for p in _NODE_CANDIDATES))


def opencli_status() -> tuple[bool, str]:
    """opencli 就绪状态：(是否就绪, 说明文案)。"""
    return _opencli_ready()


def _npm_cmd() -> str | None:
    """npm 可执行文件：PATH 优先，回退常见安装位置。"""
    exe = shutil.which("npm")
    if exe:
        return exe
    for p in _NPM_CANDIDATES:
        if p.exists():
            return str(p)
    return None


def _run_streaming(cmd: list[str], timeout_s: int = 600, on_output=None) -> dict:
    """子进程执行并流式收集输出；返回 {ok, message, output}。"""
    out_lines: list[str] = []
    try:
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", **kwargs,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                out_lines.append(line)
                if on_output:
                    on_output(line)
        proc.wait(timeout=timeout_s)
    except Exception as exc:
        return {"ok": False, "message": f"执行失败：{exc}", "output": "\\n".join(out_lines[-30:])}
    tail = "\\n".join(out_lines[-30:])
    if proc.returncode == 0:
        return {"ok": True, "message": "执行完成", "output": tail}
    return {"ok": False, "message": f"执行失败（退出码 {proc.returncode}）", "output": tail}


def install_opencli(use_mirror: bool = True, on_output=None) -> dict:
    """npm 全局安装 opencli（约 1 分钟）。"""
    npm = _npm_cmd()
    if not npm:
        return {
            "ok": False,
            "message": "未检测到 npm：请先安装 Node.js（点上方「安装 Node.js」按钮），"
                       "安装完成后重启应用再试。",
            "output": "",
        }
    cmd = [npm, "install", "-g", "@jackwener/opencli"]
    if use_mirror:
        cmd += ["--registry", NPM_MIRROR_REGISTRY]
    res = _run_streaming(cmd, on_output=on_output)
    if res["ok"]:
        res["message"] = "opencli 安装完成 ✅（请确认 Chrome 已登录 xiaohongshu.com）"
    return res




def install_node(on_output=None) -> dict:
    """按平台一键安装 Node.js：Windows 用 winget（需 Windows 10/11），
    macOS 用 Homebrew（brew install node）。"""
    if os.name == "nt":
        return install_node_winget(on_output=on_output)
    brew = shutil.which("brew")
    if not brew:
        return {
            "ok": False,
            "message": "未找到 Homebrew：请先安装 Node.js（https://nodejs.org 或 brew install node）",
            "output": "",
        }
    res = _run_streaming([brew, "install", "node"], on_output=on_output)
    if res["ok"]:
        res["message"] = "Node.js 安装完成 ✅ 请重启应用（终端重新启动 Streamlit），再点「一键安装 opencli」。已安装则忽略。"
    return res
def _winget_version() -> tuple[int, int]:
    """探测 winget 主版本（如 v1.6.3 -> (1, 6)）；失败返回 (0, 0)。"""
    try:
        winget = shutil.which("winget")
        if not winget:
            return (0, 0)
        out = subprocess.run([winget, "--version"], capture_output=True, text=True, timeout=15)
        m = re.search(r"v?(\d+)\.(\d+)", out.stdout or out.stderr or "")
        if m:
            return (int(m.group(1)), int(m.group(2)))
    except Exception:
        pass
    return (0, 0)


def _classify_winget_error(res: dict) -> str:
    """F-012：按输出文本关键词分类 winget 失败，给出可执行指引（不只按退出码）。"""
    out = (str(res.get("output") or "") + " " + str(res.get("error") or ""))
    lower = out.lower()
    if "\u65e0\u6cd5\u8bc6\u522b\u53c2\u6570\u540d\u79f0" in lower or "unrecognized" in lower or "unknown argument" in lower:
        return "winget \u7248\u672c\u8fc7\u65e7\uff1a\u8bf7\u5230 Microsoft Store \u66f4\u65b0\u300c\u5e94\u7528\u5b89\u88c5\u7a0b\u5e8f\u300d\uff0c\u6216\u5230 https://nodejs.org \u624b\u52a8\u4e0b\u8f7d\u5b89\u88c5\uff08\u5b89\u88c5\u65f6\u52fe\u9009 Add to PATH\uff09"
    if "\u8fde\u63a5" in out or "\u7f51\u7edc" in out or "timed out" in lower or "network" in lower or "source" in lower and "\u4e0d\u53ef\u7528" in out:
        return "\u7f51\u7edc/\u6e90\u4e0d\u53ef\u7528\uff1a\u8bf7\u7a0d\u540e\u91cd\u8bd5\uff0c\u6216\u5230 https://nodejs.org \u624b\u52a8\u4e0b\u8f7d"
    if "\u62d2\u7edd\u8bbf\u95ee" in out or "\u9700\u8981\u7ba1\u7406\u5458" in out or "access is denied" in lower or "admin" in lower:
        return "\u6743\u9650\u4e0d\u8db3\uff1a\u8bf7\u4ee5\u7ba1\u7406\u5458\u8eab\u4efd\u91cd\u65b0\u8fd0\u884c\u540e\u91cd\u8bd5\uff0c\u6216\u624b\u52a8\u5b89\u88c5"
    return "\u5b89\u88c5\u5931\u8d25\uff08\u672a\u77e5\u539f\u56e0\uff09\uff1a\u8bf7\u5230 https://nodejs.org \u624b\u52a8\u4e0b\u8f7d\u5b89\u88c5\uff08\u52fe\u9009 Add to PATH\uff09\uff0c\u6216\u590d\u5236\u4e0b\u65b9\u9519\u8bef\u4fe1\u606f\u56de\u4f20"


def install_node_winget(on_output=None) -> dict:
    """Windows 10/11：winget \u4e00\u952e\u5b89\u88c5 Node.js LTS（F-012\uff1a\u65e7\u7248 winget \u517c\u5bb9\uff09\u3002"""
    winget = shutil.which("winget")
    if not winget:
        return {
            "ok": False,
            "message": "\u672a\u627e\u5230 winget\uff08\u9700\u8981 Windows 10/11\uff09\uff1a\u8bf7\u5230 https://nodejs.org \u624b\u52a8\u4e0b\u8f7d\u5b89\u88c5",
            "output": "",
        }
    major, minor = _winget_version()
    cmd = [winget, "install", "OpenJS.NodeJS.LTS",
           "--accept-source-agreements", "--accept-package-agreements",
           "--silent"]
    # --disable-interactivity 需 winget >= 1.6；旧版不传，避免参数不识别中止
    if (major, minor) >= (1, 6):
        cmd.append("--disable-interactivity")
    res = _run_streaming(cmd, on_output=on_output)
    if res["ok"]:
        # F-012 \u6210\u529f\u81ea\u68c0\uff1a\u786e\u8ba4 node \u53ef\u7528\u5e76\u5199\u5165\u7248\u672c
        try:
            ver = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=15)
            version = (ver.stdout or ver.stderr or "").strip()
        except Exception:
            version = ""
        res["message"] = (
            "Node.js \u5b89\u88c5\u5b8c\u6210 ✅"
            + (f"\uff08{version}\uff09" if version else "")
            + "\uff1a\u8bf7\u91cd\u542f\u5e94\u7528\uff08\u5173\u95ed\u540e\u91cd\u65b0 run.bat\uff09\uff0c\u518d\u70b9\u300c\u5b89\u88c5 opencli\u300d\u3002\u5df2\u5b89\u88c5\u5219\u5ffd\u7565\u3002"
        )
    else:
        res["message"] = _classify_winget_error(res)
    return res
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
