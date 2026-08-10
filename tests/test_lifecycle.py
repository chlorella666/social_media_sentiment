"""数据生命周期单元测试（1.7）。

运行：python tests/test_lifecycle.py
覆盖：占用统计/data_scope、归档双条件 + 非终态/collection_path 拒绝（G1）、
报告形态校验（G8）、存量修复（G2）、超期清理与任务标记、回收站回退（G5）、
并发互斥（G3）、路径越界拒绝、日志/回归清理（G6）、worker 磁盘预检（G4）、
dry-run 无副作用。

全部路径/DB 经 SMS_* 环境变量与模块常量隔离到临时目录，不触碰真实 data/。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sms_lifecycle_test_"))
os.environ["SMS_DB_PATH"] = str(_TMP / "app.db")
os.environ["SMS_DATA_DIR"] = str(_TMP / "data")
os.environ["SMS_REPORTS_DIR"] = str(_TMP / "reports")

from app.core import jobs, lifecycle  # noqa: E402
from app.core.planner import build_plan  # noqa: E402
from app import worker  # noqa: E402


def _scope() -> Path:
    """每个用例独立临时作用域：重建目录 + 重指模块常量 + 独立 DB。"""
    tmp = Path(tempfile.mkdtemp(prefix="sms_life_scope_"))
    data = tmp / "data"
    reports = tmp / "reports"
    (reports / "archive").mkdir(parents=True)
    for name in ("secrets", "state", "logs", "regression", "datasets"):
        (data / name).mkdir(parents=True)
    os.environ["SMS_DB_PATH"] = str(tmp / "app.db")
    os.environ["SMS_DATA_DIR"] = str(data)
    os.environ["SMS_REPORTS_DIR"] = str(reports)
    lifecycle.DATA_ROOT = data
    lifecycle.REPORTS_DIR = reports
    lifecycle.ARCHIVE_DIR = reports / "archive"
    lifecycle.LOGS_DIR = data / "logs"
    lifecycle.REGRESSION_DIR = data / "regression"
    lifecycle.DB_PATH = data / "app.db"
    lifecycle.SECRETS_DIR = data / "secrets"
    lifecycle.STATE_DIR = data / "state"
    lifecycle.LOCK_DIR = data / ".lifecycle.lock"
    return tmp


def _make_report(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text("{}", encoding="utf-8")
    return d


def _insert_task(
    task_id: str,
    status: str,
    output_dir: str = "",
    collection_path: str = "",
    files_status: str = "",
) -> None:
    with closing(jobs._connect()) as conn:
        conn.execute(
            "INSERT INTO tasks (id, status, subject, plan, output_dir, "
            "collection_path, files_status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, status, "自测", "{}", output_dir, collection_path,
             files_status, jobs._now()),
        )
        conn.commit()


def test_data_usage_and_scope() -> None:
    _scope()
    _make_report(lifecycle.REPORTS_DIR, "20260101_000001")
    _make_report(lifecycle.ARCHIVE_DIR, "20250101_000001")
    (lifecycle.LOGS_DIR / "app.jsonl").write_text("x" * 2048, encoding="utf-8")
    (lifecycle.SECRETS_DIR / "llm_api_key.bin").write_bytes(b"\x00" * 128)
    (lifecycle.STATE_DIR / "ack.json").write_text("{}", encoding="utf-8")
    usage = lifecycle.data_usage()
    cats = usage["categories"]
    assert cats["reports_active"]["size_bytes"] >= 2
    assert cats["reports_archive"]["size_bytes"] >= 2
    assert cats["logs"]["size_bytes"] == 2048
    assert cats["secrets"]["size_bytes"] == 128
    assert cats["state"]["size_bytes"] >= 2
    assert cats["secrets"]["cleanable"] is False, "secrets 永不自动清理"
    assert cats["state"]["cleanable"] is False, "确认记录永不自动清理"
    assert usage["total_bytes"] > 0
    scope = lifecycle.data_scope()
    assert set(scope.keys()) == {
        "reports_active", "reports_archive", "datasets", "logs",
        "regression", "database", "secrets", "state", "other",
    }
    print("✓ 占用统计与 data_scope（secrets/state 不可清理） 通过")


def test_archive_skips_non_terminal_and_collection() -> None:
    """G1：非终态任务（running/reviewing 含 collection_path）目录不归档。"""
    _scope()
    names = ["20260601_000001", "20260602_000001", "20260603_000001",
             "20260604_000001", "20260605_000001"]
    for n in names:
        _make_report(lifecycle.REPORTS_DIR, n)
    _insert_task("t_completed", jobs.STATUS_COMPLETED,
                 str(lifecycle.REPORTS_DIR / names[0]))
    _insert_task("t_reviewing", jobs.STATUS_REVIEWING,
                 str(lifecycle.REPORTS_DIR / names[1]),
                 collection_path=str(lifecycle.REPORTS_DIR / names[1] / "collection_snapshot.json"))
    _insert_task("t_running", jobs.STATUS_RUNNING,
                 str(lifecycle.REPORTS_DIR / names[2]))
    res = lifecycle.archive_old_reports(keep=2, min_age_days=0, dry_run=False)
    moved = [m["dir"] for m in res["moved"]]
    assert moved == [names[0]], f"只应归档终态任务目录：{moved}"
    assert (lifecycle.ARCHIVE_DIR / names[0]).exists()
    assert (lifecycle.REPORTS_DIR / names[1]).exists(), "reviewing 目录不得移动"
    assert (lifecycle.REPORTS_DIR / names[2]).exists(), "running 目录不得移动"
    task = jobs.get_task("t_completed")
    assert Path(task["output_dir"]).parent == lifecycle.ARCHIVE_DIR
    assert task["files_status"] == "archived"
    reasons = "；".join(s["reason"] for s in res["skipped"])
    assert "非终态" in reasons and "人工筛选" in reasons
    print("✓ 归档拒绝非终态/含人工筛选快照任务（G1） 通过")


def test_archive_double_condition_and_shape() -> None:
    """G7/G8：双条件（不在最近 N 且 超最小年龄）+ 报告形态校验。"""
    _scope()
    old = _make_report(lifecycle.REPORTS_DIR, "20260101_000001")
    recent = _make_report(lifecycle.REPORTS_DIR, "20260720_000001")
    _make_report(lifecycle.REPORTS_DIR, "20260102_000001")
    wrong_name = lifecycle.REPORTS_DIR / "not_a_report_dir"
    wrong_name.mkdir()
    (wrong_name / "result.json").write_text("{}", encoding="utf-8")
    no_result = lifecycle.REPORTS_DIR / "20260103_000001"
    no_result.mkdir()
    res = lifecycle.archive_old_reports(keep=2, min_age_days=0, dry_run=False)
    assert old.name in [m["dir"] for m in res["moved"]]
    assert recent.name not in [m["dir"] for m in res["moved"]], "最近保留范围内不归档"
    assert wrong_name.exists(), "目录名不合规不处理"
    assert no_result.exists(), "无 result.json 不处理"
    print("✓ 归档双条件 + 目录名/result.json 校验（G7/G8） 通过")


def test_repair_legacy_archive() -> None:
    """G2：存量失效映射回填 + 孤儿统计 + dry-run。"""
    _scope()
    archived = _make_report(lifecycle.ARCHIVE_DIR, "20260701_000001")
    _make_report(lifecycle.ARCHIVE_DIR, "20260702_000002")  # 孤儿
    _insert_task("t_old", jobs.STATUS_COMPLETED,
                 str(lifecycle.REPORTS_DIR / "20260701_000001"))
    dry = lifecycle.repair_legacy_archive(dry_run=True)
    assert dry["repaired_count"] == 1 and dry["orphan_count"] == 1
    task = jobs.get_task("t_old")
    assert Path(task["output_dir"]).parent != lifecycle.ARCHIVE_DIR, "dry-run 不应改动"
    res = lifecycle.repair_legacy_archive(dry_run=False)
    assert res["repaired_count"] == 1 and res["orphan_count"] == 1
    task = jobs.get_task("t_old")
    assert task["output_dir"] == str(archived)
    assert task["files_status"] == "archived"
    print("✓ 存量归档映射修复 + 孤儿统计（G2） 通过")


def test_purge_archived_only_old_and_marks_task() -> None:
    """清理：只删超期归档，任务标记 purged，secrets/state 不动。"""
    _scope()
    old = _make_report(lifecycle.ARCHIVE_DIR, "20260101_000001")
    _make_report(lifecycle.ARCHIVE_DIR, "20260801_000001")
    _insert_task("t_old", jobs.STATUS_COMPLETED, str(old), files_status="archived")
    (lifecycle.SECRETS_DIR / "llm_api_key.bin").write_bytes(b"\x00" * 64)
    (lifecycle.STATE_DIR / "ack.json").write_text("{}", encoding="utf-8")
    res = lifecycle.purge_archived(older_than_days=30, dry_run=False)
    assert res["deleted_count"] == 1 and res["deleted"][0]["dir"] == old.name
    assert not old.exists(), "超期归档应删除"
    assert (lifecycle.ARCHIVE_DIR / "20260801_000001").exists()
    assert jobs.get_task("t_old")["files_status"] == "purged"
    assert (lifecycle.SECRETS_DIR / "llm_api_key.bin").exists(), "secrets 不得触碰"
    assert (lifecycle.STATE_DIR / "ack.json").exists(), "确认记录不得触碰"
    print("✓ 超期归档清理 + purged 标记 + secrets/state 隔离 通过")


def test_purge_dry_run_no_side_effects() -> None:
    _scope()
    old = _make_report(lifecycle.ARCHIVE_DIR, "20260101_000001")
    res = lifecycle.purge_archived(older_than_days=30, dry_run=True)
    assert res["deleted_count"] == 1 and res["freed_bytes"] > 0
    assert old.exists(), "dry-run 不删除"
    print("✓ purge dry-run 无副作用 + 预计释放量 通过")


def test_purge_recycle_bin_and_fallback() -> None:
    """G5：send2trash 优先；不可用时回退 rmtree。"""
    _scope()
    old = _make_report(lifecycle.ARCHIVE_DIR, "20260101_000001")
    sent = []
    original = lifecycle.send2trash
    original_has = lifecycle.HAS_SEND2TRASH
    try:
        fake = type("Fake", (), {})()
        fake.send2trash = lambda p: (sent.append(p), shutil.rmtree(p))
        lifecycle.send2trash = fake
        lifecycle.HAS_SEND2TRASH = True
        lifecycle.purge_archived(older_than_days=30, dry_run=False)
        assert sent and str(old) in sent[0], "应走回收站"
        assert not old.exists()
        old2 = _make_report(lifecycle.ARCHIVE_DIR, "20260102_000001")
        lifecycle.HAS_SEND2TRASH = False
        lifecycle.purge_archived(older_than_days=30, dry_run=False)
        assert not old2.exists(), "无 send2trash 时回退 rmtree"
    finally:
        lifecycle.send2trash = original
        lifecycle.HAS_SEND2TRASH = original_has
    print("✓ 回收站优先 + 回退 rmtree（G5） 通过")


def test_concurrency_lock() -> None:
    """G3：互斥锁；第二个获取被拒；stale 死锁可接管。"""
    _scope()
    lock = lifecycle.LifecycleLock(stale_seconds=600)
    assert lock.acquire() is True
    assert lock.acquire() is False, "已持锁时第二次获取应失败"
    lock.release()
    assert lock.acquire() is True
    lock.release()
    stale = lifecycle.LifecycleLock(stale_seconds=600)
    stale.lock_dir.mkdir(parents=True)
    old = time.time() - 3600
    os.utime(stale.lock_dir, (old, old))
    assert stale.acquire() is True, "stale 死锁应可接管"
    stale.release()
    print("✓ 进程级互斥 + stale 接管（G3） 通过")


def test_path_guard() -> None:
    _scope()
    try:
        lifecycle._ensure_inside(
            lifecycle.REPORTS_DIR.parent.parent / "outside.txt",
            lifecycle.ARCHIVE_DIR,
        )
        raise AssertionError("越界路径应被拒绝")
    except ValueError:
        pass
    ok = lifecycle._ensure_inside(lifecycle.ARCHIVE_DIR / "a", lifecycle.ARCHIVE_DIR)
    assert ok.name == "a"
    print("✓ 路径白名单：越界拒绝 / 根内放行 通过")


def test_logs_and_regression_cleanup() -> None:
    """G6：JSONL 保留天数 + 回归报告/历史截断。"""
    _scope()
    old_log = lifecycle.LOGS_DIR / "app.20260101.jsonl"
    new_log = lifecycle.LOGS_DIR / "app.jsonl"
    old_log.write_text("x" * 100, encoding="utf-8")
    new_log.write_text("y" * 100, encoding="utf-8")
    old_ts = time.time() - 60 * 86400
    os.utime(old_log, (old_ts, old_ts))
    for i in range(25):
        _make_report(lifecycle.REGRESSION_DIR, f"202608{i:02d}_000001")
    history = lifecycle.REGRESSION_DIR / "history.jsonl"
    history.write_text("\n".join(f"{{}}" for _ in range(600)) + "\n", encoding="utf-8")
    res = lifecycle.purge_old_logs(days=30, dry_run=False)
    assert res["deleted_count"] == 1 and not old_log.exists()
    assert new_log.exists()
    rres = lifecycle.prune_regression(keep_reports=20, keep_history_lines=500, dry_run=False)
    assert len(list(lifecycle.REGRESSION_DIR.iterdir())) == 21  # 20 目录 + history
    assert len(history.read_text(encoding="utf-8").splitlines()) == 500
    assert rres["freed_bytes"] > 0
    print("✓ JSONL 保留天数 + 回归报告/历史清理（G6） 通过")


def test_check_disk_precheck() -> None:
    """G4：check_disk 阈值判定 + 建议入口文案。"""
    import shutil

    original = shutil.disk_usage
    try:
        shutil.disk_usage = lambda _p: type("U", (), {"free": 100 * 1024 * 1024})()
        ok, msg = lifecycle.check_disk(min_free_mb=500)
        assert not ok and "数据管理" in msg, msg
        shutil.disk_usage = lambda _p: type("U", (), {"free": 2 * 1024 * 1024 * 1024})()
        ok, _ = lifecycle.check_disk(min_free_mb=500)
        assert ok
        ok_missing, _ = lifecycle.check_disk(path=Path(tempfile.mkdtemp()) / "not_exists")
        assert ok_missing, "路径缺失应回退项目根而不是抛异常"
    finally:
        shutil.disk_usage = original
    print("✓ 磁盘预检阈值/建议入口/缺失路径兜底（G4） 通过")


def test_worker_disk_precheck_fails_task() -> None:
    """G4 端到端：worker 任务开始前磁盘不足 → failed_step=collect。"""
    tmp = _scope()
    worker.REPORTS_DIR = lifecycle.REPORTS_DIR
    plan = build_plan(
        subject="磁盘预检", domain_id=None, dimension_ids=[], keyword_groups=[],
        manual_keywords=["测试 评价"], channel_ids=["demo"], date_start=None,
        date_end=None, per_keyword_limit=3, comments_enabled=False,
        comments_per_post=0, llm_enabled=False, channel_params={},
    )
    tid = jobs.submit_task(plan)
    original = lifecycle.check_disk
    lifecycle.check_disk = lambda **_: (False, "可用磁盘不足，侧边栏「数据管理」可清理")
    try:
        task = jobs.claim_next_task("disk-worker")
        worker.run_task(task, "disk-worker")
    finally:
        lifecycle.check_disk = original
    t = jobs.get_task(tid)
    assert t["status"] == jobs.STATUS_FAILED, t
    assert t["failed_step"] == "collect"
    assert "数据管理" in t["error"]
    assert tmp.exists()
    print("✓ worker 磁盘预检失败路径（G4 端到端） 通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_data_usage_and_scope()
    test_archive_skips_non_terminal_and_collection()
    test_archive_double_condition_and_shape()
    test_repair_legacy_archive()
    test_purge_archived_only_old_and_marks_task()
    test_purge_dry_run_no_side_effects()
    test_purge_recycle_bin_and_fallback()
    test_concurrency_lock()
    test_path_guard()
    test_logs_and_regression_cleanup()
    test_check_disk_precheck()
    test_worker_disk_precheck_fails_task()
    print("数据生命周期测试全部通过 ✅")


if __name__ == "__main__":
    main()
