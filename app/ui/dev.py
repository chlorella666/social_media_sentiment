"""开发者模式与评测中心入口（main.py 拆分，2026-08-22）。"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# 开发者模式：评测中心入口（默认对小白隐藏）
# ---------------------------------------------------------------------------

EVAL_PORT = 8502
EVAL_URL = f"http://localhost:{EVAL_PORT}"


def _dev_state_path() -> Path:
    state_dir = Path(os.environ.get("SMS_STATE_DIR", str(ROOT / "data" / "state")))
    return state_dir / "dev_mode.json"


def _dev_mode_enabled() -> bool:
    try:
        p = _dev_state_path()
        if p.exists():
            return bool(json.loads(p.read_text(encoding="utf-8")).get("enabled"))
    except (OSError, ValueError):
        pass
    return False


def _set_dev_mode(enabled: bool) -> None:
    try:
        p = _dev_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"enabled": enabled,
                 "updated_at": dt.datetime.now().isoformat(timespec="seconds")},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _eval_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", EVAL_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _start_eval_dashboard() -> None:
    """后台启动评测中心（无窗口），已运行则不重复启动。"""
    if _eval_running():
        return
    cmd = [
        sys.executable, "-m", "streamlit", "run",
        str(ROOT / "app" / "eval_dashboard.py"),
        "--server.port", str(EVAL_PORT),
        "--server.headless", "true",
    ]
    kwargs = {
        "cwd": str(ROOT),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
