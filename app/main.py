"""社交媒体情感分析器 — Streamlit 向导入口。

运行：python -m streamlit run app/main.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
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
from app.core import feedback
from app.core import lifecycle
from app.core import plans_store
from app.core import terms
from app.core import usage_boundary
from app.core.models import ReportBundle
from app.core.planner import MAX_CUSTOM_DIMENSIONS, build_plan, parse_custom_dimensions
from app.core.keyword_effects import expand_channel_queries, expand_websearch_keywords
from app.core.pipeline import (
    bundle_to_json,
    generate_report_text,
    recompute_summary,
)
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
from app.coding.insights import (
    build_descriptors,
    build_report_content,
    template_chart_insights,
)
from app.core.evidence import (
    dimension_evidence_label,
    display_action,
    display_finding_id,
    build_evidence,
    build_findings,
    findings_to_conclusion,
    findings_section_title,
)
from app.coding.coder import display_confidence
from app.domains.composer import (
    MAX_DIMENSIONS,
    MODULE_DESC,
    compose_schema,
    filter_schema_dims,
    module_name,
)
from app.domains.loader import save_cached_schema
from app.core.names import dimension_cn, platform_cn, register_custom_dim_names
from app.output.html_report import (
    _collection_notes,
    cooccurrence_fig,
    cooccurrence_plan,
    sentiment_sources_fig,
    topic_cluster_rows,
    topic_pairs,
    date_dim_heatmap_fig,
    dimensions_fig,
    heatmap_fig,
    intensity_fig,
    narrative_actor_fig,
    narrative_frame_actor_heatmap,
    narrative_insight_text,
    overall_fig,
    platform_dim_fig,
    platform_fig,
    radar_fig,
    trend_fig,
    words_fig,
    wordcloud_png_bytes,
)
from app.output.html_report import build_html
from app.output.excel_writer import build_excel
from app.output.word_report import build_word
from app.ui.theme import inject_global_css

st.set_page_config(page_title="社交媒体情感分析器", page_icon="📊", layout="wide")
# P0-2：全局视觉主题注入（令牌 CSS，只改视觉层）
inject_global_css()

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

# P1-2：线性 SVG 状态图标（stroke=currentColor，颜色由外层样式控制），替换步骤 emoji
_SVG_WRAP = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
    'stroke-linejoin="round">{body}</svg>'
)
_STEP_STATE_SVG = {
    "pending": _SVG_WRAP.format(body='<circle cx="12" cy="12" r="9"/>'),
    "running": _SVG_WRAP.format(
        body='<path d="M21 12a9 9 0 1 1-6.2-8.5"/>'
    ),
    "done": _SVG_WRAP.format(body='<path d="M20 6 9 17l-5-5"/>'),
    "skipped": _SVG_WRAP.format(
        body='<polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19"/>'
    ),
    "failed": _SVG_WRAP.format(
        body='<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>'
    ),
}
_WIZARD_STEP_ICONS = [
    # 品牌和维度 / 关键词 / 渠道时间 / 确认运行 / 后台执行 / 结果
    _SVG_WRAP.format(
        body=('<rect x="3" y="3" width="7" height="7" rx="1"/>'
              '<rect x="14" y="3" width="7" height="7" rx="1"/>'
              '<rect x="3" y="14" width="7" height="7" rx="1"/>'
              '<rect x="14" y="14" width="7" height="7" rx="1"/>')
    ),
    _SVG_WRAP.format(
        body='<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>'
    ),
    _SVG_WRAP.format(
        body=('<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/>'
              '<path d="M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20"/>')
    ),
    _SVG_WRAP.format(
        body=('<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>'
              '<rect x="8" y="2" width="8" height="4" rx="1"/>'
              '<path d="M9 14l2 2 4-4"/>')
    ),
    _SVG_WRAP.format(
        body=('<path d="M12 2v4M12 18v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8"/>'
              '<path d="M2 12h4M18 12h4M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8"/>')
    ),
    _SVG_WRAP.format(
        body='<path d="M3 3v18h18"/><path d="M7 15v-4M12 15V8M17 15v-6"/>'
    ),
]

CHANNEL_LIMIT_DEFAULTS = {
    "demo": 10,
    "bilibili": 10,
    "websearch": 13,
    "websearch_zhihu": 13,
    "websearch_tieba": 13,
    "websearch_taptap": 13,
    "weibo": 10,
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
    "bilibili": "B站公开 API、零登录最安全：默认 10、上限 50；加量建议加关键词",
    "weibo": "微博账号级风控最严：默认 20、上限 30，单关键词约 500 条封顶",
    "xiaohongshu": "小红书反爬最严（xsec_token+签名）：默认/封顶 10，单次建议 ≤10",
    "demo": "演示数据固定 2 条/平台/关键词，限额不影响产量",
}

# 1（2026-08-19 UX 调整）：小白获取 API Key 的操作指引（侧边栏大模型设置内展示）
API_KEY_GUIDE = (
    "**DeepSeek（推荐，便宜）**\n"
    "1. 打开 https://platform.deepseek.com 并注册/登录；\n"
    "2. 左侧菜单进入「API Keys」→ 点「创建 API Key」；\n"
    "3. 复制生成的 Key（只显示一次，关掉就看不到了）；\n"
    "4. 粘贴到上方输入框并「保存到本机」。\n"
    "5. 首次使用需在「充值」页面充值（最低 ¥10 起），否则会报余额不足。\n\n"
    "**OpenAI**\n"
    "1. 打开 https://platform.openai.com 并登录；\n"
    "2. 右上角头像 →「API keys」→「Create new secret key」；\n"
    "3. 复制 Key（只显示一次）后粘贴到上方输入框。\n\n"
    "⚠️ Key 相当于付款凭证：只粘贴到你自己的电脑，程序会加密保存在本机，"
    "不会上传或写入报告。"
)
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
# 广告/官方与人工复核：三选一（方案 A，2026-08-16）
AD_REVIEW_MODES = [
    "自动（广告/官方计入统计）",
    "自动（按规则剔除广告/官方）",
    "人工复核（采集后暂停，相关性+广告/官方一起审，标记的广告/官方剔除统计）",
]
AD_REVIEW_SHORT = {
    AD_REVIEW_MODES[0]: "自动（广告计入统计）",
    AD_REVIEW_MODES[1]: "自动（规则剔除广告）",
    AD_REVIEW_MODES[2]: "人工复核（相关性+广告/官方，标记剔除）",
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
    # 深度探针按 WebSearch 组只显示一个：三引擎探测只与网络出口有关，
    # 与子渠道（zhihu/tieba/taptap 等）无关，避免每行一个冗余按钮。
    if any(str(cid).startswith("websearch") for cid in results):
        probe_key = "diag_probe_websearch"
        result_key = "diag_probe_result_websearch"
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


def apply_need_review_feedback(
    task_id: str,
    text_id: str,
    sentiment: str,
    text: str | None = None,
    reviewed_by: str = "用户复核",
) -> bool:
    """2.11 / 8：回填单条判定到 result.json 对应编码条目。

    7 修复（2026-08-19）：旧任务同一帖子的多条评论共用 text_id
    （如 URL:comment），原实现只改第一条 → 复核永远剩 N 条无法清零。
    现按原文精确匹配；新任务 text_id 已加评论序号（coder.py），唯一。
    8：反馈「这条判错了」也走此函数（任意文本可回填，非 need_review 也可）。
    """
    task = jobs.get_task(task_id)
    if not task or not task.get("output_dir"):
        return False
    rp = Path(task["output_dir"]) / "result.json"
    if not rp.exists():
        return False
    try:
        data = json.loads(rp.read_text(encoding="utf-8"))
        matches = [
            it for it in data.get("coded_items", [])
            if it.get("text_id") == text_id
        ]
        if not matches:
            return False
        target = matches[0]
        if len(matches) > 1 and text is not None:
            exact = [
                it for it in matches
                if (it.get("text") or "").strip() == (text or "").strip()
            ]
            if len(exact) == 1:
                target = exact[0]
        target["sentiment"] = sentiment
        target["need_review"] = False
        target["need_review_reason"] = ""
        target["reviewed_by"] = reviewed_by
        rp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except (OSError, ValueError):
        return False


def _reload_bundle(task_id: str):
    """轻量重载：从任务 result.json 重建 bundle（复核/反馈回填后立即用）。

    不走 open_task_result 的完整文件装配，避免"重载失败导致界面停留在
    旧复核状态"（7 修复的一部分）。
    """
    task = jobs.get_task(task_id)
    if not task or not task.get("output_dir"):
        return None
    rp = Path(task["output_dir"]) / "result.json"
    if not rp.exists():
        return None
    try:
        return ReportBundle.model_validate(
            json.loads(rp.read_text(encoding="utf-8"))
        )
    except Exception:
        return None


def _apply_review_item(task_id: str, item, sentiment: str) -> None:
    """2.11 单条复核回填统一回调：写盘 → 重载 → 最后一条自动重建报告。"""
    if not (
        task_id
        and apply_need_review_feedback(
            task_id, item.text_id, sentiment, text=item.text
        )
    ):
        st.error("回填失败：找不到任务结果文件")
        return
    bundle2 = _reload_bundle(task_id)
    if bundle2 is not None:
        st.session_state.bundle = bundle2
    remaining = [
        x for x in st.session_state.bundle.coded_items
        if x.need_review and not x.reviewed_by
    ]
    if not remaining:
        r2 = rebuild_report_after_review(task_id, st.session_state.bundle)
        if r2:
            st.session_state.bundle, st.session_state.output_files = r2
            st.success(
                "已回填并刷新报告：全部需复核样本已确认，"
                "统计/图表/Excel/HTML 已按复核结果重算。"
            )
        else:
            st.success("已回填本条（报告自动刷新失败：结果文件不可写）")
    else:
        st.success(
            f"已回填 {item.text_id} → {sentiment}"
            f"（还剩 {len(remaining)} 条，全部确认后报告自动重算）"
        )
    st.rerun()


def _render_dim_charts(s: dict, bundle) -> None:
    """结果页维度区：维度/热力图/雷达/日期维度图 + 负面原文 Top3（P1-1 抽取共用）。"""
    dim_fig = dimensions_fig(s)
    heat_fig = heatmap_fig(s)
    rad_fig = radar_fig(s)
    dd_fig = date_dim_heatmap_fig(s)
    if not dim_fig:
        return
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
    dim_neg = {}
    for c in bundle.evidence:
        if c.get("dimension") and c.get("sentiment") == "negative":
            dim_neg.setdefault(c["dimension"], []).append(c)
    if dim_neg:
        st.markdown("**各维度负面原文 Top 3（规则抽取）**")
        for dim, cards in dim_neg.items():
            with st.expander(
                f"{dimension_cn(dim)}（{dimension_evidence_label(cards)}）",
                expanded=False,
            ):
                for c in cards:
                    label = " · 待复核" if c.get("need_review") else ""
                    st.markdown(
                        f"- 「{c.get('text', '')}」 — {c.get('platform', '')} · "
                        f"{c.get('date') or '日期未知'}{label}"
                    )


def rebuild_report_after_review(
    task_id: str, bundle: ReportBundle, regenerate_insights: bool = False,
) -> tuple[ReportBundle, dict] | None:
    """2.11 方案 A：复核后重算 summary/证据/发现/图表（可选重生成 LLM 深度结论），
    并落盘 result.json + result.xlsx + report.html，返回新 bundle + 可下载文件。

    修复（报告证据链）：复核改动了情感 → summary 数字变化 → 旧证据卡与旧发现
    全部过期。因此**总是**重建证据并重生成结论（规则路径，确定性、零费用）；
    原为 LLM 模式时置 review_refresh 标记（banner 明示"按复核结果用规则重新
    生成"），需要 LLM 深度结论时由调用方传 regenerate_insights=True。"""
    task = jobs.get_task(task_id)
    if not task or not task.get("output_dir"):
        return None
    out_dir = Path(task["output_dir"])
    channel_results = bundle.channel_results
    posts = [p for ch in channel_results for p in ch.posts]
    new_summary = recompute_summary(
        bundle.plan, bundle.coded_items, channel_results, posts)
    evidence = build_evidence(
        bundle.coded_items, new_summary,
        exclude_ad=bool(bundle.plan.exclude_ad_enabled),
    )
    if regenerate_insights and bundle.plan.llm_enabled:
        analyzer = create_analyzer(api_key=api_key, base_url=base_url, model=model_name)
        report_content = build_report_content(
            analyzer, bundle.plan, new_summary, evidence)
    else:
        # 规则路径：证据/图表解析/发现全部按复核结果重建（确定性、零费用）
        if bundle.insight_mode in ("llm", "template_fallback"):
            rule_mode, new_mode = "review", "review_refresh"
        else:
            rule_mode, new_mode = "lexicon", "lexicon"
        findings = build_findings(evidence, new_summary, mode=rule_mode)
        report_content = {
            "chart_insights": template_chart_insights(build_descriptors(new_summary)),
            "findings": findings,
            "conclusion": findings_to_conclusion(findings),
            "insight_mode": new_mode,
        }
    new_bundle = bundle.model_copy(update={
        "summary": new_summary,
        "evidence": evidence,
        "chart_insights": report_content["chart_insights"],
        "conclusion": report_content["conclusion"],
        "findings": report_content["findings"],
        "insight_mode": report_content["insight_mode"],
        "report_text": generate_report_text(
            bundle.plan, new_summary, report_content["findings"]),
    })
    try:
        bundle_to_json(new_bundle, out_dir / "result.json")
        excel_bytes = build_excel(new_bundle).getvalue()
        (out_dir / "result.xlsx").write_bytes(excel_bytes)
        html_content = build_html(new_bundle)
        (out_dir / "report.html").write_text(html_content, encoding="utf-8")
    except Exception:
        return None
    return new_bundle, {"excel": excel_bytes, "html": html_content}


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


