"""main.py 拆分模块（2026-08-22）：向导步骤 0-3、计划恢复与顶部分步条。"""

from __future__ import annotations

from app.ui.constants import (AD_REVIEW_MODES, AD_REVIEW_SHORT, CHANNEL_LIMIT_DEFAULTS, CHANNEL_LIMIT_HELP, CHANNEL_LIMIT_MAX, COMMENT_FETCH_SECONDS, HEALTH_LEVEL_CN, WS_PROBE_STATUS_CN, _WIZARD_STEP_ICONS)
from app.ui.dev import (EVAL_URL, _eval_running, _start_eval_dashboard)
from app.channels import health
from app.channels.registry import list_channel_infos
from app.core import (jobs, plans_store)
from app.core.keyword_effects import (expand_channel_queries, expand_websearch_keywords)
from app.core.planner import (build_plan, parse_custom_dimensions)
from app.core.pricing import estimate_cost
from app.core.config_status import node_status, opencli_status, weibo_status
from app.core.secrets import (clear_cookie, load_cookie, save_api_key, save_cookie)
from app.domains.composer import (MAX_DIMENSIONS, MODULE_DESC, compose_schema, filter_schema_dims, module_name)
from app.domains.loader import save_cached_schema
import datetime as dt
import html
import re
import streamlit as st

def _apply_restored_widgets():
    """P1：widget 预填延迟应用（在 widget 渲染前统一落位）。"""
    for _wk in (
        "llm_enabled", "narrative_enabled", "wizard_mode",
        "comments_enabled", "comments_per_post", "exclude_words", "ad_review_mode",
        "websearch_eval_suffix_toggle", "kwopt_weibo_toggle",
        "module_select", "dim_select",
    ):
        _restored_val = st.session_state.pop(f"_restore_{_wk}", None)
        if _restored_val is not None:
            st.session_state[_wk] = _restored_val



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

def reset_wizard() -> None:
    for key in [
        "stage", "mode", "subject", "domain_id", "selected_dims", "selected_modules",
        "schema", "custom_dim", "custom_dimensions", "custom_dim_errors",
        "module_select", "dim_select",
        "_last_modules_key", "_dim_restored_run",
        "manual_keywords", "keyword_groups",
        "websearch_eval_suffix", "keywords",
        "kwopt_bilibili", "kwopt_weibo", "kwopt_xiaohongshu",
        "channel_ids", "channel_ids_opt", "date_range", "date_range_opt", "plan",
        "bundle", "output_files", "task_id",
        "comments_enabled", "comments_per_post", "comments_enabled_opt", "comments_per_post_opt",
        "ad_review_mode", "ad_review_mode_opt", "review_enabled_opt", "exclude_ad_opt",
        "exclude_words", "exclude_words_opt", "exclude_words_text",
        "channel_limits", "official_domains", "websearch_eval_suffix_toggle", "kwopt_weibo_toggle",
        "demo_report",
    ]:
        st.session_state.pop(key, None)
    st.session_state.stage = 0

def next_stage() -> None:
    st.session_state.stage = int(st.session_state.get("stage", 0)) + 1

def prev_stage() -> None:
    st.session_state.stage = max(int(st.session_state.get("stage", 0)) - 1, 0)

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
    st.session_state.channel_ids = channel_ids or []
    st.session_state.channel_ids_opt = channel_ids or []
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
            st.session_state.date_range_opt = st.session_state.date_range

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
            st.session_state.schema = compose_schema(mods)
        except Exception:
            pass
        st.session_state._restore_module_select = mods
        st.session_state._restore_dim_select = list(st.session_state.selected_dims)
        st.session_state["_dim_restored_run"] = True
    else:
        st.session_state.schema = None
    # demo-only 计划恢复时明示，避免"以为在跑真实渠道"（2026-08-20 走查）
    if channel_ids and all(cid == "demo" for cid in channel_ids):
        _demo_note = "（注意：该计划渠道为演示数据，只会生成演示内容，不采集真实数据）"
        if st.session_state.get("plan_restored_notice"):
            st.session_state["plan_restored_notice"] += _demo_note

def _modules_for_domain(domain_id: str | None) -> list[str]:
    """domain_id（modules_content_physical）→ 模块列表反查。"""
    if not domain_id or not domain_id.startswith("modules_"):
        return []
    parts = domain_id[len("modules_"):].split("_")
    return [p for p in parts if p in ("content", "physical", "service")]

