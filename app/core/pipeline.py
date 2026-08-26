"""TaskRunner：向导确认后的一次完整分析编排（状态机 + 进度回调）。"""

from __future__ import annotations

import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.channels.registry import get_channel
from app.channels.base import degraded_result
from app.coding.cleaner import clean_posts
from app.coding.coder import Coder
from app.coding.insights import build_report_content
from app.coding.llm_analyzer import (
    BaseAnalyzer,
    MockAnalyzer,
    OpenAICompatibleAnalyzer,
    create_analyzer,
)
from app.coding.tokenizer import (
    GENERIC_NOUNS,
    SYNONYM_GROUPS,
    build_cooccurrence,
    build_phrase_cooccurrence,
    build_word_freq,
    encode_phrases,
    extract_phrases,
    segment,
)
from app.coding import lexicon_v2
from app.core.models import (
    AnalysisPlan,
    ChannelResult,
    CodedItem,
    Post,
    ReportBundle,
    SentimentLabel,
    TaskStatus,
)
from app.core.names import ATTRIBUTION_ACTORS, NARRATIVE_FRAMES
from app.core.topics import build_topic_clusters
from app.core.drops import is_quality_drop
from app.core.evidence import (
    MIN_DIM_NORMAL,
    MIN_DIM_RATING,
    build_evidence,
    display_finding_id,
)
from app.core.names import dimension_cn, register_custom_dim_names
from app.core.pricing import cost_from_usage
from app.core.progress import ProgressTracker
from app.domains.loader import task_schema

ProgressCallback = Callable[[TaskStatus, str, float, dict], None]


VOICE_TIER_RATIO_DEFAULT = 0.1  # 默认渠道正/负最小条数 = 10% × E
# 消费者声音分档按渠道校准（2026-08-18）：B站视频标题语体显式负面/正面占比
# 天然低于评论型渠道（瑞幸×B站实测 E=18、neg=3、pos=1 却被判"不足"），
# 该渠道正/负最小条数比率下调，避免"捕获充足却判不足"的误报。
VOICE_TIER_CFG = {
    "bilibili": {"neg_ratio": 0.05, "pos_ratio": 0.05},
    "demo": {"neg_ratio": 0.05, "pos_ratio": 0.05},
}


def voice_tier(
    effective: int, target: int, pos_n: int, neg_n: int,
    neg_ratio: float = VOICE_TIER_RATIO_DEFAULT,
    pos_ratio: float = VOICE_TIER_RATIO_DEFAULT,
) -> str:
    """消费者声音分档（2026-08-18，关键词优化与报告质量提升方案 §二）。

    捕获量按 E 与目标 T 的比例判定；正/负最小条数按渠道语体比率校准
    （默认 10%×E，bilibili/demo 降为 5%×E）。
    """
    tgt = max(target, 1)
    neg_min = max(1, round(neg_ratio * effective))
    pos_min = max(1, round(pos_ratio * effective))
    if effective >= 0.6 * tgt and pos_n >= pos_min and neg_n >= neg_min:
        return "充足"
    if effective >= 0.3 * tgt and neg_n >= neg_min:
        return "够用"
    return "不足"


def consumer_voice_summary(
    plan: AnalysisPlan, items: list[CodedItem], channel_results: list[ChannelResult],
) -> dict:
    """消费者声音指标（2026-08-18）：E = 保留且非广告/官方；占比 E/N；分档。"""
    collected_n = sum(
        len(ch.posts) + sum(len(p.comments) for p in ch.posts) + len(ch.dropped)
        for ch in channel_results if ch.ok
    )
    voice_items = [it for it in items if not it.ad_flag]
    v_pos = sum(1 for it in voice_items if it.sentiment == SentimentLabel.positive)
    v_neg = sum(1 for it in voice_items if it.sentiment == SentimentLabel.negative)
    v_tgt = max(1, len(plan.keywords or [plan.subject])) * max(
        1, plan.per_keyword_limit)
    v_e = len(voice_items)
    # 分档按主导渠道（采集量最大）的语体比率校准
    ch_counts = Counter(ch.channel_id for ch in channel_results if ch.ok)
    dominant_ch = ch_counts.most_common(1)[0][0] if ch_counts else ""
    v_cfg = VOICE_TIER_CFG.get(dominant_ch, {})
    v_tier = voice_tier(
        v_e, v_tgt, v_pos, v_neg,
        neg_ratio=v_cfg.get("neg_ratio", VOICE_TIER_RATIO_DEFAULT),
        pos_ratio=v_cfg.get("pos_ratio", VOICE_TIER_RATIO_DEFAULT),
    )
    return {
        "collected": collected_n,
        "effective": v_e,
        "ratio": round(v_e / collected_n, 4) if collected_n else None,
        "tier": v_tier,
        "positive": v_pos,
        "negative": v_neg,
        "tier_channel": dominant_ch,
        "tier_ratios": {
            "neg": v_cfg.get("neg_ratio", VOICE_TIER_RATIO_DEFAULT),
            "pos": v_cfg.get("pos_ratio", VOICE_TIER_RATIO_DEFAULT),
        },
        "note": "E=保留且非广告/官方；相关性依赖 LLM 复核（未复核为近似）；"
                "词典模式无 ad_flag 时为近似",
    }


def recompute_summary(
    plan: AnalysisPlan, items: list[CodedItem],
    channel_results: list[ChannelResult], posts: list[Post],
) -> dict:
    """重算 summary（2.11 方案 A 复用）：build_summary + llm_corrected + consumer_voice。"""
    summary = build_summary(plan, items, posts)
    summary["llm_corrected"] = sum(
        1
        for it in items
        if it.method == "llm"
        and it.lexicon_sentiment
        and it.lexicon_sentiment != it.sentiment.value
    )
    summary["consumer_voice"] = consumer_voice_summary(plan, items, channel_results)
    return summary


# 补采收敛与收益门控（2026-08-16 调整，降无谓请求与风控暴露）
TOPUP_MAX_ROUNDS = 2          # 补采轮数上限（原 3）
TOPUP_LIMIT_MULTIPLIER = 2    # 补采数量上限乘数（原 4）
TOPUP_MIN_YIELD = 3           # 每轮补采最少净新增保留条数
TOPUP_YIELD_RATIO = 0.2       # 净新增阈值占比（对 target）

SENTIMENT_NAMES = {
    "positive": "正面",
    "negative": "负面",
    "neutral": "中性",
}


def _quality_gate_warnings(total: int) -> list[str]:
    """数据质量门控：样本量不足时逐级降级提示（参考 SocialBrandSentiment）。"""
    warnings = []
    if total == 0:
        warnings.append("未采集到任何有效数据，请调整关键词或渠道后重试")
    elif total < 10:
        warnings.append("样本量不足 10 条，图表与结论仅供参考")
    elif total < 30:
        warnings.append("样本量较少（<30 条），趋势与对比类图表置信度有限")
    elif total < 45:
        warnings.append("样本量偏少（<45 条），平台对比结果仅供参考")
    elif total < 100:
        warnings.append("样本量一般（<100 条），建议扩大关键词或时间段")
    return warnings


