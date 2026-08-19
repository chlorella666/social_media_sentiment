"""共现话题簇：Louvain 社区发现 + 簇级文档统计（方案 Part B，2026-08-19）。

簇文档数 = **至少包含簇内任一节点词的独立文本数（并集）**。
词级 node_count 求和会重复计数（同一条文本含多个簇词时被重复计入），
已在真实数据确认偏差（OPPO"数码"簇：求和 29 vs 独立文本 12~13）。
聚合在 pipeline/build_summary 执行：渲染层拿不到文本，无法算并集。
"""

from __future__ import annotations

from app.coding.tokenizer import segment

CLUSTER_MAX = 5
MIN_TOTAL = 20
MIN_EDGES = 12  # 2026-08-19：小图（边<12 或 节点<15）不强行聚类，出词对榜
MIN_NODES = 15
PAIRS_MAX = 10


def louvain_clusters(edges: list[dict], seed: int = 42) -> list[list[str]] | None:
    """Louvain 社区划分（固定 seed 保证确定性；簇数截断为 3~5，
    最小簇合并为"其他"；networkx 不可用返回 None）。"""
    try:
        import networkx as nx
    except Exception:
        return None
    g = nx.Graph()
    for e in edges:
        g.add_edge(
            e["source"], e["target"],
            weight=float(e.get("weight", 0)),
        )
    if g.number_of_nodes() == 0:
        return None
    communities = list(
        nx.community.louvain_communities(g, seed=seed, weight="weight")
    )
    clusters = [sorted(c) for c in communities]
    clusters.sort(key=len, reverse=True)
    if len(clusters) > CLUSTER_MAX:
        kept = clusters[: CLUSTER_MAX - 1]
        others = [n for c in clusters[CLUSTER_MAX - 1:] for n in c]
        kept.append(sorted(others))
        clusters = kept
    return clusters


def build_topic_clusters(
    cooccurrence: list[dict],
    node_count: dict[str, int],
    texts: list[str],
    sentiments: list[str],
    extra_stopwords: set[str] | None = None,
) -> dict:
    """按降级纪律分类并计算话题簇统计。

    texts / sentiments 必须按相同顺序对齐（build_summary 的
    content_texts / stat_items）。返回：
    {"kind": "network"|"pairs"|"skip", "reason": str,
     "pairs": [{source, target, count, pmi}],
     "clusters": [{members, name, words, doc_count, negative_docs,
                   total_sent, negative_rate}], "node_cluster": {word: idx}}
    """
    edges = cooccurrence or []
    total = len(texts)
    if total < MIN_TOTAL:
        return {"kind": "skip", "reason": f"有效文本不足（{total} < 20）",
                "clusters": [], "node_cluster": {}}
    if not edges:
        return {"kind": "skip", "reason": "无共现数据",
                "clusters": [], "node_cluster": {}}
    if len(edges) < MIN_EDGES:
        pairs = _pairs_rows(edges)
        return {"kind": "pairs", "reason": f"共现边不足（{len(edges)} < 10），已显示话题词对",
                "pairs": pairs, "clusters": [], "node_cluster": {}}
    nodes = {e["source"] for e in edges} | {e["target"] for e in edges}
    if len(nodes) < MIN_NODES:
        pairs = _pairs_rows(edges)
        return {"kind": "pairs", "reason": f"话题节点不足（{len(nodes)} < 12），已显示话题词对",
                "pairs": pairs, "clusters": [], "node_cluster": {}}
    clusters = louvain_clusters(edges)
    if clusters is None:
        return {"kind": "pairs", "reason": "networkx 不可用，已显示话题词对",
                "pairs": _pairs_rows(edges), "clusters": [], "node_cluster": {}}
    if len(clusters[0]) / len(nodes) > 0.9:
        return {"kind": "pairs", "reason": "讨论未形成明显话题簇，已显示话题词对",
                "pairs": _pairs_rows(edges), "clusters": [], "node_cluster": {}}

    doc_words = [set(segment(t, extra_stopwords)) for t in texts]
    node_cluster: dict[str, int] = {}
    for i, members in enumerate(clusters):
        for n in members:
            node_cluster[n] = i
    degree = {
        n: sum(1 for e in edges if n in (e["source"], e["target"]))
        for n in nodes
    }
    rows = []
    for i, members in enumerate(clusters):
        member_set = set(members)
        docs = [j for j, ws in enumerate(doc_words) if ws & member_set]
        neg = sum(1 for j in docs if sentiments[j] == "negative")
        pos = sum(1 for j in docs if sentiments[j] == "positive")
        rate = neg / (neg + pos) if neg + pos else None
        name = max(members, key=lambda n: (node_count.get(n, 0), n))
        top3 = sorted(
            members,
            key=lambda n: (-degree[n] * node_count.get(n, 0), n),
        )[:3]
        rows.append({
            "members": members,
            "name": name,
            "words": top3,
            "doc_count": len(docs),
            "negative_docs": neg,
            "total_sent": neg + pos,
            "negative_rate": round(rate, 3) if rate is not None else None,
        })
    rows.sort(key=lambda r: -r["doc_count"])
    return {"kind": "network", "reason": "", "clusters": rows,
            "node_cluster": node_cluster}


def _pairs_rows(edges: list[dict]) -> list[dict]:
    """话题词对榜：按共现文档数降序，保留 PMI（诚实展示证据，不强行聚类）。"""
    rows = [
        {
            "source": e["source"],
            "target": e["target"],
            "count": int(e.get("count", 0)),
            "pmi": float(e.get("weight", 0)),
        }
        for e in edges
    ]
    rows.sort(key=lambda r: (-r["count"], r["source"], r["target"]))
    return rows[:PAIRS_MAX]
