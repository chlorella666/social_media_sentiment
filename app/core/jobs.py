"""SQLite 后台任务队列：任务 / 进度 / 结果索引持久化。

1.1 目标：任务提交后由常驻 worker 执行，关页面任务继续跑；
Web 界面只负责提交与轮询状态，不持有执行权。

设计要点：
- WAL 模式支持"UI 读 + worker 写"并发；
- 状态机：pending -> running -> completed / failed / cancelled；
- 原子抢单（UPDATE ... RETURNING），避免多 worker 重复执行；
- 敏感字段（cookie / API Key）绝不落库，提交前剥离；
- 心跳 + 启动恢复：worker 中断后 running 任务标记 failed（不自动重试，防重复扣费）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.models import AnalysisPlan
from app.core.secrets import load_cookie

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "data" / "app.db"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_REVIEWING = "reviewing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING)

# 渠道配额默认值（每日，按"预计采集条数 = 关键词数 × 渠道上限"计；-1 = 不限）
DEFAULT_QUOTA_LIMITS = {"weibo": 200, "xiaohongshu": 100}
# 风控自动退避：首次 10 分钟，同日连续触发指数翻倍，60 分钟封顶
COOLDOWN_BASE_MINUTES = 10
COOLDOWN_MAX_MINUTES = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL DEFAULT 'analysis',
    status          TEXT NOT NULL DEFAULT 'pending',
    subject         TEXT NOT NULL DEFAULT '',
    plan            TEXT NOT NULL,
    progress_frac   REAL NOT NULL DEFAULT 0,
    message         TEXT NOT NULL DEFAULT '',
    step_snapshot   TEXT NOT NULL DEFAULT '{}',
    worker_id       TEXT,
    heartbeat_at    TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    output_dir      TEXT,
    result_summary  TEXT,
    llm_usage       TEXT,
    warnings        TEXT,
    error           TEXT,
    failed_step     TEXT,
    collection_path TEXT,
    excluded_urls   TEXT,
    excluded_comment_ids TEXT,
    files_status    TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at);
CREATE TABLE IF NOT EXISTS task_logs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  TEXT NOT NULL,
    ts       TEXT NOT NULL,
    level    TEXT NOT NULL,
    step     TEXT,
    event    TEXT,
    message  TEXT NOT NULL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_logs_task_ts ON task_logs(task_id, ts);
CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id   TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_state (
    channel_id    TEXT PRIMARY KEY,
    date          TEXT NOT NULL,
    quota_used    INTEGER NOT NULL DEFAULT 0,
    quota_limit   INTEGER NOT NULL DEFAULT -1,
    paused        INTEGER NOT NULL DEFAULT 0,
    cool_until    TEXT,
    backoff_level INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    updated_at    TEXT NOT NULL
);
"""


def db_path() -> Path:
    """队列数据库路径；测试可用环境变量 SMS_DB_PATH 覆盖。"""
    return Path(os.environ.get("SMS_DB_PATH", str(DEFAULT_DB)))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _connect(db_path_: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path_ or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    _ensure_column(conn, "tasks", "failed_step", "TEXT")
    _ensure_column(conn, "tasks", "collection_path", "TEXT")
    _ensure_column(conn, "tasks", "excluded_urls", "TEXT")
    _ensure_column(conn, "tasks", "excluded_comment_ids", "TEXT")
    _ensure_column(conn, "tasks", "files_status", "TEXT DEFAULT ''")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """轻量迁移：老库缺列时补列（CREATE TABLE IF NOT EXISTS 不会加列）。"""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for key in (
        "plan", "step_snapshot", "result_summary", "llm_usage", "warnings",
        "excluded_urls", "excluded_comment_ids",
    ):
        raw = d.get(key)
        if isinstance(raw, str) and raw:
            try:
                d[key] = json.loads(raw)
            except json.JSONDecodeError:
                pass
    return d


_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_AT_RE = re.compile(r"@[\w\u4e00-\u9fff-]+")
_PHONE_RE = re.compile(r"1[3-9]\d{9}")


def _sanitize_text(text: str, max_len: int) -> str:
    """日志内容脱敏 + 截断：去 URL/@提及/手机号，压空白。"""
    s = _URL_RE.sub("[链接]", text)
    s = _AT_RE.sub("[@提及]", s)
    s = _PHONE_RE.sub("[手机号]", s)
    s = " ".join(s.split())
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def _sanitize_nested(value: Any, max_len: int = 500) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value, max_len)
    if isinstance(value, dict):
        return {k: _sanitize_nested(v, max_len) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_nested(v, max_len) for v in value]
    return value