def _reconcile_channel_posts(
    channel_results: list[ChannelResult],
    plan: AnalysisPlan,
    warnings: list[str],
) -> list[Post]:
    """补采一致性收尾（2026-08-16 修复）。

    补采会把 ch.posts 直接替换成原始未清洗帖子，而返回给统计/编码的 posts
    是上一轮清洗结果——最后一轮补采帖若被再次丢弃，会出现"报告 0 帖但漏斗有数"
    的矛盾（安克任务实证）。本函数做最终一次清洗并统一写回，保证返回 posts
    与 channel_results 同源：补采帖经最终清洗后真正相关的才进入报告。
    """
    pooled = [p for ch in channel_results for p in ch.posts]
    kept, dropped = clean_posts(pooled, subject=plan.subject, keywords=plan.keywords)
    kept_urls = {p.url for p in kept}
    by_channel = {ch.channel_id: ch for ch in channel_results}
    for ch in channel_results:
        ch.posts = [p for p in ch.posts if p.url in kept_urls]
    if dropped:
        existing = {
            d["url"]
            for ch in channel_results
            for d in (ch.dropped or [])
        }
        extra = [d for d in dropped if d["url"] not in existing]
        if extra:
            for d in extra:
                # 2026-08-19：仅改展示文案，kind 原样保留（包装不改决策语义）
                d["reason"] = f"补采一致性清洗:{d['reason']}"
                target = by_channel.get(d["platform"])
                if target is None and channel_results:
                    target = channel_results[0]
                if target is not None:
                    target.dropped = list(target.dropped or []) + [d]
            warnings.append(
                f"补采一致性清洗：{len(extra)} 条补采帖经最终清洗判定丢弃"
            )
    return kept


def _subject_stopwords(plan: AnalysisPlan) -> set[str]:
    """词云/共现网络的主题过滤停用词：泛话题词 + 分析对象 + 关键词词组词。"""
    extra = set(GENERIC_NOUNS)
    if plan.subject:
        extra.add(plan.subject)
    for kw in plan.keywords:
        for tok in segment(kw):
            extra.add(tok)
    for phrase in plan.exclude_words or []:
        phrase = phrase.strip()
        if len(phrase) >= 2:
            extra.add(phrase)
        for tok in segment(phrase):
            extra.add(tok)
    return extra


NARRATIVE_REF_N = 10  # 叙事/归因桶 count<10 标"样本有限"（方案 §3.5）


def _build_narrative_stats(stat_items: list[CodedItem]) -> dict:
    """叙事归因聚合（方案 Part A，2026-08-19）。

    样本范围：stat_items 中 LLM 编码且 narrative/attribution 非空的文本；
    分母各自取 gave_attr / gave_frame；unclear 单独计数并固定排最后。
    """
    narr_items = [
        it for it in stat_items
        if it.method == "llm" and (it.narrative or it.attribution)
    ]
    total = len(narr_items)
    gave_attr = sum(1 for it in narr_items if it.attribution)
    gave_frame = sum(1 for it in narr_items if it.narrative)
    unclear = sum(1 for it in narr_items if it.attribution == "unclear")
    empty = {
        "total": total, "gave_attr": gave_attr, "gave_frame": gave_frame,
        "unclear": unclear, "by_actor": [], "by_frame": [], "frame_actor": {},
    }
    if not total:
        return empty

    actor_stat: dict[str, dict[str, int]] = {
        a: {"count": 0, "positive": 0, "neutral": 0, "negative": 0}
        for a in ATTRIBUTION_ACTORS
    }
    frame_stat: dict[str, dict[str, int]] = {
        f: {"count": 0, "positive": 0, "neutral": 0, "negative": 0}
        for f in NARRATIVE_FRAMES
    }
    cross: dict[str, dict[str, dict[str, int]]] = {}
    for it in narr_items:
        sent = it.sentiment.value
        if it.attribution:
            b = actor_stat[it.attribution]
            b["count"] += 1
            b[sent] += 1
        if it.narrative:
            f = it.narrative.value
            b = frame_stat[f]
            b["count"] += 1
            b[sent] += 1
            if it.attribution:
                cell = cross.setdefault(f, {}).setdefault(
                    it.attribution, {"count": 0, "negative": 0}
                )
                cell["count"] += 1
                if sent == "negative":
                    cell["negative"] += 1

    def _bucket(key: str, ident: str, st: dict[str, int]) -> dict:
        return {
            key: ident, "count": st["count"],
            "positive": st["positive"], "neutral": st["neutral"],
            "negative": st["negative"],
            "negative_rate": round(st["negative"] / st["count"], 3) if st["count"] else 0,
            "ref": st["count"] < NARRATIVE_REF_N,
        }

    by_actor = [
        _bucket("actor", a, actor_stat[a])
        for a in ATTRIBUTION_ACTORS if actor_stat[a]["count"]
    ]
    by_actor.sort(key=lambda r: (-r["negative"], r["actor"]))
    by_actor = [r for r in by_actor if r["actor"] != "unclear"] + [
        r for r in by_actor if r["actor"] == "unclear"
    ]
    by_frame = [
        _bucket("frame", f, frame_stat[f])
        for f in NARRATIVE_FRAMES if frame_stat[f]["count"]
    ]
    by_frame.sort(key=lambda r: (-r["count"], r["frame"]))
    frame_actor = {
        f: {
            a: {**cell, "negative_rate": round(cell["negative"] / cell["count"], 3)}
            for a, cell in sorted(cross[f].items())
        }
        for f in NARRATIVE_FRAMES if cross.get(f)
    }
    return {
        "total": total, "gave_attr": gave_attr, "gave_frame": gave_frame,
        "unclear": unclear, "by_actor": by_actor, "by_frame": by_frame,
        "frame_actor": frame_actor,
    }


