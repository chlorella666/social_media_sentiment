"""微博渠道（V1 实现：m.weibo.cn API + Cookie 登录态）。

采集方式：微博搜索（container/getIndex）→ 长文正文（statuses/extend）→
评论（api/comments/show）。Cookie 通过向导输入，仅存于当前会话；
过期时返回含 "Cookie" 的错误触发界面弹窗重粘。

实测（2026-08-04）：SUB Cookie 有效时搜索/长文/评论接口均可用；
hotflow 评论接口返回 HTML，不可用，使用 comments/show。
"""

from __future__ import annotations

import html
import os
import re
import time
from datetime import datetime
from threading import Event

import requests

from app.channels.base import (
    ChannelAdapter,
    ProgressCallback,
    degraded_result,
    jittered_sleep,
)
from app.core.errors import is_ratelimit
from app.coding.cleaner import normalize_datetime
from app.core.models import AnalysisPlan, ChannelResult, Comment, Post

SEARCH_URL = "https://m.weibo.cn/api/container/getIndex"
EXTEND_URL = "https://m.weibo.cn/statuses/extend"
COMMENTS_URL = "https://m.weibo.cn/api/comments/show"
HOTFLOW_URL = "https://m.weibo.cn/comments/hotflow"
MOBILE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
REQUEST_TIMEOUT = 20
REQUEST_INTERVAL = 0.5  # 秒，节流均值（±40% 抖动防固定节奏）
# 每关键词条数上限封顶：账号级风控最严，单关键词约 500 条封顶；加量建议加关键词
MAX_LIMIT = 30
# 评论请求节流：每个关键词只给前 N 个帖子拉评论
MAX_COMMENT_POSTS_PER_KEYWORD = 10


def _clean_text(raw: str) -> str:
    """去 HTML 标签并反转义；保留 #话题# 文本内容。"""
    text = re.sub(r"<[^>]+>", "", raw or "")
    return html.unescape(text).replace("\u200b", "").strip()


def _parse_weibo_time(raw: str) -> str:
    """解析微博时间格式：Tue Aug 04 12:18:01 +0800 2026。"""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except ValueError:
        return raw  # 相对时间（如"3分钟前"）原样保留


def _build_headers(cookie: str) -> dict:
    return {
        "User-Agent": MOBILE_UA,
        "Cookie": cookie if cookie.startswith("SUB=") else f"SUB={cookie}",
        "Referer": "https://m.weibo.cn/",
    }


