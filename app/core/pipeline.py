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
from app.coding.insights import build_insights
from app.coding.llm_analyzer import (
    BaseAnalyzer,
    MockAnalyzer,
    OpenAICompatibleAnalyzer,
    create_analyzer,
)
from app.coding.tokenizer import (
    GENERIC_NOUNS,
    build_cooccurrence,
    build_word_freq,
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
from app.core.pricing import cost_from_usage
from app.core.progress import ProgressTracker
from app.domains.loader import load_domain

ProgressCallback = Callable[[TaskStatus, str, float, dict], None]

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

    # 共现网络（文档级去重后的讨论结构；PMI 加权 + 主题过滤）
    cooccurrence = build_cooccurrence(
        content_texts,
        window=3,
        top_n=30,
        extra_stopwords=extra_stop,
        min_count=3,
    )
    node_set = {e["source"] for e in cooccurrence} | {e["target"] for e in cooccurrence}
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
        "sentiment_sources": sentiment_sources,
        "node_negative_rate": node_negative_rate,
        "positive_words": pos_w.most_common(10),
        "negative_words": neg_w.most_common(10),
        "positive_wordcloud": pos_cloud.most_common(40),
        "negative_wordcloud": neg_cloud.most_common(40),
        "worst_dim_id": worst_dim,
        "worst_dim_wordcloud": worst_cloud.most_common(40),
        "word_dims": word_dims,
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


def generate_report_text(plan: AnalysisPlan, summary: dict) -> str:
    """结论与建议（模板兜底；LLM 深度解读在 V1 接入）。"""
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
        worst = max(dims.items(), key=lambda kv: kv[1]["negative_rate"])
        lines.append(f"负面率最高的维度是「{worst[0]}」（{worst[1]['negative_rate'] * 100:.1f}%），建议重点关注。")
    if summary["top_words"]:
        top = "、".join(w for w, _ in summary["top_words"][:5])
        lines.append(f"高频情感词：{top}。")
    lines.append("建议：结合负面率最高的维度定位问题，扩大采集范围后复测以验证趋势。")
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
                                    "reason": "LLM 相关性复核：不相关",
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
                drop_rate = len(ch.dropped) / collected
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
        if plan.domain_id:
            try:
                schema = load_domain(plan.domain_id)
            except FileNotFoundError:
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
        summary = build_summary(plan, items, posts)
        # LLM 修正条数与 token 用量（费用可见性）
        summary["llm_corrected"] = sum(
            1
            for it in items
            if it.method == "llm"
            and it.lexicon_sentiment
            and it.lexicon_sentiment != it.sentiment.value
        )
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
            "report", detail="正在生成图表解析与深度结论（LLM）", frac=0.5
        )
        insights = build_insights(analyzer, plan, summary)
        report_text = generate_report_text(plan, summary)
        self.tracker.step("report", state="done", detail="报告生成完毕", frac=1.0)
        self._progress(TaskStatus.reporting, "报告生成完毕", self._phase_weights["reporting"][1])

        return ReportBundle(
            plan=plan,
            channel_results=channel_results,
            coded_items=items,
            summary=summary,
            report_text=report_text,
            chart_insights=insights["chart_insights"],
            conclusion=insights["conclusion"],
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
        "warnings": bundle.warnings,
        "created_at": bundle.created_at.isoformat(),
        "coded_items": [it.model_dump(mode="json") for it in bundle.coded_items],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
