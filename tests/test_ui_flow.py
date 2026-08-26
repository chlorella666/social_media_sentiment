"""Streamlit 向导端到端冒烟测试（后台任务队列模式，无需浏览器）。

运行：python tests/test_ui_flow.py
覆盖：手动关键词模式 → 全部向导步骤 → 提交后台任务 → 进程内 worker 执行
      → 轮询至完成 → 结果页下载按钮；品牌 + 领域模式全流程。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 队列与输出目录隔离到临时目录（在 AppTest 执行 main.py 之前生效）
_TMP = Path(tempfile.mkdtemp(prefix="sms_ui_test_"))
os.environ["SMS_DB_PATH"] = str(_TMP / "test.db")
os.environ["SMS_STATE_DIR"] = str(_TMP / "state")
os.environ["SMS_SECRETS_DIR"] = str(_TMP / "secrets")
os.environ["SMS_DATA_DIR"] = str(_TMP / "data")
os.environ["SMS_REPORTS_DIR"] = str(_TMP / "reports")
(_TMP / "data").mkdir(parents=True, exist_ok=True)
(_TMP / "reports").mkdir(parents=True, exist_ok=True)

from streamlit.testing.v1 import AppTest

from app import worker
from app.core import jobs
from app.core import plans_store as _plans_store_mod
from app.core.planner import build_plan

# AppTest 环境限制规避：UX 5.1 复用区（上次计划/命名模板）在测试会话之间
# 会污染 stage0 条件渲染 widget 的状态收集（Streamlit testing 框架问题）。
# 复用区与 UI 流程测试目标无关，测试内全局禁用（产品功能不受影响）。
# 注意：先保存真实函数引用（同模块对象，patch 后原引用也会被覆盖）。
_REAL_LOAD_LAST_PLAN = _plans_store_mod.load_last_plan
_REAL_LIST_TEMPLATES = _plans_store_mod.list_templates
_plans_store_mod.load_last_plan = lambda: None
_plans_store_mod.list_templates = lambda: []

worker.REPORTS_DIR = _TMP / "reports"
_STOP = threading.Event()
_WORKER = threading.Thread(
    target=worker.run_loop,
    kwargs={"stop_event": _STOP, "poll_interval": 0.3, "heartbeat_interval": 2.0},
    daemon=True,
)


def click_button(at: AppTest, label: str, timeout: int = 60) -> None:
    matches = [b for b in at.button if b.label == label]
    assert matches, f"未找到按钮「{label}」；现有：{[b.label for b in at.button]}"
    # AppTest 旧帧残留：st.rerun() 后同标签按钮可能在新旧帧各出现一次，
    # 且顺序不稳定。向导导航按钮已加唯一 key（next_<stage>/prev_<stage>），
    # 按当前 stage 定位，避免点到残留帧按钮（2026-08-18）。
    try:
        stage = at.session_state["stage"]
    except Exception:
        stage = None
    target_key = {"下一步 →": f"next_{stage}", "← 上一步": f"prev_{stage}"}.get(label)
    if target_key:
        keyed = [b for b in at.button if b.key == target_key]
        if keyed:
            keyed[-1].click().run(timeout=timeout)
            return
    matches[-1].click().run(timeout=timeout)


def click_button_key(at: AppTest, key: str, timeout: int = 60) -> None:
    matches = [b for b in at.button if b.key == key]
    assert matches, f"未找到按钮 key={key}；现有：{[b.key for b in at.button]}"
    matches[-1].click().run(timeout=timeout)


def confirm_usage_boundary(at: AppTest, timeout: int = 60) -> None:
    """1.5 首次确认门禁：出现确认页则勾选并确认；已确认环境直接跳过。"""
    agree = [c for c in at.checkbox if c.label.startswith("我已阅读并同意")]
    if not agree:
        return
    agree[0].set_value(True).run(timeout=timeout)
    click_button(at, "确认并进入应用", timeout=timeout)


def ss(at: AppTest, key: str, default=None):
    try:
        return at.session_state[key]
    except (KeyError, AttributeError):
        return default


def restore_stale_widget_states(at: AppTest) -> None:
    """规避 AppTest 限制：st.rerun() 中断后元素树停留在旧帧，
    Streamlit 已按新一帧清理旧帧控件的 session_state，
    下一次 run() 收集旧帧控件状态时会 KeyError。
    提交前补回旧帧（阶段3 设置区）控件状态即可继续。"""
    # AppTest 旧帧残留：stage2 的 limit_<cid> / quota_limit_<cid> 控件在切到
    # stage3 后已被 stale 清理，但旧帧节点仍会读取 state；且 channel_ids 现为
    # widget key，切走后 `in` 判断不可靠 → 无条件补全部渠道默认值
    for cid in (
        "demo", "bilibili", "websearch", "websearch_zhihu",
        "websearch_tieba", "websearch_taptap", "weibo", "xiaohongshu",
    ):
        at.session_state[f"limit_{cid}"] = 10
        if f"quota_limit_{cid}" not in at.session_state:
            at.session_state[f"quota_limit_{cid}"] = -1
    if "date_range" not in at.session_state:
        from datetime import date, timedelta

        at.session_state["date_range"] = (
            date.today() - timedelta(days=30),
            date.today(),
        )
    for key, default in (
        ("comments_enabled", True),
        ("comments_per_post", 10),
        ("exclude_words", ""),
        ("ad_review_mode", "自动（广告/官方计入统计）"),
        ("websearch_eval_suffix", True),
        ("websearch_eval_suffix_toggle", True),
        ("kwopt_bilibili", False),
        ("kwopt_bilibili_toggle", False),
        ("kwopt_weibo", False),
        ("kwopt_weibo_toggle", False),
        ("kwopt_xiaohongshu", False),
        ("kwopt_xiaohongshu_toggle", False),
        ("official_domains", ""),
    ):
        if key not in at.session_state:
            at.session_state[key] = default


def wait_completed(at: AppTest, max_seconds: int = 180) -> None:
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        restore_stale_widget_states(at)
        try:
            at.run(timeout=60)
        except (KeyError, TypeError):
            continue
        if ss(at, "stage") == 5 and "bundle" in at.session_state:
            return
        time.sleep(0.3)
    raise AssertionError(
        f"任务未在 {max_seconds}s 内完成；stage={ss(at, 'stage')}"
    )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception, f"启动异常: {at.exception}"

    # 1.5 首次启动：《使用边界》确认门禁（临时状态目录 → 必现确认页）
    subs = [s.value for s in at.subheader]
    assert any("使用前请阅读并确认" in s for s in subs), "首次确认页未渲染"
    assert not any(b.label == "下一步 →" for b in at.button), "未确认不应进入向导"
    print("✓ 首次启动《使用边界》确认页拦截 通过")
    confirm_usage_boundary(at)
    assert not at.exception, f"确认后异常: {at.exception}"
    assert any(b.label == "下一步 →" for b in at.button), "确认后应进入向导"
    print("✓ 确认后进入向导 通过")

    print("✓ 应用启动正常（后台任务中心可见）")

    # 阶段0：手动关键词模式（不分类，无模块/维度）
    at.radio[0].set_value("手动关键词（不分类）").run()
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 1
    print("✓ 阶段0（品牌和维度，手动模式）通过")

    # 阶段1：关键词确认（手填）
    at.text_area[0].set_value("原神 抽卡\n原神 画质").run()
    keywords = at.session_state["keywords"]
    assert len(keywords) == 2 and keywords[0] == "原神 抽卡", keywords
    restore_stale_widget_states(at)
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 2
    print("✓ 阶段1（关键词）通过")

    # 阶段2：默认不再勾选演示渠道（2026-08-20 走查：demo 必须显式选择）
    assert at.session_state["channel_ids"] == [], at.session_state["channel_ids"]
    marks = [m.value for m in at.markdown if m.value]
    assert any("渠道风控安全" in m for m in marks), "侧边栏渠道风控安全区未渲染"
    ms = next(m for m in at.multiselect if m.label.startswith("采集渠道"))
    ms.set_value(["demo"]).run()  # 显式选择 demo，避免测试触发真实网络采集
    restore_stale_widget_states(at)
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 3
    restore_stale_widget_states(at)
    print("✓ 阶段2（默认无演示渠道 + 显式选择 + 侧边栏渠道风控安全区）通过")

    # 阶段3：提交后台任务
    demo_ck = next(c for c in at.checkbox if c.key == "demo_only_confirm_box")
    demo_ck.set_value(True).run()  # demo-only 提交守卫：先显式确认
    click_button(at, "🚀 启动分析")
    assert at.session_state["stage"] in (4, 5), at.session_state["stage"]
    task_id = at.session_state["task_id"]
    assert task_id, "未生成 task_id"
    print(f"✓ 阶段3 提交任务成功：{task_id}")

    # 阶段4/5：轮询后台执行至完成
    wait_completed(at)
    assert not at.exception, f"运行异常: {at.exception}"
    bundle = at.session_state["bundle"]
    assert bundle.summary["total_posts"] > 0
    assert bundle.summary["total_items"] > 0
    assert at.session_state["stage"] == 5
    print(
        f"✓ 后台任务完成：帖子 {bundle.summary['total_posts']} 条，"
        f"编码 {bundle.summary['total_items']} 条"
    )

    # 阶段6：结果页
    # 2.11 起结果页可能多出「导出需复核清单」按钮 → 改为 ≥3
    assert len(at.get("download_button")) >= 3, "缺少即时下载按钮（Excel/HTML/JSON）"
    assert any("Word" in b.label for b in at.button), "缺少 Word 按需生成按钮"
    # P1-1（2026-08-19）：结果页首屏改为 HTML 结论胶囊 + 指标卡（st.metric 不再使用）
    marks6 = " ".join(str(m.value) for m in at.markdown)
    assert "conclusion-card" in marks6, "结果页缺一句话结论胶囊"
    assert "stat-cards" in marks6, "结果页缺 4 指标卡"
    print("✓ 阶段6（结果与下载）通过：结论胶囊 + 4 指标卡 + Excel/HTML/JSON 下载")

    # 1.2 历史回看：任务详情 → 一键重跑
    task_id = at.session_state["task_id"]
    click_button_key(at, f"task_detail_{task_id}")
    click_button_key(at, f"task_rerun_{task_id}")
    new_id = ss(at, "task_id")
    assert new_id and new_id != task_id, "重跑应生成新任务"
    wait_completed(at)
    assert not at.exception
    bundle2 = at.session_state["bundle"]
    assert bundle2.summary["total_posts"] > 0
    print(f"✓ 1.2 一键重跑通过（{task_id} → {new_id}）")
    print("手动关键词模式全部通过 ✅")


def test_brand_mode() -> None:
    """品牌 + 模块模式：模块多选 → 维度实时预览 → 关键词（品牌名）→ 后台分析。"""
    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception
    confirm_usage_boundary(at)

    # 阶段0：品牌名 + 数字产品模块
    at.radio[0].set_value("按品牌分析（推荐）").run()
    at.text_input[0].set_value("恋与深空").run()
    at.multiselect[0].set_value(["content"]).run()
    assert at.session_state["domain_id"] == "modules_content", at.session_state.get("domain_id")
    selected = at.session_state["selected_dims"]
    assert len(selected) == 8, selected  # v2.0 模板：数字产品 8 维
    assert "content_quality" in selected
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 1
    print("✓ 品牌模式 阶段0（模块→维度预览→物化组合 schema）通过")

    # 阶段1：关键词只预填品牌名（不生成维度建议词）
    keywords = at.session_state["keywords"]
    assert keywords == ["恋与深空"], keywords
    # 关键词页不再含评测中心进阶入口（2026-08-18 已移至③渠道页）
    marks = [m.value for m in at.markdown if m.value]
    assert not any("关键词优化进阶" in m for m in marks), "进阶入口应已移至③渠道页"
    restore_stale_widget_states(at)
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 2
    print("✓ 品牌模式 阶段1（关键词=品牌名 + 评测中心入口）通过")

    # 阶段2（渠道页）：选 WebSearch 后出现关键词优化开关（2026-08-18 移入③）
    ms = next(m for m in at.multiselect if m.label.startswith("采集渠道"))
    ms.set_value(["demo", "websearch"]).run()
    exps = [e.label for e in at.expander]
    assert any("关键词优化" in e for e in exps), f"③页缺关键词优化折叠区: {exps}"
    assert ss(at, "websearch_eval_suffix", True) is True
    ms.set_value(["demo"]).run()  # 切回 demo，避免测试触发真实网络采集
    restore_stale_widget_states(at)
    print("✓ 品牌模式 阶段2（③渠道页含关键词优化折叠区）通过")

    # 阶段3/4：确认页并提交后台任务
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 3
    restore_stale_widget_states(at)
    demo_ck = next(c for c in at.checkbox if c.key == "demo_only_confirm_box")
    demo_ck.set_value(True).run()  # demo-only 提交守卫：先显式确认
    click_button(at, "🚀 启动分析")
    assert at.session_state["stage"] in (4, 5)
    wait_completed(at)
    assert not at.exception
    bundle = at.session_state["bundle"]
    assert bundle.summary["dimensions"], "维度统计为空"
    assert bundle.summary["total_posts"] > 0
    print(
        f"✓ 品牌模式 全流程通过：帖子 {bundle.summary['total_posts']} 条，"
        f"维度统计 {len(bundle.summary['dimensions'])} 个"
    )


def test_plan_restore_prefill() -> None:
    """P1 修复（2026-08-19 审查）：恢复上次计划应完整预填——渠道上限/
    自定义维度/复核模式/LLM/叙事/WebSearch 优化/关键词多行不再丢失。"""
    from datetime import date, timedelta

    from app.core import plans_store
    from app.core.models import Dimension
    from app.domains.composer import compose_schema

    # 本用例专测恢复链路：临时恢复复用区渲染（AppTest 对条件渲染控件的
    # state 限制通过下方模块/维度多选显式 set_value 绕开）
    _plans_store_mod.load_last_plan = _REAL_LOAD_LAST_PLAN
    _plans_store_mod.list_templates = _REAL_LIST_TEMPLATES
    try:
        plans_store.clear_all()
        dim_ids = [d.id for d in compose_schema(["content"]).dimensions][:2]
        today = date.today()
        plan = build_plan(
            subject="OPPO",
            domain_id="modules_content",
            dimension_ids=dim_ids,
            keyword_groups=[],
            manual_keywords=["OPPO", "OPPO 评价"],
            channel_ids=["demo", "websearch", "weibo"],
            date_start=today - timedelta(days=7),
            date_end=today,
            comments_enabled=False,
            comments_per_post=5,
            exclude_words=["手机"],
            llm_enabled=True,
            llm_base_url="https://api.deepseek.com",
            llm_model="deepseek-chat",
            narrative_enabled=True,
            relevance_check_enabled=True,
            custom_dimensions=[Dimension(
                id="custom_01", name="拍照", description="自定义维度（拍照）",
                source="custom", keywords=["拍照", "影像"], origin="custom")],
            channel_params={
                "demo": {"limit": 3},
                "websearch": {
                    "limit": 7, "official_domains": "oppo.com", "eval_suffix": "0",
                },
                "weibo": {"limit": 10, "queries": ["OPPO 评价"]},
            },
            review_enabled=True,
            exclude_ad_enabled=True,
        )
        plans_store.save_last_plan(plan)
        at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
        at.run()
        confirm_usage_boundary(at)
        # 品牌模式 + 模块多选；维度多选出现后显式 set_value（绕开 AppTest state 限制）
        at.radio[0].set_value("按品牌分析（推荐）").run()
        at.text_input[0].set_value("OPPO").run()
        at.multiselect[0].set_value(["content"]).run()
        dim_ms = next(
            m for m in at.multiselect if m.label.startswith("选择要分析的维度")
        )
        dim_ms.set_value(list(dim_ms.options)).run()
        click_button_key(at, "reuse_last")
        assert not at.exception, f"恢复后异常: {at.exception}"
        assert at.session_state["stage"] == 1
        ss_ = at.session_state
        assert ss_["subject"] == "OPPO"
        assert ss_["channel_ids"] == ["demo", "websearch", "weibo"]
        assert ss_["selected_dims"] == dim_ids
        assert ss_["keywords"] == ["OPPO", "OPPO 评价"]
        assert ss_["channel_limits"] == {"demo": 3, "websearch": 7, "weibo": 10}
        assert ss_["comments_enabled"] is False
        assert ss_["comments_per_post"] == 5
        assert ss_["exclude_words"] == "手机"
        assert ss_["review_enabled_opt"] is True
        assert ss_["exclude_ad_opt"] is True
        assert ss_["ad_review_mode"].startswith("人工复核")
        assert ss_["llm_enabled"] is True
        assert ss_["narrative_enabled"] is True
        assert ss_["custom_dim"] == "拍照：拍照,影像"
        assert ss_["websearch_eval_suffix"] is False
        assert ss_["kwopt_weibo"] is True
        assert ss_["official_domains"] == "oppo.com"
        assert ss_["selected_modules"] == ["content"]
        notices = [s.value for s in at.success]
        assert any("已恢复上次计划" in n for n in notices), notices
        # 9 修复：恢复后进入渠道页，应显示原渠道/上限/时间段，而非默认 demo
        click_button(at, "下一步 →")
        assert at.session_state["stage"] == 2
        ch_ms = next(m for m in at.multiselect if m.label.startswith("采集渠道"))
        assert sorted(ch_ms.value) == ["demo", "websearch", "weibo"], ch_ms.value
        lims = {
            n.key: n.value for n in at.number_input
            if n.key and n.key.startswith("limit_")
        }
        assert lims.get("limit_demo") == 3, lims
        assert lims.get("limit_websearch") == 7, lims
        assert lims.get("limit_weibo") == 10, lims
        print("✓ P1 修复：恢复上次计划完整预填（渠道上限/自定义维度/复核模式/LLM/叙事/优化开关）通过")
        print("✓ 9 修复：恢复后渠道页显示原渠道与每关键词上限 通过")
    finally:
        _plans_store_mod.load_last_plan = lambda: None
        _plans_store_mod.list_templates = lambda: []


def test_module_combo_dim_cap() -> None:
    """模块组合维度上限：实物+服务=11 维 → 阻塞下一步；裁剪到 ≤10 后放行。"""
    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception
    confirm_usage_boundary(at)

    at.radio[0].set_value("按品牌分析（推荐）").run()
    at.text_input[0].set_value("华润万家").run()
    at.multiselect[0].set_value(["physical", "service"]).run()
    assert at.session_state["domain_id"] == "modules_physical_service"
    dims = at.session_state["selected_dims"]
    assert len(dims) == 12, f"实物+服务组合应为 12 维: {len(dims)}"
    # 超限：下一步被阻塞（stage 保持 0）
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 0, "超 10 维不应放行"
    assert any("超过上限" in e.value for e in at.error), "缺少超限提示"
    print("✓ 模块组合 12 维被阻塞（强制 ≤10）")

    # 裁剪到 10 维后放行
    dim_ms = next(m for m in at.multiselect if m.label.startswith("选择要分析的维度"))
    dim_ms.set_value(dims[:10]).run()
    assert len(at.session_state["selected_dims"]) == 10
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 1
    print("✓ 模块组合裁剪到 10 维后放行")


def test_custom_dimension_ui() -> None:
    """2.8 UI 冒烟：① 自定义维度解析/校验拦截；侧边栏无相关性复核开关；确认页摘要。"""
    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception
    confirm_usage_boundary(at)

    # 侧边栏：LLM 相关性复核独立开关已移除（随 LLM 自动开启）
    toggles = [t.label for t in at.toggle]
    assert not any("相关性复核" in t for t in toggles), f"相关性复核开关应移除: {toggles}"

    # ① 品牌模式：非法自定义维度 → 校验错误 + 下一步拦截
    at.radio[0].set_value("按品牌分析（推荐）").run()
    at.text_input[0].set_value("华润万家").run()
    at.multiselect[0].set_value(["physical"]).run()
    at.text_area[0].set_value("没有冒号").run()
    assert at.session_state["custom_dim_errors"], "非法格式应报错"
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 0, "非法自定义维度不应放行"
    print("✓ 2.8 ① 页非法自定义维度拦截 通过")

    # 合法格式 → 解析成功并放行
    at.text_area[0].set_value("联名活动：联名,IP,周边\n物流体验：发货,快递,物流").run()
    cds = at.session_state["custom_dimensions"]
    assert len(cds) == 2 and cds[0].name == "联名活动" and cds[0].keywords[:2] == ["联名", "IP"]
    assert cds[1].name == "物流体验"
    assert not at.session_state["custom_dim_errors"]
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 1
    print("✓ 2.8 ① 页自定义维度解析 + 放行 通过")

    # ② 关键词 → ③ 渠道 → ④ 确认页：摘要含自定义维度行
    restore_stale_widget_states(at)
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 2
    restore_stale_widget_states(at)
    ms = next(m for m in at.multiselect if m.label.startswith("采集渠道"))
    ms.set_value(["demo"]).run()  # 显式选择 demo（默认不再预选）
    restore_stale_widget_states(at)
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 3
    tables = [str(t.value) for t in at.table]
    assert any("自定义维度" in t and "联名活动" in t for t in tables), "确认页缺自定义维度行"
    assert any("LLM 相关性复核" in t for t in tables), "确认页缺相关性复核行"
    print("✓ 2.8 确认页摘要（自定义维度行）通过")


def test_channel_strategy_ui() -> None:
    """其他渠道关键词优化（Phase 0）：④渠道页开关 → 确认页渠道查询数行。"""
    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception
    confirm_usage_boundary(at)

    at.radio[0].set_value("手动关键词（不分类）").run()
    click_button(at, "下一步 →")
    at.text_area[0].set_value("恋与深空 评价").run()
    restore_stale_widget_states(at)
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 2
    ms = next(m for m in at.multiselect if m.label.startswith("采集渠道"))
    ms.set_value(["weibo"]).run()
    # ④渠道页：关键词优化收进折叠区（2026-08-21）；AppTest 不支持展开折叠区，
    # 直接验证折叠区存在 + 微博策略键生效（B站/小红书不展示开关，2026-08-19）
    exps = [e.label for e in at.expander]
    assert any("关键词优化" in e for e in exps), f"③页缺关键词优化折叠区: {exps}"
    at.session_state["kwopt_weibo_toggle"] = True
    at.session_state["kwopt_weibo"] = True
    assert ss(at, "kwopt_weibo") is True
    restore_stale_widget_states(at)
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 3
    tables = [str(t.value) for t in at.table]
    # 恋与深空命中微博策略 5 词 → 展开为 5 个查询串
    assert any("渠道查询数（策略展开）" in t and "weibo 5" in t for t in tables), \
        "确认页缺渠道查询数行"
    print("✓ 其他渠道关键词优化：折叠区 + 确认页渠道查询数 通过")


def test_need_review_refresh_ui() -> None:
    """2.11 方案 A：结果页需复核强提示 + 一键确认并刷新报告（summary 落盘）。"""
    time.sleep(1.0)  # 等 worker 完成注册（避免空库首连竞态锁）
    plan = build_plan(
        subject="复核冒烟", domain_id=None, dimension_ids=[],
        keyword_groups=[], manual_keywords=["复核 评价"],
        channel_ids=["demo"], date_start=None, date_end=None,
        per_keyword_limit=5, comments_enabled=False,
        comments_per_post=0, llm_enabled=False, channel_params={},
    )
    tid = jobs.submit_task(plan)
    deadline = time.time() + 120
    t = None
    while time.time() < deadline:
        t = jobs.get_task(tid)
        if t and t["status"] == jobs.STATUS_COMPLETED:
            break
        time.sleep(0.2)
    assert t and t["status"] == jobs.STATUS_COMPLETED, (t or {}).get("error")
    # 注入 2 条需复核标记（模拟难例），再打开结果页
    rp = Path(t["output_dir"]) / "result.json"
    data = json.loads(rp.read_text(encoding="utf-8"))
    for it in data.get("coded_items", [])[:2]:
        it["need_review"] = True
        it["need_review_reason"] = "低置信(conf=0.30)"
        it["reviewed_by"] = ""
    rp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception
    confirm_usage_boundary(at)
    click_button_key(at, f"task_view_{tid}")
    assert at.session_state["stage"] == 5
    warns = [str(w.value) for w in at.warning]
    assert any("需复核样本未确认" in w for w in warns), f"缺需复核强提示: {warns}"
    # 6/7（2026-08-19）：移除"一键全部/完成复核并刷新"按钮，改为逐条确认，
    # 最后一条自动重算报告（校验"剩 1 条"问题已修）
    bundle0 = at.session_state["bundle"]
    n_before = sum(
        1 for it in bundle0.coded_items
        if it.need_review and not it.reviewed_by
    )
    assert n_before >= 2, f"需复核样本应 ≥2（实际 {n_before}）"
    # 逐条确认：每次点击列表第一条（确认后重载，剩余列表前移），
    # 最后一条确认时自动重算报告
    for _ in range(n_before):
        click_button_key(at, f"nr_pos_{tid}_0")
        assert not at.exception, [str(e) for e in at.exception]
    bundle = at.session_state["bundle"]
    unreviewed = [
        it for it in bundle.coded_items
        if it.need_review and not it.reviewed_by
    ]
    assert not unreviewed, "最后一条确认后不应有未复核样本"
    assert bundle.summary.get("consumer_voice"), "刷新后 summary 应含 consumer_voice"
    data2 = json.loads(rp.read_text(encoding="utf-8"))
    assert "consumer_voice" in data2.get("summary", {}), "result.json 未落盘新 summary"
    print("✓ 结果页需复核强提示 + 逐条确认自动刷新报告 通过")


def test_websearch_confirmation_page() -> None:
    """回归（2026-08-14）：选 WebSearch 渠道后确认页不再 NameError。

    commit 22454ed 把「WebSearch 关键词总量」行插入 summary_rows，
    但 ws_total 赋值在 st.table 之后 → 选 WebSearch 渠道进入确认页必现
    NameError: name 'ws_total' is not defined。本用例守护确认页正常渲染。
    """
    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception
    confirm_usage_boundary(at)

    # 手动关键词模式 → 阶段1（关键词）→ 阶段2（渠道选择）
    at.radio[0].set_value("手动关键词（不分类）").run()
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 1
    at.text_area[0].set_value("华润万家 评价\n华润万家 服务").run()
    restore_stale_widget_states(at)
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 2

    # 勾选 WebSearch 渠道（multiselect 的 option 即渠道 id）
    ms = next(m for m in at.multiselect if m.label.startswith("采集渠道"))
    ms.set_value(["websearch"]).run()
    assert at.session_state["channel_ids"] == ["websearch"]

    # 进入确认页：修复前此处抛 NameError（红屏）
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 3
    assert not at.exception, f"确认页异常: {at.exception}"
    restore_stale_widget_states(at)

    # 摘要表应包含「WebSearch 关键词总量」行（2 关键词 × 1 渠道 = 2 次查询）
    tables = [str(t.value) for t in at.table]
    assert any("WebSearch 关键词总量" in t for t in tables), "摘要缺少关键词总量行"
    assert any("2 次查询" in t for t in tables), "关键词总量数值不正确"
    print("✓ 回归：WebSearch 渠道确认页正常渲染（ws_total 不再 NameError）")


def test_failed_task_error_view() -> None:
    """1.3 错误排查：注入 failed 任务 → 详情页展示建议区。"""
    plan = build_plan(
        subject="失败冒烟",
        domain_id=None,
        dimension_ids=[],
        keyword_groups=[],
        manual_keywords=["失败 评价"],
        channel_ids=["demo"],
        date_start=None,
        date_end=None,
        per_keyword_limit=5,
        comments_enabled=False,
        comments_per_post=0,
        llm_enabled=False,
        channel_params={},
    )
    tid = jobs.submit_task(plan)
    jobs.claim_next_task("ui-fail-worker")
    jobs.fail_task(
        tid,
        "LLM 连接失败：网络连接失败或超时，请检查 Base URL、代理与网络",
        failed_step="llm",
    )
    jobs.log_event(tid, "ERROR", "llm", "failed", "执行异常：LLM 连接失败")

    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception
    confirm_usage_boundary(at)
    click_button_key(at, f"task_detail_{tid}")
    marks = [m.value for m in at.markdown if m.value]
    assert any("可能原因：LLM 服务连接失败" in m for m in marks), "LLM 建议未渲染"
    assert any("测试连接" in m for m in marks), "建议未包含操作指引"
    assert any("LLM 连接失败" in e.value for e in at.error), "失败原因未渲染"
    print("✓ 失败任务详情：失败原因 + 建议区渲染通过")


def test_review_ui_flow() -> None:
    """人工筛选 UI：审核页渲染 → 勾选帖子 → 继续分析 → 后台续跑完成。"""
    plan = build_plan(
        subject="筛选冒烟",
        domain_id=None,
        dimension_ids=[],
        keyword_groups=[],
        manual_keywords=["筛选 冒烟"],
        channel_ids=["demo"],
        date_start=None,
        date_end=None,
        per_keyword_limit=3,
        comments_enabled=True,
        comments_per_post=3,
        llm_enabled=False,
        review_enabled=True,
        channel_params={},
    )
    tid = jobs.submit_task(plan)
    # 与真实队列一致：交给后台 worker 线程执行，轮询至 REVIEWING
    # （不能手动抢单——worker 线程也在轮询，手动 claim 会与其竞态）
    deadline = time.time() + 60
    while time.time() < deadline:
        t = jobs.get_task(tid)
        if t and t["status"] == jobs.STATUS_REVIEWING:
            break
        time.sleep(0.2)
    assert t and t["status"] == jobs.STATUS_REVIEWING, (t or {}).get("error")
    snapshot = json.loads(Path(t["collection_path"]).read_text(encoding="utf-8"))
    target = snapshot["posts"][0]

    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception
    confirm_usage_boundary(at)
    click_button_key(at, f"task_view_{tid}")
    subs = [s.value for s in at.subheader]
    assert any("人工筛选" in s for s in subs), "审核页未渲染"

    key = "rv_" + hashlib.md5(target["url"].encode("utf-8")).hexdigest()[:12]
    cb = next(c for c in at.checkbox if c.key == key)
    # 统一面板：同一条帖子应同时有「不相关」与「广告/官方」两个判断
    ad_key = "rad_" + hashlib.md5(target["url"].encode("utf-8")).hexdigest()[:12]
    ad_cb = next(c for c in at.checkbox if c.key == ad_key)
    assert ad_cb.label == "广告/官方"
    cb.set_value(True).run()
    click_button_key(at, f"review_submit_{tid}")

    # 离开审核页后旧控件状态被 Streamlit 清理（AppTest 过期帧）：
    # 预置审核页全部显式 key，防止轮询 run() 读取旧节点时 KeyError。
    for k, v in {
        f"rv_platform_{tid}": "全部",
        f"rv_pending_{tid}": False,
        f"rv_llm_{tid}": False,
        f"rv_ad_suggested_{tid}": False,
    }.items():
        if k not in at.session_state:
            at.session_state[k] = v
    for p in snapshot["posts"]:
        for prefix in ("rv_", "rad_"):
            key = prefix + hashlib.md5(p["url"].encode()).hexdigest()[:12]
            if key not in at.session_state:
                at.session_state[key] = False
        for c in p.get("comments") or []:
            cid = str(c.get("id") or "")
            if cid:
                for prefix in ("rc_", "radc_"):
                    key = prefix + hashlib.md5(cid.encode()).hexdigest()[:12]
                    if key not in at.session_state:
                        at.session_state[key] = False

    t2 = jobs.get_task(tid)
    assert t2["status"] in (
        jobs.STATUS_PENDING, jobs.STATUS_RUNNING, jobs.STATUS_COMPLETED,
    ), t2.get("error")
    assert target["url"] in t2["excluded_urls"], "剔除清单未保存"
    wait_completed(at)
    assert not at.exception
    assert ss(at, "stage") == 5
    print("✓ 人工筛选 UI：审核页 → 勾选 → 续跑完成 通过")


def test_channel_diag_panel() -> None:
    """渠道诊断面板（体检×探针融合）：开始诊断 + WebSearch 深度探针冒烟。"""
    from app.channels import health
    from unittest import mock

    probe_ok = {
        "engine": "360", "status": "ok", "items": 3,
        "message": "正常（解析 3 条，质量通过）", "risk": [], "http": 200,
    }
    deep = [
        {"engine": "360", "status": "ok", "items": 3, "message": "正常",
         "risk": [], "http": 200},
        {"engine": "cn.bing", "status": "ok", "items": 2, "message": "正常",
         "risk": [], "http": 200},
        {"engine": "quark", "status": "error", "items": 0, "message": "请求失败",
         "risk": [], "http": 0},
    ]
    with mock.patch.object(health, "lightweight_probe", return_value=probe_ok), \
         mock.patch("app.channels.websearch.probe_engines", return_value=deep):
        at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
        at.run()
        assert not at.exception
        confirm_usage_boundary(at)
        at.radio[0].set_value("手动关键词（不分类）").run()
        click_button(at, "下一步 →")
        at.text_area[0].set_value("大疆 评价").run()
        restore_stale_widget_states(at)
        click_button(at, "下一步 →")
        assert at.session_state["stage"] == 2
        ms = next(m for m in at.multiselect if m.label.startswith("采集渠道"))
        ms.set_value([
            "demo", "websearch", "websearch_zhihu",
            "websearch_tieba", "websearch_taptap",
        ]).run()
        restore_stale_widget_states(at)
        # 方案 A：步骤④「广告/官方与人工复核」三选控件
        mode_radio = next(r for r in at.radio if r.key == "ad_review_mode")
        assert len(mode_radio.options) == 3, "三选控件应有 3 个档位"
        assert mode_radio.options[0] == "自动（广告/官方计入统计）"
        click_button(at, "开始诊断")
        assert not at.exception, f"诊断面板异常: {at.exception}"
        tables = [str(t.value) for t in at.table]
        assert any("演示" in t and "可用" in t for t in tables)
        assert any("WebSearch" in t and "可用" in t for t in tables)
        # 深度探针：WebSearch 组只显示 1 个按钮（多子渠道不重复）→ 三引擎表
        probe_btns = [b for b in at.button if "深度探针" in b.label]
        assert len(probe_btns) == 1, f"深度探针按钮应只有 1 个，实际 {len(probe_btns)}"
        click_button_key(at, "diag_probe_websearch")
        assert not at.exception, f"深度探针异常: {at.exception}"
        tables2 = [str(t.value) for t in at.table]
        assert any("引擎" in t and "360" in t and "quark" in t for t in tables2)
    print("✓ 渠道诊断面板（开始诊断 + WebSearch 深度探针）冒烟 通过")


def test_ad_wizard_smoke() -> None:
    """广告/官方内容：向导开关（默认计入）与确认页摘要行。"""
    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception
    confirm_usage_boundary(at)
    at.radio[0].set_value("手动关键词（不分类）").run()
    click_button(at, "下一步 →")
    at.text_area[0].set_value("大疆 评价").run()
    restore_stale_widget_states(at)
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 2
    ms = next(m for m in at.multiselect if m.label.startswith("采集渠道"))
    ms.set_value(["demo"]).run()
    mode_radio = next(r for r in at.radio if r.key == "ad_review_mode")
    assert len(mode_radio.options) == 3, "缺少广告/官方与人工复核三选控件"
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 3
    tables = [str(t.value) for t in at.table]
    assert any("广告/官方与人工复核" in t and "自动（广告计入统计）" in t for t in tables)
    assert not at.exception, f"确认页异常: {at.exception}"
    print("✓ 广告/官方内容向导开关 + 确认页摘要 冒烟 通过")


def test_config_status_center() -> None:
    """F-011：配置中心状态判定（未配置/已配置脱敏/env 命中/微博/Node）。"""
    import app.core.config_status as cs
    from app.core import secrets as _sec

    # 未配置
    st1 = cs.llm_status()
    assert st1["has_key"] is False and "未配置" in st1["text"], st1
    # 已配置 → 尾号脱敏（不显示明文）
    _sec.save_api_key("sk-test1234")
    try:
        st2 = cs.llm_status()
        assert st2["has_key"] is True and "尾号 ****1234" in st2["text"], st2
        assert "sk-test1234" not in st2["text"], "泄露明文 Key"
    finally:
        _sec.clear_api_key()
    # env 命中不误报未配置
    os.environ["OPENAI_API_KEY"] = "sk-env-xyz"
    try:
        st3 = cs.llm_status()
        assert st3["has_key"] is True and "环境变量" in st3["text"], st3
    finally:
        os.environ.pop("OPENAI_API_KEY", None)
    # 微博
    w1 = cs.weibo_status()
    assert w1["has_key"] is False, w1
    _sec.save_cookie("weibo", "SUB=abc")
    try:
        w2 = cs.weibo_status()
        assert w2["has_key"] is True, w2
    finally:
        _sec.clear_cookie("weibo")
    # Node/opencli 形状（不依赖真实安装）
    n1 = cs.node_status()
    assert "has_key" in n1 and "text" in n1
    o1 = cs.opencli_status()
    assert "has_key" in o1 and "text" in o1
    print("✓ 配置中心状态判定（未配置/脱敏/env/微博/Node）通过")


def test_weibo_cookie_block_ui() -> None:
    """F-011：确认页微博无 Cookie 阻断 + 逃生口勾选放行。"""
    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception
    confirm_usage_boundary(at)
    at.radio[0].set_value("手动关键词（不分类）").run()
    click_button(at, "下一步 →")
    at.text_area[0].set_value("测试 评价").run()
    restore_stale_widget_states(at)
    click_button(at, "下一步 →")
    ms = next(m for m in at.multiselect if m.label.startswith("采集渠道"))
    ms.set_value(["weibo"]).run()
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 3
    errs = [e.value for e in at.error]
    assert any("微博" in e and "Cookie" in e for e in errs), f"缺少微博阻断 error: {errs}"
    cb = [c for c in at.checkbox if "我了解该渠道会失败" in c.label]
    assert cb, "缺少微博逃生口勾选"
    # 不勾选时点启动 → 仍被拦截（stage 保持 3）
    start = [b for b in at.button if b.label == "🚀 启动分析"]
    assert start, "缺少启动按钮"
    start[0].click().run()
    assert at.session_state["stage"] == 3, "未勾选逃生口不应放行"
    print("✓ 确认页微博无 Cookie 阻断 + 逃生口 通过")


def test_node_opencli_install_migration() -> None:
    """F-014：安装入口迁移到侧边栏配置中心；③页只留引导按钮。"""
    from app.core.config_status import node_status, opencli_status

    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception
    confirm_usage_boundary(at)
    at.radio[0].set_value("手动关键词（不分类）").run()
    click_button(at, "下一步 →")
    at.text_area[0].set_value("测试 评价").run()
    restore_stale_widget_states(at)
    click_button(at, "下一步 →")
    ms = next(m for m in at.multiselect if m.label.startswith("采集渠道"))
    ms.set_value(["xiaohongshu"]).run()
    # ③页：不再渲染旧安装按钮（install_node_btn / install_opencli_btn 已迁移）
    assert not any(
        b.key in ("install_node_btn", "install_opencli_btn") for b in at.button
    ), "③页不应保留安装按钮"
    _n = node_status()
    _o = opencli_status()
    guide = [b for b in at.button if "去左侧" in b.label and "配置中心" in b.label]
    if not (_n["has_key"] and _o["has_key"]):
        assert guide, "opencli 未就绪时应有引导按钮"
        guide[0].click().run()
        assert at.session_state.get("cfg_center_expander") is True, "引导按钮应展开配置中心"
        # 配置中心安装面板：按状态分层给出对应按钮
        if not _n["has_key"]:
            assert any(
                b.key == "cfg_install_node_btn" for b in at.button
            ), "Node 未装应显示「安装 Node.js」按钮"
        else:
            assert any(
                b.key == "cfg_install_opencli_btn" for b in at.button
            ), "Node 已装 opencli 未装应显示「一键安装 opencli」按钮"
    else:
        assert not guide, "已就绪时不应显示引导按钮"
        assert not any(
            b.key in ("cfg_install_node_btn", "cfg_install_opencli_btn") for b in at.button
        ), "Node/opencli 都就绪时不应显示安装按钮"
    print("✓ F-014 安装入口迁移：③页无安装按钮 + 引导/配置中心面板分层 通过")


if __name__ == "__main__":
    _WORKER.start()
    try:
        main()
        test_brand_mode()
        test_plan_restore_prefill()
        test_module_combo_dim_cap()
        test_custom_dimension_ui()
        test_channel_strategy_ui()
        test_need_review_refresh_ui()
        test_websearch_confirmation_page()
        test_failed_task_error_view()
        test_review_ui_flow()
        test_channel_diag_panel()
        test_ad_wizard_smoke()
        test_config_status_center()
        test_weibo_cookie_block_ui()
        test_node_opencli_install_migration()
    finally:
        _STOP.set()
        _WORKER.join(timeout=5)
