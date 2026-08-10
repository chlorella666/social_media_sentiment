"""大规模情感词典预筛（V2）：cnsenti 知网词典 + ECSD 电商词典 + 精选网络词。

与 lexicon.py（41+41 手工小词典）保持相同输出契约，评分算法沿用
程度副词窗口 + 否定翻转 + tanh 归一化；词强度用词典自带分数调制。
词典文件来源：
- cnsenti 包 dictionary/hownet/pos.pkl|neg.pkl（知网 7k+12k 词，实测无功能词污染）
- data/dicts/ecsd/DoUP|DoUN|DoUM|DoN（苏大电商情感词典）
- _CURATED_WORDS（网络新词：绝绝子/yyds/塌房/割韭菜 等）

说明（实测 2026-08-04）：pysenti 的 12 万词旧词典混入大量功能词误标
（"刚/怎么/效果"均带情感分），导致中性文本被高置信误判，已弃用。
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path

from app.coding.tokenizer import STOPWORDS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ECSD_DIR = PROJECT_ROOT / "data" / "dicts" / "ecsd"
MAX_WORD_LEN = 8  # 子串扫描的最大词长，控制开销
USE_CNSENTI = False  # A/B 开关：是否加载 cnsenti 知网 19k 词（False=仅精选+ECSD）

# 程度副词（与 V1 一致）
DEGREE_ADVERBS = {
    "极": 2.5, "极其": 2.5, "非常": 2.0, "特别": 2.0, "很": 2.0, "太": 2.2,
    "超级": 2.2, "十分": 2.0, "相当": 2.0, "比较": 1.3, "挺": 1.3, "蛮": 1.3,
    "有点": 0.8, "稍微": 0.8, "略": 0.8,
}

# 否定词（V1 + ECSD DoN 合并）
NEGATION_WORDS = {
    "不", "没", "无", "非", "莫", "未", "别", "没有", "不是", "并非",
    "绝不", "从不", "毫不", "并不",
    "不咋", "不怎么", "没那么", "不太",
}

# 负面强化词（触发即强扣分）
NEGATIVE_STRONG_WORDS = [
    "塌房", "翻车", "暴雷", "智商税", "割韭菜", "逼氪", "欺诈", "事故",
]

THRESHOLD_POS = 0.15
THRESHOLD_NEG = -0.15

# 语境词：本身不带固定情感极性（差异/对比/一般等），从评分中排除
CONTEXT_WORDS = {
    "差异", "不同", "区别", "差距", "变化", "一般", "还行", "还好", "正常",
    "普通", "相比", "对比", "比较", "反而", "只是", "情况", "问题", "表现",
    "感觉", "觉得", "看看", "选择", "考虑", "建议", "说法", "说法", "观点",
}

# 词典噪声词：虽在 ECSD 词表中但无语义极性（"第一/唯一"等），不进入情感词报告
NOISE_WORDS = {"第一", "第二", "唯一"}

# 反讽/阴阳怪气标记：命中且存在情感词时压低置信度，交 LLM 复核
IRONY_MARKERS = [
    "像.{0,6}一样", "真是", "也太", "可真是", "好一个", "呵呵", "梦回", "仿佛",
    "厉害了", "可太", "属实", "真行",
]
_IRONY_RE = re.compile("|".join(IRONY_MARKERS))

# V1 手工词（含网络新词）无条件保留
_CURATED_WORDS: dict[str, float] = {
    "好用": 2.5, "喜欢": 2.5, "爱了": 2.5, "很棒": 2.5, "优秀": 2.5, "良心": 2.5,
    "值得": 2.0, "推荐": 2.0, "满意": 2.5, "舒服": 2.0, "方便": 2.0, "好看": 2.0,
    "好玩": 2.0, "惊喜": 2.5, "靠谱": 2.5, "划算": 2.5, "实惠": 2.5, "绝绝子": 2.8,
    "yyds": 2.8, "真香": 2.5, "完美": 2.8, "高质量": 2.5, "期待": 2.0, "感动": 2.0,
    "精致": 2.0, "高效": 2.5, "稳定": 2.0, "流畅": 2.0, "物美价廉": 2.5, "支持": 1.5,
    "好评": 2.5, "不错": 2.0, "进步": 1.5, "贴心": 2.5, "放心": 2.0, "值得入手": 2.5,
    "会回购": 2.5, "顶": 1.5, "赞": 2.0, "好": 2.0,
    "差": -2.0, "垃圾": -2.5, "烂": -2.2, "坑": -2.0, "贵": -2.0, "失望": -2.5,
    "后悔": -2.2, "难用": -2.5, "卡顿": -2.5, "闪退": -2.5, "翻车": -2.5, "塌房": -2.5,
    "暴雷": -2.5, "智商税": -2.5, "割韭菜": -2.5, "骗": -2.2, "假": -2.2, "差评": -2.5,
    "不行": -2.0, "拉胯": -2.5, "无语": -2.0, "恶心": -2.5, "敷衍": -2.2, "糊弄": -2.5,
    "投诉": -2.0, "维权": -2.0, "曝光": -1.8, "瑕疵": -2.0, "掉帧": -2.0, "发热": -1.8,
    "破损": -2.5, "异味": -2.5, "过期": -2.0, "欺骗": -2.5, "逼氪": -2.5, "劝退": -2.5,
    "踩坑": -2.5, "糟糕": -2.5, "很一般": -1.5, "堪忧": -2.5, "太差": -2.8,
    "质量堪忧": -2.5, "不推荐": -2.0, "后悔入手": -2.5,
    # 阶段一验收补充的网络情感词（2026-08-05）
    "绝了": 2.5, "心动": 2.2, "磕到了": 2.5, "上头": 2.0, "神仙": 2.5,
    "天花板": 2.5, "入坑": 2.0, "无脑冲": 2.0, "闭眼入": 2.5, "顶配": 2.0,
    "追责": -2.5, "退游": -2.5, "弃坑": -2.5, "退坑": -2.5, "下头": -2.5,
    "避雷": -2.5, "踩雷": -2.2, "反胃": -2.5, "洗地": -2.2, "背刺": -2.2,
    "摆烂": -2.0, "骚操作": -2.2, "恢复上线": -2.0, "无语子": -2.0,
}


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@lru_cache(maxsize=1)
def _word_scores() -> dict[str, float]:
    """加载 cnsenti 知网词典 + ECSD 领域词 + 精选网络词。"""
    scores: dict[str, float] = dict(_CURATED_WORDS)

    if USE_CNSENTI:
        # cnsenti 知网词典（干净正负词表）
        try:
            import cnsenti
            import pickle

            hownet = Path(cnsenti.__file__).parent / "dictionary" / "hownet"
            for name, sign in (("pos.pkl", 1.8), ("neg.pkl", -1.8)):
                with open(hownet / name, "rb") as fh:
                    words = pickle.load(fh)
                for word in words:
                    word = str(word).strip().lower()
                    if word and len(word) <= MAX_WORD_LEN:
                        scores.setdefault(word, sign)
        except (ImportError, OSError, ValueError):
            pass

    # ECSD 电商领域词（无分数，赋经验值）
    if ECSD_DIR.exists():
        for word in _read_lines(ECSD_DIR / "DoUP"):
            if word and len(word) <= MAX_WORD_LEN:
                scores.setdefault(word.lower(), 1.2)
        for word in _read_lines(ECSD_DIR / "DoUN"):
            if word and len(word) <= MAX_WORD_LEN:
                scores.setdefault(word.lower(), -1.2)
        # DoUM（中性词）不进入评分
        NEGATION_WORDS.update(w.lower() for w in _read_lines(ECSD_DIR / "DoN"))
    for word in CONTEXT_WORDS:
        scores.pop(word, None)
    return scores


def _scan(
    text: str, scores: dict[str, float], ctx_mask: dict[int, int] | None = None
) -> list[tuple[str, int]]:
    """子串扫描：每个位置取最长命中词，返回 [(word, start_idx)]。

    ctx_mask：语境词掩码（start→长度），命中位置整体跳过，
    避免被排除词（如"差异"）的子串（如"差"）再次误命中。
    """
    matches: list[tuple[str, int]] = []
    n = len(text)
    i = 0
    while i < n:
        if ctx_mask and i in ctx_mask:
            i += ctx_mask[i]
            continue
        best: tuple[str, int] | None = None
        for length in range(min(MAX_WORD_LEN, n - i), 0, -1):
            word = text[i : i + length]
            if word in scores:
                best = (word, i)
                break
        if best:
            matches.append(best)
            i += len(best[0])
        else:
            i += 1
    return matches


def _report_keyword(word: str) -> bool:
    """情感词报告过滤：长度≥2，且非停用/否定/程度/噪声词。"""
    if len(word) < 2:
        return False
    if word in NOISE_WORDS or word in NEGATION_WORDS or word in DEGREE_ADVERBS:
        return False
    if word in STOPWORDS:
        return False
    return True


def word_polarity(word: str) -> float:
    """词的极性分数（>0 正面、<0 负面、0 未知/不报告）。"""
    return _word_scores().get(word, 0.0)


def score_text(text: str) -> dict:
    """词典预筛评分（V2），输出契约与 lexicon.py 一致。"""
    lowered = text.lower()
    scores = _word_scores()
    total = 0.0
    hits_pos = 0
    hits_neg = 0
    keywords: list[str] = []
    ctx_mask: dict[int, int] = {}
    for ctx_word in CONTEXT_WORDS:
        start = 0
        while True:
            j = lowered.find(ctx_word, start)
            if j == -1:
                break
            ctx_mask[j] = len(ctx_word)
            start = j + len(ctx_word)

    for w in NEGATIVE_STRONG_WORDS:
        if w in lowered:
            total -= 1.2
            hits_neg += 1
            keywords.append(w)

    for word, idx in _scan(lowered, scores, ctx_mask):
        sc = scores[word]
        prev = lowered[max(0, idx - 2) : idx]
        degree = 1.0
        for adv, factor in DEGREE_ADVERBS.items():
            if prev.endswith(adv):
                degree = factor
                break
        negated = any(prev.endswith(n) for n in NEGATION_WORDS)
        strength = min(1.0 + abs(sc) / 5.0, 3.0)  # 词典分数越强贡献越大
        amount = 0.5 * degree * strength
        if sc > 0 and not negated:
            total += amount
            hits_pos += 1
        elif sc > 0 and negated:
            total -= amount * 0.8  # 正面词被否定 → 负面
            hits_neg += 1
        elif sc < 0 and not negated:
            total -= amount
            hits_neg += 1
        else:
            total += amount * 0.8  # 负面词被否定 → 正面
            hits_pos += 1
        if _report_keyword(word):
            keywords.append(word)

    # 信号一致性：正负命中冲突越强，置信度越低（交 LLM 精分析）
    agreement = 1.0
    if hits_pos + hits_neg > 0:
        agreement = abs(hits_pos - hits_neg) / (hits_pos + hits_neg)
    score = math.tanh(abs(total) / 3.0) if total >= 0 else -math.tanh(abs(total) / 3.0)
    if total == 0:
        confidence = 0.0
        sentiment = "neutral"
    elif score > THRESHOLD_POS:
        sentiment = "positive"
        confidence = min(abs(score) * 2.5, 1.0) * agreement
    elif score < THRESHOLD_NEG:
        sentiment = "negative"
        confidence = min(abs(score) * 2.5, 1.0) * agreement
    else:
        sentiment = "neutral"
        # 边界中性：分数接近 0 时应低置信（交 LLM），并乘信号一致性
        confidence = max(1 - abs(score) * 3, 0.0) * agreement
    if hits_pos + hits_neg == 0:
        confidence = min(confidence, 0.2)
    # 反讽降置信：正词+反讽标记（如"越来越好了，像唐朝一样"）→ 交 LLM 复核
    if hits_pos + hits_neg > 0 and _IRONY_RE.search(lowered):
        confidence = min(confidence, 0.3)
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
