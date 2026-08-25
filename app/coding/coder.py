"""编码管道：词典预筛 → 低置信度 LLM 精分析 → 维度标签 → [可选]叙事/归因。"""

from __future__ import annotations

import os
import re
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
from app.coding import ad_rules
from app.coding.cleaner import clean_text, desensitize_text, normalize_pub_date
from app.coding.dimensions import match_dimension_sentiments, match_dimensions
from app.coding.llm_analyzer import (
    CONFIDENCE_THRESHOLD,
    OpenAICompatibleAnalyzer,
    is_always_llm_domain,
    uses_subject_domain,
)

# 2.11 需复核：最终置信度低于该值（或命中反讽/黑话/问句/短句等难例候选）→ 结果页人工复核
# v1（旧）：置信 <0.5 或疑似反讽标记（2026-08-16 回测：主集命中 0%、3C 2.3%，实际无效）。
# v3（默认，2026-08-17 校准 spike 定版）：词典直判全领域必标 + 文本信号按领域分权
#   ——3C（digital3c）保留问句/短句/黑话/反讽/低置信 + 直判（召回 50.9%、标记 39.9%）；
#   其他领域仅低置信/反讽/黑话 + 直判（主集召回 25.8%、标记 ~15%）。
#   旧规则回退：SMS_NEED_REVIEW_RULE=v1（0.5+反讽）/v2（0.7+全领域文本信号）。
NEED_REVIEW_CONFIDENCE_V1 = 0.5
NEED_REVIEW_CONFIDENCE_V2 = 0.7
NEED_REVIEW_RULE = os.environ.get("SMS_NEED_REVIEW_RULE", "v3")

# 文本信号（问句/短句）生效的领域：校准 spike 显示这些信号只在 3C 有正收益，
# 在主集/边界集只增负担不加召回。
TEXT_SIGNAL_DOMAINS = frozenset({"digital3c"})

# 黑话/圈内语标记（回测扩展候选用；与词典正负词表解耦，避免改动词典影响提准结论）
SLANG_MARKERS = [
    "挤牙膏", "吃灰", "超模", "水军", "绿厂", "果粉", "智商税", "割韭菜",
    "真香", "翻车", "退坑", "孝钱", "直呼内行", "焊死", "带节奏", "顶配",
]
_SLANG_RE = re.compile("|".join(SLANG_MARKERS))
# 问句/求助候选（严格版，2026-08-16 调参定版）：求知/咨询/选择困难类提问；
# 宽版（任何"吗/呢/啥"）回测标记率 38% 过噪，仅保留语义明确的问句形态
_QUESTION_RE = re.compile(
    r"值得买吗|怎么办|怎么选|怎么样|如何|推荐|买哪个|选哪个|哪个好|"
    r"要不要|能不能|会不会|好吗|咋样|咋选|啥好|有什么推荐|[？?]$|吗$|呢$|啥$"
)
SHORT_TEXT_LEN = 5  # 短句无上下文（如"依旧/哦豁/我出/举手"），单独判断易歧义


def need_review_reason_v1(text: str, confidence: float) -> str:
    if confidence < NEED_REVIEW_CONFIDENCE_V1:
        return f"低置信(conf={confidence:.2f})"
    if lexicon.has_irony_marker(text):
        return "疑似反讽/方向不明"
    return ""


def need_review_reason_v2(text: str, confidence: float) -> str:
    reasons: list[str] = []
    if confidence < NEED_REVIEW_CONFIDENCE_V2:
        reasons.append(f"低置信(conf={confidence:.2f})")
    if lexicon.has_irony_marker(text):
        reasons.append("疑似反讽/方向不明")
    if _SLANG_RE.search(text or ""):
        reasons.append("疑似黑话/圈内语")
    if _QUESTION_RE.search(text or ""):
        reasons.append("问句/求助")
    if len((text or "").strip()) < SHORT_TEXT_LEN:
        reasons.append("短句无上下文")
    return "；".join(reasons)


def need_review_reason_v3(text: str, confidence: float, direct: bool = False,
                          domain: str = "") -> str:
    # F-007（2026-08-26）：词典直判不再无条件标记「未送LLM」——
    # 无 Key 用户全程词典模式时不再 100 条全标 need_review。
    # 仅当确有难例信号（低置信/反讽/黑话/问句/短句）时才进入复核区，
    # 并在原因前置备注「词典直判(未送LLM)」方便复核时判断。
    reasons: list[str] = []
    if confidence < NEED_REVIEW_CONFIDENCE_V2:
        reasons.append(f"低置信(conf={confidence:.2f})")
    if lexicon.has_irony_marker(text):
        reasons.append("疑似反讽/方向不明")
    if _SLANG_RE.search(text or ""):
        reasons.append("疑似黑话/圈内语")
    if domain in TEXT_SIGNAL_DOMAINS:
        if _QUESTION_RE.search(text or ""):
            reasons.append("问句/求助")
        if len((text or "").strip()) < SHORT_TEXT_LEN:
            reasons.append("短句无上下文")
    if direct and reasons:
        reasons.insert(0, "词典直判(未送LLM)")
    return "；".join(reasons)


