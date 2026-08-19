# -*- coding: utf-8 -*-
"""其他渠道关键词策略（Phase 0，2026-08-18）：
渠道级查询展开、候选按渠道写入、策略保存上限。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import keyword_effects as ke  # noqa: E402


def _strategy() -> dict:
    return {
        "schema": 2,
        "synonyms": {},
        "extra_queries": {},
        "suffix_pool": ["评价"],
        "rules": {
            "websearch": {"prefer_suffix": []},
            "bilibili": {"queries": {"恋与深空": ["恋与深空 抽卡", "恋与深空 剧情"]}},
            "weibo": {"queries": {}},
            "xiaohongshu": {
                "preferred": {"恋与深空": ["恋与深空", "恋与深空 攻略"]},
                "avoid": {},
            },
        },
    }


def test_expand_channel_queries() -> None:
    strat = _strategy()
    # bilibili：品牌词命中 → 映射查询；未命中保留
    out = ke.expand_channel_queries(["恋与深空", "华润万家 评价"], "bilibili", strat)
    assert out == ["恋与深空 抽卡", "恋与深空 剧情", "华润万家 评价"], out
    # 前缀匹配：kw 以「品牌 」开头也展开
    out2 = ke.expand_channel_queries(["恋与深空 评价"], "bilibili", strat)
    assert out2 == ["恋与深空 抽卡", "恋与深空 剧情"], out2
    # weibo 无配置 → 原样
    assert ke.expand_channel_queries(["恋与深空"], "weibo", strat) == ["恋与深空"]
    # xhs：只精简不扩量（preferred ≤2）
    out3 = ke.expand_channel_queries(["恋与深空", "华润万家"], "xiaohongshu", strat)
    assert out3 == ["恋与深空", "恋与深空 攻略", "华润万家"], out3
    # 空策略 → 原样
    assert ke.expand_channel_queries(["恋与深空"], "bilibili", {"rules": {}}) == ["恋与深空"]
    print("✓ 渠道查询展开（bilibili 映射 / xhs 精简不扩量 / 兜底）通过")


def test_apply_candidates_channel_aware() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sms_chstrat2_")) / "strategy.json"
    tmp.write_text(json.dumps(_strategy(), ensure_ascii=False), encoding="utf-8")
    out = ke.apply_candidates(
        [
            {"类型": "查询候选", "主题": "恋与深空", "候选": "恋与深空 剧情",
             "渠道": "bilibili"},
            {"类型": "避免", "主题": "恋与深空", "候选": "恋与深空 低效",
             "渠道": "xiaohongshu"},
            {"类型": "查询候选", "主题": "恋与深空", "候选": "恋与深空 叠纸",
             "渠道": "websearch"},
        ],
        path=tmp,
    )
    rules = out["rules"]
    assert "恋与深空 剧情" in rules["bilibili"]["queries"]["恋与深空"]
    assert "恋与深空 低效" in rules["xiaohongshu"]["avoid"]["恋与深空"]
    assert "恋与深空 叠纸" in out["extra_queries"]["恋与深空"]
    print("✓ 候选按渠道写入（bilibili queries / xhs avoid / websearch extra）通过")


def test_save_strategy_edit_channel_caps() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sms_chstrat_")) / "strategy.json"
    tmp.write_text(json.dumps(_strategy(), ensure_ascii=False), encoding="utf-8")
    res = ke.save_strategy_edit(
        channel_queries_updates={"bilibili": {"华润万家": ["华润万家 超市", "华润万家 购物"]}},
        xhs_preferred_updates={"星巴克": ["星巴克", "星巴克 咖啡", "星巴克 第三词"]},
        path=tmp,
    )
    strat = ke.load_keyword_strategy(tmp)
    assert strat["rules"]["bilibili"]["queries"]["华润万家"] == [
        "华润万家 超市", "华润万家 购物"]
    # 保存不静默截断（与 WebSearch 一致），展开时按上限截断
    assert len(strat["rules"]["xiaohongshu"]["preferred"]["星巴克"]) == 3
    expanded = ke.expand_channel_queries(
        ["星巴克"], "xiaohongshu", strat)
    assert expanded == ["星巴克", "星巴克 咖啡"]
    assert any("超过上限" in w for w in res["warnings"])
    print("✓ 策略保存（渠道字段 + 上限警告 + 展开截断）通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_expand_channel_queries()
    test_apply_candidates_channel_aware()
    test_save_strategy_edit_channel_caps()
    print("其他渠道关键词策略（Phase 0 机制）测试全部通过 ✅")


if __name__ == "__main__":
    main()
