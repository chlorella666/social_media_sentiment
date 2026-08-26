"""统一数据模型（Pydantic）。"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 领域维度体系
# ---------------------------------------------------------------------------

class SubDimension(BaseModel):
    id: str
    name: str
    keywords: list[str] = Field(default_factory=list)


class Dimension(BaseModel):
    id: str
    name: str
    description: str = ""
    source: str = ""  # 来源（学术/业界/预置）
    keywords: list[str] = Field(default_factory=list)  # 维度识别关键词
    sub_dimensions: list[SubDimension] = Field(default_factory=list)
    origin: str = "domain"  # 维度来源（领域配方模型 v0.2，2026-08-14）：
    # "domain" = 领域特有；"template:<id>" = 从主导对象模板选入（运行时物化，
    # 仅元数据，不改行为；模板标准维度待 2.5 证据定稿）


class DomainSchema(BaseModel):
    """领域维度 schema（预置或缓存）。"""

    domain_id: str
    domain_name: str
    dimensions: list[Dimension]
    version: str = "1.0"  # schema 版本号：变更触发该领域评测基线重冻结（指纹守卫）
    template_id: Optional[str] = None  # 主导对象模板：content/physical/service
    # （缺省 None = 旧 schema 按领域推断，只读不改行为）
    template_fingerprint: str = ""  # 模块模板文件指纹（2026-08-20）：缓存失效用，
    # 模板改动后旧 modules_* 缓存自动失效重合成；非模块 schema 为空


# ---------------------------------------------------------------------------
# 采集计划
# ---------------------------------------------------------------------------

class KeywordGroup(BaseModel):
    """按维度分组的关键词。"""

    dimension_id: str
    dimension_name: str
    keywords: list[str]


class ChannelConfig(BaseModel):
    channel_id: str
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)  # 渠道特有参数（如 cookie）


class AnalysisPlan(BaseModel):
    """向导确认后生成的采集计划（可作为协议文件落盘）。"""

    subject: str  # 品牌/产品名或核心主题
    domain_id: Optional[str] = None
    dimensions: list[str] = Field(default_factory=list)  # 选中的维度 id
    custom_dimensions: list[Dimension] = Field(default_factory=list)  # 2.8 任务级自定义维度
    keyword_groups: list[KeywordGroup] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)  # 平铺关键词（含手输）
    channels: list[ChannelConfig] = Field(default_factory=list)
    date_start: Optional[date] = None
    date_end: Optional[date] = None
    per_keyword_limit: int = 50
    comments_enabled: bool = True
    comments_per_post: int = 10
    llm_enabled: bool = False
    llm_base_url: str = ""  # LLM 服务地址（非敏感，随计划传给 worker）
    llm_model: str = ""  # LLM 模型名（非敏感，随计划传给 worker）
    narrative_enabled: bool = False  # 叙事框架/归因分析（默认关）
    relevance_check_enabled: bool = False  # LLM 相关性复核（可选，按量计费）
    exclude_words: list[str] = Field(default_factory=list)  # 词云/共现排除词（角色名、地名等）
    review_enabled: bool = False  # 人工相关性筛选：采集后暂停，人工剔除不相关帖/评论
    exclude_ad_enabled: bool = False  # 广告/官方内容：True=情感统计剔除（漏斗/关键词效果保留）
    created_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# 采集数据（统一模型，参考 Chrome 扩展版 v4 规格）
# ---------------------------------------------------------------------------

class Comment(BaseModel):
    id: str = ""
    author: str = ""
    text: str
    likes: int = 0
    time: str = ""
    is_reply: bool = False
    reply_to: str = ""
    depth: int = 0
    ad_flag: bool = False  # 广告/官方内容标记（规则预标 + 人工复核回填）


class Post(BaseModel):
    id: str
    platform: str
    keyword: str = ""
    author: str = ""
    title: str = ""
    content: str = ""
    url: str = ""
    timestamp: str = ""
    likes: int = 0
    reposts: int = 0
    comments_count: int = 0
    comments: list[Comment] = Field(default_factory=list)
    platform_specific: dict[str, Any] = Field(default_factory=dict)
    ad_flag: bool = False  # 广告/官方内容标记（人工复核回填；规则预标在编码层）


class ChannelResult(BaseModel):
    """单渠道采集结果。"""

    channel_id: str
    ok: bool
    posts: list[Post] = Field(default_factory=list)
    dropped: list[dict] = Field(default_factory=list)  # 清洗丢弃记录（含原因）
    # 采集透明度（2026-08-18）：采集层统计（API 返回/各原因跳过/保留），
    # 供报告"采集说明"解释"为什么没采满"；旧任务缺失时为空、向后兼容
    collection_stats: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)  # 渠道级提示（错配/风控等，2026-08-22）
    error: str = ""
    degraded: bool = False  # 自动降级标记
    risk: bool = False  # 2026-08-16：风控即停标记（保留已采部分，跳过补采，触发冷却）


# ---------------------------------------------------------------------------
# 编码结果
# ---------------------------------------------------------------------------

class SentimentLabel(str, Enum):
    positive = "positive"
    negative = "negative"
    neutral = "neutral"


class NarrativeFrame(str, Enum):
    """Semetko & Valkenburg (2000) 五框架（可选分析）。"""

    conflict = "conflict"
    human_interest = "human_interest"
    attribution = "attribution"
    economic = "economic"
    morality = "morality"


class CodedItem(BaseModel):
    text_id: str
    text: str
    platform: str
    keyword: str = ""  # 所属关键词（关键词效果统计用）
    pub_date: str = ""
    dimensions: list[str] = Field(default_factory=list)
    dimension_sentiments: dict[str, str] = Field(default_factory=dict)
    # 2.4 维度级情感：{维度id: positive|negative}；仅"明确带情感"的维度，
    # 无明确褒贬的维度不出现（对齐抽样与标注规范 §五）；词典兜底为轻量极性聚合。
    sentiment: SentimentLabel = SentimentLabel.neutral
    intensity: int = 0  # 1-5
    sentiment_score: float = 0.0  # -1 ~ +1
    confidence: float = 0.0
    method: str = "lexicon"  # lexicon | llm
    lexicon_sentiment: Optional[str] = None  # LLM 修正前的词典判定
    keywords: list[str] = Field(default_factory=list)
    narrative: Optional[NarrativeFrame] = None
    attribution: Optional[str] = None  # 政府/企业/个人/制度/技术/社会/自然/不明确
    ad_flag: bool = False  # 广告/官方内容：规则预标 or 人工复核；True 时按 exclude_ad_enabled 决定是否计入情感统计
    # 2.11 需复核闭环：低置信（<0.5）或疑似反讽/方向不明 → need_review=True；
    # 结果页人工确认后回填（need_review=False + reviewed_by），报告注明复核数。
    need_review: bool = False
    need_review_reason: str = ""
    reviewed_by: str = ""


# ---------------------------------------------------------------------------
# 任务与报告
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    pending = "pending"
    collecting = "collecting"
    cleaning = "cleaning"
    coding = "coding"
    reporting = "reporting"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"


class ReportBundle(BaseModel):
    """一次分析的完整结果。"""

    plan: AnalysisPlan
    channel_results: list[ChannelResult] = Field(default_factory=list)
    coded_items: list[CodedItem] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    report_text: str = ""  # 结论与建议（LLM 生成或模板兜底）
    chart_insights: dict[str, str] = Field(default_factory=dict)  # 每张图表的解析文字
    conclusion: str = ""  # 按叙事框架的深度结论与建议
    # 报告证据链（v0.20.0，报告证据链优化方案）：均向后兼容，旧 result.json 缺失时为空
    findings: list[dict[str, Any]] = Field(default_factory=list)
    # 结论区数据源（LLM 或规则生成）：[{id, claim, evidence_refs, action, narrative_label?}]
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    # 证据卡数据源（规则抽取）：[{id, text, platform, date, keyword, dimension,
    #   dimension_name, topic?, sentiment, judge, need_review, n, score, intensity, text_id}]
    insight_mode: str = ""  # llm | lexicon | template_fallback | review_refresh | no_data
    # F-027（2026-08-27）：LLM 一句话凝练总结（情感概括）；词典模式为空（用 structured_summary.overall）
    conclusion_text: str = ""
    # F-010（2026-08-26）：结构化总结（整体/正面/负面/重点问题/建议）
    structured_summary: dict[str, Any] = Field(default_factory=dict)
    # F-010/F-015（2026-08-26）：结构化总结来源 llm/rule（旧 result.json 缺失按 rule 兜底）
    structured_summary_source: str = "rule"
    llm_usage: dict = Field(default_factory=dict)  # {"prompt_tokens","completion_tokens","estimated_cost"}
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
