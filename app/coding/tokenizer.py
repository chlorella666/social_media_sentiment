"""jieba 分词与文本统计：替换简单 2-gram 切字，支撑词云/共现网络/高频词。"""

from __future__ import annotations

import math
from collections import Counter

import jieba

# 首次导入时加载词典
jieba.setLogLevel(60)

STOPWORDS = set(
    """的 了 是 在 我 你 他 她 它 这 那 也 都 就 很 有 和 与 及 或 而 之 其
    我们 你们 他们 她们 自己 大家 这个 那个 这些 那些 什么 怎么 为什么 如何
    真的 觉得 感觉 但是 不过 而且 因为 所以 如果 然后 现在 今天 明天 昨天
    时候 情况 东西 地方 一些 一下 一个 一点 这样 那样 来说 来看 比较 非常
    特别 有点 还是 就是 没有 不是 可以 可能 应该 需要 已经 一直 起来 出来
    看到 知道 了解 希望 期待 准备 开始 最后 之后 之前 其中 同时 以及 不要
    不会 不能 不过 只是 只是 大约 大概 几乎 完全 根本 实在 其实 毕竟 终于
    再 又 把 被 让 向 从 到 于 对 给 跟 比 按 据 论 则 且 若 虽 然 仅 亦
    哈哈 哈哈哈 呵呵 嘿嘿 哦 嗯 啊 吧 吗 呢 呀 啦 呗 罢了
    看看 说说 再说 确实 越来越 每个 感觉 感受 觉得 认为 表示 进行 起来 出来
    下来 开始 已经 正在 刚才 突然 终于 反正 到底 究竟 简直 居然 竟然 其实
    不过 然后 接着 最后 首先 其次 再次 另外 还有 同时 比如 例如 包括 等等
    一样 同样 一起 一直 一切 有点 有些 某些 各种 每次 每天 虽然 尽管 即使
    如果 假如 否则 何况 甚至 无论 不管 凡是 所有 全部 整个 其他 别的 方面
    来说 来看 看来 看起来 大约 左右 上下 前后 大概 几乎 差不多 基本 主要
    重要 关键 问题 事情 想法 观点 意见 评论 评价""".split()
)

STOPWORDS.update(
    """以为 还要 并且 以及 因此 于是 然而 从而 综上 此外 另外 总之 无论
    不仅 而且 反而 甚至 既然 倘若 即便 除非 假如 哪怕 只好 只能 只能 不过""".split()
)

# 通用名词/泛话题词：对情感归因无信息量，词云与共现网络默认过滤
GENERIC_NOUNS = set(
    """剧情 副本 玩家 任务 攻略 论坛 讨论 游戏 内容 帖子 评论 官方 版本
    更新 活动 公告 时候 地方 东西 朋友 现在 今天 昨天 明天 这次 整体
    体验 感觉 想法 观点 建议 消息 情况 问题 东西 关系 事情 时间
    一次 一张 很多 那么 这么 只能 个人 结果 直接 最近 遇到 出现 当时
    目前 特殊 好像 风格 时代 能力 部分 那边 发生 各位 感谢 次数 消耗
    分析 每张 最高 只少 回合 综艺 主动 亲亲 无间 此间 夫人 资源 背景
    显示 自动 超卡 超级 还是 还有 其实 非常 比较 有点 一些 这样 那样""".split()
)

# 2026-08-19（方案 Part B 修订）：平台/媒介词 + 无信息社交动作词。
# 分享—数码 这类"低频强相关"PMI 边会把泛词带进话题簇，从源头过滤；
# 注意：内容词（视频/屏幕/电池等）保留，避免过度过滤。
GENERIC_NOUNS.update(
    """微博 微信 抖音 快手 小红书 知乎 贴吧 B站 头条 搜狐 网易 公众号
    视频号 直播间 直播 链接 网址 二维码
    分享 转发 点赞 收藏 关注 订阅 留言 回复""".split()
)


def segment(text: str, extra_stopwords: set[str] | None = None) -> list[str]:
    """jieba 分词 + 停用词/噪声过滤。"""
    words = []
    for w in jieba.lcut(text):
        w = w.strip()
        if len(w) < 2:
            continue
        if w in STOPWORDS or (extra_stopwords and w in extra_stopwords):
            continue
        if w.isdigit() or not any("\u4e00" <= c <= "\u9fff" for c in w):
            continue
        words.append(w)
    return words


def build_word_freq(
    texts: list[str], top_n: int = 50, extra_stopwords: set[str] | None = None
) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for text in texts:
        counter.update(segment(text, extra_stopwords))
    return counter.most_common(top_n)


