"""UX 5.1 复用与连续性：上次计划 + 命名模板（≤5）单元测试。

运行：python tests/test_plans_store.py
覆盖：上次计划保存/恢复、模板增删、上限 5、同名覆盖、敏感字段剥离。
数据库经 SMS_DB_PATH 隔离到临时目录。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sms_plans_test_"))
os.environ["SMS_DB_PATH"] = str(_TMP / "app.db")

from app.core import plans_store  # noqa: E402
from app.core.models import AnalysisPlan, ChannelConfig, KeywordGroup  # noqa: E402


def _sample_plan(**overrides) -> AnalysisPlan:
    return AnalysisPlan(
        subject=overrides.get("subject", "瑞幸"),
        domain_id=None,
        dimensions=[],
        custom_dimensions=[],
        keyword_groups=[
            KeywordGroup(dimension_id="manual", dimension_name="手动关键词", keywords=["瑞幸"])
        ],
        keywords=["瑞幸"],
        channels=[
            ChannelConfig(channel_id="demo", enabled=True, params={}),
            ChannelConfig(
                channel_id="weibo",
                enabled=True,
                params={"limit": 20, "cookie": "SUB=secret"},
            ),
        ],
        date_start=None,
        date_end=None,
        per_keyword_limit=50,
        comments_enabled=False,
        comments_per_post=0,
        llm_enabled=False,
        llm_base_url="",
        llm_model="",
        narrative_enabled=False,
        relevance_check_enabled=False,
        exclude_words=[],
        review_enabled=False,
        exclude_ad_enabled=False,
    )


def test_last_plan_roundtrip_and_strip() -> None:
    plans_store.clear_all()
    assert plans_store.load_last_plan() is None
    plans_store.save_last_plan(_sample_plan())
    last = plans_store.load_last_plan()
    assert last is not None
    assert last["subject"] == "瑞幸"
    assert last.get("_saved_at")
    # 敏感字段剥离：cookie 不落库
    raw = json.dumps(last, ensure_ascii=False)
    assert "SUB=secret" not in raw and "cookie" not in raw
    print("✓ 上次计划保存/恢复 + cookie 剥离 通过")


def test_template_crud_limit5() -> None:
    plans_store.clear_all()
    for i in range(1, 6):
        res = plans_store.save_template(f"模板{i}", _sample_plan(subject=f"品牌{i}"))
        assert res["ok"], res
    assert len(plans_store.list_templates()) == 5
    # 第 6 个被拒绝
    res6 = plans_store.save_template("模板6", _sample_plan(subject="品牌6"))
    assert not res6["ok"] and "最多" in res6["error"]
    assert len(plans_store.list_templates()) == 5
    # 同名覆盖不占新名额
    res_upd = plans_store.save_template("模板1", _sample_plan(subject="品牌1改"))
    assert res_upd["ok"] and res_upd["action"] == "updated"
    assert len(plans_store.list_templates()) == 5
    # 删除后可再加
    tid = plans_store.list_templates()[0]["id"]
    plans_store.delete_template(tid)
    assert len(plans_store.list_templates()) == 4
    res7 = plans_store.save_template("模板6", _sample_plan(subject="品牌6"))
    assert res7["ok"]
    assert len(plans_store.list_templates()) == 5
    print("✓ 模板增删 + 上限5 + 同名覆盖 通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_last_plan_roundtrip_and_strip()
    test_template_crud_limit5()
    print("全部计划复用测试通过 ✅")


if __name__ == "__main__":
    main()
