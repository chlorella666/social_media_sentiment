"""Streamlit 向导端到端冒烟测试（AppTest，无需浏览器）。

运行：python tests/test_ui_flow.py
覆盖：手动关键词模式 → 全部向导步骤 → 启动分析 → 结果页下载按钮。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception, f"启动异常: {at.exception}"
    print("✓ 应用启动正常")

    # 阶段0：手动关键词模式
    at.radio[0].set_value("手动输入关键词").run()
    at.text_area[0].set_value("原神 抽卡\n原神 画质").run()
    at.button[0].click().run()
    assert at.session_state["stage"] == 1, at.session_state["stage"]
    print("✓ 阶段0（输入对象）通过")

    # 阶段1：无领域 → 下一步
    at.button[1].click().run()
    assert at.session_state["stage"] == 2
    print("✓ 阶段1（维度）通过")

    # 阶段2：关键词确认
    keywords = at.session_state["keywords"]
    assert len(keywords) == 2 and keywords[0] == "原神 抽卡", keywords
    at.button[1].click().run()
    assert at.session_state["stage"] == 3
    print("✓ 阶段2（关键词）通过")

    # 阶段3：默认演示渠道
    assert at.session_state["channel_ids"] == ["demo"]
    at.button[1].click().run()
    assert at.session_state["stage"] == 4
    print("✓ 阶段3（渠道/时间）通过")

    # 阶段4：启动分析（同步执行管道）
    at.button[1].click().run(timeout=120)
    assert not at.exception, f"运行异常: {at.exception}"
    assert "bundle" in at.session_state, "分析结果未写入 session_state"
    bundle = at.session_state["bundle"]
    assert bundle.summary["total_posts"] > 0
    assert bundle.summary["total_items"] > 0
    assert at.session_state["stage"] == 5
    print(f"✓ 分析完成：帖子 {bundle.summary['total_posts']} 条，编码 {bundle.summary['total_items']} 条")

    # 阶段5：结果页
    assert len(at.get("download_button")) == 3, "缺少即时下载按钮（Excel/HTML/JSON）"
    assert any("Word" in b.label for b in at.button), "缺少 Word 按需生成按钮"
    assert len(at.metric) == 5
    print("✓ 阶段5（结果与下载）通过：Excel / HTML / JSON 即时下载 + Word 按需生成")
    print("全部 UI 冒烟测试通过 ✅")


def test_brand_mode() -> None:
    """品牌名 + 领域模式：维度确认 → 关键词生成 → 分析。"""
    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception

    # 阶段0：品牌名 + 游戏领域
    at.radio[0].set_value("品牌名 + 领域（推荐）").run()
    at.text_input[0].set_value("恋与深空").run()
    at.selectbox[0].set_value("游戏（game）").run()
    at.button[0].click().run()
    assert at.session_state["stage"] == 1
    print("✓ 品牌模式 阶段0 通过")

    # 阶段1：维度默认全选，直接下一步
    selected = at.session_state["selected_dims"]
    assert len(selected) == 7, selected
    at.button[1].click().run()
    assert at.session_state["stage"] == 2
    print("✓ 品牌模式 阶段1（维度全选）通过")

    # 阶段2：按维度生成的关键词
    keywords = at.session_state["keywords"]
    assert len(keywords) >= 20, f"关键词过少: {len(keywords)}"
    assert keywords[0].startswith("恋与深空"), keywords[0]
    at.button[1].click().run()
    assert at.session_state["stage"] == 3
    print(f"✓ 品牌模式 阶段2 通过（自动生成 {len(keywords)} 个关键词）")

    # 阶段3/4：默认演示渠道并启动
    at.button[1].click().run()
    at.button[1].click().run(timeout=120)
    assert not at.exception
    assert at.session_state["stage"] == 5
    bundle = at.session_state["bundle"]
    assert bundle.summary["dimensions"], "维度统计为空"
    assert bundle.summary["total_posts"] > 0
    print(
        f"✓ 品牌模式 全流程通过：帖子 {bundle.summary['total_posts']} 条，"
        f"维度统计 {len(bundle.summary['dimensions'])} 个"
    )


if __name__ == "__main__":
    main()
    test_brand_mode()