def build_cooccurrence(
    texts: list[str],
    window: int = 3,
    top_n: int = 30,
    extra_stopwords: set[str] | None = None,
    min_count: int = 1,
    metric: str = "pmi",
    return_counts: bool = False,
) -> list[dict]:
    """句子窗口内关键词共现统计（文档级去重）。

    metric="pmi"：用点互信息（log(p_ab / p_a*p_b)）衡量关联强度，
    突出有信息量的搭配而非必然共现（如品牌名拆词）；原始次数在 count 字段。
    同一词对在单条文本内只计 1 次，避免长帖重复词灌水统计。

    return_counts=True：返回 (edges, node_count)，node_count = 每词文档频次
    （方案 Part B，2026-08-19；默认签名/返回值保持不变，向后兼容）。
    """
    pair_counter: Counter[tuple[str, str]] = Counter()
    word_counter: Counter[str] = Counter()
    total = 0  # 文档数
    for text in texts:
        words = segment(text, extra_stopwords)
        uniq = set(words)
        if not uniq:
            continue
        total += 1
        for w in uniq:
            word_counter[w] += 1
        seen: set[tuple[str, str]] = set()
        for i in range(len(words)):
            for j in range(i + 1, min(i + 1 + window, len(words))):
                a, b = words[i], words[j]
                if a != b:
                    seen.add(tuple(sorted((a, b))))
        for key in seen:
            pair_counter[key] += 1
    if metric == "pmi" and total > 0:
        scored = []
        for (a, b), c in pair_counter.items():
            if c < min_count:
                continue
            pa = word_counter[a] / total
            pb = word_counter[b] / total
            pab = c / total
            if pa > 0 and pb > 0:
                scored.append((a, b, c, math.log(pab / (pa * pb))))
        scored.sort(key=lambda x: -x[3])
        edges = [
            {"source": a, "target": b, "count": c, "weight": round(pmi, 3)}
            for a, b, c, pmi in scored[:top_n]
        ]
        if return_counts:
            nodes = {n for e in edges for n in (e["source"], e["target"])}
            return edges, {n: word_counter[n] for n in nodes}
        return edges
    edges = [
        {"source": a, "target": b, "count": c, "weight": c}
        for (a, b), c in pair_counter.most_common(top_n)
    ]
    if return_counts:
        nodes = {n for e in edges for n in (e["source"], e["target"])}
        return edges, {n: word_counter[n] for n in nodes}
    return edges
# ---------------------------------------------------------------------------
# F-009（2026-08-26）：短语级统计（bigram/trigram + PMI + 情感权重）
# ---------------------------------------------------------------------------

import re as _re

_SENT_SPLIT = _re.compile(r"[。！？!?；;\n]+")
_ASCII_TOKEN = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .\-]*$")


def _clean_match(text: str) -> str:
    """复用 cleaner 的归一化判定（F-009 吸收审计修正 M1）。"""
    from app.coding.cleaner import _compact_for_match
    return _compact_for_match(text)


# F-009：短语抽取专用虚词集（比 segment 的 STOPWORDS 小得多，保留单字与"比"等语义词）
_PHRASE_STOP = set("的了啊吗呢吧哦嗯哈呀啦呗罢了么是这那在就有和与及或而之其".split())


def _phrase_words(sent: str) -> list[str]:
    """短语抽取用分词：保留单字（仅滤极小虚词集与纯数字）。"""
    words = []
    for w in jieba.lcut(sent):
        w = w.strip()
        if not w or w in _PHRASE_STOP:
            continue
        if not _re.search(r"[\u4e00-\u9fffA-Za-z0-9]", w):
            continue  # 滤标点（保留数字 token，英文产品名如 iPhone 15 需要）
        words.append(w)
    return words


def _phrase_candidates(words: list[str]) -> list[tuple[str, str, str]]:
    """句子内连续窗口短语候选，返回 (短语, 首词, 尾词)。

    中文无空格拼接、英文按空格 join；英文允许 4-token 窗口（如 iPhone 15 Pro Max）。
    """
    out: list[tuple[str, str, str]] = []
    for n in (2, 3, 4, 5):
        for i in range(len(words) - n + 1):
            window = words[i:i + n]
            if all(_ASCII_TOKEN.match(w) for w in window):
                phrase = " ".join(window)
                if len(phrase) <= 40:
                    out.append((phrase, window[0], window[-1]))
            else:
                phrase = "".join(window)
                if len(phrase) <= 40:
                    out.append((phrase, window[0], window[-1]))
    return out


