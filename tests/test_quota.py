"""渠道安全与配额单元测试（1.4）。

运行：python tests/test_quota.py
覆盖：配额预扣/超限拦截/日重置/退款钳 0/实际回填、暂停恢复、指数退避（10→20→40→60）、
      手动解除冷却、is_ratelimit 判定、worker 渠道结算与预检路径。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import worker  # noqa: E402
from app.core import jobs  # noqa: E402
from app.core.errors import is_ratelimit  # noqa: E402
from app.core.models import ChannelResult  # noqa: E402
from app.core.planner import build_plan  # noqa: E402

_COUNTER = 0


def fresh_db() -> None:
    global _COUNTER
    _COUNTER += 1
    os.environ["SMS_DB_PATH"] = str(
        Path(tempfile.mkdtemp(prefix=f"sms_quota_{_COUNTER}_")) / "test.db"
    )


def test_quota_consume_and_block() -> None:
    fresh_db()
    st = jobs.channel_state("weibo")
    assert st["quota_limit"] == 200, st  # 默认上限
    assert jobs.check_channel_allowed("weibo", 50)[0]
    jobs.consume_quota("weibo", 150)
    assert jobs.check_channel_allowed("weibo", 50)[0]
    ok, reason = jobs.check_channel_allowed("weibo", 60)
    assert not ok and "配额不足" in reason, reason
    print("✓ 配额预扣与超限拦截 通过")


def test_quota_day_reset() -> None:
    fresh_db()
    jobs.consume_quota("weibo", 120)
    with sqlite3.connect(str(jobs.db_path())) as conn:
        conn.execute(
            "UPDATE channel_state SET date = ? WHERE channel_id = 'weibo'",
            ((datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),),
        )
    assert jobs.check_channel_allowed("weibo", 200)[0], "跨日应自动重置配额"
    assert jobs.channel_state("weibo")["quota_used"] == 0
    print("✓ 跨日配额重置 通过")


def test_refund_and_settle() -> None:
    fresh_db()
    jobs.consume_quota("weibo", 100)
    jobs.refund_quota("weibo", 100)
    assert jobs.channel_state("weibo")["quota_used"] == 0
    jobs.refund_quota("weibo", 50)  # 退款超过已扣 → 钳 0
    assert jobs.channel_state("weibo")["quota_used"] == 0
    jobs.consume_quota("weibo", 100)
    jobs.settle_quota("weibo", 100, actual=30)  # 回填实际：100-100+30
    assert jobs.channel_state("weibo")["quota_used"] == 30
    jobs.consume_quota("weibo", 20)
    jobs.settle_quota("weibo", 20, actual=0)
    assert jobs.channel_state("weibo")["quota_used"] == 30
    print("✓ 退款钳 0 与实际回填校正 通过")


def test_pause_resume() -> None:
    fresh_db()
    jobs.pause_channel("weibo")
    ok, reason = jobs.check_channel_allowed("weibo", 10)
    assert not ok and "暂停" in reason
    jobs.resume_channel("weibo")
    assert jobs.check_channel_allowed("weibo", 10)[0]
    print("✓ 暂停 / 恢复 通过")


def test_cooldown_backoff() -> None:
    fresh_db()
    m1, _ = jobs.set_cooldown("weibo", "风控：频率限制")
    m2, _ = jobs.set_cooldown("weibo", "风控：验证码")
    m3, _ = jobs.set_cooldown("weibo", "风控")
    m4, _ = jobs.set_cooldown("weibo", "风控")
    assert (m1, m2, m3, m4) == (10, 20, 40, 60), (m1, m2, m3, m4)
    assert not jobs.check_channel_allowed("weibo", 1)[0], "冷却中应不可用"
    # 冷却到期 → 自动解除并清零退避等级
    with sqlite3.connect(str(jobs.db_path())) as conn:
        conn.execute(
            "UPDATE channel_state SET cool_until = ? WHERE channel_id = 'weibo'",
            ((datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"),),
        )
    assert jobs.check_channel_allowed("weibo", 1)[0]
    assert jobs.channel_state("weibo")["backoff_level"] == 0
    print("✓ 指数退避（10→20→40→60）与到期自动解除 通过")


def test_clear_cooldown_manual() -> None:
    fresh_db()
    jobs.set_cooldown("weibo", "误判")
    jobs.clear_cooldown("weibo")
    assert jobs.check_channel_allowed("weibo", 10)[0]
    assert jobs.channel_state("weibo")["cool_until"] is None
    print("✓ 手动解除冷却（误判兜底） 通过")


def test_is_ratelimit() -> None:
    for text in ("风控：频率限制", "验证码频繁，请稍后", "429 Too Many Requests"):
        assert is_ratelimit(text), text
    for text in ("微博 Cookie 无效或已过期", "LLM 连接失败：网络超时"):
        assert not is_ratelimit(text), text
    print("✓ is_ratelimit 风控信号判定 通过")


def test_settle_channel_unit() -> None:
    fresh_db()
    # ok → 回填实际
    jobs.consume_quota("weibo", 50)
    worker._settle_channel("weibo", 50, ChannelResult(channel_id="weibo", ok=True, posts=[]))
    assert jobs.channel_state("weibo")["quota_used"] == 0
    # 风控失败 → 冷却 + 退款
    jobs.consume_quota("weibo", 30)
    ev = worker._settle_channel(
        "weibo", 30,
        ChannelResult(channel_id="weibo", ok=False, error="风控：频率限制"),
    )
    assert ev and ev["event"] == "cooldown" and "冷却" in ev["message"]
    assert jobs.channel_state("weibo")["quota_used"] == 0
    assert jobs.channel_state("weibo")["cool_until"] is not None
    # 其他失败（Cookie）→ 仅退款，不冷却
    jobs.clear_cooldown("weibo")
    jobs.consume_quota("weibo", 20)
    ev2 = worker._settle_channel(
        "weibo", 20,
        ChannelResult(channel_id="weibo", ok=False, error="微博 Cookie 无效或已过期"),
    )
    assert ev2 is None
    assert jobs.channel_state("weibo")["quota_used"] == 0
    assert jobs.channel_state("weibo")["cool_until"] is None
    print("✓ 渠道结算：回填 / 风控冷却+退款 / 其他失败仅退款 通过")


def _plan(channels: list[str]) -> object:
    return build_plan(
        subject="配额自测",
        domain_id=None,
        dimension_ids=[],
        keyword_groups=[],
        manual_keywords=["配额 评价"],
        channel_ids=channels,
        date_start=None,
        date_end=None,
        per_keyword_limit=5,
        comments_enabled=False,
        comments_per_post=0,
        llm_enabled=False,
        channel_params={},
    )


def test_worker_skips_paused_channel() -> None:
    fresh_db()
    out_root = Path(tempfile.mkdtemp(prefix="sms_quota_out_"))
    worker.REPORTS_DIR = out_root
    jobs.pause_channel("weibo")
    tid = jobs.submit_task(_plan(["demo", "weibo"]))
    task = jobs.claim_next_task("quota-worker")
    worker.run_task(task, "quota-worker")
    t = jobs.get_task(tid)
    assert t["status"] == jobs.STATUS_COMPLETED, t.get("error")
    assert t["result_summary"]["total_posts"] > 0, "demo 渠道应正常产出"
    logs = jobs.list_task_logs(tid)
    assert any(l["event"] == "channel_skipped" and "weibo" in l["message"] for l in logs)
    assert jobs.channel_state("weibo")["quota_used"] == 0, "被跳过渠道不应预扣配额"
    print("✓ 暂停渠道被跳过，任务正常完成（不空跑） 通过")


def test_worker_fails_when_all_channels_unavailable() -> None:
    fresh_db()
    jobs.pause_channel("weibo")
    tid = jobs.submit_task(_plan(["weibo"]))
    task = jobs.claim_next_task("quota-worker")
    worker.run_task(task, "quota-worker")
    t = jobs.get_task(tid)
    assert t["status"] == jobs.STATUS_FAILED
    assert t["failed_step"] == "collect"
    assert "所选渠道均不可用" in t["error"]
    print("✓ 全部渠道不可用 → 任务失败并给出原因（不空跑） 通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_quota_consume_and_block()
    test_quota_day_reset()
    test_refund_and_settle()
    test_pause_resume()
    test_cooldown_backoff()
    test_clear_cooldown_manual()
    test_is_ratelimit()
    test_settle_channel_unit()
    test_worker_skips_paused_channel()
    test_worker_fails_when_all_channels_unavailable()
    print("渠道安全与配额测试全部通过 ✅")


if __name__ == "__main__":
    main()
