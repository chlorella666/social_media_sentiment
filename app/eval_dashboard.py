# -*- coding: utf-8 -*-
"""黄金集评测仪表盘（阶段 2.1，开发者工具，小白友好版）。

运行：
    python -m streamlit run app/eval_dashboard.py

数据：data/benchmark/（runs/<时间戳>/report.json 完整明细 + history.jsonl 摘要），
由 tests/benchmark_golden.py 每次评测自动写入；目录可用 SMS_BENCHMARK_DIR 覆盖。

页面结构（标签页）：
- 概览：一句话总结 + 概览指标（悬浮"？"解释）+ 准确率趋势
- 哪里好哪里差：按渠道/领域/情感类别/类型/语言现象细分跑分 + 短板清单
- 情感表现：情感类别 P/R/F1 + 混淆矩阵
- 运行对比：任选两次运行对比 + 错误样本下钻
- 关键词效果：扫描 / 前后对照 / 按任务筛选漏斗 / 丢弃原因 / 后缀效果 / 候选确认
- 运行历史：历史列表 + 一键重跑词典评测
"""

from __future__ import annotations

import html
import subprocess
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.core import eval_store  # noqa: E402
from app.core import keyword_effects as ke  # noqa: E402

st.set_page_config(page_title="评测中心 · 黄金集", layout="wide")

MODE_LABEL = {
    "lexicon": "词典直判（无 LLM）",
    "hybrid": "混合流水线（词典+LLM）",
    "edge_lexicon": "边界集·词典直判（无 LLM）",
    "edge_hybrid": "边界集·混合流水线（词典+LLM）",
    "domain_digital3c_lexicon": "数码3C·词典直判（无 LLM）",
    "domain_digital3c_hybrid": "数码3C·混合流水线（词典+LLM）",
}
GROUP_LABEL = {
    "by_channel": "渠道",
    "by_domain": "领域",
    "by_gold_sentiment": "情感类别（gold）",
    "by_kind": "类型",
    "by_flag": "语言现象",
    "by_subset": "边界子集",
}
CLASS_ORDER = ["positive", "negative", "neutral"]
CLASS_CN = {"positive": "正面", "negative": "负面", "neutral": "中性"}

# 术语 -> 大白话解释（悬浮"？"展示）
TOOLTIPS = {
    "accuracy": "整体准确率：模型判定与人工标注一致的比例，越高越好",
    "n": "样本数：该分组的样本条数；样本太少时数字波动大",
    "lexicon_rate": "词典直判率：不花钱、直接靠内置词典判定的文本比例",
    "dim_f1": "维度微平均 F1：判断“每条文本在哪些维度上表达了正面/负面”准不准"
              "（2.4 维度级情感口径，0~1，越高越好）",
    "llm_tokens": "LLM tokens：本次评测消耗的大模型 token 数（词典模式为 0）",
    "golden_fp": "样本版本号：黄金集内容的指纹，防止新旧样本对比出错",
    "delta": "Δpp：和上次相比的变化（百分点），+2.0pp 表示提升 2 个百分点",
    "ref": "参考：样本太少（<30 条），数字波动大，先别下结论",
    "shortboard": "短板：样本≥30 且准确率<75% 的分组，最值得优化",
    "mode": "评测模式：词典=只用内置词典（免费、结果可复现）；混合=词典+AI 精分析（更准、花钱）",
    "prf": "P/R/F1：精确率=判对的结果里真对的占比；召回率=该抓到的抓了多少；F1 是两者的平衡分",
    "confusion": "混淆矩阵：行=人工标注，列=模型判定；对角线是判对的，其他格子是“把 A 判成 B”的错",
    "query": "实际查询串：系统真正发给搜索引擎的词，可能自动加了后缀或子渠道提示",
    "effective": "有效供给率：搜 100 条能留下多少条（保留÷采集），越高说明关键词越有效",
    "unattributed": "未归属丢弃：旧数据没记关键词的丢弃，无法归到具体查询串",
    "candidate": "候选：系统从历史数据里建议试试的新词，人工确认后才会启用",
    "suffix": "后缀效果：加不同后缀（评价/怎么样…）后，采集到的内容能用多少",
    "err_drill": "错误样本下钻：把判错的文本列出来，看错在哪、错得有没有规律",
}


def _fmt_acc(v) -> str:
    return "—" if v is None else f"{v:.1%}"


def _fmt_pp(v) -> str:
    return "—" if v is None else f"{v:+.1f}pp"


def _tip(key: str) -> str:
    text = TOOLTIPS.get(key, "")
    if not text:
        return ""
    return f' <abbr title="{html.escape(text)}">？</abbr>'


def _md_label(label: str, tip_key: str = "") -> str:
    return f"**{label}**{_tip(tip_key)}"


def _segment_rows(cmp: dict, group_key: str, shortboard_groups: set[str]) -> list[dict]:
    rows = []
    for seg, v in cmp.get(group_key, {}).items():
        short = ""
        n = v.get("n") or 0
        acc = v.get("accuracy")
        if group_key in shortboard_groups and n >= eval_store.REF_N and acc is not None and acc < 0.75:
            short = "短板"
        rows.append({
            "细分": seg,
            "n": n,
            "本次准确率": _fmt_acc(acc),
            "上次准确率": _fmt_acc(v.get("prev_accuracy")),
            "Δpp": _fmt_pp(v.get("delta_pp")),
            "状态": v.get("status"),
            "备注": "参考（n<30）" if v.get("ref") else short,
        })
    return rows