def extract_phrases(
    texts: list[str],
    sentiments: list[str] | None = None,
    brand: str = "",
    keywords: list[str] | None = None,
    min_count: int = 2,
    pmi_min: float = 1.5,  # P0 判例校准（2026-08-26）：2.0 会误杀真实短语（如“比想象中好”PMI≈1.6）
    max_len: int = 8,
    sample_cap: int = 2000,
    top_n: int = 50,
) -> list[dict]:
    """短语级统计（F-009）：bigram~5gram + PMI + 情感权重。

    返回 [{phrase, count, pmi, sentiment_weights, sample_text_ids}]。
    - 品牌/关键词模式：优先保留含品牌或关键词的短语（强信号）；
    - 手动关键词模式（无品牌/关键词）：频次 + PMI + 虚词过滤；
    - 中文短语限长 max_len 字；英文按空白窗口词串；
    - 性能：texts 超过 sample_cap 时均匀抽样。
    """
    if not texts:
        return []
    if len(texts) > sample_cap:
        step = max(1, len(texts) // sample_cap)
        texts = texts[::step]
        if sentiments:
            sentiments = sentiments[::step]
    signals = [s for s in ([brand] + list(keywords or [])) if s and s.strip()]
    signals_c = [_clean_match(s) for s in signals]

    phrase_docs: dict[str, set[int]] = {}
    phrase_first: dict[str, str] = {}
    phrase_last: dict[str, str] = {}
    word_doc: Counter[str] = Counter()
    total_docs = 0

    for idx, text in enumerate(texts):
        text_c = _clean_match(text)
        if not text_c:
            continue
        total_docs += 1
        for sent in _SENT_SPLIT.split(text):
            words = _phrase_words(sent)
            if not words:
                continue
            for w in set(words):
                word_doc[w] += 1
            seen: set[str] = set()
            for ph, first, last in _phrase_candidates(words):
                if len(ph) > max_len and not _ASCII_TOKEN.match(ph):
                    continue
                if ph in seen:
                    continue
                seen.add(ph)
                if idx not in phrase_docs.setdefault(ph, set()):
                    phrase_docs[ph].add(idx)
                    phrase_first[ph] = first
                    phrase_last[ph] = last

    if not phrase_docs:
        return []
    n = max(total_docs, 1)
    results: list[dict] = []
    for ph, docs in phrase_docs.items():
        c = len(docs)
        if c < min_count:
            continue
        first = phrase_first.get(ph, "")
        last = phrase_last.get(ph, "")
        c1 = word_doc.get(first, 0)
        c2 = word_doc.get(last, 0)
        # 原始 PMI：log(c*N/(c1*c2))；min_count 已防低频高估，add-1 平滑会误压真实短语
        pmi = math.log((c * n) / (c1 * c2)) if c1 > 0 and c2 > 0 else 0.0
        if pmi < pmi_min:
            continue
        strong = any(sig and sig in _clean_match(ph) for sig in signals_c)
        if signals_c and not strong and pmi < pmi_min:
            continue
        weights: dict[str, float] = {}
        if sentiments:
            buckets = Counter(sentiments[i] for i in docs if i < len(sentiments))
            total = sum(buckets.values()) or 1
            weights = {k: round(v / total, 3) for k, v in buckets.items()}
        results.append({
            "phrase": ph,
            "count": c,
            "pmi": round(pmi, 3),
            "sentiment_weights": weights,
            "sample_text_ids": sorted(docs)[:3],
        })
    # 子串碎片剔除：短短语若被更长入选短语覆盖则丢弃
    keep: list[dict] = []
    for r in sorted(results, key=lambda x: -len(x["phrase"])):
        rc = _clean_match(r["phrase"])
        if any(rc and rc in _clean_match(o["phrase"]) and o["phrase"] != r["phrase"] for o in keep):
            continue
        keep.append(r)
    keep.sort(key=lambda r: (-r["count"] * (1 + r["pmi"]), -r["count"]))
    return keep[:top_n]



def build_phrase_cooccurrence(
    texts: list[str],
    phrases: list[dict],
    top_n: int = 30,
    min_count: int = 1,
) -> tuple[list[dict], dict[str, int]]:
    """F-009：以短语为节点的句内共现（供共现网络/话题簇使用）。

    返回 (edges, node_count)；短语不足时调用方回退单词级 build_cooccurrence。
    """
    phrase_list = [p["phrase"] for p in phrases]
    phrase_c = [_clean_match(p) for p in phrase_list]
    pair_counter: Counter[tuple[str, str]] = Counter()
    node_count: Counter[str] = Counter()
    total = 0
    for text in texts:
        text_c = _clean_match(text)
        if not text_c:
            continue
        total += 1
        for sent in _SENT_SPLIT.split(text):
            sent_c = _clean_match(sent)
            present: list[str] = []
            for ph, pc in zip(phrase_list, phrase_c):
                if pc and pc in sent_c:
                    present.append(ph)
            for ph in set(present):
                node_count[ph] += 1
            seen: set[tuple[str, str]] = set()
            for i in range(len(present)):
                for j in range(i + 1, len(present)):
                    a, b = sorted((present[i], present[j]))
                    seen.add((a, b))
            for key in seen:
                pair_counter[key] += 1
    if not pair_counter:
        return [], {}
    edges = []
    for (a, b), c in pair_counter.items():
        if c < min_count:
            continue
        pa = node_count[a] / max(total, 1)
        pb = node_count[b] / max(total, 1)
        pab = c / max(total, 1)
        weight = math.log(pab / (pa * pb)) if pa > 0 and pb > 0 else 0.0
        edges.append({"source": a, "target": b, "count": c, "weight": round(weight, 3)})
    edges.sort(key=lambda e: -e["weight"])
    nodes = {n for e in edges for n in (e["source"], e["target"])}
    return edges[:top_n], {n: node_count[n] for n in nodes}
