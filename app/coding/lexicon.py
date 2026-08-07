"""内置中文情感词典（MVP 预筛）。

V1 可替换/扩充为 DUTIR + BosonNLP 大规模词典（参考 Chrome 扩展版 v3）。
"""

from __future__ import annotations

import math

POSITIVE_WORDS = [
    "好用", "喜欢", "爱了", "很棒", "优秀", "良心", "值得", "推荐", "满意", "舒服",
    "方便", "好看", "好玩", "惊喜", "靠谱", "划算", "实惠", "绝绝子", "yyds", "真香",
    "完美", "高质量", "期待", "感动", "精致", "高效", "稳定", "流畅", "物美价廉", "支持",
    "好评", "不错", "进步", "贴心", "放心", "值得入手", "会回购", "顶", "赞", "好",
]

NEGATIVE_WORDS = [
    "差", "垃圾", "烂", "坑", "贵", "失望", "后悔", "难用", "卡顿", "闪退",
    "翻车", "塌房", "暴雷", "智商税", "割韭菜", "骗", "假", "差评", "不行", "拉胯",
    "无语", "恶心", "敷衍", "糊弄", "投诉", "维权", "曝光", "瑕疵", "掉帧", "发热",
    "破损", "异味", "过期", "欺骗", "逼氪", "劝退", "踩坑", "糟糕", "很一般", "堪忧",
    "太差", "质量堪忧", "不推荐", "后悔入手",
]

# 程度副词（词 → 权重）
DEGREE_ADVERBS = {
    "极": 2.5, "极其": 2.5, "非常": 2.0, "特别": 2.0, "很": 2.0, "太": 2.2,
    "超级": 2.2, "十分": 2.0, "相当": 2.0, "比较": 1.3, "挺": 1.3, "蛮": 1.3,
    "有点": 0.8, "稍微": 0.8, "略": 0.8,
}

NEGATION_WORDS = [
    "不", "没", "无", "非", "莫", "未", "别", "没有", "不是", "并非",
    "绝不", "从不", "毫不", "并不",
]

# 负面强化词（触发即强扣分）
NEGATIVE_STRONG_WORDS = ["塌房", "翻车", "暴雷", "智商税", "割韭菜", "逼氪", "欺诈", "事故"]

THRESHOLD_POS = 0.15
THRESHOLD_NEG = -0.15


def _matched_words(text: str, words: list[str]) -> list[str]:
    lowered = text.lower()
    return [w for w in words if w in lowered]


def _preceding_window(text: str, match_start: int) -> str:
    return text[max(0, match_start - 2) : match_start]


def score_text(text: str) -> dict:
    """词典预筛评分。

    Returns:
        score: -1 ~ +1
        sentiment: positive | negative | neutral
        confidence: 0~1
        keywords: 命中的情感词
        hits_pos / hits_neg: 命中数
    """
    lowered = text.lower()
    total = 0.0
    hits_pos = 0
    hits_neg = 0
    keywords: list[str] = []

    for w in NEGATIVE_STRONG_WORDS:
        if w in lowered:
            total -= 1.2
            hits_neg += 1
            keywords.append(w)

    for w in POSITIVE_WORDS:
        idx = lowered.find(w)
        while idx != -1:
            degree = 1.0
            prev = _preceding_window(lowered, idx)
            for adv, factor in DEGREE_ADVERBS.items():
                if prev.endswith(adv):
                    degree = factor
                    break
            negated = any(prev.endswith(n) for n in NEGATION_WORDS)
            total += 0.5 * degree * (-0.8 if negated else 1.0)
            hits_pos += 1
            keywords.append(w)
            idx = lowered.find(w, idx + 1)

    for w in NEGATIVE_WORDS:
        idx = lowered.find(w)
        while idx != -1:
            degree = 1.0
            prev = _preceding_window(lowered, idx)
            for adv, factor in DEGREE_ADVERBS.items():
                if prev.endswith(adv):
                    degree = factor
                    break
            negated = any(prev.endswith(n) for n in NEGATION_WORDS)
            total -= 0.5 * degree * (-0.8 if negated else 1.0)
            hits_neg += 1
            keywords.append(w)
            idx = lowered.find(w, idx + 1)

    score = math.tanh(abs(total) / 3.0) if total >= 0 else -math.tanh(abs(total) / 3.0)
    if total == 0:
        confidence = 0.0
        sentiment = "neutral"
    elif score > THRESHOLD_POS:
        sentiment = "positive"
        confidence = min(abs(score) * 2.5, 1.0)
    elif score < THRESHOLD_NEG:
        sentiment = "negative"
        confidence = min(abs(score) * 2.5, 1.0)
    else:
        sentiment = "neutral"
        confidence = max(1 - abs(score) * 3, 0.0)
    # 无命中则置信度低（交给 LLM 精分析）
    if hits_pos + hits_neg == 0:
        confidence = min(confidence, 0.2)
    return {
        "score": round(score, 4),
        "sentiment": sentiment,
        "confidence": round(confidence, 4),
        "keywords": keywords[:10],
        "hits_pos": hits_pos,
        "hits_neg": hits_neg,
    }


def label_from_score(score: float) -> str:
    if score > THRESHOLD_POS:
        return "positive"
    if score < THRESHOLD_NEG:
        return "negative"
    return "neutral"
