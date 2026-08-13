"""小红书渠道（V1 实现：opencli xiaohongshu，复用 Chrome 登录态）。

采集方式：opencli xiaohongshu search → note（正文/互动）→ comments。
opencli 由 Node 运行（npm install -g @jackwener/opencli），要求 Chrome
已登录 xiaohongshu.com；不读取浏览器 Cookie，不替用户登录。

实测（2026-08-04）：搜索/笔记详情/评论均可用；xsec_token 机制要求
读取笔记必须使用搜索结果里的完整 URL；操作间隔 2.5s 防验证码。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from threading import Event

from app.channels.base import ChannelAdapter, ProgressCallback, degraded_result
from app.coding.cleaner import normalize_datetime
from app.core.models import AnalysisPlan, ChannelResult, Comment, Post

OPENCLI_MAIN = (
    Path.home()
    / ".nodejs"
    / "node_modules"
    / "@jackwener"
    / "opencli"
    / "dist"
    / "src"
    / "main.js"
)
OPERATION_INTERVAL = 2.5  # 小红书防验证码：每次操作间隔
OPENCLI_TIMEOUT = 180
# 笔记详情与评论请求较慢（每次约 10~15s），设置每个关键词的上限
MAX_NOTE_DETAILS_PER_KEYWORD = 10
MAX_COMMENT_NOTES_PER_KEYWORD = 5
# 每关键词条数上限封顶：反爬最严（xsec_token+签名、批量必验证码）；加量建议加关键词
MAX_LIMIT = 10


def _opencli_base() -> list[str]:
    node = shutil.which("node") or str(Path.home() / ".nodejs" / "node.exe")
    if OPENCLI_MAIN.exists():
        return [node, str(OPENCLI_MAIN)]
    raise RuntimeError(
        "未找到 opencli，请先安装：npm install -g @jackwener/opencli "
        "（并确保 Chrome 已登录 xiaohongshu.com）"
    )


def _run_opencli(*args: str) -> object:
    """调用 opencli 并解析 JSON 输出。"""
    proc = subprocess.run(
        _opencli_base() + list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=OPENCLI_TIMEOUT,
        # Windows 下禁止弹出 node 控制台窗口（worker 为 pythonw 无窗口进程，
        # 不加此标志每次采集都会闪一个黑色命令行窗口）
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip().replace("\n", " ")[:300]
        raise RuntimeError(f"opencli 无输出：{err}")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"opencli 输出无法解析：{out[:200]}")


def _parse_likes(value: object) -> int:
    s = str(value or "0").strip()
    m = re.match(r"([\d.]+)万", s)
    if m:
        return int(float(m.group(1)) * 10000)
    try:
        return int(float(s))
    except ValueError:
        return 0


def _note_id_from_url(url: str) -> str:
    m = re.search(
        r"/(?:search_result|explore|item|discovery/item)/([0-9a-f]+)", url
    )
    return m.group(1) if m else url


def _fields_to_dict(items: object) -> dict:
    if not isinstance(items, list):
        return {}
    return {
        str(item.get("field") or ""): item.get("value")
        for item in items
        if isinstance(item, dict)
    }


def _search_notes(keyword: str) -> list[dict]:
    data = _run_opencli("xiaohongshu", "search", keyword, "-f", "json")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _note_detail(url: str) -> dict:
    data = _run_opencli("xiaohongshu", "note", url, "-f", "json")
    return _fields_to_dict(data)


def _note_comments(url: str, limit: int = 20) -> list[Comment]:
    """热门评论（按点赞降序取前 limit），每条附 1 条最热楼中楼。"""
    data = _run_opencli("xiaohongshu", "comments", url, "-f", "json")
    comments: list[Comment] = []
    if not isinstance(data, list):
        return comments
    tops = [item for item in data if isinstance(item, dict) and not item.get("is_reply")]
    replies = [item for item in data if isinstance(item, dict) and item.get("is_reply")]
    tops.sort(key=lambda c: _parse_likes(c.get("likes")), reverse=True)
    for item in tops[:limit]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        author = str(item.get("author") or "")
        comments.append(
            Comment(
                author=author,
                text=text,
                likes=_parse_likes(item.get("likes")),
                time=normalize_datetime(str(item.get("time") or "")),
            )
        )
        pool = [
            r
            for r in replies
            if str(r.get("reply_to") or "") == author
            and str(r.get("text") or "").strip()
        ]
        if pool:
            hot = max(pool, key=lambda r: _parse_likes(r.get("likes")))
            comments.append(
                Comment(
                    author=str(hot.get("author") or ""),
                    text=str(hot.get("text") or "").strip(),
                    likes=_parse_likes(hot.get("likes")),
                    time=normalize_datetime(str(hot.get("time") or "")),
                    is_reply=True,
                    reply_to=author,
                    depth=1,
                )
            )
    return comments


def _in_date_range(pub_date: str, plan: AnalysisPlan) -> bool:
    if not pub_date or (not plan.date_start and not plan.date_end):
        return True
    try:
        d = datetime.strptime(pub_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return True
    if plan.date_start and d < plan.date_start:
        return False
    if plan.date_end and d > plan.date_end:
        return False
    return True


class XiaohongshuChannel(ChannelAdapter):
    id = "xiaohongshu"
    name = "小红书"
    auth_required = True
    applicability = "消费品、零售、美妆（需 Chrome 已登录小红书）"
    description = "小红书笔记搜索 + 正文 + 评论采集（opencli Chrome 会话）"

    def collect(
        self,
        plan: AnalysisPlan,
        on_progress: ProgressCallback | None = None,
        cancel_event: Event | None = None,
    ) -> ChannelResult:
        try:
            _opencli_base()
        except RuntimeError as exc:
            return degraded_result(self.id, str(exc))

        posts: list[Post] = []
        seen_urls: set[str] = set()
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
            try:
                items = _search_notes(keyword)
            except (subprocess.TimeoutExpired, RuntimeError) as exc:
                first_error = first_error or f"关键词「{keyword}」搜索失败：{exc}"
                if on_progress:
                    on_progress(
                        f"小红书：{keyword} 搜索失败",
                        (idx + 1) / max(len(keywords), 1),
                    )
                continue

            for item in items:
                url = str(item.get("url") or "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                pub_date = str(item.get("published_at") or "")[:10]
                if not _in_date_range(pub_date, plan):
                    continue
                keyword_posts.append(
                    Post(
                        id=_note_id_from_url(url),
                        platform="xiaohongshu",
                        keyword=keyword,
                        author=str(item.get("author") or ""),
                        title=str(item.get("title") or ""),
                        content=str(item.get("title") or ""),
                        url=url,
                        timestamp=pub_date,
                        likes=_parse_likes(item.get("likes")),
                        comments_count=0,
                        platform_specific={
                            "author_url": str(item.get("author_url") or ""),
                            "collects": 0,
                            "tags": "",
                        },
                    )
                )
                if len(keyword_posts) >= min(limit, MAX_NOTE_DETAILS_PER_KEYWORD):
                    break
            time.sleep(OPERATION_INTERVAL)

            # 详情（正文 + 互动数据）只对前 N 条拉取
            for post in keyword_posts:
                if cancel_event and cancel_event.is_set():
                    break
                try:
                    detail = _note_detail(post.url)
                except (subprocess.TimeoutExpired, RuntimeError):
                    continue
                if detail.get("content"):
                    post.content = str(detail["content"])
                if detail.get("title"):
                    post.title = str(detail["title"])
                if detail.get("likes"):
                    post.likes = _parse_likes(detail["likes"])
                post.platform_specific["collects"] = _parse_likes(
                    detail.get("collects")
                )
                post.comments_count = _parse_likes(detail.get("comments"))
                if detail.get("tags"):
                    post.platform_specific["tags"] = str(detail["tags"])
                time.sleep(OPERATION_INTERVAL)

            # 评论只对热度最高的 N 条拉取（按点赞降序，优先最热讨论）
            if plan.comments_enabled:
                hot_posts = sorted(
                    keyword_posts,
                    key=lambda p: p.likes,
                    reverse=True,
                )
                for post in hot_posts[:MAX_COMMENT_NOTES_PER_KEYWORD]:
                    if cancel_event and cancel_event.is_set():
                        break
                    try:
                        post.comments = _note_comments(
                            post.url, plan.comments_per_post
                        )
                    except (subprocess.TimeoutExpired, RuntimeError):
                        pass
                    time.sleep(OPERATION_INTERVAL)

            posts.extend(keyword_posts)
            if on_progress:
                on_progress(
                    f"关键词「{keyword}」已采集 {len(keyword_posts)} 条笔记",
                    (idx + 1) / max(len(keywords), 1),
                )

        if not posts:
            return degraded_result(self.id, first_error or "小红书未采集到任何内容")
        if on_progress:
            on_progress(f"小红书采集完成，共 {len(posts)} 条", 1.0)
        return ChannelResult(channel_id=self.id, ok=True, posts=posts)
