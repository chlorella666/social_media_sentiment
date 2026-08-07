"""社交媒体情感分析器 — Streamlit 向导入口。

运行：python -m streamlit run app/main.py
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from pathlib import Path

import streamlit as st

from app import __version__
from app.channels.registry import list_channel_infos
from app.channels.health import check_channel_health
from app.core.models import KeywordGroup, TaskStatus
from app.core.pipeline import TaskRunner, bundle_to_json
from app.core.planner import build_plan, flatten_keywords, generate_keyword_groups
from app.core.pricing import estimate_cost
from app.core.secrets import clear_cookie, load_cookie, save_cookie
from app.coding.llm_analyzer import create_analyzer
from app.domains.loader import load_domain, list_domains
from app.output.excel_writer import build_excel
from app.output.html_report import (
    cooccurrence_fig,
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
from app.output.html_report import build_html
from app.output.word_report import build_word

st.set_page_config(page_title="社交媒体情感分析器", page_icon="📊", layout="wide")

REPORTS_DIR = Path(__file__).resolve().parent.parent / "data" / "reports"
SENTIMENT_NAMES = {"positive": "正面", "negative": "负面", "neutral": "中性"}

CHANNEL_LIMIT_DEFAULTS = {
    "demo": 10,
    "bilibili": 20,
    "websearch": 10,
    "websearch_zhihu": 10,
    "websearch_tieba": 10,
    "websearch_taptap": 10,
    "weibo": 20,
    "xiaohongshu": 10,
}


def estimate_collection(
    keywords: list[str],
    channel_ids: list[str],
    limits: dict[str, int],
    comments_per_post: int,
) -> tuple[int, int, float]:
    """粗估（链接数, 评论数, 分钟）；实际受网络与平台频率限制影响。"""
    items = 0
    comments = 0
    seconds = 0.0
    for cid in channel_ids:
        n = int(limits.get(cid, CHANNEL_LIMIT_DEFAULTS.get(cid, 10)))
        k = max(len(keywords), 1)
        if cid == "bilibili":
            eff = n
            per_k = (n / 20) * 1.5 + min(n, 10) * 0.6
            com = min(n, 10) * comments_per_post
        elif cid == "weibo":
            eff = n
            per_k = n / 10 + min(n, 10) * 0.6
            com = min(n, 10) * comments_per_post
        elif cid == "xiaohongshu":
            eff = min(n, 10)
            per_k = 15 + eff * 13 + min(n, 5) * 13
            com = min(n, 5) * comments_per_post
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
        seconds += k * per_k
    return items, comments, round(seconds * 1.2 / 60, 1)


if "stage" not in st.session_state:
    st.session_state.stage = 0


def reset_wizard() -> None:
    for key in [
        "stage", "mode", "subject", "domain_id", "selected_dims", "keywords",
        "channel_ids", "date_range", "plan", "bundle", "output_files",
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
    api_key = st.text_input("API Key（可选）", type="password",
                            help="不填则使用词典预筛模式，离线可跑")
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
    comments_enabled = st.toggle("抓取评论", value=True)
    comments_per_post = st.slider("每帖评论上限", 0, 50, 20, key="comments_per_post")
    per_keyword_limit = st.slider("每关键词采集上限", 10, 200, 50, key="per_keyword_limit")
    st.caption("提示：LLM 设置也可通过环境变量 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL 配置。")
    st.caption(f"应用版本：v{__version__}")

# ---------------------------------------------------------------------------
# 顶部标题
# ---------------------------------------------------------------------------

st.title("📊 社交媒体情感分析器")
st.caption("输入品牌/产品名或关键词 → 确认维度与采集计划 → 自动生成图表与分析报告")


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
        options = ["不使用领域"] + [f"{d['name']}（{d['id']}）" for d in domains]
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
        custom = st.text_input("添加自定义维度（可选）", placeholder="例如：联名活动")
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
            groups.append(
                KeywordGroup(
                    dimension_id="custom",
                    dimension_name="自定义",
                    keywords=[f"{subject} {custom_dim}"],
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

    # 高风控渠道（微博/小红书）：默认上限自动调低，避免账号被平台风控/封禁
    risky_selected = [cid for cid in ("weibo", "xiaohongshu") if cid in selected]
    if risky_selected and st.session_state.get("per_keyword_limit", 50) == 50:
        recommended = 20 if "xiaohongshu" in risky_selected else 30
        st.session_state.per_keyword_limit = recommended
        st.rerun()
    if risky_selected:
        names = "、".join(
            {"weibo": "微博", "xiaohongshu": "小红书"}[cid] for cid in risky_selected
        )
        st.warning(
            f"⚠️ 高频采集会触发平台限制：{names} 风控严格，"
            "微博可能被临时限制甚至封号、小红书会触发验证码。"
            "已把每关键词默认上限调低（可在左侧边栏改回），请避免短时间重复运行。"
        )

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
                help="默认仅存于当前会话；勾选下方选项可加密保存到本机",
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
                    "4. 在请求列表里点任意一条指向 `m.weibo.cn` 的请求\n"
                    "5. 在右侧 **Headers → Request Headers** 里找到 **Cookie** 一栏，"
                    "把整段 Cookie 值复制下来\n"
                    "6. 粘贴到上面的输入框（直接粘整段最稳）\n\n"
                    "⚠️ Cookie 相当于账号凭证，请只在你自己电脑上使用；"
                    "程序仅存于当前会话，不会写入磁盘或报告。"
                )
            st.warning(
                "⚠️ 微博对自动化采集风控严格：高频请求可能导致账号被临时限制甚至封禁；"
                "请保持默认上限，避免短时间重复运行。"
            )
        if "xiaohongshu" in selected:
            st.warning(
                "⚠️ 小红书对高频采集会触发验证码，严重时影响登录态；"
                "建议每关键词上限 ≤20，两次分析之间留出间隔。"
            )
            st.info(
                "需要：Chrome 已登录 xiaohongshu.com + opencli 已安装"
                "（npm install -g @jackwener/opencli）；采集较慢，每个关键词约 1~2 分钟。"
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

    if selected:
        st.divider()
        st.markdown("**每关键词采集条数上限（按渠道）**")
        info_map = {i["id"]: i["name"] for i in infos}
        st.session_state.channel_limits = {}
        lim_cols = st.columns(min(len(selected), 4))
        for i, cid in enumerate(selected):
            with lim_cols[i % 4]:
                default = CHANNEL_LIMIT_DEFAULTS.get(cid, 10)
                val = st.number_input(
                    f"{info_map.get(cid, cid)}",
                    min_value=1,
                    max_value=200,
                    value=default,
                    key=f"limit_{cid}",
                    help="每个关键词最多抓取多少条链接（小红书单次建议 ≤10，风控严格）",
                )
                st.session_state.channel_limits[cid] = int(val)
        est_items, est_comments, est_min = estimate_collection(
            st.session_state.get("keywords", []),
            selected,
            st.session_state.channel_limits,
            st.session_state.get("comments_per_post", 20),
        )
        st.caption(
            f"预计采集：链接约 {est_items} 条、评论约 {est_comments} 条，"
            f"耗时约 {est_min} 分钟（受网络与平台频率限制影响）"
        )
        if any(cid.startswith("websearch") for cid in selected):
            st.session_state.websearch_eval_suffix = st.toggle(
                "WebSearch 自动追加『评价』后缀（纯品牌词易命中官网）",
                value=st.session_state.get("websearch_eval_suffix", True),
            )
            st.session_state.official_domains = st.text_input(
                "排除的官方域名（逗号分隔，可选）",
                placeholder="例如：dji.com,crv.com.cn",
                help="官网不会提供有效的社交媒体情感信息，命中这些域名的搜索结果将被排除",
                value=st.session_state.get("official_domains", ""),
            )
            if st.button("🔍 渠道体检（只读探测）"):
                cookie = st.session_state.get("weibo_cookie", "")
                for cid in selected:
                    ok, msg = check_channel_health(cid, {"cookie": cookie})
                    if ok:
                        st.success(f"✅ {cid}：{msg}")
                    else:
                        st.warning(f"⚠️ {cid}：{msg}")

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
        st.session_state.get("comments_per_post", 20),
    )
    cost_est = estimate_cost(
        est_items + est_comments, narrative_enabled, relevance_check_enabled
    )
    summary_rows = [
        ("分析对象", st.session_state.get("subject", "")),
        ("领域", st.session_state.get("domain_id") or "不使用"),
        ("关键词数", str(len(keywords))),
        ("渠道", "、".join(channel_ids)),
        ("官方域名排除", official_domains or "未配置"),
        ("WebSearch 评价后缀", "开" if st.session_state.get("websearch_eval_suffix", True) else "关"),
        ("LLM 相关性复核", "开（费用与耗时增加）" if relevance_check_enabled else "关"),
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
        plan = build_plan(
            subject=st.session_state.get("subject", ""),
            domain_id=st.session_state.get("domain_id") or None,
            dimension_ids=st.session_state.get("selected_dims", []),
            keyword_groups=st.session_state.get("keyword_groups", []),
            manual_keywords=None
            if st.session_state.get("domain_id")
            else st.session_state.get("keywords", []),
            channel_ids=channel_ids,
            date_start=date_range[0] if isinstance(date_range, tuple) else None,
            date_end=date_range[1] if isinstance(date_range, tuple) else None,
            per_keyword_limit=per_keyword_limit,
            comments_enabled=comments_enabled,
            comments_per_post=comments_per_post,
            llm_enabled=llm_enabled,
            narrative_enabled=narrative_enabled,
            relevance_check_enabled=relevance_check_enabled,
            channel_params=channel_params,
        )

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
        bar = st.progress(0.0, text="准备中...")
        status = st.status("任务进行中...", expanded=True)
        with status:
            step_slots = {
                sid: st.progress(0.0, text=f"⏳ {label}：等待中")
                for sid, label in STEP_DEFS
            }
        if llm_enabled:
            st.caption(
                "提示：LLM 精分析按 10 条/批并发调用 DeepSeek，进度与剩余时间会按批实时更新；"
                "耗时取决于 DeepSeek 响应速度，属正常现象。开启叙事/归因分析时会额外多一段进度。"
            )

        def on_progress(
            task_status: TaskStatus,
            message: str,
            phase_progress: float,
            snapshot: dict | None = None,
        ) -> None:
            clamped = min(max(phase_progress, 0.0), 1.0)
            bar.progress(clamped, text=message)
            status.update(label=f"{message}（{clamped:.0%}）")
            if snapshot:
                for sid, slot in step_slots.items():
                    st_data = snapshot["steps"].get(sid)
                    if not st_data:
                        continue
                    icon = STEP_ICONS.get(st_data["state"], "⏳")
                    text = f"{icon} {st_data['label']}"
                    if st_data["detail"]:
                        text += f"：{st_data['detail']}"
                    slot.progress(min(st_data["frac"], 1.0), text=text)

        runner = TaskRunner(plan, on_progress=on_progress)
        analyzer = None
        if llm_enabled and api_key:
            analyzer = create_analyzer(api_key=api_key, base_url=base_url, model=model_name)
        try:
            bundle = runner.run(analyzer=analyzer)
        except Exception as exc:
            st.error(f"分析失败：{exc}")
            st.stop()

        # 输出物落盘
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = REPORTS_DIR / ts
        out_dir.mkdir(parents=True, exist_ok=True)
        excel_bytes = build_excel(bundle).getvalue()
        html_content = build_html(bundle)
        bundle_to_json(bundle, out_dir / "result.json")
        (out_dir / "report.html").write_text(html_content, encoding="utf-8")
        st.session_state.bundle = bundle
        st.session_state.output_files = {
            "excel": excel_bytes,
            "html": html_content,
        }
        st.session_state.stage = 5
        st.rerun()

elif stage == 5:
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
    st.subheader("⑥ 分析结果")

    for w in bundle.warnings:
        st.warning(w)

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
    wc_bytes = wordcloud_png_bytes(s)
    if wc_bytes:
        st.image(wc_bytes, caption="内容关键词词云（jieba 分词）", width=700)
    st.markdown(f"**解析：**{bundle.chart_insights.get('wordcloud', '')}")

    co_fig = cooccurrence_fig(s)
    if co_fig:
        st.plotly_chart(co_fig, width="stretch")
        st.markdown(f"**解析：**{bundle.chart_insights.get('cooccurrence', '')}")

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
steps = ["输入对象", "维度", "关键词", "渠道/时间", "确认运行", "结果"]
current = min(int(st.session_state.get("stage", 0)), 5)
st.caption("步骤：" + " → ".join(f"{'●' if i == current else '○'} {s}" for i, s in enumerate(steps)))
