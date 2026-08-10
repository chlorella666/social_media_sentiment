"""结构化日志：本地 JSONL 日志文件的统一出口。

1.3 目标：任务运行全链路可定位"哪一步、为什么失败"。
- data/logs/app.jsonl：每行一条 JSON（ts/level/logger/task_id/step/event/message/detail/traceback）；
- RotatingFileHandler（5MB × 3）防日志膨胀；
- init_file_logging() 幂等，worker / 测试重复调用不会叠加 handler。
"""

from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_FILE = Path(os.environ.get("SMS_LOG_FILE", str(PROJECT_ROOT / "data" / "logs" / "app.jsonl")))

_INITED = False


class JsonlFormatter(logging.Formatter):
    """每条日志输出为单行 JSON，异常堆栈进 traceback 字段。"""

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("task_id", "step", "event"):
            val = getattr(record, key, None)
            if val is not None:
                data[key] = val
        detail = getattr(record, "detail", None)
        if detail is not None:
            data["detail"] = detail if isinstance(detail, (dict, list)) else str(detail)
        if record.exc_info:
            data["traceback"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def init_file_logging() -> None:
    """配置 sms 根 logger → app.jsonl（幂等）。"""
    global _INITED
    if _INITED:
        return
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(JsonlFormatter())
    root = logging.getLogger("sms")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    _INITED = True


def get_logger(name: str = "sms") -> logging.Logger:
    return logging.getLogger(name)


def log_task_event(
    task_id: str,
    level: int,
    step: str | None,
    event: str,
    message: str,
    detail: dict | list | str | None = None,
    exc_info=None,
) -> None:
    """记录一条带任务上下文的 JSONL 日志（供本地文件排查）。"""
    logger = get_logger("sms.task")
    logger.log(
        level,
        message,
        extra={
            "task_id": task_id,
            "step": step,
            "event": event,
            "detail": detail,
        },
        exc_info=exc_info,
    )
