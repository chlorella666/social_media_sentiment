"""main.py 拆分模块（2026-08-22）：结果页与人工复核/反馈闭环。"""

from __future__ import annotations

from app.coding.coder import display_confidence
from app.coding.insights import (build_descriptors, build_report_content, template_chart_insights)
from app.coding.llm_analyzer import create_analyzer
from app.core import (errors, feedback, jobs, terms)
from app.core.evidence import (build_evidence, build_findings, dimension_evidence_label, display_action, display_finding_id, findings_section_title, findings_to_conclusion)
from app.core.models import ReportBundle
from app.core.names import (dimension_cn, platform_cn, register_custom_dim_names)
from app.core.pipeline import (bundle_to_json, generate_report_text, recompute_summary)
from app.output.excel_writer import build_excel
from app.output.html_report import (_collection_notes, build_html, cooccurrence_fig, cooccurrence_plan, date_dim_heatmap_fig, dimensions_fig, heatmap_fig, intensity_fig, narrative_actor_fig, narrative_frame_actor_heatmap, narrative_insight_text, overall_fig, platform_dim_fig, platform_fig, radar_fig, sentiment_sources_fig, topic_cluster_rows, topic_pairs, trend_fig, wordcloud_png_bytes, words_fig)
from app.output.word_report import build_word
from collections import Counter
from pathlib import Path
import hashlib
import html
import json
import streamlit as st
from app.ui.tasks import (_render_task_logs)
from app.ui.wizard import (reset_wizard)

