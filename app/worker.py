"""常驻后台执行进程：轮询 SQLite 任务队列并执行分析任务。

启动方式（run.bat 自动执行）：pythonw app/worker.py —— 无窗口、关浏览器不影响。
行为：
- 每 2 秒轮询 pending 任务，单任务顺序执行（本地桌面级规模，避免并发加重平台限频）；
- 进度回调节流写库（阶段切换必写 + 每 1 秒最多一次）；
- 任务级心跳 10 秒一次，worker 级心跳 10 秒一次；
- 启动时把 heartbeat 超时的 running 任务标记 failed（可重跑，不自动重试）；
- 取消为尽力而为：采集阶段立即停，其余阶段跑完当前批次后按取消处理（不落输出）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import uuid
import ctypes
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))  # 支持直接运行 python app/worker.py

from app.core import jobs
from app.core import lifecycle
from app.core import logging_utils
from app.core.errors import is_ratelimit
from app.core.pipeline import TaskRunner
from app.coding.llm_analyzer import create_analyzer
from app.core.models import ChannelResult, Comment, Post, ReportBundle
from app.core.secrets import ensure_legacy_key_migrated, load_api_key
from app.output.excel_writer import build_excel
from app.output.html_report import build_html

REPORTS_DIR = Path(os.environ.get("SMS_REPORTS_DIR", str(PROJECT_ROOT / "data" / "reports")))
PID_FILE = PROJECT_ROOT / "data" / "worker.pid"
VERSION_FILE = Path(
    os.environ.get("SMS_VERSION_FILE", str(PROJECT_ROOT / "app" / "__init__.py"))
)

POLL_INTERVAL = 2.0
HEARTBEAT_INTERVAL = 10.0
PROGRESS_WRITE_INTERVAL = 1.0
CANCEL_POLL_INTERVAL = 2.0
VERSION_CHECK_INTERVAL = 10.0
_VERSION_RE = re.compile(r'__version__\s*=\s*"([^"]+)"')

log = logging_utils.get_logger("sms.worker")


def _pid_alive(pid: int) -> bool:
    """Windows 进程存活探测（OpenProcess + GetExitCodeProcess）。"""
    try:
        handle = ctypes.windll.kernel32.OpenProcess(
            0x1000, False, pid
        )  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(code)
            ):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def _heartbeat_pid(worker_id: str) -> int | None:
    """从 worker_id（格式：<pid>-<uuid>）解析真实进程 PID。"""
    try:
        return int(worker_id.split("-", 1)[0])
    except (ValueError, IndexError):
        return None


def _read_code_version(path: Path | None = None) -> str:
    """读取当前代码版本号（app/__init__.py；测试可用 SMS_VERSION_FILE 覆盖）。"""
    version_path = path or Path(os.environ.get("SMS_VERSION_FILE", str(VERSION_FILE)))
    try:
        text = version_path.read_text(encoding="utf-8")
        m = _VERSION_RE.search(text)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _task_log(
    task_id: str,
    level: str,
    step: str | None,
    event: str,
    message: str,
    detail: dict | list | str | None = None,
    exc_info=None,
) -> None:
    """任务级日志：SQLite（供 UI 错误视图）+ JSONL 文件（供排查）。

    日志写入一律 best-effort：任何日志故障都不能影响任务执行本身。
    """
    try:
        jobs.log_event(task_id, level, step, event, message, detail)
    except Exception:
        pass
    try:
        logging_utils.log_task_event(
            task_id, getattr(logging, level, logging.INFO),
            step, event, message, detail, exc_info,
        )
    except Exception:
        pass


def _warning_step(w: str) -> str | None:
    """warning 文本 → 归属步骤（渠道错误归 collect，LLM 提示归 llm）。"""
    if w.startswith("["):
        return "collect"
    if w.startswith("LLM"):
        return "llm"
    return "system"


def _failed_step_from_snapshot(snapshot: dict | None) -> str | None:
    """从任务清单快照中找出 state=failed 的步骤（哪一步失败）。"""
    try:
        steps = (snapshot or {}).get("steps") or {}
        for sid, s in steps.items():
            if s.get("state") == "failed":
                return sid
    except Exception:
        pass
    return None


def _channel_est(cfg, plan) -> int:
    """渠道预计消耗（与 UI 口径一致：关键词数 × 渠道上限）。"""
    limit = int(cfg.params.get("limit") or plan.per_keyword_limit or 0)
    return max(0, len(plan.keywords) * limit)


def _prepare_channels(plan, task_id: str):
    """渠道预检：剔除暂停/冷却/配额不足的渠道并预扣配额。

    返回 (run_plan, quota_tracked, blocked_texts)；
    全部渠道不可用时 run_plan 为 None。
    """
    run_plan = plan.model_copy(deep=True)
    quota_tracked: list[tuple[str, int]] = []
    blocked: list[str] = []
    for cfg in list(run_plan.channels):
        if cfg.channel_id == "demo":
            continue
        est = _channel_est(cfg, plan)
        ok, reason = jobs.check_channel_allowed(cfg.channel_id, est)
        if not ok:
            run_plan.channels.remove(cfg)
            blocked.append(f"{cfg.channel_id}（{reason}）")
            _task_log(
                task_id, "WARNING", "collect", "channel_skipped",
                f"跳过渠道 {cfg.channel_id}：{reason}",
            )
            continue
        jobs.consume_quota(cfg.channel_id, est)
        quota_tracked.append((cfg.channel_id, est))
    if not run_plan.channels:
        return None, [], blocked
    return run_plan, quota_tracked, blocked


def _settle_channel(channel_id: str, est_cost: int, result) -> dict | None:
    """渠道结算：ok 回填实际；风控失败→冷却+退款；其他失败→退款。返回事件信息。"""
    if result is None:
        jobs.refund_quota(channel_id, est_cost)
        return None
    if result.ok:
        jobs.settle_quota(channel_id, est_cost, len(result.posts or []))
        return None
    jobs.refund_quota(channel_id, est_cost)
    if is_ratelimit(result.error or ""):
        minutes, until = jobs.set_cooldown(channel_id, result.error)
        return {
            "event": "cooldown",
            "message": (
                f"渠道 {channel_id} 检测到风控，已自动冷却 {minutes} 分钟"
                f"（至 {until}），其余渠道继续"
            ),
        }
    return None


def _write_snapshot(task_id: str, plan, res: dict) -> Path:
    """人工筛选快照落盘：清洗后的帖子+评论、LLM 建议标注、启发式丢弃、进度状态。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = REPORTS_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    posts = []
    for p in res["posts"]:
        posts.append(
            {
                "url": p.url,
                "platform": p.platform,
                "keyword": p.keyword,
                "author": p.author,
                "title": p.title,
                "content": p.content,
                "timestamp": p.timestamp,
                "likes": p.likes,
                "comments": [
                    {
                        "id": f"{p.url}::{i}",
                        "author": c.author,
                        "text": c.text,
                        "likes": c.likes,
                        "time": c.time,
                        "is_reply": c.is_reply,
                        "reply_to": c.reply_to,
                        "depth": c.depth,
                    }
                    for i, c in enumerate(p.comments)
                ],
                "llm_relevant": res["llm_relevant_by_url"].get(p.url),
            }
        )
    drops = [d for ch in res["channel_results"] for d in (ch.dropped or [])]
    data = {
        "task_id": task_id,
        "subject": plan.subject,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "step_snapshot": res["tracker_snapshot"],
        "posts": posts,
        "drops": drops,
        "collection_stats": {
            ch.channel_id: ch.collection_stats
            for ch in res["channel_results"]
        },
        "warnings": res["warnings"],
    }
    (out_dir / "collection_snapshot.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_dir


def _load_snapshot(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _apply_exclusions(snapshot: dict, task: dict[str, Any]):
    """按人工筛选结果重建 posts/channel_results：
    帖子剔除（评论随帖级联）+ 单条评论剔除；丢弃记录进入丢弃明细。"""
    excluded_urls = set(task.get("excluded_urls") or [])
    excluded_cids = set(task.get("excluded_comment_ids") or [])
    ad_urls = set(task.get("ad_urls") or [])
    ad_cids = set(task.get("ad_comment_ids") or [])
    kept_posts: list[Post] = []
    manual_drops: list[dict] = []
    for p in snapshot.get("posts", []):
        title = (p.get("title") or p.get("content") or "")[:80]
        comments = p.get("comments") or []
        if p["url"] in excluded_urls:
            manual_drops.append(
                {
                    "platform": p["platform"],
                    "url": p["url"],
                    "title": title,
                    "reason": f"人工筛选：帖子不相关/无意义（{len(comments)} 条评论随帖剔除）",
                    "kind": "quality",
                }
            )
            continue
        kept_comments = [c for c in comments if c["id"] not in excluded_cids]
        if len(kept_comments) != len(comments):
            manual_drops.append(
                {
                    "platform": p["platform"],
                    "url": p["url"],
                    "title": title,
                    "reason": f"人工筛选：评论不相关/无意义（剔除 {len(comments) - len(kept_comments)} 条）",
                    "kind": "quality",
                }
            )
        kept_posts.append(
            Post(
                id=p["url"],
                platform=p["platform"],
                keyword=p.get("keyword", ""),
                author=p.get("author", ""),
                title=p.get("title", ""),
                content=p.get("content", ""),
                url=p["url"],
                timestamp=p.get("timestamp", ""),
                likes=p.get("likes", 0),
                ad_flag=p["url"] in ad_urls,
                comments=[
                    Comment(
                        id=c.get("id", ""),
                        ad_flag=c.get("id") in ad_cids,
                        **{k: v for k, v in c.items() if k not in ("id",)},
                    )
                    for c in kept_comments
                ],
            )
        )

    by_platform: dict[str, list[Post]] = {}
    for p in kept_posts:
        by_platform.setdefault(p.platform, []).append(p)
    base_drops = snapshot.get("drops") or []
    stats_by_channel = snapshot.get("collection_stats") or {}
    channel_results: list[ChannelResult] = []
    for pid, plist in by_platform.items():
        kept_urls = {p.url for p in plist}
        drops = [d for d in base_drops if d.get("platform") == pid or d.get("url") in kept_urls]
        drops.extend(d for d in manual_drops if d["platform"] == pid)
        channel_results.append(
            ChannelResult(
                channel_id=pid, ok=True, posts=plist, dropped=drops,
                collection_stats=stats_by_channel.get(pid) or {},
            )
        )
    warnings = list(snapshot.get("warnings") or [])
    return kept_posts, channel_results, warnings


def _write_outputs(
    bundle: ReportBundle, out_dir: Path | None = None
) -> tuple[Path, dict, dict, list]:
    """结果落盘（Excel / HTML / 完整 JSON），返回目录与结果索引数据。"""
    if out_dir is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = REPORTS_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    excel_bytes = build_excel(bundle).getvalue()
    (out_dir / "result.xlsx").write_bytes(excel_bytes)

    html_content = build_html(bundle)
    (out_dir / "report.html").write_text(html_content, encoding="utf-8")

    data = bundle.model_dump(mode="json")
    data["plan"] = jobs.sanitize_plan(bundle.plan)  # 结果文件同样不落 cookie
    (out_dir / "result.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary = bundle.summary
    index = {
        "total_posts": summary.get("total_posts", 0),
        "total_items": summary.get("total_items", 0),
        "overall_sentiment": summary.get("overall_sentiment", ""),
        "avg_score": summary.get("avg_score", 0.0),
        "sentiment_distribution": summary.get("sentiment_distribution", {}),
    }
    return out_dir, index, bundle.llm_usage, bundle.warnings


def run_task(task: dict[str, Any], worker_id: str) -> None:
    """执行单个任务（含取消监听与心跳保活），全程异常兜底。"""
    task_id = task["id"]
    log.info("领取任务 %s（%s）", task_id, task.get("subject", ""))
    _task_log(
        task_id, "INFO", "system", "started",
        f"任务开始：{task.get('subject') or '未命名'}",
        {
            "channels": [
                c.get("channel_id")
                for c in (task.get("plan") or {}).get("channels", [])
            ],
            "llm_enabled": bool((task.get("plan") or {}).get("llm_enabled")),
        },
    )
    jobs.touch_task(task_id, worker_id)

    stop_monitors = threading.Event()
    runner_ref: dict[str, Any] = {}

    def heartbeat_monitor() -> None:
        while not stop_monitors.wait(HEARTBEAT_INTERVAL):
            jobs.touch_task(task_id, worker_id)

    def cancel_monitor() -> None:
        while not stop_monitors.wait(CANCEL_POLL_INTERVAL):
            if jobs.is_cancel_requested(task_id):
                r = runner_ref.get("runner")
                if r is not None:
                    r.cancel()
                log.info("任务 %s 收到取消请求", task_id)

    threading.Thread(target=heartbeat_monitor, daemon=True).start()
    threading.Thread(target=cancel_monitor, daemon=True).start()

    if jobs.is_cancel_requested(task_id):
        stop_monitors.set()
        jobs.mark_cancelled(task_id, "用户取消（执行前）")
        return

    # 磁盘预检（1.7 G4）：空间不足直接失败并给数据管理入口建议，避免任务中途写满；
    # 预检自身异常不阻塞任务（best-effort）
    try:
        disk_ok, disk_msg = lifecycle.check_disk()
    except Exception:
        disk_ok, disk_msg = True, ""
    if not disk_ok:
        stop_monitors.set()
        _task_log(task_id, "ERROR", "collect", "disk_low", disk_msg)
        jobs.fail_task(task_id, disk_msg, failed_step="collect")
        return

    last_write = 0.0
    last_status: str | None = None
    last_snapshot: dict | None = None

    def on_progress(status, message: str, frac: float, snapshot: dict | None) -> None:
        nonlocal last_write, last_status, last_snapshot
        now = time.monotonic()
        phase_changed = status != last_status
        last_snapshot = snapshot
        if phase_changed:
            _task_log(
                task_id, "INFO", None, "phase", f"阶段：{status} —— {message}"
            )
        failed_step = _failed_step_from_snapshot(snapshot)
        if failed_step:
            _task_log(
                task_id, "ERROR", failed_step, "step_failed",
                f"步骤失败：{failed_step} —— {message}",
            )
        if phase_changed or now - last_write >= PROGRESS_WRITE_INTERVAL:
            last_status = status
            last_write = now
            jobs.update_task_progress(task_id, frac, message, snapshot or {})

    try:
        plan = jobs.restore_plan(task)
    except Exception as exc:
        stop_monitors.set()
        _task_log(
            task_id, "ERROR", "unknown", "failed",
            f"计划解析失败：{exc}", {"traceback": traceback.format_exc()}, exc_info=True,
        )
        jobs.fail_task(task_id, f"计划解析失败：{exc}", failed_step="unknown")
        return

    resume = bool(task.get("collection_path"))
    if resume:
        # 续跑（人工筛选后）：不再重新采集，也不重复检查配额
        run_plan = plan.model_copy(deep=True)
        quota_tracked: list[tuple[str, int]] = []
    else:
        run_plan, quota_tracked, blocked_texts = _prepare_channels(plan, task_id)
        if run_plan is None:
            stop_monitors.set()
            msg = "所选渠道均不可用：" + ("；".join(blocked_texts) or "未选择可用渠道")
            _task_log(task_id, "ERROR", "collect", "no_channels", msg)
            jobs.fail_task(task_id, msg, failed_step="collect")
            return

    try:
        # LLM Key：只从 DPAPI 本机加密存储读取（提交时由向导写入）。
        # 环境变量仅用于开发/评测脚本，不进入应用链路（1.5 决策）。
        api_key = load_api_key(allow_env=False)
        analyzer = create_analyzer(
            api_key=api_key or None,
            base_url=run_plan.llm_base_url or None,
            model=run_plan.llm_model or None,
        )
        runner = TaskRunner(run_plan, on_progress=on_progress)
        runner_ref["runner"] = runner
        if resume:
            # 阶段2：读取人工筛选后的数据，仅编码 + 报告
            snapshot = _load_snapshot(task["collection_path"])
            posts, channel_results, warnings = _apply_exclusions(snapshot, task)
            runner.restore_tracker(snapshot.get("step_snapshot"))
            jobs.update_task_progress(
                task_id, 0.6, "恢复执行：读取已筛选数据",
                snapshot.get("step_snapshot") or {},
            )
            bundle = runner.code_and_report(
                posts, channel_results, warnings, analyzer=analyzer
            )
        elif run_plan.review_enabled:
            # 阶段1：采集 + 清洗后暂停，等待人工筛选
            res = runner.collect_and_clean(analyzer=analyzer, review_mode=True)
            for cid, est in quota_tracked:
                result = next(
                    (r for r in res["channel_results"] if r.channel_id == cid), None
                )
                ev = _settle_channel(cid, est, result)
                if ev:
                    _task_log(task_id, "ERROR", "collect", ev["event"], ev["message"])
            out_dir = _write_snapshot(task_id, run_plan, res)
            jobs.mark_reviewing(task_id, str(out_dir / "collection_snapshot.json"))
            _task_log(
                task_id, "INFO", "clean", "review_pending",
                f"采集完成，等待人工筛选：{len(res['posts'])} 帖待审",
                {"snapshot": str(out_dir / "collection_snapshot.json")},
            )
            stop_monitors.set()
            log.info("任务 %s 进入人工筛选：%s", task_id, out_dir)
            return
        else:
            bundle = runner.run(analyzer=analyzer)
    except Exception as exc:
        stop_monitors.set()
        for cid, est in quota_tracked:
            jobs.refund_quota(cid, est)
        failed_step = _failed_step_from_snapshot(last_snapshot) or last_status or "unknown"
        _task_log(
            task_id, "ERROR", failed_step, "failed",
            f"执行异常：{exc}", {"traceback": traceback.format_exc()}, exc_info=True,
        )
        log.exception("任务 %s 执行失败", task_id)
        jobs.fail_task(task_id, f"执行异常：{exc}", failed_step=failed_step)
        return

    if jobs.is_cancel_requested(task_id):
        stop_monitors.set()
        for cid, est in quota_tracked:
            jobs.refund_quota(cid, est)
        log.info("任务 %s 已取消，跳过输出落盘", task_id)
        _task_log(task_id, "INFO", None, "cancelled", "用户取消，跳过输出落盘")
        jobs.mark_cancelled(task_id, "用户取消（当前阶段结束后停止）")
        return

    try:
        out_dir = Path(task["collection_path"]).parent if resume else None
        out_dir, index, llm_usage, warnings = _write_outputs(bundle, out_dir=out_dir)
    except Exception as exc:
        stop_monitors.set()
        for cid, est in quota_tracked:
            jobs.refund_quota(cid, est)
        _task_log(
            task_id, "ERROR", "report", "output_failed",
            f"输出落盘失败：{exc}", {"traceback": traceback.format_exc()}, exc_info=True,
        )
        log.exception("任务 %s 输出落盘失败", task_id)
        jobs.fail_task(task_id, f"输出落盘失败：{exc}", failed_step="report")
        return

    stop_monitors.set()
    # 渠道结算：ok 回填实际 / 风控失败自动冷却 / 其他失败退款
    for cid, est in quota_tracked:
        result = next(
            (r for r in bundle.channel_results if r.channel_id == cid), None
        )
        ev = _settle_channel(cid, est, result)
        if ev:
            _task_log(task_id, "ERROR", "collect", ev["event"], ev["message"])
    for w in bundle.warnings:
        _task_log(task_id, "WARNING", _warning_step(w), "warning", f"提示：{w}")
    _task_log(
        task_id, "INFO", "system", "completed",
        f"任务完成：{index.get('total_posts', 0)} 帖 / "
        f"{index.get('total_items', 0)} 条编码",
    )
    jobs.finish_task(task_id, str(out_dir), index, llm_usage, warnings)
    log.info("任务 %s 完成：%s", task_id, out_dir)


def run_loop(
    stop_event: threading.Event | None = None,
    poll_interval: float = POLL_INTERVAL,
    heartbeat_interval: float = HEARTBEAT_INTERVAL,
    version_check_interval: float = VERSION_CHECK_INTERVAL,
) -> None:
    """worker 主循环（测试可直接以线程方式调用）。"""
    stop_event = stop_event or threading.Event()
    worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    startup_version = _read_code_version()
    jobs.register_worker(worker_id)
    # 旧明文 Key 一次性迁移：导入 DPAPI 并验证后立即删除明文文件（幂等）
    if ensure_legacy_key_migrated():
        log.info("已迁移旧明文 llm_apikey.txt 到 DPAPI 并删除明文文件")
    log.info("worker 启动：%s", worker_id)

    def beat() -> None:
        while not stop_event.wait(heartbeat_interval):
            jobs.beat_worker(worker_id)

    threading.Thread(target=beat, daemon=True).start()

    recovered = jobs.recover_stale_tasks()
    if recovered:
        log.info("启动恢复：%d 个中断任务标记为 failed", recovered)

    def periodic_recover() -> None:
        """周期恢复：孤儿任务（worker 异常退出、心跳过期）及时标记 failed。"""
        while not stop_event.wait(30):
            n = jobs.recover_stale_tasks()
            if n:
                log.info("周期恢复：%d 个中断任务标记为 failed", n)

    threading.Thread(target=periodic_recover, daemon=True).start()

    last_version_check = 0.0
    while not stop_event.is_set():
        now = time.monotonic()
        if now - last_version_check >= version_check_interval:
            last_version_check = now
            cur_version = _read_code_version()
            if cur_version and cur_version != startup_version:
                log.info(
                    "检测到代码版本变化（%s → %s），worker 主动退出，"
                    "等待 run.bat 重新拉起新版本",
                    startup_version,
                    cur_version,
                )
                break
        task = jobs.claim_next_task(worker_id)
        if task is None:
            stop_event.wait(poll_interval)
            continue
        run_task(task, worker_id)

    log.info("worker 退出：%s", worker_id)


def _run_worker_main() -> None:
    logging_utils.init_file_logging()
    # 防重复启动：以 pidfile 的进程存活为准（崩溃后残留 pid 自动接管，
    # 不依赖心跳窗口，避免崩溃后长时间无法重启）
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            if _pid_alive(old_pid):
                log.info("worker 已在运行（pid=%s），本次退出", old_pid)
                return
            log.info("接管僵尸 pidfile（pid=%s 已不在运行）", old_pid)
            PID_FILE.unlink(missing_ok=True)
        except (OSError, ValueError):  # 僵尸 pidfile → 接管
            PID_FILE.unlink(missing_ok=True)
    # 兜底：pidfile 缺失/错乱时，心跳中仍存活（含启动器包装进程场景）的
    # worker 视为已运行，避免双实例
    for w in jobs.active_workers(within_seconds=60):
        pid = _heartbeat_pid(w.get("worker_id", ""))
        if pid is not None and _pid_alive(pid):
            log.info("检测到其他存活 worker（pid=%s），本次退出", pid)
            return

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    log.info("pid 写入 %s", PID_FILE)
    try:
        run_loop()
    finally:
        PID_FILE.unlink(missing_ok=True)
        log.info("pid 文件已清理")


def _log_launcher_failure(exc: BaseException) -> None:
    """F-004（2026-08-26）：worker 启动失败留痕——pythonw 无声崩溃时，
    data/logs/launcher.log 仍可查原因（黑窗提示与后续排障共用）。"""
    try:
        path = PROJECT_ROOT / "data" / "logs" / "launcher.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] worker 启动失败：{exc}\n"
            )
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass


def main() -> None:
    try:
        _run_worker_main()
    except Exception as exc:
        log.exception("worker 启动失败：%s", exc)
        _log_launcher_failure(exc)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
