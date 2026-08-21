"""社交媒体情感分析器 — Streamlit 向导入口（模块化，2026-08-22）。"""

from __future__ import annotations

import streamlit as st

from app.core import lifecycle, usage_boundary
from app.core.secrets import ensure_legacy_key_migrated
from app.ui.results import render_results
from app.ui.sidebar import render_sidebar
from app.ui.tasks import render_running, render_task_center
from app.ui.theme import inject_global_css
from app.ui.wizard import _apply_restored_widgets, render_step_bar, render_wizard

st.set_page_config(page_title="社交媒体情感分析器", page_icon="📊", layout="wide")
# P0-2：全局视觉主题注入（令牌 CSS，只改视觉层）
inject_global_css()

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

if "stage" not in st.session_state:
    st.session_state.stage = 0


_apply_restored_widgets()
render_sidebar()

st.title("📊 社交媒体情感分析器")
st.caption("输入品牌/产品名或关键词 → 确认维度与采集计划 → 自动生成图表与分析报告")
render_task_center()

stage = st.session_state.get("stage", 0)
if stage == 4:
    render_running()
elif stage == 5:
    render_results()
else:
    render_wizard()

render_step_bar()
