"""WebSearch 渠道（V1 实现：360 搜索主引擎 + cn.bing 兜底，零登录）。

定位：全品类兜底渠道 + 知乎/贴吧/TapTap 子渠道。360 对中文与社区内容
覆盖好、无需登录；cn.bing 直连作为备用。每个关键词做质量校验（结果必须
含关键词主题词），避免搜索引擎降级页污染样本。

子渠道通过查询词提示（如"关键词 知乎"）+ 域名过滤实现，实测命中率高：
知乎 7/7、贴吧 5/7、TapTap 5/7（2026-08-05）。

实现说明（实测 2026-08-04）：Jina Reader 免费通道存在缓存串号与 403 限流，
已弃用；sogou 连续请求触发反爬；Baidu 直连触发验证码。
"""

from __future__ import annotations

import html
import json
import os
import random
import re
import time
import urllib.parse
from datetime import date
from pathlib import Path
from threading import Event

import requests

from app.channels.base import (
    ChannelAdapter,
    ProgressCallback,
    degraded_result,
    jittered_sleep,
)
from app.core.keyword_effects import load_keyword_strategy
from app.core.models import AnalysisPlan, ChannelResult, Post

SO_URL = "https://www.so.com/s"
BING_URL = "https://cn.bing.com/search"
QUARK_URL = "https://quark.sm.cn/s"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]
REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
REQUEST_TIMEOUT = 25
REQUEST_INTERVAL = 4.5  # 节流均值（±25% 抖动防固定节奏；2026-08-13 由 1.5→2.5→4.5s 逐级降频）
MAX_PAGES = 2
MAX_RETRIES = 2
# 单查询可获取量实测约 5~13 条；产出封顶，平衡收益与反爬风险（加量靠策略加词）
MAX_LIMIT = 13
# 风控特征词：解析为空/异常时用于识别验证码、限流、无结果等降级页
RISK_MARKERS = (
    "验证码", "安全验证", "captcha", "访问过于频繁", "操作频繁",
    "请输入验证码", "滑动验证", "请求过于频繁", "无结果",
)
# 硬风控特征：命中即"验证码即停"——不再请求后续引擎、不再重试
HARD_RISK_MARKERS = ("验证码", "安全验证", "captcha", "滑动验证", "请输入验证码")
# 空结果关键词重试：等待区间（秒）与重试次数；连续空结果阈值（判定风控）
EMPTY_RETRY_DELAY = (10.0, 20.0)
EMPTY_RETRY_TIMES = 1
EMPTY_RISK_THRESHOLD = 2
# 每日 WebSearch 关键词总量上限（含子渠道展开），超限拦截防触发风控
DAILY_KEYWORD_MAX = 24
_ENGINE_ORDER = ("360", "cn.bing", "quark")

SO_BLOCK_RE = re.compile(r'<li class="res-list[^"]*".*?</li>', flags=re.S)
SO_ITEM_RE = re.compile(
    r'<h3[^>]*>\s*<a[^>]+href="[^"]*"[^>]*data-mdurl="([^"]+)"[^>]*>(.*?)</a>',
    flags=re.S,
)
SO_SNIPPET_RE = re.compile(
    r'<span class="res-list-summary">(.*?)</span>', flags=re.S
)

BING_BLOCK_RE = re.compile(r'<li class="b_algo".*?</li>', flags=re.S)
BING_ITEM_RE = re.compile(
    r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', flags=re.S
)
BING_SNIPPET_RE = re.compile(r"<p[^>]*>(.*?)</p>", flags=re.S)

# 子渠道定义：查询提示词 + 目标域名（域名任一命中即保留）
TARGETS: dict[str, dict] = {
    "": {
        "id": "websearch",
        "name": "WebSearch 全网",
        "hint": "",
        "domains": (),
        "applicability": "全品类，零登录；覆盖新闻/论坛/App 商店",
        "description": "360 搜索 + cn.bing 兜底聚合全网内容",
    },
    "zhihu": {
        "id": "websearch_zhihu",
        "name": "WebSearch 知乎",
        "hint": "知乎",
        "domains": ("zhihu.com",),
        "applicability": "知乎深度讨论/竞品对比（零登录）",
        "description": "360 搜索限定知乎内容（关键词+知乎）",
    },
    "tieba": {
        "id": "websearch_tieba",
        "name": "WebSearch 贴吧",
        "hint": "贴吧",
        "domains": ("tieba.baidu.com", "tieba.com"),
        "applicability": "百度贴吧社区讨论（零登录）",
        "description": "360 搜索限定贴吧内容（关键词+贴吧）",
    },
    "taptap": {
        "id": "websearch_taptap",
        "name": "WebSearch TapTap",
        "hint": "TapTap",
        "domains": ("taptap.cn", "taptap.com"),
        "applicability": "TapTap 游戏评分/论坛（零登录）",
        "description": "360 搜索限定 TapTap 内容（关键词+TapTap）",
    },
}

