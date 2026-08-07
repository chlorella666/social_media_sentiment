"""jieba 分词与文本统计：替换简单 2-gram 切字，支撑词云/共现网络/高频词。"""

from __future__ import annotations

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


def segment(text: str) -> list[str]:
    """jieba 分词 + 停用词/噪声过滤。"""
    words = []
    for w in jieba.lcut(text):
        w = w.strip()
        if len(w) < 2:
            continue
        if w in STOPWORDS:
            continue
        if w.isdigit() or not any("\u4e00" <= c <= "\u9fff" for c in w):
            continue
        words.append(w)
    return words


def build_word_freq(texts: list[str], top_n: int = 50) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for text in texts:
        counter.update(segment(text))
    return counter.most_common(top_n)


def build_cooccurrence(texts: list[str], window: int = 3, top_n: int = 30) -> list[dict]:
    """句子窗口内关键词共现统计，返回 [{source, target, weight}]。"""
    pair_counter: Counter[tuple[str, str]] = Counter()
    for text in texts:
        words = segment(text)
        for i in range(len(words)):
            for j in range(i + 1, min(i + 1 + window, len(words))):
                a, b = words[i], words[j]
                if a != b:
                    key = tuple(sorted((a, b)))
                    pair_counter[key] += 1
    return [
        {"source": a, "target": b, "weight": c}
        for (a, b), c in pair_counter.most_common(top_n)
    ]