def _sanitize_detail(detail: Any) -> Any:
    if isinstance(detail, str):
        return _sanitize_text(detail, 2000)
    return _sanitize_nested(detail)


def sanitize_plan(plan: AnalysisPlan) -> dict[str, Any]:
    """计划落库前剥离敏感字段（cookie 等），仅保留执行所需配置。"""
    data = plan.model_dump(mode="json")
    for ch in data.get("channels", []):
        params = ch.get("params") or {}
        if "cookie" in params:
            params.pop("cookie", None)
            ch["params"] = params
    return data


def restore_plan(task: dict[str, Any]) -> AnalysisPlan:
    """从任务记录重建执行计划；微博 Cookie 从 DPAPI secrets 恢复（若有）。"""
    data = json.loads(task["plan"]) if isinstance(task["plan"], str) else task["plan"]
    for ch in data.get("channels", []):
        if ch.get("channel_id") == "weibo":
            params = ch.get("params") or {}
            if not params.get("cookie"):
                cookie = load_cookie("weibo")
                if cookie:
                    params["cookie"] = cookie
                    ch["params"] = params
    return AnalysisPlan.model_validate(data)


# ---------------------------------------------------------------------------
# 任务 CRUD 与队列操作
# ---------------------------------------------------------------------------

def submit_task(plan: AnalysisPlan, task_type: str = "analysis", db_path_: Path | None = None) -> str:
    """提交任务入队（计划敏感字段剥离后入库），返回 task_id。"""
    task_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    now = _now()
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "INSERT INTO tasks (id, type, status, subject, plan, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, task_type, STATUS_PENDING, plan.subject,
             json.dumps(sanitize_plan(plan), ensure_ascii=False), now),
        )
        conn.commit()
    return task_id


def get_task(task_id: str, db_path_: Path | None = None) -> dict[str, Any] | None:
    with closing(_connect(db_path_)) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_dict(row)


def rerun_task(task_id: str, db_path_: Path | None = None) -> str | None:
    """一键重跑：用原任务计划新建任务（凭据由 worker 运行时从 DPAPI/环境变量恢复）。"""
    task = get_task(task_id, db_path_)
    if not task or not task.get("plan"):
        return None
    data = task["plan"] if isinstance(task["plan"], dict) else json.loads(task["plan"])
    try:
        plan = AnalysisPlan.model_validate(data)
    except Exception:
        return None
    return submit_task(plan, task_type=task.get("type") or "analysis", db_path_=db_path_)


def delete_task(task_id: str, db_path_: Path | None = None) -> str | None:
    """删除任务记录（排队中/运行中禁止删除），返回 output_dir 供清理报告文件。"""
    with closing(_connect(db_path_)) as conn:
        row = conn.execute(
            "SELECT status, output_dir FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None or row["status"] in ACTIVE_STATUSES:
            return None
        out = row["output_dir"]
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
        conn.commit()
    return out


def find_task_by_output_dir(
    output_dir: str | Path, db_path_: Path | None = None
) -> dict | None:
    """按报告目录精确查找任务（归档时同步 output_dir 用）。"""
    target = str(output_dir).replace("\\", "/")
    with closing(_connect(db_path_)) as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE output_dir IS NOT NULL"
        ).fetchall()
    for r in row:
        cur = (r["output_dir"] or "").replace("\\", "/")
        if cur == target:
            return dict(r)
    return None


def find_tasks_by_output_tail(
    tail: str, db_path_: Path | None = None
) -> list[dict]:
    """按目录名尾部匹配任务 output_dir（存量归档修复用）。"""
    with closing(_connect(db_path_)) as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE output_dir IS NOT NULL"
        ).fetchall()
    out = []
    for r in rows:
        cur = (r["output_dir"] or "").replace("\\", "/")
        if cur.endswith("/" + tail) or cur == tail:
            out.append(dict(r))
    return out


