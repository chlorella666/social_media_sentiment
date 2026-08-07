"""数据清洗管道（借鉴 SocialBrandSentiment 的 cleaner 设计）。

每个清洗步骤是独立函数，可扩展增删。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta

from app.core.models import Post

URL_RE = re.compile(r"https?://\S+|www\.\S+")
HTML_RE = re.compile(r"<[^>]+>")
MENTION_RE = re.compile(r"@[\w\u4e00-\u9fff\-]+")
HASHTAG_RE = re.compile(r"#([^#\s]+)#")
NOISE_RE = re.compile(
    r"^(哈哈哈+|嘿嘿嘿+|呵呵+|2333*|666+|6666+|hhhh+|hhhhh+|路过|围观|沙发|前排|纯表情|\.{2,}|…+)$",
    re.IGNORECASE,
)
WHITESPACE_RE = re.compile(r"\s+")
PUNCT_DUP_RE = re.compile(r"([。！？!?~～，,.])\1+")

# 样板/页面壳文本：命中即判定为无效内容（不进入情感分析）
BOILERPLATE_PATTERNS = [
    r"加载中",
    r"空空如也",
    r"扫码下载",
    r"APK ?下载",
    r"版本[:：]? ?v",
    r"下载手机 ?APP",
    r"IP[:：] ?属地",
    r"发私信",
    r"查看详细资料",
    r"加入 .{0,15} 话题讨论",
    r"手机贴吧",
    r"百度首页",
    r"注册问题反馈",
    r"关注发私信",
    r"欢迎, \{\{nick_name\}\}",
]
BOILERPLATE_RE = re.compile("|".join(BOILERPLATE_PATTERNS))

# 官网/官方页标题特征：命中则排除（社交媒体情感分析不需要官方公告页）
OFFICIAL_PAGE_PATTERNS = [
    r"官网",
    r"官方网站",
    r"欢迎您",
    r"首页",
    r"服务与支持",
    r"官方周边",
]
OFFICIAL_PAGE_RE = re.compile("|".join(OFFICIAL_PAGE_PATTERNS))


def normalize_datetime(raw: str, now: datetime | None = None) -> str:
    """把平台相对时间/英文时间统一为 yyyy-mm-dd（无法解析则原样返回）。"""
    if not raw:
        return ""
    now = now or datetime.now()
    s = raw.strip()
    if s.startswith("刚刚"):
        return now.date().isoformat()
    m = re.match(r"昨天", s)
    if m:
        return (now.date() - timedelta(days=1)).isoformat()
    m = re.match(r"(\d+)分钟前", s)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).date().isoformat()
    m = re.match(r"(\d+)小时前", s)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).date().isoformat()
    m = re.match(r"(\d+)天前", s)
    if m:
        return (now - timedelta(days=int(m.group(1)))).date().isoformat()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{2})-(\d{2}) (\d{2}):\d{2}", s)
    if m:
        return f"{now.year}-{m.group(1)}-{m.group(2)}"
    try:
        return datetime.strptime(
            s, "%a %b %d %H:%M:%S %z %Y"
        ).date().isoformat()
    except ValueError:
        pass
    return s


def clean_text(text: str) -> str:
    """清洗单条文本：Unicode 规范化 → HTML/URL 清理 → 噪声过滤 → 空白合并。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = HTML_RE.sub(" ", t)
    t = URL_RE.sub(" ", t)
    t = MENTION_RE.sub(" ", t)
    t = NOISE_RE.sub(" ", t.strip())
    t = PUNCT_DUP_RE.sub(r"\1", t)
    t = WHITESPACE_RE.sub(" ", t)
    return t.strip()


def dedupe_posts(posts: list[Post]) -> list[Post]:
    """四重去重：ID / URL / 标题前50字 / 内容前50字。

    修复：空标题/空正文不作为去重键（微博无标题，若用空串作键会误删全部无标题帖）。
    """
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    seen_contents: set[str] = set()
    result: list[Post] = []
    for post in posts:
        title_key = (post.title or "").strip()[:50]
        content_key = (post.content or "").strip()[:50]
        if (
            post.id in seen_ids
            or post.url in seen_urls
            or (title_key and title_key in seen_titles)
            or (content_key and content_key in seen_contents)
        ):
            continue
        seen_ids.add(post.id)
        seen_urls.add(post.url)
        if title_key:
            seen_titles.add(title_key)
        if content_key:
            seen_contents.add(content_key)
        result.append(post)
    return result


def is_boilerplate(text: str) -> bool:
    return bool(text and BOILERPLATE_RE.search(text))


def is_official_page(title: str) -> bool:
    return bool(title and OFFICIAL_PAGE_RE.search(title))


def clean_posts(
    posts: list[Post], subject: str = "", keywords: list[str] | None = None
) -> tuple[list[Post], list[dict]]:
    """过滤 + 去重，返回 (保留帖子, 丢弃记录[platform/url/title/reason])。

    规则：样板/页面壳、官网标题、清洗后为空、文本过短、与品牌/关键词不相关、
    四重去重。评论在此不处理（编码阶段按需过滤）。
    """
    keywords = keywords or []
    kept: list[Post] = []
    dropped: list[dict] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    seen_contents: set[str] = set()

    for post in posts:
        reasons: list[str] = []
        title = (post.title or "").strip()
        content = clean_text(post.content or "")
        if not content:
            reasons.append("正文为空")
        if is_boilerplate(content) or is_boilerplate(title):
            reasons.append("样板/页面壳文本")
        if is_official_page(title):
            reasons.append("官方页面")
        if len(content) < 10 and len(title) < 10:
            reasons.append("文本过短")
        hay = title + content
        if subject or keywords:
            relevant = (subject and subject in hay) or any(
                kw and kw in hay for kw in keywords
            )
            if not relevant:
                reasons.append("与品牌/关键词不相关")

        if not reasons:
            title_key = title[:50]
            content_key = content[:50]
            if post.id in seen_ids:
                reasons.append("重复（相同ID）")
            elif post.url in seen_urls:
                reasons.append("重复（相同链接）")
            elif title_key and title_key in seen_titles:
                reasons.append("重复（相同标题）")
            elif content_key and content_key in seen_contents:
                reasons.append("重复（相同正文）")
            else:
                seen_ids.add(post.id)
                seen_urls.add(post.url)
                if title_key:
                    seen_titles.add(title_key)
                if content_key:
                    seen_contents.add(content_key)
                kept.append(post)

        if reasons:
            dropped.append(
                {
                    "platform": post.platform,
                    "url": post.url,
                    "title": title[:80],
                    "reason": "；".join(reasons),
                }
            )
    return kept, dropped


CLEANERS = {
    "clean_text": clean_text,
    "dedupe_posts": dedupe_posts,
    "clean_posts": clean_posts,
    "is_boilerplate": is_boilerplate,
}