def _remove_strategy_word(word: str, subject: str) -> None:
    """从策略配置删除指定词（额外查询或后缀池）；不在配置中则抛错。"""
    strat = ke.load_keyword_strategy()
    extra = (strat.get("extra_queries") or {}).get(subject, [])
    if word in extra:
        ke.save_strategy_edit(extra_queries_updates={subject: [w for w in extra if w != word]})
        return
    pool = strat.get("suffix_pool") or []
    if word in pool:
        ke.save_strategy_edit(suffix_pool=[s for s in pool if s != word])
        return
    raise ValueError(f"「{word}」不在策略配置中（可能来自手动关键词，请在向导中修改）")


def _plain_summary(history: list[dict], runs: list[dict], latest: dict,
                   prev: dict | None, mode: str, ke_history: list[dict]) -> str:
    """一句话人话总结：整体升降 / 负面短板 / 细分短板 / 下一步。"""
    lines: list[str] = []
    acc = latest.get("accuracy")
    if acc is not None:
        if prev and prev.get("accuracy") is not None:
            d = (acc - prev["accuracy"]) * 100
            if abs(d) < 1:
                trend = "和上次持平"
            elif d > 0:
                trend = f"比上次提升 {d:.1f} 个百分点"
            else:
                trend = f"比上次下降 {abs(d):.1f} 个百分点"
        else:
            trend = "暂无上次可对比"
        lines.append(f"「{MODE_LABEL.get(mode, mode)}」整体准确率 {acc:.1%}，{trend}。")
    neg = (latest.get("by_sentiment_class") or {}).get("negative") or {}
    if neg.get("recall") is not None:
        r = neg["recall"]
        if r < 0.6:
            lines.append(
                f"负面召回率 {r:.0%}——真正的负面只抓到 {r:.0%}，漏掉较多，是最需要提升的。"
            )
        else:
            lines.append(f"负面召回率 {r:.0%}，负面抓取尚可。")
    short: list[str] = []
    for gk, gname in (("by_channel", "渠道"), ("by_domain", "领域"),
                      ("by_gold_sentiment", "情感类别"), ("by_subset", "边界子集")):
        for seg, v in (latest.get("groups") or {}).get(gk, {}).items():
            n = v.get("n") or 0
            a = v.get("accuracy")
            if n >= eval_store.REF_N and a is not None and a < 0.75:
                short.append(f"{gname}「{seg}」{a:.0%}（样本 {n}）")
    if short:
        lines.append("短板：" + "；".join(short[:3]) + (" 等" if len(short) > 3 else "") + "，建议优先优化。")
    else:
        lines.append("未发现明显短板（样本充足的分组准确率均 ≥75%）。")
    if ke_history:
        lines.append("关键词效果数据已就绪，可在「关键词效果」页确认候选词，再跑一次任务做前后对照。")
    else:
        lines.append("还没跑过真实采集任务，先跑一次含 WebSearch 的任务，积累关键词效果数据。")
    return "\n".join(f"· {ln}" for ln in lines)


def _render_guide() -> None:
    """首次进入三步引导（会话内记住）。"""
    if st.session_state.get("eval_guide_done"):
        return
    with st.expander("👋 首次使用引导（3 步）", expanded=True):
        st.markdown("1. **跑一次评测**：词典评测点下方「运行历史」页的「重新跑词典评测」；真实任务在分析向导里跑。")
        st.markdown("2. **看总结和短板**：本页顶部会告诉你整体好不好、哪里是短板；细节在「哪里好哪里差」页。")
        st.markdown("3. **确认新词再对比**：在「关键词效果」页勾选候选词写入策略，再跑一次任务，用「前后对照」看有没有变好。")
        if st.button("我已了解，开始使用", key="eval_guide_ok"):
            st.session_state["eval_guide_done"] = True
            st.rerun()


