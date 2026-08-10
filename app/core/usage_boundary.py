"""《使用边界》首次启动确认状态管理。

- 内容源：docs/使用边界.md（打包缺失时用内置兜底文案，不白屏）；
- 确认口径：显式版本号（文档头"版本：vX.Y"），仅实质性条款变更才 bump；
- 状态文件：data/state/usage_boundary_ack.json（SMS_STATE_DIR 可覆盖，测试隔离）。
  确认记录与可清理的缓存/报告分开存放，阶段 4"一键清除数据"应显式包含清除确认记录。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from app import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_DIR = PROJECT_ROOT / "data" / "state"
USAGE_BOUNDARY_PATH = PROJECT_ROOT / "docs" / "使用边界.md"
ACK_FILE_NAME = "usage_boundary_ack.json"

_VERSION_RE = re.compile(r"版本[:：]\s*v([0-9.]+)")
DEFAULT_VERSION = "1.0"

FALLBACK_TEXT = """（《使用边界》文档缺失，以下为内置兜底说明）

本工具用于非商业、非攻击性的个人研究与市场舆情观察。请遵守各平台服务条款与频率限制，
采集内容版权归原作者/平台所有，请勿二次传播或商用。
开启 LLM 精分析后，低置信度文本将发送给所选服务商，请勿输入含个人敏感信息的内容。
"""


def state_dir() -> Path:
    """状态目录；测试可用环境变量 SMS_STATE_DIR 覆盖。"""
    return Path(os.environ.get("SMS_STATE_DIR", str(DEFAULT_STATE_DIR)))


def ack_path() -> Path:
    return state_dir() / ACK_FILE_NAME


def doc_version() -> str:
    """从文档头解析版本号；解析失败回退默认版本。"""
    try:
        text = USAGE_BOUNDARY_PATH.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_VERSION
    m = _VERSION_RE.search(text)
    return m.group(1) if m else DEFAULT_VERSION


def boundary_text() -> str:
    """确认页/侧边栏展示的《使用边界》全文；文档缺失时用内置兜底。"""
    try:
        return USAGE_BOUNDARY_PATH.read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_TEXT


def is_acknowledged() -> bool:
    """已按当前文档版本确认过才放行（版本变化需重新确认）。"""
    path = ack_path()
    if not path.exists():
        return False
    try:
        ack = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return ack.get("version") == doc_version()


def acknowledge() -> None:
    """写入确认记录：文档版本 + 确认时间 + 应用版本。"""
    state_dir().mkdir(parents=True, exist_ok=True)
    ack_path().write_text(
        json.dumps(
            {
                "version": doc_version(),
                "acked_at": datetime.now(timezone.utc).isoformat(),
                "app_version": __version__,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def reset_ack() -> None:
    """测试/调试用：清除确认状态（下次启动重新确认）。"""
    ack_path().unlink(missing_ok=True)
