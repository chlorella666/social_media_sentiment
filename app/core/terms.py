"""UX 5.3 术语"人话层"：主应用关键术语 → 大白话解释（「？」悬浮）。

用法：st.markdown(terms.md_label("消费者声音占比", "consumer_voice"),
                   unsafe_allow_html=True)
文案口径与评测中心 TOOLTIPS 保持一致；新增术语先补此处文案池。
"""

from __future__ import annotations

import html

TOOLTIPS: dict[str, str] = {
    "overall_sentiment": "整体倾向：所有文本情感加总后的倾向（偏正面/偏负面/中性），"
                         "只反映大方向，不代表每个平台都一样",
    "avg_score": "平均情感分：-1（负面）~ +1（正面）的平均值；越接近 0 表示情绪越中性",
    "total_posts": "帖子数：采集到的独立帖子/链接数量（不含评论）",
    "encoded_items": "编码文本数：真正进入情感判定的文本条数（帖子+评论，去除无效内容后）",
    "positive_ratio": "正面占比：被判为正面的文本数 ÷ 编码文本数",
    "consumer_voice": "消费者声音占比：保留且非广告/官方文本 ÷ 采集量；"
                      "占比低说明有效用户声音少，建议检查关键词与广告/官方剔除",
    "effective_rate": "有效供给率：保留文本 ÷ 采集文本；越高说明关键词搜到的内容越能用，"
                      "低效词建议优化或停用",
    "lexicon_vs_llm": "词典直判=免费离线、靠内置词库判定（快但有误差）；"
                      "LLM 精分析=把低置信文本发给大模型判定（更准、按量计费、数据出境）",
    "need_review": "需复核样本：系统认为拿不准的难例（反讽/黑话/问句等），"
                   "人工确认后报告会按你的判定重算",
    "template_fallback": "模板兜底：LLM 不可用时用规则模板生成的结论，"
                         "确定性可复现，但不如 LLM 结论灵活",
    "collection_notes": "采集说明：某些渠道实际保留数未达到设置上限时的原因与建议，"
                        "例如风控限制、官网命中、去重等",
    "narrative": "叙事框架/归因：LLM 高级分析提取的「怎么讲这件事」（叙事框架）与"
                 "「责任归给谁」（归因主体）；仅对 LLM 精分析过的文本执行",
    "cooccurrence": "共现网络：高频词在文本中同时出现的关系图；"
                    "节点=话题词，连线=共同出现，用于看讨论围绕哪些话题展开",
    "cluster": "话题簇：共现网络里抱团出现的话题词组合，代表一类相关讨论",
    "pmi": "PMI：点互信息，衡量两个词共现是否「超乎偶然」；值越高说明关系越紧密",
    "word_pairs": "话题词对榜：样本不足时的降级展示——直接列高频共现词对与次数，"
                  "不强行聚类，诚实展示证据",
    "worst_dim": "负面率最高维度词云：讨论量≥门槛的维度里负面占比最高的维度，"
                 "其高频词云帮你一眼看用户在骂什么",
    "f1_metric": "F1：精确率与召回率的平衡分（0~1），越高说明情感判定越准",
    "download_html": "HTML 交互报告：带图表的网页文件，双击即可在浏览器打开分享",
    "download_excel": "原始数据 Excel：帖子/评论/编码明细，可筛选编辑、二次分析",
    "download_word": "Word 报告：正式汇报用的图文文档",
    "download_json": "结果 JSON：机器可读的完整结果，供评测/二次开发",
}


def tip(key: str) -> str:
    text = TOOLTIPS.get(key)
    if not text:
        return ""
    return f' <abbr title="{html.escape(text)}">？</abbr>'


def md_label(label: str, key: str) -> str:
    return f"**{label}**{tip(key)}"
