"""SQLite 后台任务队列单元测试。

运行：python tests/test_job_queue.py
覆盖：提交/抢单原子性/进度/心跳/完成/失败/取消/敏感字段剥离/崩溃恢复/
      worker 端到端（demo 渠道，不依赖网络）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH = Path(tempfile.mkdtemp(prefix="sms_job_test_")) / "test.db"
os.environ["SMS_DB_PATH"] = str(DB_PATH)

from app import worker  # noqa: E402
from app.core import jobs  # noqa: E402
from app.core.planner import build_plan  # noqa: E402

_COUNTER = 0


def fresh_db() -> None:
    """每个用例独立数据库，避免 FIFO 抢单互相干扰。"""
    global _COUNTER
    _COUNTER += 1
    os.environ["SMS_DB_PATH"] = str(
        Path(tempfile.mkdtemp(prefix=f"sms_job_{_COUNTER}_")) / "test.db"
    )


def make_plan(*, with_cookie: bool = False) -> object:
    channel_params = {}
    channel_ids = ["demo"]
    if with_cookie:
        channel_params["weibo"] = {"cookie": "SUB=should_not_persist"}
        channel_ids.append("weibo")
    return build_plan(
        subject="队列自测",
        domain_id=None,
        dimension_ids=[],
        keyword_groups=[],
        manual_keywords=["自测 评价"],
        channel_ids=channel_ids,
        date_start=None,
        date_end=None,
        per_keyword_limit=5,
        comments_enabled=False,
        comments_per_post=0,
        llm_enabled=False,
        channel_params=channel_params,
    )


def test_submit_and_sanitize() -> None:
    fresh_db()
    tid = jobs.submit_task(make_plan(with_cookie=True))
    task = jobs.get_task(tid)
    assert task["status"] == jobs.STATUS_PENDING
    assert task["subject"] == "队列自测"
    assert "cookie" not in json.dumps(task["plan"], ensure_ascii=False), "cookie 泄漏入库"
    listed = jobs.list_tasks(limit=10)
    assert any(t["id"] == tid for t in listed)
    print("✓ 提交 + 敏感字段剥离 通过")


def test_claim_atomicity() -> None:
    fresh_db()
    tid = jobs.submit_task(make_plan())
    first = jobs.claim_next_task("worker-a")
    second = jobs.claim_next_task("worker-b")
    assert first["id"] == tid and first["status"] == jobs.STATUS_RUNNING
    assert first["worker_id"] == "worker-a"
    assert second is None, "同一任务被重复抢单"
    print("✓ 原子抢单 通过")


def test_progress_heartbeat_finish() -> None:
    fresh_db()
    tid = jobs.submit_task(make_plan())
    task = jobs.claim_next_task("worker-a")
    jobs.update_task_progress(tid, 0.42, "清洗中", {"steps": {"clean": {"state": "running"}}})
    got = jobs.get_task(tid)
    assert abs(got["progress_frac"] - 0.42) < 1e-6
    assert got["message"] == "清洗中"
    assert got["step_snapshot"]["steps"]["clean"]["state"] == "running"
    jobs.touch_task(tid, "worker-a")
    jobs.finish_task(tid, r"D:\tmp\out", {"total_posts": 6}, {"cost": 0.1}, ["w"])
    done = jobs.get_task(tid)
    assert done["status"] == jobs.STATUS_COMPLETED
    assert done["output_dir"] == r"D:\tmp\out"
    assert done["result_summary"]["total_posts"] == 6
    assert done["warnings"] == ["w"]
    print("✓ 进度 / 心跳 / 完成落库 通过")


def test_fail_and_cancel() -> None:
    fresh_db()
    t1 = jobs.submit_task(make_plan())
    jobs.claim_next_task("worker-a")
    jobs.fail_task(t1, "boom")
    assert jobs.get_task(t1)["status"] == jobs.STATUS_FAILED
    assert "boom" in jobs.get_task(t1)["error"]

    t2 = jobs.submit_task(make_plan())
    jobs.claim_next_task("worker-a")
    jobs.request_cancel(t2)
    assert jobs.is_cancel_requested(t2)
    jobs.mark_cancelled(t2, "用户取消")
    assert jobs.get_task(t2)["status"] == jobs.STATUS_CANCELLED

    t3 = jobs.submit_task(make_plan())
    jobs.request_cancel(t3)  # 排队中取消 → 直接 cancelled
    assert jobs.get_task(t3)["status"] == jobs.STATUS_CANCELLED
    print("✓ 失败 / 取消 通过")


def test_stale_recovery() -> None:
    fresh_db()
    tid = jobs.submit_task(make_plan())
    jobs.claim_next_task("ghost-worker")
    stale = (datetime.now() - timedelta(seconds=600)).isoformat(timespec="seconds")
    with sqlite3.connect(str(jobs.db_path())) as conn:
        conn.execute("UPDATE tasks SET heartbeat_at=? WHERE id=?", (stale, tid))
    n = jobs.recover_stale_tasks(stale_seconds=90)
    assert n == 1
    assert jobs.get_task(tid)["status"] == jobs.STATUS_FAILED
    print("✓ 崩溃恢复（stale → failed） 通过")


def test_restore_plan_cookie() -> None:
    fresh_db()
    original = jobs.load_cookie
    try:
        jobs.load_cookie = lambda key: "SUB=restored" if key == "weibo" else ""
        plan = make_plan(with_cookie=True)
        tid = jobs.submit_task(plan)
        restored = jobs.restore_plan(jobs.get_task(tid))
        weibo = [c for c in restored.channels if c.channel_id == "weibo"]
        assert weibo and weibo[0].params.get("cookie") == "SUB=restored"
    finally:
        jobs.load_cookie = original
    print("✓ 计划重建 + Cookie 从凭据库恢复 通过")


def test_worker_end_to_end() -> None:
    fresh_db()
    out_root = Path(tempfile.mkdtemp(prefix="sms_worker_out_"))
    worker.REPORTS_DIR = out_root
    tid = jobs.submit_task(make_plan())
    task = jobs.claim_next_task("e2e-worker")
    worker.run_task(task, "e2e-worker")
    done = jobs.get_task(tid)
    assert done["status"] == jobs.STATUS_COMPLETED, done.get("error")
    out = Path(done["output_dir"])
    assert (out / "result.json").exists()
    assert (out / "result.xlsx").exists()
    assert (out / "report.html").exists()
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert "cookie" not in json.dumps(data["plan"], ensure_ascii=False)
    bundle = worker.ReportBundle.model_validate(data)
    assert bundle.summary["total_posts"] > 0
    assert bundle.summary["total_items"] > 0
    assert done["result_summary"]["total_items"] > 0
    logs = jobs.list_task_logs(tid)
    assert any(l["event"] == "started" for l in logs)
    assert any(l["event"] == "completed" for l in logs)
    print(f"✓ worker 端到端：输出 {len(list(out.iterdir()))} 个文件，bundle 可回读")


def test_task_logs() -> None:
    fresh_db()
    tid = jobs.submit_task(make_plan())
    jobs.log_event(tid, "INFO", "system", "started", "任务开始")
    jobs.log_event(
        tid, "WARNING", "collect", "degraded", "渠道降级", {"channel": "weibo"}
    )
    jobs.log_event(tid, "ERROR", "llm", "failed", "执行异常", {"traceback": "tb"})
    logs = jobs.list_task_logs(tid)
    assert len(logs) == 3
    assert logs[0]["level"] == "INFO" and logs[0]["event"] == "started"
    assert logs[1]["detail"]["channel"] == "weibo"
    assert logs[2]["level"] == "ERROR" and logs[2]["step"] == "llm"
    assert jobs.list_task_logs("not-exist") == []
    print("✓ 任务日志写入/查询（级别/步骤/详情 JSON） 通过")


def test_worker_failure_logs() -> None:
    """失败路径：记录失败步骤 + 异常堆栈，任务标记 failed。"""
    fresh_db()
    original_runner = worker.TaskRunner

    class BoomRunner:
        def __init__(self, plan, on_progress=None):
            self.plan = plan
            self.on_progress = on_progress

        def cancel(self) -> None:
            pass

        def run(self, analyzer=None):
            if self.on_progress:
                self.on_progress(
                    "coding", "LLM 批处理", 0.75,
                    {"steps": {"llm": {"state": "failed", "detail": "boom"}}},
                )
            raise RuntimeError("模拟失败：LLM 调用异常")

    worker.TaskRunner = BoomRunner
    try:
        tid = jobs.submit_task(make_plan())
        task = jobs.claim_next_task("fail-worker")
        worker.run_task(task, "fail-worker")
    finally:
        worker.TaskRunner = original_runner

    t = jobs.get_task(tid)
    assert t["status"] == jobs.STATUS_FAILED
    assert t["failed_step"] == "llm", t.get("failed_step")
    assert "模拟失败" in t["error"]
    logs = jobs.list_task_logs(tid)
    errors = [l for l in logs if l["level"] == "ERROR" and l["event"] == "failed"]
    assert errors and errors[0]["step"] == "llm"
    assert "traceback" in errors[0]["detail"], errors[0]["detail"]
    print("✓ 失败路径：failed_step=llm + 异常堆栈入库 通过")


def test_rerun_task() -> None:
    fresh_db()
    tid = jobs.submit_task(make_plan(with_cookie=False))
    new_id = jobs.rerun_task(tid)
    assert new_id and new_id != tid
    original = jobs.get_task(tid)
    rerun = jobs.get_task(new_id)
    assert rerun["status"] == jobs.STATUS_PENDING
    assert rerun["subject"] == original["subject"]
    assert rerun["plan"] == original["plan"], "重跑计划应与原计划一致（凭据不入库）"
    assert jobs.rerun_task("not-exist") is None
    print("✓ 一键重跑（原计划复用，新任务入队） 通过")


def test_delete_cascades_logs() -> None:
    """删除任务时级联清理 task_logs，不留孤儿日志。"""
    fresh_db()
    tid = jobs.submit_task(make_plan())
    jobs.claim_next_task("del-worker")
    jobs.fail_task(tid, "模拟失败", failed_step="llm")
    jobs.log_event(tid, "ERROR", "llm", "failed", "执行异常")
    assert jobs.list_task_logs(tid)
    jobs.delete_task(tid)
    assert jobs.get_task(tid) is None
    assert jobs.list_task_logs(tid) == [], "删除任务后日志应级联清理"
    print("✓ 删除任务级联清理日志 通过")


def test_logging_failure_does_not_break_task() -> None:
    """日志写入故障（SQLite 抛异常）不影响任务执行完成。"""
    fresh_db()
    out_root = Path(tempfile.mkdtemp(prefix="sms_worker_out_"))
    worker.REPORTS_DIR = out_root
    tid = jobs.submit_task(make_plan())
    task = jobs.claim_next_task("log-broken")
    with mock.patch(
        "app.core.jobs.log_event", side_effect=RuntimeError("日志库故障")
    ):
        worker.run_task(task, "log-broken")
    t = jobs.get_task(tid)
    assert t["status"] == jobs.STATUS_COMPLETED, t.get("error")
    print("✓ 日志写入故障不影响任务完成 通过")


def test_log_sanitize_and_truncate() -> None:
    """日志内容脱敏（URL/@/手机号）与截断。"""
    fresh_db()
    tid = jobs.submit_task(make_plan())
    long_msg = "细节" * 400  # 800 字符
    jobs.log_event(
        tid, "INFO", "system", "note",
        f"访问 https://weibo.com/u/123 联系 @小明 电话 13800138000 {long_msg}",
    )
    logs = jobs.list_task_logs(tid)
    msg = logs[0]["message"]
    assert "https://" not in msg and "@小明" not in msg and "13800138000" not in msg
    assert "[链接]" in msg and "[@提及]" in msg and "[手机号]" in msg
    assert len(msg) <= 501, len(msg)
    jobs.log_event(tid, "ERROR", "llm", "failed", "异常", {"traceback": "长堆栈" * 600})
    detail = jobs.list_task_logs(tid)[1]["detail"]
    assert len(json.dumps(detail, ensure_ascii=False)) <= 3000
    print("✓ 日志脱敏与截断 通过")


def test_worker_version_restart() -> None:
    """方案 A：代码版本变化 → worker 主动退出，等待 run.bat 重新拉起。"""
    fresh_db()
    ver_file = Path(tempfile.mkdtemp(prefix="sms_ver_")) / "version.txt"
    ver_file.write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    os.environ["SMS_VERSION_FILE"] = str(ver_file)
    stop = threading.Event()
    t = threading.Thread(
        target=worker.run_loop,
        kwargs={
            "stop_event": stop,
            "poll_interval": 0.05,
            "heartbeat_interval": 0.2,
            "version_check_interval": 0.05,
        },
        daemon=True,
    )
    try:
        t.start()
        time.sleep(0.3)  # 让 worker 启动并读到旧版本
        assert t.is_alive(), "worker 未正常启动"
        ver_file.write_text('__version__ = "0.2.0"\n', encoding="utf-8")
        t.join(timeout=5)
        assert not t.is_alive(), "版本变化后 worker 未主动退出"
        print("✓ 版本自检：代码版本变化 → worker 主动退出 通过")
    finally:
        stop.set()
        os.environ.pop("SMS_VERSION_FILE", None)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_submit_and_sanitize()
    test_claim_atomicity()
    test_progress_heartbeat_finish()
    test_fail_and_cancel()
    test_stale_recovery()
    test_restore_plan_cookie()
    test_worker_end_to_end()
    test_task_logs()
    test_worker_failure_logs()
    test_rerun_task()
    test_delete_cascades_logs()
    test_logging_failure_does_not_break_task()
    test_log_sanitize_and_truncate()
    test_worker_version_restart()
    print("全部任务队列测试通过 ✅")


if __name__ == "__main__":
    main()
