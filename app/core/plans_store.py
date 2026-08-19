"""复用与连续性（UX 5.1）：上次计划 + ≤5 个命名模板，SQLite state（app.db）。

- 上次计划：提交任务时自动覆盖保存（脱敏后），向导可"一键恢复"；
- 命名模板：用户主动保存常用分析对象，≤5 个，同名覆盖；
- 敏感字段（Cookie/API Key）绝不落库：保存前剥离 channel_params 中的 cookie。
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.models import AnalysisPlan

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "data" / "app.db"
MAX_TEMPLATES = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_plans (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'template',
    plan       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_saved_plans_kind_updated
    ON saved_plans(kind, updated_at);
"""


def db_path() -> Path:
    """队列数据库路径；测试可用环境变量 SMS_DB_PATH 覆盖（与 jobs 同库）。"""
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


def _strip_sensitive(plan: dict) -> dict:
    """保存前剥离敏感字段：channels[].params / channel_params 中的 cookie 等。"""
    clean = dict(plan)
    channels = []
    for ch in (clean.get("channels") or []):
        ch = dict(ch)
        params = dict(ch.get("params") or {})
        ch["params"] = {k: v for k, v in params.items() if k.lower() != "cookie"}
        channels.append(ch)
    if channels:
        clean["channels"] = channels
    params = clean.get("channel_params") or {}
    stripped = {
        cid: {k: v for k, v in cfg.items() if k.lower() != "cookie"}
        for cid, cfg in params.items()
    }
    clean["channel_params"] = {k: v for k, v in stripped.items() if v}
    return clean


def plan_to_dict(plan: AnalysisPlan | dict) -> dict:
    if isinstance(plan, AnalysisPlan):
        return _strip_sensitive(plan.model_dump(mode="json"))
    return _strip_sensitive(dict(plan))


def save_last_plan(plan: AnalysisPlan | dict) -> None:
    """覆盖保存最近一次计划（提交任务成功后调用；幂等）。"""
    payload = plan_to_dict(plan)
    now = _now()
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO saved_plans (id, name, kind, plan, created_at, updated_at) "
            "VALUES (?, ?, 'last', ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET plan=excluded.plan, updated_at=excluded.updated_at",
            ("__last__", "最近一次计划", json.dumps(payload, ensure_ascii=False), now, now),
        )
        conn.commit()


def load_last_plan() -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT plan, updated_at FROM saved_plans WHERE id='__last__'"
        ).fetchone()
    if row is None:
        return None
    try:
        plan = json.loads(row["plan"])
        plan["_saved_at"] = row["updated_at"]
        return plan
    except (json.JSONDecodeError, KeyError):
        return None


def list_templates() -> list[dict]:
    """命名模板列表（不含上次计划；按更新时间倒序）。"""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, name, plan, updated_at FROM saved_plans "
            "WHERE kind='template' ORDER BY updated_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        try:
            plan = json.loads(r["plan"])
        except json.JSONDecodeError:
            continue
        out.append({
            "id": r["id"],
            "name": r["name"],
            "updated_at": r["updated_at"],
            "plan": plan,
        })
    return out


def save_template(name: str, plan: AnalysisPlan | dict) -> dict:
    """保存命名模板（≤MAX_TEMPLATES；同名覆盖）。返回 {ok, error?, templates}。"""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "模板名不能为空"}
    if len(name) > 30:
        return {"ok": False, "error": "模板名请控制在 30 字以内"}
    payload = plan_to_dict(plan)
    now = _now()
    with closing(_connect()) as conn:
        existing = conn.execute(
            "SELECT id FROM saved_plans WHERE kind='template' AND name=?",
            (name,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE saved_plans SET plan=?, updated_at=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), now, existing["id"]),
            )
            tid = existing["id"]
            action = "updated"
        else:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM saved_plans WHERE kind='template'"
            ).fetchone()["n"]
            if count >= MAX_TEMPLATES:
                return {
                    "ok": False,
                    "error": f"命名模板最多 {MAX_TEMPLATES} 个，请先删除不用的模板",
                }
            tid = f"tmpl_{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO saved_plans (id, name, kind, plan, created_at, updated_at) "
                "VALUES (?, ?, 'template', ?, ?, ?)",
                (tid, name, json.dumps(payload, ensure_ascii=False), now, now),
            )
            action = "created"
        conn.commit()
    return {"ok": True, "id": tid, "action": action, "templates": len(list_templates())}


def load_template(tid: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT plan FROM saved_plans WHERE kind='template' AND id=?", (tid,)
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["plan"])
    except json.JSONDecodeError:
        return None


def delete_template(tid: str) -> None:
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM saved_plans WHERE kind='template' AND id=?", (tid,))
        conn.commit()


def clear_all() -> int:
    """清空上次计划与全部模板（P1-4 一键清除联动）。返回删除条数。"""
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM saved_plans")
        conn.commit()
        return cur.rowcount
