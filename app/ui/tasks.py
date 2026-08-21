"""main.py 拆分模块（2026-08-22）：后台任务中心与运行页。"""

from __future__ import annotations

from app.ui.constants import (REPORTS_DIR, STEP_DEFS, _STEP_STATE_SVG, _SVG_WRAP)
from app.core import (errors, jobs)
from app.core.models import ReportBundle
from pathlib import Path
import datetime as dt
import html
import json
import shutil
import streamlit as st
import time
from app.ui.wizard import (_prefill_wizard_from_plan, reset_wizard)

def open_task_result(task_id: str) -> tuple[ReportBundle, dict] | None:
    """从任务记录加载完整结果（bundle + 可下载文件字节）。"""
    task = jobs.get_task(task_id)
    if not task or task["status"] != jobs.STATUS_COMPLETED or not task["output_dir"]:
        return None
    out_dir = Path(task["output_dir"])
    result_path = out_dir / "result.json"
    if not result_path.exists():
        return None
    try:
        bundle = ReportBundle.model_validate(
            json.loads(result_path.read_text(encoding="utf-8"))
        )
    except Exception:
        return None
    files = {
        "excel": (out_dir / "result.xlsx").read_bytes()
        if (out_dir / "result.xlsx").exists() else b"",
        "html": (out_dir / "report.html").read_text(encoding="utf-8")
        if (out_dir / "report.html").exists() else "",
    }
    return bundle, files

