"""WebSearch 评论抓取 spike（TapTap / 知乎 · 受控预研）。

依据：docs/WebSearch评论抓取Spike方案.md v1.1（2026-08-20 修订）。

本脚本是独立预研脚本：**不入回归、不改动生产文件**。
- 复用 app/channels/websearch.py 的 build_query / _fetch / USER_AGENTS /
  HARD_RISK_MARKERS 与 app.channels.base.jittered_sleep（只 import）；
- 预检/复检：SQLite **只读**连接读取 jobs 数据（tasks / channel_state /
  worker_heartbeats），不调用任何写库函数，也不扫描 data/state/*.json 猜状态；
- 硬信号即停：CAPTCHA / RATE_LIMIT / ZH_403_ZSE / ZH_403_EMPTY → 停该平台剩余
  请求并落盘 data/state/spike_websearch_stop.json；
- 全天 1 次会话：--smoke 冒烟与正式跑合并计 1 次
  （data/state/spike_websearch_day.json，冒烟完成后当日才允许正式跑）。

用法：
  python tests/spike_websearch_comments.py --smoke            # 冒烟 1/1/5
  python tests/spike_websearch_comments.py                    # 正式跑 3/3/20
  python tests/spike_websearch_comments.py --platform taptap  # 只跑 TapTap
  python tests/spike_websearch_comments.py --clear-stop       # 确认 IP 状态后清除 stop 标记
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

# 独立脚本：确保从任意 cwd 都能导入项目包
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.channels import websearch as ws
from app.channels.base import jittered_sleep
from app.core import jobs as job_api  # 仅用于 db_path() 等只读常量/路径

STATE_DIR = PROJECT_ROOT / "data" / "state"
OUT_ROOT = PROJECT_ROOT / "data" / "datasets" / "spike_websearch_comments"

DAY_FILE = STATE_DIR / "spike_websearch_day.json"
STOP_FILE = STATE_DIR / "spike_websearch_stop.json"
WS_DAY_FILE = STATE_DIR / "websearch_day.json"  # 生产文件，只读

MAX_QUERIES = 3
MAX_POSTS = 3
MAX_COMMENTS = 20
INTERVAL_DEFAULT = 4.5

# 预检覆盖的真实渠道（同家庭 IP 出口）
CHANNEL_IDS = (
    "websearch",
    "websearch_zhihu",
    "websearch_tieba",
    "websearch_taptap",
    "weibo",
    "bilibili",
    "xiaohongshu",
)
TASKS_ACTIVE_STATUSES = ("queued", "running", "reviewing")

TAP_DOMAINS = ("taptap.cn", "taptap.com")
ZH_DOMAINS = ("zhihu.com",)
ENGINE_REFERER = "https://www.so.com/"

HARD_SIGNALS = {"CAPTCHA", "RATE_LIMIT", "ZH_403_ZSE", "ZH_403_EMPTY"}

FILLER_WORDS = {
    "哈哈", "哈哈哈", "不错", "沙发", "顶", "赞", "好", "666", "路过", "支持",
    "感谢", "谢谢", "围观", "看看", "卧槽", "nb", "可以", "牛", "棒", "牛批",
}

_PACE = {"on": False}


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pace(interval: float) -> None:
    """请求间隔：均值 interval ±25% 抖动；会话首个请求不等待。"""
    if _PACE["on"]:
        jittered_sleep(interval, 0.25)
    _PACE["on"] = True


def _log(platform: str, phase: str, url: str, status: Any, signals: list[str]) -> None:
    print(f"{_ts()} | {platform} | {phase} | {url[:120]} | {status} | {signals}")


# ---------------------------------------------------------------------------
# 状态文件（spike 自己的 day/stop 标记）
# ---------------------------------------------------------------------------

def _load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def check_day_gate(smoke: bool) -> tuple[bool, str]:
    """全天 1 次会话：冒烟(--smoke) → 正式跑合并计 1 次。"""
    today = datetime.now().strftime("%Y-%m-%d")
    marker = _load_json(DAY_FILE, {}) or {}
    if marker.get("date") != today:
        return True, "首次运行（或跨日），允许"
    mode = marker.get("mode")
    if mode == "full":
        return False, "今日已运行过正式跑（全天 1 次会话上限），请顺延次日"
    if mode == "smoke":
        if smoke:
            return False, "今日已运行过冒烟；正式跑请去掉 --smoke 后再执行"
        if not marker.get("finished"):
            return False, "今日冒烟未正常结束，先检查上次输出后再重试"
        return True, "今日冒烟已完成，本会话按正式跑继续（合并计 1 次）"
    return True, "允许"


def write_day_marker(mode: str, finished: bool, session_id: str) -> None:
    _write_json(
        DAY_FILE,
        {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "mode": mode,
            "finished": finished,
            "session_id": session_id,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


def stop_marker_for(platform: str) -> dict | None:
    today = datetime.now().strftime("%Y-%m-%d")
    m = _load_json(STOP_FILE, {}) or {}
    if m.get("date") == today and m.get("platform") == platform:
        return m
    return None


def write_stop_marker(platform: str, signal: str, advice: str) -> None:
    payload = {
        "platform": platform,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().isoformat(timespec="seconds"),
        "signal": signal,
        "advice": advice,
    }
    _write_json(STOP_FILE, payload)
    print(f"[stop] 已写 {STOP_FILE}：{platform} {signal}")


# ---------------------------------------------------------------------------
# 预检 / 复检（SQLite 只读，不调用任何写库函数）
# ---------------------------------------------------------------------------

def _ro_connect(path: Path) -> sqlite3.Connection | None:
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def precheck() -> tuple[bool, list[str]]:
    """返回 (是否可运行, 原因列表)。"拒绝"开头的原因为硬阻断，其余为提示。"""
    reasons: list[str] = []
    db_path = job_api.db_path()

    # WebSearch 每日关键词计数（生产文件，只读）：已用 ≥16 → 顺延次日
    wsd = _load_json(WS_DAY_FILE, {}) or {}
    if (
        wsd.get("date") == datetime.now().strftime("%Y-%m-%d")
        and int(wsd.get("used") or 0) >= 16
    ):
        reasons.append(
            f"拒绝：今日 WebSearch 已用 {wsd.get('used')} 个关键词（≥16），spike 顺延次日"
        )

    conn = _ro_connect(db_path)
    if conn is None:
        reasons.append(f"（提示）SQLite 只读连接失败：{db_path}，按无任务/无冷却处理")
        return True, reasons
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "tasks" in tables:
            placeholders = ",".join("?" * len(TASKS_ACTIVE_STATUSES))
            n = conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE status IN ({placeholders})",
                list(TASKS_ACTIVE_STATUSES),
            ).fetchone()[0]
            if n:
                reasons.append(
                    f"拒绝：有 {n} 个进行中/排队任务（{TASKS_ACTIVE_STATUSES}）"
                    "，请 stop_worker.bat 或等任务结束后再跑"
                )
        if "channel_state" in tables:
            now = datetime.now().isoformat(timespec="seconds")
            for row in conn.execute(
                "SELECT channel_id, cool_until, paused, quota_limit, quota_used "
                "FROM channel_state"
            ):
                cid = row["channel_id"]
                if cid not in CHANNEL_IDS:
                    continue
                if row["cool_until"] and str(row["cool_until"]) > now:
                    reasons.append(f"拒绝：渠道 {cid} 冷却中（至 {row['cool_until']}）")
                if row["paused"]:
                    reasons.append(f"拒绝：渠道 {cid} 已暂停")
                ql = row["quota_limit"]
                qu = int(row["quota_used"] or 0)
                if ql is not None and int(ql) >= 0 and qu > int(ql):
                    reasons.append(f"拒绝：渠道 {cid} 配额已超用（{qu}/{ql}）")
        if "worker_heartbeats" in tables:
            now = datetime.now()
            alive = 0
            for row in conn.execute("SELECT heartbeat_at FROM worker_heartbeats"):
                try:
                    if (now - datetime.fromisoformat(row["heartbeat_at"])).total_seconds() <= 60:
                        alive += 1
                except ValueError:
                    continue
            if alive:
                reasons.append(
                    f"（提示）检测到 {alive} 个存活 worker（60s 心跳内）"
                    "，建议 stop_worker.bat 后运行（批次前复检兜底）"
                )
    except sqlite3.Error as exc:
        reasons.append(f"（提示）只读预检异常：{exc}，按放行处理")
    finally:
        conn.close()

    ok = not any(r.startswith("拒绝") for r in reasons)
    return ok, reasons


# ---------------------------------------------------------------------------
# 请求与信号
# ---------------------------------------------------------------------------

def _http_get(
    session: requests.Session,
    url: str,
    referer: str = "",
    extra_headers: dict | None = None,
) -> tuple[str, int, str]:
    """本地请求包装：与生产 _fetch 同源策略（UA 池/超时），允许附加头。

    网络异常重试 1 次（共 2 次尝试）；4xx/风控不重试（由信号表即停）。
    """
    last_error = ""
    for attempt in range(2):
        try:
            hdrs = dict(ws.REQUEST_HEADERS)
            hdrs["User-Agent"] = random.choice(ws.USER_AGENTS)
            if referer:
                hdrs["Referer"] = referer
            if extra_headers:
                hdrs.update(extra_headers)
            resp = session.get(url, headers=hdrs, timeout=ws.REQUEST_TIMEOUT)
            return resp.text, resp.status_code, ""
        except requests.RequestException as exc:
            last_error = str(exc)
        jittered_sleep(2.0 * (attempt + 1), 0.3)
    return "", 0, last_error or "请求失败"


def _detect_signals(status: int, text: str) -> list[str]:
    low = (text or "").lower()
    signals: list[str] = []
    # 限流：429 必硬；"limit" 只在 4xx/5xx 生效（200 页面常含无关 "limit" 字样）；
    # 中文限流短语在任意状态都算（页面级风控特征）。
    if (
        status == 429
        or (status >= 400 and ("频繁" in (text or "") or "limit" in low))
        or any(
            p in (text or "")
            for p in ("访问过于频繁", "请求过于频繁", "操作频繁", "访问频繁")
        )
    ):
        signals.append("RATE_LIMIT")
    if any(m in (text or "") for m in ws.HARD_RISK_MARKERS):
        # 硬验证码只认"异常页面"（非 200 或页面很小 <10KB，历史证据：360 验证码页 3236 字符）；
        # 正常大页面里出现"验证码"字样（登录弹窗 JS 等）降级为软信号，不即停，留人工复核。
        abnormal = status >= 400 or len(text or "") < 10000
        signals.append("CAPTCHA" if abnormal else "SUSPECT_CAPTCHA")
    if status == 403:
        if "invalid zse" in low or "zse" in low:
            signals.append("ZH_403_ZSE")
        elif not (text or "").strip():
            signals.append("ZH_403_EMPTY")
        else:
            signals.append("HTTP_403")
    return signals


def _is_hard(signals: list[str]) -> bool:
    return any(s in HARD_SIGNALS for s in signals)


# ---------------------------------------------------------------------------
# 发现帖子（复用生产搜索引擎设施）
# ---------------------------------------------------------------------------

def _url_score(platform: str, url: str) -> int:
    """选帖偏好：评论页/回答页 > 详情页/提问页 > 主题页等。"""
    if platform == "taptap":
        if "/moment/" in url or "/review/" in url:
            return 2
        if "/app/" in url:
            return 1
        return 0
    if "/answer/" in url:
        return 2
    if "/question/" in url:
        return 1
    return 0


def discovery(
    session: requests.Session,
    platform: str,
    query: str,
    posts_needed: int,
    blocked: set[str],
    meta: dict,
    interval: float,
) -> tuple[list[dict], list[str], list[dict]]:
    domains = TAP_DOMAINS if platform == "taptap" else ZH_DOMAINS
    found_map: dict[str, dict] = {}
    signals: list[str] = []
    page_records: list[dict] = []
    for page in (1, 2):
        if len(found_map) >= posts_needed:
            break
        _pace(interval)
        t0 = datetime.now()
        engine, items, diag = ws._search_engine(session, query, page, blocked)
        elapsed = round((datetime.now() - t0).total_seconds(), 1)
        status = diag[-1]["status"] if diag else 0
        risks = [r for d in diag for r in (d.get("risk") or [])]
        hard = [r for r in risks if r in ws.HARD_RISK_MARKERS]
        _log(platform, "search", query, f"{engine}:HTTP{status}", hard or risks[:2])
        page_records.append(
            {
                "phase": "search",
                "query": query,
                "url": f"{engine or '?'} p{page}",
                "http_status": status if status else (200 if items else 0),
                "first_bytes": 0,
                "risk_signals": hard or [],
                "parsed_items": len(items),
                "comments": [],
                "elapsed_s": elapsed,
                "engine_attempts": len(diag),
            }
        )
        if hard:
            signals.append("CAPTCHA")
            break
        for it in items:
            url = it.get("url") or ""
            if any(dom in url for dom in domains):
                found_map.setdefault(
                    url,
                    {
                        "title": it.get("title", ""),
                        "url": url,
                        "score": _url_score(platform, url),
                    },
                )
    ranked = sorted(found_map.values(), key=lambda x: -x["score"])
    return (
        [{"title": x["title"], "url": x["url"]} for x in ranked][:posts_needed],
        signals,
        page_records,
    )


# ---------------------------------------------------------------------------
# 评论抽取（探测点：多策略兜底，失败也要可诊断）
# ---------------------------------------------------------------------------

def _is_content_key(key: str) -> bool:
    k = key.lower()
    return any(t in k for t in ("content", "text", "body", "message", "comment"))


def _extract_author_time(d: dict) -> tuple[str, str]:
    author, ctime = "", ""
    for k in ("author", "user", "nickname", "nick", "username"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            author = v.strip()
            break
        if isinstance(v, dict):
            for k2 in ("name", "nickname", "nick", "username", "url_token"):
                if isinstance(v.get(k2), str) and v[k2].strip():
                    author = v[k2].strip()
                    break
            if author:
                break
    for k in ("created_time", "create_time", "published_at", "pub_time", "created_at", "time"):
        v = d.get(k)
        if isinstance(v, (str, int, float)):
            ctime = str(v)
            break
    return author, ctime


def _collect_from_obj(obj: Any, out: list[dict], limit: int, depth: int = 0) -> None:
    """递归收集"疑似评论"：content 类字段 + author/time（启发式，探测用）。"""
    if depth > 8 or len(out) >= limit:
        return
    if isinstance(obj, dict):
        content = ""
        for k, v in obj.items():
            if isinstance(v, str) and len(v.strip()) >= 2 and _is_content_key(k):
                content = v.strip()
                break
        if content:
            author, ctime = _extract_author_time(obj)
            out.append({"author": author, "content": content, "time": ctime})
        for v in obj.values():
            _collect_from_obj(v, out, limit, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _collect_from_obj(v, out, limit, depth + 1)


def _json_blobs(text: str) -> list[str]:
    blobs = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
        text,
        re.S,
    ):
        blobs.append(m.group(1))
    for key in ("__NEXT_DATA__", "__INITIAL_STATE__"):
        m = re.search(key + r"\s*=\s*(.*?)</script>", text, re.S)
        if m:
            blobs.append(m.group(1))
    return blobs


_AUTHOR_CLASS_RE = re.compile(
    r'class="[^"]*(?:author|user|nickname|username)[^"]*"[^>]*>(.*?)</', re.S | re.I
)
_CONTENT_CLASS_RE = re.compile(
    r'class="[^"]*(?:content|comment|message|body|text)[^"]*"[^>]*>(.*?)</', re.S | re.I
)
_TIME_CLASS_RE = re.compile(
    r'class="[^"]*(?:time|date)[^"]*"[^>]*>(.*?)</', re.S | re.I
)


def _hint_text(snippet: str, pattern: re.Pattern) -> str:
    m = pattern.search(snippet)
    if not m:
        return ""
    return re.sub(r"<[^>]+>", "", html.unescape(m.group(1))).strip()


def _html_comment_blocks(text: str) -> list[dict]:
    blocks: list[dict] = []
    for li in re.findall(r"<li[^>]*>(.*?)</li>", text, re.S)[:300]:
        author = _hint_text(li, _AUTHOR_CLASS_RE)
        content = _hint_text(li, _CONTENT_CLASS_RE)
        ctime = _hint_text(li, _TIME_CLASS_RE)
        if content and (author or ctime):
            blocks.append({"author": author, "content": content, "time": ctime})
        if len(blocks) >= 40:
            break
    return blocks


def _zh_api_comments(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except Exception:
        return []
    out: list[dict] = []
    for item in (data.get("data") or []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        content = (item.get("content") or "").strip()
        if not content:
            continue
        author, ctime = _extract_author_time(item)
        out.append(
            {
                "author": author,
                "content": content,
                "time": ctime,
                "id": str(item.get("id") or ""),
            }
        )
    return out


def _extract_comments(text: str, platform: str, endpoint: str) -> list[dict]:
    comments: list[dict] = []
    if platform == "zhihu" and endpoint == "api":
        comments = _zh_api_comments(text)
    if not comments:
        for blob in _json_blobs(text):
            try:
                raw = json.loads(blob)
            except Exception:
                continue
            _collect_from_obj(raw, comments, 40)
    if not comments:
        comments = _html_comment_blocks(text)
    seen: set[str] = set()
    out: list[dict] = []
    for c in comments:
        content = (c.get("content") or "").strip()
        if not content or content in seen:
            continue
        seen.add(content)
        out.append(
            {
                "author": (c.get("author") or "").strip(),
                "content": content,
                "time": str(c.get("time") or ""),
                "id": str(c.get("id") or ""),
            }
        )
    return out


def _noise_hint(content: str) -> bool:
    c = content.strip()
    if len(c) <= 4:
        return True
    if c in FILLER_WORDS:
        return True
    if re.fullmatch(r"(.)\1{2,}", c):
        return True
    return False


# ---------------------------------------------------------------------------
# 平台执行
# ---------------------------------------------------------------------------

def _build_queries(platform: str, brand: str) -> list[str]:
    hint = "TapTap" if platform == "taptap" else "知乎"
    return [
        ws.build_query(brand, hint=hint),
        ws.build_query(f"{brand} 评价", hint=hint),
        ws.build_query(f"{brand} 怎么样", hint=hint),
    ]


def _taptap_app_id(url: str) -> str | None:
    m = re.search(r"/app/(\d+)", url)
    return m.group(1) if m else None


def run_platform(
    session: requests.Session,
    platform: str,
    cfg: dict,
    meta: dict,
    session_id: str,
) -> dict:
    """执行单平台探测；返回平台报告 dict（部分结果在硬信号时照常保留）。"""
    queries = _build_queries(platform, cfg["brand"])[: cfg["queries_per_platform"]]
    planned = (
        cfg["queries_per_platform"]
        * cfg["posts_per_query"]
        * cfg["comments_per_post"]
    )
    req_log: list[dict] = []
    all_comments: list[dict] = []
    posts_attempted = 0
    stop_signal = None
    blocked: set[str] = set()
    seq = 0
    empty_streak = 0
    http_actual = 0

    def bump(req: dict) -> None:
        nonlocal seq, empty_streak
        seq += 1
        req["seq"] = seq
        req_log.append(req)
        if req["http_status"] < 400 and req["parsed_items"] > 0:
            empty_streak = 0
        else:
            empty_streak += 1
        cap = cfg["request_cap_per_platform"]
        if len(req_log) > cap:
            raise RuntimeError(
                f"{platform} 请求数超上限（> {cap}），脚本终止：疑似失控"
            )

    for query in queries:
        if stop_signal:
            break
        posts, sigs, page_records = discovery(
            session, platform, query, cfg["posts_per_query"], blocked, meta, cfg["interval"]
        )
        for rec in page_records:
            meta["requests"] += 1
            http_actual += rec.get("engine_attempts", 1)
            bump(rec)
            if http_actual > 2 * cfg["request_cap_per_platform"]:
                raise RuntimeError(
                    f"{platform} 实际 HTTP 请求超预算（> {2 * cfg['request_cap_per_platform']}），"
                    "疑似引擎兜底失控，脚本终止"
                )
        if sigs and _is_hard(sigs):
            stop_signal = sigs[0]
            write_stop_marker(
                platform, stop_signal, "搜索引擎验证码即停：当日不再重试该平台"
            )
            break
        if not posts:
            if empty_streak >= 2:
                req_log[-1].setdefault("risk_signals", []).append("EMPTY_2X")
            continue
        for post in posts:
            if stop_signal:
                break
            url = post["url"]
            _pace(cfg["interval"])
            t0 = datetime.now()
            text, status, err = _http_get(session, url, referer=ENGINE_REFERER)
            elapsed = round((datetime.now() - t0).total_seconds(), 1)
            signals = _detect_signals(status, text)
            comments = (
                _extract_comments(text, platform, "html") if status < 400 else []
            )
            meta["requests"] += 1
            http_actual += 1
            _log(platform, "detail", url, status, signals or (["NET_ERR"] if err else []))
            bump(
                {
                    "phase": "detail",
                    "query": query,
                    "url": url,
                    "http_status": status,
                    "first_bytes": len((text or "")[:200]),
                    "risk_signals": signals,
                    "parsed_items": len(comments),
                    "comments": comments[: cfg["comments_per_post"]],
                    "elapsed_s": elapsed,
                    "snippet": (
                        _sanitize_snippet(err or text, 200)
                        if status >= 400 or err or signals
                        else ""
                    ),
                }
            )
            if _is_hard(signals):
                stop_signal = signals[0]
                write_stop_marker(
                    platform,
                    stop_signal,
                    f"{platform} 命中硬信号即停：当日不再重试该平台",
                )
                break
            if status < 400:
                posts_attempted += 1
                all_comments.extend(comments)

            # TapTap 备用端点：webapiv2（仅当 HTML 没解析出评论且 URL 含 app_id）
            if platform == "taptap" and not comments and status < 400:
                app_id = _taptap_app_id(url)
                if app_id:
                    api_url = (
                        "https://www.taptap.com/webapiv2/review/v2/by-app"
                        f"?app_id={app_id}"
                    )
                    _pace(cfg["interval"])
                    t0 = datetime.now()
                    jtext, jstatus, jerr = _http_get(
                        session,
                        api_url,
                        referer=url,
                        extra_headers={
                            "X-UA": "V=1&PN=WebApp&VN=2.0.0",
                            "X-Requested-With": "XMLHttpRequest",
                        },
                    )
                    elapsed = round((datetime.now() - t0).total_seconds(), 1)
                    jsignals = _detect_signals(jstatus, jtext)
                    jcomments = (
                        _extract_comments(jtext, platform, "json") if jstatus < 400 else []
                    )
                    meta["requests"] += 1
                    http_actual += 1
                    _log(platform, "api", api_url, jstatus, jsignals or (["NET_ERR"] if jerr else []))
                    bump(
                        {
                            "phase": "api",
                            "query": query,
                            "url": api_url,
                            "http_status": jstatus,
                            "first_bytes": len((jtext or "")[:200]),
                            "risk_signals": jsignals,
                            "parsed_items": len(jcomments),
                            "comments": jcomments[: cfg["comments_per_post"]],
                            "elapsed_s": elapsed,
                            "snippet": (
                                _sanitize_snippet(jerr or jtext, 200)
                                if jstatus >= 400 or jerr or jsignals
                                else ""
                            ),
                        }
                    )
                    if _is_hard(jsignals):
                        stop_signal = jsignals[0]
                        write_stop_marker(
                            platform, stop_signal, "TapTap webapiv2 命中硬信号即停"
                        )
                        break
                    if jcomments:
                        posts_attempted += 1
                        all_comments.extend(jcomments)

            # 知乎备用：根评论 API（仅当 URL 是回答页且匿名预期 403 即停）
            if platform == "zhihu" and status < 400 and not stop_signal:
                aid_m = re.search(r"/answer/(\d+)", url)
                if aid_m and (not comments or cfg["allow_html"]):
                    api_url = (
                        "https://www.zhihu.com/api/v4/answers/"
                        f"{aid_m.group(1)}/root_comments?limit=20&offset=0"
                    )
                    _pace(cfg["interval"])
                    t0 = datetime.now()
                    ztext, zstatus, zerr = _http_get(
                        session,
                        api_url,
                        referer=url,
                        extra_headers={"x-requested-with": "fetch"},
                    )
                    elapsed = round((datetime.now() - t0).total_seconds(), 1)
                    zsignals = _detect_signals(zstatus, ztext)
                    zcomments = (
                        _extract_comments(ztext, "zhihu", "api") if zstatus < 400 else []
                    )
                    meta["requests"] += 1
                    http_actual += 1
                    _log(platform, "api", api_url, zstatus, zsignals or (["NET_ERR"] if zerr else []))
                    bump(
                        {
                            "phase": "api",
                            "query": query,
                            "url": api_url,
                            "http_status": zstatus,
                            "first_bytes": len((ztext or "")[:200]),
                            "risk_signals": zsignals,
                            "parsed_items": len(zcomments),
                            "comments": zcomments[: cfg["comments_per_post"]],
                            "elapsed_s": elapsed,
                            "snippet": (
                                _sanitize_snippet(zerr or ztext, 200)
                                if zstatus >= 400 or zerr or zsignals
                                else ""
                            ),
                        }
                    )
                    if _is_hard(zsignals):
                        stop_signal = zsignals[0]
                        write_stop_marker(
                            platform,
                            stop_signal,
                            "知乎 403 签名墙（需登录态）：即停并如实记录，不突破",
                        )
                        break
                    if zcomments:
                        posts_attempted += 1
                        all_comments.extend(zcomments)
        # EMPTY_2X：连续 2 次空结果 → 停该查询，跳到下一查询
        if empty_streak >= 2 and req_log:
            req_log[-1].setdefault("risk_signals", []).append("EMPTY_2X")
            empty_streak = 0

    return _build_platform_report(
        platform,
        queries,
        req_log,
        all_comments,
        posts_attempted,
        planned,
        stop_signal,
    )


def _build_platform_report(
    platform: str,
    queries: list[str],
    req_log: list[dict],
    comments: list[dict],
    posts_attempted: int,
    planned: int,
    stop_signal: str | None,
) -> dict:
    total = len(req_log)
    success = sum(
        1
        for r in req_log
        if r["http_status"] < 400 and r["parsed_items"] > 0
    )
    hard = sum(1 for r in req_log if _is_hard(r.get("risk_signals") or []))
    dedup: list[dict] = []
    seen: set[str] = set()
    for c in comments:
        content = (c.get("content") or "").strip()
        if not content or content in seen:
            continue
        seen.add(content)
        dedup.append(c)

    request_success_rate = round(success / total, 4) if total else 0.0
    parse_success_rate = (
        round(
            sum(
                1
                for r in req_log
                if r.get("phase") in ("detail", "api") and r.get("parsed_items") > 0
            )
            / max(1, posts_attempted),
            4,
        )
        if posts_attempted
        else 0.0
    )
    comment_completeness = round(len(dedup) / planned, 4) if planned else 0.0
    hard_signal_rate = round(hard / total, 4) if total else 0.0

    if platform == "zhihu" and stop_signal in ("ZH_403_ZSE", "ZH_403_EMPTY"):
        conclusion = "需登录态"
    elif stop_signal == "CAPTCHA" or comment_completeness < 0.5:
        conclusion = "no-go"
    elif stop_signal == "RATE_LIMIT" or 0.5 <= comment_completeness < 0.8:
        conclusion = "有条件"
    elif comment_completeness >= 0.8 and hard_signal_rate == 0.0:
        conclusion = "go（谨慎）" if platform == "zhihu" else "go"
    else:
        conclusion = "有条件"

    samples = dedup[:20]
    noise_flagged = sum(1 for c in samples if _noise_hint(c.get("content") or ""))
    return {
        "platform": platform,
        "queries": queries,
        "requests_total": total,
        "request_success_rate": request_success_rate,
        "parse_success_rate": parse_success_rate,
        "comment_completeness": comment_completeness,
        "hard_signal_rate": hard_signal_rate,
        "stop_signal": stop_signal,
        "conclusion": conclusion,
        "comments_total_dedup": len(dedup),
        "info_quality_samples": samples,
        "info_quality_noise_flagged": noise_flagged,
        "requests": req_log,
    }


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------

def _sanitize_snippet(text: str, limit: int = 200) -> str:
    text = re.sub(r"<[^>]+>", " ", (text or "")).strip()
    return " ".join(text.split())[:limit]


def _write_reports(
    session_id: str,
    cli_args: dict,
    platforms: list[dict],
    findings: list[str],
) -> Path:
    out_dir = OUT_ROOT / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "spike": {
            "id": session_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "script": "tests/spike_websearch_comments.py",
            "cli_args": cli_args,
        },
        "platforms": platforms,
        "findings": findings,
        "conclusion": "，".join(
            f"{p['platform']}={p['conclusion']}" for p in platforms
        ),
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines: list[str] = [
        f"# WebSearch 评论抓取 spike 报告（{session_id}）",
        "",
        f"结论：{report['conclusion']}",
        "",
    ]
    for p in platforms:
        lines += [
            f"## {p['platform']}",
            f"- 结论：{p['conclusion']} ｜ 停因：{p.get('stop_signal') or '无'}",
            (
                "- 指标：请求 {requests_total} 次，成功率 {request_success_rate}，"
                "解析率 {parse_success_rate}，完整率 {comment_completeness}，"
                "硬信号率 {hard_signal_rate}（去重评论 {comments_total_dedup} 条）"
            ).format(**p),
            "",
            "### 信号时间线",
            "",
            "| 序号 | 阶段 | HTTP | 信号 | 解析条数 | URL |",
            "|---|---|---|---|---|---|",
        ]
        for r in p["requests"]:
            signals = "/".join(r.get("risk_signals") or []) or "-"
            lines.append(
                f"| {r['seq']} | {r['phase']} | {r['http_status']} | {signals} "
                f"| {r['parsed_items']} | {r['url'][:80]} |"
            )
        lines += ["", "### 样本（TapTap 单帖 3~5 条 + 信息量抽样 ≤20 条）", ""]
        for i, c in enumerate(p.get("info_quality_samples") or [], 1):
            flag = "⚠️可能噪音" if _noise_hint(c.get("content") or "") else ""
            lines.append(
                f"{i}. [{c.get('author') or '匿名'}][{c.get('time') or '?'}] "
                f"{c.get('content')[:120]} {flag}"
            )
        lines.append("")
        lines.append(
            f"- 信息量启发式：{p.get('info_quality_noise_flagged')}/{len(p.get('info_quality_samples') or [])} "
            "条疑似无信息量（参考，人工复核为准）"
        )
        lines += ["", "### 失败响应前 200 字符（脱敏）", ""]
        for r in p["requests"]:
            if r["http_status"] >= 400 or r.get("risk_signals") or r.get("snippet"):
                lines.append(
                    f"- {r['phase']} HTTP{r['http_status']}：{r.get('snippet') or '（无响应体）'}"
                )
        lines.append("")
    lines += ["## findings", ""]
    lines += [f"- {f}" for f in findings]
    lines += [
        "",
        "## 人工验收提示",
        "",
        "- 信息量比例 = 有信息量评论 / 抽样评论（人工在样本上判定），参考线 ≥60%；",
        "- Go 仅指技术可行：正式立项须含冷却进渠道层、评论清洗过滤、4.5 合规复核；",
        "- stop 标记见 data/state/spike_websearch_stop.json（确认 IP 状态后 --clear-stop 清除）。",
    ]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return out_dir


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="WebSearch 评论抓取 spike（受控预研）")
    ap.add_argument("--smoke", action="store_true", help="冒烟模式：1 查询/1 帖/5 评论")
    ap.add_argument(
        "--platform",
        choices=("taptap", "zhihu", "all"),
        default="all",
        help="只跑单平台或全部（默认 all）",
    )
    ap.add_argument("--brand", default="恋与深空", help="分析品牌/产品名")
    ap.add_argument("--queries-per-platform", type=int, default=MAX_QUERIES)
    ap.add_argument("--posts-per-query", type=int, default=MAX_POSTS)
    ap.add_argument("--comments-per-post", type=int, default=MAX_COMMENTS)
    ap.add_argument("--interval", type=float, default=INTERVAL_DEFAULT)
    ap.add_argument("--allow-html", action="store_true", help="知乎意外匿名成功时授权 HTML 备选端点")
    ap.add_argument("--clear-stop", action="store_true", help="确认 IP 状态后清除 stop 标记")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.clear_stop:
        if STOP_FILE.exists():
            STOP_FILE.unlink()
            print(f"已清除 {STOP_FILE}")
        else:
            print("无 stop 标记可清除")
        return 0

    smoke = args.smoke
    ok, reasons = precheck()
    for r in reasons:
        print(f"[precheck] {r}")
    if not ok:
        print("预检未通过，拒绝运行（详见上方原因）")
        return 2

    gate_ok, gate_msg = check_day_gate(smoke)
    print(f"[day-gate] {gate_msg}")
    if not gate_ok:
        return 2

    q = 1 if smoke else min(args.queries_per_platform, MAX_QUERIES)
    p = 1 if smoke else min(args.posts_per_query, MAX_POSTS)
    c = 5 if smoke else min(args.comments_per_post, MAX_COMMENTS)
    interval = max(1.0, args.interval)
    if not smoke and (q, p, c) != (args.queries_per_platform, args.posts_per_query, args.comments_per_post):
        print(f"[cap] 参数超出上限，已钳制为 {q}/{p}/{c}（上限 3/3/20）")
    if smoke and (q, p, c) != (1, 1, 5):
        print("[cap] 冒烟模式固定 1 查询/1 帖/5 评论")

    cfg = {
        "brand": args.brand,
        "queries_per_platform": q,
        "posts_per_query": p,
        "comments_per_post": c,
        "interval": interval,
        "request_cap_per_platform": 2 * q * (1 + p),  # §2.1：搜索 ≤2q + 详情/API ≤2qp
        "allow_html": args.allow_html,
    }
    session_id = f"spike_websearch_comments_{datetime.now().strftime('%Y%m%d_%H%M')}"
    mode = "smoke" if smoke else "full"
    write_day_marker(mode, False, session_id)
    print(f"[session] {session_id} mode={mode} platform={args.platform}")

    session = requests.Session()
    meta = {"requests": 0}
    platforms_order = ["taptap", "zhihu"] if args.platform == "all" else [args.platform]
    platform_reports: list[dict] = []
    findings: list[str] = []
    try:
        for platform in platforms_order:
            print(f"\n=== 平台批次开始：{platform} ===")
            # 批次前复检（TOCTOU 兜底）
            rok, rreasons = precheck()
            for r in rreasons:
                print(f"[recheck] {r}")
            if not rok:
                print("批次前复检未通过，停止后续平台")
                findings.append(f"{platform} 批次前复检未通过，未执行")
                break
            marker = stop_marker_for(platform)
            if marker:
                print(f"[skip] {platform} 今日已有 stop 标记（{marker.get('signal')}），跳过")
                findings.append(f"{platform} 因 stop 标记跳过：{marker.get('signal')}")
                continue
            rep = run_platform(session, platform, cfg, meta, session_id)
            platform_reports.append(rep)
            print(
                f"[{platform}] 请求 {rep['requests_total']} 次，"
                f"完整率 {rep['comment_completeness']}，硬信号率 {rep['hard_signal_rate']}，"
                f"结论={rep['conclusion']}"
            )
            if rep["stop_signal"]:
                findings.append(
                    f"{platform} 命中 {rep['stop_signal']} 即停（当日不再重试该平台）"
                )
    except KeyboardInterrupt:
        print("\n用户中断：已抓部分已保留，day 标记置为未完成")
        write_day_marker(mode, False, session_id)
        return 130
    except RuntimeError as exc:
        print(f"[abort] {exc}")
        write_day_marker(mode, False, session_id)
        return 3
    finally:
        session.close()

    if platform_reports:
        write_day_marker(mode, True, session_id)
    else:
        write_day_marker(mode, False, session_id)
        print("无平台报告产出（可能全部被 stop 标记/复检拦截），day 标记保持未完成")
        return 4

    findings.append(
        f"总请求 {meta['requests']} 次（≤ {len(platform_reports) * cfg['request_cap_per_platform']} 上限）"
    )
    out_dir = _write_reports(session_id, vars(args), platform_reports, findings)
    print(f"\n产出：{out_dir}")
    print(f"结论：{_load_json(out_dir / 'report.json').get('conclusion')}")
    print("提示：信息量比例需人工在 report.md 样本上评估；正式立项须满足 §八 前置清单。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
