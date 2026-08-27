"""人工相关性筛选（两阶段：采集暂停 → 筛选 → 续跑）单元测试。

运行：python tests/test_review.py
覆盖：pipeline 拆分等价性、worker 阶段1 暂停+快照、筛选续跑（帖子剔除/评论剔除/级联）、
      筛选中取消、save_review 守卫。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import worker  # noqa: E402
from app.core import jobs  # noqa: E402
from app.core.models import ReportBundle  # noqa: E402
from app.core.pipeline import TaskRunner  # noqa: E402
from app.core.planner import build_plan  # noqa: E402

_COUNTER = 0


def fresh_db() -> None:
    global _COUNTER
    _COUNTER += 1
    os.environ["SMS_DB_PATH"] = str(
        Path(tempfile.mkdtemp(prefix=f"sms_review_{_COUNTER}_")) / "test.db"
    )


def _plan(*, review: bool = True) -> object:
    return build_plan(
        subject="筛选自测",
        domain_id=None,
        dimension_ids=[],
        keyword_groups=[],
        manual_keywords=["筛选 评价"],
        channel_ids=["demo"],
        date_start=None,
        date_end=None,
        per_keyword_limit=3,
        comments_enabled=True,
        comments_per_post=3,
        llm_enabled=False,
        review_enabled=review,
        channel_params={},
    )


def _reports_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="sms_review_out_"))
    worker.REPORTS_DIR = root
    return root


def _run_to_reviewing(tid: str) -> dict:
    task = jobs.claim_next_task("review-worker")
    worker.run_task(task, "review-worker")
    t = jobs.get_task(tid)
    assert t["status"] == jobs.STATUS_REVIEWING, t.get("error")
    assert t["collection_path"] and Path(t["collection_path"]).exists()
    return t


def test_llm_rebuild_keeps_llm_outputs() -> None:
    """F-038：LLM 任务复核重建保留 topics/findings/conclusion_text（不清空 LLM 产出）。"""
    from unittest import mock

    import streamlit as st

    from app import demo_report
    from app.ui.results import rebuild_report_after_review

    bundle = demo_report.build_demo_bundle()  # LLM 模式：有 topics/conclusion_text/findings
    assert bundle.insight_mode == "llm"
    assert bundle.summary.get("topics"), "fixture 应有 LLM topics"
    assert bundle.conclusion_text and bundle.findings, "fixture 应有结论层"
    out = Path(tempfile.mkdtemp(prefix="sms_rebuild_llm_"))
    fake_task = {"output_dir": str(out), "id": "fake"}
    with mock.patch.object(jobs, "get_task", return_value=fake_task), \
         mock.patch.object(
             st,
             "session_state",
             {"_sidebar_api_key": "", "_sidebar_base_url": "", "_sidebar_model_name": ""},
         ):
        res = rebuild_report_after_review("fake", bundle)
    assert res, "复核重建应成功"
    nb, _ = res
    assert nb.insight_mode == "review_refresh", nb.insight_mode
    assert nb.summary.get("topics"), "复核重建后 LLM topics 应保留"
    assert nb.findings, "复核重建后结构化 findings 应保留"
    assert nb.conclusion_text, "复核重建后 conclusion_text 应保留"
    print("✓ F-038 LLM 复核重建保留主题/结论层 通过")


def test_pipeline_split_equivalence() -> None:
    fresh_db()
    plan = _plan(review=False)
    res = TaskRunner(plan).collect_and_clean(review_mode=True)
    assert res["posts"] and res["channel_results"] and res["tracker_snapshot"]
    bundle = TaskRunner(plan).code_and_report(
        res["posts"], res["channel_results"], res["warnings"], analyzer=res["analyzer"]
    )
    full = TaskRunner(plan).run()
    assert bundle.summary["total_posts"] == full.summary["total_posts"]
    assert bundle.summary["total_items"] == full.summary["total_items"]
    print("✓ pipeline 拆分与 run() 等价 通过")


def test_worker_review_pause_and_resume() -> None:
    fresh_db()
    _reports_root()
    tid = jobs.submit_task(_plan(review=True))
    t = _run_to_reviewing(tid)
    snapshot = json.loads(Path(t["collection_path"]).read_text(encoding="utf-8"))
    posts = snapshot["posts"]
    assert posts and t["status"] == jobs.STATUS_REVIEWING
    keep_post = posts[0]
    drop_post = posts[1]
    cid = keep_post["comments"][0]["id"] if keep_post["comments"] else None
    excluded_cids = [cid] if cid else []
    assert jobs.save_review(tid, [drop_post["url"]], excluded_cids)
    assert jobs.get_task(tid)["status"] == jobs.STATUS_PENDING

    task = jobs.claim_next_task("review-worker")
    worker.run_task(task, "review-worker")
    done = jobs.get_task(tid)
    assert done["status"] == jobs.STATUS_COMPLETED, done.get("error")
    data = json.loads(
        (Path(done["output_dir"]) / "result.json").read_text(encoding="utf-8")
    )
    kept = [p for ch in data["channel_results"] for p in ch["posts"]]
    kept_urls = {p["url"] for p in kept}
    assert drop_post["url"] not in kept_urls, "被剔除帖子不应出现在结果"
    if cid:
        keep_in_result = next(p for p in kept if p["url"] == keep_post["url"])
        assert len(keep_in_result["comments"]) == len(keep_post["comments"]) - 1, (
            "被剔除评论不应出现在结果"
        )
    drops = [d for ch in data["channel_results"] for d in ch["dropped"]]
    assert any("人工筛选" in d["reason"] for d in drops), "人工筛除应进入丢弃明细"
    print("✓ worker 阶段1 暂停 + 筛选续跑（帖子/评论剔除） 通过")


def test_review_cascade() -> None:
    fresh_db()
    _reports_root()
    tid = jobs.submit_task(_plan(review=True))
    t = _run_to_reviewing(tid)
    snapshot = json.loads(Path(t["collection_path"]).read_text(encoding="utf-8"))
    target = next(p for p in snapshot["posts"] if p["comments"])
    # 只剔除帖子，评论不写进 excluded_comment_ids（级联）
    assert jobs.save_review(tid, [target["url"]], [])
    task = jobs.claim_next_task("review-worker")
    worker.run_task(task, "review-worker")
    done = jobs.get_task(tid)
    assert done["status"] == jobs.STATUS_COMPLETED
    assert jobs.get_task(tid)["excluded_comment_ids"] == []
    data = json.loads(
        (Path(done["output_dir"]) / "result.json").read_text(encoding="utf-8")
    )
    kept = [p for ch in data["channel_results"] for p in ch["posts"]]
    assert target["url"] not in {p["url"] for p in kept}, "被剔除帖子不应出现在结果"
    drops = [d for ch in data["channel_results"] for d in ch["dropped"]]
    assert any("评论随帖剔除" in d["reason"] for d in drops)
    print("✓ 帖子剔除 → 评论级联（不入 excluded_comment_ids） 通过")


def test_review_cancel_and_guard() -> None:
    fresh_db()
    _reports_root()
    tid = jobs.submit_task(_plan(review=True))
    _run_to_reviewing(tid)
    assert not jobs.save_review("not-exist", [], []), "不存在任务应拒绝"
    jobs.request_cancel(tid)
    t = jobs.get_task(tid)
    assert t["status"] == jobs.STATUS_CANCELLED
    assert "人工筛选中" in t["error"]
    assert not jobs.save_review(tid, [], []), "已取消任务应拒绝保存筛选"
    print("✓ 筛选中取消 + save_review 守卫 通过")


def test_worker_ad_flags_roundtrip() -> None:
    """广告/官方标记：人工复核 ad_urls/ad_comment_ids → 编码后 CodedItem.ad_flag。"""
    fresh_db()
    _reports_root()
    tid = jobs.submit_task(_plan(review=True))
    t = _run_to_reviewing(tid)
    snapshot = json.loads(Path(t["collection_path"]).read_text(encoding="utf-8"))
    posts = snapshot["posts"]
    target = posts[0]
    cid = target["comments"][0]["id"] if target["comments"] else None
    assert jobs.save_review(
        tid,
        [],
        [],
        ad_urls=[target["url"]],
        ad_comment_ids=[cid] if cid else [],
    )
    task = jobs.claim_next_task("review-worker")
    worker.run_task(task, "review-worker")
    done = jobs.get_task(tid)
    assert done["status"] == jobs.STATUS_COMPLETED, done.get("error")
    data = json.loads(
        (Path(done["output_dir"]) / "result.json").read_text(encoding="utf-8")
    )
    items = data["coded_items"]
    post_item = next(it for it in items if it["text_id"] == f"{target['url']}:post")
    assert post_item["ad_flag"] is True
    if cid:
        # 评论 text_id 以 url 开头；该帖下应有 ad_flag 评论
        assert any(
            it["text_id"].startswith(f"{target['url']}:comment") and it["ad_flag"]
            for it in items
        )
    assert (data["summary"].get("ads") or {}).get("count", 0) >= 1
    print("✓ 广告标记人工复核 → CodedItem.ad_flag + summary.ads 通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_llm_rebuild_keeps_llm_outputs()
    test_pipeline_split_equivalence()
    test_worker_review_pause_and_resume()
    test_review_cascade()
    test_review_cancel_and_guard()
    test_worker_ad_flags_roundtrip()
    print("人工相关性筛选测试全部通过 ✅")


if __name__ == "__main__":
    main()