def need_review_reason(text: str, confidence: float, direct: bool = False,
                       domain: str = "") -> str:
    if NEED_REVIEW_RULE == "v3":
        return need_review_reason_v3(text, confidence, direct=direct, domain=domain)
    if NEED_REVIEW_RULE == "v2":
        return need_review_reason_v2(text, confidence)
    return need_review_reason_v1(text, confidence)


# 校准 spike（2026-08-17）：词典直判置信度不可信（3C 直判 conf≥0.8 实际准确率 ~51%、
# 主集 ~67%，却显示 0.98 类高置信）→ 报告侧展示按校准口径封顶 0.6（可信度"中"），
# 不再误导；LLM 置信度相对可信（0.9+ 桶 88~94%），原样展示。result.json 原始值不变。
LEXICON_DISPLAY_CONFIDENCE_CAP = 0.6


def display_confidence(confidence: float, method: str) -> float:
    """报告展示用置信度：词典直判按校准口径封顶，LLM 原样。"""
    if method == "lexicon":
        return min(float(confidence), LEXICON_DISPLAY_CONFIDENCE_CAP)
    return float(confidence)


def display_confidence_tier(confidence: float, method: str) -> str:
    c = display_confidence(confidence, method)
    return "高" if c >= 0.8 else "中" if c >= 0.5 else "低"


def _intensity(score: float) -> int:
    return min(max(int(abs(score) * 5) + 1, 1), 5)


def _match_dimensions(text: str, schema: DomainSchema | None) -> list[str]:
    """维度提及（2.4 起复用 app/coding/dimensions.py，行为不变）。"""
    return match_dimensions(text, schema)


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
        self.domain_id = getattr(schema, "domain_id", "") if schema else ""
        self._force_llm = is_always_llm_domain(self.domain_id)

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
                need_llm = llm_available and (
                    self._force_llm
                    or plan.llm_enabled
                    or plan.custom_dimensions
                    or pre["confidence"] < CONFIDENCE_THRESHOLD
                )
                llm_indices.append((len(items), content))
                items.append(
                    CodedItem(
                        text_id=f"{post.id}:post",
                        text=content,
                        platform=post.platform,
                        keyword=post.keyword,
                        pub_date=normalize_pub_date(post.timestamp),
                        dimensions=_match_dimensions(content, self.schema),
                        dimension_sentiments=match_dimension_sentiments(content, self.schema),
                        sentiment=SentimentLabel(pre["sentiment"]),
                        ad_flag=bool(getattr(post, "ad_flag", False))
                        or ad_rules.is_ad(content, post.title or ""),
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
            for ci, comment in enumerate(post.comments, 1):
                ctext = clean_text(comment.text)
                if not ctext:
                    continue
                pre = lexicon.score_text(ctext)
                item = CodedItem(
                    # 7 修复（2026-08-19）：同一帖子的多条评论曾共用
                    # post.id:comment 作 text_id → 需复核/反馈无法定位单条，
                    # 永远"剩 1 条"。现加评论序号保证唯一。
                    text_id=f"{post.id}:comment:{ci}",
                    text=ctext,
                    platform=post.platform,
                    keyword=post.keyword,
                    pub_date=normalize_pub_date(comment.time),
                    ad_flag=bool(getattr(comment, "ad_flag", False))
                    or ad_rules.is_ad(ctext),
                    dimensions=_match_dimensions(ctext, self.schema),
                    dimension_sentiments=match_dimension_sentiments(ctext, self.schema),
                    sentiment=SentimentLabel(pre["sentiment"]),
                    intensity=_intensity(pre["score"]),
                    sentiment_score=pre["score"],
                    confidence=pre["confidence"],
                    method="lexicon",
                    lexicon_sentiment=pre["sentiment"],
                    keywords=pre["keywords"],
                )
                items.append(item)
                if llm_available and (
                    self._force_llm
                    or plan.llm_enabled
                    or plan.custom_dimensions
                    or pre["confidence"] < CONFIDENCE_THRESHOLD
                ):
                    llm_indices.append((len(items) - 1, ctext))
                _tick()

        # 第二遍：低置信度文本交给 LLM
        if llm_indices:
            # P1-3：发 LLM 前脱敏（@/链接/邮箱/手机号/身份证），与评测链路同函数
            texts = [desensitize_text(t) for _, t in llm_indices]
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
                dimension_schema=self.schema,
                subject=(
                    (plan.subject or "").strip()
                    if uses_subject_domain(plan.domain_id)
                    and (plan.subject or "").strip()
                    else None
                ),
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
                # 2.4：LLM 维度级情感（校验清洗在 llm_analyzer 内完成；无则保留词典兜底）
                llm_dims = res.get("dimension_sentiments")
                if isinstance(llm_dims, dict) and llm_dims:
                    items[idx].dimension_sentiments = llm_dims

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
                    [desensitize_text(it.text) for it in narrative_items],
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
        # 2.11：低置信/疑似反讽方向不明 → 需复核标记
        for it in items:
            reason = need_review_reason(
                it.text, it.confidence,
                direct=(it.method == "lexicon"), domain=self.domain_id,
            )
            if reason:
                it.need_review = True
                it.need_review_reason = reason
        return items
