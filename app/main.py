"""社交媒体情感分析器 — Streamlit 向导入口。

运行：python -m streamlit run app/main.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from collections import Counter
from pathlib import Path

import streamlit as st

from app import __version__
from app.channels.registry import list_channel_infos
from app.channels import health
from app.core import jobs
from app.core import errors
from app.core import lifecycle
from app.core import usage_boundary
from app.core.models import KeywordGroup, ReportBundle
from app.core.planner import build_plan, flatten_keywords, generate_keyword_groups
from app.core.pricing import estimate_cost
from app.core.secrets import (
    clear_api_key,
    clear_cookie,
    ensure_legacy_key_migrated,
    load_api_key,
    load_cookie,
    save_api_key,
    save_cookie,
)
from app.coding.llm_analyzer import create_analyzer
from app.domains.loader import load_domain, list_domains
from app.core.names import platform_cn
from app.output.html_report import (
    cooccurrence_fig,
    sentiment_sources_fig,
    date_dim_heatmap_fig,
    dimensions_fig,
    heatmap_fig,
    intensity_fig,
    overall_fig,
    platform_dim_fig,
    platform_fig,
    radar_fig,
    trend_fig,
    words_fig,
    wordcloud_png_bytes,
)
from app.output.word_report import build_word

st.set_page_config(page_title="社交媒体情感分析器", page_icon="📊", layout="wide")

ROOT = Path(__file__).resolve().parent.parent

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


# ---------------------------------------------------------------------------
# 首次启动：《使用边界》确认门禁（未确认时只渲染确认页）
# ---------------------------------------------------------------------------

# 旧明文 Key 一次性迁移（幂等；导入并验证后立即删除明文文件）
ensure_legacy_key_migrated()

if not usage_boundary.is_acknowledged():
    st.title("📊 社交媒体情感分析器")
    st.subheader("使用前请阅读并确认《使用边界》")
    st.warning(
        "⚠️ 开启 LLM 精分析后，低置信度文本将发送给所选服务商"
        "（DeepSeek/OpenAI 等），请勿输入含个人敏感信息的内容。"
    )
    with st.expander("《使用边界》全文", expanded=True):
        st.markdown(usage_boundary.boundary_text())
    agreed = st.checkbox("我已阅读并同意《使用边界》", key="usage_boundary_agree")
    if st.button("确认并进入应用", type="primary", width="stretch"):
        if not agreed:
            st.error("请先勾选「我已阅读并同意《使用边界》」")
        else:
            usage_boundary.acknowledge()
            st.rerun()
    st.stop()

# ---------------------------------------------------------------------------
# 数据生命周期（1.7）：存量归档映射一次性修复 + 磁盘占用阈值横幅
# ---------------------------------------------------------------------------

if not st.session_state.get("_lifecycle_repair_done", False):
    try:
        repair_res = lifecycle.repair_legacy_archive(dry_run=False)
        if repair_res["repaired_count"]:
            st.session_state["lifecycle_repair_notice"] = (
                f"已修复 {repair_res['repaired_count']} 个存量归档映射"
            )
        if repair_res.get("orphan_count"):
            st.session_state["lifecycle_orphan_count"] = repair_res["orphan_count"]
    except Exception:
        pass
    st.session_state["_lifecycle_repair_done"] = True

try:
    _usage = lifecycle.data_usage()
    if _usage["total_bytes"] > lifecycle.WARN_TOTAL_MB * 1024 * 1024:
        st.warning(
            f"⚠️ data/ 目录占用 {_usage['total_bytes'] / 1048576:.0f} MB，"
            "建议在侧边栏「数据管理」归档/清理旧报告。"
        )
except Exception:
    pass

REPORTS_DIR = Path(__file__).resolve().parent.parent / "data" / "reports"
SENTIMENT_NAMES = {"positive": "正面", "negative": "负面", "neutral": "中性"}

STEP_DEFS = [
    ("collect", "采集数据"),
    ("clean", "清洗与去重"),
    ("lexicon", "词典预筛"),
    ("llm", "LLM 精分析"),
    ("narrative", "叙事/归因分析"),
    ("report", "生成报告"),
]
STEP_ICONS = {
    "pending": "⏳",
    "running": "🔄",
    "done": "✅",
    "skipped": "⏭",
    "failed": "❌",
}

CHANNEL_LIMIT_DEFAULTS = {
    "demo": 10,
    "bilibili": 20,
    "websearch": 13,
    "websearch_zhihu": 13,
    "websearch_tieba": 13,
    "websearch_taptap": 13,
    "weibo": 20,
    "xiaohongshu": 10,
}
# 各渠道每关键词条数上限：默认值 + 封顶（UI 与渠道层双重限制）。
# 加量建议增加关键词（策略加词），而非调大上限；demo 为确定性演示数据，限额不影响产量。
CHANNEL_LIMIT_MAX = {
    "demo": 200,
    "bilibili": 50,
    "websearch": 13,
    "websearch_zhihu": 13,
    "websearch_tieba": 13,
    "websearch_taptap": 13,
    "weibo": 30,
    "xiaohongshu": 10,
}
CHANNEL_LIMIT_HELP = {
    "bilibili": "B站公开 API、零登录最安全：默认 20、上限 50；加量建议加关键词",
    "weibo": "微博账号级风控最严：默认 20、上限 30，单关键词约 500 条封顶",
    "xiaohongshu": "小红书反爬最严（xsec_token+签名）：默认/封顶 10，单次建议 ≤10",
    "demo": "演示数据固定 2 条/平台/关键词，限额不影响产量",
}
WS_PROBE_STATUS_CN = {
    "ok": "✅ 正常",
    "degraded": "⚠️ 降级",
    "risk": "❌ 风控",
    "error": "❌ 失败",
    "empty": "⚠️ 空结果",
}
HEALTH_LEVEL_CN = {
    "ok": "✅ 可用",
    "warn": "⚠️ 存疑",
    "error": "🔴 不可用",
}
COMMENT_FETCH_SECONDS = 0.3  # 每条评论抓取耗时粗估（阶段 2 按实测校准）


def _diag_badge(rich: dict) -> str:
    """渠道诊断徽标：系统侧（暂停/冷却/配额）优先，其次健康 level。"""
    sysd = rich.get("system") or {}
    if not sysd.get("ok"):
        text = sysd.get("text", "")
        if "暂停" in text:
            return "⏸ 已暂停"
        if "冷却" in text:
            return "⏳ 冷却中"
        if "配额" in text:
            return "🔴 配额不足"
    return HEALTH_LEVEL_CN.get(rich.get("level"), "❓ 未知")


def render_channel_diag(results: dict, query: str, info_map: dict) -> None:
    """渠道诊断面板：轻量层结果表 + WebSearch 深度探针（按需触发）。"""
    rows = []
    for cid, rich in results.items():
        sysd = rich.get("system") or {}
        sys_text = sysd.get("text", "")
        if not sysd.get("ok") and sysd.get("reason"):
            sys_text = f"{sys_text}（{sysd['reason']}）"
        rows.append({
            "渠道": info_map.get(cid, cid),
            "状态": _diag_badge(rich),
            "说明": rich.get("msg", ""),
            "系统状态": sys_text or "—",
        })
    st.table(rows)
    for cid, rich in results.items():
        if not str(cid).startswith("websearch"):
            continue
        probe_key = f"diag_probe_{cid}"
        result_key = f"diag_probe_result_{cid}"
        if st.button("深度探针（360 / bing / 夸克）", key=probe_key):
            from app.channels.websearch import probe_engines

            with st.spinner(f"正在探测三引擎（查询「{query}」，约 10~30 秒）…"):
                st.session_state[result_key] = (query, probe_engines(query))
        saved = st.session_state.get(result_key)
        if saved and saved[0] == query:
            pr = saved[1]
            st.table([
                {
                    "引擎": r["engine"],
                    "状态": WS_PROBE_STATUS_CN.get(r["status"], r["status"]),
                    "结果数": r["items"],
                    "说明": r["message"],
                }
                for r in pr
            ])
            bad = [r for r in pr if r["status"] in ("risk", "error", "degraded", "empty")]
            if bad:
                st.warning(
                    "当前网络下 WebSearch 可能无法正常出数（"
                    + "、".join(f"{r['engine']}：{r['message']}" for r in bad[:3])
                    + "）。建议换网络/代理后再试，或暂时不勾选 WebSearch。"
                )
            else:
                st.success("三引擎均可用，WebSearch 可正常出数。")


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
    show_all = st.session_state.get("task_center_show_all", False)
    tasks = jobs.list_tasks(limit=200 if show_all else 10)
    workers = jobs.active_workers(within_seconds=60)
    if not tasks:
        # 零任务也显示后台进程状态，避免小白首次打开无法确认 worker 是否在运行
        if workers:
            st.caption(
                "🗂 后台任务中心 · 后台执行进程：运行中 ✅"
                "（暂无任务，提交分析后此处显示进度）"
            )
        else:
            st.warning(
                "⚠️ 后台执行进程未运行：新任务将排队等待，"
                "请通过 run.bat 启动后台进程后才会执行。"
            )
        return
    running = [t for t in tasks if t["status"] in jobs.ACTIVE_STATUSES]
    with st.expander(f"🗂 后台任务中心（运行中 {len(running)}）", expanded=bool(running)):
        if workers:
            st.caption("后台执行进程：运行中 ✅")
        else:
            st.warning(
                "⚠️ 后台执行进程未运行：新任务将排队等待，"
                "请通过 run.bat 启动后台进程后才会执行。"
            )
        st.toggle(
            "显示全部历史任务",
            value=show_all,
            key="task_center_show_all",
            help="关闭时只显示最近 10 条",
        )
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
                if t["status"] == jobs.STATUS_COMPLETED:
                    res = open_task_result(t["id"])
                    if res is None:
                        st.error("结果文件缺失，无法打开")
                        st.stop()
                    st.session_state.bundle, st.session_state.output_files = res
                    st.session_state.stage = 6
                else:
                    st.session_state.task_id = t["id"]
                    st.session_state.stage = 5
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
        st.session_state.stage = 6
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
        st.session_state.stage = 5
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


REVIEW_PAGE_SIZE = 20


def _review_key(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.md5(value.encode('utf-8')).hexdigest()[:12]}"


def _render_review_view(task: dict, task_id: str) -> None:
    """人工相关性筛选：二级结构（帖子→评论），帖子级联 + 单条评论剔除。"""
    st.subheader("👀 人工相关性筛选")
    path = task.get("collection_path")
    if not path or not Path(path).exists():
        st.error("筛选数据缺失，无法继续")
        if st.button("✋ 取消任务", key=f"review_cancel_missing_{task_id}"):
            jobs.request_cancel(task_id)
            st.rerun()
        st.stop()
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    posts = snapshot.get("posts", [])

    url_key = f"review_urls_{task_id}"
    cid_key = f"review_cids_{task_id}"
    page_key = f"review_page_{task_id}"
    ad_url_key = f"review_ad_urls_{task_id}"
    ad_cid_key = f"review_ad_cids_{task_id}"
    if url_key not in st.session_state:
        st.session_state[url_key] = set(task.get("excluded_urls") or [])
    if cid_key not in st.session_state:
        st.session_state[cid_key] = set(task.get("excluded_comment_ids") or [])
    if ad_url_key not in st.session_state:
        st.session_state[ad_url_key] = set(task.get("ad_urls") or [])
    if ad_cid_key not in st.session_state:
        st.session_state[ad_cid_key] = set(task.get("ad_comment_ids") or [])
    url_set: set[str] = st.session_state[url_key]
    cid_set: set[str] = st.session_state[cid_key]
    ad_url_set: set[str] = st.session_state[ad_url_key]
    ad_cid_set: set[str] = st.session_state[ad_cid_key]

    st.caption("LLM 判定仅为建议徽标，最终以人工为准；帖子标记不相关后其评论随帖剔除。")
    excluded_post_comments = sum(
        len(p.get("comments") or []) for p in posts if p["url"] in url_set
    )
    remaining_posts = len(posts) - len(url_set)
    remaining_comments = sum(
        len([c for c in p.get("comments", []) if c["id"] not in cid_set])
        for p in posts
        if p["url"] not in url_set
    )
    st.info(
        f"已剔除 {len(url_set)} 帖（含随帖评论 {excluded_post_comments} 条）· "
        f"手动剔除评论 {len(cid_set)} 条；筛选后剩余 {remaining_posts} 帖 / "
        f"{remaining_comments} 条评论"
    )

    c1, c2, c3 = st.columns([2, 2, 2])
    platform = c1.selectbox(
        "平台", ["全部"] + sorted({p["platform"] for p in posts}),
        key=f"rv_platform_{task_id}",
    )
    only_unreviewed = c2.checkbox("只看未筛", key=f"rv_unreviewed_{task_id}")
    only_llm = c3.checkbox("只看 LLM 建议不相关", key=f"rv_llm_{task_id}")
    tc1, tc2 = st.columns(2)
    if tc1.button("全部标记不相关", key=f"rv_all_{task_id}"):
        for p in posts:
            url_set.add(p["url"])
        st.rerun()
    if tc2.button("重置筛选", key=f"rv_reset_{task_id}"):
        st.session_state[url_key] = set()
        st.session_state[cid_key] = set()
        st.rerun()

    filtered = posts
    if platform != "全部":
        filtered = [p for p in filtered if p["platform"] == platform]
    if only_unreviewed:
        filtered = [p for p in filtered if p["url"] not in url_set]
    if only_llm:
        filtered = [p for p in filtered if p.get("llm_relevant") is False]

    total_pages = max(1, (len(filtered) + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
    page = min(int(st.session_state.get(page_key, 1) or 1), total_pages)
    start = (page - 1) * REVIEW_PAGE_SIZE
    for p in filtered[start : start + REVIEW_PAGE_SIZE]:
        _render_review_post(p, url_set, cid_set, task_id)
    if total_pages > 1:
        st.caption(
            f"第 {page} / {total_pages} 页 · 共 {len(filtered)} 条（每页 {REVIEW_PAGE_SIZE} 条）"
        )
        pc1, pc2 = st.columns(2)
        if pc1.button("← 上一页", key=f"rv_prev_{task_id}", disabled=page <= 1):
            st.session_state[page_key] = max(1, page - 1)
            st.rerun()
        if pc2.button("下一页 →", key=f"rv_next_{task_id}", disabled=page >= total_pages):
            st.session_state[page_key] = min(total_pages, page + 1)
            st.rerun()

    st.divider()
    b1, b2 = st.columns(2)
    if b1.button("✔ 继续分析（保留筛选结果）", type="primary", key=f"review_submit_{task_id}"):
        if jobs.save_review(
            task_id, sorted(url_set), sorted(cid_set),
            ad_urls=sorted(ad_url_set), ad_comment_ids=sorted(ad_cid_set),
        ):
            for k in (url_key, cid_key, page_key, ad_url_key, ad_cid_key):
                st.session_state.pop(k, None)
            st.rerun()
        else:
            st.error("任务状态已变化，无法保存筛选结果")
    if b2.button("✋ 取消任务", key=f"review_cancel_{task_id}"):
        jobs.request_cancel(task_id)
        for k in (url_key, cid_key, page_key, ad_url_key, ad_cid_key):
            st.session_state.pop(k, None)
        st.rerun()

    # 广告/官方内容复核（追加区，不改变相关性页结构）
    with st.expander("📢 广告/官方内容复核（可选，规则预标为建议）", expanded=False):
        _render_review_ad_section(
            posts, task_id, ad_url_set, ad_cid_set, ad_url_key, ad_cid_key
        )


def _render_review_ad_section(
    posts: list[dict],
    task_id: str,
    ad_url_set: set,
    ad_cid_set: set,
    ad_url_key: str,
    ad_cid_key: str,
) -> None:
    """广告/官方内容复核：规则预标为建议（🔖），人工确认/取消/补标。

    标记不影响采集量/关键词效果；是否剔除由计划 exclude_ad_enabled 决定
    （默认计入，剔除仅影响情感统计）。
    """
    from app.coding.ad_rules import is_ad

    st.caption(
        "规则预标为建议（🔖 标记），请人工确认：广告/官方内容默认计入情感统计；"
        "若任务开启了「剔除广告/官方内容」，被标记项将仅从情感统计中剔除。"
    )
    st.info(f"已标记广告/官方：{len(ad_url_set)} 帖 · {len(ad_cid_set)} 条评论")

    ac1, ac2 = st.columns([2, 2])
    platform = ac1.selectbox(
        "平台", ["全部"] + sorted({p["platform"] for p in posts}),
        key=f"rv_ad_platform_{task_id}",
    )
    only_suggested = ac2.checkbox("只看规则预标", key=f"rv_ad_suggested_{task_id}")
    tc1, tc2 = st.columns(2)
    if tc1.button("全部标记广告/官方", key=f"rv_ad_all_{task_id}"):
        for p in posts:
            ad_url_set.add(p["url"])
            for c in p.get("comments") or []:
                ad_cid_set.add(c["id"])
        st.rerun()
    if tc2.button("重置标记", key=f"rv_ad_reset_{task_id}"):
        st.session_state[ad_url_key] = set()
        st.session_state[ad_cid_key] = set()
        st.rerun()

    filtered = posts
    if platform != "全部":
        filtered = [p for p in filtered if p["platform"] == platform]
    if only_suggested:
        filtered = [
            p for p in filtered
            if p["url"] in ad_url_set
            or is_ad(p.get("content") or "", p.get("title") or "")
            or any(is_ad(c.get("text") or "") for c in (p.get("comments") or []))
        ]

    for p in filtered[:50]:
        url = p["url"]
        marked = url in ad_url_set
        title = (p.get("title") or p.get("content") or "（无标题）")[:60]
        meta = (
            f"{platform_cn(p['platform'])} · {p.get('timestamp') or '时间未知'} · "
            f"点赞 {p.get('likes', 0)}"
        )
        suggested = is_ad(p.get("content") or "", p.get("title") or "")
        c1, c2 = st.columns([4, 1])
        with c1:
            st.markdown(f"{'📢 ' if marked else ''}**{title}**")
            st.caption(meta)
            if suggested:
                st.caption(f"🔖 规则预标：{suggested}")
        flag = c2.checkbox("广告/官方", value=marked, key=_review_key("rad", url))
        if flag != marked:
            (ad_url_set.add if flag else ad_url_set.discard)(url)
            st.rerun()
        with st.expander("查看帖子与评论", expanded=False):
            st.markdown(p.get("content") or p.get("title") or "（无正文）")
            for c in p.get("comments") or []:
                cm = c["id"] in ad_cid_set
                csug = is_ad(c.get("text") or "")
                cc1, cc2 = st.columns([4, 1])
                cc1.caption(
                    f"{c.get('author') or '匿名'}：{c.get('text')}"
                    + (f"　🔖 {csug}" if csug else "")
                )
                rm = cc2.checkbox("广告/官方", value=cm, key=_review_key("radc", c["id"]))
                if rm != cm:
                    (ad_cid_set.add if rm else ad_cid_set.discard)(c["id"])
                    st.rerun()

    if len(filtered) > 50:
        st.caption(f"仅展示前 50 条，共 {len(filtered)} 条（建议按平台/只看规则预标筛选）")

    st.divider()
    b1, b2 = st.columns(2)
    if b1.button("✔ 保存广告/官方标记", type="primary", key=f"review_ad_submit_{task_id}"):
        from app.core import jobs as _jobs

        url_key = f"review_urls_{task_id}"
        cid_key = f"review_cids_{task_id}"
        if _jobs.save_review(
            task_id,
            sorted(st.session_state.get(url_key, set())),
            sorted(st.session_state.get(cid_key, set())),
            ad_urls=sorted(ad_url_set),
            ad_comment_ids=sorted(ad_cid_set),
        ):
            for k in (url_key, cid_key, ad_url_key, ad_cid_key, f"review_page_{task_id}"):
                st.session_state.pop(k, None)
            st.rerun()
        else:
            st.error("任务状态已变化，无法保存标记")
    if b2.button("✋ 取消任务", key=f"review_ad_cancel_{task_id}"):
        jobs.request_cancel(task_id)
        st.rerun()


def _render_review_post(p: dict, url_set: set, cid_set: set, task_id: str) -> None:
    """单条帖子：相关/不相关切换 + 展开评论逐条剔除。"""
    url = p["url"]
    excluded = url in url_set
    title = (p.get("title") or p.get("content") or "（无标题）")[:60]
    meta = (
        f"{platform_cn(p['platform'])} · {p.get('timestamp') or '时间未知'} · "
        f"点赞 {p.get('likes', 0)} · 关键词 {p.get('keyword') or '-'}"
    )
    c1, c2 = st.columns([4, 1])
    with c1:
        st.markdown(f"{'🚫 ' if excluded else ''}**{title}**")
        st.caption(meta)
        if p.get("llm_relevant") is False:
            st.caption("🔖 LLM 建议不相关（人工最终决定）")
    mark = c2.checkbox("不相关", value=excluded, key=_review_key("rv", url))
    if mark != excluded:
        if mark:
            url_set.add(url)
        else:
            url_set.discard(url)
        st.rerun()
    with st.expander("查看帖子与评论", expanded=False):
        st.markdown(p.get("content") or p.get("title") or "（无正文）")
        comments = p.get("comments") or []
        if excluded:
            st.caption(f"帖子已标记不相关，{len(comments)} 条评论随帖剔除")
        elif comments:
            st.markdown("**评论**")
            for c in comments:
                marked = c["id"] in cid_set
                cc1, cc2 = st.columns([4, 1])
                cc1.caption(f"{c.get('author') or '匿名'}：{c.get('text')}")
                rm = cc2.checkbox("剔除", value=marked, key=_review_key("rc", c["id"]))
                if rm != marked:
                    if rm:
                        cid_set.add(c["id"])
                    else:
                        cid_set.discard(c["id"])
                    st.rerun()
        else:
            st.caption("（该帖无评论）")


def estimate_collection(
    keywords: list[str],
    channel_ids: list[str],
    limits: dict[str, int],
    comments_per_post: int,
    comments_enabled: bool = True,
) -> tuple[int, int, float]:
    """粗估（链接数, 评论数, 分钟）；实际受网络与平台频率限制影响。

    耗时 = 搜索/浏览时间 + 评论抓取时间；评论抓取随"每帖评论上限"变化，
    关闭评论时不计评论量与评论耗时。
    """
    items = 0
    comments = 0
    seconds = 0.0
    for cid in channel_ids:
        n = int(limits.get(cid, CHANNEL_LIMIT_DEFAULTS.get(cid, 10)))
        k = max(len(keywords), 1)
        if cid == "bilibili":
            eff = n
            per_k = (n / 20) * 1.5
            com = min(n, 10) * comments_per_post if comments_enabled else 0
        elif cid == "weibo":
            eff = n
            per_k = n / 10
            com = min(n, 10) * comments_per_post if comments_enabled else 0
        elif cid == "xiaohongshu":
            eff = min(n, 10)
            per_k = 15 + eff * 13
            com = min(n, 5) * comments_per_post if comments_enabled else 0
        elif cid.startswith("websearch"):
            eff = n
            per_k = 8
            com = 0
        else:
            eff = n
            per_k = 5
            com = 0
        items += k * eff
        comments += k * com
        seconds += k * (per_k + com * COMMENT_FETCH_SECONDS)
    return items, comments, round(seconds * 1.2 / 60, 1)


if "stage" not in st.session_state:
    st.session_state.stage = 0


def reset_wizard() -> None:
    for key in [
        "stage", "mode", "subject", "domain_id", "selected_dims", "keywords",
        "channel_ids", "date_range", "plan", "bundle", "output_files", "task_id",
        "exclude_words", "exclude_words_opt",
    ]:
        st.session_state.pop(key, None)
    st.session_state.stage = 0


def next_stage() -> None:
    st.session_state.stage = int(st.session_state.get("stage", 0)) + 1


def prev_stage() -> None:
    st.session_state.stage = max(int(st.session_state.get("stage", 0)) - 1, 0)


# ---------------------------------------------------------------------------
# 侧边栏：大模型与高级设置
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("⚙ 大模型设置")
    saved_key = load_api_key(allow_env=False)
    api_key = st.text_input(
        "API Key（可选）", type="password", key="api_key_input",
        value=saved_key,
        help="不填则使用词典预筛模式，离线可跑；本机已保存的 Key 会自动回填",
    )
    col_save, col_clear = st.columns(2)
    if col_save.button("💾 保存到本机", width="stretch"):
        if api_key and api_key.strip():
            save_api_key(api_key.strip())
            st.success("已加密保存到本机（Windows DPAPI）")
        else:
            st.warning("未填写 API Key，无需保存")
    if col_clear.button("🗑 清除已保存", width="stretch"):
        clear_api_key()
        st.session_state["api_key_input"] = ""
        st.info("已清除本机保存的 API Key")
    st.caption(
        "Key 以 Windows DPAPI 加密存于本机（data/secrets/），仅当前用户可解密；"
        "换机/重装后需重新填写。"
    )
    preset = st.selectbox(
        "服务商预设",
        ["DeepSeek", "OpenAI", "自定义"],
        key="llm_preset",
        help="选择 DeepSeek 会自动填好 Base URL 与模型名",
    )
    presets = {
        "DeepSeek": ("https://api.deepseek.com", "deepseek-chat"),
        "OpenAI": ("https://api.openai.com/v1", "gpt-4o-mini"),
    }
    st.session_state.setdefault("base_url", presets["DeepSeek"][0])
    st.session_state.setdefault("model_name", presets["DeepSeek"][1])
    if preset != "自定义":
        target_base, target_model = presets[preset]
        if (
            st.session_state.get("base_url") != target_base
            or st.session_state.get("model_name") != target_model
        ):
            st.session_state.base_url = target_base
            st.session_state.model_name = target_model
            st.rerun()
    base_url = st.text_input("Base URL", key="base_url",
                             help="DeepSeek：https://api.deepseek.com；OpenAI：https://api.openai.com/v1")
    model_name = st.text_input("模型名", key="model_name",
                               help="DeepSeek 用 deepseek-chat；推理模型 deepseek-reasoner 较慢")
    if st.button("🔌 测试连接", width="stretch"):
        if not api_key:
            st.warning("请先填写 API Key 再测试")
        else:
            probe = create_analyzer(api_key=api_key, base_url=base_url, model=model_name)
            ok, msg = probe.ping()
            if ok:
                st.success(f"连接正常 ✅ {base_url} / {model_name}")
            else:
                st.error(f"连接失败：{msg}")
    st.divider()
    st.subheader("高级设置")
    llm_enabled = st.toggle("启用 LLM 精分析", value=bool(api_key),
                            help="低置信度文本调用大模型，可提高准确率（按量计费）")
    if llm_enabled:
        st.warning(
            "⚠️ 开启后，低置信度文本将发送给所选服务商（DeepSeek/OpenAI 等），"
            "请勿输入含个人敏感信息的内容。"
        )
    narrative_enabled = st.toggle("叙事框架/归因分析", value=False,
                                  help="高级模式：分析文本的叙事框架与责任归因（默认关）")
    relevance_check_enabled = st.toggle(
        "LLM 相关性复核（可选）",
        value=False,
        help="用大模型判断每条内容是否与品牌相关，剔除无关信息；"
        "会增加 LLM 费用并延长分析耗时，默认关闭",
    )
    if relevance_check_enabled:
        st.warning(
            "⚠️ LLM 相关性复核会增加大模型调用量与费用（按文本数计费），"
            "分析耗时也会变长，请确认可接受后再开启。"
        )
    st.session_state.exclude_ad_opt = st.toggle(
        "剔除广告/官方内容（默认计入）",
        value=st.session_state.get("exclude_ad_opt", False),
        help=(
            "广告也是消费者可见的市场信号：默认计入（按 neutral 参与统计，报告注明占比）；"
            "开启后仅从情感统计中剔除（采集量/关键词效果保留），报告会给出剔除说明。"
            "建议配合「人工筛选相关性」开启：审核界面可复核广告/官方标记。"
        ),
    )
    st.caption(
        "提示：环境变量 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL 仅用于"
        "开发/评测脚本，应用内 Key 只存本机 DPAPI。"
    )
    with st.expander("查看《使用边界》"):
        st.markdown(usage_boundary.boundary_text())
    with st.expander("🗃 数据管理"):
        try:
            _usage = lifecycle.data_usage()
            _c = _usage["categories"]
            st.markdown(
                f"报告 {_c['reports_active']['size_bytes'] / 1048576:.0f} MB · "
                f"归档 {_c['reports_archive']['size_bytes'] / 1048576:.0f} MB · "
                f"数据集 {_c['datasets']['size_bytes'] / 1048576:.0f} MB · "
                f"日志 {_c['logs']['size_bytes'] / 1048576:.0f} MB · "
                f"回归 {_c['regression']['size_bytes'] / 1048576:.0f} MB · "
                f"数据库 {_c['database']['size_bytes'] / 1048576:.0f} MB · "
                f"密钥/确认 {_c['secrets']['size_bytes'] / 1048576:.0f} MB"
            )
            if st.session_state.get("lifecycle_repair_notice"):
                st.success(st.session_state["lifecycle_repair_notice"])
            if st.session_state.get("lifecycle_orphan_count"):
                st.caption(
                    f"孤儿归档（无对应任务，仅统计不处理）："
                    f"{st.session_state['lifecycle_orphan_count']} 个"
                )
            _arch = lifecycle.archive_old_reports(dry_run=True)
            st.caption(
                f"可归档 {_arch['moved_count']} 个 / 跳过 {_arch['skipped_count']} 个"
                "（不在最近 30 个 且 超 7 天 且 终态无人工筛选才归档）"
            )
            if st.button("🗜 归档旧报告", key="lifecycle_archive", width="stretch"):
                try:
                    with lifecycle.LifecycleLock():
                        res = lifecycle.archive_old_reports(dry_run=False)
                    st.success(f"已归档 {res['moved_count']} 个报告目录")
                except RuntimeError as exc:
                    st.warning(str(exc))
            if st.button(
                "🗑 清理超期归档（90 天，回收站）",
                key="lifecycle_purge",
                width="stretch",
            ):
                st.session_state["lifecycle_confirm_purge"] = True
            if st.session_state.get("lifecycle_confirm_purge"):
                _pv = lifecycle.purge_archived(dry_run=True)
                st.warning(
                    f"将删除 {_pv['deleted_count']} 个归档目录，"
                    f"预计释放 {_pv['freed_bytes'] / 1048576:.1f} MB；"
                    "删除不可恢复（优先回收站）。"
                )
                if st.button(
                    "确认清理（不可恢复）",
                    key="lifecycle_purge_yes",
                    width="stretch",
                ):
                    try:
                        with lifecycle.LifecycleLock():
                            res = lifecycle.purge_archived(dry_run=False)
                        st.session_state.pop("lifecycle_confirm_purge", None)
                        st.success(
                            f"已清理 {res['deleted_count']} 个，"
                            f"释放 {res['freed_bytes'] / 1048576:.1f} MB"
                        )
                    except RuntimeError as exc:
                        st.warning(str(exc))
            with st.expander("高级清理（日志/回归报告）"):
                _lv = lifecycle.purge_old_logs(dry_run=True)
                _rv = lifecycle.prune_regression(dry_run=True)
                st.caption(
                    f"旧日志 {_lv['deleted_count']} 个（超 30 天）· "
                    f"回归报告 {_rv['removed_count']} 项（保留 20 份）"
                )
                if st.button(
                    "执行高级清理",
                    key="lifecycle_extra",
                    width="stretch",
                ):
                    try:
                        with lifecycle.LifecycleLock():
                            lres = lifecycle.purge_old_logs(dry_run=False)
                            rres = lifecycle.prune_regression(dry_run=False)
                        st.success(
                            f"日志清理 {lres['deleted_count']} 个，"
                            f"回归清理 {rres['removed_count']} 项"
                        )
                    except RuntimeError as exc:
                        st.warning(str(exc))
        except Exception as exc:
            st.caption(f"数据管理暂不可用：{exc}")
    with st.expander("🛠 开发者模式"):
        dev_mode = st.toggle(
            "启用开发者模式",
            value=_dev_mode_enabled(),
            help="开启后显示评测中心入口等开发者工具；小白用户无需开启",
        )
        if dev_mode != _dev_mode_enabled():
            _set_dev_mode(dev_mode)
        if dev_mode:
            if _eval_running():
                st.markdown(
                    f"评测中心已在运行：[打开 {EVAL_URL}]({EVAL_URL})"
                )
            else:
                if st.button(
                    "🚀 启动并打开评测中心",
                    key="start_eval_dashboard",
                    width="stretch",
                ):
                    _start_eval_dashboard()
                    webbrowser.open(EVAL_URL)
                    st.info(
                        f"正在后台启动评测中心（{EVAL_URL}），首次启动需几秒，"
                        "浏览器会自动打开；如未打开请手动访问该地址。"
                    )
            st.caption(
                "评测中心：黄金集细分跑分 / 关键词效果 / 候选确认（开发者工具）"
            )
        else:
            st.caption("评测中心为开发者调优工具，小白用户无需开启。")
    st.caption(f"应用版本：v{__version__}")

# ---------------------------------------------------------------------------
# 顶部标题
# ---------------------------------------------------------------------------

st.title("📊 社交媒体情感分析器")
st.caption("输入品牌/产品名或关键词 → 确认维度与采集计划 → 自动生成图表与分析报告")
render_task_center()


# ---------------------------------------------------------------------------
# 向导各阶段
# ---------------------------------------------------------------------------

stage = st.session_state.stage

if stage == 0:
    st.subheader("① 输入分析对象")
    mode = st.radio("输入方式", ["品牌名 + 领域（推荐）", "手动输入关键词"], horizontal=True)
    st.session_state.mode = mode

    if mode.startswith("品牌名"):
        subject = st.text_input("品牌 / 产品 / 事件名称", placeholder="例如：华润万家、恋与深空、iPhone")
        domains = list_domains()
        # 领域配方模型（2026-08-14）：主导对象模板优先 + 领域示例，帮助用户理解
        template_cn = {"content": "数字内容产品", "physical": "实物产品", "service": "服务过程"}
        options = ["不使用领域"] + [
            f"{template_cn.get(d.get('template_id'), d['name'])}（如{d['name']}）"
            for d in domains
        ]
        domain_choice = st.selectbox("所属领域", options, index=0)
        st.session_state.subject = subject.strip()
        st.session_state.domain_id = "" if domain_choice.startswith("不使用") else domains[options.index(domain_choice) - 1]["id"]
    else:
        manual = st.text_area("关键词（每行一个）", placeholder="例如：\n原神 画质\n原神 抽卡")
        st.session_state.subject = "手动关键词"
        st.session_state.domain_id = ""
        st.session_state.manual_keywords = [k.strip() for k in manual.splitlines() if k.strip()]

    col1, _ = st.columns([1, 3])
    if col1.button("下一步 →", type="primary", width="stretch"):
        if mode.startswith("品牌名") and not st.session_state.get("subject"):
            st.error("请输入品牌/产品名称")
        elif not mode.startswith("品牌名") and not st.session_state.get("manual_keywords"):
            st.error("请输入至少一个关键词")
        else:
            next_stage()
            st.rerun()

elif stage == 1:
    st.subheader("② 确认分析维度")
    if st.session_state.get("domain_id"):
        schema = load_domain(st.session_state.domain_id)
        st.session_state.schema = schema
        names = {d.id: f"{d.name} — {d.description}" for d in schema.dimensions}
        selected = st.multiselect(
            "选择要分析的维度（可全选/单选，报告将按维度统计）",
            options=list(names.keys()),
            format_func=lambda k: names[k],
            default=list(names.keys()),
        )
        st.session_state.selected_dims = selected
        custom = st.text_input(
            "添加自定义维度（可选）",
            placeholder="运营服务、联名活动",
            help="多个维度用逗号/顿号分隔；每个维度会生成一条「品牌+维度」采集关键词。"
            "自定义维度仅用于补充采集，不参与维度统计",
        )
        st.session_state.custom_dim = custom.strip()
    else:
        st.info("未选择领域，将按整体情感分析（不区分维度）")
        st.session_state.selected_dims = []

    col1, col2 = st.columns(2)
    if col1.button("← 上一步", width="stretch"):
        prev_stage()
        st.rerun()
    if col2.button("下一步 →", type="primary", width="stretch"):
        next_stage()
        st.rerun()

elif stage == 2:
    st.subheader("③ 确认关键词")
    subject = st.session_state.get("subject", "")
    domain_id = st.session_state.get("domain_id")
    mode = st.session_state.get("mode", "")

    if domain_id and st.session_state.get("selected_dims"):
        schema = st.session_state.schema
        groups = generate_keyword_groups(subject, schema, st.session_state.selected_dims)
        custom_dim = st.session_state.get("custom_dim")
        if custom_dim:
            custom_dims = [
                d.strip()
                for d in re.split(r"[,，、\s]+", custom_dim)
                if d.strip()
            ]
            if custom_dims:
                groups.append(
                    KeywordGroup(
                        dimension_id="custom",
                        dimension_name="自定义",
                        keywords=[f"{subject} {d}" for d in custom_dims],
                    )
                )
        st.session_state.keyword_groups = groups
        with st.expander("按维度查看建议关键词", expanded=True):
            for g in groups:
                st.markdown(f"**{g.dimension_name}**：" + "、".join(g.keywords))
        default_text = "\n".join(flatten_keywords(groups))
    elif domain_id:
        st.info("未选择任何维度，将以品牌名为关键词，可补充输入")
        default_text = subject
    elif mode.startswith("品牌名"):
        st.info("未选择领域，将以品牌名为关键词，可补充输入")
        default_text = subject
    else:
        st.info("手动关键词模式：将直接使用以下关键词采集")
        default_text = "\n".join(st.session_state.get("manual_keywords", []))

    if domain_id and st.session_state.get("selected_dims"):
        editable = st.text_area(
            "编辑关键词（每行一个，可增删改）",
            value=default_text,
            height=220,
            help="建议保留 10~30 个关键词；短词搜索范围更广，长词更精准",
        )
    else:
        editable = st.text_area(
            "关键词（每行一个）",
            value=default_text,
            height=160,
            help="多关键词会叠加搜索结果；短词搜索范围更广",
        )
    st.session_state.keywords = [k.strip() for k in editable.splitlines() if k.strip()]
    st.caption(
        f"当前将采集 **{len(st.session_state.get('keywords', []))}** 个关键词，"
        "以本框内容为准（可增删改）。"
    )

    col1, col2 = st.columns(2)
    if col1.button("← 上一步", width="stretch"):
        prev_stage()
        st.rerun()
    if col2.button("下一步 →", type="primary", width="stretch"):
        if not st.session_state.get("keywords"):
            st.error("关键词列表不能为空")
        else:
            next_stage()
            st.rerun()

elif stage == 3:
    st.subheader("④ 选择采集渠道与时间段")
    infos = list_channel_infos()
    default_channels = ["demo"]
    selected = st.multiselect(
        "采集渠道（建议先使用演示数据体验全流程）",
        options=[i["id"] for i in infos],
        default=default_channels,
        format_func=lambda cid: {
            i["id"]: f"{i['name']} — {i['applicability']}"
            for i in infos
        }[cid],
    )
    st.session_state.channel_ids = selected

    # 渠道提示：已选渠道的风控/前置条件/采集规则统一展示（避免警告散落各处）
    risk_texts = []
    info_texts = []
    if "weibo" in selected:
        risk_texts.append(
            "微博：风控严格，高频请求可能导致账号被临时限制甚至封禁；"
            "默认上限 20，请避免短时间重复运行。"
        )
    if "xiaohongshu" in selected:
        risk_texts.append(
            "小红书：风控严格，高频采集会触发验证码，严重时影响登录态；"
            "默认上限 10，建议每关键词 ≤20 且两次分析之间留出间隔。"
        )
        info_texts.append(
            "小红书前置条件：Chrome 已登录 xiaohongshu.com + opencli 已安装"
            "（npm install -g @jackwener/opencli）；采集较慢，每个关键词约 1~2 分钟。\n"
            "评论抓取范围：每个关键词最多收录 10 帖，评论只对其中**最热（按点赞）前 5 帖**抓取，"
            "每帖最多抓取设定的评论条数——降低操作频率以防验证码与风控、控制采集耗时。"
        )
    if risk_texts:
        st.warning("⚠️ 渠道风控提示\n" + "\n".join(f"- {t}" for t in risk_texts))
    for t in info_texts:
        st.info(t)

    col1, col2 = st.columns(2)
    with col1:
        default_start = dt.date.today() - dt.timedelta(days=30)
        date_range = st.date_input(
            "时间段", value=(default_start, dt.date.today()), help="采集该时间段内的内容"
        )
        if isinstance(date_range, tuple) and len(date_range) == 2:
            st.session_state.date_range = date_range
    with col2:
        if "weibo" in selected:
            if not st.session_state.get("weibo_cookie"):
                saved_cookie = load_cookie("weibo")
                if saved_cookie:
                    st.session_state.weibo_cookie = saved_cookie
            cookie = st.text_input(
                "微博 Cookie（粘贴已登录的 Cookie）",
                type="password",
                value=st.session_state.get("weibo_cookie", ""),
                help="用于后台采集；将以 Windows DPAPI 加密仅保存在本机，"
                "不会写入报告或上传",
            )
            st.session_state.weibo_cookie = cookie
            remember = st.checkbox(
                "记住 Cookie（Windows DPAPI 加密，仅本机可解）",
                value=bool(st.session_state.get("weibo_cookie_remember", False)),
            )
            st.session_state.weibo_cookie_remember = remember
            if remember and cookie.strip():
                try:
                    save_cookie("weibo", cookie.strip())
                except Exception:
                    st.warning("Cookie 加密保存失败（本会话仍可使用）")
            if st.button("清除已保存的 Cookie"):
                clear_cookie("weibo")
                st.session_state.weibo_cookie = ""
                st.session_state.weibo_cookie_remember = False
                st.rerun()
            with st.expander("❓ 怎么获取微博 Cookie？（小白版）"):
                st.markdown(
                    "1. 用 Chrome / Edge 打开 **m.weibo.cn** 并登录你的微博账号\n"
                    "2. 按 **F12** 打开开发者工具，点上方 **Network（网络）** 标签\n"
                    "3. **刷新页面（F5）**\n"
                    "4. 在请求列表里**筛选 XHR/API 请求**，点一条指向 "
                    "`m.weibo.cn/api/...` 的请求\n"
                    "5. 在右侧 **Headers → Request Headers** 里找到 **Cookie** 一栏，"
                    "把整段 Cookie 值复制下来（或只复制 `SUB=...` 到分号前的这一段）\n"
                    "6. 粘贴到上面的输入框（直接粘整段最稳；不要复制 `Cookie:` 前缀）\n\n"
                    "⚠️ Cookie 相当于账号凭证，请只在你自己电脑上使用；"
                    "程序会以 Windows DPAPI 加密仅保存在本机，仅供后台采集使用，"
                    "不会写入报告或上传。\n"
                    "⏳ Cookie 会过期（通常几天到几周），失效后需重新获取。"
                )

    if selected:
        st.divider()
        c1, c2 = st.columns([1, 3])
        with c1:
            comments_enabled = st.toggle(
                "抓取评论", value=True, key="comments_enabled",
                help="关闭后不抓取评论，只分析帖子正文",
            )
        with c2:
            comments_per_post = st.slider(
                "每帖评论上限", 0, 50, 20, key="comments_per_post",
                help="每条帖子最多抓取多少条评论（评论越多采集越慢）",
            )
        # Streamlit 会在控件卸载时清理其 widget key，镜像到普通键供确认页/提交读取
        st.session_state.comments_enabled_opt = comments_enabled
        st.session_state.comments_per_post_opt = comments_per_post
        st.text_input(
            "词云排除词（可选）",
            key="exclude_words",
            placeholder="例如：黄金之地、夏萧因（角色名/地名，逗号或空格分隔）",
            help="填写的词不会出现在词云和共现网络里；角色名、地名等建议填在这里",
        )
        st.caption(
            "用法：填写不想出现在词云/共现网络里的角色名或地名；"
            "多个词用逗号、顿号或空格分隔，例如：黄金之地、夏萧因、顾时夜"
        )
        st.session_state.exclude_words_opt = [
            t.strip()
            for t in re.split(r"[,，、\s]+", st.session_state.get("exclude_words", ""))
            if t.strip()
        ]
        st.session_state.review_enabled_opt = st.toggle(
            "人工筛选相关性（采集后暂停）",
            value=st.session_state.get("review_enabled_opt", False),
            key="review_enabled",
            help=(
                "采集并清洗后暂停任务，进入人工筛选：剔除与品牌相关性不高的帖子和评论，"
                "再继续情感分析。默认关闭。"
            ),
        )
        st.divider()
        st.markdown("**每关键词采集条数上限（按渠道）**")
        info_map = {i["id"]: i["name"] for i in infos}
        st.session_state.channel_limits = {}
        lim_cols = st.columns(min(len(selected), 4))
        for i, cid in enumerate(selected):
            with lim_cols[i % 4]:
                default = CHANNEL_LIMIT_DEFAULTS.get(cid, 10)
                is_ws = cid.startswith("websearch")
                help_txt = CHANNEL_LIMIT_HELP.get(
                    cid, "每个关键词最多抓取多少条链接"
                )
                if is_ws:
                    help_txt = (
                        "单查询可获取量约 5~13 条，上限 13 为收益/反爬平衡；"
                        "要增加采集量建议增加关键词（策略加词），而非调大上限"
                    )
                val = st.number_input(
                    f"{info_map.get(cid, cid)}",
                    min_value=1,
                    max_value=CHANNEL_LIMIT_MAX.get(cid, 200),
                    value=default,
                    key=f"limit_{cid}",
                    help=help_txt,
                )
                st.session_state.channel_limits[cid] = int(val)
        st.divider()
        st.markdown("**渠道安全（每日配额 / 风控冷却 / 暂停）**")
        st.caption(
            "每日配额按「关键词数 × 每关键词上限」估算消耗；微博/小红书有默认上限，"
            "可在下方调整（-1=不限；不限的渠道不在此显示）。"
            "检测到风控时渠道会自动冷却，可在此手动解除。"
        )
        quota_save: dict[str, int] = {}
        for cid in selected:
            s = jobs.channel_state(cid)
            name = info_map.get(cid, cid)
            if int(s.get("quota_limit") or -1) < 0:
                continue  # 不限额度渠道不显示配额行，避免用户困惑
            if s.get("paused"):
                status = "⏸ 已暂停"
            elif s.get("cool_until"):
                status = f"🔥 冷却至 {s['cool_until']}"
            else:
                limit = s.get("quota_limit")
                status = f"今日 {s.get('quota_used', 0)}/{limit}"
            q1, q2, q3, q4 = st.columns([2.2, 1.6, 1, 1])
            q1.caption(f"{name}：{status}")
            limit_val = q2.number_input(
                "每日上限",
                min_value=-1,
                max_value=100000,
                value=int(s.get("quota_limit", -1)),
                key=f"quota_limit_{cid}",
                label_visibility="collapsed",
                help="每日消耗上限（按预计采集条数计）；-1 表示不限",
            )
            quota_save[cid] = int(limit_val)
            if q3.button("恢复" if s.get("paused") else "暂停", key=f"pause_{cid}"):
                (jobs.resume_channel if s.get("paused") else jobs.pause_channel)(cid)
                st.rerun()
            if s.get("cool_until") and q4.button("解除冷却", key=f"uncool_{cid}"):
                jobs.clear_cooldown(cid)
                st.rerun()
        if st.button("保存配额设置", key="save_quota"):
            for cid, val in quota_save.items():
                jobs.set_quota_limit(cid, val)
            st.success("每日配额已保存")
            st.rerun()
        est_items, est_comments, est_min = estimate_collection(
            st.session_state.get("keywords", []),
            selected,
            st.session_state.channel_limits,
            comments_per_post,
            comments_enabled,
        )
        st.caption(
            f"预计采集：链接约 {est_items} 条、评论约 {est_comments} 条，"
            f"耗时约 {est_min} 分钟（受网络与平台频率限制影响；"
            "估算随关键词数、渠道上限与评论设置变化）"
        )

        # ── 渠道诊断（体检 × 一键探针融合，docs/渠道诊断融合方案.md）──
        st.markdown("**🔍 渠道诊断**")
        st.caption(
            "轻量层并行真实探测各渠道（WebSearch = 360 单引擎出数探测，含风控/降级"
            "识别，不再只看 HTTP 200），并合并系统侧状态（暂停/冷却/配额）；"
            "WebSearch 行可展开三引擎深度探针。"
        )
        if st.button("开始诊断", key="diag_start"):
            cookie = st.session_state.get("weibo_cookie", "")
            probe_subject = (
                str(st.session_state.get("subject") or "").strip() or "测试"
            )
            query = f"{probe_subject} 评价"
            with st.spinner("正在并行诊断已选渠道（约 5~10 秒）…"):
                st.session_state["diag_results"] = health.check_channels(
                    selected, {"cookie": cookie, "query": query}
                )
            st.session_state["diag_query"] = query
        if st.session_state.get("diag_results"):
            cur = {
                cid: rich
                for cid, rich in st.session_state["diag_results"].items()
                if cid in selected
            }
            if cur:
                render_channel_diag(
                    cur,
                    st.session_state.get("diag_query") or "测试 评价",
                    info_map,
                )
                if any(
                    rich.get("system") and not rich["system"].get("ok")
                    for rich in cur.values()
                ):
                    st.caption(
                        "暂停/冷却/配额不足的渠道请到上方「渠道安全」区处理"
                        "（解除冷却 / 恢复 / 调整每日上限）。"
                    )

        if any(cid.startswith("websearch") for cid in selected):
            st.session_state.websearch_eval_suffix = st.toggle(
                "WebSearch 关键词优化（自动追加『评价』等后缀）",
                value=st.session_state.get("websearch_eval_suffix", True),
                help="纯品牌词（如『恋与深空』）容易命中官网/壳页面，自动加『评价』等后缀"
                     "更易搜到真实用户讨论；关闭后完全按原词搜索",
            )
            st.caption(
                "说明：开启后实际查询串可能与确认关键词不同（如『恋与深空』→"
                "『恋与深空 评价』）；实际查询串可在报告「实际查询串（WebSearch）」表核对。"
            )
            st.session_state.official_domains = st.text_input(
                "排除的官方域名（逗号分隔，可选）",
                placeholder="例如：dji.com,crv.com.cn",
                help="官网不会提供有效的社交媒体情感信息，命中这些域名的搜索结果将被排除",
                value=st.session_state.get("official_domains", ""),
            )

    col1, col2 = st.columns(2)
    if col1.button("← 上一步", width="stretch"):
        prev_stage()
        st.rerun()
    if col2.button("下一步 →", type="primary", width="stretch"):
        if not selected:
            st.error("请至少选择一个渠道")
        else:
            next_stage()
            st.rerun()

elif stage == 4:
    st.subheader("⑤ 确认采集计划并启动")
    keywords = st.session_state.get("keywords", [])
    channel_ids = st.session_state.get("channel_ids", ["demo"])
    date_range = st.session_state.get("date_range", (dt.date.today() - dt.timedelta(days=30), dt.date.today()))
    channel_params = {}
    if "weibo" in channel_ids and st.session_state.get("weibo_cookie"):
        channel_params["weibo"] = {"cookie": st.session_state.weibo_cookie}
    for cid in channel_ids:
        lim = st.session_state.get("channel_limits", {}).get(cid)
        if lim:
            channel_params.setdefault(cid, {})["limit"] = lim
    official_domains = st.session_state.get("official_domains", "")
    if official_domains and any(cid.startswith("websearch") for cid in channel_ids):
        for cid in channel_ids:
            if cid.startswith("websearch"):
                channel_params.setdefault(cid, {})["official_domains"] = official_domains
    if not st.session_state.get("websearch_eval_suffix", True):
        for cid in channel_ids:
            if cid.startswith("websearch"):
                channel_params.setdefault(cid, {})["eval_suffix"] = "0"

    st.markdown("### 计划摘要")
    est_items, est_comments, est_min = estimate_collection(
        keywords, channel_ids, st.session_state.get("channel_limits", {}),
        st.session_state.get("comments_per_post_opt", 20),
        st.session_state.get("comments_enabled_opt", True),
    )
    cost_est = estimate_cost(
        est_items + est_comments, narrative_enabled, relevance_check_enabled
    )
    # WebSearch 关键词总量（关键词数 × WebSearch 渠道数）：
    # 必须在 summary_rows 构建前计算，否则确认页引用未定义变量抛 NameError
    # （回归见 commit 22454ed：ws_total 赋值曾误放在 st.table 之后）。
    ws_channels = [c for c in channel_ids if c.startswith("websearch")]
    ws_total = len(keywords) * len(ws_channels)
    summary_rows = [
        ("分析对象", st.session_state.get("subject", "")),
        ("领域", st.session_state.get("domain_id") or "不使用"),
        ("关键词数", str(len(keywords))),
        ("渠道", "、".join(channel_ids)),
        ("官方域名排除", official_domains or "未配置"),
        ("WebSearch 关键词优化", "开（自动加评价等后缀）" if st.session_state.get("websearch_eval_suffix", True) else "关（按原词搜索）"),
        *(
            [(
                "WebSearch 关键词总量（今日上限 24）",
                f"{ws_total} 次查询"
                + (f"（{len(ws_channels)} 个 WebSearch 渠道 × {len(keywords)} 关键词）")
                + ("，超限提交将被拦截" if ws_total > 24 else ""),
            )]
            if ws_channels
            else []
        ),
        ("LLM 相关性复核", "开（费用与耗时增加）" if relevance_check_enabled else "关"),
        ("广告/官方内容", "计入（默认）" if not st.session_state.get("exclude_ad_opt", False)
         else "剔除（仅情感统计）"),
        ("词云排除词", "、".join(st.session_state.get("exclude_words_opt", [])) or "未配置"),
        ("预计 LLM 费用", f"约 ¥{cost_est['estimated_cost']}（预估）"),
        (
            "预计采集",
            f"链接约 {est_items} 条、评论约 {est_comments} 条，耗时约 {est_min} 分钟",
        ),
        ("时间段", f"{date_range[0]} ~ {date_range[1]}" if isinstance(date_range, tuple) else "不限"),
        ("LLM 精分析", "开" if llm_enabled else "关（词典模式）"),
        ("叙事/归因", "开" if narrative_enabled else "关"),
        ("LLM 服务", f"{model_name} @ {base_url}" if llm_enabled else "—"),
    ]
    st.table(summary_rows)
    if ws_total > 24:
        st.warning(
            f"WebSearch 关键词总量 {ws_total} 超今日上限 24，提交后该渠道会被拦截；"
            "建议减少关键词或子渠道数量，分次运行。"
        )
    with st.expander("费用预估说明"):
        st.markdown(cost_est["assumptions"])
        st.caption("预估仅供参考，实际费用以 DeepSeek 官方计费与实际 token 用量为准；单价可能随时调整。")
    if llm_enabled and not api_key:
        st.warning("已开启 LLM 精分析但未填写 API Key，将自动使用词典模式")
    with st.expander("关键词清单"):
        for i, k in enumerate(keywords, 1):
            st.markdown(f"{i}. {k}")

    col1, col2 = st.columns(2)
    if col1.button("← 上一步", width="stretch"):
        prev_stage()
        st.rerun()

    start_btn = col2.button("🚀 启动分析", type="primary", width="stretch")
    if start_btn:
        # LLM Key：DPAPI 加密存本机供后台 worker 读取（不落库、不落明文）
        if api_key and api_key.strip():
            try:
                save_api_key(api_key.strip())
            except Exception:
                pass  # 加密保存失败不阻塞提交，worker 将使用词典模式
        # 微博 Cookie：提交时一并加密存本机，后台 worker 才能恢复使用
        # （计划入库前会剥离 cookie，worker 只从 DPAPI 读取）
        if "weibo" in channel_ids and st.session_state.get("weibo_cookie"):
            try:
                save_cookie("weibo", st.session_state.weibo_cookie.strip())
            except Exception:
                pass  # 加密保存失败不阻塞提交，该渠道会降级提示
        plan = build_plan(
            subject=st.session_state.get("subject", ""),
            domain_id=st.session_state.get("domain_id") or None,
            dimension_ids=st.session_state.get("selected_dims", []),
            keyword_groups=st.session_state.get("keyword_groups", []),
            manual_keywords=st.session_state.get("keywords", []),
            channel_ids=channel_ids,
            date_start=date_range[0] if isinstance(date_range, tuple) else None,
            date_end=date_range[1] if isinstance(date_range, tuple) else None,
            comments_enabled=st.session_state.get("comments_enabled_opt", True),
            comments_per_post=st.session_state.get("comments_per_post_opt", 20),
            exclude_words=st.session_state.get("exclude_words_opt", []),
            llm_enabled=llm_enabled,
            llm_base_url=base_url,
            llm_model=model_name,
            narrative_enabled=narrative_enabled,
            relevance_check_enabled=relevance_check_enabled,
            channel_params=channel_params,
            review_enabled=st.session_state.get("review_enabled_opt", False),
            exclude_ad_enabled=st.session_state.get("exclude_ad_opt", False),
        )
        # 渠道安全预检：暂停/冷却/配额不足在提交前拦截，避免任务空跑
        blocked = []
        for cfg in plan.channels:
            if cfg.channel_id == "demo":
                continue
            limit = int(cfg.params.get("limit") or plan.per_keyword_limit or 0)
            est = len(plan.keywords) * limit
            ok, reason = jobs.check_channel_allowed(cfg.channel_id, est)
            if not ok:
                blocked.append(f"{cfg.channel_id}（{reason}）")
        if blocked:
            st.error(
                "以下渠道暂不可用，请到「③ 渠道/时间」的渠道安全设置中处理："
                + "；".join(blocked)
            )
            if st.button("← 返回渠道设置", key="stage4_quota_back"):
                st.session_state.stage = 3
                st.rerun()
            st.stop()
        # 任务提交后台队列：关页面/刷新不中断，由常驻 worker 执行
        task_id = jobs.submit_task(plan)
        st.session_state.task_id = task_id
        st.session_state.stage = 5
        st.rerun()

elif stage == 5:
    st.subheader("⑥ 后台执行")
    if st.session_state.get("rerun_notice"):
        st.success(st.session_state.pop("rerun_notice"))
    task_id = st.session_state.get("task_id")
    task = jobs.get_task(task_id) if task_id else None
    if not task:
        st.warning("没有正在执行的任务，请重新提交")
        if st.button("返回确认页", key="stage5_back"):
            st.session_state.stage = 4
            st.rerun()
        st.stop()

    status = task["status"]
    if status == jobs.STATUS_COMPLETED:
        res = open_task_result(task_id)
        if res is None:
            st.error("任务已完成，但结果文件缺失，无法打开")
            st.stop()
        st.session_state.bundle, st.session_state.output_files = res
        st.session_state.stage = 6
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
        snapshot = task.get("step_snapshot") or {}
        step_map = (snapshot.get("steps") or {}) if isinstance(snapshot, dict) else {}
        for sid, label in STEP_DEFS:
            st_data = step_map.get(sid)
            if not st_data:
                st.caption(f"⏳ {label}：等待中")
                continue
            icon = STEP_ICONS.get(st_data.get("state", "pending"), "⏳")
            text = f"{icon} {label}"
            if st_data.get("detail"):
                text += f"：{st_data['detail']}"
            st.caption(text)
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
            st.session_state.stage = 4
            st.rerun()
        if c2.button("🔄 开始新的分析", key="stage5_restart"):
            reset_wizard()
            st.rerun()

    if status == jobs.STATUS_CANCELLED:
        st.info("任务已取消")
        if st.button("🔄 开始新的分析", key="stage5_cancel_restart"):
            reset_wizard()
            st.rerun()

elif stage == 6:
    bundle = st.session_state.get("bundle")
    files = st.session_state.get("output_files", {})
    if not bundle:
        st.warning("没有可展示的结果，请重新开始")
        if st.button("重新开始"):
            reset_wizard()
            st.rerun()
        st.stop()

    s = bundle.summary
    dist = s["sentiment_distribution"]
    st.subheader("⑦ 分析结果")

    for w in bundle.warnings:
        st.warning(w)
    task_id = st.session_state.get("task_id")
    if task_id:
        task_logs = jobs.list_task_logs(task_id, limit=200)
        if any(
            l.get("level") in ("WARNING", "ERROR")
            for l in task_logs
        ):
            _render_task_logs(task_id, max_entries=200, min_level="WARNING")
        task = jobs.get_task(task_id)
        if task:
            fixes = errors.suggest_fixes(task)
            if fixes:
                with st.expander("💡 错误排查与建议", expanded=False):
                    for f in fixes:
                        st.markdown(f"- {f}")

    cookie_issue = any("Cookie" in w for w in bundle.warnings)
    if cookie_issue:
        with st.expander("⚠️ 需要更新微博 Cookie（粘贴后返回重试）", expanded=True):
            new_cookie = st.text_input("新的微博 Cookie", type="password")
            if st.button("保存并返回确认页重试"):
                if new_cookie.strip():
                    st.session_state.weibo_cookie = new_cookie.strip()
                    st.session_state.stage = 4
                    st.rerun()
                else:
                    st.error("请先粘贴新的 Cookie")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("整体倾向", s["overall_sentiment"])
    m2.metric("平均情感分", f"{s['avg_score']:.2f}")
    m3.metric("帖子数", s["total_posts"])
    m4.metric("编码文本数", s["total_items"])
    m5.metric("正面占比", f"{dist['positive']['ratio'] * 100:.1f}%")

    if bundle.plan.llm_enabled:
        methods = Counter(it.method for it in bundle.coded_items)
        st.subheader("LLM 分析状态")
        c1, c2, c3 = st.columns(3)
        c1.metric("LLM 精分析文本数", methods.get("llm", 0))
        c2.metric("词典预筛文本数", methods.get("lexicon", 0))
        corrected = bundle.summary.get("llm_corrected", 0)
        c3.metric("LLM 修正词典判定", corrected)
        llm_errors = [w for w in bundle.warnings if w.startswith("LLM")]
        if methods.get("llm", 0) == 0:
            c3.warning("未产生 LLM 结果")
            st.info("未产生 LLM 精分析结果：请确认侧边栏已填写 API Key 且「测试连接」通过；"
                    "上方 LLM 提示给出了具体原因。")
        else:
            c3.success("LLM 精分析已生效")
        if llm_errors:
            with st.expander("LLM 调用提示详情"):
                for e in llm_errors:
                    st.markdown(f"- {e}")
        if bundle.llm_usage and bundle.llm_usage.get("prompt_tokens"):
            st.caption(
                f"LLM 实际用量：输入 {bundle.llm_usage['prompt_tokens']} / "
                f"输出 {bundle.llm_usage['completion_tokens']} token，"
                f"约 ¥{bundle.llm_usage['estimated_cost']}（以 DeepSeek 官方计费为准）"
            )

    col1, col2 = st.columns(2)
    with col1:
        st.plotly_chart(overall_fig(s), width="stretch")
        st.markdown(f"**解析：**{bundle.chart_insights.get('overall', '')}")
    with col2:
        st.plotly_chart(platform_fig(s), width="stretch")
        st.markdown(f"**解析：**{bundle.chart_insights.get('platform', '')}")

    st.plotly_chart(trend_fig(s), width="stretch")
    st.markdown(f"**解析：**{bundle.chart_insights.get('trend', '')}")

    st.plotly_chart(intensity_fig(s), width="stretch")
    st.markdown(f"**解析：**{bundle.chart_insights.get('intensity', '')}")

    dim_fig = dimensions_fig(s)
    heat_fig = heatmap_fig(s)
    rad_fig = radar_fig(s)
    dd_fig = date_dim_heatmap_fig(s)
    if dim_fig:
        col1, col2 = st.columns(2)
        with col1:
            st.plotly_chart(dim_fig, width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('dimensions', '')}")
        with col2:
            st.plotly_chart(heat_fig, width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('heatmap', '')}")
        if rad_fig:
            st.plotly_chart(rad_fig, width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('radar', '')}")
        if dd_fig:
            st.plotly_chart(dd_fig, width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('date_dim', '')}")

    pd_fig = platform_dim_fig(s)
    if pd_fig:
        st.plotly_chart(pd_fig, width="stretch")
        st.markdown(f"**解析：**{bundle.chart_insights.get('platform_dim', '')}")

    st.plotly_chart(words_fig(s), width="stretch")
    st.markdown(f"**解析：**{bundle.chart_insights.get('words', '')}")
    st.subheader("情感词云（按情感拆分，权重 = 词频 × 情感强度）")
    for which, caption in (
        ("positive", "正面讨论词云"),
        ("negative", "负面讨论词云"),
        ("worst_dim", "负面率最高维度词云"),
    ):
        wc_bytes = wordcloud_png_bytes(s, which)
        if wc_bytes:
            st.image(wc_bytes, caption=caption, width=700)
    st.markdown(f"**解析：**{bundle.chart_insights.get('wordcloud', '')}")

    co_fig = cooccurrence_fig(s)
    if co_fig:
        st.plotly_chart(co_fig, width="stretch")
        st.markdown(f"**解析：**{bundle.chart_insights.get('cooccurrence', '')}")
    src_fig = sentiment_sources_fig(s)
    if src_fig:
        st.plotly_chart(src_fig, width="stretch")

    st.subheader("概览")
    st.markdown(bundle.report_text)

    st.subheader("深度结论与建议（按叙事框架）")
    st.markdown(bundle.conclusion)

    st.subheader("下载报告")
    d1, d2, d3, d4 = st.columns(4)
    d1.download_button(
        "📥 原始数据 Excel",
        data=files.get("excel", b""),
        file_name=f"{bundle.plan.subject}_原始数据与编码.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )
    d2.download_button(
        "📥 HTML 交互报告",
        data=files.get("html", ""),
        file_name=f"{bundle.plan.subject}_分析报告.html",
        mime="text/html",
        width="stretch",
    )
    if "word_bytes" in st.session_state:
        d3.download_button(
            "📥 Word 报告（含图表）",
            data=st.session_state.word_bytes,
            file_name=f"{bundle.plan.subject}_分析报告.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            width="stretch",
        )
    else:
        if d3.button("⏳ 生成 Word 报告（含图表图片，约 30~60 秒）", width="stretch"):
            with st.spinner("正在渲染图表图片并生成 Word 报告…"):
                st.session_state.word_bytes = build_word(bundle).getvalue()
            st.rerun()
    d4.download_button(
        "📥 结果 JSON",
        data=bundle.model_dump_json(indent=2),
        file_name=f"{bundle.plan.subject}_result.json",
        mime="application/json",
        width="stretch",
    )

    if st.button("🔄 开始新的分析", width="stretch"):
        reset_wizard()
        st.rerun()


# 顶部分步提示
st.divider()
steps = ["输入对象", "维度", "关键词", "渠道/时间", "确认运行", "后台执行", "结果"]
current = min(int(st.session_state.get("stage", 0)), 6)
st.caption("步骤：" + " → ".join(f"{'●' if i == current else '○'} {s}" for i, s in enumerate(steps)))
