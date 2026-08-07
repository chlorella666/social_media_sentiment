"""演示数据渠道：无需任何登录即可跑通全流程。

按关键词生成四个平台风格的模拟帖子与评论，覆盖正/负/中性，
用于验证向导 → 采集 → 编码 → 报告端到端链路。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from threading import Event

from app.channels.base import ChannelAdapter, ProgressCallback
from app.core.models import AnalysisPlan, ChannelResult, Comment, Post

SAMPLE_TEXTS: dict[str, list[str]] = {
    "positive": [
        "用了几天感觉很不错，{kw}比想象中好很多，推荐给大家！",
        "整体体验超出预期，{kw}性价比非常高，感觉挺良心的，满意。",
        "设计得很用心，{kw}细节到位，用起来很舒服，值得入手。",
        "朋友推荐的果然靠谱，{kw}各方面都很满意，期待后续更新。",
        "真的绝绝子！{kw}质量在线，服务也好，会回购。",
    ],
    "negative": [
        "体验太差了，{kw}跟宣传差距很大，后悔入手，不建议买。",
        "价格有点贵，{kw}感觉不值这个价，性价比太低，有点失望。",
        "用了没几天{kw}就出问题，质量堪忧，客服还敷衍，体验很糟糕。",
        "更新之后{kw}卡顿闪退严重，体验拉胯，希望尽快修复。",
        "这就是割韭菜，{kw}宣传吹得天花乱坠，实际很一般，踩坑了。",
    ],
    "neutral": [
        "刚收到{kw}，还没怎么用，后续看看效果再说。",
        "看了很多{kw}的评测，准备入手试试，先观望一下。",
        "{kw}中规中矩吧，没什么特别惊喜，但也说不上差。",
        "{kw}价格和配置摆在这，按需选择就行，看个人需求。",
        "听朋友提过{kw}，具体怎么样不太清楚，蹲一个评价。",
    ],
}

COMMENT_TEXTS: dict[str, list[str]] = {
    "positive": ["同感，我也觉得不错", "顶一个，确实值得", "好用+1", "支持，希望越来越好"],
    "negative": ["确实，我也遇到了同样的问题", "劝退，别买", "同感，体验很一般", "太失望了"],
    "neutral": ["围观一下", "蹲后续", "每个人感受不一样吧", "看看再说"],
}

DETAILS = [
    "周末在商场里看到的",
    "朋友安利了很久才入手",
    "蹲了一周才等到发货",
    "线下实体店亲自体验过",
    "跟同事一起试用了几天",
    "看测评视频被种草的",
    "首发当天就下单了",
    "用了大概半个月",
    "家里长辈也在用",
    "双十一活动价入手的",
]

PLATFORM_META = {
    "weibo": {"author_prefix": "微博用户", "url": "https://weibo.com/u/demo"},
    "xiaohongshu": {"author_prefix": "小红书用户", "url": "https://www.xiaohongshu.com/explore/demo"},
    "bilibili": {"author_prefix": "B站用户", "url": "https://www.bilibili.com/video/demo"},
    "zhihu": {"author_prefix": "知乎用户", "url": "https://www.zhihu.com/question/demo"},
}


class DemoChannel(ChannelAdapter):
    id = "demo"
    name = "演示数据"
    auth_required = False
    applicability = "无需登录，生成模拟数据用于体验全流程"
    description = "内置模拟数据，展示采集、编码、报告完整链路"
    demo = True

    def collect(
        self,
        plan: AnalysisPlan,
        on_progress: ProgressCallback | None = None,
        cancel_event: Event | None = None,
    ) -> ChannelResult:
        random.seed(42)
        posts: list[Post] = []
        start = plan.date_start or datetime.now().date() - timedelta(days=30)
        end = plan.date_end or datetime.now().date()
        span_days = max((end - start).days, 1)
        platforms = list(PLATFORM_META.keys())

        for idx, keyword in enumerate(plan.keywords):
            if cancel_event and cancel_event.is_set():
                break
            if on_progress:
                on_progress(f"演示数据：正在生成「{keyword}」的模拟内容", (idx + 1) / max(len(plan.keywords), 1))
            for platform in platforms:
                for _ in range(2):
                    sentiment = random.choices(
                        ["positive", "negative", "neutral"], weights=[0.4, 0.35, 0.25]
                    )[0]
                    text = (
                        random.choice(SAMPLE_TEXTS[sentiment]).format(kw=keyword)
                        + "，" + random.choice(DETAILS)
                    )
                    pub_date = start + timedelta(days=random.randint(0, span_days))
                    meta = PLATFORM_META[platform]
                    comments = [
                        Comment(
                            author=f"{meta['author_prefix']}评{i + 1}",
                            text=random.choice(COMMENT_TEXTS[random.choice(["positive", "negative", "neutral"])]),
                            likes=random.randint(0, 500),
                            time=str(pub_date + timedelta(days=1)),
                        )
                        for i in range(random.randint(0, plan.comments_per_post))
                    ]
                    post_id = f"demo_{platform}_{idx}_{len(posts)}"
                    posts.append(
                        Post(
                            id=post_id,
                            platform=platform,
                            keyword=keyword,
                            author=f"{meta['author_prefix']}{idx + 1}",
                            title=f"关于{plan.subject}「{keyword}」的讨论：{random.choice(DETAILS)}",
                            content=text,
                            url=f"{meta['url']}/{post_id}",
                            timestamp=pub_date.isoformat(),
                            likes=random.randint(0, 5000),
                            reposts=random.randint(0, 2000),
                            comments_count=len(comments),
                            comments=comments,
                            platform_specific={"demo": True},
                        )
                    )
        if on_progress:
            on_progress("演示数据采集完成", 1.0)
        return ChannelResult(channel_id=self.id, ok=True, posts=posts)