def render_task_center() -> None:
    """后台任务中心：运行中 + 历史任务（列表 → 详情 → 打开旧报告 / 一键重跑）。"""
    # UX 5.7 性能：任务中心真分页（每页 10 条），不再一次性渲染全量历史
    page_size = 10
    total_tasks = jobs.count_tasks()
    total_pages = max(1, (total_tasks + page_size - 1) // page_size)
    page = min(max(int(st.session_state.get("task_center_page", 1) or 1), 1), total_pages)
    tasks = jobs.list_tasks(limit=page_size, offset=(page - 1) * page_size)
    workers = jobs.active_workers(within_seconds=60)
    if total_tasks == 0:
        # P1-3 空态：零任务时用空态卡（图标 + 为什么没有 + 怎么办）
        _empty_icon = _SVG_WRAP.format(
            body=('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>')
        )
        st.markdown(
            '<div class="empty-state">'
            f'<div class="empty-ico">{_empty_icon}</div>'
            "<b>还没有历史任务</b>"
            "<p>完成一次分析后，报告会保存在这里，可随时回看或重跑。</p>"
            "</div>",
            unsafe_allow_html=True,
        )
        if workers:
            st.caption(
                "后台任务中心 · 后台执行进程：运行中（暂无任务，提交分析后此处显示进度）"
            )
        else:
            st.warning(
                "⚠️ 后台执行进程未运行：新任务将排队等待，"
                "请通过 run.bat 启动后台进程后才会执行。"
            )
        return
    running_n = jobs.count_tasks(statuses=list(jobs.ACTIVE_STATUSES))
    with st.expander(
        f"🗂 后台任务中心（共 {total_tasks} 条 · 运行中 {running_n}）",
        expanded=running_n > 0,
    ):
        if workers:
            st.caption("后台执行进程：运行中 ✅")
        else:
            st.warning(
                "⚠️ 后台执行进程未运行：新任务将排队等待，"
                "请通过 run.bat 启动后台进程后才会执行。"
            )
        if total_pages > 1:
            p1, p2, p3 = st.columns([1, 3, 1])
            if p1.button("← 上一页", disabled=(page <= 1), key="task_page_prev"):
                st.session_state.task_center_page = max(1, page - 1)
                st.rerun()
            p2.caption(f"第 {page} / {total_pages} 页（每页 {page_size} 条）")
            if p3.button("下一页 →", disabled=(page >= total_pages), key="task_page_next"):
                st.session_state.task_center_page = min(total_pages, page + 1)
                st.rerun()
        else:
            st.caption(f"共 {total_tasks} 条历史任务（每页 {page_size} 条）")
        for t in tasks:
            icon = {
                jobs.STATUS_PENDING: "⏳",
                jobs.STATUS_RUNNING: "🔄",
                jobs.STATUS_REVIEWING: "👀",
                jobs.STATUS_COMPLETED: "✅",
                jobs.STATUS_FAILED: "❌",
                jobs.STATUS_CANCELLED: "⛔",
            }.get(t["status"], "•")
            if t["status"] in jobs.ACTIVE_STATUSES:
                state_txt = f"{t['progress_frac'] * 100:.0f}% · {t['message'] or '排队中'}"
            else:
                state_txt = {
                    jobs.STATUS_REVIEWING: "等待人工筛选",
                    jobs.STATUS_COMPLETED: "已完成",
                    jobs.STATUS_FAILED: "失败",
                    jobs.STATUS_CANCELLED: "已取消",
                }.get(t["status"], t["status"])
            c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
            c1.caption(f"{icon} {t['subject'] or '未命名任务'}（{t['created_at']}）— {state_txt}")
            if c2.button("查看", key=f"task_view_{t['id']}"):
                st.session_state.task_id = t["id"]
                if t["status"] == jobs.STATUS_COMPLETED:
                    res = open_task_result(t["id"])
                    if res is None:
                        st.error("结果文件缺失，无法打开")
                        st.stop()
                    st.session_state.bundle, st.session_state.output_files = res
                    st.session_state.stage = 5
                else:
                    st.session_state.stage = 4
                st.rerun()
            if c3.button("详情", key=f"task_detail_{t['id']}"):
                st.session_state.task_detail = t["id"]
                st.rerun()
            if t["status"] not in jobs.ACTIVE_STATUSES and c4.button(
                "删除", key=f"task_del_{t['id']}"
            ):
                st.session_state.confirm_delete = t["id"]
                st.rerun()

        detail_id = st.session_state.get("task_detail")
        if detail_id:
            dtask = next((t for t in tasks if t["id"] == detail_id), None) or jobs.get_task(detail_id)
            if dtask:
                with st.expander(
                    f"📋 任务详情：{dtask['subject'] or '未命名任务'}（{dtask['id']}）",
                    expanded=True,
                ):
                    _render_task_detail(dtask)

        confirm_id = st.session_state.get("confirm_delete")
        if confirm_id:
            target = next((t for t in tasks if t["id"] == confirm_id), None)
            if target:
                st.warning(
                    f"⚠️ 确定删除「{target['subject'] or '未命名任务'}」吗？"
                    "任务记录和对应的报告文件将一并删除，无法恢复。"
                )
                cc1, cc2 = st.columns(2)
                if cc1.button("确认删除", type="primary", key="confirm_del_yes"):
                    out = jobs.delete_task(confirm_id)
                    if out:
                        out_dir = Path(out)
                        reports_root = REPORTS_DIR.resolve()
                        try:
                            if out_dir.resolve().is_relative_to(reports_root):
                                shutil.rmtree(out_dir, ignore_errors=True)
                        except Exception:
                            pass
                    st.session_state.pop("confirm_delete", None)
                    st.rerun()
                if cc2.button("取消", key="confirm_del_no"):
                    st.session_state.pop("confirm_delete", None)
                    st.rerun()

def _render_task_detail(t: dict) -> None:
    """任务详情：元信息 + 计划摘要 + 结果指标 + 打开报告/一键重跑。"""
    status_cn = {
        jobs.STATUS_PENDING: "排队中",
        jobs.STATUS_RUNNING: "执行中",
        jobs.STATUS_REVIEWING: "等待人工筛选",
        jobs.STATUS_COMPLETED: "已完成",
        jobs.STATUS_FAILED: "失败",
        jobs.STATUS_CANCELLED: "已取消",
    }.get(t["status"], t["status"])
    rows = [
        ("任务 ID", t["id"]),
        ("状态", f"{status_cn}（{t['progress_frac'] * 100:.0f}% · {t['message'] or '-'}）"),
        ("创建时间", t["created_at"]),
        ("开始时间", t["started_at"] or "-"),
        ("完成时间", t["finished_at"] or "-"),
    ]
    plan = t.get("plan") or {}
    if plan:
        channels = "、".join(
            c.get("channel_id", "") for c in plan.get("channels", [])
        )
        rows += [
            ("分析对象", plan.get("subject", "")),
            ("关键词数", str(len(plan.get("keywords") or []))),
            ("渠道", channels or "-"),
            (
                "LLM 精分析",
                "开" if plan.get("llm_enabled") else "关（词典模式）",
            ),
            (
                "时间段",
                f"{plan.get('date_start') or '不限'} ~ {plan.get('date_end') or '不限'}",
            ),
        ]
    st.markdown("**任务信息**")
    st.table(rows)

    rs = t.get("result_summary") or {}
    if rs:
        dist = rs.get("sentiment_distribution") or {}
        st.markdown("**结果概览**")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("整体倾向", rs.get("overall_sentiment", "-"))
        m2.metric("平均情感分", f"{rs.get('avg_score', 0):.2f}")
        m3.metric("帖子数", rs.get("total_posts", 0))
        m4.metric("编码文本数", rs.get("total_items", 0))
        m5.metric(
            "负面占比",
            f"{(dist.get('negative', {}).get('ratio', 0) * 100):.1f}%",
        )

    warnings = t.get("warnings") or []
    if warnings:
        st.markdown("**提示**")
        for w in warnings[:8]:
            st.caption(f"- {w}")
    if t.get("error"):
        st.error(f"失败原因：{t['error']}")
    fixes = errors.suggest_fixes(t)
    if fixes:
        with st.expander(
            "💡 如何解决", expanded=t["status"] == jobs.STATUS_FAILED
        ):
            for f in fixes:
                st.markdown(f"- {f}")
    # 4.3 失败恢复：从失败详情直接进入向导并预填原计划（改参数重试）
    if t["status"] == jobs.STATUS_FAILED and st.button(
        "✏️ 修改参数重试", key=f"task_edit_{t['id']}", type="primary"
    ):
        _prefill_wizard_from_plan(t.get("plan") or {})
        st.session_state.stage = 2
        st.rerun()
    usage = t.get("llm_usage") or {}
    if usage.get("prompt_tokens"):
        st.caption(
            f"LLM 用量：输入 {usage.get('prompt_tokens')} / "
            f"输出 {usage.get('completion_tokens')} token，"
            f"约 ¥{usage.get('estimated_cost', 0)}"
        )
    if t.get("output_dir"):
        st.caption(f"报告目录：{t['output_dir']}")
    _fs = t.get("files_status") or ""
    if _fs == "archived":
        st.caption("状态：已归档（原报告移入 archive/，仍可打开）")
    elif _fs == "purged":
        st.caption("状态：报告已按数据管理策略清理（可一键重跑）")

    d1, d2, d3 = st.columns(3)
    if t["status"] == jobs.STATUS_COMPLETED and d1.button(
        "📖 打开旧报告", key=f"task_open_{t['id']}"
    ):
        if _fs == "purged":
            st.error("报告文件已清理，可一键重跑生成新报告")
            st.stop()
        res = open_task_result(t["id"])
        if res is None:
            st.error("结果文件缺失，无法打开")
            st.stop()
        st.session_state.bundle, st.session_state.output_files = res
        st.session_state.stage = 5
        st.rerun()
    if t["status"] != jobs.STATUS_RUNNING and d2.button(
        "🔁 一键重跑", key=f"task_rerun_{t['id']}"
    ):
        new_id = jobs.rerun_task(t["id"])
        if not new_id:
            st.error("重跑失败：原任务计划不可用")
            st.stop()
        st.session_state.task_id = new_id
        st.session_state.rerun_notice = f"已用原计划重新提交任务：{new_id}"
        st.session_state.stage = 4
        st.rerun()
    if t["status"] != jobs.STATUS_RUNNING and d3.button(
        "🗑 删除", key=f"task_detail_del_{t['id']}"
    ):
        st.session_state.confirm_delete = t["id"]
        st.rerun()
    st.divider()
    _render_task_logs(t["id"])

def _render_task_logs(
    task_id: str, max_entries: int = 200, min_level: str | None = None
) -> None:
    """执行日志视图：哪一步、为什么失败（INFO 阶段 / WARNING 降级 / ERROR 失败）。"""
    logs = jobs.list_task_logs(task_id, limit=max(1, int(max_entries)))
    order = {"INFO": 0, "WARNING": 1, "ERROR": 2}
    if min_level:
        threshold = order.get(min_level, 0)
        logs = [l for l in logs if order.get(l.get("level", "INFO"), 0) >= threshold]
    if not logs:
        st.caption("（暂无执行日志）")
        return
    icons = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "❌"}
    with st.expander(
        f"🪵 执行日志（{len(logs)} 条）", expanded=min_level == "ERROR"
    ):
        for l in logs[-max_entries:]:
            level = l.get("level", "INFO")
            line = f"{icons.get(level, '•')} [{l.get('ts', '')}] {l.get('message', '')}"
            if l.get("step"):
                line += f"（步骤：{l['step']}）"
            st.caption(line)
            if level == "ERROR" and l.get("detail"):
                st.code(json.dumps(l["detail"], ensure_ascii=False, indent=2))