def build_summary(plan: AnalysisPlan, items: list[CodedItem], posts: list[Post]) -> dict:
    """汇总统计：整体分布、平台统计、维度统计、时间趋势、高频词。"""
    total = len(items)
    # 广告/官方内容（2.6）：默认计入；exclude_ad_enabled=True 时仅情感统计剔除，
    # 采集漏斗（total/platform posts/kw coded）与关键词效果保留并注明。
    ads_count = sum(1 for it in items if it.ad_flag)
    exclude_ad = bool(plan.exclude_ad_enabled)
    stat_items = [it for it in items if not (exclude_ad and it.ad_flag)]
    stat_n = len(stat_items)
    sent_counter: Counter[str] = Counter(it.sentiment.value for it in stat_items)
    platform_stats: dict[str, dict] = defaultdict(
        lambda: {"posts": 0, "scores": [], "positive": 0, "negative": 0, "neutral": 0}
    )
    dim_stats: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "negative": 0, "scores": []}
    )
    trend: dict[str, dict] = defaultdict(lambda: {"count": 0, "scores": [], "negative": 0})
    platform_dim: dict[str, dict] = defaultdict(
        lambda: defaultdict(lambda: {"count": 0, "negative": 0})
    )
    date_dim: dict[str, dict] = defaultdict(
        lambda: defaultdict(lambda: {"count": 0, "negative": 0})
    )
    intensity_counter: Counter[int] = Counter()
    all_keywords: Counter[str] = Counter()
    content_texts: list[str] = []
    kw_stats: dict[str, dict] = defaultdict(
        lambda: {"posts": 0, "comments": 0, "coded": 0, "stat": 0,
                 "positive": 0, "negative": 0, "neutral": 0}
    )

    for it in items:
        platform_stats[it.platform]["posts"] += 1
        kw_stats[it.keyword or "未分类"]["coded"] += 1
        if exclude_ad and it.ad_flag:
            continue
        ks = kw_stats[it.keyword or "未分类"]
        ks["stat"] += 1
        ks[it.sentiment.value] += 1
        platform_stats[it.platform]["scores"].append(it.sentiment_score)
        platform_stats[it.platform][it.sentiment.value] += 1
        date = it.pub_date or "未知"
        trend[date]["count"] += 1
        trend[date]["scores"].append(it.sentiment_score)
        if it.sentiment == SentimentLabel.negative:
            trend[date]["negative"] += 1
        # 2.4 口径：维度统计按"维度级情感"计数（而非整条情感聚合到提及维度）
        for dim, dval in it.dimension_sentiments.items():
            dscore = 1.0 if dval == "positive" else -1.0
            dim_stats[dim]["count"] += 1
            dim_stats[dim]["scores"].append(dscore)
            if dval == "negative":
                dim_stats[dim]["negative"] += 1
            platform_dim[it.platform][dim]["count"] += 1
            if dval == "negative":
                platform_dim[it.platform][dim]["negative"] += 1
            date_dim[it.pub_date or "未知"][dim]["count"] += 1
            if dval == "negative":
                date_dim[it.pub_date or "未知"][dim]["negative"] += 1
        intensity_counter[it.intensity] += 1
        for kw in it.keywords:
            all_keywords[kw] += 1
        content_texts.append(it.text)

    # 情感词正负榜：词典极性 + |情感分| 加权
    pos_w: Counter[str] = Counter()
    neg_w: Counter[str] = Counter()
    for it in stat_items:
        w = abs(it.sentiment_score)
        for kw in it.keywords:
            pol = lexicon_v2.word_polarity(kw)
            if pol > 0:
                pos_w[kw] += w
            elif pol < 0:
                neg_w[kw] += w

    # F-009/F-021（2026-08-26）：短语级统计（bigram~5gram + PMI + 情感权重）
    # F-021：词典模式撤销主题层——回退 jieba 单词词频（v0.1.8 口径），
    # 短语/主题仅在 LLM 模式生成（LLM 编码工作流在此基础上归类）
    phrase_data: list[dict] = []
    topics: list[dict] = []
    if plan.llm_enabled:
        try:
            phrase_data = extract_phrases(
                [it.text for it in stat_items],
                sentiments=[it.sentiment.value for it in stat_items],
                brand=plan.subject,
                keywords=plan.keywords,
            )
        except Exception:
            phrase_data = []
    _pos_phrases = [
        p for p in phrase_data
        if (p.get("sentiment_weights") or {}).get("positive", 0) >= 0.5
    ]
    _neg_phrases = [
        p for p in phrase_data
        if (p.get("sentiment_weights") or {}).get("negative", 0) >= 0.5
    ]
    top_phrases = {
        "positive": _pos_phrases[:8],
        "negative": _neg_phrases[:8],
    }

    # F-015 P2 / F-021（2026-08-26）：主题层——短语编码归并（维度关键词 + 品牌别名，
    # 保守）；仅 LLM 模式生成（词典模式回退单词，见上）
    if plan.llm_enabled:
        try:
            _schema = task_schema(plan)
            _dim_kw: dict[str, list[str]] = {}
            if _schema is not None:
                for _d in _schema.dimensions:
                    _kw = list(getattr(_d, "keywords", []) or [])
                    if _kw:
                        _dim_kw[_d.id] = _kw
            from app.coding.cleaner import BRAND_ALIASES
            _aliases: dict[str, str] = {}
            for _std, _alist in BRAND_ALIASES.items():
                for _a in _alist:
                    _aliases[_a] = _std
                _aliases[_std] = _std
            topics = encode_phrases(phrase_data, _dim_kw, _aliases, SYNONYM_GROUPS)
        except Exception:
            topics = []

    # 主题/泛词过滤（词云与共现网络共用）
    extra_stop = _subject_stopwords(plan)

    # 情感一致性：正负文本混用的词（角色名/地名/泛词）信号≈0，
    # 从词云与共现网络剔除；权重 = 词频 × 情感强度
    tok_pos: dict[str, float] = defaultdict(float)
    tok_neg: dict[str, float] = defaultdict(float)
    tok_cnt: Counter[str] = Counter()
    for it in stat_items:
        sc = it.sentiment_score
        for tok in set(segment(it.text, extra_stop)):
            tok_cnt[tok] += 1
            if sc >= 0:
                tok_pos[tok] += sc
            else:
                tok_neg[tok] += -sc
    signal: dict[str, float] = {}
    for tok in tok_cnt:
        denom = tok_pos[tok] + tok_neg[tok]
        signal[tok] = (tok_pos[tok] - tok_neg[tok]) / denom if denom > 0 else 0.0

    def _cloud_weight(tok: str) -> float:
        """词云权重：情感信号强，或弱信号但有词典极性（如"喜欢"）才保留。"""
        sig = signal.get(tok, 0.0)
        pol = lexicon_v2.word_polarity(tok)
        if sig > 0 and (sig >= 0.25 or (pol > 0 and sig >= 0.1)):
            pass
        elif sig < 0 and (abs(sig) >= 0.25 or (pol < 0 and abs(sig) >= 0.1)):
            pass
        else:
            return 0.0
        return (tok_pos[tok] + tok_neg[tok]) * min(tok_cnt[tok], 20)

    pos_cloud = Counter(
        {
            tok: _cloud_weight(tok) * signal[tok]
            for tok in tok_cnt
            if signal[tok] >= 0.25
            or (
                signal[tok] > 0
                and lexicon_v2.word_polarity(tok) > 0
                and signal[tok] >= 0.1
            )
        }
    )
    neg_cloud = Counter(
        {
            tok: _cloud_weight(tok) * abs(signal[tok])
            for tok in tok_cnt
            if signal[tok] <= -0.25
            or (
                signal[tok] < 0
                and lexicon_v2.word_polarity(tok) < 0
                and abs(signal[tok]) >= 0.1
            )
        }
    )

    # 负面率最高维度（样本 ≥3）的负面词云
    worst_dim = ""
    valid_dims = [d for d, v in dim_stats.items() if v["count"] >= 3]
    if valid_dims:
        worst_dim = max(
            valid_dims, key=lambda d: dim_stats[d]["negative"] / dim_stats[d]["count"]
        )
    worst_cloud: Counter[str] = Counter()
    if worst_dim:
        for it in stat_items:
            if it.dimension_sentiments.get(worst_dim) == "negative":
                for tok in set(segment(it.text, extra_stop)):
                    sig = signal.get(tok, 0.0)
                    if sig <= -0.25 or (
                        sig < 0 and lexicon_v2.word_polarity(tok) < 0
                    ):
                        worst_cloud[tok] += abs(it.sentiment_score)

    # 情绪来源话题榜（数据驱动，不依赖词典）：话题词在正面/负面评论中的占比
    pos_docs: Counter[str] = Counter()
    neg_docs: Counter[str] = Counter()
    for it in stat_items:
        toks = set(segment(it.text, extra_stop))
        if it.sentiment == SentimentLabel.positive:
            for t in toks:
                pos_docs[t] += 1
        elif it.sentiment == SentimentLabel.negative:
            for t in toks:
                neg_docs[t] += 1
    sentiment_sources = []
    for t in pos_docs | neg_docs:
        p, n = pos_docs[t], neg_docs[t]
        if p + n >= 5:
            sentiment_sources.append(
                {
                    "word": t,
                    "positive": p,
                    "negative": n,
                    "negative_rate": round(n / (p + n), 3),
                }
            )
    sentiment_sources.sort(
        key=lambda r: (-r["negative_rate"], -(r["positive"] + r["negative"]))
    )
    sentiment_sources = sentiment_sources[:12]

    # 共现网络（F-019：优先主题名节点，与代表观点/词云同一主题口径；
    # 主题不足回退短语节点 F-009；再回退单词级）
    phrase_edges: list[dict] = []
    phrase_node_count: dict[str, int] = {}
    _co_nodes: list[dict] = []
    if topics and len(topics) >= 2:
        _co_nodes = [{"phrase": t["name"]} for t in topics]
    elif phrase_data and len(phrase_data) >= 2:
        _co_nodes = phrase_data
    if _co_nodes:
        try:
            phrase_edges, phrase_node_count = build_phrase_cooccurrence(
                content_texts, _co_nodes, top_n=30, min_count=1,
            )
        except Exception:
            phrase_edges, phrase_node_count = [], {}
    if phrase_edges:
        cooccurrence_raw = phrase_edges
        node_count = phrase_node_count
    else:
        cooccurrence_raw, node_count = build_cooccurrence(
            content_texts,
            window=3,
            top_n=30,
            extra_stopwords=extra_stop,
            min_count=3,
            return_counts=True,
        )
    # 方案 Part B（2026-08-19）：节点 top20（按文档频次）+ 固定取 PMI 前 20 条边
    # （不追节点数——门槛判定交给 build_topic_clusters：边≥12 且 节点≥15 才聚类，
    #  否则走词对榜；追节点数会自相矛盾地把小图扩到门槛以上）。
    top_nodes = set(
        sorted(node_count, key=lambda n: (-node_count[n], n))[:20]
    )
    cooccurrence = [
        e for e in cooccurrence_raw
        if e["source"] in top_nodes and e["target"] in top_nodes
    ]
    cooccurrence = cooccurrence[:20]
    node_set = {e["source"] for e in cooccurrence} | {e["target"] for e in cooccurrence}
    # 话题簇（2026-08-19 修订）：簇文档数 = 至少含簇内任一节点词的独立文本数
    # （并集）。词频求和会重复计数（OPPO"数码"簇：29 vs 独立文本 12~13），
    # 因此必须在有文本的 build_summary 层计算，渲染层只消费结果。
    topic_clusters = build_topic_clusters(
        cooccurrence,
        node_count,
        content_texts,
        [it.sentiment.value for it in stat_items],
        extra_stopwords=extra_stop,
    )
    w_dims: dict[str, Counter] = defaultdict(Counter)
    for it in stat_items:
        for tok in segment(it.text, extra_stop):
            if tok in node_set:
                for dim in it.dimensions:
                    w_dims[tok][dim] += 1
    word_dims = {w: c.most_common(1)[0][0] for w, c in w_dims.items() if c}
    node_negative_rate = {
        t: round(neg_docs[t] / (neg_docs[t] + pos_docs[t]), 3)
        for t in node_set
        if neg_docs[t] + pos_docs[t] > 0
    }

    for post in posts:
        ks = kw_stats[post.keyword or "未分类"]
        ks["posts"] += 1
        ks["comments"] += len(post.comments)

    avg = sum(it.sentiment_score for it in stat_items) / stat_n if stat_n else 0.0
    narr_stats = _build_narrative_stats(stat_items)
    summary = {
        "total_items": total,
        "total_posts": len(posts),
        "ads": {
            "count": ads_count,
            "ratio_of_total": round(ads_count / total, 4) if total else 0,
            "excluded": exclude_ad,
            "stat_n": stat_n,
            "mode": "已从情感统计剔除" if exclude_ad else "计入（含广告/官方内容）",
        },
        "avg_score": round(avg, 4),
        "overall_sentiment": "正面" if avg > 0.15 else ("负面" if avg < -0.15 else "中性"),
        "sentiment_distribution": {
            k: {"count": sent_counter[k],
                "ratio": round(sent_counter[k] / stat_n, 4) if stat_n else 0}
            for k in ["positive", "negative", "neutral"]
        },
        "platforms": {
            pid: {
                "posts": v["posts"],
                "avg_score": round(sum(v["scores"]) / len(v["scores"]), 4) if v["scores"] else 0,
                "positive": v["positive"],
                "negative": v["negative"],
                "neutral": v["neutral"],
            }
            for pid, v in platform_stats.items()
        },
        "dimensions": {
            did: {
                "count": v["count"],
                "negative": v["negative"],
                "negative_rate": round(v["negative"] / v["count"], 4) if v["count"] else 0,
                "avg_score": round(sum(v["scores"]) / len(v["scores"]), 4) if v["scores"] else 0,
            }
            for did, v in dim_stats.items()
        },
        "intensity_distribution": {
            str(level): intensity_counter[level] for level in range(1, 6)
        },
        "platform_dim": {
            pid: {
                did: {
                    "count": v["count"],
                    "negative": v["negative"],
                    "negative_rate": round(v["negative"] / v["count"], 4)
                    if v["count"]
                    else 0,
                }
                for did, v in dims.items()
            }
            for pid, dims in platform_dim.items()
        },
        "date_dim": {
            d: {
                did: {
                    "count": v["count"],
                    "negative": v["negative"],
                    "negative_rate": round(v["negative"] / v["count"], 4)
                    if v["count"]
                    else 0,
                }
                for did, v in dims.items()
            }
            for d, dims in sorted(date_dim.items())
        },
        "trend": {
            d: {
                "count": v["count"],
                "avg_score": round(sum(v["scores"]) / len(v["scores"]), 4) if v["scores"] else 0,
                "negative": v["negative"],
            }
            for d, v in sorted(trend.items())
        },
        "top_words": all_keywords.most_common(20),
        "top_content_words": build_word_freq(
            content_texts, top_n=50, extra_stopwords=extra_stop
        ),
        "cooccurrence": cooccurrence,
        "topic_clusters": topic_clusters,
        "node_count": {n: node_count[n] for n in node_set},
        "node_negative_count": {n: neg_docs[n] for n in node_set},
        "node_positive_count": {n: pos_docs[n] for n in node_set},
        "sentiment_sources": sentiment_sources,
        "node_negative_rate": node_negative_rate,
        "top_phrases": top_phrases,
        "topics": topics,
        "positive_words": (
            [(p["phrase"], p["count"]) for p in _pos_phrases[:10]]
            if _pos_phrases else pos_w.most_common(10)
        ),
        "negative_words": (
            [(p["phrase"], p["count"]) for p in _neg_phrases[:10]]
            if _neg_phrases else neg_w.most_common(10)
        ),
        "positive_wordcloud": (
            [(t["name"].replace(" ", "\u3000"), t["count"]) for t in topics
             if t.get("polarity") == "positive"][:40]
            if topics else (
                [(p["phrase"].replace(" ", "\u3000"), p["count"]) for p in _pos_phrases[:40]]
                if _pos_phrases else pos_cloud.most_common(40)
            )
        ),
        "negative_wordcloud": (
            [(t["name"].replace(" ", "\u3000"), t["count"]) for t in topics
             if t.get("polarity") == "negative"][:40]
            if topics else (
                [(p["phrase"].replace(" ", "\u3000"), p["count"]) for p in _neg_phrases[:40]]
                if _neg_phrases else neg_cloud.most_common(40)
            )
        ),
        "worst_dim_id": worst_dim,
        "worst_dim_wordcloud": (
            [(t["name"].replace(" ", "\u3000"), t["count"]) for t in topics
             if t.get("polarity") == "negative"][:40]
            if topics else (
                [(p["phrase"].replace(" ", "\u3000"), p["count"]) for p in _neg_phrases[:40]]
                if _neg_phrases else worst_cloud.most_common(40)
            )
        ),
        "word_dims": word_dims,
        "narrative_stats": narr_stats,
        "keyword_stats": {
            kw: {
                "posts": v["posts"],
                "comments": v["comments"],
                "coded": v["coded"],
                "stat": v["stat"],
                "positive": v["positive"],
                "negative": v["negative"],
                "neutral": v["neutral"],
                "negative_rate": round(
                    v["negative"] / (v["stat"] or v["coded"]), 4
                )
                if (v["stat"] or v["coded"])
                else 0,
            }
            for kw, v in kw_stats.items()
        },
    }
    return summary


