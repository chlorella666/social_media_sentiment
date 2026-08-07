"""编码管道：词典预筛 → 低置信度 LLM 精分析 → 维度标签 → [可选]叙事/归因。"""

from __future__ import annotations

import time
from typing import Callable

from app.core.models import (
    AnalysisPlan,
    CodedItem,
    DomainSchema,
    NarrativeFrame,
    Post,
    SentimentLabel,
)
from app.coding import lexicon_v2 as lexicon
from app.coding.cleaner import clean_text
from app.coding.llm_analyzer import CONFIDENCE_THRESHOLD, OpenAICompatibleAnalyzer


def _intensity(score: float) -> int:
    return min(max(int(abs(score) * 5) + 1, 1), 5)


def _match_dimensions(text: str, schema: DomainSchema | None) -> list[str]:
    if not schema:
        return []
    matched: list[str] = []
    for dim in schema.dimensions:
        if any(kw.lower() in text.lower() for kw in dim.keywords):
            matched.append(dim.id)
    return matched


def _format_eta(elapsed: float, done: int, total: int) -> str:
    """根据已用时间和完成量估算剩余时间。"""
    if done <= 0 or total <= 0:
        return "估算中…"
    remain = elapsed * (total - done) / done
    if remain < 60:
        return f"约剩 {int(remain)} 秒"
    return f"约剩 {int(remain // 60)} 分 {int(remain % 60)} 秒"


class Coder:
    """编码管道编排。"""

    def __init__(self, analyzer: BaseAnalyzer, schema: DomainSchema | None = None):
        self.analyzer = analyzer
        self.schema = schema

    def code_posts(
        self,
        posts: list[Post],
        plan: AnalysisPlan,
        on_progress: Callable[[str, float, str, float], None] | None = None,
    ) -> list[CodedItem]:
        """对帖子正文和评论编码，返回 CodedItem 列表。"""
        items: list[CodedItem] = []
        llm_texts: list[str] = []
        llm_indices: list[tuple[int, str]] = []
        llm_available = isinstance(self.analyzer, OpenAICompatibleAnalyzer)
        total = sum(1 + len(p.comments) for p in posts)
        done = 0

        def _tick() -> None:
            nonlocal done
            done += 1
            if on_progress:
                note = "（低置信度将交给 LLM）" if llm_available else ""
                on_progress(
                    f"词典预筛 {done}/{total}{note}",
                    0.5 * (done / total if total else 0),
                    "lexicon",
                    done / total if total else 0,
                )

        # 第一遍：词典预筛
        for post in posts:
            content = clean_text(post.content)
            if content:
                pre = lexicon.score_text(content)
                need_llm = llm_available and pre["confidence"] < CONFIDENCE_THRESHOLD
                llm_indices.append((len(items), content))
                items.append(
                    CodedItem(
                        text_id=f"{post.id}:post",
                        text=content,
                        platform=post.platform,
                        keyword=post.keyword,
                        pub_date=(post.timestamp or "")[:10],
                        dimensions=_match_dimensions(content, self.schema),
                        sentiment=SentimentLabel(pre["sentiment"]),
                        intensity=_intensity(pre["score"]),
                        sentiment_score=pre["score"],
                        confidence=pre["confidence"],
                        method="lexicon",
                        lexicon_sentiment=pre["sentiment"],
                        keywords=pre["keywords"],
                    )
                )
                if not need_llm:
                    llm_indices.pop()
            _tick()
            for comment in post.comments:
                ctext = clean_text(comment.text)
                if not ctext:
                    continue
                pre = lexicon.score_text(ctext)
                item = CodedItem(
                    text_id=f"{post.id}:comment",
                    text=ctext,
                    platform=post.platform,
                    keyword=post.keyword,
                    pub_date=(comment.time or "")[:10],
                    dimensions=_match_dimensions(ctext, self.schema),
                    sentiment=SentimentLabel(pre["sentiment"]),
                    intensity=_intensity(pre["score"]),
                    sentiment_score=pre["score"],
                    confidence=pre["confidence"],
                    method="lexicon",
                    lexicon_sentiment=pre["sentiment"],
                    keywords=pre["keywords"],
                )
                items.append(item)
                if llm_available and pre["confidence"] < CONFIDENCE_THRESHOLD:
                    llm_indices.append((len(items) - 1, ctext))
                _tick()

        # 第二遍：低置信度文本交给 LLM
        if llm_indices:
            texts = [t for _, t in llm_indices]
            n_llm = len(texts)
            llm_weight = 0.35 if plan.narrative_enabled else 0.5
            llm_started = time.monotonic()
            if on_progress:
                on_progress(
                    f"LLM 精分析 0/{n_llm}：已提交并发批量请求，DeepSeek 处理中…",
                    0.5,
                    "llm",
                    0.0,
                )
            llm_results = self.analyzer.analyze_batch(
                texts,
                on_batch_progress=(
                    (lambda done_llm, total_llm: on_progress(
                        f"LLM 精分析 {done_llm}/{total_llm}（"
                        f"{_format_eta(time.monotonic() - llm_started, done_llm, total_llm)}）",
                        0.5 + llm_weight * (done_llm / total_llm if total_llm else 0),
                        "llm",
                        done_llm / total_llm if total_llm else 0,
                    ))
                    if on_progress
                    else None
                ),
            )
            for (idx, _), res in zip(llm_indices, llm_results):
                sent = res.get("sentiment", "neutral")
                # 防御式清洗：模型偶发返回非法值（如 mixed），保留词典结果
                if sent in SentimentLabel.__members__:
                    items[idx].sentiment = SentimentLabel(sent)
                    items[idx].sentiment_score = float(res.get("score", 0.0))
                    items[idx].confidence = max(
                        items[idx].confidence, float(res.get("confidence", 0.0))
                    )
                    items[idx].intensity = _intensity(items[idx].sentiment_score)
                items[idx].method = "llm"
                items[idx].keywords = res.get("keywords", items[idx].keywords)

        # 第三遍（可选）：叙事框架与归因
        if plan.narrative_enabled and llm_available:
            narrative_items = [it for it in items if it.method == "llm"]
            if narrative_items:
                n_narr = len(narrative_items)
                narr_started = time.monotonic()
                if on_progress:
                    on_progress(
                        f"叙事/归因分析 0/{n_narr}：已提交并发批量请求…",
                        0.85,
                        "narrative",
                        0.0,
                    )
                n_results = self.analyzer.analyze_narrative(
                    [it.text for it in narrative_items],
                    on_batch_progress=(
                        (lambda done_n, total_n: on_progress(
                            f"叙事/归因分析 {done_n}/{total_n}（"
                            f"{_format_eta(time.monotonic() - narr_started, done_n, total_n)}）",
                            0.85 + 0.15 * (done_n / total_n if total_n else 0),
                            "narrative",
                            done_n / total_n if total_n else 0,
                        ))
                        if on_progress
                        else None
                    ),
                )
                for it, res in zip(narrative_items, n_results):
                    # 防御式赋值：叙事/归因是可选分析，任何异常都不能中断主流程
                    try:
                        narr = res.get("narrative")
                        if narr in NarrativeFrame.__members__:
                            it.narrative = NarrativeFrame(narr)
                        attr = res.get("attribution")
                        if attr:
                            it.attribution = attr
                    except Exception:
                        continue
        return items
