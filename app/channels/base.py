"""渠道适配器抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from threading import Event
from typing import Callable

from app.core.models import AnalysisPlan, ChannelResult

ProgressCallback = Callable[[str, float], None]  # (message, progress 0-1)


class ChannelAdapter(ABC):
    """每个平台/渠道一个适配器，统一接口、统一数据模型。"""

    id: str = ""
    name: str = ""
    auth_required: bool = False
    applicability: str = ""  # 适用性建议（展示给用户）
    description: str = ""
    demo: bool = False

    @abstractmethod
    def collect(
        self,
        plan: AnalysisPlan,
        on_progress: ProgressCallback | None = None,
        cancel_event: Event | None = None,
    ) -> ChannelResult:
        """执行采集，返回统一格式的 ChannelResult。"""

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