def update_task_files(
    task_id: str,
    output_dir: str | None = None,
    files_status: str | None = None,
    db_path_: Path | None = None,
) -> bool:
    """单事务更新任务的报告路径/状态（归档、修复、清理共用）。"""
    sets, params = [], []
    if output_dir is not None:
        sets.append("output_dir = ?")
        params.append(str(output_dir))
    if files_status is not None:
        sets.append("files_status = ?")
        params.append(files_status)
    if not sets:
        return False
    params.append(task_id)
    with closing(_connect(db_path_)) as conn:
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params
        )
        conn.commit()
    return cur.rowcount > 0


def list_tasks(
    limit: int = 20,
    statuses: list[str] | None = None,
    db_path_: Path | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM tasks"
    params: list[Any] = []
    if statuses:
        sql += " WHERE status IN (%s)" % ",".join("?" * len(statuses))
        params.extend(statuses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    with closing(_connect(db_path_)) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [d for d in (_row_to_dict(r) for r in rows) if d is not None]


def claim_next_task(worker_id: str, db_path_: Path | None = None) -> dict[str, Any] | None:
    """原子抢单：pending -> running；无任务返回 None。"""
    now = _now()
    with closing(_connect(db_path_)) as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = ?, worker_id = ?, started_at = ?, heartbeat_at = ? "
            "WHERE id = (SELECT id FROM tasks WHERE status = ? ORDER BY created_at LIMIT 1) "
            "RETURNING id",
            (STATUS_RUNNING, worker_id, now, now, STATUS_PENDING),
        )
        row = cur.fetchone()
        if row is None:
            return None
        conn.commit()
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
    return _row_to_dict(task)


def update_task_progress(
    task_id: str,
    frac: float,
    message: str,
    step_snapshot: dict | None = None,
    db_path_: Path | None = None,
) -> None:
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "UPDATE tasks SET progress_frac = ?, message = ?, step_snapshot = ?, heartbeat_at = ? "
            "WHERE id = ? AND status = ?",
            (max(0.0, min(float(frac), 1.0)), message,
             json.dumps(step_snapshot or {}, ensure_ascii=False),
             _now(), task_id, STATUS_RUNNING),
        )
        conn.commit()


def touch_task(task_id: str, worker_id: str, db_path_: Path | None = None) -> None:
    """任务级心跳（长阶段无进度回调时保活）。"""
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "UPDATE tasks SET heartbeat_at = ?, worker_id = ? WHERE id = ? AND status = ?",
            (_now(), worker_id, task_id, STATUS_RUNNING),
        )
        conn.commit()


def finish_task(
    task_id: str,
    output_dir: str,
    result_summary: dict | None = None,
    llm_usage: dict | None = None,
    warnings: list[str] | None = None,
    db_path_: Path | None = None,
) -> None:
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, output_dir = ?, result_summary = ?, llm_usage = ?, "
            "warnings = ?, finished_at = ? WHERE id = ?",
            (STATUS_COMPLETED, output_dir,
             json.dumps(result_summary or {}, ensure_ascii=False),
             json.dumps(llm_usage or {}, ensure_ascii=False),
             json.dumps(warnings or [], ensure_ascii=False),
             _now(), task_id),
        )
        conn.commit()


def fail_task(
    task_id: str,
    error: str,
    failed_step: str | None = None,
    db_path_: Path | None = None,
) -> None:
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, error = ?, failed_step = ?, finished_at = ? "
            "WHERE id = ?",
            (STATUS_FAILED, error[:1000], failed_step, _now(), task_id),
        )
        conn.commit()


def log_event(
    task_id: str,
    level: str,
    step: str | None,
    event: str,
    message: str,
    detail: dict | list | str | None = None,
    db_path_: Path | None = None,
) -> None:
    """任务级结构化日志（SQLite，供结果页/详情页错误视图）。"""
    if not task_id:
        return
    message = _sanitize_text(str(message or ""), 500)
    detail_json = None
    if detail is not None:
        detail_json = json.dumps(_sanitize_detail(detail), ensure_ascii=False)
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "INSERT INTO task_logs (task_id, ts, level, step, event, message, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, _now(), level.upper(), step, event, message, detail_json),
        )
        conn.commit()


