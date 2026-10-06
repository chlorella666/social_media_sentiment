"""数据生命周期（1.7）：磁盘占用统计、自动归档、存量修复、显式清理、worker 磁盘预检。

原则：
- 自动只做归档（移动 + tasks.output_dir 单事务同步，可逆）；删除必须显式触发；
- secrets/（API Key/Cookie）与 state/（使用边界确认记录）永不自动触碰，
  app.db 任务/配额状态不自动清理；P1-4 才提供"一键全清含确认记录"；
- dry-run 默认；路径白名单 + 目录名（yyyyMMdd_HHmmss）+ result.json 双重校验；
- 进程级互斥锁（data/.lifecycle.lock，目录原子创建，stale 可接管）；
- 删除优先回收站（send2trash，可选依赖），不可用时回退显式确认删除。

CLI：python app/core/lifecycle.py --dry-run / --apply --archive --purge 90 ...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import send2trash  # type: ignore

    HAS_SEND2TRASH = True
except ImportError:  # 未安装时回退 rmtree（UI 二次确认 + 文案明确不可恢复）
    send2trash = None
    HAS_SEND2TRASH = False

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core import jobs

DATA_ROOT = Path(os.environ.get("SMS_DATA_DIR", str(PROJECT_ROOT / "data")))
REPORTS_DIR = Path(os.environ.get("SMS_REPORTS_DIR", str(DATA_ROOT / "reports")))
ARCHIVE_DIR = REPORTS_DIR / "archive"
LOCK_DIR = DATA_ROOT / ".lifecycle.lock"
LOGS_DIR = DATA_ROOT / "logs"
REGRESSION_DIR = DATA_ROOT / "regression"
DB_PATH = DATA_ROOT / "app.db"
SECRETS_DIR = DATA_ROOT / "secrets"
STATE_DIR = DATA_ROOT / "state"

REPORT_NAME_RE = re.compile(r"^\d{8}_\d{6}$")
LOCK_STALE_SECONDS = 600

# 默认策略（可在 CLI/UI 调整）
KEEP_RECENT = 30          # 报告目录保留最近 N 个
ARCHIVE_MIN_AGE_DAYS = 7  # 归档最小年龄（防与运行中任务竞争）
PURGE_AFTER_DAYS = 90     # 归档删除保留期
LOG_RETENTION_DAYS = 30   # JSONL 日志保留天数
REGRESSION_KEEP_REPORTS = 20
REGRESSION_KEEP_HISTORY_LINES = 500
DISK_MIN_FREE_MB = 500    # worker 磁盘预检阈值
WARN_TOTAL_MB = 2048      # UI 启动横幅阈值

TERMINAL_STATUSES = (jobs.STATUS_COMPLETED, jobs.STATUS_FAILED, jobs.STATUS_CANCELLED)


def _is_report_dir(path: Path) -> bool:
    """报告目录判定：命名 yyyyMMdd_HHmmss 且含 result.json（防误处理无关文件夹）。"""
    return (
        path.is_dir()
        and bool(REPORT_NAME_RE.match(path.name))
        and (path / "result.json").exists()
    )


def _ensure_inside(path: Path, root: Path) -> Path:
    """路径白名单：目标必须解析后仍在根目录内，否则拒绝。"""
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"拒绝越界路径：{path}（根 {root}）") from exc
    return resolved


def _dir_age_days(path: Path, now: datetime | None = None) -> float:
    """目录年龄：取「目录名时间戳」与「文件系统 mtime」两者的**较小值**（向安全侧）。

    为什么取较小值：清理是不可逆操作。目录名可能因系统时钟回拨（虚拟机快照回滚、
    主板电池失效、手动改时间）而看起来"很老"，只信名字会误删刚写出的报告；
    而 mtime 因整目录复制/迁移变新时，后果只是"少清理"，不会误删。
    （2026-10-02 前只信目录名、解析失败才回退 mtime。）

    now 可注入：测试传固定时刻，避免用例依赖真实挂钟（时间腐化）与运行时漂移。
    默认 None → datetime.now()。
    """
    now = now or datetime.now()
    ages: list[float] = []
    if REPORT_NAME_RE.match(path.name):
        try:
            ts = datetime.strptime(path.name, "%Y%m%d_%H%M%S")
            ages.append((now - ts).total_seconds() / 86400.0)
        except ValueError:
            pass
    try:
        ages.append((now.timestamp() - path.stat().st_mtime) / 86400.0)
    except OSError:
        pass
    if not ages:
        return 0.0
    return max(0.0, min(ages))

def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(
        p.stat().st_size
        for p in path.rglob("*")
        if p.is_file()
    )


def _list_children(root: Path) -> list[Path]:
    return [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []


# ---------------------------------------------------------------------------
# 互斥锁
# ---------------------------------------------------------------------------

class LifecycleLock:
    """进程级互斥：目录原子创建即持锁；mtime 超时视为死锁可接管。"""

    def __init__(self, lock_dir: Path | None = None, stale_seconds: int = LOCK_STALE_SECONDS):
        self.lock_dir = lock_dir or LOCK_DIR
        self.stale_seconds = stale_seconds

    def acquire(self) -> bool:
        for _ in range(2):
            try:
                self.lock_dir.mkdir(parents=True)
                return True
            except FileExistsError:
                try:
                    age = time.time() - self.lock_dir.stat().st_mtime
                    if age > self.stale_seconds:
                        self.lock_dir.rmdir()  # 死锁接管
                        continue
                except OSError:
                    pass
                return False
        return False

    def release(self) -> None:
        try:
            self.lock_dir.rmdir()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "LifecycleLock":
        if not self.acquire():
            raise RuntimeError("另一个数据管理任务正在进行中，请稍后再试")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# ---------------------------------------------------------------------------
# 占用统计与可清理清单（P1-4 复用 data_scope）
# ---------------------------------------------------------------------------

def data_scope() -> dict:
    """数据分类：{类别 → {paths, cleanable, reason}}。1.7 只清理 marked 项；
    secrets/state 明确不可清理（P1-4 才一键全清并重新弹确认）。"""
    reports_active = [p for p in _list_children(REPORTS_DIR) if p.name != "archive"]
    archives = _list_children(ARCHIVE_DIR)
    logs = [p for p in LOGS_DIR.glob("*.jsonl")] if LOGS_DIR.is_dir() else []
    regression_reports = _list_children(REGRESSION_DIR)
    misc = sorted(DATA_ROOT.glob("tmp_*.py")) if DATA_ROOT.is_dir() else []
    return {
        "reports_active": {
            "paths": reports_active,
            "cleanable": False,
            "reason": "最新报告，可由「归档旧报告」移入 archive（保留最近 30 个）",
        },
        "reports_archive": {
            "paths": archives,
            "cleanable": True,
            "reason": f"超过 {PURGE_AFTER_DAYS} 天的归档可显式清理（优先回收站）",
        },
        "datasets": {
            "paths": [DATA_ROOT / "datasets", DATA_ROOT / "models", DATA_ROOT / "dicts"],
            "cleanable": False,
            "reason": "黄金集/词典等分析资产，不自动清理",
        },
        "logs": {
            "paths": logs,
            "cleanable": True,
            "reason": f"超过 {LOG_RETENTION_DAYS} 天的 JSONL 轮转文件可清理",
        },
        "regression": {
            "paths": regression_reports,
            "cleanable": True,
            "reason": f"仅保留最近 {REGRESSION_KEEP_REPORTS} 次回归报告",
        },
        "database": {
            "paths": [DB_PATH],
            "cleanable": False,
            "reason": "任务/配额/日志库，不自动清理（删除任务时级联日志）",
        },
        "secrets": {
            "paths": [SECRETS_DIR],
            "cleanable": False,
            "reason": "API Key / Cookie（DPAPI），仅 P1-4 一键清除",
        },
        "state": {
            "paths": [STATE_DIR],
            "cleanable": False,
            "reason": "使用边界确认记录，仅 P1-4 一键清除",
        },
        "other": {
            "paths": misc,
            "cleanable": False,
            "reason": "data/ 根目录杂项（tmp_*.py 等），仅统计不自动处理",
        },
    }


def data_usage() -> dict:
    """按类返回占用字节数 + 总占用。"""
    scope = data_scope()
    categories = {}
    total = 0
    for name, item in scope.items():
        size = sum(_dir_size(p) for p in item["paths"])
        total += size
        categories[name] = {
            "size_bytes": size,
            "cleanable": item["cleanable"],
            "paths": [str(p) for p in item["paths"]],
        }
    return {"categories": categories, "total_bytes": total}


# ---------------------------------------------------------------------------
# 存量失效映射修复（G2）
# ---------------------------------------------------------------------------

def repair_legacy_archive(dry_run: bool = True) -> dict:
    """best-effort 修复存量归档：按目录名匹配 tasks.output_dir 尾部，
    命中则回填 archive 新路径并标记 files_status='archived'；未命中归孤儿。"""
    repaired, orphans = [], []
    for d in _list_children(ARCHIVE_DIR):
        if not _is_report_dir(d):
            continue
        matches = jobs.find_tasks_by_output_tail(d.name)
        if not matches:
            orphans.append(d.name)
            continue
        for task in matches:
            cur = (task.get("output_dir") or "").replace("\\", "/")
            new = str(d).replace("\\", "/")
            if cur != new:
                repaired.append({"task_id": task["id"], "from": cur, "to": new})
                if not dry_run:
                    jobs.update_task_files(task["id"], output_dir=str(d), files_status="archived")
            elif (task.get("files_status") or "") != "archived":
                repaired.append({"task_id": task["id"], "from": cur, "to": new, "only_status": True})
                if not dry_run:
                    jobs.update_task_files(task["id"], files_status="archived")
    return {
        "dry_run": dry_run,
        "repaired_count": len(repaired),
        "repaired": repaired[:50],
        "orphan_count": len(orphans),
        "orphans": orphans[:50],
    }


# ---------------------------------------------------------------------------
# 自动归档（G1/G7/G8）
# ---------------------------------------------------------------------------

def archive_old_reports(
    keep: int = KEEP_RECENT,
    min_age_days: int = ARCHIVE_MIN_AGE_DAYS,
    dry_run: bool = True,
    now: datetime | None = None,
) -> dict:
    """归档条件（双条件 + 终态 + 报告形态）：
    不在最近 keep 个 **且** 超过 min_age_days 天 **且** 目录名/result.json 合规
    **且** 任务为终态（completed/failed/cancelled）且无 collection_path。"""
    active = [p for p in _list_children(REPORTS_DIR) if p.name != "archive"]
    active.sort(key=lambda p: p.name)  # 名字即时间戳，升序 = 旧→新
    keep_names = {p.name for p in active[-keep:]} if active and keep > 0 else set()
    moved, skipped = [], []
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for d in active:
        if not _is_report_dir(d):
            continue
        reason = ""
        if d.name in keep_names:
            reason = "在最近保留范围内"
        elif _dir_age_days(d, now=now) < min_age_days:
            reason = f"未满 {min_age_days} 天"
        else:
            task = jobs.find_task_by_output_dir(str(d))
            if task is not None:
                if task.get("status") not in TERMINAL_STATUSES:
                    reason = "任务非终态（运行中/排队/待人工筛选）"
                elif task.get("collection_path"):
                    reason = "任务含人工筛选快照（collection_path）"
        if reason:
            skipped.append({"dir": d.name, "reason": reason})
            continue
        if dry_run:
            moved.append({"dir": d.name, "to": str(ARCHIVE_DIR / d.name)})
            continue
        dst = ARCHIVE_DIR / d.name
        if dst.exists():
            suffix = 2
            while (ARCHIVE_DIR / f"{d.name}_{suffix}").exists():
                suffix += 1
            dst = ARCHIVE_DIR / f"{d.name}_{suffix}"
        shutil.move(str(d), str(dst))
        task = jobs.find_task_by_output_dir(str(d))  # 移动后原路径已失效，重新按名字查
        if task is not None:
            jobs.update_task_files(task["id"], output_dir=str(dst), files_status="archived")
        moved.append({"dir": d.name, "to": str(dst), "task_id": task["id"] if task else None})
    return {
        "dry_run": dry_run,
        "keep": keep,
        "min_age_days": min_age_days,
        "moved_count": len(moved),
        "moved": moved,
        "skipped_count": len(skipped),
        "skipped": skipped[:50],
        "note": "归档条件：不在最近 N 个 且 超过最小年龄 且 终态任务且无人工筛选快照",
    }


# ---------------------------------------------------------------------------
# 显式清理（G5/G8/G3）
# ---------------------------------------------------------------------------

def _remove_dir_safely(path: Path, use_recycle: bool) -> int:
    """删除目录（已通过白名单校验），优先回收站；返回释放字节数。"""
    _ensure_inside(path, ARCHIVE_DIR)
    size = _dir_size(path)
    if use_recycle and HAS_SEND2TRASH:
        send2trash.send2trash(str(path))
    else:
        shutil.rmtree(str(path))
    return size


def purge_archived(
    older_than_days: int = PURGE_AFTER_DAYS,
    use_recycle: bool = True,
    dry_run: bool = True,
    now: datetime | None = None,
) -> dict:
    """删除超过保留期的归档目录（显式触发）；任务摘要保留并标记 purged。
    删除前重读任务状态，仍非终态则跳过。"""
    deleted, skipped, freed = [], [], 0
    for d in sorted(_list_children(ARCHIVE_DIR)):
        if not _is_report_dir(d):
            continue
        if _dir_age_days(d, now=now) < older_than_days:
            skipped.append({"dir": d.name, "reason": "未达保留期"})
            continue
        task = jobs.find_tasks_by_output_tail(d.name)
        task = task[0] if task else None
        if task is not None and task.get("status") not in TERMINAL_STATUSES:
            skipped.append({"dir": d.name, "reason": "任务非终态（重读确认）"})
            continue
        size = _dir_size(d)
        if dry_run:
            deleted.append({"dir": d.name, "freed_bytes": size})
            freed += size
            continue
        freed += _remove_dir_safely(d, use_recycle=use_recycle)
        deleted.append({"dir": d.name, "freed_bytes": size})
        if task is not None:
            jobs.update_task_files(task["id"], files_status="purged")
    return {
        "dry_run": dry_run,
        "older_than_days": older_than_days,
        "deleted_count": len(deleted),
        "deleted": deleted[:100],
        "skipped_count": len(skipped),
        "skipped": skipped[:50],
        "freed_bytes": freed,
        "note": "清理不可恢复；优先回收站（send2trash）；secrets/state/app.db 永不触碰",
    }


def purge_old_logs(days: int = LOG_RETENTION_DAYS, dry_run: bool = True) -> dict:
    """清理超保留期的 JSONL 轮转日志（不动 SQLite task_logs）。"""
    deleted, freed = [], 0
    if LOGS_DIR.is_dir():
        for f in LOGS_DIR.glob("*.jsonl"):
            if (time.time() - f.stat().st_mtime) / 86400.0 < days:
                continue
            size = f.stat().st_size
            if not dry_run:
                f.unlink()
            deleted.append(f.name)
            freed += size
    return {"dry_run": dry_run, "days": days, "deleted_count": len(deleted),
            "deleted": deleted, "freed_bytes": freed}


def prune_regression(
    keep_reports: int = REGRESSION_KEEP_REPORTS,
    keep_history_lines: int = REGRESSION_KEEP_HISTORY_LINES,
    dry_run: bool = True,
) -> dict:
    """回归报告只留最近 keep_reports 个；history.jsonl 只留最近 keep_history_lines 行。"""
    removed, freed = [], 0
    if REGRESSION_DIR.is_dir():
        dirs = sorted(
            [p for p in REGRESSION_DIR.iterdir() if p.is_dir()],
            key=lambda p: p.name,
        )
        for d in dirs[:-keep_reports] if len(dirs) > keep_reports else []:
            size = _dir_size(d)
            if not dry_run:
                shutil.rmtree(str(d))
            removed.append({"dir": d.name, "freed_bytes": size})
            freed += size
        history = REGRESSION_DIR / "history.jsonl"
        if history.exists():
            lines = history.read_text(encoding="utf-8").splitlines()
            if len(lines) > keep_history_lines:
                if not dry_run:
                    history.write_text(
                        "\n".join(lines[-keep_history_lines:]) + "\n", encoding="utf-8"
                    )
                removed.append({"file": "history.jsonl", "trimmed": len(lines) - keep_history_lines})
    return {"dry_run": dry_run, "removed_count": len(removed), "removed": removed,
            "freed_bytes": freed}


# ---------------------------------------------------------------------------
# worker 磁盘预检（G4）
# ---------------------------------------------------------------------------

def check_disk(min_free_mb: int = DISK_MIN_FREE_MB, path: Path | None = None) -> tuple[bool, str]:
    """磁盘可用空间预检：不足返回 (False, 原因+入口建议)。"""
    target = path or DATA_ROOT
    if not target.exists():
        target = PROJECT_ROOT  # 目录缺失时退回项目根，避免预检本身抛异常
    usage = shutil.disk_usage(str(target))
    free_mb = usage.free / (1024 * 1024)
    if free_mb < min_free_mb:
        return False, (
            f"可用磁盘空间不足 {free_mb:.0f}MB（阈值 {min_free_mb}MB），"
            "可在侧边栏「数据管理」查看占用并归档/清理后重试"
        )
    return True, f"可用磁盘 {free_mb:.0f}MB"


# ---------------------------------------------------------------------------
# 编排与 CLI
# ---------------------------------------------------------------------------

def run_cleanup(
    repair: bool = True,
    archive: bool = True,
    purge_days: int | None = None,
    logs_days: int | None = None,
    regression: bool = True,
    dry_run: bool = True,
) -> dict:
    """带互斥锁的编排入口：修复 → 归档 → （显式）清理。"""
    with LifecycleLock():
        res = {"dry_run": dry_run}
        if repair:
            res["repair"] = repair_legacy_archive(dry_run=dry_run)
        if archive:
            res["archive"] = archive_old_reports(dry_run=dry_run)
        if purge_days:
            res["purge"] = purge_archived(older_than_days=purge_days, dry_run=dry_run)
        if logs_days:
            res["logs"] = purge_old_logs(days=logs_days, dry_run=dry_run)
        if regression:
            res["regression"] = prune_regression(dry_run=dry_run)
        return res


def clear_all_data(dry_run: bool = True, use_recycle: bool = True) -> dict:
    """P1-4 一键清除全部数据：报告（含归档）+ 回归报告 + 轮转日志 +
    任务记录 + 已存 Cookie/Key + 使用边界确认记录（重启后重新确认）。

    删除不可恢复（优先回收站）；演示数据与黄金集等测试夹具不删除。
    """
    freed = 0
    deleted_reports = 0
    with LifecycleLock():
        for d in _list_children(REPORTS_DIR):
            if d.name == "archive":
                continue
            size = _dir_size(d)
            if not dry_run:
                _remove_dir_safely(d, use_recycle=use_recycle)
            freed += size
            deleted_reports += 1
        if ARCHIVE_DIR.is_dir():
            for d in _list_children(ARCHIVE_DIR):
                size = _dir_size(d)
                if not dry_run:
                    _remove_dir_safely(d, use_recycle=use_recycle)
                freed += size
                deleted_reports += 1
        regression = prune_regression(keep_reports=0, dry_run=dry_run)
        logs = purge_old_logs(days=0, dry_run=dry_run)
        tasks_deleted = 0
        if not dry_run:
            for t in jobs.list_tasks(limit=10000):
                jobs.delete_task(t["id"])
                tasks_deleted += 1
        from app.core import plans_store

        plans_count = 0
        feedback_count = 0
        if not dry_run:
            plans_count = plans_store.clear_all()
            from app.core import feedback

            feedback_count = feedback.clear_all()
            from app.core.secrets import clear_api_key, clear_cookie
            from app.core.usage_boundary import reset_ack

            clear_api_key()
            for key in ("weibo", "bilibili", "xiaohongshu", "websearch"):
                try:
                    clear_cookie(key)
                except Exception:
                    pass
            reset_ack()
        return {
            "dry_run": dry_run,
            "reports_deleted": deleted_reports,
            "tasks_deleted": tasks_deleted,
            "plans_deleted": plans_count,
            "feedback_deleted": feedback_count,
            "regression": regression,
            "logs": logs,
            "freed_bytes": freed,
            "note": "已删除全部报告/任务/日志/Cookie/Key/反馈；使用边界确认记录已重置"
                    "；已清空上次计划与命名模板（重启后重新确认）。"
                    "演示数据与黄金集等测试夹具不删除。",
        }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="数据生命周期：占用统计/归档/清理（默认 dry-run）")
    ap.add_argument("--usage", action="store_true", help="仅输出占用统计")
    ap.add_argument("--repair", action="store_true", help="修复存量归档映射")
    ap.add_argument("--archive", action="store_true", help="归档旧报告（保留最近 30）")
    ap.add_argument("--purge", type=int, metavar="DAYS", help="清理超 DAYS 天的归档")
    ap.add_argument("--logs", type=int, metavar="DAYS", help="清理超 DAYS 天的 JSONL 日志")
    ap.add_argument("--regression", action="store_true", help="清理旧回归报告/截断 history")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    ap.add_argument("--no-recycle", action="store_true", help="删除不回回收站（rmtree）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()
    dry_run = not args.apply
    if args.usage or not any([args.repair, args.archive, args.purge, args.logs, args.regression]):
        out = {"dry_run": dry_run, "usage": data_usage()}
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            for name, c in out["usage"]["categories"].items():
                print(f"{name}: {c['size_bytes'] / 1048576:.1f} MB（可清理={c['cleanable']}）")
            print(f"总计：{out['usage']['total_bytes'] / 1048576:.1f} MB")
        return 0
    if not dry_run and (args.purge or args.logs or args.regression):
        print("⚠ 即将执行不可恢复的清理（归档/修复可逆；清理删除不保留）。")
        print("  确认继续请加 --apply（回收站可用时删除进回收站）。")
    res = run_cleanup(
        repair=args.repair,
        archive=args.archive,
        purge_days=args.purge,
        logs_days=args.logs,
        regression=args.regression,
        dry_run=dry_run,
    )
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        for k, v in res.items():
            if k == "dry_run":
                continue
            print(f"[{k}] {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
