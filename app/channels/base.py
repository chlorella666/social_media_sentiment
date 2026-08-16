"""渠道适配器抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
import random
from threading import Event
import time as _time
from typing import Callable

from app.core.models import AnalysisPlan, ChannelResult

ProgressCallback = Callable[[str, float], None]  # (message, progress 0-1)


def jittered_sleep(base: float, ratio: float = 0.3) -> None:
    """按均值抖动后的间隔休眠（防固定节奏被识别为机器行为）。

    实际间隔落在 [base×(1-ratio), base×(1+ratio)] 内，均值≈base，
    总耗时与固定间隔基本一致。
    """
    low = max(0.05, base * (1 - ratio))
    high = base * (1 + ratio)
    _time.sleep(random.uniform(low, high))


class ChannelAdapter(ABC):
    """每个平台/渠道一个适配器，统一接口、统一数据模型。"""

    id: str = ""
    name: str = ""
    auth_required: bool = False
    applicability: str = ""  # 适用性建议（展示给用户）
    description: str = ""
    demo: bool = False
    skip_key: str = "url"  # 补采跳过已采内容的身份键：url 或 id

    @abstractmethod
    def collect(
        self,
        plan: AnalysisPlan,
        on_progress: ProgressCallback | None = None,
        cancel_event: Event | None = None,
        skip_urls: set[str] | None = None,
    ) -> ChannelResult:
        """执行采集，返回统一格式的 ChannelResult。

        skip_urls：已采集内容的身份键集合（按 skip_key 解释），补采时跳过，
        避免同关键词重复拉取第一轮已采内容。
        """

    def info(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "auth_required": self.auth_required,
            "applicability": self.applicability,
            "description": self.description,
            "demo": self.demo,
        }


def degraded_result(channel_id: str, error: str) -> ChannelResult:
    """自动降级：单渠道失败不影响整体流程。"""
    return ChannelResult(channel_id=channel_id, ok=False, error=error, degraded=True)