SUFFIX_WORDS = ("评价", "怎么样", "吐槽", "测评")


def _pick_suffix(strategy: dict, query: str) -> str | None:
    """按策略选后缀：先 prefer_suffix 再 suffix_pool；默认保持「评价」。"""
    pool = strategy.get("suffix_pool") or ["评价"]
    rules = (strategy.get("rules") or {}).get("websearch", {}) or {}
    prefer = rules.get("prefer_suffix") or []
    for s in list(prefer) + list(pool):
        s = str(s).strip()
        if s and s not in query:
            return s
    return None


def build_query(
    keyword: str,
    hint: str = "",
    eval_suffix: bool = True,
    strategy: dict | None = None,
) -> str:
    """构建实际查询串：子渠道提示词 + 效果策略后缀（2.2）。

    规则保持不变：子渠道不加后缀；已有评价类词不再叠加；策略缺失时
    回退到原「评价」后缀行为，保证旧行为不回归。
    """
    strategy = strategy or {}
    query = f"{keyword} {hint}".strip() if hint else keyword
    if eval_suffix and not hint and not any(k in query for k in SUFFIX_WORDS):
        suffix = _pick_suffix(strategy, query)
        if suffix:
            query = f"{query} {suffix}".strip()
    return query


def _clean(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _parse_360(html_text: str) -> list[dict]:
    results: list[dict] = []
    for block in SO_BLOCK_RE.findall(html_text):
        m = SO_ITEM_RE.search(block)
        if not m:
            continue
        url = m.group(1)
        if not url or url.startswith("//"):
            url = "https:" + url if url else ""
        if "so.com/link" in url or not url:
            continue
        title = _clean(m.group(2))
        if not title:
            continue
        snip = SO_SNIPPET_RE.search(block)
        snippet = _clean(snip.group(1)) if snip else ""
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


def _parse_bing(html_text: str) -> list[dict]:
    results: list[dict] = []
    for block in BING_BLOCK_RE.findall(html_text):
        m = BING_ITEM_RE.search(block)
        if not m:
            continue
        url = html.unescape(m.group(1))
        if url.startswith("//"):
            url = "https:" + url
        if "bing.com" in url or "go.microso" in url or not url:
            continue
        title = _clean(m.group(2))
        if not title:
            continue
        snip = BING_SNIPPET_RE.search(block)
        snippet = _clean(snip.group(1)) if snip else ""
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


def _quality_check(keyword: str, results: list[dict]) -> bool:
    """结果必须包含关键词中的主题词（最长词），防止降级页/无关结果。"""
    tokens = [t for t in keyword.split() if not t.startswith("site:")]
    if not tokens:
        return bool(results)
    core = max(tokens, key=len)
    return any(core in r["title"] or core in r["url"] for r in results)


def _fetch(
    session: requests.Session,
    url: str,
    referer: str = "",
) -> tuple[str, int, str]:
    """返回 (页面文本, HTTP 状态码, 错误信息)；网络异常不抛出，由调用方决定降级。

    每次请求随机 UA + Referer/Accept-Language 伪装，降低被搜索引擎反爬的概率。
    """
    last_error = ""
    for attempt in range(MAX_RETRIES):
        try:
            hdrs = dict(REQUEST_HEADERS)
            hdrs["User-Agent"] = random.choice(USER_AGENTS)
            if referer:
                hdrs["Referer"] = referer
            resp = session.get(url, headers=hdrs, timeout=REQUEST_TIMEOUT)
            return resp.text, resp.status_code, ""
        except requests.RequestException as exc:
            last_error = str(exc)
        jittered_sleep(2.0 * (attempt + 1), 0.3)
    return "", 0, last_error or "搜索请求失败"


def _risk_info(text: str) -> list[str]:
    """从页面/错误文本识别风控特征（验证码/限流/无结果等）。"""
    low = (text or "").lower()
    return [m for m in RISK_MARKERS if m.lower() in low]


def _is_hard_risk(risk: list[str]) -> bool:
    """硬风控（验证码/安全验证等）：命中即停，不再请求后续引擎。"""
    return any(m in risk for m in HARD_RISK_MARKERS)


def _diag_has_hard_risk(diag: list[dict]) -> bool:
    return any(_is_hard_risk(d.get("risk") or []) for d in diag)


def _diag_compact(diag: list[dict]) -> str:
    """诊断摘要：每引擎 状态码/结果数/错误/风控特征。"""
    parts = []
    for d in diag:
        extra = ""
        if d.get("error"):
            extra += f", {str(d['error'])[:40]}"
        if d.get("risk"):
            extra += ", 风控:" + "/".join(str(x) for x in d["risk"][:2])
        parts.append(
            f"{d.get('engine')}(HTTP{d.get('status')}, {d.get('items')}条{extra})"
        )
    return ";".join(parts)


def _parse_generic(html_text: str) -> list[dict]:
    """通用标题/链接解析（夸克等未定结构引擎的兜底）。"""
    results: list[dict] = []
    for m in re.findall(
        r'<h[23][^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html_text,
        flags=re.S,
    ):
        url, title_raw = m
        if not url or url.startswith("//") or not url.startswith("http"):
            continue
        title = _clean(title_raw)
        if not title:
            continue
        results.append({"title": title, "url": url, "snippet": ""})
    return results


def _parse_engine(name: str, html_text: str) -> list[dict]:
    if name == "360":
        return _parse_360(html_text)
    if name == "cn.bing":
        return _parse_bing(html_text)
    return _parse_generic(html_text)


def _engine_request(name: str, keyword: str, page: int) -> tuple[str, str]:
    """返回 (请求 URL, Referer)。"""
    q = urllib.parse.quote(keyword)
    if name == "360":
        url = SO_URL + "?q=" + q + (f"&pn={page}" if page > 1 else "")
        return url, "https://www.so.com/"
    if name == "cn.bing":
        url = BING_URL + "?q=" + q + "&setlang=zh-hans"
        if page > 1:
            url += f"&first={(page - 1) * 30 + 1}"
        return url, "https://cn.bing.com/"
    return QUARK_URL + "?q=" + q, "https://quark.sm.cn/"


def _search_engine(
    session: requests.Session,
    keyword: str,
    page: int,
    blocked: set[str] | None = None,
) -> tuple[str, list[dict], list[dict]]:
    """依次尝试 360 → cn.bing → 夸克；验证码等硬风控**立即停止**。

    返回 (引擎名, 结果, diag)；两个引擎都失败时 engine="", items=[]，
    diag 记录每引擎状态码/结果数/错误/风控特征（供风控识别与诊断）。
    硬风控引擎加入 blocked（会话内不再请求），减少被封后的无效请求。
    """
    diag: list[dict] = []
    for name in _ENGINE_ORDER:
        url, referer = _engine_request(name, keyword, page)
        if blocked and name in blocked:
            diag.append({"engine": name, "status": "skip", "items": 0,
                         "error": "会话内已判定风控，跳过", "risk": ["blocked"]})
            continue
        try:
            html_text, status, err = _fetch(session, url, referer)
        except Exception as exc:  # 兜底：单引擎任何异常都继续尝试下一个
            html_text, status, err = "", 0, str(exc)
        items = _parse_engine(name, html_text) if html_text else []
        risk = _risk_info(html_text + (" " + err if err else ""))
        diag.append({"engine": name, "status": status, "items": len(items),
                     "error": err, "risk": risk})
        if _is_hard_risk(risk):
            # 验证码即停：不再请求后续引擎、不再重试
            if blocked is not None:
                blocked.add(name)
            return "", [], diag
        if items and _quality_check(keyword, items):
            return name, items, diag
    return "", [], diag


def _probe_one(session: requests.Session, name: str, query: str) -> dict:
    """单引擎探测（probe_engines / lightweight_probe 共用）。

    status：ok=解析且质量通过；degraded=有结果但与查询无关（降级页）；
    risk=风控特征（验证码/安全验证等）；error=请求/解析失败；empty=无结果。
    """
    url, referer = _engine_request(name, query, 1)
    try:
        html_text, status, err = _fetch(session, url, referer)
    except Exception as exc:
        html_text, status, err = "", 0, str(exc)
    items = _parse_engine(name, html_text) if html_text else []
    risk = _risk_info(html_text + (" " + err if err else ""))
    if _is_hard_risk(risk):
        st = "risk"
        msg = "风控：" + "/".join(str(x) for x in risk[:3])
    elif items and _quality_check(query, items):
        st = "ok"
        msg = f"正常（解析 {len(items)} 条，质量通过）"
    elif items:
        st = "degraded"
        msg = f"降级（{len(items)} 条但与查询无关/质量不过）"
    elif err:
        st = "error"
        msg = f"请求失败：{str(err)[:60]}"
    else:
        st = "empty"
        msg = "无结果"
    return {
        "engine": name, "status": st, "items": len(items),
        "message": msg, "risk": risk, "http": status,
    }


def probe_engines(query: str = "大疆 评价") -> list[dict]:
    """一键探针（深度层）：分别向 360 / cn.bing / 夸克 各发 1 次请求。

    用于向导"渠道/时间"步骤与换网络后的快速验证（每次 3 个请求）。
    """
    session = requests.Session()
    return [_probe_one(session, name, query) for name in _ENGINE_ORDER]


def lightweight_probe(query: str) -> dict:
    """轻量探测（体检层）：只打 360 主引擎 1 次请求，含风控/质量识别。

    与 probe_engines 单条同构（engine/status/items/message/risk/http），
    供渠道诊断的 WebSearch 行复用——不再以 HTTP 200 为准（验证码页/降级页
    会被识别为 risk/degraded）。见 docs/渠道诊断融合方案.md。
    """
    return _probe_one(requests.Session(), "360", query)

def _day_state_path() -> Path:
    state_dir = Path(os.environ.get("SMS_STATE_DIR", str(Path(__file__).resolve().parents[2] / "data" / "state")))
    return state_dir / "websearch_day.json"


def _day_used() -> int:
    try:
        p = _day_state_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("date") == date.today().isoformat():
                return int(data.get("used") or 0)
    except (OSError, ValueError):
        pass
    return 0


def _add_day_used(n: int) -> None:
    try:
        p = _day_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"date": date.today().isoformat(), "used": _day_used() + n}
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


