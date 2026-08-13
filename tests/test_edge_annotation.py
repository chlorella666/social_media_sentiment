# -*- coding: utf-8 -*-
"""边界样本专项集标注配套库测试（2.3 阶段 1）。

运行：python tests/test_edge_annotation.py
覆盖：Cohen's kappa（含已知值）、取值校验、CSV/XLSX 解析、双 AI 合并比对、
      复核表抽样（配额/去重/种子可复现）、定版规则（人工优先 + 未决分歧入争议集）。
"""

from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tests import edge_annotation as ea  # noqa: E402


def test_kappa_perfect() -> None:
    assert ea.cohen_kappa(["positive", "negative"], ["positive", "negative"]) == 1.0
    assert ea.cohen_kappa(["positive", "negative", "neutral"], ["positive", "negative", "neutral"]) == 1.0
    print("✓ kappa 完全一致 = 1.0 通过")


def test_kappa_known_value() -> None:
    # 经典 2x2 例：两 rater 45 条，一致 30（20+10），期望一致 0.5185 → kappa≈0.3077
    a = ["positive"] * 30 + ["negative"] * 15
    b = (["positive"] * 20 + ["negative"] * 10 + ["positive"] * 5 + ["negative"] * 10)
    k = ea.cohen_kappa(a, b)
    assert k is not None and abs(k - 0.3077) < 0.001, k
    assert ea.cohen_kappa([], []) is None
    print("✓ kappa 已知值 0.3077 通过")


def _row(tid: str, sentiment: str, subset: str = "irony", relevant: str = "yes",
          intensity: str = "3", dims: dict | None = None, flags: list[str] | None = None) -> dict:
    return {
        "text_id": tid, "text": "测试文本", "platform": "weibo", "domain": "game",
        "brand": "", "keyword": "", "kind": "评论", "is_reply": "",
        "subset_cn": ea.SUB_EN_TO_CN[subset], "sentiment": sentiment,
        "intensity": intensity, "dims": dims or {}, "flags": flags or [],
        "relevant": relevant, "remark": "", "annotator": "tester",
        "annotated_at": "2026-08-13", "url": "", "time": "", "likes": "",
        "batch": "test",
    }


def test_validate_rows() -> None:
    good = _row("t1", "positive")
    assert ea.validate_rows([good]) == []
    bad = [
        _row("t1", "goodbad"),
        _row("t2", "positive", intensity=""),
        _row("t3", "neutral", intensity="9"),
        _row("t4", "neutral", relevant="maybe"),
        _row("t5", "negative", dims={"价格价值": "awful"}),
        _row("t6", "negative", flags=["外星语"]),
    ]
    errs = ea.validate_rows(bad)
    assert len(errs) == 6, errs
    print("✓ 取值校验 6 类错误全检出 通过")


def test_parse_csv(tmp_path: Path) -> None:
    headers = [
        "序号", "text_id", "平台", "领域", "品牌/主题", "关键词", "文本类型", "是否楼中楼",
        "原文", "帖子标题", "作者", "链接", "发布时间", "点赞数", "采集批次",
        "情感(整条)", "强度(1-5)", "子集", "角色偏好", "剧情评价", "氪金体验",
        "美术反馈", "玩法体验", "运营与社区", "Bug/技术",
        "语言现象", "是否相关", "备注", "标注人", "标注日期",
    ]
    p = tmp_path / "annot.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerow([
            1, "e1", "weibo", "game", "恋与深空", "恋与深空", "评论", "",
            "画面好看但价格劝退", "", "作者A", "https://x/1", "2026-08-01", "5",
            "batch1", "Negative", "4", "反讽", "positive", "", "", "", "", "", "",
            "反讽、emoji主导", "是", "含义备注", "workbuddy", "2026-08-13",
        ])
    rows = ea.load_annotation_csv(p)
    assert len(rows) == 1
    r = rows[0]
    assert r["text_id"] == "e1" and r["sentiment"] == "negative"
    assert r["intensity"] == "4", (repr(r["intensity"]), repr(r["relevant"]), repr(r.get("subset_cn")))
    assert r["relevant"] == "yes", repr(r["relevant"])
    assert r["subset_cn"] == "反讽"
    assert r["dims"] == {"角色偏好": "positive"}
    assert r["flags"] == ["反讽", "emoji主导"]
    assert r["remark"] == "含义备注" and r["annotator"] == "workbuddy"
    print("✓ CSV 解析（情感小写/相关映射/维度/语言现象/备注）通过")