NO_DATA_REPORT_TEXT = (
    "本次分析未采集到有效文本，无法生成情感结论。\n"
    "建议：检查关键词是否过窄或拼写有误、增加渠道、放宽时间段后重试；"
    "若仍无数据，可在任务详情查看各渠道的失败原因与采集日志。"
)


def generate_report_text(
    plan: AnalysisPlan, summary: dict, findings: list[dict] | None = None
) -> str:
    """概览文案（数据驱动；结尾指向核心发现，不使用万能句）。"""
    lines = []
    lines.append(f"本次分析对象为「{plan.subject}」，共采集 {summary['total_posts']} 条内容，"
                 f"编码 {summary['total_items']} 条文本。")
    ads = summary.get("ads") or {}
    if ads.get("count"):
        tail = (
            f"该部分已从情感统计剔除，情感分布基于其余 {ads.get('stat_n', 0)} 条文本计算"
            if ads.get("excluded")
            else "该部分计入情感统计（广告也是消费者可见的市场信号）"
        )
        lines.append(
            f"其中广告/官方内容 {ads['count']} 条（占总文本 {ads.get('ratio_of_total', 0):.1%}），"
            f"{tail}。"
        )
    dist = summary["sentiment_distribution"]
    lines.append(
        f"整体情感倾向为{summary['overall_sentiment']}（均分 {summary['avg_score']}）："
        f"正面 {dist['positive']['count']} 条（{dist['positive']['ratio'] * 100:.1f}%）、"
        f"负面 {dist['negative']['count']} 条（{dist['negative']['ratio'] * 100:.1f}%）、"
        f"中性 {dist['neutral']['count']} 条（{dist['neutral']['ratio'] * 100:.1f}%）。"
    )
    dims = summary["dimensions"]
    if dims:
        valid = [(did, v) for did, v in dims.items() if v["count"] >= MIN_DIM_RATING]
        if valid:
            worst = max(valid, key=lambda kv: kv[1]["negative_rate"])
            marker = "" if worst[1]["count"] >= MIN_DIM_NORMAL else "，样本有限"
            lines.append(
                f"负面率最高的维度是「{dimension_cn(worst[0])}」"
                f"（{worst[1]['negative_rate'] * 100:.1f}%，n={worst[1]['count']}{marker}），"
                "建议重点关注。"
            )
        else:
            lines.append("各维度样本均不足（n<3），负面率最高维度未评级。")
    sources = [
        r
        for r in (summary.get("sentiment_sources") or [])
        if r["negative_rate"] > 0.5
        and r["positive"] + r["negative"] >= MIN_DIM_RATING
    ]
    if sources:
        top = "、".join(
            f"{r['word']}（负面 {r['negative_rate'] * 100:.0f}%，n={r['positive'] + r['negative']}）"
            for r in sources[:3]
        )
        lines.append(f"负面情绪来源话题：{top}。")
    tp = summary.get("top_phrases") or {}
    tp_pos = tp.get("positive") or []
    tp_neg = tp.get("negative") or []
    if tp_pos or tp_neg:
        top = "、".join(
            f"{p['phrase']}（{p['count']} 条）" for p in (tp_pos + tp_neg)[:5]
        )
        lines.append(f"代表观点（短语）：{top}。")
    elif summary["top_words"]:
        top = "、".join(w for w, _ in summary["top_words"][:5])
        lines.append(f"代表观点（短语）：样本少，未形成短语，回退单词词频：{top}。")
    if findings:
        ids = "、".join(display_finding_id(f.get("id", "")) for f in findings[:3])
        lines.append(f"具体证据与行动建议见下方「核心发现」（{ids} 等）。")
    else:
        # F-018（2026-08-26，修订版）：词典模式单区合并——解读区承载统计结论
        lines.append(
            "具体解读见下方「解读与建议」区；"
            "开启 LLM 精分析可获得可归因的结论与行动建议。"
        )
    return "\n".join(lines)


