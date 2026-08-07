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
import re
import time
import urllib.parse
from threading import Event

import requests

from app.channels.base import ChannelAdapter, ProgressCallback, degraded_result
from app.core.models import AnalysisPlan, ChannelResult, Post

SO_URL = "https://www.so.com/s"
BING_URL = "https://cn.bing.com/search"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 25
REQUEST_INTERVAL = 1.5  # 节流，降低被搜索引擎反爬的概率
MAX_PAGES = 2
MAX_RETRIES = 2

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


def _fetch(session: requests.Session, url: str) -> str:
    last_error = ""
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"搜索请求失败：{last_error}")


def _search_engine(session: requests.Session, keyword: str, page: int) -> tuple[str, list[dict]]:
    """依次尝试 360 → cn.bing，返回 (引擎名, 解析结果)。"""
    so_url = SO_URL + "?q=" + urllib.parse.quote(keyword)
    if page > 1:
        so_url += f"&pn={page}"
    items = _parse_360(_fetch(session, so_url))
    if items and _quality_check(keyword, items):
        return "360", items

    bing_url = BING_URL + "?q=" + urllib.parse.quote(keyword) + "&setlang=zh-hans"
    if page > 1:
        bing_url += f"&first={(page - 1) * 30 + 1}"
    items = _parse_bing(_fetch(session, bing_url))
    if items and _quality_check(keyword, items):
        return "cn.bing", items
    return "", []


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
    ) -> ChannelResult:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        posts: list[Post] = []
        dropped: list[dict] = []
        seen_urls: set[str] = set()
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

        for idx, keyword in enumerate(keywords):
            if cancel_event and cancel_event.is_set():
                break
            keyword_posts: list[Post] = []
            query = f"{keyword} {self._hint}".strip() if self._hint else keyword
            if (
                eval_suffix
                and not self._hint
                and not any(k in query for k in ("评价", "怎么样", "吐槽", "测评"))
            ):
                query = f"{query} 评价"  # 纯品牌词易命中官网，追加评价后缀提高有效供给
            pages = min(MAX_PAGES, max(1, (limit + 19) // 20))
            for page in range(1, pages + 1):
                if cancel_event and cancel_event.is_set():
                    break
                try:
                    engine, items = _search_engine(session, query, page)
                except (requests.RequestException, RuntimeError) as exc:
                    first_error = first_error or f"关键词「{query}」失败：{exc}"
                    break
                if not items:
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
                            },
                        )
                    )
                    if len(keyword_posts) >= limit:
                        break
                time.sleep(REQUEST_INTERVAL)
                if len(keyword_posts) >= limit:
                    break

            posts.extend(keyword_posts)
            if on_progress:
                on_progress(
                    f"关键词「{keyword}」已获取 {len(keyword_posts)} 条结果",
                    (idx + 1) / max(len(keywords), 1),
                )

        if not posts:
            return degraded_result(
                self.id, first_error or "WebSearch 未获取到任何结果"
            )
        if on_progress:
            on_progress(f"WebSearch 采集完成，共 {len(posts)} 条", 1.0)
        return ChannelResult(
            channel_id=self.id, ok=True, posts=posts, dropped=dropped
        )
