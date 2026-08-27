"""F-021（2026-08-26）：LLM 编码分析工作流（传统访谈式编码）。

词典模式回退 jieba 单词词频（topics 不生成）；LLM 模式每次报告新增本工作流：
输入 coded_items 文本全量分批 → LLM 做编码（主题/诉求类型/归因/代表 text_ids/
解读句）→ 系统用 text_ids 反算 count/占比/情感（数字绝不交给 LLM，沿用 F-010
混合架构经验）。解析/校验/合并为纯函数，单测覆盖四态。
"""

from __future__ import annotations

import json

BATCH_SIZE = 200
VALID_TYPES = {"pain", "expectation", "neutral"}

# 2026-08-27（用户反馈）：LLM 自由命名的主题名规范映射——圈内/难懂名换成口语化表达
TOPIC_NAME_ALIASES = {
    "流水数据": "打榜流水",
}

_SYSTEM = (
    "你是资深的用户研究分析师。给定一批社交媒体帖文/评论（已按品牌分析），"
    "执行传统访谈式编码（编码→主题→诉求分类→洞察）：\n"
    "1) 为每条文本归入一个主题（主题名用用户语言，如「续航」「涨价」「售后」）；\n"
    "2) 标注诉求类型：pain（痛点）/ expectation（期望）/ neutral（中性信息）；\n"
    "3) 给出归因方向（产品/服务/价格/物流/其他，尽量具体）；\n"
    "4) 每个主题给一句解读句（insight），说明用户到底在说什么/要什么。\n"
    "⚠️ 只输出 JSON，不要其他文字；text_ids 必须来自输入列表中的 id，不得编造。\n"
    '输出 JSON：{"topics":[{"name":"...","type":"pain","attribution":"产品",'
    '"text_ids":["..."],"insight":"..."}]}'
)


def _chat(analyzer, system: str, user: str) -> str:
    """调用分析器的底层 chat（OpenAICompatibleAnalyzer 才有）。"""
    content = analyzer._chat(system, user, max_tokens=3000, timeout=120)
    if not isinstance(content, str):
        raise ValueError("LLM 编码输出非字符串")
    return content


def parse_coding_output(content: str) -> list[dict] | None:
    """解析 LLM JSON 输出 → [{name,type,attribution,text_ids,insight}]；失败返回 None。"""
    if not content or not isinstance(content, str):
        return None
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    topics = data.get("topics")
    if not isinstance(topics, list):
        return None
    return topics


def validate_coding_output(raw: list[dict]) -> list[dict] | None:
    """校验：name 非空 str、type 合法、text_ids 为 str 列表；数字字段一律丢弃。

    返回清洗后列表；整体不可用时返回 None（调用方回退规则主题）。
    """
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        ttype = t.get("type")
        if ttype not in VALID_TYPES:
            ttype = "neutral"
        ids = t.get("text_ids")
        if not isinstance(ids, list):
            continue
        clean_ids = [x for x in ids if isinstance(x, str) and x.strip()]
        if not clean_ids:
            continue
        entry: dict = {"name": name.strip(), "type": ttype, "text_ids": clean_ids}
        att = t.get("attribution")
        if isinstance(att, str) and att.strip():
            entry["attribution"] = att.strip()
        ins = t.get("insight")
        if isinstance(ins, str) and ins.strip():
            entry["insight"] = ins.strip()
        out.append(entry)
    return out or None


def merge_topics(validated: list[dict], items_by_id: dict) -> list[dict]:
    """同名主题合并 + text_ids 去重 + 系统反算 count/sentiment/polarity/phrases。"""
    from collections import Counter

    groups: dict[str, list[str]] = {}
    meta: dict[str, dict] = {}
    for t in validated:
        key = TOPIC_NAME_ALIASES.get(t["name"], t["name"])
        groups.setdefault(key, []).extend(t["text_ids"])
        meta.setdefault(key, {"type": t.get("type", "neutral"),
                              "attribution": t.get("attribution", ""),
                              "insight": t.get("insight", "")})
    topics: list[dict] = []
    for name, ids in groups.items():
        uniq = sorted({x for x in ids if x in items_by_id})
        if not uniq:
            continue
        buckets: Counter[str] = Counter()
        phrases: list[str] = []
        for tid in uniq:
            it = items_by_id[tid]
            buckets[it.sentiment.value] += 1
            txt = (it.text or "").strip()
            if txt and len(phrases) < 3:
                phrases.append(txt[:20])
        total = sum(buckets.values()) or 1
        pol = max(("positive", "negative", "neutral"), key=lambda k: buckets.get(k, 0))
        m = meta[name]
        topics.append({
            "name": name,
            "type": m["type"],
            "attribution": m.get("attribution", ""),
            "insight": m.get("insight", ""),
            "phrases": phrases,
            "count": len(uniq),
            "sentiment_weights": {k: round(v / total, 3) for k, v in buckets.items()},
            "polarity": pol,
            "sample_text_ids": uniq[:3],
        })
    topics.sort(key=lambda t: -t["count"])
    return topics


def run_coding_workflow(analyzer, items, plan=None) -> list[dict]:
    """全量分批跑 LLM 编码；任意失败/空返回 []（调用方回退规则主题）。"""
    if not items:
        return []
    items_by_id = {it.text_id: it for it in items if getattr(it, "text_id", "")}
    batch: list[dict] = []
    validated_all: list[dict] = []
    for it in items:
        txt = (it.text or "").strip()
        if not txt or not it.text_id:
            continue
        batch.append({"id": it.text_id, "text": txt})
        if len(batch) >= BATCH_SIZE:
            try:
                user = json.dumps({"texts": batch}, ensure_ascii=False)
                content = _chat(analyzer, _SYSTEM, user)
                raw = parse_coding_output(content)
                valid = validate_coding_output(raw)
                if valid:
                    validated_all.extend(valid)
            except Exception:
                pass  # 单批失败跳过，保留已成功批次
            batch = []
    if batch:
        try:
            user = json.dumps({"texts": batch}, ensure_ascii=False)
            content = _chat(analyzer, _SYSTEM, user)
            raw = parse_coding_output(content)
            valid = validate_coding_output(raw)
            if valid:
                validated_all.extend(valid)
        except Exception:
            pass
    if not validated_all:
        return []
    return merge_topics(validated_all, items_by_id)