def test_load_xlsx(tmp_path: Path) -> None:
    from openpyxl import Workbook

    headers = [
        "序号", "text_id", "平台", "领域", "品牌/主题", "关键词", "文本类型", "是否楼中楼",
        "原文", "帖子标题", "作者", "链接", "发布时间", "点赞数", "采集批次",
        "情感(整条)", "强度(1-5)", "子集", "角色偏好", "剧情评价", "氪金体验",
        "美术反馈", "玩法体验", "运营与社区", "Bug/技术",
        "语言现象", "是否相关", "备注", "标注人", "标注日期",
    ]
    p = tmp_path / "annot.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "游戏标注"
    ws.append(headers)
    ws.append([1, "x1", "bilibili", "game", "恋与深空", "恋与深空", "评论", "",
               "太棒了真的", "", "作者B", "https://x/2", "", "3", "batch2",
               "mixed", "2", "低置信", "", "", "", "", "", "", "",
               "黑话", "否", "", "trae-GLM-5.2", "2026-08-13"])
    wb.save(p)
    rows = ea.load_annotation_xlsx(p)
    assert len(rows) == 1
    r = rows[0]
    assert r["sentiment"] == "mixed" and r["intensity"] == "2"
    assert r["subset_cn"] == "低置信" and r["relevant"] == "no"
    assert r["flags"] == ["黑话"] and r["annotator"] == "trae-GLM-5.2"
    print("✓ XLSX 解析通过")


def _synthetic_pair() -> tuple[list[dict], list[dict], dict]:
    primary = [
        _row("t1", "positive", subset="long"),
        _row("t2", "negative", subset="jargon"),
        _row("t3", "neutral", subset="dialect"),
        _row("t4", "positive", subset="emoji"),
        _row("t5", "neutral", subset="lowconf"),
        _row("t6", "positive", subset="long", relevant="no"),
        _row("t7", "mixed", subset="lowconf"),
        _row("t8", "positive", subset="irony"),
    ]
    secondary = [
        _row("t1", "positive", subset="long"),
        _row("t2", "negative", subset="jargon"),
        _row("t3", "neutral", subset="dialect"),
        _row("t4", "negative", subset="emoji"),
        _row("t5", "negative", subset="lowconf"),
        _row("t6", "positive", subset="long", relevant="yes"),
        _row("t7", "neutral", subset="lowconf"),
        _row("t8", "positive", subset="irony"),
    ]
    return primary, secondary, ea.merge_compare(primary, secondary)


def test_merge_compare() -> None:
    _, _, rep = _synthetic_pair()
    assert rep["n"] == 8
    assert rep["sentiment_agreement"] == 0.625  # t1/t2/t3/t6/t8
    assert rep["relevance_agreement"] == 0.875  # 仅 t6 分歧
    assert {d["text_id"] for d in rep["disputes"]} == {"t4", "t5", "t6", "t7"}
    assert {h["text_id"] for h in rep["high_ambiguity"]} == {"t6", "t7", "t8"}
    expected_kappa = round(ea.cohen_kappa(
        ["positive", "negative", "neutral", "positive", "neutral", "positive", "mixed", "positive"],
        ["positive", "negative", "neutral", "negative", "negative", "positive", "neutral", "positive"],
    ), 4)
    assert rep["kappa"] == expected_kappa, (rep["kappa"], expected_kappa)
    print("✓ 合并比对（一致率/分歧/高歧义/kappa）通过")


def test_review_sheets_deterministic_and_quota() -> None:
    primary, secondary = [], []
    for sub in ea.SUBSET_ORDER:
        for i in range(50):
            sent = "positive" if i % 2 == 0 else "negative"
            primary.append(_row(f"{sub}_{i}", sent, subset=sub))
            secondary.append(_row(f"{sub}_{i}", sent, subset=sub))
    # 制造高歧义：irony_1~5 副标 mixed；dialect_1/2 副标 relevant=no
    p_map = {r["text_id"]: r for r in primary}
    s_map = {r["text_id"]: r for r in secondary}
    for i in range(5):
        s_map[f"irony_{i}"]["sentiment"] = "mixed"
    s_map["dialect_1"]["relevant"] = "no"
    s_map["dialect_2"]["relevant"] = "no"
    rep = ea.merge_compare(primary, secondary)
    # 反讽子集 50 条全量 + 方言 2 条 relevant=no → 高歧义 52 条（方案：反讽 100% 复核）
    assert len(rep["high_ambiguity"]) == 52

    c1, k1 = ea.build_review_sheets(primary, secondary, rep, seed=42)
    c2, k2 = ea.build_review_sheets(primary, secondary, rep, seed=42)
    assert c1 == c2 and k1 == k2, "固定种子必须可复现"
    high = {h["text_id"] for h in rep["high_ambiguity"]}
    assert high <= set(c1), "一致性核对表必须含高歧义 100%"
    # 反讽子集全部为高歧义 → 分层随机仅 50 条（每非反讽子集 10），合计 50 + 52 = 102
    assert len(c1) == 50 + 52
    by_sub = {s: 0 for s in ea.SUBSET_ORDER}
    for tid in c1:
        if tid not in high:
            by_sub[tid.rsplit("_", 1)[0]] += 1
    assert by_sub["irony"] == 0
    assert all(v == 10 for k, v in by_sub.items() if k != "irony"), by_sub
    assert len(k1) == 60
    assert k1[:12] == [h["text_id"] for h in rep["high_ambiguity"]][:12]
    cal_sub = {s: 0 for s in ea.SUBSET_ORDER}
    cal_high = set(k1[:12])
    for tid in k1[12:]:
        cal_sub[tid.rsplit("_", 1)[0]] += 1
    assert all(v == 8 for v in cal_sub.values()), cal_sub
    print("✓ 复核表（配额/高歧义全量/种子可复现）通过")