class TaskRunner:
    """一次分析任务的执行器（同步执行 + 进度回调；V2 迁入后台任务队列）。"""

    def __init__(self, plan: AnalysisPlan, on_progress: ProgressCallback | None = None):
        self.plan = plan
        self.on_progress = on_progress
        self.cancel_event = threading.Event()
        steps = [
            ("collect", "采集数据"),
            ("clean", "清洗与去重"),
            ("lexicon", "词典预筛"),
            ("llm", "LLM 精分析"),
            ("narrative", "叙事/归因分析"),
            ("report", "生成报告"),
        ]
        self.tracker = ProgressTracker(steps)
        if not plan.llm_enabled:
            self.tracker.step("llm", state="skipped", detail="未启用")
        if not plan.narrative_enabled:
            self.tracker.step("narrative", state="skipped", detail="未启用")
        # 每个阶段映射到整体进度条的一段区间：(start, end)
        self._phase_weights = {
            "collecting": (0.0, 0.5),
            "cleaning": (0.5, 0.6),
            "coding": (0.6, 0.9),
            "reporting": (0.9, 1.0),
        }

    def cancel(self) -> None:
        self.cancel_event.set()

    def _progress(self, status: TaskStatus, message: str, phase_progress: float) -> None:
        phase_progress = min(max(phase_progress, 0.0), 1.0)
        # 单调兜底：进度条永不倒退（即使未来某段区间算错，也只卡住不乱跳）
        phase_progress = max(phase_progress, self.tracker.overall)
        self.tracker.overall = phase_progress
        self.tracker.message = message
        if self.on_progress:
            self.on_progress(status, message, phase_progress, self.tracker.snapshot())

    def run(self, analyzer: BaseAnalyzer | None = None) -> ReportBundle:
        """完整流水线：采集+清洗 → 编码+报告（兼容原调用）。"""
        res = self.collect_and_clean(analyzer=analyzer)
        return self.code_and_report(
            res["posts"], res["channel_results"], res["warnings"],
            analyzer=res["analyzer"],
        )

    def restore_tracker(self, snapshot: dict | None) -> None:
        """续跑时恢复阶段1的任务清单状态（collect/clean 已 done）。"""
        if not snapshot:
            return
        for sid, s in (snapshot.get("steps") or {}).items():
            cur = self.tracker.get(sid)
            if cur is not None:
                cur.state = s.get("state", cur.state)
                cur.detail = s.get("detail", cur.detail)
                try:
                    cur.frac = float(s.get("frac", cur.frac))
                except (TypeError, ValueError):
                    pass

    def collect_and_clean(
        self, analyzer: BaseAnalyzer | None = None, review_mode: bool = False
    ) -> dict:
        """阶段1：采集 + 清洗。

        review_mode=True（人工筛选启用）时，LLM 相关性复核只记录标注
        （llm_relevant_by_url）不剔除，最终由人工决定。
        """
        plan = self.plan
        posts: list[Post] = []
        channel_results = []
        warnings: list[str] = []
        llm_relevant_by_url: dict[str, bool] = {}

        # 1. 采集（多渠道并行，失败自动降级；整体进度按完成渠道数单调推进）
        self.tracker.step("collect", state="running", detail="开始采集", frac=0.0)
        self._progress(TaskStatus.collecting, "开始采集", 0.0)
        collect_start, collect_end = self._phase_weights["collecting"]
        collect_span = collect_end - collect_start
        channel_configs = list(plan.channels)
        results: list = [None] * len(channel_configs)
        lock = threading.Lock()
        completed = 0
        channel_fracs = [0.0] * len(channel_configs)  # 各渠道自身进度，整体=求和（单调）

        def _safe_collect(channel_cfg, idx: int):
            channel = get_channel(channel_cfg.channel_id)

            def _thread_progress(msg, p, c=channel):
                with lock:
                    frac = min(max(float(p or 0.0), 0.0), 1.0)
                    channel_fracs[idx] = max(channel_fracs[idx], frac)
                    done = sum(1 for f in channel_fracs if f >= 1.0)
                    mean_frac = sum(channel_fracs) / max(len(channel_configs), 1)
                    overall = collect_start + collect_span * (
                        mean_frac
                    )
                    self.tracker.channel_state(c.name, msg, frac)
                    self.tracker.step(
                        "collect",
                        state="running",
                        detail=f"采集中：{done}/{len(channel_configs)} 渠道完成",
                        frac=mean_frac,
                    )
                    self._progress(
                        TaskStatus.collecting,
                        f"采集中：{done}/{len(channel_configs)} 渠道完成",
                        overall,
                    )

            return channel, channel.collect(
                plan, on_progress=_thread_progress, cancel_event=self.cancel_event
            )

        with ThreadPoolExecutor(max_workers=min(4, max(len(channel_configs), 1))) as pool:
            futures = {
                pool.submit(_safe_collect, cfg, i): i
                for i, cfg in enumerate(channel_configs)
                if not self.cancel_event.is_set()
            }
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    channel, result = fut.result()
                except Exception as exc:  # 单渠道异常 → 降级
                    channel = get_channel(channel_configs[i].channel_id)
                    result = degraded_result(channel.id, f"采集异常: {exc}")
                results[i] = result
                warnings.extend(
                    f"[{channel.name}] {w}" for w in (result.warnings or [])
                )
                completed += 1
                with lock:
                    channel_fracs[i] = 1.0
                    mean_frac = sum(channel_fracs) / max(len(channel_configs), 1)
                    overall = collect_start + collect_span * mean_frac
                if result.ok:
                    posts.extend(result.posts)
                else:
                    warnings.append(f"[{channel.name}] {result.error}")
                    self.tracker.step(
                        "collect",
                        detail=(
                            f"{channel.name}：{result.error[:40]}"
                            f"{'…' if len(result.error) > 40 else ''}"
                        ),
                    )
                if result.error and "Cookie" in result.error:
                    warnings.append("关键提示：请更新认证信息后重试该渠道")
                self._progress(
                    TaskStatus.collecting,
                    f"采集中：{completed}/{len(channel_configs)} 渠道完成",
                    overall,
                )
        channel_results = [r for r in results if r is not None]
        self.tracker.step(
            "collect",
            state="done",
            detail=f"完成 {len(plan.channels)} 个渠道，共 {len(posts)} 帖",
            frac=1.0,
        )

        # 分析器创建与自检（提前，供清洗阶段的 LLM 相关性复核使用）
        analyzer = analyzer or create_analyzer()
        if not plan.llm_enabled:
            # LLM 未开启时一律词典模式，避免环境变量/本机残留 Key 误触发计费
            analyzer = MockAnalyzer()
        if plan.llm_enabled and isinstance(analyzer, OpenAICompatibleAnalyzer):
            ok, msg = analyzer.ping()
            if not ok:
                warnings.append(f"LLM 连接失败：{msg}，已自动降级为词典模式")
                self.tracker.step(
                    "llm", state="failed", detail=f"连接失败：{msg}，已降级为词典", frac=0.0
                )
                self.tracker.step("narrative", state="skipped", detail="LLM 不可用")
                analyzer = MockAnalyzer()
        elif plan.llm_enabled and not isinstance(analyzer, OpenAICompatibleAnalyzer):
            self.tracker.step(
                "llm", state="skipped", detail="未配置 API Key，使用词典模式"
            )
            self.tracker.step("narrative", state="skipped", detail="LLM 不可用")
        self._analyzer_ready = True

        # 2. 清洗与去重（记录丢弃原因；可选 LLM 相关性复核；丢弃率>15% 补采）
        self.tracker.step("clean", state="running", detail="正在清洗", frac=0.0)
        self._progress(TaskStatus.cleaning, "正在清洗与去重", self._phase_weights["cleaning"][0])
        for round_i in range(3):
            original_urls_by_channel = {
                ch.channel_id: {p.url for p in ch.posts} for ch in channel_results
            }
            posts = [p for ch in channel_results for p in ch.posts]
            posts, dropped = clean_posts(
                posts, subject=plan.subject, keywords=plan.keywords
            )
            if plan.relevance_check_enabled and isinstance(
                analyzer, OpenAICompatibleAnalyzer
            ):
                self.tracker.step(
                    "clean", detail="LLM 相关性复核中（可选步骤，按量计费）", frac=0.5
                )
                self._progress(
                    TaskStatus.cleaning,
                    "LLM 相关性复核中（可选步骤，按量计费）",
                    self._phase_weights["cleaning"][0] + 0.5 * 0.1,
                )
                flags = analyzer.check_relevance(
                    plan.subject, [f"{p.title} {p.content}" for p in posts]
                )
                if review_mode:
                    # 人工筛选模式：只标注建议，不剔除
                    for post, flag in zip(posts, flags):
                        llm_relevant_by_url[post.url] = bool(flag)
                else:
                    kept2: list[Post] = []
                    for post, flag in zip(posts, flags):
                        if flag:
                            kept2.append(post)
                        else:
                            dropped.append(
                                {
                                    "platform": post.platform,
                                    "url": post.url,
                                    "title": (post.title or post.content)[:80],
                                    "keyword": post.keyword,
                                    "query": (post.platform_specific or {}).get("query", ""),
                                    "reason": "LLM 相关性复核：不相关/无意义",
                                    "kind": "quality",
                                }
                            )
                    posts = kept2
            # 渠道结果写回：原始数据 sheet 只展示清洗保留的帖子，丢弃记录进 dropped
            kept_urls = {p.url for p in posts}
            for ch in channel_results:
                ch.posts = [p for p in ch.posts if p.url in kept_urls]
                merged_dropped = list(ch.dropped or [])
                merged_dropped.extend(
                    d
                    for d in dropped
                    if d["url"] in original_urls_by_channel.get(ch.channel_id, set())
                    or d["platform"] == ch.channel_id
                )
                ch.dropped = merged_dropped

            # 丢弃率 >15% 且保留不足 → 补采（最多 TOPUP_MAX_ROUNDS 轮）
            need_topup = False
            no_progress = False
            low_yield_stop = False
            for ch in channel_results:
                if not ch.ok or getattr(ch, "risk", False):
                    continue
                collected = len(ch.posts) + len(ch.dropped)
                if collected <= 0:
                    continue
                # 2026-08-19（口径结构化）：补采触发只看 kind=quality——
                # collection/duplicate（重复返回/超窗/广告/去重）重采同一池子
                # 只会形成回路（瑞幸×B站实测 44 条重复触发补采后反复重判）。
                quality_dropped = [
                    d for d in (ch.dropped or [])
                    if is_quality_drop(d)
                    and d.get("reason", "") != "疑似不相关（信息不足）"
                ]
                drop_rate = len(quality_dropped) / collected
                limit = plan.per_keyword_limit
                for cfg in plan.channels:
                    if cfg.channel_id == ch.channel_id:
                        limit = int(cfg.params.get("limit") or limit)
                target = limit * max(len(plan.keywords), 1)
                if (
                    drop_rate > 0.15
                    and ch.channel_id != "demo"  # demo 为确定性数据，补采无意义
                    and len(ch.posts) < target
                ):
                    need_topup = True
                    new_limit = min(
                        math.ceil(target / 0.85), limit * TOPUP_LIMIT_MULTIPLIER
                    )
                    prev_posts = len(ch.posts)
                    new_plan = plan.model_copy(deep=True)
                    for cfg in new_plan.channels:
                        if cfg.channel_id == ch.channel_id:
                            cfg.params["limit"] = new_limit
                    channel = get_channel(ch.channel_id)
                    # D：冷却/暂停/超配额渠道跳过补采；小红书高风险先提示
                    if ch.channel_id == "xiaohongshu":
                        warnings.append(
                            "小红书触发补采：将追加搜索请求（高风险渠道），"
                            "如遇风控将自动停止"
                        )
                    try:
                        from app.core import jobs

                        extra_ok, extra_reason = jobs.check_channel_allowed(
                            ch.channel_id, 0
                        )
                    except Exception:
                        extra_ok, extra_reason = True, ""
                    if not extra_ok:
                        warnings.append(f"{channel.name} 跳过补采：{extra_reason}")
                        break
                    # A：跳过本任务已采集内容，避免同关键词重复拉取
                    if getattr(channel, "skip_key", "url") == "id":
                        skip_keys = {p.id for p in ch.posts if p.id}
                    else:
                        skip_keys = {p.url for p in ch.posts if p.url} | {
                            d.get("url", "") for d in (ch.dropped or []) if d.get("url")
                        }
                    try:
                        result = channel.collect(
                            new_plan,
                            on_progress=lambda msg, p, c=channel, r=round_i: (
                                self._topup_progress(c.name, msg, p, r)
                            ),
                            cancel_event=self.cancel_event,
                            skip_urls=skip_keys,
                        )
                    except Exception as exc:
                        result = degraded_result(channel.id, f"补采异常: {exc}")
                    # B：每轮补采立即清洗，按净新增保留数判定收益
                    cleaned_topup, topup_dropped = clean_posts(
                        result.posts, subject=plan.subject, keywords=plan.keywords
                    )
                    existing_keys = {p.url for p in ch.posts if p.url} | {
                        d.get("url", "") for d in (ch.dropped or []) if d.get("url")
                    }
                    net_new = [p for p in cleaned_topup if p.url not in existing_keys]
                    yield_threshold = max(
                        TOPUP_MIN_YIELD, math.ceil(target * TOPUP_YIELD_RATIO)
                    )
                    if len(net_new) < yield_threshold:
                        low_yield_stop = True
                        no_progress = True
                        warnings.append(
                            f"{channel.name} 补采收益低（净新增 {len(net_new)} 条 "
                            f"< {yield_threshold}），已停止补采"
                        )
                        ch.dropped = list(ch.dropped or []) + topup_dropped
                        self.tracker.step(
                            "clean",
                            detail=f"{channel.name} 补采收益低，已停止",
                            frac=0.5 + 0.5 * (round_i + 1) / TOPUP_MAX_ROUNDS,
                        )
                        break
                    # 合并而非替换：skip 后补采结果不含第一轮已保留帖
                    ch.posts = list(ch.posts) + list(result.posts)
                    ch.dropped = list(ch.dropped or []) + list(result.dropped or [])
                    ch.ok = result.ok
                    if len(ch.posts) <= prev_posts:
                        no_progress = True
                    self.tracker.step(
                        "clean",
                        detail=(
                            f"{channel.name} 丢弃率 {drop_rate:.0%} >15%，"
                            f"补采至 {new_limit}/关键词（第 {round_i + 1} 轮）"
                        ),
                        frac=0.5 + 0.5 * (round_i + 1) / TOPUP_MAX_ROUNDS,
                    )
                    break
            if no_progress:
                if not low_yield_stop:
                    warnings.append(
                        "部分渠道补采未新增有效内容，已停止补采（数据源可获取量有限）"
                    )
                break
            if not need_topup:
                break
        else:
            warnings.append(
                f"部分渠道丢弃率补采 {TOPUP_MAX_ROUNDS} 轮后仍高于 15%，"
                "已在丢弃明细标注原因；"
                "该渠道结果可能不完整，其余渠道不受影响"
            )
        # 兜底遥测（2026-08-19）：统计无 kind 的丢弃记录（旧数据/漏标），
        # 任务级单条汇总告警，避免字符串兜底路径静默无限期使用。
        unkinded_drops = sum(
            1
            for ch in channel_results
            for d in (ch.dropped or [])
            if not str(d.get("kind") or "").strip()
        )
        if unkinded_drops:
            warnings.append(
                f"丢弃口径：{unkinded_drops} 条记录缺 kind"
                "（旧任务数据或漏标），已按原因文案兜底判定"
            )
        # 补采一致性收尾：最终清洗并统一写回，保证 posts 与 channel_results 同源
        # （修复补采原始帖残留导致"报告 0 帖但漏斗有数"）
        posts = _reconcile_channel_posts(channel_results, plan, warnings)
        self.tracker.step(
            "collect", state="done", detail=f"完成 {len(plan.channels)} 个渠道", frac=1.0
        )
        self.tracker.step(
            "clean",
            state="done",
            detail=f"清洗保留 {len(posts)} 帖，丢弃 "
            f"{sum(len(ch.dropped) for ch in channel_results)} 帖",
            frac=1.0,
        )
        self._progress(
            TaskStatus.cleaning,
            f"清洗完成：保留 {len(posts)} 帖",
            self._phase_weights["cleaning"][1],
        )

        return {
            "posts": posts,
            "channel_results": channel_results,
            "warnings": warnings,
            "analyzer": analyzer,
            "llm_relevant_by_url": llm_relevant_by_url,
            "tracker_snapshot": self.tracker.snapshot(),
        }

    def code_and_report(
        self,
        posts: list[Post],
        channel_results: list[ChannelResult],
        warnings: list[str],
        analyzer: BaseAnalyzer | None = None,
    ) -> ReportBundle:
        """阶段2：编码 + 汇总报告（可基于人工筛选后的数据续跑）。"""
        plan = self.plan
        if not getattr(self, "_analyzer_ready", False):
            analyzer = analyzer or create_analyzer()
            if not plan.llm_enabled:
                analyzer = MockAnalyzer()
            if plan.llm_enabled and isinstance(analyzer, OpenAICompatibleAnalyzer):
                ok, msg = analyzer.ping()
                if not ok:
                    warnings.append(f"LLM 连接失败：{msg}，已自动降级为词典模式")
                    self.tracker.step(
                        "llm", state="failed",
                        detail=f"连接失败：{msg}，已降级为词典", frac=0.0,
                    )
                    self.tracker.step("narrative", state="skipped", detail="LLM 不可用")
                    analyzer = MockAnalyzer()
            elif plan.llm_enabled and not isinstance(analyzer, OpenAICompatibleAnalyzer):
                self.tracker.step(
                    "llm", state="skipped", detail="未配置 API Key，使用词典模式"
                )
                self.tracker.step("narrative", state="skipped", detail="LLM 不可用")
            self._analyzer_ready = True

        # 3. 编码
        coding_start, coding_end = self._phase_weights["coding"]
        coding_span = coding_end - coding_start
        self._progress(TaskStatus.coding, "正在情感编码（词典预筛 + LLM）", coding_start)
        schema = None
        if plan.domain_id or plan.custom_dimensions:
            # 2.8：预置/模块 schema + 任务级自定义维度合并（id custom_ 前缀）
            schema = task_schema(plan)
            if schema is None and plan.domain_id:
                warnings.append(f"领域 schema 不存在：{plan.domain_id}，跳过维度分析")
        coder = Coder(analyzer, schema=schema)
        items = coder.code_posts(
            posts,
            plan,
            on_progress=self._coder_progress,
        )
        if plan.llm_enabled and self.tracker.get("llm").state != "failed":
            self.tracker.step("llm", state="done", frac=1.0)
        if plan.narrative_enabled:
            self.tracker.step("narrative", state="done", frac=1.0)
        self.tracker.step("lexicon", state="done", frac=1.0)
        self._progress(TaskStatus.coding, "编码完成", coding_end)
        if getattr(analyzer, "errors", None):
            warnings.extend(f"LLM 提示：{err}" for err in analyzer.errors[:10])

        # 4. 汇总与报告文本
        self.tracker.step("report", state="running", detail="正在生成统计与报告", frac=0.0)
        self._progress(TaskStatus.reporting, "正在生成统计与报告", self._phase_weights["reporting"][0])
        register_custom_dim_names(plan)  # 2.8：任务级自定义维度显示名（图表/洞察共用）
        summary = recompute_summary(plan, items, channel_results, posts)
        llm_usage: dict = {}
        if hasattr(analyzer, "usage"):
            u = analyzer.usage
            llm_usage = {
                "prompt_tokens": int(u.get("prompt_tokens") or 0),
                "completion_tokens": int(u.get("completion_tokens") or 0),
                "estimated_cost": cost_from_usage(
                    int(u.get("prompt_tokens") or 0),
                    int(u.get("completion_tokens") or 0),
                ),
            }
        warnings.extend(_quality_gate_warnings(summary["total_items"]))
        self.tracker.step(
            "report", detail="正在生成证据链与报告内容", frac=0.5
        )
        if summary["total_items"] == 0:
            # 零数据短路：不调 LLM、不生成任何推断/建议（病灶 F）
            evidence: list[dict] = []
            report_content = {
                "chart_insights": {},
                "conclusion": "",
                "findings": [],
                "insight_mode": "no_data",
            }
            report_text = NO_DATA_REPORT_TEXT
        else:
            evidence = build_evidence(
                items, summary, exclude_ad=bool(plan.exclude_ad_enabled)
            )
            report_content = build_report_content(analyzer, plan, summary, evidence)
            # F-021（2026-08-26）：LLM 编码分析工作流——LLM 归类 + 系统反算数字；
            # 成功后替换 summary.topics 并重算词云（主视觉/主题洞察四端一致）
            if (
                report_content.get("insight_mode") == "llm"
                and isinstance(analyzer, OpenAICompatibleAnalyzer)
                and items
            ):
                try:
                    from app.coding.coding_workflow import run_coding_workflow
                    _llm_topics = run_coding_workflow(analyzer, items, plan)
                    if _llm_topics:
                        summary["topics"] = _llm_topics
                        _pos_t = [
                            t for t in _llm_topics if t.get("polarity") == "positive"
                        ][:40]
                        _neg_t = [
                            t for t in _llm_topics if t.get("polarity") == "negative"
                        ][:40]
                        summary["positive_wordcloud"] = [
                            (t["name"].replace(" ", "\u3000"), t["count"])
                            for t in _pos_t
                        ]
                        summary["negative_wordcloud"] = [
                            (t["name"].replace(" ", "\u3000"), t["count"])
                            for t in _neg_t
                        ]
                        summary["worst_dim_wordcloud"] = summary["negative_wordcloud"]
                except Exception:
                    pass  # 工作流失败保持规则主题兜底
            report_text = generate_report_text(
                plan, summary, report_content["findings"]
            )
        self.tracker.step("report", state="done", detail="报告生成完毕", frac=1.0)
        self._progress(TaskStatus.reporting, "报告生成完毕", self._phase_weights["reporting"][1])

        return ReportBundle(
            plan=plan,
            channel_results=channel_results,
            coded_items=items,
            summary=summary,
            report_text=report_text,
            chart_insights=report_content["chart_insights"],
            conclusion=report_content["conclusion"],
            findings=report_content["findings"],
            evidence=evidence,
            insight_mode=report_content["insight_mode"],
            structured_summary=report_content.get("structured_summary") or {},
            structured_summary_source=report_content.get("structured_summary_source", "rule"),
            conclusion_text=report_content.get("conclusion_text", ""),
            llm_usage=llm_usage,
            warnings=warnings,
        )

    def _topup_progress(
        self, channel_name: str, msg: str, p: float, round_i: int
    ) -> None:
        """补采进度映射到清洗段后半（55%~60%），多轮单调推进，不回到采集段。"""
        clean_start, clean_end = self._phase_weights["cleaning"]
        clean_span = clean_end - clean_start
        frac = min(max(float(p or 0.0), 0.0), 1.0)
        sub = 0.5 + 0.5 * (
            min(round_i, TOPUP_MAX_ROUNDS - 1) + frac
        ) / TOPUP_MAX_ROUNDS
        self.tracker.step(
            "clean",
            state="running",
            detail=f"补采中（{channel_name}）：{msg}",
            frac=sub,
        )
        self._progress(
            TaskStatus.cleaning,
            f"补采中（{channel_name}）：{msg}",
            clean_start + clean_span * sub,
        )

    def _coder_progress(self, message: str, frac: float, step_id: str, step_frac: float) -> None:
        coding_start, coding_end = self._phase_weights["coding"]
        coding_span = coding_end - coding_start
        self.tracker.step(step_id, state="running", detail=message, frac=step_frac)
        self._progress(
            TaskStatus.coding, message, coding_start + coding_span * frac
        )


def bundle_to_json(bundle: ReportBundle, path: str | Path) -> None:
    """落盘完整结果（供历史记录/回看，V2 完善）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "plan": bundle.plan.model_dump(mode="json"),
        "channel_results": [
            ch.model_dump(mode="json") for ch in bundle.channel_results
        ],
        "summary": bundle.summary,
        "report_text": bundle.report_text,
        "chart_insights": bundle.chart_insights,
        "conclusion": bundle.conclusion,
        "findings": bundle.findings,
        "evidence": bundle.evidence,
        "insight_mode": bundle.insight_mode,
        "llm_usage": bundle.llm_usage,
        "warnings": bundle.warnings,
        "created_at": bundle.created_at.isoformat(),
        "coded_items": [it.model_dump(mode="json") for it in bundle.coded_items],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
