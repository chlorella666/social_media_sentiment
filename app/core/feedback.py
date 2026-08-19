"""UX 5.6 反馈闭环：结果页"这条判错了" → SQLite 反馈表 → 评测中心候选池。

字段：text_id / 原文摘录 / 模型判定 / 用户判定 / 原因 / 状态 / 时间。
反馈表放 data/app.db（SQLite state，与任务队列同库），P1-4 一键清除联动清空。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "data" / "app.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    text_id         TEXT NOT NULL,
    text_snippet    TEXT NOT NULL,
    model_sentiment TEXT NOT NULL DEFAULT '',
    user_sentiment  TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_status_created
    ON feedback(status, created_at);
"""


def db_path() -> Path:
    """数据库路径；测试可用环境变量 SMS_DB_PATH 覆盖（与 jobs 同库）。"""
    return Path(os.environ.get("SMS_DB_PATH", str(DEFAULT_DB)))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn


def add_feedback(
    *,
    task_id: str,
    text_id: str,
    text_snippet: str,
    model_sentiment: str,
    user_sentiment: str,
    reason: str = "",
) -> int:
    """新增一条反馈；返回反馈 ID。user_sentiment 限 正/负/中 三值。"""
    if user_sentiment not in ("positive", "neutral", "negative"):
        raise ValueError(f"非法用户判定：{user_sentiment}")
    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT INTO feedback "
            "(task_id, text_id, text_snippet, model_sentiment, user_sentiment, "
            " reason, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
            (
                task_id or "",
                text_id or "",
                (text_snippet or "")[:200],
                model_sentiment or "",
                user_sentiment,
                (reason or "").strip()[:300],
                _now(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_feedback(status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql = "SELECT * FROM feedback"
    params: list[Any] = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    with closing(_connect()) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def mark_processed(fid: int) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE feedback SET status='processed' WHERE id=?", (int(fid),)
        )
        conn.commit()


def delete_feedback(fid: int) -> None:
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM feedback WHERE id=?", (int(fid),))
        conn.commit()


def clear_all() -> int:
    """清空反馈表（P1-4 一键清除联动）。返回删除条数。"""
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM feedback")
        conn.commit()
        return cur.rowcount
