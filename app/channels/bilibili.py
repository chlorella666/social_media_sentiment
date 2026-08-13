"""B站渠道（V1 实现：api.bilibili.com 公开 API，零登录）。

流程：视频搜索 → 视频信息（view/danmaku/coin/favorite/share）→ 热门评论。
实测（2026-08-04）：先访问 www.bilibili.com 获取 buvid3 cookie 后，
搜索与评论 API 均无需登录即可使用；频率过高会触发 -412，故请求间保留间隔。
"""

from __future__ import annotations

import html
import math
import re
import time
from datetime import datetime
from threading import Event
from typing import Any

import requests

from app.channels.base import ChannelAdapter, ProgressCallback, degraded_result
from app.core.models import AnalysisPlan, ChannelResult, Comment, Post

SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
REPLY_URL = "https://api.bilibili.com/x/v2/reply/main"
COOKIE_BOOT_URL = "https://www.bilibili.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
PAGE_SIZE = 20
REQUEST_TIMEOUT = 20
REQUEST_INTERVAL = 0.3  # 秒，节流防 -412
# 每关键词条数上限封顶：公开 API 零登录最安全，但高频仍会触发 -412；加量建议加关键词
MAX_LIMIT = 50
# 评论请求节流：每个关键词只给前 N 个视频拉评论（一次分析约 10 次评论请求/关键词）
MAX_COMMENT_VIDEOS_PER_KEYWORD = 10

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    """全局复用 Session；先访问首页拿 buvid3 cookie，降低搜索被拦截概率。"""
    global _session
    if _session is None:
        session = requests.Session()
        session.headers.update(
            {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"}
        )
        try:
            session.get(COOKIE_BOOT_URL, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            pass  # 拿不到 cookie 也先尝试请求，部分网络环境仍可用
        _session = session
    return _session


def _clean_html(text: str) -> str:
    """去掉 <em class="keyword"> 等高亮标签并反转义。"""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _search_videos(session: requests.Session, keyword: str, page: int) -> list[dict]:
    """B站视频搜索（综合排序），返回原始结果列表。"""
    resp = session.get(
        SEARCH_URL,
        params={
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "page_size": PAGE_SIZE,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"B站搜索返回错误 code={data.get('code')}：{data.get('message')}")
    return (data.get("data") or {}).get("result") or []


def _to_post(video: dict[str, Any], keyword: str) -> Post:
    bvid = video.get("bvid") or ""
    pub_ts = int(video.get("pubdate") or 0)
    title = _clean_html(str(video.get("title") or ""))
    desc = str(video.get("description") or video.get("desc") or "").strip()
    content = desc or title
    return Post(
        id=bvid,
        platform="bilibili",
        keyword=keyword,
        author=str(video.get("author") or ""),
        title=title,
        content=content,
        url=f"https://www.bilibili.com/video/{bvid}",
        timestamp=datetime.fromtimestamp(pub_ts).isoformat() if pub_ts else "",
        likes=int(video.get("like") or 0),
        reposts=int(video.get("share") or 0),
        comments_count=int(video.get("video_review") or 0),
        platform_specific={
            "aid": int(video.get("aid") or 0),
            "view": int(video.get("play") or 0),
            "danmaku": int(video.get("danmaku") or 0),
            "coin": int(video.get("coin") or 0),
            "favorite": int(video.get("favorites") or 0),
            "share": int(video.get("share") or 0),
            "category": str(video.get("typename") or video.get("cate_name") or ""),
            "publish_time": pub_ts,
        },
    )


def _fetch_comments(session: requests.Session, aid: int, limit: int) -> list[Comment]:
    """拉取视频热门评论（第一页），最多 limit 条；每条附 1 条最热楼中楼。"""
    comments: list[Comment] = []
    resp = session.get(
        REPLY_URL,
        params={"type": 1, "oid": aid, "mode": 3, "next": 0},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        return comments
    replies = (data.get("data") or {}).get("replies") or []
    for reply in replies[:limit]:
        text = str((reply.get("content") or {}).get("message") or "").strip()
        if not text:
            continue
        author = str((reply.get("member") or {}).get("uname") or "")
        like = int(reply.get("like") or 0)
        comments.append(
            Comment(
                author=author,
                text=text,
                likes=like,
                time=(
                    datetime.fromtimestamp(int(reply.get("ctime") or 0)).isoformat()
                    if reply.get("ctime")
                    else ""
                ),
            )
        )
        children = reply.get("replies") or []
        if children:
            hot = max(
                children,
                key=lambda c: int(c.get("like") or 0),
            )
            hot_text = str((hot.get("content") or {}).get("message") or "").strip()
            if hot_text:
                comments.append(
                    Comment(
                        author=str((hot.get("member") or {}).get("uname") or ""),
                        text=hot_text,
                        likes=int(hot.get("like") or 0),
                        time=(
                            datetime.fromtimestamp(int(hot.get("ctime") or 0)).isoformat()
                            if hot.get("ctime")
                            else ""
                        ),
                        is_reply=True,
                        depth=1,
                    )
                )
    return comments


def _in_date_range(post: Post, plan: AnalysisPlan) -> bool:
    if not post.timestamp or (not plan.date_start and not plan.date_end):
        return True
    try:
        pub_date = datetime.fromisoformat(post.timestamp).date()
    except ValueError:
        return True
    if plan.date_start and pub_date < plan.date_start:
        return False
    if plan.date_end and pub_date > plan.date_end:
        return False
    return True


class BilibiliChannel(ChannelAdapter):
    id = "bilibili"
    name = "B站"
    auth_required = False
    applicability = "游戏、科技、年轻化品牌（公开 API 零登录）"
    description = "B站搜索 + 视频信息 + 评论采集（公开 API）"

    def collect(
        self,
        plan: AnalysisPlan,
        on_progress: ProgressCallback | None = None,
        cancel_event: Event | None = None,
    ) -> ChannelResult:
        session = _get_session()
        posts: list[Post] = []
        seen_ids: set[str] = set()
        first_error = ""
        keywords = plan.keywords or [plan.subject]
        limit = plan.per_keyword_limit
        for channel in plan.channels:
            if channel.channel_id == self.id:
                limit = int(channel.params.get("limit") or limit)
        limit = min(limit, MAX_LIMIT)

        for idx, keyword in enumerate(keywords):
            if cancel_event and cancel_event.is_set():
                break
            keyword_posts: list[Post] = []
            total_pages = max(1, math.ceil(max(limit, 1) / PAGE_SIZE))
            for page in range(1, total_pages + 1):
                if cancel_event and cancel_event.is_set():
                    break
                try:
                    results = _search_videos(session, keyword, page)
                except (requests.RequestException, RuntimeError) as exc:
                    first_error = first_error or f"关键词「{keyword}」失败：{exc}"
                    break
                if not results:
                    break
                for video in results:
                    bvid = str(video.get("bvid") or "")
                    if not bvid or bvid in seen_ids:
                        continue
                    seen_ids.add(bvid)
                    post = _to_post(video, keyword)
                    if not _in_date_range(post, plan):
                        continue
                    keyword_posts.append(post)
                    if len(keyword_posts) >= limit:
                        break
                time.sleep(REQUEST_INTERVAL)
                if len(keyword_posts) >= limit:
                    break

            # 评论：只给前 N 个视频拉热门评论，控制请求量与耗时
            if plan.comments_enabled:
                for comment_idx, post in enumerate(
                    keyword_posts[:MAX_COMMENT_VIDEOS_PER_KEYWORD]
                ):
                    if cancel_event and cancel_event.is_set():
                        break
                    aid = int(post.platform_specific.get("aid") or 0)
                    if not aid:
                        continue
                    try:
                        post.comments = _fetch_comments(
                            session, aid, plan.comments_per_post
                        )
                    except (requests.RequestException, RuntimeError):
                        pass  # 评论失败不影响主数据
                    time.sleep(REQUEST_INTERVAL)

            posts.extend(keyword_posts)
            if on_progress:
                on_progress(
                    f"关键词「{keyword}」已采集 {len(keyword_posts)} 条视频",
                    (idx + 1) / max(len(keywords), 1),
                )

        if not posts:
            return degraded_result(
                self.id,
                first_error or "B站未采集到任何内容（关键词无结果或全部超出时间范围）",
            )
        if on_progress:
            on_progress(f"B站采集完成，共 {len(posts)} 条", 1.0)
        return ChannelResult(channel_id=self.id, ok=True, posts=posts)
