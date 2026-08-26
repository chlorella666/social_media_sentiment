"""内置演示报告：虚构品牌「云朵咖啡」的成品级示例。

用途（产品体验闭环）：
1) 用户在开启 LLM Key 之前，先看到一个"结论可读、图表完整、证据可追溯"
   的成品报告，理解 LLM 模式的价值，建立信任；
2) 作品集/评审场景下无需真实数据即可演示完整报告形态。

实现原则：
- 纯离线生成，零 LLM 调用、零网络请求；
- 数据全部为虚构样例（品牌/账号/文本均系虚构），不代表任何真实用户；
- 复用真实链路：recompute_summary → build_evidence → 报告模板/图表/Excel，
  保证与真实报告视觉与结构完全一致；
- insight_mode="llm"：本报告用于展示"LLM 精分析模式"的成品效果，
  页面会明确标注为演示数据。

准确率对照（词典 vs LLM）：来源为内置评测集（脱敏真实评论，主集/边界集/
数码3C 三卷），口径为整条情感准确率，详见 docs/评测记录.md 与
docs/阶段2出口评估.md。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from app.core.models import (
    AnalysisPlan,
    ChannelConfig,
    ChannelResult,
    CodedItem,
    Comment,
    Post,
    ReportBundle,
    SentimentLabel,
)
from app.core.names import dimension_cn, register_custom_dim_names
from app.core.pipeline import bundle_to_json, recompute_summary
from app.core.pricing import cost_from_usage
from app.coding.insights import build_descriptors, template_chart_insights
from app.output.excel_writer import build_excel
from app.output.html_report import build_html

ROOT = Path(__file__).resolve().parent.parent

DEMO_SUBJECT = "云朵咖啡"
DEMO_KEYWORD = "云朵咖啡"
DEMO_START = date(2026, 7, 20)
DEMO_END = date(2026, 8, 19)

DEMO_DIR = ROOT / "data" / "state" / "demo_report"
_DEMO_SOURCE: Path | None = None  # 本地演示源覆盖（测试用）；None = 默认 demo_source.json


def _demo_source_path() -> Path | None:
    """真实演示报告源（本地、gitignored、不入库）：
    data/state/demo_report/demo_source.json 存在则优先加载；
    缺失/损坏时回退到内置虚构「云朵咖啡」。"""
    p = _DEMO_SOURCE if _DEMO_SOURCE is not None else DEMO_DIR / "demo_source.json"
    return p if p and p.exists() else None

# 准确率对照（产品明示用；数值来自内置评测集，口径：整条情感准确率）
ACCURACY_COMPARE = [
    {
        "mode": "词典模式（默认 · 离线免费）",
        "accuracy": "约 47%~51%",
        "note": "无 Key 可跑、零数据出境；反讽/黑话/方言等难例判定偏弱",
    },
    {
        "mode": "LLM 精分析（推荐）",
        "accuracy": "约 80%~88%",
        "note": "低置信文本发送给所选服务商，按量计费；支持维度级情感/叙事归因",
    },
]


def accuracy_compare_md() -> str:
    """准确率对照的 Markdown（侧边栏/首次引导展示）。"""
    rows = []
    for r in ACCURACY_COMPARE:
        rows.append(
            f"- **{r['mode']}**：整条情感准确率 **{r['accuracy']}**\n"
            f"  {r['note']}"
        )
    return (
        "**两种模式准确率（内置评测集实测）**\n\n"
        + "\n".join(rows)
        + "\n\n> 数值基于项目内置评测集（脱敏真实评论，按领域综合）。"
        "开启 LLM 后可获得更高准确率与更完整的分析维度。"
    )


# ---------------------------------------------------------------------------
# 虚构演示数据（文本均为编者虚构，仅用于展示报告形态）
# ---------------------------------------------------------------------------

# 维度 id 复用消费品 schema（产品质量/价格价值/使用体验/渠道服务/品牌形象/成分安全）
_DIM_TASTE = "product_quality"
_DIM_PRICE = "price_value"
_DIM_EXP = "experience"
_DIM_SVC = "channel_service"
_DIM_BRAND = "brand_image"
_DIM_SAFE = "safety"


def _it(
    text: str,
    platform: str,
    days_ago: int,
    sentiment: str,
    dims: dict[str, str],
    *,
    intensity: int = 3,
    score: float = 0.0,
    confidence: float = 0.93,
    narrative: str | None = None,
    attribution: str | None = None,
    keywords: list[str] | None = None,
) -> dict:
    """演示条目（posts/comments 共用）。dims: {维度id: positive|negative}。"""
    return {
        "text": text,
        "platform": platform,
        "days_ago": days_ago,
        "sentiment": sentiment,
        "intensity": intensity,
        "score": score,
        "confidence": confidence,
        "dims": dims,
        "narrative": narrative,
        "attribution": attribution,
        "keywords": keywords or [],
    }


def _post(
    platform: str,
    days_ago: int,
    title: str,
    content: dict,
    comments: list[dict],
) -> dict:
    return {
        "platform": platform,
        "days_ago": days_ago,
        "title": title,
        "content": content,
        "comments": comments,
    }


# 评论语料池（每帖取 3 条，真实社交评论普遍较短）
_COMMENT_POOL = [
    _it("同感，生椰拿铁真的上头", "weibo", 0, "positive",
        {_DIM_TASTE: "positive"}, intensity=4, score=0.8,
        keywords=["生椰拿铁", "上头"]),
    _it("涨这么多确实离谱，已经去隔壁了", "weibo", 0, "negative",
        {_DIM_PRICE: "negative"}, intensity=4, score=-0.7,
        narrative="economic", attribution="enterprise", keywords=["涨价", "离谱"]),
    _it("我也被安利了，明天就去试试", "xiaohongshu", 0, "positive",
        {_DIM_TASTE: "positive"}, intensity=2, score=0.5,
        keywords=["试试"]),
    _it("排队是真的久，周末别去", "xiaohongshu", 0, "negative",
        {_DIM_SVC: "negative"}, intensity=3, score=-0.6,
        narrative="conflict", attribution="enterprise", keywords=["排队"]),
    _it("榴莲拿铁勇士，我是不敢试", "bilibili", 0, "neutral",
        {_DIM_TASTE: "neutral"}, intensity=1, score=0.0,
        keywords=["榴莲拿铁"]),
    _it("品控下滑+1，上次的燕麦拿铁淡如水", "bilibili", 0, "negative",
        {_DIM_TASTE: "negative"}, intensity=4, score=-0.75,
        narrative="attribution", attribution="enterprise", keywords=["品控", "下滑"]),
    _it("十块钱的咖啡还要求什么，能喝就行", "zhihu", 0, "neutral",
        {_DIM_PRICE: "neutral"}, intensity=1, score=0.0,
        keywords=["性价比"]),
    _it("客服真的拉胯，电话永远打不通", "weibo", 0, "negative",
        {_DIM_SVC: "negative"}, intensity=4, score=-0.8,
        narrative="attribution", attribution="enterprise", keywords=["客服"]),
    _it("券是到账了，但要下个月才能用，套路", "xiaohongshu", 0, "negative",
        {_DIM_PRICE: "negative"}, intensity=3, score=-0.6,
        keywords=["优惠券", "套路"]),
    _it("冷萃确实不错，夏天必点", "bilibili", 0, "positive",
        {_DIM_TASTE: "positive"}, intensity=3, score=0.7,
        keywords=["冷萃", "必点"]),
    _it("豆子偏焦苦+1，是换了供应商吧", "zhihu", 0, "negative",
        {_DIM_TASTE: "negative"}, intensity=3, score=-0.6,
        narrative="attribution", attribution="enterprise", keywords=["焦苦"]),
    _it("小程序比之前好用了，应该升级过", "weibo", 0, "positive",
        {_DIM_SVC: "positive"}, intensity=2, score=0.5,
        keywords=["小程序"]),
    _it("杯子确实薄，我都套两层拿着", "xiaohongshu", 0, "negative",
        {_DIM_EXP: "negative"}, intensity=3, score=-0.5,
        keywords=["杯子"]),
    _it("联名周边质量还行，就是门槛有点高", "bilibili", 0, "neutral",
        {_DIM_BRAND: "neutral"}, intensity=2, score=0.0,
        keywords=["联名"]),
    _it("桂花拿铁 yyds，秋天就靠它续命", "weibo", 0, "positive",
        {_DIM_TASTE: "positive"}, intensity=5, score=0.9,
        keywords=["桂花拿铁"]),
    _it("少糖还是很甜，下次选无糖", "xiaohongshu", 0, "neutral",
        {_DIM_TASTE: "neutral"}, intensity=2, score=0.0,
        keywords=["甜度"]),
    _it("品控不稳定是真的，同一家店两次都不一样", "zhihu", 0, "negative",
        {_DIM_TASTE: "negative"}, intensity=4, score=-0.7,
        narrative="attribution", attribution="enterprise", keywords=["品控"]),
    _it("早八人离不开云朵，出杯速度是真快", "weibo", 0, "positive",
        {_DIM_SVC: "positive"}, intensity=4, score=0.75,
        keywords=["出杯", "效率"]),
    _it("咖啡因含量标注这事支持，夜猫子需要", "bilibili", 0, "neutral",
        {_DIM_SAFE: "neutral"}, intensity=2, score=0.0,
        keywords=["咖啡因"]),
    _it("新店装修真的好看，适合拍照打卡", "xiaohongshu", 0, "positive",
        {_DIM_EXP: "positive"}, intensity=3, score=0.65,
        keywords=["装修", "打卡"]),
    _it("涨价可以，但好歹提前说一声", "weibo", 0, "negative",
        {_DIM_PRICE: "negative"}, intensity=3, score=-0.55,
        narrative="economic", attribution="enterprise", keywords=["涨价"]),
    _it("云朵的司康配咖啡一绝，下午茶好去处", "xiaohongshu", 0, "positive",
        {_DIM_TASTE: "positive", _DIM_EXP: "positive"}, intensity=4, score=0.8,
        keywords=["司康", "下午茶"]),
    _it("外卖撒漏过一次，包装真该加固", "bilibili", 0, "negative",
        {_DIM_SVC: "negative"}, intensity=3, score=-0.55,
        keywords=["外卖", "撒漏"]),
    _it("咖啡豆对得起价格，同价位算能打", "zhihu", 0, "positive",
        {_DIM_TASTE: "positive", _DIM_PRICE: "positive"}, intensity=3, score=0.7,
        keywords=["咖啡豆", "价格"]),
    _it("联名周边是好看的，就是得喝六杯", "weibo", 0, "neutral",
        {_DIM_BRAND: "neutral"}, intensity=2, score=0.0,
        keywords=["联名周边"]),
    _it("店员一问三不知，培训真的要加强", "xiaohongshu", 0, "negative",
        {_DIM_SVC: "negative"}, intensity=4, score=-0.7,
        narrative="attribution", attribution="enterprise", keywords=["店员", "培训"]),
    _it("低因款对咖啡因敏感的人太友好了", "bilibili", 0, "positive",
        {_DIM_SAFE: "positive"}, intensity=3, score=0.7,
        keywords=["低因", "友好"]),
    _it("别的不说，冷萃是真不错", "weibo", 0, "positive",
        {_DIM_TASTE: "positive"}, intensity=3, score=0.7,
        keywords=["冷萃"]),
    _it("包装设计一直在线，杯套都收藏了", "xiaohongshu", 0, "positive",
        {_DIM_BRAND: "positive"}, intensity=3, score=0.65,
        keywords=["包装", "杯套"]),
    _it("周末下午人从众，建议错峰", "weibo", 0, "neutral",
        {_DIM_SVC: "neutral"}, intensity=1, score=0.0,
        keywords=["排队"]),
    _it("冰萃气泡系列清爽，夏天可以冲", "bilibili", 0, "positive",
        {_DIM_TASTE: "positive"}, intensity=4, score=0.8,
        keywords=["冰萃", "清爽"]),
    _it("会员生日券确实大方，半价羊毛", "weibo", 0, "positive",
        {_DIM_PRICE: "positive"}, intensity=3, score=0.65,
        keywords=["会员", "生日券"]),
    _it("买一送一要先注册会员，绕来绕去", "xiaohongshu", 0, "negative",
        {_DIM_PRICE: "negative", _DIM_BRAND: "negative"}, intensity=3, score=-0.6,
        keywords=["活动", "套路"]),
    _it("豆子偏焦是最近的普遍问题？", "zhihu", 0, "neutral",
        {_DIM_TASTE: "neutral"}, intensity=2, score=0.0,
        keywords=["豆子"]),
    _it("今天堂食杯子换纸杯了，比之前好", "weibo", 0, "positive",
        {_DIM_EXP: "positive"}, intensity=2, score=0.5,
        keywords=["纸杯"]),
]

# 榴莲拿铁专题评论（挂在对应帖子下，支撑 F5 的"评价分化"结论）
_DURIAN_COMMENTS = [
    _it("榴莲拿铁意外的好喝，没有想象中冲，榴莲控可冲", "bilibili", 0, "positive",
        {_DIM_TASTE: "positive"}, intensity=3, score=0.7,
        keywords=["榴莲拿铁", "榴莲控"]),
    _it("榴莲拿铁慎点，味道真的顶不住，喝两口就放弃", "bilibili", 0, "negative",
        {_DIM_TASTE: "negative"}, intensity=4, score=-0.8,
        keywords=["榴莲拿铁", "慎点"]),
    _it("榴莲拿铁有人喝过吗？想试试又怕踩雷", "bilibili", 0, "neutral",
        {_DIM_TASTE: "neutral"}, intensity=2, score=0.0,
        keywords=["榴莲拿铁"]),
]


# 帖子（标题 + 正文；日期按 days_ago 分布，近一周集中负面讨论形成"事件感"）
_POSTS = [
    _post("weibo", 29, "生椰拿铁喝了三天，上头了",
        _it("云朵咖啡的生椰拿铁真的绝，椰香浓又不腻，比预想中顺口，一周喝了三次",
            "weibo", 29, "positive", {_DIM_TASTE: "positive"}, intensity=5,
            score=0.9, keywords=["生椰拿铁", "椰香", "顺口"]), []),
    _post("xiaohongshu", 27, "燕麦拿铁初体验",
        _it("被朋友安利了云朵咖啡的燕麦拿铁，口感丝滑，杯量也实在，拍照也好看",
            "xiaohongshu", 27, "positive",
            {_DIM_TASTE: "positive", _DIM_EXP: "positive"}, intensity=4,
            score=0.85, keywords=["燕麦拿铁", "丝滑"]), []),
    _post("bilibili", 25, "四款新品横评",
        _it("测评了云朵咖啡四款新品，美式和冷萃发挥稳定，冷萃尤其清爽，冰萃气泡系列推荐",
            "bilibili", 25, "positive", {_DIM_TASTE: "positive"}, intensity=4,
            score=0.8, keywords=["新品", "冷萃", "清爽"]), []),
    _post("zhihu", 23, "十块钱价位值得买吗",
        _it("客观说云朵咖啡性价比不错，十块钱出头能喝到不错的拿铁，咖啡豆品质对得起价格",
            "zhihu", 23, "positive",
            {_DIM_PRICE: "positive", _DIM_TASTE: "positive"}, intensity=3,
            score=0.75, keywords=["性价比", "咖啡豆"]), []),
    _post("weibo", 22, "小程序点单真方便",
        _it("云朵咖啡小程序点单太方便了，到店即取，通勤救星",
            "weibo", 22, "positive", {_DIM_SVC: "positive"}, intensity=4,
            score=0.8, keywords=["小程序", "点单", "通勤"]), []),
    _post("xiaohongshu", 20, "新店装修好治愈",
        _it("新开的云朵咖啡装修好治愈，奶油风加大落地窗，店员还会问口味偏好，体验很好",
            "xiaohongshu", 20, "positive",
            {_DIM_EXP: "positive", _DIM_SVC: "positive"}, intensity=4,
            score=0.85, narrative="human_interest", attribution="enterprise",
            keywords=["装修", "治愈", "店员"]), []),
    _post("bilibili", 18, "联名款包装开箱",
        _it("云朵咖啡联名款包装设计挺用心的，杯套是插画风，已经收藏",
            "bilibili", 18, "positive", {_DIM_BRAND: "positive"}, intensity=3,
            score=0.7, keywords=["联名", "包装", "插画"]), []),
    _post("weibo", 16, "配料表挺干净",
        _it("喝完没有不舒服，配料表干净，低因款对咖啡因敏感人群友好",
            "weibo", 16, "positive", {_DIM_SAFE: "positive"}, intensity=3,
            score=0.75, keywords=["配料表", "低因"]), []),
    _post("xiaohongshu", 14, "桂花拿铁回购",
        _it("回购第 n 次，云朵咖啡的桂花拿铁秋天氛围感拉满",
            "xiaohongshu", 14, "positive", {_DIM_TASTE: "positive"}, intensity=5,
            score=0.9, keywords=["桂花拿铁", "回购"]), []),
    _post("zhihu", 12, "连锁咖啡卫生与稳定度小议",
        _it("对比了几家连锁咖啡，云朵的卫生和出品稳定度在同等价位里算靠前",
            "zhihu", 12, "positive",
            {_DIM_SAFE: "positive", _DIM_TASTE: "positive"}, intensity=3,
            score=0.7, keywords=["卫生", "出品稳定"]), []),
    _post("bilibili", 11, "外卖包装升级了？",
        _it("外卖收到时包装完好，杯托很稳，撒漏率比之前低多了",
            "bilibili", 11, "positive", {_DIM_SVC: "positive"}, intensity=3,
            score=0.7, keywords=["外卖", "包装"]), []),
    _post("weibo", 9, "生日券羊毛",
        _it("会员生日券很大方，第二杯半价，羊毛薅得开心",
            "weibo", 9, "positive", {_DIM_PRICE: "positive"}, intensity=3,
            score=0.65, keywords=["生日券", "半价"]), []),
    _post("xiaohongshu", 8, "司康配咖啡，下午茶好去处",
        _it("云朵咖啡的司康配咖啡绝了，下午茶好去处，会再来",
            "xiaohongshu", 8, "positive",
            {_DIM_TASTE: "positive", _DIM_EXP: "positive"}, intensity=4,
            score=0.8, keywords=["司康", "下午茶"]), []),
    _post("bilibili", 6, "冷萃是自己萃的，好评",
        _it("云朵的冷萃是自己萃的，不是浓缩兑水，这点好评",
            "bilibili", 6, "positive", {_DIM_TASTE: "positive"}, intensity=3,
            score=0.7, keywords=["冷萃"]), []),
    _post("weibo", 5, "早八人救赎",
        _it("公司楼下就是云朵咖啡，早八人救赎，出杯速度很快",
            "weibo", 5, "positive", {_DIM_SVC: "positive"}, intensity=4,
            score=0.8, keywords=["出杯", "早八"]), []),
    _post("weibo", 7, "生椰拿铁涨价了？",
        _it("云朵咖啡生椰拿铁涨价了？从 13 涨到 16，连个公告都没有，有点败好感",
            "weibo", 7, "negative", {_DIM_PRICE: "negative"}, intensity=4,
            score=-0.75, narrative="economic", attribution="enterprise",
            keywords=["涨价", "公告"]), []),
    _post("xiaohongshu", 6, "排队 40 分钟避雷",
        _it("避雷！下午三点去的云朵咖啡，排队 40 分钟，出杯还错了两单，体验太差了",
            "xiaohongshu", 6, "negative",
            {_DIM_SVC: "negative"}, intensity=5, score=-0.9,
            narrative="conflict", attribution="enterprise",
            keywords=["排队", "出杯"]), []),
    _post("bilibili", 5, "榴莲拿铁踩雷",
        _it("云朵咖啡新品榴莲拿铁是什么鬼，味道一言难尽，喝一口就后悔",
            "bilibili", 5, "negative", {_DIM_TASTE: "negative"}, intensity=4,
            score=-0.85, keywords=["榴莲拿铁", "踩雷"]), []),
    _post("zhihu", 4, "品控下滑明显",
        _it("云朵咖啡最近品控下滑明显，同一款拿铁两次味道差很多，像开盲盒",
            "zhihu", 4, "negative", {_DIM_TASTE: "negative"}, intensity=4,
            score=-0.8, narrative="attribution", attribution="enterprise",
            keywords=["品控", "下滑"]), []),
    _post("weibo", 3, "外卖超时一小时",
        _it("外卖配送超时一小时，咖啡都凉了，冰块化完只剩水，客服只会说抱歉",
            "weibo", 3, "negative", {_DIM_SVC: "negative"}, intensity=5,
            score=-0.9, narrative="attribution", attribution="enterprise",
            keywords=["外卖", "超时", "客服"]), []),
    _post("xiaohongshu", 2, "小程序崩了两次",
        _it("小程序崩了两次，券也没到账，客服排半天队，体验真的很下头",
            "xiaohongshu", 2, "negative",
            {_DIM_SVC: "negative", _DIM_EXP: "negative"}, intensity=5,
            score=-0.85, narrative="conflict", attribution="enterprise",
            keywords=["小程序", "客服"]), []),
    _post("weibo", 1, "联名周边割韭菜？",
        _it("联名周边要买 6 杯才送，明显割韭菜，热度过了谁还买",
            "weibo", 1, "negative",
            {_DIM_BRAND: "negative", _DIM_PRICE: "negative"}, intensity=4,
            score=-0.75, narrative="morality", attribution="enterprise",
            keywords=["联名", "割韭菜"]), []),
    _post("xiaohongshu", 1, "少糖还是齁甜",
        _it("点了少糖还是齁甜，糖浆不要钱吗？品控能不能统一",
            "xiaohongshu", 1, "negative", {_DIM_TASTE: "negative"}, intensity=4,
            score=-0.7, keywords=["糖浆", "品控"]), []),
    _post("zhihu", 1, "店员培训不到位",
        _it("云朵咖啡店员培训不到位，新品一问三不知，体验很一般",
            "zhihu", 1, "negative", {_DIM_SVC: "negative"}, intensity=3,
            score=-0.6, narrative="attribution", attribution="enterprise",
            keywords=["店员", "培训"]), []),
    _post("bilibili", 2, "杯子质量下滑",
        _it("云朵咖啡的纸杯质量越来越差，一捏就变形，热饮差点烫到",
            "bilibili", 2, "negative",
            {_DIM_EXP: "negative", _DIM_SAFE: "negative"}, intensity=4,
            score=-0.7, keywords=["纸杯", "烫"]), []),
    _post("weibo", 0, "今晚路过新店",
        _it("云朵咖啡又开新店了，路过看了一眼，人还挺多，改天去试试",
            "weibo", 0, "neutral", {_DIM_BRAND: "neutral"}, intensity=1,
            score=0.0, keywords=["新店"]), []),
]


def _attach_comments(posts: list[dict]) -> list[dict]:
    """每帖按确定性轮转挂 3 条评论，评论平台跟随帖子。"""
    pool_len = len(_COMMENT_POOL)
    for i, p in enumerate(posts):
        if "榴莲拿铁" in (p.get("title") or "") or "榴莲拿铁" in (p["content"]["text"]):
            p["comments"] = [
                {**c, "platform": p["platform"], "days_ago": p["days_ago"]}
                for c in _DURIAN_COMMENTS
            ]
            continue
        picked = [
            {**_COMMENT_POOL[(i * 3 + j) % pool_len],
             "platform": p["platform"], "days_ago": p["days_ago"]}
            for j in range(3)
        ]
        p["comments"] = picked
    return posts


def _coded(
    text: str,
    platform: str,
    pub_date: str,
    sentiment: str,
    dims: dict[str, str],
    *,
    text_id: str,
    keyword: str = DEMO_KEYWORD,
    intensity: int = 3,
    score: float = 0.0,
    confidence: float = 0.93,
    narrative: str | None = None,
    attribution: str | None = None,
    keywords: list[str] | None = None,
    is_post: bool = True,
) -> CodedItem:
    return CodedItem(
        text_id=text_id,
        text=text,
        platform=platform,
        keyword=keyword,
        pub_date=pub_date,
        dimensions=list(dims.keys()),
        dimension_sentiments=dims,
        sentiment=SentimentLabel(sentiment),
        intensity=intensity,
        sentiment_score=score,
        confidence=confidence,
        method="llm",
        lexicon_sentiment=None,
        keywords=keywords or [],
        narrative=narrative,
        attribution=attribution,
        ad_flag=False,
        need_review=False,
        need_review_reason="",
        reviewed_by="",
    )


def _demo_plan() -> AnalysisPlan:
    return AnalysisPlan(
        subject=DEMO_SUBJECT,
        domain_id=None,
        dimensions=[],
        keywords=[DEMO_KEYWORD],
        channels=[
            ChannelConfig(channel_id="weibo"),
            ChannelConfig(channel_id="xiaohongshu"),
            ChannelConfig(channel_id="bilibili"),
            ChannelConfig(channel_id="zhihu"),
        ],
        date_start=DEMO_START,
        date_end=DEMO_END,
        per_keyword_limit=30,
        comments_enabled=True,
        comments_per_post=3,
        llm_enabled=True,
        llm_base_url="https://api.deepseek.com",
        llm_model="deepseek-chat",
        narrative_enabled=True,
        relevance_check_enabled=False,
        exclude_words=[],
        review_enabled=False,
        exclude_ad_enabled=True,
    )


def _demo_data() -> tuple[list[Post], list[CodedItem], list[ChannelResult]]:
    """构建虚构数据：帖子（含评论）→ CodedItem（帖子正文 + 评论各一条）。"""
    posts_spec = _attach_comments([dict(p) for p in _POSTS])
    base = DEMO_END
    posts: list[Post] = []
    items: list[CodedItem] = []
    idx = 0
    by_platform: dict[str, list[Post]] = {p: [] for p in ("weibo", "xiaohongshu", "bilibili", "zhihu")}
    for i, ps in enumerate(posts_spec):
        d = base - timedelta(days=ps["days_ago"])
        dstr = d.isoformat()
        c = ps["content"]
        post_comments = []
        for j, cm in enumerate(ps["comments"]):
            post_comments.append(
                Comment(
                    id=f"c{i}_{j}",
                    author=f"用户{1000 + (i * 3 + j) * 7 % 9000}",
                    text=cm["text"],
                    likes=(i * 13 + j * 5) % 120,
                    time=f"{dstr} {(10 + j * 4) % 24:02d}:00",
                )
            )
        post = Post(
            id=f"p{i}",
            platform=ps["platform"],
            keyword=DEMO_KEYWORD,
            author=f"用户{(i * 31) % 9000 + 1000}",
            title=ps["title"],
            content=c["text"],
            url=f"https://demo.example/{ps['platform']}/{i}",
            timestamp=f"{dstr} 09:{i % 60:02d}",
            likes=30 + (i * 37) % 300,
            reposts=(i * 11) % 80,
            comments_count=len(post_comments),
            comments=post_comments,
        )
        posts.append(post)
        by_platform[ps["platform"]].append(post)
        items.append(
            _coded(
                c["text"], ps["platform"], dstr, c["sentiment"], c["dims"],
                text_id=f"p{i}",
                intensity=c["intensity"], score=c["score"], confidence=c["confidence"],
                narrative=c.get("narrative"), attribution=c.get("attribution"),
                keywords=c["keywords"],
            )
        )
        for j, cm in enumerate(ps["comments"]):
            items.append(
                _coded(
                    cm["text"], ps["platform"], dstr, cm["sentiment"], cm["dims"],
                    text_id=f"p{i}:c{j}",
                    intensity=cm["intensity"], score=cm["score"],
                    confidence=cm["confidence"],
                    narrative=cm.get("narrative"), attribution=cm.get("attribution"),
                    keywords=cm["keywords"], is_post=False,
                )
            )
    channel_results = [
        ChannelResult(
            channel_id=pid,
            ok=True,
            posts=by_platform[pid],
            dropped=[],
            collection_stats={
                "returned": len(by_platform[pid]) + sum(p.comments_count for p in by_platform[pid]),
                "kept": len(by_platform[pid]) + sum(p.comments_count for p in by_platform[pid]),
                "skipped": 0,
                "note": "演示数据：虚构内容，仅用于展示报告效果",
            },
        )
        for pid in ("weibo", "xiaohongshu", "bilibili", "zhihu")
    ]
    return posts, items, channel_results


def _find_item(items: list[CodedItem], marker: str) -> CodedItem | None:
    """按原文子串取演示条目（保证证据卡与文本一一对应）。"""
    for it in items:
        if marker in it.text:
            return it
    return None


def _dim_stat(summary: dict, dim: str) -> dict:
    v = summary.get("dimensions", {}).get(dim) or {}
    return {
        "count": int(v.get("count") or 0),
        "negative": int(v.get("negative") or 0),
        "negative_rate": float(v.get("negative_rate") or 0.0),
    }


def _recent_neg(items: list[CodedItem], days: int = 7) -> tuple[int, int, float]:
    """近 days 天（含当天）负面占比：(负面 n, 总数 n, 占比)。"""
    start = (DEMO_END - timedelta(days=days - 1)).isoformat()
    recent = [it for it in items if it.pub_date >= start]
    neg = sum(1 for it in recent if it.sentiment == SentimentLabel.negative)
    total = len(recent)
    return neg, total, (neg / total if total else 0.0)


def _demo_evidence(items: list[CodedItem], summary: dict) -> list[dict]:
    """演示证据卡：确定性抽取 + 统计卡，id 与发现引用严格对应。"""
    def _card(marker: str, dim: str, sentiment: str, n: int) -> dict:
        it = _find_item(items, marker)
        return {
            "id": "",
            "kind": "text",
            "text": it.text if it else marker,
            "platform": it.platform if it else "weibo",
            "date": it.pub_date if it else "",
            "keyword": DEMO_KEYWORD,
            "dimension": dim,
            "dimension_name": dimension_cn(dim),
            "topic": "",
            "sentiment": sentiment,
            "judge": "llm",
            "need_review": False,
            "n": n,
            "score": it.sentiment_score if it else 0.0,
            "intensity": it.intensity if it else 3,
            "text_id": it.text_id if it else "",
        }

    cards: list[dict] = [
        _card("生椰拿铁涨价了", _DIM_PRICE, "negative", 7),      # E1
        _card("买一送一要先注册会员", _DIM_PRICE, "negative", 5),  # E2
        _card("排队 40 分钟", _DIM_SVC, "negative", 8),            # E3
        _card("外卖配送超时一小时", _DIM_SVC, "negative", 6),      # E4
        _card("品控下滑明显", _DIM_TASTE, "negative", 9),          # E5
        _card("少糖还是齁甜", _DIM_TASTE, "negative", 4),          # E6
        _card("豆子偏焦苦", _DIM_TASTE, "negative", 5),            # E7
        _card("榴莲拿铁", _DIM_TASTE, "negative", 7),              # E8
        _card("纸杯质量越来越差", _DIM_EXP, "negative", 3),        # E9
    ]
    for i, c in enumerate(cards, start=1):
        c["id"] = f"E{i}"

    stat_all = summary.get("sentiment_distribution") or {}
    total = int(summary.get("total_items") or 0)
    cards.append(
        {
            "id": "S1",
            "kind": "stat",
            "stat_key": "overall",
            "text": (
                f"整体：编码 {total} 条，正面 {stat_all.get('positive', {}).get('count', 0)} 条"
                f"（{stat_all.get('positive', {}).get('ratio', 0) * 100:.0f}%）、"
                f"中性 {stat_all.get('neutral', {}).get('count', 0)} 条、"
                f"负面 {stat_all.get('negative', {}).get('count', 0)} 条"
                f"（{stat_all.get('negative', {}).get('ratio', 0) * 100:.0f}%）。"
            ),
            "platform": "",
            "date": "",
            "dimension": "",
            "sentiment": "",
            "judge": "stat",
            "need_review": False,
            "n": total,
        }
    )
    for sid, dim, label in (
        ("S2", _DIM_PRICE, "价格价值"),
        ("S3", _DIM_SVC, "渠道服务"),
        ("S4", _DIM_TASTE, "产品质量"),
    ):
        ds = _dim_stat(summary, dim)
        cards.append(
            {
                "id": sid,
                "kind": "stat",
                "stat_key": f"dimension:{dim}",
                "text": (
                    f"维度「{label}」：讨论 {ds['count']} 条，负面 {ds['negative']} 条，"
                    f"负面率 {ds['negative_rate'] * 100:.0f}%。"
                ),
                "platform": "",
                "date": "",
                "dimension": dim,
                "sentiment": "",
                "judge": "stat",
                "need_review": False,
                "n": ds["count"],
            }
        )
    return cards


def _build_demo_findings(summary: dict, items: list[CodedItem]) -> list[dict]:
    """演示级发现：数字全部取自本次生成的 summary/items，避免文案与统计不一致。"""
    dist = summary.get("sentiment_distribution") or {}
    pos_n = int(dist.get("positive", {}).get("count") or 0)
    neg_n = int(dist.get("negative", {}).get("count") or 0)
    total = int(summary.get("total_items") or 0)
    neg, recent_n, recent_rate = _recent_neg(items)
    earlier_n = total - recent_n
    earlier_neg = neg_n - neg
    price = _dim_stat(summary, _DIM_PRICE)
    svc = _dim_stat(summary, _DIM_SVC)
    taste = _dim_stat(summary, _DIM_TASTE)
    durian = [it for it in items if "榴莲拿铁" in it.text]
    durian_pos = sum(1 for it in durian if it.sentiment == SentimentLabel.positive)
    durian_neg = sum(1 for it in durian if it.sentiment == SentimentLabel.negative)
    # 按负面率排序（保证结论与图表一致）；每维度的证据引用/建议固定
    _dim_meta = {
        "渠道服务": {
            "stat": svc, "sid": "S3", "refs": ["E3", "E4"],
            "issue": "门店排队与外卖超时/撒漏",
            "action": "高峰时段增加出杯通道/预点单提醒，外卖侧加固杯托并缩短承诺时长。",
            "narrative_label": "归因",
        },
        "价格价值": {
            "stat": price, "sid": "S2", "refs": ["E1", "E2"],
            "issue": "涨价无公告、活动规则绕路",
            "action": "调价前先做用户沟通与缓冲期方案，并同步上线套餐券降低感知涨幅。",
            "narrative_label": "经济后果",
        },
        "产品质量": {
            "stat": taste, "sid": "S4", "refs": ["E5", "E6", "E7"],
            "issue": "同款两次味道差异、少糖偏甜、豆子焦苦",
            "action": "梳理门店 SOP 与原料批次稳定性，重点门店先行校准。",
            "narrative_label": "归因",
        },
    }
    ranked = sorted(
        _dim_meta.items(),
        key=lambda kv: -kv[1]["stat"]["negative_rate"],
    )
    top_name, top_meta = ranked[0]
    second_name, second_meta = ranked[1]

    return [
        {
            "id": "F1",
            "claim": (
                f"整体讨论以正面为主（正面 {pos_n} 条 / {pos_n / total * 100:.0f}%，"
                f"负面 {neg_n} 条 / {neg_n / total * 100:.0f}%，共 {total} 条）；"
                f"近一周负面占比 {recent_rate * 100:.0f}%（{neg}/{recent_n}）"
                f"明显高于此前（{earlier_neg / max(earlier_n, 1) * 100:.0f}%），"
                "主要来自涨价与门店体验事件。"
            ),
            "evidence_refs": ["S1"],
            "action": "建议先看近一周负面来源（涨价/排队/外卖），再决定沟通与运营动作。",
            "narrative_label": "",
        },
        {
            "id": "F2",
            "claim": (
                f"负面率最高的维度是「{top_name}」"
                f"（负面率 {top_meta['stat']['negative_rate'] * 100:.0f}%，"
                f"n={top_meta['stat']['count']}）：{top_meta['issue']}。"
            ),
            "evidence_refs": [top_meta["sid"], *top_meta["refs"]],
            "action": top_meta["action"],
            "narrative_label": top_meta["narrative_label"],
        },
        {
            "id": "F3",
            "claim": (
                f"「{second_name}」负面率次高"
                f"（{second_meta['stat']['negative_rate'] * 100:.0f}%，"
                f"n={second_meta['stat']['count']}）：{second_meta['issue']}。"
            ),
            "evidence_refs": [second_meta["sid"], *second_meta["refs"]],
            "action": second_meta["action"],
            "narrative_label": second_meta["narrative_label"],
        },
        {
            "id": "F4",
            "claim": (
                f"「产品质量」出现品控不一致信号（n={taste['count']}，"
                f"负面率 {taste['negative_rate'] * 100:.0f}%）："
                "同款两次味道差异、少糖偏甜、豆子焦苦，"
                "属于口碑风险累积项，但整体仍以正面评价为主。"
            ),
            "evidence_refs": ["S4", "E5", "E6", "E7"],
            "action": "梳理门店 SOP 与原料批次稳定性，重点门店先行校准。",
            "narrative_label": "归因",
        },
        {
            "id": "F5",
            "claim": (
                f"新品「榴莲拿铁」评价分化（正面 n={durian_pos} / 负面 n={durian_neg}），"
                "话题热度高但口味接受度有限，适合作为话题型产品而非走量款。"
            ),
            "evidence_refs": ["E8"],
            "action": "新品定位为限时话题款，用试饮装降低尝鲜门槛。",
            "narrative_label": "",
        },
    ]


def build_demo_bundle() -> ReportBundle:
    """构建演示报告 Bundle（离线、可重复）。

    优先加载本地真实演示源（恋与深空示例，2026-08-21 用户指定）；
    源缺失时回退到内置虚构品牌「云朵咖啡」。"""
    src = _demo_source_path()
    if src is not None:
        try:
            return ReportBundle.model_validate(
                json.loads(src.read_text(encoding="utf-8"))
            )
        except Exception:
            pass  # 源损坏 → 回退虚构兜底
    posts, items, channel_results = _demo_data()
    plan = _demo_plan()
    register_custom_dim_names(plan)
    summary = recompute_summary(plan, items, channel_results, posts)
    evidence = _demo_evidence(items, summary)
    # 与真实报告一致：图表解析由模板生成；发现为演示级结论（见 _DEMO_FINDINGS）
    chart_insights = template_chart_insights(build_descriptors(summary))
    findings = _build_demo_findings(summary, items)
    dist = summary.get("sentiment_distribution") or {}
    total = int(summary.get("total_items") or 0)
    pos_n = int(dist.get("positive", {}).get("count") or 0)
    neu_n = int(dist.get("neutral", {}).get("count") or 0)
    neg_n = int(dist.get("negative", {}).get("count") or 0)
    price = _dim_stat(summary, _DIM_PRICE)
    svc = _dim_stat(summary, _DIM_SVC)
    _, _, recent_rate = _recent_neg(items)
    _meta = {
        "渠道服务": svc,
        "价格价值": price,
    }
    _ranked = sorted(_meta.items(), key=lambda kv: -kv[1]["negative_rate"])
    top_name, top = _ranked[0]
    second_name, second = _ranked[1]
    report_text = (
        f"本次分析对象为「{DEMO_SUBJECT}」（演示数据），共编码 {total} 条文本。"
        f"整体情感倾向为正面（正面 {pos_n / max(total, 1) * 100:.1f}%、"
        f"中性 {neu_n / max(total, 1) * 100:.1f}%、负面 {neg_n / max(total, 1) * 100:.1f}%）："
        "口味与性价比是口碑主力，"
        f"近一周负面占比抬升至约 {recent_rate * 100:.0f}%，"
        "主要来自「价格价值」的涨价争议与「渠道服务」的排队/外卖体验。"
        f"负面率最高的维度是「{top_name}」（{top['negative_rate'] * 100:.1f}%，"
        f"n={top['count']}），建议重点关注；"
        f"「{second_name}」（{second['negative_rate'] * 100:.1f}%，n={second['count']}）次之。"
        "具体证据与行动建议见下方「核心发现」。"
    )
    conclusion = (
        "整体口碑健康但出现边际转弱信号：近一周负面占比显著上升，"
        "集中在涨价沟通、门店排队与外卖体验；品控一致性存在累积风险。"
        "建议优先处理调价公告与高峰产能两个可快速见效的触点，"
        "并跟进同款产品口味一致性，避免口碑从「话题热度」转入「质量质疑」。"
    )
    llm_usage = {
        "prompt_tokens": 26000,
        "completion_tokens": 3400,
        "estimated_cost": round(
            cost_from_usage(26000, 3400), 3
        ),
    }
    # F-018（2026-08-26，修订版回退）：LLM 模式不再产出 structured_summary——
    # 演示报告为 LLM 模式成品展示，解读由 findings + conclusion 承担。
    return ReportBundle(
        plan=plan,
        channel_results=channel_results,
        coded_items=items,
        summary=summary,
        report_text=report_text,
        chart_insights=chart_insights,
        conclusion=conclusion,
        findings=findings,
        evidence=evidence,
        insight_mode="llm",
        structured_summary={},
        structured_summary_source="rule",
        llm_usage=llm_usage,
        warnings=[],
    )


DEMO_RESULT = DEMO_DIR / "result.json"
DEMO_EXCEL = DEMO_DIR / "result.xlsx"
DEMO_HTML = DEMO_DIR / "report.html"


def prepare_demo_files(bundle: ReportBundle) -> dict:
    """产出演示报告可下载文件（Excel/HTML，带缓存；Word 由结果页按需生成）。"""
    files: dict = {"excel": b"", "html": ""}
    try:
        if DEMO_EXCEL.exists() and DEMO_HTML.exists() and DEMO_RESULT.exists():
            files["excel"] = DEMO_EXCEL.read_bytes()
            files["html"] = DEMO_HTML.read_text(encoding="utf-8")
        else:
            DEMO_DIR.mkdir(parents=True, exist_ok=True)
            bundle_to_json(bundle, DEMO_RESULT)
            files["excel"] = build_excel(bundle).getvalue()
            files["html"] = build_html(bundle)
            DEMO_EXCEL.write_bytes(files["excel"])
            DEMO_HTML.write_text(files["html"], encoding="utf-8")
    except Exception:
        # 文件产物失败不阻断页面展示：结果页仍可基于 bundle 渲染
        files = {"excel": b"", "html": ""}
    return files


def demo_report_card_md() -> str:
    """首次引导卡中"演示报告"入口说明。"""
    return (
        "✨ **先看成品报告**（约 2 秒）：内置虚构品牌「云朵咖啡」的完整示例报告，"
        "展示 LLM 精分析模式下的结论、图表与证据链效果，无需任何配置。"
    )


if __name__ == "__main__":  # 开发自检：python -m app.demo_report
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    b = build_demo_bundle()
    files = prepare_demo_files(b)
    s = b.summary
    dist = s["sentiment_distribution"]
    print(f"items={s['total_items']} posts={s['total_posts']} "
          f"pos={dist['positive']['count']} neu={dist['neutral']['count']} "
          f"neg={dist['negative']['count']} "
          f"dims={len(s['dimensions'])} findings={len(b.findings)} "
          f"evidence={len(b.evidence)} excel={len(files['excel'])} "
          f"html={len(files['html'])}")