def list_task_logs(
    task_id: str, limit: int = 500, db_path_: Path | None = None
) -> list[dict[str, Any]]:
    with closing(_connect(db_path_)) as conn:
        rows = conn.execute(
            "SELECT ts, level, step, event, message, detail FROM task_logs "
            "WHERE task_id = ? ORDER BY id ASC LIMIT ?",
            (task_id, max(1, int(limit))),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        raw = d.get("detail")
        if isinstance(raw, str) and raw:
            try:
                d["detail"] = json.loads(raw)
            except json.JSONDecodeError:
                pass
        out.append(d)
    return out


def mark_cancelled(task_id: str, note: str = "用户取消", db_path_: Path | None = None) -> None:
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (STATUS_CANCELLED, note, _now(), task_id),
        )
        conn.commit()


def mark_reviewing(
    task_id: str, collection_path: str, db_path_: Path | None = None
) -> None:
    """采集+清洗完成，任务进入人工筛选（reviewing）等待用户。"""
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, collection_path = ?, error = NULL, "
            "finished_at = NULL WHERE id = ?",
            (STATUS_REVIEWING, collection_path, task_id),
        )
        conn.commit()


def save_review(
    task_id: str,
    excluded_urls: list[str],
    excluded_comment_ids: list[str],
    db_path_: Path | None = None,
) -> bool:
    """保存人工筛选结果并恢复任务执行（reviewing → pending）。"""
    with closing(_connect(db_path_)) as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None or row["status"] != STATUS_REVIEWING:
            return False
        conn.execute(
            "UPDATE tasks SET excluded_urls = ?, excluded_comment_ids = ?, "
            "status = ?, error = NULL, finished_at = NULL WHERE id = ?",
            (
                json.dumps(list(excluded_urls), ensure_ascii=False),
                json.dumps(list(excluded_comment_ids), ensure_ascii=False),
                STATUS_PENDING,
                task_id,
            ),
        )
        conn.commit()
    return True


def request_cancel(task_id: str, db_path_: Path | None = None) -> None:
    with closing(_connect(db_path_)) as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row and row["status"] == STATUS_PENDING:
            # 排队中直接取消，不等 worker
            conn.execute(
                "UPDATE tasks SET status = ?, error = ?, finished_at = ? "
                "WHERE id = ? AND status = ?",
                (STATUS_CANCELLED, "用户取消（排队中）", _now(), task_id, STATUS_PENDING),
            )
        elif row and row["status"] == STATUS_REVIEWING:
            conn.execute(
                "UPDATE tasks SET status = ?, error = ?, finished_at = ? "
                "WHERE id = ? AND status = ?",
                (STATUS_CANCELLED, "用户取消（人工筛选中）", _now(), task_id, STATUS_REVIEWING),
            )
        elif row and row["status"] == STATUS_RUNNING:
            conn.execute(
                "UPDATE tasks SET cancel_requested = 1 "
                "WHERE id = ? AND status = ?",
                (task_id, STATUS_RUNNING),
            )
        conn.commit()


def is_cancel_requested(task_id: str, db_path_: Path | None = None) -> bool:
    with closing(_connect(db_path_)) as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return bool(row and row["cancel_requested"])


def recover_stale_tasks(stale_seconds: int = 90, db_path_: Path | None = None) -> int:
    """启动/周期恢复：heartbeat 超时的 running 任务标记 failed（可重跑，不自动重试）。"""
    deadline = (datetime.now() - timedelta(seconds=stale_seconds)).isoformat(timespec="seconds")
    with closing(_connect(db_path_)) as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = ?, error = ?, failed_step = ?, finished_at = ? "
            "WHERE status = ? AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
            (STATUS_FAILED, "任务中断：后台执行进程异常退出（可一键重跑）", "worker",
             _now(), STATUS_RUNNING, deadline),
        )
        conn.commit()
        return cur.rowcount


# ---------------------------------------------------------------------------
# worker 心跳与存活探测
# ---------------------------------------------------------------------------

def register_worker(worker_id: str, db_path_: Path | None = None) -> None:
    now = _now()
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO worker_heartbeats (worker_id, started_at, heartbeat_at) "
            "VALUES (?, ?, ?)",
            (worker_id, now, now),
        )
        conn.commit()