REVIEW_PAGE_SIZE = 20  # 人工筛选分页每页条数

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
    api_key = st.session_state.get("_sidebar_api_key", "")
    base_url = st.session_state.get("_sidebar_base_url", "")
    model_name = st.session_state.get("_sidebar_model_name", "")

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
        # F-018（2026-08-26，修订版）：规则路径单区合并——findings 不再产出
        # 统计总结型 claim（structured_summary 唯一承担统计结论），置空由解读区承载
        from app.coding.rule_insights import build_structured_summary
        report_content = {
            "chart_insights": template_chart_insights(build_descriptors(new_summary)),
            "findings": [],
            "conclusion": "",
            "insight_mode": new_mode,
            "structured_summary": build_structured_summary(
                new_summary, evidence, new_summary.get("top_phrases")
            ),
            "structured_summary_source": "rule",
        }
    new_bundle = bundle.model_copy(update={
        "summary": new_summary,
        "evidence": evidence,
        "chart_insights": report_content["chart_insights"],
        "conclusion": report_content["conclusion"],
        "findings": report_content["findings"],
        "insight_mode": report_content["insight_mode"],
        "structured_summary": report_content.get("structured_summary") or {},
        "structured_summary_source": report_content.get("structured_summary_source", "rule"),
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
        "同一行两个判断：**不相关/无意义** = 剔除（帖子随帖评论级联）；"
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
    only_llm = c3.checkbox("只看 LLM 建议不相关/无意义", key=f"rv_llm_{task_id}")
    only_ad_suggested = c4.checkbox("只看广告预标", key=f"rv_ad_suggested_{task_id}")
    tc1, tc2, tc3 = st.columns(3)
    if tc1.button("全部标记不相关/无意义", key=f"rv_all_{task_id}"):
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
            st.caption("🔖 LLM 建议不相关/无意义（人工最终决定）")
        if suggested:
            st.caption(f"🔖 广告规则预标：{suggested}")
    mark = c2.checkbox("不相关/无意义", value=excluded, key=_review_key("rv", url))
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
            st.caption(f"帖子已标记不相关/无意义，{len(comments)} 条评论随帖剔除")
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

def render_results():
    api_key = st.session_state.get("_sidebar_api_key", "")
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
    _is_demo = bool(st.session_state.get("demo_report"))
    if _is_demo:
        _demo_subject = bundle.plan.subject or "示例"
        # F-020（2026-08-26）：演示数据为恋与深空玩家讨论（脱敏样本）——
        # 内置 demo_source.json 随源码打包；无内置源时回退虚构数据并标注
        _demo_src_note = (
            "演示数据为恋与深空玩家讨论（脱敏样本）"
            if "恋与深空" in _demo_subject
            else "内置虚构演示数据"
        )
        st.info(
            f"📋 这是**演示报告**：示例为「{html.escape(_demo_subject)}」"
            f"（{_demo_src_note}；LLM 精分析模式效果），用于展示报告形态。"
            "真实分析请在向导中提交任务。"
        )
        if st.button("← 返回向导", key="demo_back"):
            reset_wizard()
            st.rerun()
    # 固定「只看结论」视图（2026-08-22 用户决定：不再提供「全部图表」切换）
    show_all = False
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
    <div class="k" title="{terms.TOOLTIPS['avg_score']}">平均情感分</div>
    <div class="v">{s['avg_score']:.2f}<span style="font-size:14px;font-weight:600;color:var(--text-muted)"> / 1.0</span></div>
    <div class="d">区间 −1.0 ~ +1.0</div>
  </div>
  <div class="stat-card">
    <div class="k">负面占比</div>
    <div class="v neg">{dist['negative']['ratio'] * 100:.1f}<span style="font-size:14px;font-weight:600;color:var(--text-muted)">%</span></div>
    <div class="d">{f'集中在「{_worst}」维度' if _worst != '—' else '—'}</div>
  </div>
  <div class="stat-card">
    <div class="k" title="{terms.TOOLTIPS['consumer_voice']}">消费者声音</div>
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
    if (
        nr_unreviewed
        and not _is_demo
        and not st.session_state.get(f"nr_skip_{task_id}")
    ):
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
            "配置中心：未填 API Key → 无 LLM 深度分析。本次为词典模式，"
            "维度情感与结论仅供参考；填写 Key 后开启 LLM 精分析可获得可归因的结论与行动建议。"
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
            f"🔍 需复核样本（{len(nr_items)} 条：低置信/反讽/黑话/问句/短句等难例）",
            expanded=len(nr_items) <= 20 or focus,
        ):
            if _is_demo:
                st.caption(
                    "演示报告为静态示例：以下需复核样本仅作展示，不支持回填；"
                    "真实任务可在此逐条复核并自动重算报告。"
                )
            else:
                st.caption(
                    "人工确认后回填情感并标记已复核；**全部确认后报告统计/图表/Excel/HTML "
                    "会按复核结果自动重算**（确认最后一条即自动刷新，无需手动操作）。"
                )
                st.caption(
                    "词典模式说明：此处仅列出低置信/反讽/黑话等难例；"
                    "普通词典判定不进入复核区（如需逐条审核请开启「人工筛选」）。"
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
                elif _is_demo:
                    c5.caption("演示样本")
                else:
                    b1, b2, b3 = c5.columns(3)
                    if b1.button("正", key=f"nr_pos_{task_id}_{i}"):
                        _apply_review_item(task_id, it, "positive")
                    if b2.button("负", key=f"nr_neg_{task_id}_{i}"):
                        _apply_review_item(task_id, it, "negative")
                    if b3.button("中", key=f"nr_neu_{task_id}_{i}"):
                        _apply_review_item(task_id, it, "neutral")

    # 2.11 方案 A：复核后重新生成 LLM 深度结论（基于当前复核结果）
    if st.session_state.get(f"nr_had_{task_id}") and not _is_demo:
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
        with st.expander("📊 各平台情感分布", expanded=False):
            st.plotly_chart(platform_fig(s), width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('platform', '')}")
        with st.expander("📈 时间趋势", expanded=False):
            st.plotly_chart(trend_fig(s), width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('trend', '')}")
        with st.expander("🔥 情绪强度分布", expanded=False):
            st.plotly_chart(intensity_fig(s), width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('intensity', '')}")
        with st.expander("🧩 维度分析", expanded=False):
            _render_dim_charts(s, bundle)
    # 非核心钻取区：结论视图折叠；全部视图展开（词云保持按需生成）
    pd_fig = platform_dim_fig(s)
    if pd_fig:
        with st.expander("📊 平台 × 维度负面率", expanded=show_all):
            st.plotly_chart(pd_fig, width="stretch")
            st.markdown(f"**解析：**{bundle.chart_insights.get('platform_dim', '')}")
    # F-019（2026-08-26）：主题上主视觉——代表观点区默认可见（主题观点卡），
    # 原始短语降级为证据层（hover 代表短语 + 主题洞察可展开）
    st.markdown("### 🧩 主题观点（代表观点）")
    st.plotly_chart(words_fig(s), width="stretch")
    st.markdown(f"**解析：**{bundle.chart_insights.get('words', '')}")
    st.caption(
        "观点按主题（同义归并）聚合展示；悬停查看代表短语，"
        "下方「主题洞察」可展开代表短语与原文，占比分母为所属情感子集。"
    )
    # F-021（2026-08-26）：主题洞察仅 LLM 模式显示（折叠保持收起，效果走主视觉）；
    # 词典模式回退单词词频，显示引导
    _topics = s.get("topics") or []
    if _topics:
        with st.expander("🧩 主题洞察", expanded=False):
            st.caption(
                "主题由编码分析生成（LLM 归类 + 系统计数）；"
                "提及量按去重文本计，占比分母为所属情感子集。"
            )
            for _t in _topics[:8]:
                _tpol = {"positive": "正面", "negative": "负面", "neutral": "中性"}.get(
                    _t.get("polarity", ""), "中性"
                )
                st.markdown(
                    f"**{_t.get('name', '')}**（{dimension_cn(_t.get('dimension', ''))}）· "
                    f"{_t.get('count', 0)} 条 · {_tpol}主导"
                )
                st.caption("、".join(_t.get("phrases") or []))
    elif bundle.insight_mode == "lexicon":
        st.caption(
            "💡 开启 LLM 精分析可获得主题洞察"
            "（词典模式为单词级统计，主题归并需 LLM 编码分析）。"
        )

    # F-018（2026-08-26，修订版）：词典/规则模式合并为单一「解读与建议」区——
    # structured_summary 为主干（一句话结论 → 正负反馈 → 重点问题归因 → 改进建议）；
    # LLM 模式无 structured_summary（保留 findings + conclusion），本区不渲染。
    if bundle.structured_summary:
        _ss = bundle.structured_summary
        _ss_src = bundle.structured_summary_source or "rule"
        _ss_label = "规则推测（非 AI 归因）"
        with st.expander(f"📊 解读与建议（{_ss_label}）", expanded=True):
            st.markdown(f"**一句话结论：**{_ss.get('overall', '')}")
            _pos = _ss.get("positive") or {}
            _neg = _ss.get("negative") or {}
            if _pos:
                _pp = "；".join(
                    f"{p.get('phrase', '')}（{p.get('count', 0)} 条/"
                    f"{p.get('ratio', 0) * 100:.0f}%）"
                    for p in _pos.get("phrases") or []
                )
                st.markdown(
                    f"**正面反馈**：占比 {_pos.get('ratio', 0) * 100:.1f}%"
                    + (f"；{_pp}" if _pp else "")
                )
            if _neg:
                _np = "；".join(
                    f"{p.get('phrase', '')}（{p.get('count', 0)} 条/"
                    f"{p.get('ratio', 0) * 100:.0f}%）"
                    for p in _neg.get("phrases") or []
                )
                st.markdown(
                    f"**负面反馈**：占比 {_neg.get('ratio', 0) * 100:.1f}%"
                    + (f"；{_np}" if _np else "")
                )
            for _issue in _ss.get("top_issues") or []:
                st.markdown(
                    f"**{_issue.get('name', '')}**"
                    f"（负面率 {_issue.get('rate', 0) * 100:.0f}%，n={_issue.get('count', 0)}）"
                )
                if _issue.get("cause"):
                    st.markdown(f"*可能原因：{_issue['cause']}*")
                if _issue.get("direction"):
                    st.markdown(f"建议：{_issue['direction']}")
            if _ss.get("improvements"):
                st.markdown("**改进建议**")
                for _im in _ss["improvements"]:
                    st.markdown(f"- {_im}")
            st.caption("原因基于统计特征的规则推测，非 AI 归因，请结合报告原文验证。")
            # F-018（2026-08-26，修订版）：findings 降级为证据支撑——
            # 「查看证据与原文」折叠明确标注为支撑材料而非第二结论
            if bundle.evidence:
                with st.expander("查看证据与原文（支撑材料）", expanded=False):
                    st.caption("以下为支撑上述解读的统计卡与原文摘录，非第二结论。")
                    for _c in bundle.evidence[:40]:
                        if _c.get("kind") == "stat":
                            st.markdown(f"📊 {_c.get('text', '')}（n={_c.get('n')}）")
                        else:
                            _j = (
                                "LLM 判定" if _c.get("judge") == "llm"
                                else "词典判定 · 仅供参考"
                            )
                            st.markdown(
                                f"「{_c.get('text', '')}」\n\n"
                                f"来源：{_c.get('platform', '')} · "
                                f"{_c.get('date') or '日期未知'} · {_j}"
                            )
    with st.expander("☁️ 情感词云", expanded=False):
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
        with st.expander("🕸 讨论话题共现网络", expanded=show_all):
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
                            "代表短语": r["words"],
                            "涉及文本数": r["doc_count"],
                            "负面率": r["negative_rate"],
                        }
                        for r in cluster_rows
                    ]
                )
    elif cooccurrence_plan(s)["kind"] == "pairs":
        with st.expander("🕸 话题词对榜", expanded=show_all):
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
        with st.expander("🗣 负面情绪来源话题榜", expanded=show_all):
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

