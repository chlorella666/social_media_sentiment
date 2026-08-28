# -*- coding: utf-8 -*-
"""清洗规则测试（2.2 增强）：WebSearch 误杀修复。

运行：python tests/test_cleaner.py
覆盖：文本过短阈值按渠道放宽、官方页面规则不再误杀第三方评价/评分聚合页、
      WebSearch 摘要混入页面壳词时按标题是否有信息量决定是否保留。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coding import cleaner  # noqa: E402
from app.core.models import Comment, Post  # noqa: E402


def _post(platform: str, title: str, content: str, pid: str = "x",
             url: str | None = None) -> Post:
    return Post(id=pid, platform=platform, title=title, content=content,
                url=url or f"https://x.example/{pid}")


def test_websearch_short_text_not_dropped() -> None:
    kept, dropped = cleaner.clean_posts(
        [_post("websearch", "恋与深空 短评", "超好玩")], subject="恋与深空"
    )
    assert len(kept) == 1 and not dropped
    # 非 WebSearch 渠道仍按 10 字阈值
    kept2, dropped2 = cleaner.clean_posts(
        [_post("weibo", "短评", "好")], subject="恋与深空"
    )
    assert not kept2 and dropped2 and "文本过短" in dropped2[0]["reason"]
    print("✓ WebSearch 短评不再因文本过短误杀 通过")


def test_official_page_review_aggregator_not_dropped() -> None:
    assert cleaner.is_official_page("恋与深空的最新评论和评分-应用宝官网") is False
    assert cleaner.is_official_page("恋与深空官网") is True
    assert cleaner.is_official_page("官方网站") is True
    kept, dropped = cleaner.clean_posts(
        [_post("websearch", "恋与深空的最新评论和评分-应用宝官网",
               "应用宝上的评分汇总，用户评价很多")],
        subject="恋与深空",
    )
    assert len(kept) == 1 and not dropped
    print("✓ 官方页面规则不再误杀第三方评价/评分聚合页 通过")


def test_websearch_boilerplate_snippet_not_dropped() -> None:
    kept, dropped = cleaner.clean_posts(
        [_post("websearch", ". 对 恋与深空的评价 - TapTap",
               "查看详细资料 很好玩 强烈推荐这个游戏")],
        subject="恋与深空",
    )
    assert len(kept) == 1 and not dropped
    # 非 WebSearch 渠道仍按严格壳词规则
    kept2, dropped2 = cleaner.clean_posts(
        [_post("weibo", "某帖子", "查看详细资料")], subject="恋与深空"
    )
    assert not kept2 and dropped2 and "样板/页面壳文本" in dropped2[0]["reason"]
    print("✓ WebSearch 摘要混入页面壳词时不误杀有信息量的帖子 通过")


def test_desensitize_text() -> None:
    d = cleaner.desensitize_text
    s = "联系 a@b.com 或 13800138000 或 110101199003071234，@某用户 https://weibo.com/x"
    out = d(s)
    assert "a@b.com" not in out
    assert "13800138000" not in out
    assert "110101199003071234" not in out
    assert "@某用户" not in out
    assert "weibo.com" not in out
    # 保守原则：短数字串（游戏模组码/产品编号）不得被误删
    assert d("模组码:8099410 【红石版】:821226 其余保持原样") == "模组码:8099410 【红石版】:821226 其余保持原样"
    print("✓ LLM 前脱敏（邮箱/手机/身份证/@/链接）且不误伤短数字码 通过")

# ---------------------------------------------------------------------------
# 2026-08-22：容错相关性判定（别名/变体/信息不足降级）+ 小红书详情一致性守卫
# ---------------------------------------------------------------------------


def _dropped_reasons(kept, dropped):
    return [d.get("reason", "") for d in dropped]


def test_relevance_alias_kept() -> None:
    """别名（LYSK/叠纸）命中即保留，不再误杀。"""
    kept, dropped = cleaner.clean_posts(
        [
            _post("websearch_zhihu", "LYSK乙游事件 - 知乎", "关于恋与深空运营的讨论", pid="a"),
            _post("websearch_zhihu", "为什么叠纸一直致力于得罪玩家?", "玩家对恋与深空的不满", pid="b"),
        ],
        subject="恋与深空",
    )
    assert len(kept) == 2, _dropped_reasons(kept, dropped)


def test_relevance_variant_kept() -> None:
    """变体（空格/大小写/简称）经归一化+最长公共子串命中。"""
    kept, dropped = cleaner.clean_posts(
        [
            _post("websearch", "华润超市怎么样", "华润 超市 购物体验", pid="c"),
            _post("websearch", "iPhone 15 评测", "iphone15 拍照体验", pid="d"),
            _post("websearch", "迈从X9机械键盘", "迈从 x9 手感不错", pid="e"),
        ],
        subject="华润万家",
        keywords=["iphone", "迈从X9"],
    )
    assert len(kept) == 3, _dropped_reasons(kept, dropped)


def test_relevance_unrelated_real_content_kept_weak() -> None:
    """有实质正文但未命中：保留并标记 weak（交由 LLM/人工判定），不硬丢。"""
    post = _post("websearch", "今日股市行情", "上证指数收盘上涨", pid="f")
    kept, dropped = cleaner.clean_posts([post], subject="恋与深空")
    assert len(kept) == 1 and not dropped
    assert post.platform_specific.get("relevance") == "weak"


def test_relevance_insufficient_info_reason() -> None:
    """标题不含品牌且正文缺失/过短时，不再标「与品牌/关键词不相关」。"""
    kept, dropped = cleaner.clean_posts(
        [_post("xiaohongshu", "今天天气真好适合出去玩", "今天天气真好适合出去玩", pid="h")],
        subject="恋与深空",
    )
    assert not kept and dropped
    assert "信息不足" in dropped[0]["reason"]
    assert "与品牌/关键词不相关" not in dropped[0]["reason"]
    # 极短内容仍由「文本过短」兜底，也不会误标不相关
    kept2, dropped2 = cleaner.clean_posts(
        [_post("xiaohongshu", "快跑！！！", "快跑！！！", pid="i")],
        subject="恋与深空",
    )
    assert not kept2 and dropped2
    assert "与品牌/关键词不相关" not in dropped2[0]["reason"]


def test_relevance_weak_content_kept_with_flag() -> None:
    """有实质正文但未命中：保留并标记 low relevance，不误杀。"""
    post = _post("xiaohongshu", "isa 你没有心", "刚买的这款耳机真的太难用了，音质很差")
    kept, dropped = cleaner.clean_posts([post], subject="某品牌耳机")
    assert len(kept) == 1 and not dropped
    assert post.platform_specific.get("relevance") == "weak"


def test_drop_record_has_content_and_match() -> None:
    """丢弃记录补全正文摘要与判定依据，供人工核对。"""
    _, dropped = cleaner.clean_posts(
        [_post("xiaohongshu", "快跑！！！", "快跑！！！", pid="g")],
        subject="恋与深空",
    )
    assert dropped
    assert "content" in dropped[0]
    assert "match" in dropped[0]


def test_detail_consistency_guard() -> None:
    """小红书详情一致性守卫：正常配对通过、错配被识别。"""
    from app.channels.xiaohongshu import _detail_is_consistent

    assert _detail_is_consistent("西村力五杀一帅", "#西村力 #西村力晚安")
    assert _detail_is_consistent("恋与深空 七夕", "和哥哥过七夕吧~")
    assert not _detail_is_consistent("全网无代餐极品阴湿男", "一直非常相信全棉时代的生产卫生")
    assert not _detail_is_consistent("北海道温泉旅馆", "#恋与深空 #收谷")
    # 任一为空/过短不阻断
    assert _detail_is_consistent("", "任意详情")
    assert _detail_is_consistent("短", "内容")


# ---------------------------------------------------------------------------
# F-030 阶段0（2026-08-28）：真实采集快照基线 + 全原因/去重/壳内容覆盖
# ---------------------------------------------------------------------------


def _load_baseline_posts():
    fixture = ROOT / "tests" / "fixtures" / "cleaner_baseline_posts.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    posts = []
    for p in data["posts"]:
        posts.append(
            Post(
                id=p.get("id") or p["url"],
                platform=p.get("platform", ""),
                keyword=p.get("keyword", ""),
                author=p.get("author", ""),
                title=p.get("title", ""),
                content=p.get("content", ""),
                url=p.get("url", ""),
                timestamp=p.get("timestamp", ""),
                likes=int(p.get("likes") or 0),
                comments=[Comment(**c) for c in p.get("comments") or []],
            )
        )
    return posts, data


def test_cleaner_baseline_fixture() -> None:
    """F-030 阶段0：真实采集快照重建 fixture 的保留/丢弃行为冻结。

    数据源：恋与深空任务 20260821_001440_de87bd（220 保留帖 + 53 清洗层丢弃）。
    快照丢弃记录无 content，重建入参正文为空 → 当前行为按「正文为空」丢弃；
    原始 drop_reason 仅作追溯。后续重构阶段必须保持本基线不变
    （stage 1/4/5 严格等价；stage 2 允许的归一化变化须显式更新并复核）。
    """
    posts, data = _load_baseline_posts()
    kept, dropped = cleaner.clean_posts(
        posts, subject=data["subject"], keywords=[data["subject"]]
    )
    assert data["subject"] == "恋与深空"
    assert len(posts) == 273
    assert len(kept) == 220
    assert len(dropped) == 53
    by_reason = Counter()
    by_kind = Counter()
    for d in dropped:
        for r in (d.get("reason") or "").split("；"):
            r = r.strip()
            if r:
                by_reason[r] += 1
        by_kind[d.get("kind", "quality")] += 1
    assert dict(by_reason) == {"正文为空": 53, "文本过短": 7}
    assert dict(by_kind) == {"quality": 53}
    print("✓ 真实快照基线（273 入参 → 220 保留 / 53 丢弃）冻结 通过")


def test_clean_posts_full_reason_coverage() -> None:
    """全丢弃原因覆盖（合成）：正文为空/样板/官方页/文本过短/信息不足 + weak 保留。"""
    posts = [
        _post("weibo", "空正文标题", ""),
        _post("weibo", "加载中", "加载中"),
        _post("weibo", "某品牌官网", "欢迎访问官网首页"),
        _post("weibo", "标题", "太短"),
        _post("xiaohongshu", "今天天气真好适合出去玩", "今天天气真好适合出去玩"),
        _post("websearch", "有正文但无关", "这是一段超过十个字的真实正文内容但主题完全无关"),
    ]
    kept, dropped = cleaner.clean_posts(posts, subject="恋与深空")
    reasons = "；".join(d["reason"] for d in dropped)
    assert "正文为空" in reasons
    assert "样板/页面壳文本" in reasons
    assert "官方页面" in reasons
    assert "文本过短" in reasons
    assert "疑似不相关（信息不足）" in reasons
    assert len(kept) == 1
    assert kept[0].platform_specific.get("relevance") == "weak"
    print("✓ 全丢弃原因覆盖（合成）通过")


def test_clean_posts_dedupe_keys() -> None:
    """四重去重键（合成）：相同ID/链接/标题/正文。"""
    posts = [
        _post("weibo", "恋与深空 标题甲 去重测试", "内容一", pid="a"),
        _post("weibo", "恋与深空 标题甲 去重测试", "内容二", pid="b"),
        _post("weibo", "恋与深空 标题乙 去重测试", "这是一段足够长的正文内容用于触发正文去重机制啊", pid="c"),
        _post("weibo", "恋与深空 标题丙 去重测试", "这是一段足够长的正文内容用于触发正文去重机制啊", pid="d"),
        _post("weibo", "恋与深空 标题丁 去重测试", "内容五", pid="e"),
        _post("weibo", "恋与深空 标题戊 去重测试", "内容六", pid="f", url="https://x.example/dup"),
        _post("weibo", "恋与深空 标题己 去重测试", "内容七", pid="g", url="https://x.example/dup"),
        _post("weibo", "恋与深空 标题庚 去重测试", "内容八", pid="a"),
    ]
    kept, dropped = cleaner.clean_posts(posts, subject="恋与深空")
    assert len(kept) == 4
    reasons = "；".join(d["reason"] for d in dropped)
    assert "重复（相同标题）" in reasons
    assert "重复（相同正文）" in reasons
    assert "重复（相同链接）" in reasons
    assert "重复（相同ID）" in reasons
    print("✓ 四重去重键覆盖（合成）通过")


def test_f031_shell_zero_residual() -> None:
    """F-031 壳内容：投诉举报邮箱/攻略大全/组合特征判壳；礼包码/兑换码真实讨论不误杀。"""
    shell_posts = [
        _post("weibo", "壳1", "投诉举报邮箱：kefu@example.com 有问题请联系"),
        _post("weibo", "壳2", "攻略大全 恋与深空全角色攻略"),
        _post("weibo", "壳3", "兑换码领取攻略大全 手慢无"),
        _post("weibo", "壳4", "礼包码 xxxx 领取地址见下"),
    ]
    kept, dropped = cleaner.clean_posts(shell_posts, subject="恋与深空")
    assert len(kept) == 0, _dropped_reasons(kept, dropped)
    for d in dropped:
        assert "样板/页面壳文本" in d["reason"], d
    legit_posts = [
        _post("weibo", "真实讨论1", "恋与深空新兑换码真的换到了好皮肤，太开心了", pid="legit1"),
        _post("weibo", "真实讨论2", "恋与深空礼包码被用完了，客服说要等补货", pid="legit2"),
    ]
    kept2, dropped2 = cleaner.clean_posts(legit_posts, subject="恋与深空")
    assert len(kept2) == 2, _dropped_reasons(kept2, dropped2)
    print("✓ F-031 壳内容零残留 + 礼包码/兑换码真实讨论不误杀 通过")

def test_export_text_desensitized() -> None:
    """F-030 阶段4：Excel/HTML 导出物文本列 PII 打码；报告正文保留原文。"""
    from app.core.models import AnalysisPlan, ChannelResult, Comment, Post, ReportBundle
    from app.output import excel_writer
    from app.output.html_report import _sanitize_evidence_cards

    plan = AnalysisPlan(subject="测试品牌")
    post = Post(
        id="p1", platform="weibo", title="联系 13800138000",
        content="邮箱 a@b.com 或电话 13800138000", url="https://x.example/1",
        comments=[Comment(id="c1", author="某人", text="身份证 110101199003071234")],
    )
    ch = ChannelResult(
        channel_id="weibo", ok=True, posts=[post],
        dropped=[{
            "platform": "weibo", "url": "https://x.example/2",
            "title": "标题 13800138000", "keyword": "", "query": "",
            "reason": "样板", "content": "a@b.com", "match": "",
        }],
    )
    bundle = ReportBundle(plan=plan, channel_results=[ch])
    pr = excel_writer._posts_rows(bundle)[0]
    assert "13800138000" not in pr["正文"] and "a@b.com" not in pr["正文"]
    assert "13800138000" not in pr["标题"]
    assert "a@b.com" not in pr["评论内容"] and "110101199003071234" not in pr["评论内容"]
    cr = excel_writer._comments_rows(bundle)[0]
    assert "110101199003071234" not in cr["评论内容"]
    dr = excel_writer._dropped_rows(bundle)[0]
    assert "13800138000" not in dr["标题"] and "a@b.com" not in dr["正文摘要"]
    cards = _sanitize_evidence_cards([
        {"id": "e1", "text": "原文 13800138000 a@b.com", "platform": "weibo", "kind": "text"},
    ])
    assert "13800138000" not in cards[0]["text"] and "a@b.com" not in cards[0]["text"]
    print("✓ 导出物文本列 PII 打码（Excel/HTML），报告正文保留原文 通过")

def test_light_normalize_boilerplate_variants() -> None:
    """F-030 阶段2：样板/壳判定前轻归一化——标题全角变体命中，原文不被修改。"""
    assert cleaner._light_normalize("ＡＢＣ　Ｄ") == "abc d"
    posts = [
        _post("weibo", "ＡＰＫ下载", "这是一段超过十个字的真实正文内容"),
        _post("weibo", "恋与深空 合法兑换码", "兑换码真的换到了好皮肤，太开心了"),
    ]
    kept, dropped = cleaner.clean_posts(posts, subject="恋与深空")
    assert len(dropped) == 1, _dropped_reasons(kept, dropped)
    assert "样板/页面壳文本" in dropped[0]["reason"], dropped[0]
    assert len(kept) == 1 and kept[0].title == "恋与深空 合法兑换码"
    kept2, _ = cleaner.clean_posts(
        [_post("weibo", "恋与深空 保留空白", "内容 里有  连续空白  的正文内容")],
        subject="恋与深空",
    )
    assert kept2[0].content == "内容 里有  连续空白  的正文内容"
    print("✓ 轻归一化：标题全角变体命中样板，原文不变 通过")

def test_cleaner_ledger_fields() -> None:
    """F-030 阶段1：丢弃记录含 step/fingerprint，指纹可复现，判定行为不变。"""
    import hashlib

    posts = [
        _post("weibo", "恋与深空 样板页 测试", "加载中", pid="a"),
        _post("weibo", "恋与深空 重复甲 测试", "正文内容甲", pid="b"),
        _post("weibo", "恋与深空 重复甲 测试", "正文内容乙", pid="c"),
        _post("xiaohongshu", "今天天气真好适合出去玩", "今天天气真好适合出去玩", pid="d"),
    ]
    kept, dropped = cleaner.clean_posts(posts, subject="恋与深空")
    by_url = {d["url"]: d for d in dropped}
    sample = by_url["https://x.example/a"]
    expected_fp = hashlib.sha256(
        "恋与深空 样板页 测试\n加载中".encode("utf-8")
    ).hexdigest()[:16]
    assert sample["fingerprint"] == expected_fp
    assert sample["step"] == "4_boilerplate"
    assert by_url["https://x.example/c"]["step"] == "7_dedup"
    assert by_url["https://x.example/d"]["step"] == "6_relevance"
    for d in dropped:
        assert "step" in d and "fingerprint" in d and "content" in d
    print("✓ 开账：丢弃记录 step/fingerprint 可回溯且判定不变 通过")

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_websearch_short_text_not_dropped()
    test_official_page_review_aggregator_not_dropped()
    test_websearch_boilerplate_snippet_not_dropped()
    test_desensitize_text()
    test_relevance_alias_kept()
    test_relevance_variant_kept()
    test_relevance_unrelated_real_content_kept_weak()
    test_relevance_insufficient_info_reason()
    test_relevance_weak_content_kept_with_flag()
    test_drop_record_has_content_and_match()
    test_detail_consistency_guard()
    test_cleaner_baseline_fixture()
    test_clean_posts_full_reason_coverage()
    test_clean_posts_dedupe_keys()
    test_f031_shell_zero_residual()
    test_cleaner_ledger_fields()
    test_light_normalize_boilerplate_variants()
    test_export_text_desensitized()
    print("清洗规则测试全部通过 ✅")


if __name__ == "__main__":
    main()
