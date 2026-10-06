"""main.py 拆分模块（2026-08-22）：侧边栏（LLM 设置/高级选项/数据管理/开发者模式）。"""

from __future__ import annotations

from app.ui.constants import (API_KEY_GUIDE)
from app.ui.dev import (EVAL_URL, _dev_mode_enabled, _eval_running, _set_dev_mode, _start_eval_dashboard)
from app import (__version__, demo_report)
from app.channels.registry import list_channel_infos
from app.coding.llm_analyzer import create_analyzer
from app.core import (jobs, lifecycle, usage_boundary)
from app.core.config_status import llm_status, node_status, opencli_status, weibo_status
from app.ui.install_panel import render_node_opencli_install
from app.core.secrets import (clear_api_key, load_api_key, save_api_key)
import streamlit as st
import webbrowser

def _open_demo_report() -> None:
    """打开内置演示报告（成品示例）：直接进结果页，不产生真实任务/数据。"""
    with st.spinner("正在生成演示报告（几秒）…"):
        try:
            bundle = demo_report.build_demo_bundle()
            files = demo_report.prepare_demo_files(bundle)
        except Exception as exc:  # 演示入口失败不阻塞主流程
            st.error(f"演示报告生成失败：{exc}")
            st.stop()
    st.session_state.bundle = bundle
    st.session_state.output_files = files
    st.session_state.task_id = None
    st.session_state.demo_report = True
    st.session_state.stage = 5
    st.rerun()

def render_sidebar():
    with st.sidebar:
        # 4.1 首次引导：快速上手卡（首次会话显示、可关闭、不重复）
        if not st.session_state.get("quickstart_dismissed"):
            st.markdown("### 🚀 快速上手")
            st.markdown(
                "三步开始：\n"
                "1. **先看成品演示报告**（约 2 秒，无需配置）；\n"
                "2. 在向导中选择真实渠道开始分析；\n"
                "3. 开启 LLM 精分析，结论更可归因。"
            )
            qc1, qc2 = st.columns(2)
            if qc1.button(
                "✨ 看演示报告", type="primary", width="stretch", key="quickstart_demo_report"
            ):
                st.session_state["quickstart_dismissed"] = True
                _open_demo_report()
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
            help="默认词典模式（离线可跑，不发送数据）；开启后文本将发送给"
                 "所选服务商，可提高准确率（按量计费）",
        )
        st.caption(
            "准确率（整条情感，冻结基线 2026-08-21）：词典模式 50.9%"
            "（主集 n=175）；LLM 精分析 86.9%（主集 n=175）／87.5%（边界集 n=220）。"
        )
        # 2.8（2026-08-18）：LLM 相关性复核随 LLM 自动开启，不再提供独立开关
        relevance_check_enabled = llm_enabled
        if llm_enabled:
            with st.expander("❓ 如何获取 API Key（小白版）"):
                st.markdown(API_KEY_GUIDE)
            saved_key = load_api_key(allow_env=False)
            api_key = st.text_input(
                "API Key", type="password", key="api_key_input",
                value=saved_key,
                help="不填则使用词典模式，离线可跑；本机已保存的 Key 会自动回填",
            )
            col_save, col_clear = st.columns(2)
            if col_save.button("💾 保存到本机", width="stretch"):
                if api_key and api_key.strip():
                    try:
                        save_api_key(api_key.strip())
                    except Exception:
                        # F-017（2026-08-26）：保存失败明确报错，不静默吞掉
                        st.error("保存失败（权限/只读目录）：请检查 data/secrets/ 目录可写后重试。")
                    else:
                        st.success("已加密保存到本机（Windows DPAPI）")
                        st.rerun()  # 保存后立即刷新配置中心状态区
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
        # F-011/F-014（2026-08-26）：配置中心 = 状态区 + Node/opencli 安装面板闭环
        # （③页引导按钮通过 cfg_center_expander 让本区展开）
        st.session_state.setdefault("cfg_center_expander", True)
        with st.expander("⚙️ 配置中心", key="cfg_center_expander"):
            st.caption("凭据与运行环境状态（凭据仅本机加密保存）")
            # F-017（2026-08-26）：区分「输入框已填（未保存）」与「已保存」——
            # 配置中心回答的是「本机是否已持久化」，但用户心智中"填了=配了"，
            # 因此输入框有值但未保存时状态行明示，保存后 st.rerun() 实时变绿。
            _llm_st = llm_status()
            _api_input = str(st.session_state.get("api_key_input", "") or "")
            if not _llm_st["has_key"] and _api_input.strip():
                _llm_st = {
                    "level": "warn",
                    "text": "⚠️ LLM API Key：输入框已填（未保存）——点击「保存到本机」后生效",
                    "has_key": False,
                }
            _wb_st = weibo_status()
            _wb_input = str(st.session_state.get("weibo_cookie", "") or "")
            if not _wb_st["has_key"] and _wb_input.strip():
                _wb_st = {
                    "level": "warn",
                    "text": "⚠️ 微博 Cookie：输入框已填（未保存）——勾选「记住 Cookie」或提交时保存",
                    "has_key": False,
                }
            _cfg_rows = [
                _llm_st,
                _wb_st,
                node_status(),
                opencli_status(),
            ]
            for _st in _cfg_rows:
                st.markdown(_st["text"])
            st.divider()
            st.markdown("**Node.js / opencli 一键安装**")
            render_node_opencli_install()
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
            st.divider()
            st.markdown("**渠道风控安全（每日配额）**")
            st.caption(
                "每日配额按「关键词数 × 每关键词上限」估算消耗；微博/小红书有默认上限，"
                "可在下方调整（-1=不限；不限的渠道不在此显示）。"
                "检测到风控时渠道会自动冷却，可在此手动解除。"
            )
            _quota_channels = list(
                st.session_state.get("channel_ids_opt")
                or st.session_state.get("channel_ids")
                or []
            )
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

    st.session_state["_sidebar_api_key"] = api_key
    st.session_state["_sidebar_base_url"] = base_url
    st.session_state["_sidebar_model_name"] = model_name
    st.session_state["_sidebar_llm_enabled"] = llm_enabled
    st.session_state["_sidebar_narrative_enabled"] = narrative_enabled