def main() -> None:
    st.title("🎯 黄金集评测中心")
    st.caption(
        "开发者调优工具：看分析准不准、关键词采得好不好。"
        "把鼠标移到「？」上可查看术语解释；明细（含错误样本原文）仅存本机 data/benchmark/，不入库。"
    )

    history = eval_store.load_history()
    if not history:
        st.info(
            "暂无评测历史。先运行词典评测（无需 Key）：`python tests/benchmark_golden.py`，"
            "或混合评测（需 Key）：`python tests/benchmark_golden.py --llm`。"
        )
        return

    with st.sidebar:
        st.header("筛选")
        mode = st.radio(
            "评测模式",
            ["lexicon", "hybrid", "edge_lexicon", "edge_hybrid"],
            format_func=lambda m: MODE_LABEL.get(m, m),
        )
        st.markdown(_md_label("评测模式是什么", "mode"), unsafe_allow_html=True)
        st.caption("词典模式确定性可重复；混合模式受 LLM 温度影响，对比仅供参考。")
        st.divider()
        st.caption("数据目录：")
        st.code(str(eval_store._benchmark_dir()))

    runs = [h for h in history if h.get("mode_key") == mode]
    runs.sort(key=lambda h: (h.get("ts") or "", h.get("run_id") or ""))
    if not runs:
        st.warning(f"「{MODE_LABEL.get(mode, mode)}」暂无历史，请先跑一次评测。")
        return
    latest = runs[-1]
    prev = runs[-2] if len(runs) >= 2 else None

    _render_guide()
    ke_history = ke.load_history()
    st.info(_plain_summary(history, runs, latest, prev, mode, ke_history))

    tab_overview, tab_segments, tab_sentiment, tab_compare, tab_keywords, tab_strategy, tab_history = st.tabs(
        ["📊 概览", "🎯 哪里好哪里差", "😊 情感表现", "🔁 运行对比",
         "🔑 关键词效果", "🎛 关键词策略配置", "🕘 运行历史"]
    )

    # ---------- 概览 ----------
    with tab_overview:
        st.subheader("概览")
        delta = None
        if prev is not None and latest.get("accuracy") is not None and prev.get("accuracy") is not None:
            delta = f"{(latest['accuracy'] - prev['accuracy']) * 100:+.1f}pp"
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.markdown(_md_label("整体准确率", "accuracy"), unsafe_allow_html=True)
        c1.metric("整体准确率", _fmt_acc(latest.get("accuracy")), delta, label_visibility="collapsed")
        c2.markdown(_md_label("主评分样本 n", "n"), unsafe_allow_html=True)
        c2.metric("主评分样本 n", latest.get("n_main"), label_visibility="collapsed")
        routing = latest.get("routing") or {}
        c3.markdown(_md_label("词典直判率", "lexicon_rate"), unsafe_allow_html=True)
        c3.metric("词典直判率", _fmt_acc(routing.get("direct_rate")), label_visibility="collapsed")
        dim = latest.get("dimension") or {}
        c4.markdown(_md_label("维度情感 F1（2.4 口径）", "dim_f1"), unsafe_allow_html=True)
        c4.metric("维度情感 F1（2.4 口径）",
                  "—" if dim.get("micro_f1") is None else f"{dim['micro_f1']:.2f}",
                  label_visibility="collapsed")
        llm_usage = latest.get("llm_usage") or {}
        c5.markdown(_md_label("LLM tokens", "llm_tokens"), unsafe_allow_html=True)
        c5.metric("LLM tokens",
                  "—" if not llm_usage else
                  f"{llm_usage.get('prompt_tokens', 0) + llm_usage.get('completion_tokens', 0):,}",
                  label_visibility="collapsed")
        c6.markdown(_md_label("golden 指纹", "golden_fp"), unsafe_allow_html=True)
        c6.metric("golden 指纹", (latest.get("golden_fp") or "—")[:18], label_visibility="collapsed")
        if prev is None:
            st.caption("本次为「该模式 + 该黄金集」的首跑，无上次可对比。")
        elif prev.get("summary_only"):
            st.caption("上次为冻结基线回填（摘要级），无细分数据可对比。")
        if any(
            h.get("dimension_metric") == "mention"
            and (h.get("dimension") or {}).get("micro_f1") is not None
            for h in history
        ):
            st.caption("注意：历史中 2.4 之前的维度 F1 为「提及识别」旧口径，"
                       "与新「维度情感」口径不可直接对比（趋势仅参考整体准确率）。")

        st.subheader("准确率趋势（全部模式）")
        trend_rows = []
        for h in history:
            if h.get("accuracy") is None:
                continue
            trend_rows.append({
                "时间": (h.get("ts") or "")[:16],
                "模式": MODE_LABEL.get(h.get("mode_key"), h.get("mode_key", "")),
                "准确率": h["accuracy"],
                "类型": "冻结基线" if h.get("summary_only") else "评测",
            })
        if trend_rows:
            tdf = pd.DataFrame(trend_rows)
            fig = px.line(
                tdf, x="时间", y="准确率", color="模式", markers=True,
                title=None,
            )
            fig.update_layout(yaxis_tickformat=".0%", height=360,
                              legend=dict(orientation="h", yanchor="bottom", y=1.02))
            st.plotly_chart(fig, use_container_width=True)

    # ---------- 哪里好哪里差 ----------
    with tab_segments:
        st.subheader("细分跑分（本次 vs 上次）")
        st.markdown(
            _md_label("Δpp / 参考 / 短板", "delta") + " · "
            + _tip("ref") + " · " + _tip("shortboard"),
            unsafe_allow_html=True,
        )
        group_key = st.selectbox(
            "细分维度", list(GROUP_LABEL), format_func=lambda k: GROUP_LABEL[k]
        )
        cmp = eval_store.compare_segments(latest, prev)
        rows = _segment_rows(
            cmp, group_key,
            shortboard_groups={"by_channel", "by_domain", "by_gold_sentiment"},
        )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption(
            "n<30 标「参考」（小样本不作判断依据）；渠道/领域/情感类别 n≥30 且准确率<75% 标「短板」。"
        )
        short_lines = []
        for gk, gname in (("by_channel", "渠道"), ("by_domain", "领域"),
                          ("by_gold_sentiment", "情感类别"), ("by_subset", "边界子集")):
            for seg, v in cmp.get(gk, {}).items():
                n = v.get("n") or 0
                acc = v.get("accuracy")
                if n >= eval_store.REF_N and acc is not None and acc < 0.75:
                    short_lines.append(f"{gname}「{seg}」：准确率 {acc:.0%}（样本 {n} 条）")
        st.markdown("**短板清单（样本≥30 且准确率<75%）**")
        if short_lines:
            for line in short_lines:
                st.markdown(f"- ⚠️ {line}")
        else:
            st.caption("暂无短板。")

    # ---------- 情感表现 ----------
    with tab_sentiment:
        st.subheader("情感类别 P/R/F1 与混淆矩阵")
        st.markdown(_md_label("P/R/F1 与混淆矩阵怎么看", "prf") + " · " + _tip("confusion"),
                    unsafe_allow_html=True)
        cls_rows = []
        for cls in CLASS_ORDER:
            v = (latest.get("by_sentiment_class") or {}).get(cls) or {}
            cls_rows.append({
                "类别": CLASS_CN.get(cls, cls),
                "n_gold": v.get("n_gold"),
                "n_pred": v.get("n_pred"),
                "精确率": "—" if v.get("precision") is None else f"{v['precision']:.2f}",
                "召回率": "—" if v.get("recall") is None else f"{v['recall']:.2f}",
                "F1": "—" if v.get("f1") is None else f"{v['f1']:.2f}",
            })
        conf = latest.get("confusion_matrix") or {}
        cm_df = pd.DataFrame(0, index=[CLASS_CN[c] for c in CLASS_ORDER],
                             columns=[CLASS_CN[c] for c in CLASS_ORDER])
        for k, cnt in conf.items():
            if "->" not in k:
                continue
            pred, gold = k.split("->", 1)
            if pred in CLASS_CN and gold in CLASS_CN:
                cm_df.loc[CLASS_CN[gold], CLASS_CN[pred]] = cnt
        c_left, c_right = st.columns([1, 1.4])
        with c_left:
            st.dataframe(pd.DataFrame(cls_rows), use_container_width=True, hide_index=True)
        with c_right:
            st.dataframe(cm_df.rename_axis("gold \\ pred"), use_container_width=True)

    # ---------- 运行对比 ----------
    with tab_compare:
        st.subheader("任选两次运行对比")
        run_ids = [h["run_id"] for h in runs]
        idx_b = len(runs) - 2 if len(runs) >= 2 else 0
        base_id = st.selectbox("基准运行（左侧）", run_ids, index=idx_b, key="base_run")
        cur_id = st.selectbox("对比运行（右侧，即本次）", run_ids, index=len(runs) - 1, key="cur_run")
        base = next(h for h in runs if h["run_id"] == base_id)
        cur = next(h for h in runs if h["run_id"] == cur_id)
        rc = eval_store.compare_runs(cur, base)
        c1, c2 = st.columns(2)
        c1.metric("基准准确率", _fmt_acc(base.get("accuracy")), base.get("run_id"))
        c2.metric("本次准确率", _fmt_acc(cur.get("accuracy")),
                  _fmt_pp(rc.get("overall_delta_pp")))
        if rc.get("prev_summary_only"):
            st.caption("基准为冻结基线回填（摘要级），仅整体可对比。")
        all_rows = []
        rc_segments = rc.get("segments") or {}
        for gk, gname in GROUP_LABEL.items():
            for row in _segment_rows(rc_segments, gk, shortboard_groups=set()):
                all_rows.append({"维度": gname, **row})
        st.dataframe(pd.DataFrame(all_rows), use_container_width=True, hide_index=True)

        st.subheader("错误样本下钻（对比运行）")
        st.markdown(_md_label("错误样本下钻是什么", "err_drill"), unsafe_allow_html=True)
        run_report = eval_store.load_run(cur_id) or {}
        errors = run_report.get("errors") or []
        if not errors:
            st.caption("该运行没有错误样本（或报告未包含 errors）。")
        else:
            err_df = pd.DataFrame(errors)
            gk = st.selectbox("错误筛选维度", list(GROUP_LABEL),
                              format_func=lambda k: GROUP_LABEL[k], key="err_group")
            segs = sorted({r.get("platform") if gk == "by_channel" else
                           r.get("domain") if gk == "by_domain" else
                           r.get("gold") if gk == "by_gold_sentiment" else
                           r.get("kind") if gk == "by_kind" else
                           r.get("subset") if gk == "by_subset" else
                           (r.get("flags") or "")
                           for r in errors})
            picked = st.multiselect("筛选段（留空=全部）", segs, key="err_segs")
            sub = err_df
            if picked:
                if gk == "by_gold_sentiment":
                    sub = err_df[err_df["gold"].isin(picked)]
                elif gk == "by_kind":
                    sub = err_df[err_df["kind"].isin(picked)]
                elif gk == "by_channel":
                    sub = err_df[err_df["platform"].isin(picked)]
                elif gk == "by_domain":
                    sub = err_df[err_df["domain"].isin(picked)]
                elif gk == "by_subset":
                    sub = err_df[err_df["subset"].isin(picked)]
                else:
                    sub = err_df[err_df["flags"].isin(picked)]
            st.dataframe(
                sub[[c for c in ("text_id", "text", "gold", "pred", "platform",
                                 "domain", "kind", "flags", "llm_used") if c in sub.columns]],
                use_container_width=True, hide_index=True,
            )
            st.caption(f"共 {len(errors)} 条错误，当前筛选 {len(sub)} 条。原文仅本地查看。")

    # ---------- 关键词效果 ----------
    with tab_keywords:
        st.subheader("关键词效果（2.2）")
        st.markdown(_md_label("有效供给率 / 查询串 / 候选", "effective") + " · "
                    + _tip("query") + " · " + _tip("candidate") + " · " + _tip("unattributed"),
                    unsafe_allow_html=True)
        if st.button("扫描 data/reports 并刷新", key="ke_scan"):
            proc = subprocess.run(
                [sys.executable, str(ROOT / "app" / "core" / "keyword_effects.py"),
                 "--scan-dir", str(ROOT / "data" / "reports"), "--candidates"],
                cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            st.code("\n".join([ln for ln in out.splitlines() if ln.strip()][-20:]))
            if proc.returncode == 0:
                st.success("扫描完成，已刷新关键词效果数据。")
                st.rerun()
            else:
                st.error("扫描失败，请查看上方输出。")
        if not ke_history:
            st.info(
                "暂无关键词效果历史：点击上方按钮扫描历史任务报告；"
                "新任务采集会自动带关键词/查询串埋点。"
            )
        else:
            agg = ke.aggregate_unique(ke_history)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("已扫描任务", agg["tasks"])
            c2.metric("查询串数", len(agg["funnel"]))
            c3.metric("LLM 费用合计（元）", f"{agg['llm_cost']:.2f}")
            c4.metric("未归属丢弃", agg["unattributed_dropped"])

            # 前后对照
            ke_runs = [h for h in ke_history if h.get("funnel")]
            if ke_runs:
                st.markdown("##### 前后对照（策略效果判定）")
                default_subject = ke_runs[-1].get("subject") or "（无主题）"
                ke_subjects = sorted({h.get("subject") or "（无主题）" for h in ke_runs})
                subj_pick = st.selectbox(
                    "对比品牌",
                    ke_subjects,
                    index=ke_subjects.index(default_subject)
                    if default_subject in ke_subjects else 0,
                    key="ke_ba_subject",
                )
                brand_runs = [
                    h for h in ke_runs
                    if (h.get("subject") or "（无主题）") == subj_pick
                ]
                klabels = [
                    f"{str(h.get('created_at') or '')[:16]} · {h.get('subject') or '（无主题）'} · "
                    f"{len(h.get('funnel') or [])} 查询"
                    for h in brand_runs
                ]
                ibase = max(0, len(brand_runs) - 2)
                base_label = st.selectbox("基准任务（旧）", klabels, index=ibase, key="ke_ba_base")
                cur_label = st.selectbox("对比任务（新）", klabels, index=len(brand_runs) - 1, key="ke_ba_cur")
                base = brand_runs[klabels.index(base_label)]
                cur = brand_runs[klabels.index(cur_label)]

                brand_agg = ke.aggregate_unique(brand_runs)
                m = ke.compare_runs_metrics(base, cur, agg_rows=brand_agg["funnel"])
                eff = m["effective"]
                c1, c2, c3 = st.columns(3)
                c1.metric(
                    "有效供给率（WebSearch）",
                    f"{eff['after_pct']}%" if eff["after_pct"] is not None else "—",
                    f"{eff['delta_pp']:+.1f}pp" if eff["delta_pp"] is not None else None,
                    help="达标要求较基准提升 ≥5pp",
                )
                c1.caption(
                    f"基准 {eff['before_pct']}%（n={eff['before_n']}）→ "
                    f"本次 {eff['after_pct']}%（n={eff['after_n']}）· {eff['verdict']}"
                )
                pb = m["pure_brand"]
                if pb.get("present"):
                    drop_delta = None
                    if pb.get("before_drop_pct") is not None and pb.get("after_drop_pct") is not None:
                        drop_delta = round(pb["after_drop_pct"] - pb["before_drop_pct"], 1)
                    before_txt = f"{pb['before_drop_pct']}%" if pb.get("before_drop_pct") is not None else "—"
                    after_txt = f"{pb['after_drop_pct']}%" if pb.get("after_drop_pct") is not None else "—"
                    c2.metric(
                        "纯品牌词丢弃率",
                        after_txt,
                        f"{drop_delta:+.1f}pp" if drop_delta is not None else None,
                        help="确认关键词=品牌名那组查询的丢弃率，达标 <15%",
                    )
                    c2.caption(f"基准 {before_txt} → 本次 {after_txt} · {pb['verdict']}")
                else:
                    c2.metric(
                        "纯品牌词丢弃率",
                        "无法判定",
                        help="需任务含「确认关键词=品牌名」的词才能计算",
                    )
                    c2.caption(pb.get("note", ""))
                nq = m["new_queries"]
                c3.metric("新增查询串", nq["count"], help="本次比基准多出来的查询串数量")
                c3.caption(
                    f"新增采集 {nq['collected']} 保留 {nq['kept']}"
                    f"（有效供给率 {nq['effective_pct']}%）"
                    if nq["count"] else "无新增查询串"
                )
                st.info(m["conclusion"])

                st.markdown("**⚠️ 低效词删除建议**")
                st.caption(
                    "低效词按该品牌全部历史任务的累计样本判断"
                    "（n≥30 且 <50% 强建议 / n≥10 且 <40% 弱建议）。"
                )
                low = m["low_efficiency"]
                if low:
                    df_low = pd.DataFrame([{
                        "词": x["word"],
                        "涉及渠道": x["channels"],
                        "采集": x["collected"],
                        "保留": x["kept"],
                        "有效供给率": f"{x['effective_rate']:.1f}%",
                        "建议": x["level"],
                        "说明": x["reason"],
                        "可一键删除": "是" if x["in_strategy"] else "否（手动关键词，请在向导修改）",
                    } for x in low])
                    st.dataframe(df_low, use_container_width=True, hide_index=True)
                    del_options = [x["word"] for x in low if x["in_strategy"]]
                    if del_options:
                        del_word = st.selectbox("选择要删除的词", del_options, key="ke_del_word")
                        if st.button("🗑 从策略配置删除所选词", key="ke_del_btn"):
                            st.session_state["ke_del_confirm"] = True
                        if st.session_state.get("ke_del_confirm"):
                            st.warning(
                                f"确认从策略配置删除「{del_word}」？（自动备份，可回滚）"
                            )
                            if st.button("确认删除", key="ke_del_yes"):
                                try:
                                    _remove_strategy_word(del_word, cur.get("subject") or "")
                                    st.session_state.pop("ke_del_confirm", None)
                                    st.success(
                                        "已删除并自动备份（可在「关键词策略配置」页回滚）"
                                    )
                                    st.rerun()
                                except ValueError as exc:
                                    st.error(str(exc))
                else:
                    st.caption(
                        "暂未识别到低效词：累计样本不足或未跌破阈值，"
                        "再跑几次任务后会自动出现建议。"
                    )

                st.markdown("**按渠道汇总（WebSearch 各子渠道）**")
                ch_rows = [{
                    "渠道": x["channel"],
                    "基准有效率": _fmt_acc(x["before_rate"]),
                    "本次有效率": _fmt_acc(x["after_rate"]),
                    "Δpp": _fmt_pp(x["delta_pp"]),
                } for x in m["per_channel"]]
                st.dataframe(pd.DataFrame(ch_rows), use_container_width=True, hide_index=True)

                st.markdown("**逐查询串明细**")
                def _fmap(entry: dict) -> dict:
                    return {(r.get("channel"), r.get("query")): r for r in (entry.get("funnel") or [])}

                fm_base, fm_cur = _fmap(base), _fmap(cur)
                ba_rows = []
                for (ch, q) in sorted(set(fm_base) | set(fm_cur)):
                    old = fm_base.get((ch, q))
                    new = fm_cur.get((ch, q))
                    old_eff = old.get("effective_rate") if old else None
                    new_eff = new.get("effective_rate") if new else None
                    delta = None
                    if old_eff is not None and new_eff is not None:
                        delta = round((new_eff - old_eff) * 100, 1)
                    status = "新增" if not old else (
                        "持平" if delta is None or abs(delta) < 1 else (
                            "改善" if delta > 0 else "回退"
                        )
                    )
                    n_new = (new or {}).get("collected", 0)
                    n_old = (old or {}).get("collected", 0)
                    ref = "参考" if min(n_new, n_old) < ke.REF_N else ""
                    ba_rows.append({
                        "查询串": q,
                        "渠道": ch,
                        "旧采集": n_old,
                        "旧有效率": _fmt_acc(old_eff),
                        "新采集": n_new,
                        "新有效率": _fmt_acc(new_eff),
                        "Δpp": _fmt_pp(delta),
                        "状态": status,
                        "备注": ref,
                    })
                st.dataframe(pd.DataFrame(ba_rows), use_container_width=True, hide_index=True)
                st.caption("新增=旧任务没有的查询串；Δpp=新-旧；样本<30 标「参考」。")

            # 按任务筛选漏斗
            run_labels = [
                f"{str(h.get('created_at') or '')[:16]} · {h.get('subject') or '（无主题）'} · "
                f"{len(h.get('funnel') or [])} 查询"
                for h in ke_history
            ]
            pick = st.selectbox(
                "任务筛选（只看某一次任务）",
                ["全部任务（聚合）"] + run_labels,
                key="ke_run_filter",
            )
            run_entry = None
            funnel_source = agg["funnel"]
            if pick != "全部任务（聚合）":
                run_entry = ke_history[run_labels.index(pick)]
                funnel_source = run_entry.get("funnel") or []
            funnel_rows = []
            for r in funnel_source[:50]:
                funnel_rows.append({
                    "查询串": r["query"],
                    "渠道": r["channel"],
                    "采集": r["collected"],
                    "保留": r["kept"],
                    "丢弃": r["dropped"],
                    "编码": r["coded"],
                    "有效供给率": _fmt_acc(r["effective_rate"]),
                    "负面率": _fmt_acc(r["negative_rate"]),
                })
            if run_entry is None:
                st.caption(
                    "按查询串聚合漏斗（采集/丢弃/保留/编码）· 当前：全部任务"
                    "（已按 URL 跨任务去重，2026-08-13 口径修订）"
                    + (f"；⚠ {len(agg.get('dedup_missing') or [])} 个旧任务无明细已排除"
                       if agg.get("dedup_missing") else "")
                )
            else:
                st.caption(
                    f"按查询串聚合漏斗（采集/丢弃/保留/编码）· 当前：{pick}"
                    + (
                        f"；该任务另有 {run_entry.get('unattributed_dropped') or 0} "
                        "条丢弃未归属到关键词（旧版数据）"
                        if run_entry.get("unattributed_dropped")
                        else ""
                    )
                )
                if not funnel_source:
                    st.caption("该任务无查询串数据（可能未采集到有效内容）。")
            st.dataframe(pd.DataFrame(funnel_rows), use_container_width=True, hide_index=True)

            reason_counter: dict = {}
            for r in agg["funnel"]:
                for reason, n in (r.get("drop_reasons") or {}).items():
                    reason_counter[reason] = reason_counter.get(reason, 0) + n
            if reason_counter:
                st.caption("丢弃原因分布")
                st.dataframe(
                    pd.DataFrame([{"原因": k, "条数": v} for k, v in
                                  sorted(reason_counter.items(), key=lambda kv: -kv[1])]),
                    use_container_width=True, hide_index=True,
                )
            suffix_rows = [{
                "后缀": s, "查询数": st2["n_queries"], "采集": st2["collected"],
                "保留": st2["kept"], "有效供给率": _fmt_acc(st2["effective_rate"]),
                "参考": "是" if st2["collected"] < ke.REF_N else "",
            } for s, st2 in agg.get("suffix_stats", {}).items()]
            if suffix_rows:
                st.markdown(_md_label("WebSearch 后缀效果（反推依据）", "suffix"),
                            unsafe_allow_html=True)
                st.dataframe(pd.DataFrame(suffix_rows), use_container_width=True, hide_index=True)
            cand = ke.load_candidates()
            if cand:
                df_cand = pd.DataFrame(cand)
                st.caption("候选确认：证据 n≥2；人工确认后写入策略配置，WebSearch 下次采集生效")
                st.dataframe(df_cand, use_container_width=True, hide_index=True)
                idx = st.multiselect(
                    "采纳候选（可多选）", df_cand.index.tolist(),
                    format_func=lambda i: f"{df_cand.loc[i, '类型']}｜"
                                          f"{df_cand.loc[i, '候选']}（n={df_cand.loc[i, '证据n']}）",
                    key="ke_pick",
                )
                if idx and st.button("写入关键词策略配置", key="ke_apply"):
                    ke.apply_candidates(df_cand.loc[idx].to_dict("records"))
                    st.success(
                        "已写入 app/channels/keyword_strategy.json"
                        "（同义词每品牌≤2、额外查询≤5、后缀池≤5）"
                    )
            else:
                st.caption("候选清单为空：运行 `python app/core/keyword_effects.py --candidates` 生成。")

    # ---------- 关键词策略配置 ----------
    with tab_strategy:
        st.subheader("关键词策略配置")
        st.caption(
            "查看/修改 WebSearch 查询策略：同义词与额外查询按品牌生效，后缀池全局生效；"
            "改动只影响之后含 WebSearch 的任务。每次保存/写入候选/还原前会自动备份，可一键回滚。"
        )
        strat = ke.load_keyword_strategy()
        syn = strat.get("synonyms") or {}
        ex = strat.get("extra_queries") or {}
        pool = strat.get("suffix_pool") or []
        all_brands = sorted(set(syn) | set(ex))

        st.markdown("**当前配置总览**")
        overview_rows = []
        for b in all_brands:
            overview_rows.append({
                "品牌": b,
                "同义词": "、".join(syn.get(b, [])) or "—",
                "额外查询": "、".join(ex.get(b, [])) or "—",
            })
        if overview_rows:
            st.dataframe(pd.DataFrame(overview_rows), use_container_width=True, hide_index=True)
        st.caption(
            f"后缀池：{'、'.join(pool) or '（空，回退默认「评价」）'} · "
            f"更新时间：{strat.get('updated_at') or '—'} · "
            "配置文件：app/channels/keyword_strategy.json"
        )

        st.markdown("**编辑品牌配置**")
        brand_options = ["（新增品牌…）"] + all_brands
        sel_brand = st.selectbox("选择品牌", brand_options, key="strat_brand")
        new_brand = ""
        if sel_brand == "（新增品牌…）":
            new_brand = st.text_input("新品牌名", key="strat_new_brand").strip()
        brand = new_brand or (sel_brand if sel_brand != "（新增品牌…）" else "")
        syn_text = st.text_area(
            "同义词（每行一个，最多 2 个；清空=删除该品牌同义词）",
            value="\n".join(syn.get(brand, [])) if brand else "",
            key=f"strat_syn_{brand or '_new'}",
            height=90,
        )
        ex_text = st.text_area(
            "额外查询（每行一个，最多 5 个；清空=删除该品牌额外查询）",
            value="\n".join(ex.get(brand, [])) if brand else "",
            key=f"strat_ex_{brand or '_new'}",
            height=120,
        )
        c_save, c_del = st.columns(2)
        if c_save.button("💾 保存该品牌", key="strat_save"):
            if not brand:
                st.warning("请先选择或输入品牌名")
            else:
                res = ke.save_strategy_edit(
                    synonyms_updates={brand: syn_text.splitlines()},
                    extra_queries_updates={brand: ex_text.splitlines()},
                )
                for w in res["warnings"]:
                    st.warning(w)
                st.success("已保存（旧配置已自动备份）")
                st.rerun()
        if c_del.button("🗑 删除该品牌", key="strat_del"):
            if not brand or brand not in all_brands:
                st.warning("只能删除已存在的品牌")
            else:
                st.session_state["strat_confirm_del"] = True
        if st.session_state.get("strat_confirm_del") and brand in all_brands:
            st.warning(f"确认删除「{brand}」的全部同义词与额外查询？")
            if st.button("确认删除", key="strat_del_yes"):
                ke.save_strategy_edit(
                    synonyms_updates={brand: []},
                    extra_queries_updates={brand: []},
                )
                st.session_state.pop("strat_confirm_del", None)
                st.success("已删除（旧配置已自动备份）")
                st.rerun()

        st.markdown("**后缀池（全局，影响所有品牌）**")
        pool_text = st.text_area(
            "后缀（每行一个，最多 5 个；留空回退默认「评价」）",
            value="\n".join(pool),
            key="strat_pool",
            height=90,
        )
        if st.button("💾 保存后缀池", key="strat_pool_save"):
            res = ke.save_strategy_edit(suffix_pool=pool_text.splitlines())
            for w in res["warnings"]:
                st.warning(w)
            st.success("已保存后缀池（旧配置已自动备份）")
            st.rerun()

        st.markdown("**候选快捷写入**")
        cand = ke.load_candidates()
        if cand:
            df_cand = pd.DataFrame(cand)
            idx = st.multiselect(
                "采纳候选（可多选）", df_cand.index.tolist(),
                format_func=lambda i: f"{df_cand.loc[i, '类型']}｜"
                                      f"{df_cand.loc[i, '候选']}（n={df_cand.loc[i, '证据n']}）",
                key="strat_cand_pick",
            )
            if idx and st.button("写入选中候选", key="strat_cand_apply"):
                ke.apply_candidates(df_cand.loc[idx].to_dict("records"))
                st.success("已写入（自动备份 + 去重 + 上限）")
                st.rerun()
        else:
            st.caption("候选清单为空：在「关键词效果」页点扫描生成。")

        st.markdown("**备份与回滚**")
        backups = ke.list_backups()
        if backups:
            b_names = [b["name"] for b in backups]
            b_pick = st.selectbox("最近备份", b_names, key="strat_backup")
            if st.button("↩ 还原所选备份", key="strat_restore"):
                b = next(x for x in backups if x["name"] == b_pick)
                try:
                    ke.restore_backup(b["path"])
                    st.success("已还原（还原前的配置也已备份）")
                    st.rerun()
                except (OSError, ValueError) as exc:
                    st.error(f"还原失败：{exc}")
        else:
            st.caption("暂无备份。每次保存/写入候选/还原前会自动生成备份。")

        st.markdown("**变更历史（最近 20 条）**")
        hist = ke.load_strategy_history()
        if hist:
            st.dataframe(
                pd.DataFrame([{
                    "时间": h.get("ts", ""),
                    "操作": h.get("action", ""),
                    "详情": (h.get("detail") or "")[:120],
                } for h in hist]),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("暂无变更记录。")

    # ---------- 运行历史 ----------
    with tab_history:
        st.subheader("运行历史")
        hist_rows = [{
            "时间": (h.get("ts") or "")[:19],
            "run_id": h.get("run_id"),
            "准确率": _fmt_acc(h.get("accuracy")),
            "n": h.get("n_main"),
            "类型": "冻结基线" if h.get("summary_only") else "评测",
            "备注": h.get("note") or "",
        } for h in runs]
        st.dataframe(pd.DataFrame(hist_rows), use_container_width=True, hide_index=True)

        st.subheader("重新评测")
        if st.button("重新跑词典评测（无 Key，秒级）", key="rerun_lexicon"):
            proc = subprocess.run(
                [sys.executable, str(ROOT / "tests" / "benchmark_golden.py"), "--no-record"],
                cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            st.code("\n".join([ln for ln in out.splitlines() if ln.strip()][-25:]))
            if proc.returncode == 0:
                st.success("评测完成并写入历史。")
                st.rerun()
            else:
                st.error("评测失败，请查看上方输出。")


if __name__ == "__main__":
    main()