def _fetch_long_text(session: requests.Session, headers: dict, mid: int) -> str:
    try:
        resp = session.get(
            EXTEND_URL, params={"id": mid}, headers=headers, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok") == 1:
            return _clean_text((data.get("data") or {}).get("longTextContent") or "")
    except (requests.RequestException, ValueError):
        pass
    return ""


def _fetch_comments(
    session: requests.Session, headers: dict, bid: str, limit: int
) -> list[Comment]:
    """拉取热门评论（hotflow，自带热评+楼中楼），最多 limit 条。"""
    comments: list[Comment] = []
    raw_items: list[dict] = []
    try:
        resp = session.get(
            HOTFLOW_URL,
            params={"id": bid, "mid": bid, "max_id_type": 0},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok") == 1:
            raw_items = ((data.get("data") or {}).get("data")) or []
        else:
            # 兜底：comments/show（按 like_counts 排序，无楼中楼）
            resp2 = session.get(
                COMMENTS_URL,
                params={"id": bid, "page": 1},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            resp2.raise_for_status()
            data2 = resp2.json()
            if data2.get("ok") == 1:
                raw_items = ((data2.get("data") or {}).get("data")) or []
    except (requests.RequestException, ValueError):
        pass  # 评论失败不影响主数据

    seen_ids: set[int] = set()
    ordered: list[dict] = []
    for item in raw_items:
        cid = item.get("id")
        if cid in seen_ids:
            continue
        seen_ids.add(cid)
        ordered.append(item)
    ordered.sort(key=lambda c: int(c.get("like_count") or c.get("like_counts") or 0), reverse=True)

    for item in ordered[:limit]:
        text = _clean_text(item.get("text") or "")
        if not text:
            continue
        author = str((item.get("user") or {}).get("screen_name") or "")
        likes = int(item.get("like_count") or item.get("like_counts") or 0)
        comments.append(
            Comment(
                author=author,
                text=text,
                likes=likes,
                time=normalize_datetime(str(item.get("created_at") or "")),
            )
        )
        children = item.get("comments") or item.get("replies") or []
        if children:
            hot = max(
                children,
                key=lambda c: int(c.get("like_count") or c.get("like_counts") or 0),
            )
            hot_text = _clean_text(hot.get("text") or "")
            if hot_text:
                comments.append(
                    Comment(
                        author=str(
                            (hot.get("user") or {}).get("screen_name") or ""
                        ),
                        text=hot_text,
                        likes=int(hot.get("like_count") or hot.get("like_counts") or 0),
                        time=normalize_datetime(str(hot.get("created_at") or "")),
                        is_reply=True,
                        reply_to=author,
                        depth=1,
                    )
                )
    return comments


def _to_post(mblog: dict, keyword: str) -> Post:
    bid = str(mblog.get("bid") or "")
    user = mblog.get("user") or {}
    text = _clean_text(mblog.get("text") or "")
    return Post(
        id=bid,
        platform="weibo",
        keyword=keyword,
        author=str(user.get("screen_name") or ""),
        title="",
        content=text,
        url=f"https://m.weibo.cn/detail/{bid}",
        timestamp=_parse_weibo_time(mblog.get("created_at") or ""),
        likes=int(mblog.get("attitudes_count") or 0),
        reposts=int(mblog.get("reposts_count") or 0),
        comments_count=int(mblog.get("comments_count") or 0),
        platform_specific={
            "mid": int(mblog.get("id") or 0),
            "source_app": str(mblog.get("source") or ""),
            "retweeted_text": (
                _clean_text((mblog.get("retweeted_status") or {}).get("text") or "")
                if mblog.get("retweeted_status")
                else ""
            ),
        },
    )


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


class WeiboChannel(ChannelAdapter):
    id = "weibo"
    name = "微博"
    auth_required = True
    skip_key = "id"  # 帖子身份键 = bid
    applicability = "品牌舆情主战场，全品类适用（需 Cookie）"
    description = "微博搜索 + 正文 + 评论采集（m.weibo.cn API）"

    def collect(
        self,
        plan: AnalysisPlan,
        on_progress: ProgressCallback | None = None,
        cancel_event: Event | None = None,
        skip_urls: set[str] | None = None,
    ) -> ChannelResult:
        cookie = None
        for channel in plan.channels:
            if channel.channel_id == self.id:
                cookie = channel.params.get("cookie")
        if not cookie:
            # 关键失败：缺少认证 → 标记 degraded 并给出明确提示，不中断流程
            return degraded_result(self.id, "微博渠道需要 Cookie：请在设置中粘贴已登录的微博 Cookie")

        session = requests.Session()
        headers = _build_headers(cookie)
        posts: list[Post] = []
        dropped_records: list[dict] = []  # 采集层丢弃（透明度，2026-08-18）
        stats: dict[str, int] = {
            "requested_limit": 0, "api_returned_cards": 0, "mblog_cards": 0,
            "skipped_other_type": 0, "skipped_dup": 0, "skipped_ad": 0,
            "skipped_out_of_range": 0, "kept": 0,
        }
        seen_ids: set[str] = set(skip_urls or ())
        first_error = ""
        # 2026-08-18：渠道策略展开后的查询串优先（channel_params.queries）
        cfg = next((c for c in plan.channels if c.channel_id == self.id), None)
        keywords = (cfg.params.get("queries") if cfg else None) \
            or plan.keywords or [plan.subject]
        limit = plan.per_keyword_limit
        if cfg:
            limit = int(cfg.params.get("limit") or limit)
        limit = min(limit, MAX_LIMIT)
        stats["requested_limit"] = limit

        for idx, keyword in enumerate(keywords):
            if cancel_event and cancel_event.is_set():
                break
            keyword_posts: list[Post] = []
            pages = max(1, (limit + 9) // 10)
            for page in range(1, min(pages, 5) + 1):
                if cancel_event and cancel_event.is_set():
                    break
                try:
                    resp = session.get(
                        SEARCH_URL,
                        params={
                            "containerid": f"100103type=1&q={keyword}",
                            "page_type": "searchall",
                            "page": page,
                        },
                        headers=headers,
                        timeout=REQUEST_TIMEOUT,
                    )
                    resp.raise_for_status()
                    try:
                        data = resp.json()
                    except ValueError:
                        # 登录态失效时 m.weibo.cn 常返回 HTML 登录页而非 JSON
                        raise RuntimeError("微博 Cookie 无效或已过期，请重新粘贴")
                    if data.get("ok") != 1:
                        msg = str(data.get("msg") or "")
                        if "登录" in msg or "login" in msg.lower():
                            raise RuntimeError("微博 Cookie 无效或已过期，请重新粘贴")
                        raise RuntimeError(f"微博搜索失败：{msg}")
                except (requests.RequestException, RuntimeError) as exc:
                    if is_ratelimit(str(exc)):
                        # 风控即停：立即终止本渠道（保留已采部分），不再请求后续关键词/页
                        return ChannelResult(
                            channel_id=self.id, ok=False, posts=posts,
                            error=f"风控停止：{exc}", degraded=True, risk=True,
                            dropped=dropped_records, collection_stats=stats,
                        )
                    first_error = first_error or f"关键词「{keyword}」失败：{exc}"
                    break
                cards = (data.get("data") or {}).get("cards") or []
                # 诊断插桩（SMS_DEBUG_WEIBO=1 时输出逐页分流，正常采集不受影响）
                debug = os.environ.get("SMS_DEBUG_WEIBO") == "1"
                if debug:
                    _dbg = {"cards": len(cards), "type9": 0, "other_type": 0,
                            "dup": 0, "ad": 0, "out_of_range": 0, "kept": 0}
                stats["api_returned_cards"] += len(cards)
                for card in cards:
                    if card.get("card_type") != 9:
                        stats["skipped_other_type"] += 1
                        if debug:
                            _dbg["other_type"] += 1
                        continue
                    mblog = card.get("mblog") or {}
                    stats["mblog_cards"] += 1
                    if debug:
                        _dbg["type9"] += 1
                    bid = str(mblog.get("bid") or "")
                    if not bid or bid in seen_ids:
                        stats["skipped_dup"] += 1
                        if debug:
                            _dbg["dup"] += 1
                        if bid:
                            dropped_records.append({
                                "platform": self.id,
                                "url": f"https://m.weibo.cn/detail/{bid}",
                                "title": str(mblog.get("text") or "")[:80],
                                "keyword": keyword, "query": keyword,
                                "reason": "重复返回（同帖）",
                                "kind": "collection",
                            })
                        continue
                    if mblog.get("ad_marked"):
                        stats["skipped_ad"] += 1
                        if debug:
                            _dbg["ad"] += 1
                        dropped_records.append({
                            "platform": self.id,
                            "url": f"https://m.weibo.cn/detail/{bid}",
                            "title": str(mblog.get("text") or "")[:80],
                            "keyword": keyword, "query": keyword,
                            "reason": "广告（ad_marked）",
                            "kind": "collection",
                        })
                        continue  # 过滤广告
                    seen_ids.add(bid)
                    post = _to_post(mblog, keyword)
                    post.platform_specific["query"] = keyword  # 2.9：实际查询串（渠道级）
                    if mblog.get("isLongText"):
                        long_text = _fetch_long_text(
                            session, headers, int(mblog.get("id") or 0)
                        )
                        if long_text:
                            post.content = long_text
                    if not _in_date_range(post, plan):
                        stats["skipped_out_of_range"] += 1
                        if debug:
                            _dbg["out_of_range"] += 1
                        dropped_records.append({
                            "platform": self.id, "url": post.url,
                            "title": (post.title or post.content or "")[:80],
                            "keyword": keyword, "query": keyword,
                            "reason": "超出时间范围",
                            "kind": "collection",
                        })
                        continue
                    stats["kept"] += 1
                    if debug:
                        _dbg["kept"] += 1
                    keyword_posts.append(post)
                    if len(keyword_posts) >= limit:
                        break
                if debug:
                    print(
                        f"[weibo-debug] kw={keyword} page={page} "
                        f"cards={_dbg['cards']} type9={_dbg['type9']} "
                        f"other_type={_dbg['other_type']} dup={_dbg['dup']} "
                        f"ad={_dbg['ad']} out_of_range={_dbg['out_of_range']} "
                        f"kept={_dbg['kept']}"
                    )
                jittered_sleep(REQUEST_INTERVAL, 0.4)
                if len(keyword_posts) >= limit:
                    break

            if plan.comments_enabled:
                for post in keyword_posts[:MAX_COMMENT_POSTS_PER_KEYWORD]:
                    if cancel_event and cancel_event.is_set():
                        break
                    try:
                        post.comments = _fetch_comments(
                            session, headers, post.id, plan.comments_per_post
                        )
                    except (requests.RequestException, RuntimeError) as exc:
                        if is_ratelimit(str(exc)):
                            return ChannelResult(
                                channel_id=self.id, ok=False, posts=posts,
                                error=f"风控停止：{exc}", degraded=True, risk=True,
                            )
                    jittered_sleep(REQUEST_INTERVAL, 0.4)

            posts.extend(keyword_posts)
            if on_progress:
                on_progress(
                    f"关键词「{keyword}」已采集 {len(keyword_posts)} 条微博",
                    (idx + 1) / max(len(keywords), 1),
                )

        if not posts:
            return ChannelResult(
                channel_id=self.id, ok=False, posts=posts,
                error=first_error
                or "微博未采集到任何内容（关键词无结果或全部超出时间范围）",
                degraded=True, dropped=dropped_records, collection_stats=stats,
            )
        if on_progress:
            on_progress(f"微博采集完成，共 {len(posts)} 条", 1.0)
        return ChannelResult(
            channel_id=self.id, ok=True, posts=posts,
            dropped=dropped_records, collection_stats=stats,
        )