class WebSearchChannel(ChannelAdapter):
    """全网/知乎/贴吧/TapTap 子渠道，通过 target 参数实例化。"""

    def __init__(self, target: str = ""):
        meta = TARGETS[target]
        self.target = target
        self.id = meta["id"]
        self.name = meta["name"]
        self.auth_required = False
        self.applicability = meta["applicability"]
        self.description = meta["description"]
        self.demo = False
        self._hint = meta["hint"]
        self._domains = meta["domains"]

    def collect(
        self,
        plan: AnalysisPlan,
        on_progress: ProgressCallback | None = None,
        cancel_event: Event | None = None,
        skip_urls: set[str] | None = None,
    ) -> ChannelResult:
        strategy = load_keyword_strategy()
        subject = plan.subject or ""
        aliases = (strategy.get("synonyms") or {}).get(subject, [])[:2]
        extra_queries = (strategy.get("extra_queries") or {}).get(subject, [])[:5]
        session = requests.Session()
        session.headers.update(dict(REQUEST_HEADERS))
        session.headers["User-Agent"] = random.choice(USER_AGENTS)
        posts: list[Post] = []
        dropped: list[dict] = []
        seen_urls: set[str] = set(skip_urls or ())
        first_error = ""
        keywords = plan.keywords or [plan.subject]
        limit = plan.per_keyword_limit
        official_domains: list[str] = []
        eval_suffix = True
        for channel in plan.channels:
            if channel.channel_id == self.id:
                limit = int(channel.params.get("limit") or limit)
                if channel.params.get("eval_suffix") == "0":
                    eval_suffix = False
                raw_domains = channel.params.get("official_domains") or ""
                if isinstance(raw_domains, str):
                    official_domains = [
                        d.strip().lower()
                        for d in raw_domains.split(",")
                        if d.strip()
                    ]
                else:
                    official_domains = list(raw_domains)
        limit = min(limit, MAX_LIMIT)

        query_keywords: list[str] = []
        for kw in keywords:
            query_keywords.append(kw)
            if kw == subject:
                query_keywords.extend(aliases)
                query_keywords.extend(extra_queries)

        # 每日关键词总量拦截（防触发风控，含子渠道展开后的查询次数）
        used = _day_used()
        total_queries = len(query_keywords)
        if used + total_queries > DAILY_KEYWORD_MAX:
            return degraded_result(
                self.id,
                f"今日 WebSearch 关键词总量将超限（已用 {used} + 本次 {total_queries} "
                f"> 上限 {DAILY_KEYWORD_MAX}），建议分次运行或明日再试",
            )
        _add_day_used(total_queries)
        blocked: set[str] = set()

        empty_in_a_row = 0
        risk_hit = False
        diag_summary: list[str] = []
        for idx, keyword in enumerate(query_keywords):
            if cancel_event and cancel_event.is_set():
                break
            keyword_posts: list[Post] = []
            query = build_query(keyword, self._hint, eval_suffix, strategy)
            pages = MAX_PAGES  # 搜索深度固定 2 页，产出由 limit（≤13）封顶
            kw_diag: list[dict] = []
            hard_stop = False
            for attempt in range(EMPTY_RETRY_TIMES + 1):
                hard_stop = False
                for page in range(1, pages + 1):
                    if cancel_event and cancel_event.is_set():
                        break
                    engine, items, diag = _search_engine(
                        session, query, page, blocked
                    )
                    kw_diag.extend(diag)
                    hard_stop = _diag_has_hard_risk(diag)
                    if not items or hard_stop:
                        break
                    for item in items:
                        if item["url"] in seen_urls:
                            continue
                        if self._domains and not any(
                            d in item["url"] for d in self._domains
                        ):
                            continue
                        domain = urllib.parse.urlparse(item["url"]).netloc
                        if official_domains and any(
                            d in domain for d in official_domains
                        ):
                            dropped.append(
                                {
                                    "platform": self.id,
                                    "url": item["url"],
                                    "title": item["title"][:80],
                                    "keyword": keyword,
                                    "query": query,
                                    "reason": "官网域名黑名单",
                                }
                            )
                            continue
                        seen_urls.add(item["url"])
                        content = item["snippet"] or item["title"]
                        keyword_posts.append(
                            Post(
                                id=item["url"],
                                platform=self.id,
                                keyword=keyword,
                                author=domain,
                                title=item["title"],
                                content=content,
                                url=item["url"],
                                timestamp="",
                                platform_specific={
                                    "source": "websearch",
                                    "target": self.target,
                                    "engine": engine,
                                    "domain": domain,
                                    "query": query,
                                },
                            )
                        )
                        if len(keyword_posts) >= limit:
                            break
                    jittered_sleep(REQUEST_INTERVAL, 0.25)
                    if len(keyword_posts) >= limit:
                        break
                if keyword_posts or attempt == EMPTY_RETRY_TIMES or hard_stop:
                    break
                # 空结果：记录诊断，等待后重试 1 次（降低偶发抖动误判）
                first_error = first_error or f"关键词「{query}」两引擎均无有效结果"
                time.sleep(random.uniform(*EMPTY_RETRY_DELAY))

            posts.extend(keyword_posts)
            if on_progress:
                on_progress(
                    f"关键词「{keyword}」已获取 {len(keyword_posts)} 条结果",
                    (idx + 1) / max(len(query_keywords), 1),
                )
            if not keyword_posts:
                if hard_stop:
                    empty_in_a_row = EMPTY_RISK_THRESHOLD  # 验证码即停：直接触发冷却
                else:
                    empty_in_a_row += 1
                if hard_stop or any(d.get("risk") for d in kw_diag):
                    risk_hit = True
                diag_summary.append(f"「{query}」{_diag_compact(kw_diag)}")
                if empty_in_a_row >= EMPTY_RISK_THRESHOLD:
                    break  # 连续 ≥2 个空结果或验证码即停 → 判定风控，提前结束
            else:
                empty_in_a_row = 0

        if not posts:
            if empty_in_a_row >= EMPTY_RISK_THRESHOLD:
                diag_text = "；".join(diag_summary[-8:]) or "无诊断信息"
                error = (
                    f"[WebSearch] 疑似风控：连续 {empty_in_a_row} 个关键词空结果，"
                    f"已建议冷却后重试（诊断：{diag_text[:300]}）"
                )
            else:
                error = first_error or "WebSearch 未获取到任何结果"
            return degraded_result(self.id, error)
        if risk_hit:
            # 部分成功但触发风控信号：本轮结果可用，冷却防止下轮继续空跑
            try:
                from app.core import jobs

                jobs.set_cooldown(
                    self.id,
                    "WebSearch 部分关键词触发风控（连续空结果），已自动冷却",
                )
            except Exception:
                pass
        if on_progress:
            on_progress(f"WebSearch 采集完成，共 {len(posts)} 条", 1.0)
        return ChannelResult(
            channel_id=self.id, ok=True, posts=posts, dropped=dropped
        )