def test_finalize_rows() -> None:
    primary = [
        _row("t1", "positive", subset="long"),
        _row("t2", "positive", subset="jargon"),
        _row("t3", "neutral", subset="irony"),
    ]
    secondary = [
        _row("t1", "negative", subset="long"),
        _row("t2", "positive", subset="jargon"),
        _row("t3", "positive", subset="irony"),
    ]
    human = {
        "t1": {"text_id": "t1", "sentiment": "negative", "intensity": "4",
               "dims": {}, "relevant": "yes", "flags": [], "remark": "反讽",
               "reviewer": "pm", "reviewed_at": "2026-08-13"},
        "t2": {"text_id": "t2", "sentiment": "negative", "intensity": "",
               "dims": {}, "relevant": "", "flags": [], "remark": "",
               "reviewer": "", "reviewed_at": ""},
    }
    final_rows, disputed = ea.finalize_rows(primary, secondary, human)
    by_id = {r["text_id"]: r for r in final_rows}
    assert {r["text_id"] for r in final_rows} == {"t1", "t2"}
    assert by_id["t1"]["sentiment"] == "negative"
    assert by_id["t1"]["intensity"] == "4"
    assert "人工:pm" in by_id["t1"]["finalize_note"]
    assert by_id["t2"]["sentiment"] == "negative"  # 双 AI 一致但人工覆盖
    assert len(disputed) == 1 and disputed[0]["text_id"] == "t3"
    assert "情感分歧未决" in disputed[0]["reason"]
    print("✓ 定版规则（人工优先/覆盖一致样本/未决分歧入争议集）通过")


def test_finalize_rows_reviewed_blank_means_primary() -> None:
    """复核表内留空=认可主标（填写说明口径），只有完全未复核的分歧才入争议集。"""
    primary = [
        _row("t1", "positive", subset="irony"),
        _row("t2", "neutral", subset="irony"),
    ]
    secondary = [
        _row("t1", "negative", subset="irony"),
        _row("t2", "positive", subset="irony"),
    ]
    # t1/t2 都在复核表内但人工判定留空（无 human 条目）→ 认可主标
    final_rows, disputed = ea.finalize_rows(
        primary, secondary, {}, reviewed_ids={"t1", "t2"}
    )
    assert len(disputed) == 0
    by_id = {r["text_id"]: r for r in final_rows}
    assert by_id["t1"]["sentiment"] == "positive"
    assert by_id["t2"]["sentiment"] == "neutral"
    # t3 完全未复核 → 分歧入争议集
    primary.append(_row("t3", "neutral", subset="irony"))
    secondary.append(_row("t3", "negative", subset="irony"))
    final_rows, disputed = ea.finalize_rows(
        primary, secondary, {}, reviewed_ids={"t1", "t2"}
    )
    assert [d["text_id"] for d in disputed] == ["t3"]
    print("✓ 定版规则（复核表内留空=认可主标；未复核分歧入争议集）通过")


def test_is_phenomenon_brand() -> None:
    # 现象信号词（方言/反讽/emoji）→ True
    assert ea.is_phenomenon_brand("唔该")
    assert ea.is_phenomenon_brand("巴适")
    assert ea.is_phenomenon_brand("😡😡 太坑了")
    assert ea.is_phenomenon_brand("笑死")
    # 额外关键词表（采集脚本 EDGE_PLAN）
    assert ea.is_phenomenon_brand("好鬼正", extra_keywords=frozenset({"好鬼正"}))
    # 真实品牌 / 空值 → False
    for brand in ("恋与深空", "华润万家", "大疆", "瑞幸", "星巴克 对比", ""):
        assert not ea.is_phenomenon_brand(brand), brand
    print("✓ 现象样本品牌识别（方言/反讽/emoji/真实品牌）通过")


def test_review_row_brand_display() -> None:
    a = _row("t1", "positive", subset="dialect")
    a["brand"] = ""
    a["text"] = "唔该晒"
    b = _row("t1", "positive", subset="dialect")
    b["brand"] = ""
    row = ea.review_row(1, "t1", {"t1": a}, {"t1": b})
    assert row["品牌/主题"] == "现象样本"
    assert row["子集"] == "方言"
    a["brand"] = "恋与深空"
    row2 = ea.review_row(1, "t1", {"t1": a}, {"t1": b})
    assert row2["品牌/主题"] == "恋与深空"
    print("✓ 复核表品牌展示（现象样本/真实品牌）通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_kappa_perfect()
        test_kappa_known_value()
        test_validate_rows()
        test_parse_csv(tmp)
        test_load_xlsx(tmp)
        test_merge_compare()
        test_review_sheets_deterministic_and_quota()
        test_finalize_rows()
        test_finalize_rows_reviewed_blank_means_primary()
        test_is_phenomenon_brand()
        test_review_row_brand_display()
    print("边界样本专项集标注配套库测试全部通过 ✅")


if __name__ == "__main__":
    main()
