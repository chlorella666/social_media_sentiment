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
from app.core.planner import build_plan

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
    matches[0].click().run(timeout=timeout)


def click_button_key(at: AppTest, key: str, timeout: int = 60) -> None:
    matches = [b for b in at.button if b.key == key]
    assert matches, f"未找到按钮 key={key}；现有：{[b.key for b in at.button]}"
    matches[0].click().run(timeout=timeout)


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
    if "channel_ids" in at.session_state:
        for cid in at.session_state["channel_ids"]:
            key = f"limit_{cid}"
            if key not in at.session_state:
                at.session_state[key] = 10
            qkey = f"quota_limit_{cid}"
            if qkey not in at.session_state:
                at.session_state[qkey] = -1
    for key, default in (
        ("comments_enabled", True),
        ("comments_per_post", 20),
        ("exclude_words", ""),
        ("websearch_eval_suffix", True),
        ("official_domains", ""),
    ):
        if key not in at.session_state:
            at.session_state[key] = default


def wait_completed(at: AppTest, max_seconds: int = 180) -> None:
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        at.run(timeout=60)
        if ss(at, "stage") == 6 and "bundle" in at.session_state:
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

    # 阶段0：手动关键词模式
    at.radio[0].set_value("手动输入关键词").run()
    at.text_area[0].set_value("原神 抽卡\n原神 画质").run()
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 1
    print("✓ 阶段0（输入对象）通过")

    # 阶段1：无领域 → 下一步
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 2
    print("✓ 阶段1（维度）通过")

    # 阶段2：关键词确认
    keywords = at.session_state["keywords"]
    assert len(keywords) == 2 and keywords[0] == "原神 抽卡", keywords
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 3
    print("✓ 阶段2（关键词）通过")

    # 阶段3：默认演示渠道
    assert at.session_state["channel_ids"] == ["demo"]
    marks = [m.value for m in at.markdown if m.value]
    assert any("渠道安全" in m for m in marks), "渠道安全设置区未渲染"
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 4
    restore_stale_widget_states(at)
    print("✓ 阶段3（渠道/时间 + 渠道安全设置区）通过")

    # 阶段4：提交后台任务
    click_button(at, "🚀 启动分析")
    assert at.session_state["stage"] in (5, 6), at.session_state["stage"]
    task_id = at.session_state["task_id"]
    assert task_id, "未生成 task_id"
    print(f"✓ 阶段4 提交任务成功：{task_id}")

    # 阶段5：轮询后台执行至完成
    wait_completed(at)
    assert not at.exception, f"运行异常: {at.exception}"
    bundle = at.session_state["bundle"]
    assert bundle.summary["total_posts"] > 0
    assert bundle.summary["total_items"] > 0
    assert at.session_state["stage"] == 6
    print(
        f"✓ 后台任务完成：帖子 {bundle.summary['total_posts']} 条，"
        f"编码 {bundle.summary['total_items']} 条"
    )

    # 阶段6：结果页
    assert len(at.get("download_button")) == 3, "缺少即时下载按钮（Excel/HTML/JSON）"
    assert any("Word" in b.label for b in at.button), "缺少 Word 按需生成按钮"
    assert len(at.metric) == 5
    print("✓ 阶段6（结果与下载）通过：Excel / HTML / JSON 即时下载 + Word 按需生成")

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
    """品牌名 + 领域模式：维度确认 → 关键词生成 → 后台分析。"""
    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception
    confirm_usage_boundary(at)

    # 阶段0：品牌名 + 游戏领域
    at.radio[0].set_value("品牌名 + 领域（推荐）").run()
    at.text_input[0].set_value("恋与深空").run()
    at.selectbox[0].set_value("数字内容产品（如游戏）").run()
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 1
    print("✓ 品牌模式 阶段0 通过")

    # 阶段1：维度默认全选，直接下一步
    selected = at.session_state["selected_dims"]
    assert len(selected) == 7, selected
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 2
    print("✓ 品牌模式 阶段1（维度全选）通过")

    # 阶段2：按维度生成的关键词
    keywords = at.session_state["keywords"]
    assert len(keywords) >= 20, f"关键词过少: {len(keywords)}"
    assert keywords[0].startswith("恋与深空"), keywords[0]
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 3
    print(f"✓ 品牌模式 阶段2 通过（自动生成 {len(keywords)} 个关键词）")

    # 阶段3/4：默认演示渠道并提交后台任务
    click_button(at, "下一步 →")
    restore_stale_widget_states(at)
    click_button(at, "🚀 启动分析")
    assert at.session_state["stage"] in (5, 6)
    wait_completed(at)
    assert not at.exception
    bundle = at.session_state["bundle"]
    assert bundle.summary["dimensions"], "维度统计为空"
    assert bundle.summary["total_posts"] > 0
    print(
        f"✓ 品牌模式 全流程通过：帖子 {bundle.summary['total_posts']} 条，"
        f"维度统计 {len(bundle.summary['dimensions'])} 个"
    )


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

    # 手动关键词模式 → 阶段3（渠道选择）
    at.radio[0].set_value("手动输入关键词").run()
    at.text_area[0].set_value("华润万家 评价\n华润万家 服务").run()
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 1
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 2
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 3

    # 勾选 WebSearch 渠道（multiselect 的 option 即渠道 id）
    ms = next(m for m in at.multiselect if m.label.startswith("采集渠道"))
    ms.set_value(["websearch"]).run()
    assert at.session_state["channel_ids"] == ["websearch"]

    # 进入确认页：修复前此处抛 NameError（红屏）
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 4
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
    assert any("人工相关性筛选" in s for s in subs), "审核页未渲染"

    key = "rv_" + hashlib.md5(target["url"].encode("utf-8")).hexdigest()[:12]
    cb = next(c for c in at.checkbox if c.key == key)
    cb.set_value(True).run()
    click_button_key(at, f"review_submit_{tid}")

    t2 = jobs.get_task(tid)
    assert t2["status"] in (
        jobs.STATUS_PENDING, jobs.STATUS_RUNNING, jobs.STATUS_COMPLETED,
    ), t2.get("error")
    assert target["url"] in t2["excluded_urls"], "剔除清单未保存"
    wait_completed(at)
    assert not at.exception
    assert ss(at, "stage") == 6
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
        at.radio[0].set_value("手动输入关键词").run()
        at.text_area[0].set_value("大疆 评价").run()
        click_button(at, "下一步 →")
        click_button(at, "下一步 →")
        click_button(at, "下一步 →")
        assert at.session_state["stage"] == 3
        ms = next(m for m in at.multiselect if m.label.startswith("采集渠道"))
        ms.set_value(["demo", "websearch"]).run()
        click_button(at, "开始诊断")
        assert not at.exception, f"诊断面板异常: {at.exception}"
        tables = [str(t.value) for t in at.table]
        assert any("演示" in t and "可用" in t for t in tables)
        assert any("WebSearch" in t and "可用" in t for t in tables)
        # 深度探针：WebSearch 行内按钮 → 三引擎表
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
    at.radio[0].set_value("手动输入关键词").run()
    at.text_area[0].set_value("大疆 评价").run()
    click_button(at, "下一步 →")
    click_button(at, "下一步 →")
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 3
    ms = next(m for m in at.multiselect if m.label.startswith("采集渠道"))
    ms.set_value(["demo"]).run()
    toggles = [t.label for t in at.toggle]
    assert any("剔除广告/官方内容" in t for t in toggles), f"缺少广告开关: {toggles}"
    click_button(at, "下一步 →")
    assert at.session_state["stage"] == 4
    tables = [str(t.value) for t in at.table]
    assert any("广告/官方内容" in t and "计入" in t for t in tables)
    assert not at.exception, f"确认页异常: {at.exception}"
    print("✓ 广告/官方内容向导开关 + 确认页摘要 冒烟 通过")


if __name__ == "__main__":
    _WORKER.start()
    try:
        main()
        test_brand_mode()
        test_websearch_confirmation_page()
        test_failed_task_error_view()
        test_review_ui_flow()
        test_channel_diag_panel()
        test_ad_wizard_smoke()
    finally:
        _STOP.set()
        _WORKER.join(timeout=5)