def render_stage0():
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
                key="module_select",
            )
            st.session_state.selected_modules = mods
            # 模块组合变更时，维度选择重置为「当前组合全部维度默认全选」
            # （恢复计划时跳过，保留计划里的维度选择）
            _dim_restored = st.session_state.pop("_dim_restored_run", False)
            if not _dim_restored and st.session_state.get("_last_modules_key") != tuple(mods):
                st.session_state.pop("dim_select", None)
                st.session_state["_dim_reset_pending"] = True
            st.session_state["_last_modules_key"] = tuple(mods)
            schema = None
            if mods:
                try:
                    schema = compose_schema(mods)
                except ValueError as exc:
                    st.warning(str(exc))
            if schema:
                st.session_state.schema = schema
                names = {d.id: f"{d.name} — {d.description}" for d in schema.dimensions}
                if st.session_state.pop("_dim_reset_pending", False):
                    # 显式落位全选，避免不同会话状态下 default 不生效
                    st.session_state["dim_select"] = [d.id for d in schema.dimensions]
                selected = st.multiselect(
                    "选择要分析的维度（默认全选；组合最多 10 个，报告按维度统计）",
                    options=[d.id for d in schema.dimensions],
                    format_func=lambda k: names[k],
                    default=[d.id for d in schema.dimensions],
                    key="dim_select",
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


def render_stage1():
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


def render_stage2():
        st.subheader("③ 选择采集渠道与时间段")
        infos = list_channel_infos()
        selected = st.multiselect(
            "采集渠道",
            options=[i["id"] for i in infos],
            key="channel_ids",
            format_func=lambda cid: {
                i["id"]: f"{i['name']} — {i['applicability']}"
                for i in infos
            }[cid],
        )
        # 2026-08-20 走查修复：控件卸载会清理 widget key，镜像到普通键，
        # 确认页/提交读取镜像（否则④确认页显示真实渠道、提交时被清理回退 demo）
        st.session_state.channel_ids_opt = selected

        # 渠道提示：已选渠道的风控/前置条件/采集规则统一展示（避免警告散落各处）
        risk_texts = []
        info_texts = []
        if "weibo" in selected:
            risk_texts.append(
                "微博：风控严格，高频请求可能导致账号被临时限制甚至封禁；"
                "默认上限 10，请避免短时间重复运行。"
            )
        if "xiaohongshu" in selected:
            # F-025（2026-08-26）：风控提示新口径
            risk_texts.append(
                "小红书：风控严格，高频采集会触发验证码，严重时影响登录态；"
                "默认上限 10，采集较慢，每个关键词约 1~2 分钟。"
            )
        if risk_texts:
            st.warning("⚠️ 渠道风控提示\n" + "\n".join(f"- {t}" for t in risk_texts))
        for t in info_texts:
            st.info(t)

        if "xiaohongshu" in selected:
            # F-023/F-024（2026-08-26）：③页不再展示 Node/opencli 重复状态行
            # （侧边栏配置中心已承担）；前置条件仅在任一未就绪时按需呈现
            _node_st = node_status()
            _ocl_st = opencli_status()
            if not (_node_st["has_key"] and _ocl_st["has_key"]):
                st.info(
                    "小红书前置条件：1、Chrome 已登录 xiaohongshu.com；"
                    "2、opencli 已安装（在左侧 ⚙️ 配置中心一键安装，无需手动命令）。"
                )
                st.warning(
                    "小红书需要 Node.js 与 opencli，当前未就绪。"
                    "请到左侧 **⚙️ 配置中心** 一键安装（无需手动命令）。"
                )
                if st.button(
                    "去左侧 ⚙️ 配置中心安装", key="stage2_go_cfg_center", width="stretch"
                ):
                    st.session_state["cfg_center_expander"] = True
                    st.rerun()

        col1, col2 = st.columns(2)
        with col1:
            default_start = dt.date.today() - dt.timedelta(days=30)
            # 控件卸载会清理 widget key：镜像键恢复/落位（与 channel_ids_opt 同模式，2026-08-22）
            if st.session_state.get("date_range_opt") and "date_range" not in st.session_state:
                st.session_state.date_range = st.session_state.date_range_opt
            date_range = st.date_input(
                "时间段", value=(default_start, dt.date.today()),
                key="date_range",
                help=(
                    "时间段过滤器：平台不提供按历史日期检索能力，此设置只保证"
                    "『不保留超出窗口的内容』，不保证『窗口内的内容都采得到』"
                    "（各渠道只能返回近期内容，历史内容请定期运行分析积累）。"
                ),
            )
            st.session_state.date_range_opt = date_range
        with col2:
            if "weibo" in selected:
                _wb_saved = load_cookie("weibo")
                if not st.session_state.get("weibo_cookie") and _wb_saved:
                    st.session_state.weibo_cookie = _wb_saved
                # F-011：无 Cookie 黄色警示条（配置后实时消失）
                if not st.session_state.get("weibo_cookie", "") and not _wb_saved:
                    st.warning(
                        "⚠️ 未配置微博 Cookie：该渠道采集会失败。请按下方 5 步获取"
                        "（Application/Storage 路径），或取消勾选微博。"
                    )
                with st.container(border=True):
                    st.markdown("🔐 **微博 Cookie（该渠道采集必需）**")
                    st.caption(
                        "填写才能采集微博；不填则微博渠道采集会失败。"
                        "Cookie 将以 Windows DPAPI 加密仅保存在本机，不会写入报告或上传。"
                    )
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
                    cc1, cc2 = st.columns(2)
                    if cc1.button("🔍 校验 Cookie 是否有效", key="weibo_check_btn", width="stretch"):
                        if cookie.strip():
                            _ck = health.check_weibo_rich(cookie.strip())
                            if _ck["ok"]:
                                st.success("微博登录态有效 ✅")
                            else:
                                st.error(f"Cookie 无效或已过期：{_ck['msg']}")
                        else:
                            st.warning("请先粘贴 Cookie 再校验")
                    if cc2.button("清除已保存的 Cookie", width="stretch"):
                        clear_cookie("weibo")
                        st.session_state.weibo_cookie = ""
                        st.session_state.weibo_cookie_remember = False
                        st.rerun()
                    with st.expander("❓ 怎么获取微博 Cookie？（小白版）"):
                        st.markdown(
                            "1. 登录微博：电脑浏览器打开 **m.weibo.cn** 并完成登录\n"
                            "2. 打开开发者工具：按 **F12**，或右键页面选「检查」\n"
                            "3. 点顶部 **Application（应用程序）** 标签"
                            "（某些浏览器显示为「存储 / Storage」）\n"
                            "4. 左侧展开 **Storage → Cookies**，选择 https://m.weibo.cn\n"
                            "5. 找到 **SUB** 这一行，复制它对应的 **VALUE** 值，"
                            "粘贴到上方微博 Cookie 文本框\n\n"
                            "💡 直接粘贴 VALUE 即可（程序会自动补 `SUB=` 前缀）；"
                            "如果只看到整行 Cookie，也可以整段粘贴。\n"
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
            # 控件卸载会清理 widget key：返回本页时用镜像键回填控件（2026-08-22 加固）
            if "comments_enabled" not in st.session_state and "comments_enabled_opt" in st.session_state:
                st.session_state.comments_enabled = st.session_state.comments_enabled_opt
            if "comments_per_post" not in st.session_state and "comments_per_post_opt" in st.session_state:
                st.session_state.comments_per_post = st.session_state.comments_per_post_opt
            c1, c2 = st.columns([1, 3])
            with c1:
                comments_enabled = st.toggle(
                    "抓取评论", value=True, key="comments_enabled",
                    help="关闭后不抓取评论，只分析帖子正文。"
                    "评论抓取仅对 B站/微博/小红书渠道生效",
                )
            with c2:
                comments_per_post = st.slider(
                    "每帖评论上限", 0, 10, 3, key="comments_per_post",
                    help=(
                        "每条帖子最多抓取多少条评论（评论越多采集越慢）。"
                        "评论抓取范围：每个关键词最多收录 10 帖，"
                        "评论只对其中最热（按点赞）前 5 帖抓取，"
                        "每帖最多抓取设定的评论条数——降低操作频率以防验证码与风控、控制采集耗时。"
                    ),
                )
            # Streamlit 会在控件卸载时清理其 widget key，镜像到普通键供确认页/提交读取
            st.session_state.comments_enabled_opt = comments_enabled
            st.session_state.comments_per_post_opt = comments_per_post
            st.divider()
            st.markdown("**每关键词采集条数上限（按渠道）**")
            _prev_limits = st.session_state.get("channel_limits", {})
            st.session_state.channel_limits = {}
            lim_cols = st.columns(min(len(selected), 4))
            for i, cid in enumerate(selected):
                with lim_cols[i % 4]:
                    default = CHANNEL_LIMIT_DEFAULTS.get(cid, 10)
                    if f"limit_{cid}" not in st.session_state and cid in _prev_limits:
                        st.session_state[f"limit_{cid}"] = int(_prev_limits[cid])
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
                with st.expander("⚙️ 关键词优化", expanded=False):
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
            if "exclude_words" not in st.session_state and "exclude_words_text" in st.session_state:
                st.session_state.exclude_words = st.session_state.exclude_words_text
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
            _exclude_raw = st.session_state.get("exclude_words", "")
            st.session_state.exclude_words_text = _exclude_raw
            st.session_state.exclude_words_opt = [
                t.strip()
                for t in re.split(r"[,，、\s]+", _exclude_raw)
                if t.strip()
            ]
            # 广告/官方与人工复核（方案 A：单三选控件取代两个开关，2026-08-16）
            _review_on = bool(st.session_state.get("review_enabled_opt", False))
            _exclude_on = bool(st.session_state.get("exclude_ad_opt", False))
            _mode_default = 3 if _review_on else (2 if _exclude_on else 1)
            if "ad_review_mode" not in st.session_state and "ad_review_mode_opt" in st.session_state:
                st.session_state.ad_review_mode = st.session_state.ad_review_mode_opt
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
            st.session_state.ad_review_mode_opt = _mode
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


def render_stage3():
    api_key = st.session_state.get("_sidebar_api_key", "")
    llm_enabled = st.session_state.get("_sidebar_llm_enabled", False)
    narrative_enabled = st.session_state.get("_sidebar_narrative_enabled", False)
    base_url = st.session_state.get("_sidebar_base_url", "")
    model_name = st.session_state.get("_sidebar_model_name", "")
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
    channel_ids = st.session_state.get("channel_ids_opt") or st.session_state.get("channel_ids", [])
    date_range = st.session_state.get("date_range_opt") or st.session_state.get("date_range") or (dt.date.today() - dt.timedelta(days=30), dt.date.today())
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
        st.session_state.get("comments_per_post_opt", 3),
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
            st.session_state.get("ad_review_mode_opt") or st.session_state.get("ad_review_mode", ""),
            AD_REVIEW_SHORT[AD_REVIEW_MODES[0]])),
        ("词云排除词", "、".join(st.session_state.get("exclude_words_opt", [])) or "未配置"),
        ("预计 LLM 费用", f"约 ¥{cost_est['estimated_cost']}（预估）"),
        (
            "预计采集",
            f"链接约 {est_items} 条、评论约 {est_comments} 条，耗时约 {est_min} 分钟",
        ),
        ("时间段", f"{date_range[0]} ~ {date_range[1]}" if isinstance(date_range, tuple) else "不限"),
        ("LLM 精分析", (
            "开（未配置 Key → 降级词典）" if (llm_enabled and not api_key)
            else ("开" if llm_enabled else "关（词典模式）")
        )),
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
        st.error(
            "❌ 已开启 LLM 精分析但**未填写 API Key**：本次将全程使用词典模式，"
            "维度结论仅供参考。请返回侧边栏「大模型设置」填写并保存 Key 后再提交。"
        )
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
        comments_per_post=st.session_state.get("comments_per_post_opt", 3),
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
    # 2026-08-20 走查：demo 渠道不得静默提交——确认页显著警示 + 显式勾选
    demo_only = bool(plan.channels) and all(
        cfg.channel_id == "demo" for cfg in plan.channels
    )
    if demo_only:
        st.error(
            "⚠️ 本次计划只包含「演示数据」渠道：**不会采集任何真实内容**，"
            "报告为内置模拟数据。若需分析真实品牌，请返回 ③ 选择真实渠道。"
        )
        st.session_state.demo_only_confirm = st.checkbox(
            "我确认本次只跑演示数据（不采集真实内容）",
            key="demo_only_confirm_box",
        )
    else:
        st.session_state.demo_only_confirm = False

    # F-011：微博无 Cookie 阻断 + 逃生口二次确认
    # （与 demo-only 互斥：计划含真实微博渠道即非全 demo，两套勾选天然不同时出现）
    _weibo_no_cookie = (
        "weibo" in channel_ids
        and not st.session_state.get("weibo_cookie", "")
        and not load_cookie("weibo")
    )
    if _weibo_no_cookie:
        st.error(
            "❌ 已选择微博渠道但未配置微博 Cookie：该渠道采集会失败。"
            "请返回 ③ 配置 Cookie（Application/Storage → SUB VALUE），"
            "或勾选下方逃生口后提交。"
        )
        st.session_state.weibo_escape_confirm = st.checkbox(
            "我了解该渠道会失败，仍要提交（不勾选则提交被拦截）",
            key="weibo_escape_confirm_box",
        )
    else:
        st.session_state.weibo_escape_confirm = False

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
            "⏱ 暂无同类任务历史，耗时无法预估；建议先跑小规模任务"
            "（1~2 个关键词、单个渠道）验证链路。"
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
        if not plan.channels:
            st.error("未选择任何采集渠道，请返回 ③ 至少选择一个渠道。")
            st.stop()
        if demo_only and not st.session_state.get("demo_only_confirm"):
            st.error("本次为演示数据任务：请先勾选「我确认本次只跑演示数据」再启动。")
            st.stop()
        if _weibo_no_cookie and not st.session_state.get("weibo_escape_confirm"):
            st.error(
                "已选择微博渠道但未配置微博 Cookie：请返回 ③ 配置 Cookie，"
                "或勾选「我了解该渠道会失败，仍要提交」后重试。"
            )
            st.stop()
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

def render_step_bar():
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

def render_wizard():
    stage = st.session_state.get("stage", 0)
    if stage == 0:
        render_stage0()
    elif stage == 1:
        render_stage1()
    elif stage == 2:
        render_stage2()
    elif stage == 3:
        render_stage3()