REVIEW_PAGE_SIZE = 20


def _review_key(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.md5(value.encode('utf-8')).hexdigest()[:12]}"


def _render_review_view(task: dict, task_id: str) -> None:
    """人工筛选 × 广告/官方复核 统一面板：同一条内容一次看完、两个判断一次勾完。

    相关性（不相关=剔除）与广告/官方（默认计入、剔除仅影响情感统计）判断的是
    同一批内容，合并到同一行避免重复阅读。"""
    st.subheader("👀 人工筛选 × 广告/官方复核")
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

    st.caption(
        "同一行两个判断：**不相关** = 剔除（帖子随帖评论级联）；"
        "**广告/官方** = 默认计入，开启「剔除广告/官方内容」时仅从情感统计剔除"
        "（🔖 规则预标为建议，人工最终决定）。"
    )
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
        f"手动剔除评论 {len(cid_set)} 条；已标广告/官方 {len(ad_url_set)} 帖 · "
        f"{len(ad_cid_set)} 条评论；筛选后剩余 {remaining_posts} 帖 / "
        f"{remaining_comments} 条评论"
    )

    c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
    platform = c1.selectbox(
        "平台", ["全部"] + sorted({p["platform"] for p in posts}),
        key=f"rv_platform_{task_id}",
    )
    only_pending = c2.checkbox("只看未判定", key=f"rv_pending_{task_id}",
                               help="相关性与广告/官方都尚未处理")
    only_llm = c3.checkbox("只看 LLM 建议不相关", key=f"rv_llm_{task_id}")
    only_ad_suggested = c4.checkbox("只看广告预标", key=f"rv_ad_suggested_{task_id}")
    tc1, tc2, tc3 = st.columns(3)
    if tc1.button("全部标记不相关", key=f"rv_all_{task_id}"):
        for p in posts:
            url_set.add(p["url"])
        st.rerun()
    if tc2.button("全部标记广告/官方", key=f"rv_ad_all_{task_id}"):
        for p in posts:
            ad_url_set.add(p["url"])
            for c in p.get("comments") or []:
                ad_cid_set.add(c["id"])
        st.rerun()
    if tc3.button("重置筛选（相关+广告）", key=f"rv_reset_{task_id}"):
        st.session_state[url_key] = set()
        st.session_state[cid_key] = set()
        st.session_state[ad_url_key] = set()
        st.session_state[ad_cid_key] = set()
        st.rerun()

    filtered = posts
    if platform != "全部":
        filtered = [p for p in filtered if p["platform"] == platform]
    if only_pending:
        filtered = [
            p for p in filtered
            if p["url"] not in url_set and p["url"] not in ad_url_set
        ]
    if only_llm:
        filtered = [p for p in filtered if p.get("llm_relevant") is False]
    if only_ad_suggested:
        from app.coding.ad_rules import is_ad

        filtered = [
            p for p in filtered
            if is_ad(p.get("content") or "", p.get("title") or "")
            or any(is_ad(c.get("text") or "") for c in (p.get("comments") or []))
        ]

    total_pages = max(1, (len(filtered) + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
    page = min(int(st.session_state.get(page_key, 1) or 1), total_pages)
    start = (page - 1) * REVIEW_PAGE_SIZE
    for p in filtered[start : start + REVIEW_PAGE_SIZE]:
        _render_review_post(p, url_set, cid_set, ad_url_set, ad_cid_set, task_id)
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

def _render_review_post(
    p: dict,
    url_set: set,
    cid_set: set,
    ad_url_set: set,
    ad_cid_set: set,
    task_id: str,
) -> None:
    """单条帖子：不相关 + 广告/官方 两个判断同行完成；评论同贴逐条处理。"""
    from app.coding.ad_rules import is_ad

    url = p["url"]
    excluded = url in url_set
    ad_marked = url in ad_url_set
    title = (p.get("title") or p.get("content") or "（无标题）")[:60]
    meta = (
        f"{platform_cn(p['platform'])} · {p.get('timestamp') or '时间未知'} · "
        f"点赞 {p.get('likes', 0)} · 关键词 {p.get('keyword') or '-'}"
    )
    suggested = is_ad(p.get("content") or "", p.get("title") or "")
    c1, c2, c3 = st.columns([4, 1, 1])
    with c1:
        st.markdown(f"{'🚫 ' if excluded else ''}{'📢 ' if ad_marked else ''}**{title}**")
        st.caption(meta)
        if p.get("llm_relevant") is False:
            st.caption("🔖 LLM 建议不相关（人工最终决定）")
        if suggested:
            st.caption(f"🔖 广告规则预标：{suggested}")
    mark = c2.checkbox("不相关", value=excluded, key=_review_key("rv", url))
    if mark != excluded:
        (url_set.add if mark else url_set.discard)(url)
        st.rerun()
    aflag = c3.checkbox("广告/官方", value=ad_marked, key=_review_key("rad", url))
    if aflag != ad_marked:
        (ad_url_set.add if aflag else ad_url_set.discard)(url)
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
                cm = c["id"] in ad_cid_set
                csug = is_ad(c.get("text") or "")
                cc1, cc2, cc3 = st.columns([4, 1, 1])
                cc1.caption(
                    f"{c.get('author') or '匿名'}：{c.get('text')}"
                    + (f"　🔖 {csug}" if csug else "")
                )
                rm = cc2.checkbox("剔除", value=marked, key=_review_key("rc", c["id"]))
                if rm != marked:
                    (cid_set.add if rm else cid_set.discard)(c["id"])
                    st.rerun()
                rm2 = cc3.checkbox("广告/官方", value=cm, key=_review_key("radc", c["id"]))
                if rm2 != cm:
                    (ad_cid_set.add if rm2 else ad_cid_set.discard)(c["id"])
                    st.rerun()
        else:
            st.caption("（该帖无评论）")


def estimate_collection(
    keywords: list[str],
    channel_ids: list[str],
    limits: dict[str, int],
    comments_per_post: int,
    comments_enabled: bool = True,
    channel_queries: dict[str, list[str]] | None = None,
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
        qs = (channel_queries or {}).get(cid)
        k = max(len(qs) if qs is not None else len(keywords), 1)
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
        "stage", "mode", "subject", "domain_id", "selected_dims", "selected_modules",
        "schema", "custom_dim", "custom_dimensions", "custom_dim_errors",
        "manual_keywords", "keyword_groups",
        "websearch_eval_suffix", "keywords",
        "kwopt_bilibili", "kwopt_weibo", "kwopt_xiaohongshu",
        "channel_ids", "date_range", "plan", "bundle", "output_files", "task_id",
        "exclude_words", "exclude_words_opt",
    ]:
        st.session_state.pop(key, None)
    st.session_state.stage = 0


def next_stage() -> None:
    st.session_state.stage = int(st.session_state.get("stage", 0)) + 1


def prev_stage() -> None:
    st.session_state.stage = max(int(st.session_state.get("stage", 0)) - 1, 0)


def _submit_demo_plan() -> None:
    """一键体验（4.1）：demo 渠道 + 示例品牌预填提交，只跑演示数据。"""
    demo_plan = build_plan(
        subject="瑞幸",
        domain_id=None,
        dimension_ids=[],
        keyword_groups=[],
        manual_keywords=["瑞幸"],
        channel_ids=["demo"],
        date_start=dt.date.today() - dt.timedelta(days=7),
        date_end=dt.date.today(),
        comments_enabled=False,
        comments_per_post=0,
        exclude_words=[],
        llm_enabled=False,
        narrative_enabled=False,
        relevance_check_enabled=False,
    )
    task_id = jobs.submit_task(demo_plan)
    st.session_state.task_id = task_id
    st.session_state.stage = 4


def _prefill_wizard_from_plan(plan: dict) -> None:
    """4.3 失败重试 + 5.1 恢复上次计划/模板：把任务计划完整预填回向导。

    P1 修复（2026-08-19 审查）：此前只恢复 8 个字段且键名用错
    （plan 实际是 AnalysisPlan.model_dump：dimensions/channels，旧代码读
    dimension_ids/channel_ids 取不到），渠道上限/自定义维度/复核模式/
    WebSearch 优化/LLM/叙事开关全部丢失；comments/exclude_words 的
    镜像键对 widget 不生效。现按真实结构完整恢复。
    """
    st.session_state.subject = plan.get("subject", "")
    st.session_state._restore_wizard_mode = (
        "手动关键词（不分类）"
        if str(plan.get("subject") or "") == "手动关键词"
        else "按品牌分析（推荐）"
    )
    st.session_state.domain_id = plan.get("domain_id")
    st.session_state.selected_dims = list(
        plan.get("dimensions") or plan.get("dimension_ids") or []
    )
    st.session_state.keywords = list(plan.get("keywords") or [])
    st.session_state.keyword_groups = plan.get("keyword_groups") or []
    channels = plan.get("channels") or []
    channel_ids = [
        c.get("channel_id") for c in channels if c.get("channel_id")
    ] or list(plan.get("channel_ids") or [])
    st.session_state.channel_ids = channel_ids or ["demo"]
    # 渠道级参数：上限 / 官方域名 / WebSearch 优化 / 微博关键词展开
    limits: dict[str, int] = {}
    official_domains = ""
    ws_suffix = None
    kwopt_weibo = False
    for c in channels:
        cid = c.get("channel_id")
        params = c.get("params") or {}
        if not cid:
            continue
        if params.get("limit") is not None:
            limits[cid] = int(params["limit"])
        if cid.startswith("websearch") and params.get("official_domains"):
            official_domains = str(params["official_domains"])
        if cid.startswith("websearch") and "eval_suffix" in params:
            ws_suffix = str(params["eval_suffix"]) != "0"
        if cid == "weibo" and params.get("queries"):
            kwopt_weibo = True
    if limits:
        st.session_state.channel_limits = limits
        # 9 修复：渠道上限的 number_input 有 widget key（limit_<cid>），
        # 只设镜像 channel_limits 会被控件默认值覆盖 → 直接预填 widget key
        for cid, v in limits.items():
            st.session_state[f"limit_{cid}"] = v
    if official_domains:
        st.session_state.official_domains = official_domains
    if ws_suffix is not None:
        st.session_state.websearch_eval_suffix = ws_suffix
        st.session_state._restore_websearch_eval_suffix_toggle = ws_suffix
    if kwopt_weibo:
        st.session_state.kwopt_weibo = True
        st.session_state._restore_kwopt_weibo_toggle = True
    ds, de = plan.get("date_start"), plan.get("date_end")
    try:
        if ds and de:
            st.session_state.date_range = (
                dt.date.fromisoformat(str(ds)),
                dt.date.fromisoformat(str(de)),
            )
    except (ValueError, TypeError):
        pass
    # widget key 统一走延迟应用（顶部 _restore_ 块在 widget 渲染前落位；
    # 避免"widget 已实例化后修改 session_state"报错）
    st.session_state._restore_comments_enabled = bool(plan.get("comments_enabled"))
    _restore_cpp = int(plan.get("comments_per_post") or 0)
    st.session_state._restore_comments_per_post = min(max(_restore_cpp, 0), 10)
    st.session_state._restore_exclude_words = "、".join(
        plan.get("exclude_words") or []
    )
    review_on = bool(plan.get("review_enabled"))
    exclude_ad = bool(plan.get("exclude_ad_enabled"))
    st.session_state.review_enabled_opt = review_on
    st.session_state.exclude_ad_opt = exclude_ad
    st.session_state._restore_ad_review_mode = AD_REVIEW_MODES[
        2 if review_on else (1 if exclude_ad else 0)
    ]
    # 自定义维度：list[dict] → 向导文本（维度名：关键词1,关键词2）
    custom_dims = plan.get("custom_dimensions") or []
    if custom_dims:
        st.session_state.custom_dim = "\n".join(
            f"{d.get('name')}：{','.join(d.get('keywords') or [])}"
            for d in custom_dims
        )
        st.session_state.custom_dimensions = custom_dims
    # LLM / 叙事开关（sidebar widget，延迟应用）
    st.session_state._restore_llm_enabled = bool(plan.get("llm_enabled"))
    st.session_state._restore_narrative_enabled = bool(plan.get("narrative_enabled"))
    # 模块反查 + schema 物化（domain_id → modules，供确认页模块行与 worker 维度）
    mods = _modules_for_domain(plan.get("domain_id"))
    st.session_state.selected_modules = mods
    if mods:
        try:
            schema = compose_schema(mods)
            st.session_state.schema = schema
            save_cached_schema(
                filter_schema_dims(schema, st.session_state.selected_dims)
            )
        except Exception:
            pass
    else:
        st.session_state.schema = None


def _modules_for_domain(domain_id: str | None) -> list[str]:
    """domain_id（modules_content_physical）→ 模块列表反查。"""
    if not domain_id or not domain_id.startswith("modules_"):
        return []
    parts = domain_id[len("modules_"):].split("_")
    return [p for p in parts if p in ("content", "physical", "service")]


def _empty_state(icon: str, title: str, why: str, action: str = "") -> None:
    """4.2 统一空态：回答"为什么没有 + 怎么才有"。"""
    st.markdown(f"**{icon} {title}**")
    st.caption(why)
    if action:
        st.caption(f"💡 {action}")


# ---------------------------------------------------------------------------
# 恢复计划的 widget 预填延迟应用（P1 修复）：
# sidebar/stage0 的带 key widget 在点击"恢复"时已实例化，Streamlit 禁止
# 直接改写；改为存延迟键，由本块在下一次运行、widget 渲染前统一应用。
# ---------------------------------------------------------------------------
for _wk in (
    "llm_enabled", "narrative_enabled", "wizard_mode",
    "comments_enabled", "comments_per_post", "exclude_words", "ad_review_mode",
    "websearch_eval_suffix_toggle", "kwopt_weibo_toggle",
):
    _restored_val = st.session_state.pop(f"_restore_{_wk}", None)
    if _restored_val is not None:
        st.session_state[_wk] = _restored_val


# ---------------------------------------------------------------------------
# 侧边栏：大模型与高级设置
# ---------------------------------------------------------------------------

with st.sidebar:
    # 4.1 首次引导：快速上手卡（首次会话显示、可关闭、不重复）
    if not st.session_state.get("quickstart_dismissed"):
        st.markdown("### 🚀 快速上手")
        st.markdown(
            "三步开始：\n"
            "1. **一键体验演示数据**（1 分钟跑通全流程）；\n"
            "2. 换真实渠道（微博 / B站 等）；\n"
            "3. 开启 LLM 精分析，结论更可归因。"
        )
        qc1, qc2 = st.columns(2)
        if qc1.button(
            "▶️ 一键体验", type="primary", width="stretch", key="quickstart_demo"
        ):
            st.session_state["quickstart_dismissed"] = True
            _submit_demo_plan()
            st.rerun()
        if qc2.button("知道了", width="stretch", key="quickstart_dismiss"):
            st.session_state["quickstart_dismissed"] = True
            st.rerun()
        st.divider()

    st.header("⚙ 大模型设置")
    presets = {
        "DeepSeek": ("https://api.deepseek.com", "deepseek-chat"),
        "OpenAI": ("https://api.openai.com/v1", "gpt-4o-mini"),
    }
    # 1（2026-08-19）：LLM 开关先行，开启后才展示 Key/服务商配置区；
    # P1-2 默认词典模式（离线可跑），LLM 由用户主动开启
    llm_enabled = st.toggle(
        "启用 LLM 精分析",
        value=False,
        key="llm_enabled",
        help="默认词典模式（离线可跑，不发送数据）；开启后低置信度文本将发送给"
             "所选服务商，可提高准确率（按量计费）",
    )
    # 2.8（2026-08-18）：LLM 相关性复核随 LLM 自动开启，不再提供独立开关
    relevance_check_enabled = llm_enabled
    if llm_enabled:
        st.warning(
            "⚠️ 开启后，低置信度文本将发送给所选服务商（DeepSeek/OpenAI 等），"
            "请勿输入含个人敏感信息的内容。"
        )
        with st.expander("❓ 如何获取 API Key（小白版）"):
            st.markdown(API_KEY_GUIDE)
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
        base_url = st.text_input(
            "Base URL", key="base_url",
            help="DeepSeek：https://api.deepseek.com；OpenAI：https://api.openai.com/v1",
        )
        model_name = st.text_input(
            "模型名", key="model_name",
            help="DeepSeek 用 deepseek-chat；推理模型 deepseek-reasoner 较慢",
        )
        if st.button("🔌 测试连接", width="stretch"):
            if not api_key:
                st.warning("请先填写 API Key 再测试")
            else:
                probe = create_analyzer(
                    api_key=api_key, base_url=base_url, model=model_name
                )
                ok, msg = probe.ping()
                if ok:
                    st.success(f"连接正常 ✅ {base_url} / {model_name}")
                else:
                    st.error(f"连接失败：{msg}")
    else:
        # LLM 关闭时给确认页兜底定义（不渲染配置控件，避免 NameError）
        api_key = ""
        base_url = presets["DeepSeek"][0]
        model_name = presets["DeepSeek"][1]
    st.divider()
    # 4（2026-08-19）：渠道风控安全从③渠道页移入"高级选项"；叙事/归因同组
    with st.expander("高级选项（渠道风控安全/叙事归因，默认收起）", expanded=False):
        narrative_enabled = st.toggle(
            "叙事框架/归因分析", value=False, key="narrative_enabled",
            help="高级模式：分析文本的叙事框架与责任归因（默认关）",
        )
        st.caption(
            "仅对 LLM 精分析过的文本执行（成本控制设计）；归因/框架为固定词表。"
        )
        st.caption(
            "提示：环境变量 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL 仅用于"
            "开发/评测脚本，应用内 Key 只存本机 DPAPI。"
        )
        st.divider()
        st.markdown("**渠道风控安全（每日配额）**")
        st.caption(
            "每日配额按「关键词数 × 每关键词上限」估算消耗；微博/小红书有默认上限，"
            "可在下方调整（-1=不限；不限的渠道不在此显示）。"
            "检测到风控时渠道会自动冷却，可在此手动解除。"
        )
        _quota_channels = list(st.session_state.get("channel_ids") or [])
        _quota_infos = {i["id"]: i["name"] for i in list_channel_infos()}
        quota_save: dict[str, int] = {}
        if not _quota_channels:
            st.caption("未选择采集渠道，暂无可管理的风控设置。")
        for cid in _quota_channels:
            s = jobs.channel_state(cid)
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
            q1.caption(f"{_quota_infos.get(cid, cid)}：{status}")
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
        if _quota_channels and st.button("保存配额设置", key="save_quota"):
            for cid, val in quota_save.items():
                jobs.set_quota_limit(cid, val)
            st.success("每日配额已保存")
            st.rerun()
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
            st.divider()
            # P1-4 一键清除全部数据（删除不可恢复，优先回收站；双重确认）
            if st.button(
                "⚠️ 一键清除全部数据（报告/任务/日志/Cookie/Key）",
                key="lifecycle_clear_all",
                width="stretch",
            ):
                st.session_state["lifecycle_confirm_clear_all"] = True
            if st.session_state.get("lifecycle_confirm_clear_all"):
                _cv = lifecycle.clear_all_data(dry_run=True)
                st.warning(
                    f"将删除 {_cv['reports_deleted']} 个报告目录（含归档）、全部任务记录、"
                    f"日志、已存 Cookie/API Key、上次计划与命名模板，并重置《使用边界》确认"
                    "（重启后重新确认）；"
                    f"预计释放 {_cv['freed_bytes'] / 1048576:.1f} MB。"
                    "删除不可恢复（优先回收站）；演示数据与黄金集夹具不删除。"
                )
                cc1, cc2 = st.columns(2)
                if cc1.button(
                    "确认全部清除（不可恢复）",
                    key="lifecycle_clear_all_yes",
                    type="primary",
                    width="stretch",
                ):
                    try:
                        with lifecycle.LifecycleLock():
                            res = lifecycle.clear_all_data(dry_run=False)
                        st.session_state.pop("lifecycle_confirm_clear_all", None)
                        st.success(
                            f"已清除 {res['reports_deleted']} 个报告目录，"
                            f"释放 {res['freed_bytes'] / 1048576:.1f} MB；"
                            "Key/Cookie 已清除，使用边界确认已重置。"
                        )
                        st.rerun()
                    except RuntimeError as exc:
                        st.warning(str(exc))
                if cc2.button("取消", key="lifecycle_clear_all_no", width="stretch"):
                    st.session_state.pop("lifecycle_confirm_clear_all", None)
                    st.rerun()
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
    st.subheader("① 品牌和分析维度确认")
    # UX 5.1 复用与连续性：最近一次计划 + ≤5 命名模板（免重填）
    _last_plan = plans_store.load_last_plan()
    _templates = plans_store.list_templates()
    if _last_plan or _templates:
        with st.expander("♻️ 复用之前的计划（免重填）", expanded=True):
            if _last_plan:
                _lp = _last_plan
                r1, r2 = st.columns([3, 1])
                r1.caption(
                    f"**最近一次**：{_lp.get('subject') or '未命名'} · "
                    f"{len(_lp.get('keywords') or [])} 个关键词 · "
                    f"{'、'.join(c.get('channel_id', '') for c in _lp.get('channels') or []) or '—'} · "
                    f"保存于 {(_lp.get('_saved_at') or '')[:16]}"
                )
                if r2.button("↩ 恢复上次计划", key="reuse_last", type="primary", width="stretch"):
                    _prefill_wizard_from_plan(_lp)
                    st.session_state.plan_restored_notice = (
                        f"已恢复上次计划「{_lp.get('subject') or '未命名'}」，"
                        "请核对关键词、渠道、时间与高级参数（渠道上限/自定义维度/复核模式已一并恢复）。"
                    )
                    st.session_state.stage = 1
                    st.rerun()
            if _templates:
                _tmap = {
                    t["id"]: f"{t['name']}（{(t.get('updated_at') or '')[:16]}）"
                    for t in _templates
                }
                tpick = st.selectbox(
                    "命名模板",
                    options=list(_tmap),
                    format_func=lambda i: _tmap[i],
                    key="reuse_tmpl_pick",
                    help="保存的常用分析对象；最多 5 个，可在确认页新增/覆盖",
                )
                t1, t2 = st.columns([3, 1])
                if t1.button("↩ 使用该模板", key="reuse_tmpl_use", type="primary", width="stretch"):
                    _tmpl_plan = plans_store.load_template(tpick) or {}
                    _prefill_wizard_from_plan(_tmpl_plan)
                    st.session_state.plan_restored_notice = (
                        f"已使用模板「{_tmap[tpick].split('（')[0]}」，"
                        "请核对关键词、渠道、时间与高级参数（渠道上限/自定义维度/复核模式已一并恢复）。"
                    )
                    st.session_state.stage = 1
                    st.rerun()
                if t2.button("🗑 删除", key="reuse_tmpl_del", width="stretch"):
                    plans_store.delete_template(tpick)
                    st.rerun()
            st.caption(
                f"模板占用 {len(_templates)}/{plans_store.MAX_TEMPLATES}；"
                "恢复后请在后续步骤核对关键词、渠道与时间段。"
            )
        st.divider()
    mode = st.radio(
        "分析方式",
        ["按品牌分析（推荐）", "手动关键词（不分类）"],
        horizontal=True,
        key="wizard_mode",
    )
    st.session_state.mode = mode

    if mode.startswith("按品牌"):
        subject = st.text_input(
            "品牌 / 产品 / 事件名称",
            value=st.session_state.get("subject", ""),
            placeholder="例如：华润万家、恋与深空、iPhone",
        )
        st.session_state.subject = subject.strip()
        st.markdown("**选择品牌模块（可多选）**——模块决定分析维度组合：")
        mods = st.multiselect(
            "模块（可多选，例如零售商 = 有形实物 + 服务内容）",
            options=["content", "physical", "service"],
            format_func=lambda m: f"{module_name(m)}（{MODULE_DESC[m]}）",
        )
        st.session_state.selected_modules = mods
        schema = None
        if mods:
            try:
                schema = compose_schema(mods)
            except ValueError as exc:
                st.warning(str(exc))
        if schema:
            st.session_state.schema = schema
            names = {d.id: f"{d.name} — {d.description}" for d in schema.dimensions}
            selected = st.multiselect(
                "选择要分析的维度（默认全选；组合最多 10 个，报告按维度统计）",
                options=[d.id for d in schema.dimensions],
                format_func=lambda k: names[k],
                default=[d.id for d in schema.dimensions],
            )
            st.session_state.selected_dims = selected
            st.session_state.domain_id = schema.domain_id
            if len(selected) > MAX_DIMENSIONS:
                st.error(
                    f"已选 {len(selected)} 个维度，超过上限 {MAX_DIMENSIONS}，"
                    "请取消部分维度后再继续"
                )
        else:
            st.session_state.schema = None
            st.session_state.selected_dims = []
            st.session_state.domain_id = ""
            st.info("未选择模块，将按整体情感分析（不区分维度）")
        custom = st.text_area(
            "添加自定义维度（可选，参与分析统计）",
            value=st.session_state.get("custom_dim", ""),
            placeholder="例如：\n联名活动：联名,IP,周边\n物流体验：发货,快递,物流",
            height=100,
            help="格式：维度名：关键词1,关键词2,…（每行一个维度，最多 4 个；"
            "名称 ≤8 字、关键词 2~5 个）。自定义维度由 LLM 判定并参与维度统计，"
            "本任务将强制全量 LLM（费用/耗时上升）；未配置 API Key 时降级为"
            "词典兜底，分析仅供参考。",
        )
        custom_dims, custom_errors = parse_custom_dimensions(custom or "")
        st.session_state.custom_dim = custom.strip()
        st.session_state.custom_dimensions = custom_dims
        st.session_state.custom_dim_errors = custom_errors
        if custom_errors:
            st.error("自定义维度校验未通过：\n" + "\n".join(f"- {e}" for e in custom_errors))
        if custom_dims:
            st.info(
                "自定义维度将参与维度统计："
                + "、".join(f"{d.name}（{len(d.keywords)} 关键词）" for d in custom_dims)
                + "。本任务将强制全量 LLM。"
            )
    else:
        st.caption("手动关键词模式：不进行模块/维度分类，在下一步直接输入关键词。")
        st.session_state.subject = "手动关键词"
        st.session_state.domain_id = ""
        st.session_state.selected_dims = []
        st.session_state.schema = None
        st.session_state.selected_modules = []

    col1, _ = st.columns([1, 3])
    if col1.button("下一步 →", type="primary", width="stretch", key="next_0"):
        if mode.startswith("按品牌") and not st.session_state.get("subject"):
            st.error("请输入品牌/产品名称")
        elif (
            mode.startswith("按品牌")
            and st.session_state.get("selected_modules")
            and len(st.session_state.get("selected_dims", [])) > MAX_DIMENSIONS
        ):
            st.error(f"维度超过上限 {MAX_DIMENSIONS}，请精简后再继续")
        elif (
            mode.startswith("按品牌")
            and st.session_state.get("selected_modules")
            and not st.session_state.get("selected_dims")
        ):
            st.error("请至少选择一个分析维度")
        elif st.session_state.get("custom_dim_errors"):
            st.error("自定义维度校验未通过，请修正后再继续")
        else:
            # 物化组合 schema（只保留用户勾选的维度），worker 按 domain_id 加载
            schema = st.session_state.get("schema")
            if schema and st.session_state.get("domain_id"):
                try:
                    save_cached_schema(
                        filter_schema_dims(
                            schema, st.session_state.get("selected_dims", [])
                        )
                    )
                except Exception:
                    pass  # 物化失败不阻塞向导，worker 将按领域缺失降级提示
            next_stage()
            st.rerun()

elif stage == 1:
    st.subheader("② 确认关键词")
    _restored_notice = st.session_state.pop("plan_restored_notice", None)
    if _restored_notice:
        st.success(_restored_notice)
    mode = st.session_state.get("mode", "")
    subject = st.session_state.get("subject", "")
    _saved_kws = st.session_state.get("keywords") or []
    _kw_default = (
        "\n".join(_saved_kws)
        if _saved_kws
        else (subject if mode.startswith("按品牌") else "")
    )
    editable = st.text_area(
        "关键词（每行一个，可编辑）",
        value=_kw_default,
        key="keywords_input",
        height=160,
        help="关键词 = 实际搜索词；数量越多采集越广，但风控风险与费用也越高",
    )
    st.session_state.keywords = [k.strip() for k in editable.splitlines() if k.strip()]
    st.caption(
        f"当前将采集 **{len(st.session_state.get('keywords', []))}** 个关键词。"
        "建议从 1~10 个关键词起步；WebSearch 每日关键词总量上限 24，"
        "超限提交将被拦截。"
    )
    st.caption(
        "💡 关键词优化（自动加后缀/按策略展开）的开关与进阶策略配置，"
        "在下一步「渠道/时间」页设置；实际查询串可在报告「实际查询串（按渠道）」表核对。"
    )

    col1, col2 = st.columns(2)
    if col1.button("← 上一步", width="stretch", key="prev_1"):
        prev_stage()
        st.rerun()
    if col2.button("下一步 →", type="primary", width="stretch", key="next_1"):
        if not st.session_state.get("keywords"):
            st.error("关键词列表不能为空")
        else:
            next_stage()
            st.rerun()

elif stage == 2:
    st.subheader("③ 选择采集渠道与时间段")
    infos = list_channel_infos()
    default_channels = ["demo"]
    selected = st.multiselect(
        "采集渠道（建议先使用演示数据体验全流程）",
        options=[i["id"] for i in infos],
        default=default_channels,
        key="channel_ids",
        format_func=lambda cid: {
            i["id"]: f"{i['name']} — {i['applicability']}"
            for i in infos
        }[cid],
    )

    # 渠道提示：已选渠道的风控/前置条件/采集规则统一展示（避免警告散落各处）
    risk_texts = []
    info_texts = []
    if "weibo" in selected:
        risk_texts.append(
            "微博：风控严格，高频请求可能导致账号被临时限制甚至封禁；"
            "默认上限 10，请避免短时间重复运行。"
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
            "时间段", value=(default_start, dt.date.today()),
            key="date_range", help="采集该时间段内的内容",
        )
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
        # 5（2026-08-19）：渠道诊断紧随渠道多选之后，选完即可体检
        info_map = {i["id"]: i["name"] for i in infos}
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
                        "暂停/冷却/配额不足的渠道请到侧边栏「高级选项 → 渠道风控安全」"
                        "处理（解除冷却 / 恢复 / 调整每日上限）。"
                    )
        st.divider()
        c1, c2 = st.columns([1, 3])
        with c1:
            comments_enabled = st.toggle(
                "抓取评论", value=True, key="comments_enabled",
                help="关闭后不抓取评论，只分析帖子正文。"
                "评论抓取仅对 B站/微博/小红书渠道生效",
            )
        with c2:
            comments_per_post = st.slider(
                "每帖评论上限", 0, 10, 10, key="comments_per_post",
                help="每条帖子最多抓取多少条评论（评论越多采集越慢）",
            )
        # Streamlit 会在控件卸载时清理其 widget key，镜像到普通键供确认页/提交读取
        st.session_state.comments_enabled_opt = comments_enabled
        st.session_state.comments_per_post_opt = comments_per_post
        st.divider()
        st.markdown("**每关键词采集条数上限（按渠道）**")
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

        # ── 关键词优化模块（2026-08-18）──
        # 仅展示已实际生效的渠道：微博（2 品牌达标）。B站经 A4 修复后单关键词
        # 有效率 100% 无需扩词、小红书暂无低效词数据——都不呈现开关，避免误导。
        opt_channels = ["weibo"] if "weibo" in selected else []
        has_ws = any(cid.startswith("websearch") for cid in selected)
        if has_ws or opt_channels:
            with st.container(border=True):
                st.markdown("**⚙️ 关键词优化**")
                st.caption(
                    "开启后系统会按策略自动加后缀/展开查询，降低纯品牌词命中官网/壳页的"
                    "丢弃率。"
                )
                if has_ws:
                    # WebSearch 关键词优化总开关（2026-08-18：后缀/同义词/策略词）
                    st.session_state.websearch_eval_suffix = st.toggle(
                        "WebSearch 关键词优化（自动加后缀/同义词/策略词）",
                        value=st.session_state.get("websearch_eval_suffix", True),
                        key="websearch_eval_suffix_toggle",
                        help="开启后按关键词策略自动加『评价』等后缀、同义词与策略查询"
                        "（如『恋与深空』→『恋与深空 评价』『深空 评价』），更容易搜到"
                        "真实用户讨论；关闭后完全按确认关键词原词搜索",
                    )
                    st.caption(
                        "说明：开启后实际查询串可能与确认关键词不同（如『恋与深空』→"
                        "『恋与深空 评价』『深空 评价』）；实际查询串可在报告"
                        "「实际查询串（按渠道）」表核对。"
                    )
                if opt_channels:
                    for cid in opt_channels:
                        on = st.toggle(
                            f"{info_map.get(cid, cid)} 关键词优化（按策略展开查询）",
                            value=bool(st.session_state.get(f"kwopt_{cid}", False)),
                            key=f"kwopt_{cid}_toggle",
                            help="开启后按该渠道策略把品牌词展开为查询组合（默认关）；"
                            "展开数量计入配额估算。",
                        )
                        st.session_state[f"kwopt_{cid}"] = on
                    st.caption(
                        "策略按渠道配置：开启后按该渠道已确认的策略展开查询（进阶入口见下方）。"
                    )
                st.markdown("**🔑 关键词优化进阶**")
                if _eval_running():
                    st.link_button(
                        "打开评测中心「关键词策略配置」",
                        f"{EVAL_URL}/?tab=strategy",
                        type="secondary",
                    )
                else:
                    if st.button("启动评测中心（关键词策略配置）", key="kw_strategy_start"):
                        _start_eval_dashboard()
                        st.info(
                            f"正在后台启动评测中心（{EVAL_URL}/?tab=strategy），"
                            "首次启动需几秒；启动后请点击上方链接进入策略配置页。"
                        )
                st.caption(
                    "怎么用：在评测中心「关键词效果」页扫描历史任务 → 勾选候选词写入关键词策略 → "
                    "回到本页保持上方各渠道「关键词优化」开启，系统会按策略自动加词，降低纯品牌词"
                    "命中官网/壳页的丢弃率。"
                )

        # WebSearch 采集过滤（非关键词优化，独立分组避免模块名不符）
        if has_ws:
            st.markdown("**WebSearch 采集设置**")
            st.caption("官方域名不会提供有效的社交媒体情感信息，命中这些域名的搜索结果将被排除。")
            st.session_state.official_domains = st.text_input(
                "排除的官方域名（逗号分隔，可选）",
                placeholder="例如：dji.com,crv.com.cn",
                help="官网不会提供有效的社交媒体情感信息，命中这些域名的搜索结果将被排除",
                value=st.session_state.get("official_domains", ""),
            )

        # 2（2026-08-19）：词云排除词 / 广告与人工复核放到采集设置之后
        st.divider()
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
        # 广告/官方与人工复核（方案 A：单三选控件取代两个开关，2026-08-16）
        _review_on = bool(st.session_state.get("review_enabled_opt", False))
        _exclude_on = bool(st.session_state.get("exclude_ad_opt", False))
        _mode_default = 3 if _review_on else (2 if _exclude_on else 1)
        _mode = st.radio(
            "广告/官方与人工复核",
            AD_REVIEW_MODES,
            index=_mode_default - 1,
            key="ad_review_mode",
            help=(
                "广告/官方默认计入（消费者可见的市场信号）；规则剔除仅为建议；"
                "人工复核时标记的广告/官方将从情感统计剔除（不标记的照常计入）。"
            ),
        )
        st.session_state.review_enabled_opt = _mode.startswith("人工复核")
        st.session_state.exclude_ad_opt = _mode != AD_REVIEW_MODES[0]
        st.caption(
            "「人工复核」= 采集并清洗后暂停，进入审核页把相关性剔除与广告/官方标记一次做完，"
            "再继续情感分析；标记的广告/官方仅从情感统计剔除（采集量/关键词效果保留）。"
        )

    col1, col2 = st.columns(2)
    if col1.button("← 上一步", width="stretch", key="prev_2"):
        prev_stage()
        st.rerun()
    if col2.button("下一步 →", type="primary", width="stretch", key="next_2"):
        if not selected:
            st.error("请至少选择一个渠道")
        else:
            next_stage()
            st.rerun()

elif stage == 3:
    st.subheader("④ 确认采集计划并启动")
    custom_dims = st.session_state.get("custom_dimensions", [])
    if custom_dims:
        # 2.8：含自定义维度 → 有 Key 强制全量 LLM；无 Key 降级词典并显著提示
        if api_key:
            llm_enabled = True
        else:
            st.warning(
                "⚠️ 已添加自定义维度但未配置 API Key：将使用词典兜底模式，"
                "自定义维度判定可能大量缺失，分析结果仅供参考。"
            )
    relevance_check_enabled = llm_enabled  # 与侧边栏口径一致（随 LLM 自动开启）
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

    # 其他渠道关键词展开（Phase 0，2026-08-18）：任务级开关 → 渠道查询串
    # 仅微博生效（B站/小红书暂无策略内容，不展示开关）
    channel_queries = {}
    for cid in ("weibo",):
        if cid in channel_ids and st.session_state.get(f"kwopt_{cid}"):
            qs = expand_channel_queries(keywords, cid)
            channel_queries[cid] = qs
            channel_params.setdefault(cid, {})["queries"] = qs
    # WebSearch 查询串展开（2026-08-18 总开关语义）：与采集共用
    # expand_websearch_keywords，确认页估算与真实采集一致
    ws_channels = [c for c in channel_ids if c.startswith("websearch")]
    ws_queries: list[str] = []
    if ws_channels:
        ws_queries = expand_websearch_keywords(
            keywords,
            st.session_state.get("subject", ""),
            enabled=bool(st.session_state.get("websearch_eval_suffix", True)),
        )
        for cid in ws_channels:
            channel_queries[cid] = ws_queries

    st.markdown("### 计划摘要")
    est_items, est_comments, est_min = estimate_collection(
        keywords, channel_ids, st.session_state.get("channel_limits", {}),
        st.session_state.get("comments_per_post_opt", 10),
        st.session_state.get("comments_enabled_opt", True),
        channel_queries=channel_queries,
    )
    cost_est = estimate_cost(
        est_items + est_comments, narrative_enabled, relevance_check_enabled
    )
    # WebSearch 关键词总量（展开后查询串数 × WebSearch 渠道数）：
    # 必须在 summary_rows 构建前计算，否则确认页引用未定义变量抛 NameError
    # （回归见 commit 22454ed：ws_total 赋值曾误放在 st.table 之后）。
    ws_total = len(ws_queries) * len(ws_channels)
    summary_rows = [
        ("分析对象", st.session_state.get("subject", "")),
        (
            "模块",
            "、".join(
                module_name(m)
                for m in st.session_state.get("selected_modules", [])
            )
            or "不分类",
        ),
        (
            "分析维度",
            (
                f"{len(st.session_state.get('selected_dims', []))} 个"
                if st.session_state.get("selected_dims")
                else "整体情感（不区分维度）"
            ),
        ),
        *(
            [(
                "自定义维度",
                "、".join(f"{d.name}（{len(d.keywords)} 关键词）" for d in custom_dims),
            )]
            if custom_dims
            else []
        ),
        ("关键词数", str(len(keywords))),
        ("渠道", "、".join(channel_ids)),
        *(
            [(
                "渠道查询数（策略展开）",
                "、".join(f"{cid} {len(qs)}" for cid, qs in channel_queries.items()),
            )]
            if channel_queries
            else []
        ),
        ("官方域名排除", official_domains or "未配置"),
        ("WebSearch 关键词优化", "开（按策略加后缀/同义词/策略词）" if st.session_state.get("websearch_eval_suffix", True) else "关（按原词搜索）"),
        *(
            [(
                "WebSearch 关键词总量（今日上限 24）",
                f"{ws_total} 次查询"
                + (
                    f"（{len(ws_channels)} 个 WebSearch 渠道 × {len(ws_queries)} 个查询串"
                    + ("，含策略展开" if len(ws_queries) > len(keywords) else "")
                    + "）"
                )
                + ("，超限提交将被拦截" if ws_total > 24 else ""),
            )]
            if ws_channels
            else []
        ),
        ("LLM 相关性复核", "开（随 LLM 自动开启）" if relevance_check_enabled else "关"),
        ("广告/官方与人工复核", AD_REVIEW_SHORT.get(
            st.session_state.get("ad_review_mode", ""),
            AD_REVIEW_SHORT[AD_REVIEW_MODES[0]])),
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

    # 确认页物化计划（供提交与模板保存共用；不产生副作用）
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
        comments_per_post=st.session_state.get("comments_per_post_opt", 10),
        exclude_words=st.session_state.get("exclude_words_opt", []),
        llm_enabled=llm_enabled,
        llm_base_url=base_url,
        llm_model=model_name,
        custom_dimensions=st.session_state.get("custom_dimensions", []),
        narrative_enabled=narrative_enabled,
        relevance_check_enabled=relevance_check_enabled,
        channel_params=channel_params,
        review_enabled=st.session_state.get("review_enabled_opt", False),
        exclude_ad_enabled=st.session_state.get("exclude_ad_opt", False),
    )

    # UX 5.2 预期管理：同类任务历史耗时均值（供"通常多久跑完"预期）
    _dur = jobs.history_duration_stats(plan)
    if _dur.get("avg_minutes") is not None:
        st.caption(
            f"⏱ 同类任务（LLM {'开' if plan.llm_enabled else '关'} · "
            f"{'抓评论' if plan.comments_enabled else '不抓评论'}）历史执行约 "
            f"**{_dur['avg_minutes']:.0f} 分钟**（中位 {_dur['median_minutes']:.0f} 分钟，"
            f"基于最近 {_dur['n']} 次{'同类' if _dur['bucket'] == '同类' else '全部'}任务；"
            "不含排队时间，实际受网络与平台频率限制影响）"
        )
    else:
        st.caption(
            "⏱ 暂无同类任务历史，耗时无法预估；建议先用演示数据体验全流程。"
        )

    # UX 5.1：把当前配置存为命名模板（≤5，同名覆盖）
    st.divider()
    tp1, tp2 = st.columns([3, 1])
    _tmpl_name = tp1.text_input(
        "把当前配置存为命名模板（≤5 个，下次在①直接恢复）",
        placeholder="例如：OPPO 周度复盘",
        key="tmpl_save_name",
    )
    if tp2.button("💾 保存模板", key="tmpl_save_btn", width="stretch"):
        _res = plans_store.save_template(_tmpl_name, plan)
        if _res["ok"]:
            st.success(
                f"已{'更新' if _res['action'] == 'updated' else '新增'}模板「{_tmpl_name.strip()}」"
                f"（{len(plans_store.list_templates())}/{plans_store.MAX_TEMPLATES}）"
            )
        else:
            st.error(_res["error"])

    col1, col2 = st.columns(2)
    if col1.button("← 上一步", width="stretch", key="prev_3"):
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
                st.session_state.stage = 2
                st.rerun()
            st.stop()
        # 任务提交后台队列：关页面/刷新不中断，由常驻 worker 执行
        task_id = jobs.submit_task(plan)
        # UX 5.1：提交成功后自动记录"最近一次计划"（脱敏落库，免重填）
        plans_store.save_last_plan(plan)
        st.session_state.task_id = task_id
        st.session_state.stage = 4
        st.rerun()

elif stage == 4:
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

elif stage == 5:
    bundle = st.session_state.get("bundle")
    files = st.session_state.get("output_files", {})
    if bundle:
        register_custom_dim_names(bundle.plan)  # 2.8：结果页渲染前注册自定义维度名
    if not bundle:
        st.warning("没有可展示的结果，请重新开始")
        if st.button("重新开始"):
            reset_wizard()
            st.rerun()
        st.stop()

    s = bundle.summary
    dist = s["sentiment_distribution"]
    st.subheader("⑥ 分析结果")
    # P1-1：视图切换（结论视图 = 结论 + 4 指标 + 整体情感主图；全部图表 = 展开全部）
    view = st.segmented_control(
        "视图",
        ["只看结论", "全部图表"],
        default="只看结论",
        key="result_view",
        label_visibility="collapsed",
        help="结论视图 = 一句话结论 + 4 指标 + 整体情感主图；需要全部图表时切换",
    )
    show_all = view == "全部图表"
    if s["total_items"] < 10:
        st.caption("⚠️ 小样本（n<10）：以下指标与结论仅供参考。")

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
                    st.session_state.stage = 3
                    st.rerun()
                else:
                    st.error("请先粘贴新的 Cookie")

    # —— ① 一句话结论胶囊（含可信度胶囊：数据量 + 时间窗 + 待复核） ——
    cv = s.get("consumer_voice") or {}
    _lead = (bundle.report_text or "").strip()
    _lead = _lead.splitlines()[0] if _lead else (bundle.conclusion or "")
    _dr = (
        f"{bundle.plan.date_start} ~ {bundle.plan.date_end}"
        if bundle.plan.date_start or bundle.plan.date_end
        else "不限时间"
    )
    _llm_n = sum(1 for it in bundle.coded_items if it.method == "llm")
    _llm_ratio = round(_llm_n / max(s["total_items"], 1) * 100)
    _nr_n = sum(
        1 for it in bundle.coded_items
        if it.need_review and not it.reviewed_by
    )
    _ti = s["total_items"]
    _trust_txt = (
        "较高（样本充足）" if _ti >= 100
        else "中等（样本较少）" if _ti >= 30
        else "较低（小样本）"
    )
    st.markdown(
        f"""
<div class="conclusion-card">
  <div class="kicker">分析结论</div>
  <h2>{html.escape(_lead or "暂无结论")}</h2>
  <div class="trust">
    <span class="t-item">{len(bundle.channel_results)} 渠道 · {_dr}</span>
    <span class="t-item">帖子 {s['total_posts']} · 编码文本 {s['total_items']} 条</span>
    <span class="t-item">LLM 精分析 {_llm_ratio}%</span>
    <span class="t-item">可信度：{_trust_txt}</span>
    {f'<span class="t-item t-warn">{_nr_n} 条样本待人工复核</span>' if _nr_n else ''}
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

    # —— ② 4 张指标卡（数字 tabular-nums；整体倾向用情感标签三件套） ——
    _ov_key = (
        "pos" if s["overall_sentiment"] == "正面"
        else ("neg" if s["overall_sentiment"] == "负面" else "neu")
    )
    _ov_sym = {"pos": "✓", "neg": "✗", "neu": "～"}[_ov_key]
    _worst = "—"
    _dims = s.get("dimensions") or {}
    if _dims:
        _worst_dim = max(
            _dims, key=lambda d: (_dims[d].get("negative_rate") or 0)
        )
        if _dims[_worst_dim].get("count", 0) >= 3:
            _worst = dimension_cn(_worst_dim)
    _cv_ratio = cv.get("ratio")
    st.markdown(
        f"""
<div class="stat-cards">
  <div class="stat-card">
    <div class="k">整体倾向</div>
    <div class="v"><span class="tag tag-{_ov_key}" style="font-size:18px;padding:4px 12px"><span class="sym">{_ov_sym}</span>{html.escape(s['overall_sentiment'])}</span></div>
    <div class="d">正面 {dist['positive']['ratio'] * 100:.1f}% · 中性 {dist['neutral']['ratio'] * 100:.1f}% · 负面 {dist['negative']['ratio'] * 100:.1f}%</div>
  </div>
  <div class="stat-card">
    <div class="k">平均情感分</div>
    <div class="v">{s['avg_score']:.2f}<span style="font-size:14px;font-weight:600;color:var(--text-muted)"> / 1.0</span></div>
    <div class="d">区间 −1.0 ~ +1.0</div>
  </div>
  <div class="stat-card">
    <div class="k">负面占比</div>
    <div class="v neg">{dist['negative']['ratio'] * 100:.1f}<span style="font-size:14px;font-weight:600;color:var(--text-muted)">%</span></div>
    <div class="d">{f'集中在「{_worst}」维度' if _worst != '—' else '—'}</div>
  </div>
  <div class="stat-card">
    <div class="k">消费者声音</div>
    <div class="v">{'—' if _cv_ratio is None else f'{_cv_ratio * 100:.1f}'}<span style="font-size:14px;font-weight:600;color:var(--text-muted)">%</span></div>
    <div class="d">{html.escape(str(cv.get('tier') or '—'))}</div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

    # 采集说明（2026-08-18 采集透明度）：实际保留 < 配置上限时解释缺口
    _notes = _collection_notes(bundle)
    if _notes:
        with st.expander(f"采集说明：{len(_notes)} 个渠道未采满（实际保留 < 设置上限）"):
            for _n in _notes:
                st.markdown(
                    f"**{_n['channel']}**：目标 {_n['requested']} 条，"
                    f"实际保留 {_n['kept']} 条。"
                )
                st.caption("；".join(_n["reasons"]) + "。建议：" + "、".join(_n["tips"]))

    # 2.11 方案 A：需复核样本强提示（可跳过；复核后刷新报告）
    nr_items = [it for it in bundle.coded_items if it.need_review]
    if nr_items:
        st.session_state[f"nr_had_{task_id}"] = True
    nr_unreviewed = [it for it in nr_items if not it.reviewed_by]
    if nr_unreviewed and not st.session_state.get(f"nr_skip_{task_id}"):
        st.warning(
            f"⚠️ 有 **{len(nr_unreviewed)}** 条需复核样本未确认："
            "当前指标与结论基于模型判定。复核后报告统计/图表/Excel/HTML 会自动更新。"
        )
        c_go, c_skip = st.columns(2)
        if c_go.button("✅ 先去复核", key=f"nr_go_{task_id}"):
            st.session_state[f"nr_focus_{task_id}"] = True
            st.rerun()
        if c_skip.button("先看报告", key=f"nr_skip_btn_{task_id}"):
            st.session_state[f"nr_skip_{task_id}"] = True
            st.rerun()

    if bundle.insight_mode == "lexicon":
        st.warning(
            "本次为词典模式，维度情感与结论仅供参考；"
            "开启 LLM 精分析可获得可归因的结论与行动建议。"
        )
    elif bundle.insight_mode == "template_fallback":
        st.warning(
            "本次深度结论由规则模板生成（LLM 兜底），仅供参考；"
            "建议检查 LLM 配置后重新生成。"
        )
    elif bundle.insight_mode == "no_data":
        st.warning("本次未采集到有效文本，未生成情感结论。")
    elif bundle.insight_mode == "review_refresh":
        st.warning(
            "报告已按人工复核结果用规则重新生成，结论仅供参考；"
            "如需 LLM 深度结论，请点击「重新生成 LLM 深度结论」。"
        )

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

    # 2.11：需复核样本（词典直判/低置信/反讽/黑话/问句/短句等难例）人工确认闭环
    nr_items = [it for it in bundle.coded_items if it.need_review]
    if nr_items:
        task_id = st.session_state.get("task_id")
        focus = bool(st.session_state.get(f"nr_focus_{task_id}"))
        with st.expander(
            f"🔍 需复核样本（{len(nr_items)} 条：词典直判/低置信/反讽/黑话/问句/短句等难例）",
            expanded=len(nr_items) <= 20 or focus,
        ):
            st.caption(
                "人工确认后回填情感并标记已复核；**全部确认后报告统计/图表/Excel/HTML "
                "会按复核结果自动重算**（确认最后一条即自动刷新，无需手动操作）。"
            )
            unreviewed = [it for it in nr_items if not it.reviewed_by]
            shown = unreviewed[:50]
            if len(unreviewed) > 50:
                st.caption(f"仅展示前 50 条（共 {len(unreviewed)} 条未确认）。")
            for i, it in enumerate(shown):
                c1, c2, c3, c4, c5 = st.columns([0.3, 3, 1.3, 1.4, 1.2])
                c1.caption(str(i + 1))
                c2.write(f"{it.text}")
                c3.write(
                    f"{it.sentiment.value}"
                    f"（conf {display_confidence(it.confidence, it.method):.2f}）"
                )
                c4.write(it.need_review_reason)
                if it.reviewed_by:
                    c5.caption(f"✅ {it.reviewed_by}")
                else:
                    b1, b2, b3 = c5.columns(3)
                    if b1.button("正", key=f"nr_pos_{task_id}_{i}"):
                        _apply_review_item(task_id, it, "positive")
                    if b2.button("负", key=f"nr_neg_{task_id}_{i}"):
                        _apply_review_item(task_id, it, "negative")
                    if b3.button("中", key=f"nr_neu_{task_id}_{i}"):
                        _apply_review_item(task_id, it, "neutral")

    # 2.11 方案 A：复核后重新生成 LLM 深度结论（基于当前复核结果）
    if st.session_state.get(f"nr_had_{task_id}"):
        if st.button(
            "✨ 重新生成 LLM 深度结论（基于当前复核结果）",
            key=f"nr_regen_{task_id}",
        ):
            if not api_key:
                st.warning("未配置 API Key：将生成词典规则结论（不调用 LLM）")
            try:
                res = rebuild_report_after_review(
                    task_id, st.session_state.bundle, regenerate_insights=True)
            except Exception as exc:
                # 7：LLM 调用失败不阻断——降级规则路径重算并明示
                st.warning(f"LLM 深度结论生成失败（{exc}），已按词典规则重新生成报告。")
                res = rebuild_report_after_review(
                    task_id, st.session_state.bundle)
            if res:
                st.session_state.bundle, st.session_state.output_files = res
                st.success(
                    "报告已按当前复核结果重新生成（统计/图表/结论已重算，"
                    "Excel/HTML 已同步更新）"
                )
                st.rerun()
            else:
                st.error("重新生成失败：任务结果文件不可写")
        st.caption(
            "重新生成会按复核后的统计重新生成图表解析与深度结论"
            "（LLM 模式需 API Key，按量计费）。"
        )

    if bundle.findings:
        st.subheader(findings_section_title(bundle.insight_mode))
        for f in bundle.findings:
            with st.expander(
                f"{display_finding_id(f.get('id', ''))} {f.get('claim', '')}",
                expanded=False,
            ):
                for rid in (f.get("evidence_refs") or []):
                    c = next((e for e in bundle.evidence if e.get("id") == rid), None)
                    if not c:
                        continue
                    if c.get("kind") == "stat":
                        st.markdown(
                            f"📊 {c.get('text', '')}\n\n来源：报告统计 · n={c.get('n')}"
                        )
                        continue
                    judge = "LLM 判定" if c.get("judge") == "llm" else "词典判定 · 仅供参考"
                    label = (
                        f"来源：{c.get('platform', '')} · {c.get('date') or '日期未知'} · "
                        f"{judge}"
                    )
                    if c.get("need_review"):
                        label += " · 待复核"
                    st.markdown(f"「{c.get('text', '')}」\n\n{label}")
                    # UX 5.6 反馈闭环：证据旁"这条判错了"一键反馈 → 评测中心候选池
                    _fkey = f"fb_{task_id}_{f.get('id')}_{rid}"
                    if not st.session_state.get(_fkey, False):
                        if st.button("这条判错了？", key=f"{_fkey}_btn"):
                            st.session_state[_fkey] = True
                            st.rerun()
                    else:
                        _sent_cn = {
                            "positive": "正面 ✓",
                            "neutral": "中性 ～",
                            "negative": "负面 ✗",
                        }
                        _val = st.radio(
                            "应判定为",
                            ["positive", "neutral", "negative"],
                            format_func=lambda v: _sent_cn[v],
                            horizontal=True,
                            key=f"{_fkey}_val",
                        )
                        _reason = st.text_input("原因（可选，帮我们改进）", key=f"{_fkey}_reason")
                        _sub1, _sub2 = st.columns(2)
                        if _sub1.button("提交反馈", key=f"{_fkey}_submit"):
                            try:
                                feedback.add_feedback(
                                    task_id=task_id or "",
                                    text_id=c.get("text_id") or rid,
                                    text_snippet=c.get("text", ""),
                                    model_sentiment=c.get("sentiment") or "",
                                    user_sentiment=_val,
                                    reason=_reason or "",
                                )
                            except ValueError as _exc:
                                st.error(str(_exc))
                            else:
                                # 8：反馈真正生效——回填该文本判定并重算报告，
                                # 不再是"只记录不影响报告"的无效入口
                                _fb_text_id = c.get("text_id") or rid
                                if task_id and apply_need_review_feedback(
                                    task_id,
                                    _fb_text_id,
                                    _val,
                                    text=c.get("text", ""),
                                    reviewed_by="用户反馈",
                                ):
                                    _bundle2 = _reload_bundle(task_id)
                                    if _bundle2 is not None:
                                        st.session_state.bundle = _bundle2
                                    _r3 = rebuild_report_after_review(
                                        task_id, st.session_state.bundle
                                    )
                                    if _r3:
                                        st.session_state.bundle, (
                                            st.session_state.output_files
                                        ) = _r3
                                    _fb_note = (
                                        "已按你的判定更新本条并刷新报告"
                                        "（统计/图表已重算，Excel/HTML 已同步）；"
                                        "如原为 LLM 深度结论，可点上方「重新生成 LLM 深度结论」。"
                                    )
                                else:
                                    _fb_note = (
                                        "反馈已记录（用于评测改进）；"
                                        "本条不在任务编码中，报告未改动。"
                                    )
                                st.session_state.pop(_fkey, None)
                                st.success(f"已记录，感谢反馈！{_fb_note}")
                                st.rerun()
                        if _sub2.button("取消", key=f"{_fkey}_cancel"):
                            st.session_state.pop(_fkey, None)
                            st.rerun()
                if f.get("action"):
                    st.markdown(f"**建议：**{display_action(f.get('action'))}")

    # —— ③ 整体情感主图（结论视图唯一默认展开） ——
    st.markdown("### 整体情感占比")
    st.plotly_chart(overall_fig(s), width="stretch")
    st.markdown(f"**解析：**{bundle.chart_insights.get('overall', '')}")

    # —— 其余图：全部视图展开；结论视图折叠钻取 ——
    if show_all:
        col1, col2 = st.columns(2)
        with col2:
            st.plotly_chart(platform_fig(s), width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('platform', '')}")
        st.plotly_chart(trend_fig(s), width="stretch")
        st.markdown(f"**解析：**{bundle.chart_insights.get('trend', '')}")
        st.plotly_chart(intensity_fig(s), width="stretch")
        st.markdown(f"**解析：**{bundle.chart_insights.get('intensity', '')}")
        _render_dim_charts(s, bundle)
    else:
        with st.expander("📊 各平台情感分布（细节）", expanded=False):
            st.plotly_chart(platform_fig(s), width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('platform', '')}")
        with st.expander("📈 时间趋势（细节）", expanded=False):
            st.plotly_chart(trend_fig(s), width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('trend', '')}")
        with st.expander("🔥 情绪强度分布（细节）", expanded=False):
            st.plotly_chart(intensity_fig(s), width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('intensity', '')}")
        with st.expander("🧩 维度分析（细节）", expanded=False):
            _render_dim_charts(s, bundle)
    # 非核心钻取区：结论视图折叠；全部视图展开（词云保持按需生成）
    pd_fig = platform_dim_fig(s)
    if pd_fig:
        with st.expander("📊 平台 × 维度负面率（细节）", expanded=show_all):
            st.plotly_chart(pd_fig, width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('platform_dim', '')}")
    with st.expander("🔤 高频情感词 Top20（细节）", expanded=show_all):
        st.plotly_chart(words_fig(s), width="stretch")
        st.markdown(f"**解析：**{bundle.chart_insights.get('words', '')}")
    with st.expander("☁️ 情感词云（细节，按需生成）", expanded=False):
        if not st.session_state.get(f"wc_gen_{task_id}"):
            st.caption("词云图片生成较慢（3 张约 2~5 秒），点击后生成。")
            if st.button("生成词云图片", key=f"wc_gen_btn_{task_id}"):
                st.session_state[f"wc_gen_{task_id}"] = True
                st.rerun()
        else:
            wc = {
                w: wordcloud_png_bytes(s, w)
                for w in ("positive", "negative", "worst_dim")
            }
            if any(wc.values()):
                for which, caption in (
                    ("positive", "正面讨论词云"),
                    ("negative", "负面讨论词云"),
                    ("worst_dim", "负面率最高维度词云"),
                ):
                    if wc[which]:
                        st.image(wc[which], caption=caption, width=700)
                st.markdown(f"**解析：**{bundle.chart_insights.get('wordcloud', '')}")
            else:
                st.caption("暂无词云数据（样本过少）。")
    co_fig = cooccurrence_fig(s)
    if co_fig:
        with st.expander("🕸 讨论话题共现网络（细节）", expanded=show_all):
            st.markdown(
                terms.md_label("共现网络", "cooccurrence"),
                unsafe_allow_html=True,
            )
            st.plotly_chart(co_fig, width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('cooccurrence', '')}")
            cluster_rows = topic_cluster_rows(s)
            if cluster_rows:
                st.markdown(
                    terms.md_label("话题簇榜单", "cluster"),
                    unsafe_allow_html=True,
                )
                st.table(
                    [
                        {
                            "簇名": r["name"],
                            "代表词": r["words"],
                            "涉及文本数": r["doc_count"],
                            "负面率": r["negative_rate"],
                        }
                        for r in cluster_rows
                    ]
                )
    elif cooccurrence_plan(s)["kind"] == "pairs":
        with st.expander("🕸 话题词对榜（细节）", expanded=show_all):
            st.caption(
                "讨论结构样本不足，已显示话题词对榜："
                f"{cooccurrence_plan(s)['reason']}。"
            )
            pair_rows = topic_pairs(s)
            if pair_rows:
                st.markdown(
                    terms.md_label("话题词对榜", "word_pairs"),
                    unsafe_allow_html=True,
                )
                st.markdown(
                    terms.md_label("PMI 列口径", "pmi"),
                    unsafe_allow_html=True,
                )
                st.table(
                    [
                        {
                            "词对": f"{r['source']} — {r['target']}",
                            "共现文本数": r["count"],
                            "PMI": round(r["pmi"], 2),
                        }
                        for r in pair_rows
                    ]
                )
    src_fig = sentiment_sources_fig(s)
    if src_fig:
        with st.expander("🗣 负面情绪来源话题榜（细节）", expanded=show_all):
            st.plotly_chart(src_fig, width="stretch")
    narr_stats = s.get("narrative_stats") or {}
    narr_total = narr_stats.get("total", 0)
    if narr_total >= 10:
        with st.expander("🧩 叙事框架与归因（LLM 高级分析）", expanded=show_all):
            st.caption("仅对 LLM 精分析过的文本执行（成本控制设计）；归因/框架为固定词表。")
            for line in narrative_insight_text(s):
                st.markdown(line)
            a1 = narrative_actor_fig(s)
            if a1:
                st.plotly_chart(a1, width="stretch")
            a2 = narrative_frame_actor_heatmap(s)
            if a2:
                st.plotly_chart(a2, width="stretch")
    elif narr_total > 0:
        with st.expander("🧩 叙事框架与归因（LLM 高级分析）", expanded=False):
            st.caption(f"叙事/归因样本仅 {narr_total} 条，样本不足，未生成聚合图。")

    # 4.4 降噪：概览与（无 findings 时的）深度结论合并为"结论"一个区
    st.subheader("结论")
    st.markdown(bundle.report_text)
    if not bundle.findings and bundle.conclusion:
        st.divider()
        st.markdown(bundle.conclusion)

    st.subheader("下载报告")
    st.caption("⚠️ 导出物包含用户原文等个人信息，仅限内部使用，禁止二次传播（P1-6）。")
    d1, d2, d3, d4 = st.columns(4)
    with d1:
        st.download_button(
            "📥 原始数据 Excel",
            data=files.get("excel", b""),
            file_name=f"{bundle.plan.subject}_原始数据与编码.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
        st.caption("原始帖子/评论/编码明细，可编辑、可二次分析")
    with d2:
        st.download_button(
            "📥 HTML 交互报告",
            data=files.get("html", ""),
            file_name=f"{bundle.plan.subject}_分析报告.html",
            mime="text/html",
            width="stretch",
        )
        st.caption("带交互图表的分析报告，适合分享")
    with d3:
        if "word_bytes" in st.session_state:
            st.download_button(
                "📥 Word 报告（含图表）",
                data=st.session_state.word_bytes,
                file_name=f"{bundle.plan.subject}_分析报告.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                width="stretch",
            )
        else:
            if st.button("⏳ 生成 Word 报告（约 30~60 秒）", width="stretch"):
                with st.spinner("正在渲染图表图片并生成 Word 报告…"):
                    st.session_state.word_bytes = build_word(bundle).getvalue()
                st.rerun()
        st.caption("正式汇报用文档")
    with d4:
        st.download_button(
            "📥 结果 JSON",
            data=bundle.model_dump_json(indent=2),
            file_name=f"{bundle.plan.subject}_result.json",
            mime="application/json",
            width="stretch",
        )
        st.caption("机器可读结果，供二次分析/评测")

    if st.button("🔄 开始新的分析", width="stretch"):
        reset_wizard()
        st.rerun()


# 顶部分步提示（P1-2：SVG 图标 + 语义色，对照 prototype_app.html 步骤条）
st.divider()
steps = ["品牌和维度", "关键词", "渠道/时间", "确认运行", "后台执行", "结果"]
current = min(int(st.session_state.get("stage", 0)), 5)
_ws_parts = []
for i, (label, _icon) in enumerate(zip(steps, _WIZARD_STEP_ICONS)):
    _cls = "done" if i < current else ("cur" if i == current else "")
    _ws_parts.append(
        f'<span class="ws-step {_cls}">{_icon}<span>{html.escape(label)}</span></span>'
    )
    if i < len(steps) - 1:
        _link_cls = "done" if i < current else ""
        _ws_parts.append(f'<span class="ws-link {_link_cls}"></span>')
st.markdown(
    f'<div class="wizard-steps">{"".join(_ws_parts)}</div>',
    unsafe_allow_html=True,
)
