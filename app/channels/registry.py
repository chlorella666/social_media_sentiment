"""渠道注册表：新增平台只需注册一个适配器。"""

from __future__ import annotations

from app.channels.base import ChannelAdapter
from app.channels.bilibili import BilibiliChannel
from app.channels.demo import DemoChannel
from app.channels.websearch import WebSearchChannel
from app.channels.weibo import WeiboChannel
from app.channels.xiaohongshu import XiaohongshuChannel

CHANNELS: dict[str, ChannelAdapter] = {
    adapter.id: adapter
    for adapter in (
        DemoChannel(),
        WebSearchChannel(),
        WebSearchChannel("zhihu"),
        WebSearchChannel("tieba"),
        WebSearchChannel("taptap"),
        WeiboChannel(),
        BilibiliChannel(),
        XiaohongshuChannel(),
    )
}


def get_channel(channel_id: str) -> ChannelAdapter:
    if channel_id not in CHANNELS:
        raise KeyError(f"未知渠道: {channel_id}")
    return CHANNELS[channel_id]


def list_channel_infos() -> list[dict]:
    """按优先级排序：演示数据 → 零登录 → 需登录。"""
    order = {
        "demo": 0,
        "bilibili": 1,
        "websearch": 2,
        "websearch_zhihu": 3,
        "websearch_tieba": 4,
        "websearch_taptap": 5,
        "weibo": 6,
        "xiaohongshu": 7,
    }
    infos = [adapter.info() for adapter in CHANNELS.values()]
    return sorted(infos, key=lambda i: order.get(i["id"], 99))