def render_running():
    from app.ui.results import _render_review_view  # 避免模块环

    st.subheader("⑤ 后台执行")
    if st.session_state.get("rerun_notice"):
        st.success(st.session_state.pop("rerun_notice"))
    task_id = st.session_state.get("task_id")
    task = jobs.get_task(task_id) if task_id else None
    if not task:
        st.warning("没有正在执行的任务，请重新提交")
        if st.button("返回确认页", key="stage5_back"):
            st.session_state.stage = 3
            st.rerun()
        st.stop()

    status = task["status"]
    if status == jobs.STATUS_COMPLETED:
        res = open_task_result(task_id)
        if res is None:
            st.error("任务已完成，但结果文件缺失，无法打开")
            st.stop()
        st.session_state.bundle, st.session_state.output_files = res
        st.session_state.stage = 5
        st.rerun()

    if status == jobs.STATUS_REVIEWING:
        _render_review_view(task, task_id)
        st.stop()

    if status in jobs.ACTIVE_STATUSES:
        workers = jobs.active_workers(within_seconds=60)
        st.caption("任务在后台执行中：关闭本页面、刷新或重启应用均不会中断任务。")
        if not workers:
            st.warning("⚠️ 后台执行进程未运行：任务处于排队状态，请通过 run.bat 启动后台进程。")
        # 实时风控提示：worker 检测到风控自动冷却时展示
        cooldowns = []
        for l in jobs.list_task_logs(task_id, limit=100):
            if l.get("event") == "cooldown" and l.get("message"):
                if l["message"] not in cooldowns:
                    cooldowns.append(l["message"])
        for m in cooldowns:
            st.warning(f"⚠️ {m}")
        frac = max(0.0, min(float(task["progress_frac"]), 1.0))
        st.progress(frac, text=task["message"] or "排队中…")
        st.caption(f"整体进度：{frac * 100:.0f}%")
        _started = task.get("started_at")
        if _started:
            try:
                _run_mins = (
                    dt.datetime.now() - dt.datetime.fromisoformat(_started)
                ).total_seconds() / 60.0
                st.caption(f"已运行约 {_run_mins:.0f} 分钟")
            except (TypeError, ValueError):
                pass
        snapshot = task.get("step_snapshot") or {}
        step_map = (snapshot.get("steps") or {}) if isinstance(snapshot, dict) else {}
        for sid, label in STEP_DEFS:
            st_data = step_map.get(sid)
            if not st_data:
                _st, _detail = "pending", "等待中"
            else:
                _st = st_data.get("state", "pending")
                _detail = st_data.get("detail") or ""
            _color = {
                "pending": "var(--g-500)",
                "running": "var(--c-brand-hover)",
                "done": "var(--s-success)",
                "skipped": "var(--g-400)",
                "failed": "var(--s-danger)",
            }.get(_st, "var(--g-500)")
            _opacity = "opacity:.45;" if _st == "skipped" else ""
            _row = html.escape(label)
            if _detail:
                _row += f"：{html.escape(_detail)}"
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:8px;'
                f'color:{_color};{_opacity}min-height:26px">'
                f'<span style="display:inline-flex;flex:none">'
                f'{_STEP_STATE_SVG.get(_st, _STEP_STATE_SVG["pending"])}</span>'
                f'<span>{_row}</span></div>',
                unsafe_allow_html=True,
            )
            if sid == "collect":
                channels = (snapshot.get("channels") or {}) if isinstance(snapshot, dict) else {}
                if channels:
                    for cname, cd in channels.items():
                        cdetail = (cd or {}).get("detail") or ""
                        try:
                            cfrac = float((cd or {}).get("frac") or 0)
                        except (TypeError, ValueError):
                            cfrac = 0.0
                        suffix = f"（{cfrac * 100:.0f}%）" if cfrac > 0 else ""
                        st.caption(f"　　{cname}：{cdetail}{suffix}")
        if status in jobs.ACTIVE_STATUSES and not task.get("cancel_requested"):
            if st.button("✋ 取消任务", key="stage5_cancel"):
                jobs.request_cancel(task_id)
                st.rerun()
        elif task.get("cancel_requested"):
            st.caption("已收到取消请求，将在当前阶段结束后停止…")
        time.sleep(1.5)
        st.rerun()

    if status == jobs.STATUS_FAILED:
        step_cn = errors.step_label(task.get("failed_step"))
        st.error(
            f"任务失败：{task.get('error') or '未知错误'}"
            + (f"（失败步骤：{step_cn}）" if task.get("failed_step") else "")
        )
        for f in errors.suggest_fixes(task):
            st.markdown(f"- {f}")
        _render_task_logs(task_id, max_entries=30)
        c1, c2 = st.columns(2)
        if c1.button("← 返回确认页重试", key="stage5_retry"):
            st.session_state.stage = 3
            st.rerun()
        if c2.button("🔄 开始新的分析", key="stage5_restart"):
            reset_wizard()
            st.rerun()

    if status == jobs.STATUS_CANCELLED:
        st.info("任务已取消")
        if st.button("🔄 开始新的分析", key="stage5_cancel_restart"):
            reset_wizard()
            st.rerun()
