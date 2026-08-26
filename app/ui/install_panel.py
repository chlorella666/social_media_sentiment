"""F-014（2026-08-26）：Node/opencli 一键安装面板（配置中心闭环）。

从 wizard.py ③渠道页迁移而来：状态分层（Node 未装 → 只给装 Node；
Node 已装 opencli 未装 → 给装 opencli；都就绪 → 不展示安装按钮），
安装成功 st.rerun() 刷新状态行；联动 F-013（health.install_opencli
已注入 Node 目录到子进程 PATH）。
"""

from __future__ import annotations

from app.channels import health
from app.core.config_status import node_status, opencli_status
import streamlit as st


def render_node_opencli_install() -> None:
    """侧边栏配置中心安装面板（窄栏：长输出折叠为可复制 code 块）。"""
    _node = node_status()
    _ocl = opencli_status()
    if _node["has_key"] and _ocl["has_key"]:
        return  # 状态行已绿，不展示安装按钮
    if not _node["has_key"]:
        st.caption("⚠️ 将安装系统级软件（Node.js 走 winget / npm），请确认本机允许。")
        st.caption(
            "Windows 10/11 内置一键安装；更老的 Windows（7/8）请到 "
            "nodejs.org 手动安装（勾选 Add to PATH）。"
        )
        if st.button(
            "安装 Node.js（约 1~2 分钟）", key="cfg_install_node_btn", width="stretch"
        ):
            with st.spinner("正在安装 Node.js…"):
                _res = health.install_node()
            if _res["ok"]:
                st.success(_res["message"])
                st.rerun()
            else:
                st.error(_res["message"])
                if _res["output"]:
                    with st.expander("安装输出（可复制）", expanded=False):
                        st.code(_res["output"], language="text")
        return
    st.caption("小红书采集依赖 opencli 命令行工具；自动安装无需手动敲命令。")
    st.caption("⚠️ 将安装系统级软件（opencli 走 npm），请确认本机允许。")
    if st.button(
        "一键安装 opencli（约 1 分钟）", key="cfg_install_opencli_btn", width="stretch"
    ):
        with st.spinner("正在安装 opencli…"):
            _res = health.install_opencli(
                use_mirror=bool(st.session_state.get("opencli_mirror", True)),
            )
        if _res["ok"]:
            st.success(_res["message"])
            st.rerun()
        else:
            st.error(_res["message"])
            if _res["output"]:
                with st.expander("安装输出（可复制）", expanded=False):
                    st.code(_res["output"], language="text")