def beat_worker(worker_id: str, db_path_: Path | None = None) -> None:
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "UPDATE worker_heartbeats SET heartbeat_at = ? WHERE worker_id = ?",
            (_now(), worker_id),
        )
        conn.commit()


def active_workers(within_seconds: int = 60, db_path_: Path | None = None) -> list[dict[str, Any]]:
    deadline = (datetime.now() - timedelta(seconds=within_seconds)).isoformat(timespec="seconds")
    with closing(_connect(db_path_)) as conn:
        rows = conn.execute(
            "SELECT worker_id, started_at, heartbeat_at FROM worker_heartbeats "
            "WHERE heartbeat_at >= ? ORDER BY heartbeat_at DESC",
            (deadline,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 渠道安全与配额（1.4）
# ---------------------------------------------------------------------------

def channel_state(channel_id: str, db_path_: Path | None = None) -> dict[str, Any]:
    """渠道状态（惰性建行，默认配额取自 DEFAULT_QUOTA_LIMITS）。"""
    with closing(_connect(db_path_)) as conn:
        row = conn.execute(
            "SELECT * FROM channel_state WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if row is None:
            limit = DEFAULT_QUOTA_LIMITS.get(channel_id, -1)
            now = _now()
            conn.execute(
                "INSERT INTO channel_state (channel_id, date, quota_limit, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (channel_id, _today(), limit, now),
            )
            conn.commit()
            return {
                "channel_id": channel_id, "date": _today(), "quota_used": 0,
                "quota_limit": limit, "paused": 0, "cool_until": None,
                "backoff_level": 0, "last_error": None, "updated_at": now,
            }
        return dict(row)


def _maybe_reset_day(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """跨日惰性重置：配额与退避等级归零。"""
    today = _today()
    if row["date"] != today:
        conn.execute(
            "UPDATE channel_state SET date = ?, quota_used = 0, backoff_level = 0 "
            "WHERE channel_id = ?",
            (today, row["channel_id"]),
        )
        return True
    return False


def check_channel_allowed(
    channel_id: str, est_cost: int = 0, db_path_: Path | None = None
) -> tuple[bool, str]:
    """渠道是否可用（暂停 / 冷却 / 配额）；返回 (ok, 不可用原因)。"""
    with closing(_connect(db_path_)) as conn:
        row = conn.execute(
            "SELECT * FROM channel_state WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO channel_state (channel_id, date, quota_limit, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (channel_id, _today(), DEFAULT_QUOTA_LIMITS.get(channel_id, -1), _now()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM channel_state WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        if _maybe_reset_day(conn, row):
            row = conn.execute(
                "SELECT * FROM channel_state WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        if row["cool_until"]:
            if _now() >= row["cool_until"]:
                # 冷却到期：解除并清零退避等级
                conn.execute(
                    "UPDATE channel_state SET cool_until = NULL, backoff_level = 0 "
                    "WHERE channel_id = ?",
                    (channel_id,),
                )
            else:
                reason = (row["last_error"] or "风控限频").strip()
                conn.commit()
                return False, f"风控冷却中（至 {row['cool_until']}）：{reason}"
        if row["paused"]:
            conn.commit()
            return False, "渠道已暂停"
        if int(row["quota_limit"] or -1) >= 0:
            remaining = int(row["quota_limit"]) - int(row["quota_used"] or 0)
            if est_cost > remaining:
                conn.commit()
                return (
                    False,
                    f"今日配额不足（已用 {row['quota_used']}/{row['quota_limit']}，"
                    f"本次还需 {est_cost}）",
                )
        conn.commit()
    return True, ""


def consume_quota(channel_id: str, est_cost: int, db_path_: Path | None = None) -> None:
    """提交任务时预扣配额（预计采集条数）。"""
    if est_cost <= 0:
        return
    with closing(_connect(db_path_)) as conn:
        row = conn.execute(
            "SELECT * FROM channel_state WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO channel_state (channel_id, date, quota_limit, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (channel_id, _today(), DEFAULT_QUOTA_LIMITS.get(channel_id, -1), _now()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM channel_state WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        if _maybe_reset_day(conn, row):
            row = conn.execute(
                "SELECT * FROM channel_state WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        conn.execute(
            "UPDATE channel_state SET quota_used = ?, updated_at = ? WHERE channel_id = ?",
            (int(row["quota_used"] or 0) + est_cost, _now(), channel_id),
        )
        conn.commit()


def refund_quota(channel_id: str, est_cost: int, db_path_: Path | None = None) -> None:
    """任务失败/取消时退还预扣配额（钳 0）。"""
    if est_cost <= 0:
        return
    with closing(_connect(db_path_)) as conn:
        row = conn.execute(
            "SELECT * FROM channel_state WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if row is None:
            return
        conn.execute(
            "UPDATE channel_state SET quota_used = ?, updated_at = ? WHERE channel_id = ?",
            (max(0, int(row["quota_used"] or 0) - est_cost), _now(), channel_id),
        )
        conn.commit()


def settle_quota(
    channel_id: str, est_cost: int, actual: int, db_path_: Path | None = None
) -> None:
    """任务完成后回填校正：quota_used = used - est + actual（跨日不回填，钳 0）。"""
    with closing(_connect(db_path_)) as conn:
        row = conn.execute(
            "SELECT * FROM channel_state WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if row is None:
            return
        if _maybe_reset_day(conn, row):
            conn.commit()
            return
        new_used = max(0, int(row["quota_used"] or 0) - est_cost + max(0, actual))
        conn.execute(
            "UPDATE channel_state SET quota_used = ?, updated_at = ? WHERE channel_id = ?",
            (new_used, _now(), channel_id),
        )
        conn.commit()


def pause_channel(channel_id: str, db_path_: Path | None = None) -> None:
    channel_state(channel_id, db_path_)
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "UPDATE channel_state SET paused = 1, updated_at = ? WHERE channel_id = ?",
            (_now(), channel_id),
        )
        conn.commit()


def resume_channel(channel_id: str, db_path_: Path | None = None) -> None:
    channel_state(channel_id, db_path_)
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "UPDATE channel_state SET paused = 0, updated_at = ? WHERE channel_id = ?",
            (_now(), channel_id),
        )
        conn.commit()


def set_cooldown(
    channel_id: str, reason: str, db_path_: Path | None = None
) -> tuple[int, str]:
    """风控自动退避：按退避等级指数增加冷却时长（10→20→40→60 封顶）。"""
    with closing(_connect(db_path_)) as conn:
        row = conn.execute(
            "SELECT * FROM channel_state WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO channel_state (channel_id, date, quota_limit, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (channel_id, _today(), DEFAULT_QUOTA_LIMITS.get(channel_id, -1), _now()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM channel_state WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        level = int(row["backoff_level"] or 0)
        minutes = min(COOLDOWN_BASE_MINUTES * (2 ** level), COOLDOWN_MAX_MINUTES)
        cool_until = (datetime.now() + timedelta(minutes=minutes)).isoformat(
            timespec="seconds"
        )
        conn.execute(
            "UPDATE channel_state SET cool_until = ?, backoff_level = ?, last_error = ?, "
            "updated_at = ? WHERE channel_id = ?",
            (cool_until, level + 1, str(reason)[:300], _now(), channel_id),
        )
        conn.commit()
    return minutes, cool_until


def clear_cooldown(channel_id: str, db_path_: Path | None = None) -> None:
    """手动解除冷却（误判兜底），同时清零退避等级。"""
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "UPDATE channel_state SET cool_until = NULL, backoff_level = 0, updated_at = ? "
            "WHERE channel_id = ?",
            (_now(), channel_id),
        )
        conn.commit()


def set_quota_limit(
    channel_id: str, limit: int, db_path_: Path | None = None
) -> None:
    channel_state(channel_id, db_path_)
    with closing(_connect(db_path_)) as conn:
        conn.execute(
            "UPDATE channel_state SET quota_limit = ?, updated_at = ? WHERE channel_id = ?",
            (max(-1, int(limit)), _now(), channel_id),
        )
        conn.commit()


def list_channel_states(db_path_: Path | None = None) -> list[dict[str, Any]]:
    with closing(_connect(db_path_)) as conn:
        rows = conn.execute(
            "SELECT * FROM channel_state ORDER BY channel_id"
        ).fetchall()
    return [dict(r) for r in rows]
