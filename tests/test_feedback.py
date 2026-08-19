"""UX 5.6 反馈闭环：SQLite 反馈表单元测试。

运行：python tests/test_feedback.py
覆盖：新增/列表/状态流转/删除/清空、非法判定拦截。
数据库经 SMS_DB_PATH 隔离到临时目录。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sms_feedback_test_"))
os.environ["SMS_DB_PATH"] = str(_TMP / "app.db")

from app.core import feedback  # noqa: E402


def test_add_and_list() -> None:
    feedback.clear_all()
    fid = feedback.add_feedback(
        task_id="t1",
        text_id="c_1",
        text_snippet="这个产品续航太差了",
        model_sentiment="negative",
        user_sentiment="positive",
        reason="反讽：说的是续航差但实际是夸",
    )
    assert fid > 0
    rows = feedback.list_feedback()
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "open" and r["user_sentiment"] == "positive"
    assert r["text_snippet"] == "这个产品续航太差了"
    assert r["created_at"]
    # 非法判定拦截
    try:
        feedback.add_feedback(
            task_id="t1", text_id="c_2", text_snippet="x",
            model_sentiment="negative", user_sentiment="unknown",
        )
        assert False, "应拒绝非法用户判定"
    except ValueError:
        pass
    print("✓ 反馈新增/列表/非法判定拦截 通过")


def test_status_and_clear() -> None:
    feedback.clear_all()
    fid = feedback.add_feedback(
        task_id="t2", text_id="c_9", text_snippet="还行",
        model_sentiment="neutral", user_sentiment="negative", reason="",
    )
    feedback.mark_processed(fid)
    assert feedback.list_feedback(status="open") == []
    assert len(feedback.list_feedback(status="processed")) == 1
    feedback.delete_feedback(fid)
    assert feedback.list_feedback() == []
    feedback.add_feedback(
        task_id="t3", text_id="c_10", text_snippet="s",
        model_sentiment="positive", user_sentiment="negative",
    )
    assert feedback.clear_all() == 1
    assert feedback.list_feedback() == []
    print("✓ 反馈状态流转 / 删除 / 清空 通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_add_and_list()
    test_status_and_clear()
    print("全部反馈闭环测试通过 ✅")


if __name__ == "__main__":
    main()
