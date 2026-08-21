"""报告证据链：规则证据抽取 + 规则 findings 生成（确定性、可回归）。

报告证据链优化方案（docs/报告证据链优化方案.md）：
- 证据由规则抽取，LLM 只解读不选材——防止幻觉引文；
- 一套规则内核两个入口（无 LLM 默认 / LLM 失败兜底）；
- n 守卫（n<3 不评级、n<10 标"样本有限"）、零数据短路、降噪；
- 数字口径与 build_summary 一致：广告剔除跟随 plan.exclude_ad_enabled，
  need_review 样本保留并标注"待复核"（不在结论里隐藏，供 2.11 人工闭环承接）。
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

from app.core.names import dimension_cn, platform_cn

# 保守脏话清单（仅真实粗口；"垃圾/差劲"等普通负面评价词不在此列，避免误伤证据）
PROFANITY_WORDS = [
    "操你妈", "去你妈的", "草泥马", "傻逼", "煞笔", "傻B", "傻b",
    "他妈的", "妈的", "尼玛", "艹", "fuck", "shit",
]

EVIDENCE_MAX_LEN = 80  # 证据原文截断上限（字符）
FINDINGS_MAX = 5  # 结论区发现上限
MIN_DIM_NORMAL = 10  # n>=10 可正常评级；3~9 标"样本有限"；<3 不评级
MIN_DIM_RATING = 3

# action 校验用的渠道/平台标记（验收标准 2：建议必须含"渠道"）。
# 刻意不含"官方/公关"等词，避免放行无渠道的公关套话。
CHANNEL_MARKERS = [
    "微博", "微信", "小红书", "B站", "B 站", "知乎", "贴吧", "TapTap",
    "抖音", "快手", "京东", "天猫", "淘宝", "拼多多", "评论区", "社区",
    "渠道", "平台", "全网", "线下", "线上",
]


def _denoise_text(text: str) -> str:
    """降噪顺序：脱敏（PII）→ 脏话替换 → 截断 ≤80 字。"""
    if not text:
        return ""
    from app.coding.cleaner import desensitize_text

    t = desensitize_text(text)
    lowered = t.lower()
    for word in PROFANITY_WORDS:
        if word.lower() in lowered:
            t = "原文含粗口，已略"
            break
    if len(t) > EVIDENCE_MAX_LEN:
        t = t[:EVIDENCE_MAX_LEN] + "…"
    return t.strip()


def _date_key(pub_date: str) -> str:
    """日期排序键：空日期排最后。"""
    return pub_date or "0000-00-00"


def _card_sort_key(card: dict) -> tuple:
    """证据选择排序（确定性）：负面优先 → 情感分 → 强度 → 日期新 → text_id 决胜。

    刻意不用"新颖度加权"这类会随时间漂移的公式，保证回归 golden 断言稳定。
    """
    sent = card["sentiment"]
    sent_rank = 0 if sent == "negative" else (1 if sent == "positive" else 2)
    return (
        sent_rank,
        -float(card.get("score") or 0.0),
        -int(card.get("intensity") or 0),
        _date_key(card.get("date") or ""),
        card.get("text_id", ""),
    )


def _stat_items(items: list, exclude_ad: bool) -> list:
    """与 build_summary 的 stat_items 同口径（广告剔除跟随 exclude_ad_enabled）。"""
    return [it for it in items if not (exclude_ad and getattr(it, "ad_flag", False))]


def _sent(it) -> str:
    """兼容 SentimentLabel 枚举与字符串两种形态的文本。"""
    s = getattr(it, "sentiment", None)
    return getattr(s, "value", s) or ""


def _dimension_cards_for(items: Iterable, max_per_dim: int) -> list[dict]:
    """维度级证据卡：LLM 维度情感 → 词典维度情感 → 整条情感×维度关键词 级联。"""
    buckets: dict[tuple[str, str], list] = defaultdict(list)
    for it in items:
        ds = getattr(it, "dimension_sentiments", None) or {}
        dim_sents: dict[str, str] = {}
        if ds:
            dim_sents = {k: v for k, v in ds.items() if v in ("negative", "positive")}
        elif _sent(it) in ("negative", "positive"):
            # 无维度级情感（无 schema 或词典未命中）→ 整条情感 × 维度提及
            dim_sents = {
                d: _sent(it)
                for d in (getattr(it, "dimensions", None) or [])
            }
        for dim, dval in dim_sents.items():
            buckets[(dim, dval)].append(it)

    cards: list[dict] = []
    # 稳定顺序：负面桶优先，桶内按强度（count 降序、dim id 升序）
    bucket_order = sorted(
        buckets.keys(),
        key=lambda k: (
            0 if k[1] == "negative" else 1,
            -len(buckets[k]),
            k[0],
        ),
    )
    for dim, sent in bucket_order:
        bucket = buckets[(dim, sent)]
        candidates = [
            {
                "text_id": it.text_id,
                "text": _denoise_text(it.text),
                "platform": it.platform,
                "date": it.pub_date or "",
                "keyword": it.keyword or "",
                "dimension": dim,
                "sentiment": sent,
                "judge": "llm" if getattr(it, "method", "") == "llm" else "lexicon",
                "need_review": bool(getattr(it, "need_review", False)),
                "score": float(getattr(it, "sentiment_score", 0.0) or 0.0),
                "intensity": int(getattr(it, "intensity", 0) or 0),
            }
            for it in bucket
        ]
        candidates.sort(key=_card_sort_key)
        for c in candidates[:max_per_dim]:
            c["n"] = len(bucket)  # 该维度×该判定下的样本数（与 summary 同口径）
            cards.append(c)
    return cards


def _topic_cards(items: list, summary: dict, used_ids: set, max_topic: int) -> list[dict]:
    """话题词反查：sentiment_sources 中负面占比>50% 的话题，反查原文（负面优先）。"""
    sources = summary.get("sentiment_sources") or []
    negative_items = sorted(
        (it for it in items if _sent(it) == "negative"),
        key=lambda it: (
            -float(it.sentiment_score or 0.0),
            -int(it.intensity or 0),
            _date_key(it.pub_date or ""),
            it.text_id,
        ),
    )
    cards: list[dict] = []
    for src in sources:
        if src["negative_rate"] <= 0.5:
            continue
        if src["positive"] + src["negative"] < MIN_DIM_RATING:
            continue
        word = src["word"]
        for it in negative_items:
            if it.text_id in used_ids or word not in (it.text or ""):
                continue
            used_ids.add(it.text_id)
            cards.append(
                {
                    "text_id": it.text_id,
                    "text": _denoise_text(it.text),
                    "platform": it.platform,
                    "date": it.pub_date or "",
                    "keyword": it.keyword or "",
                    "dimension": "",
                    "topic": word,
                    "sentiment": "negative",
                    "judge": "llm" if getattr(it, "method", "") == "llm" else "lexicon",
                    "need_review": bool(getattr(it, "need_review", False)),
                    "score": float(it.sentiment_score or 0.0),
                    "intensity": int(it.intensity or 0),
                    "n": src["positive"] + src["negative"],
                }
            )
            if len(cards) >= max_topic:
                return cards
    return cards


def _overall_negative_cards(
    items: list, summary: dict, used_ids: set, max_overall: int
) -> list[dict]:
    """整体负面文本 TOP（未被维度/话题卡占用，负面优先）。"""
    negative_items = sorted(
        (it for it in items if _sent(it) == "negative" and it.text_id not in used_ids),
        key=lambda it: (
            -float(it.sentiment_score or 0.0),
            -int(it.intensity or 0),
            _date_key(it.pub_date or ""),
            it.text_id,
        ),
    )
    total_neg = (summary.get("sentiment_distribution") or {}).get("negative", {}).get("count", 0)
    cards = []
    for it in negative_items[:max_overall]:
        cards.append(
            {
                "text_id": it.text_id,
                "text": _denoise_text(it.text),
                "platform": it.platform,
                "date": it.pub_date or "",
                "keyword": it.keyword or "",
                "dimension": "",
                "sentiment": "negative",
                "judge": "llm" if getattr(it, "method", "") == "llm" else "lexicon",
                "need_review": bool(getattr(it, "need_review", False)),
                "score": float(it.sentiment_score or 0.0),
                "intensity": int(it.intensity or 0),
                "n": total_neg,
            }
        )
    return cards


def _stat_cards(summary: dict) -> list[dict]:
    """统计证据卡（kind=stat）：为统计/聚合类结论提供可引用的真实数字。

    解决"强情绪占比 43% / 走势下降 / 维度负面率 80%"这类结论被 LLM 挂到
    单条原文上的问题——统计结论应由统计卡支撑，而非任意一条文本。
    """
    cards: list[dict] = []
    dist = summary.get("sentiment_distribution") or {}
    total = int(summary.get("total_items") or 0)
    if total:
        def _r(key):
            v = dist.get(key, {})
            return int(v.get("count") or 0), (v.get("ratio") or 0) * 100
        pn, pr = _r("positive")
        nn, nr = _r("negative")
        un, ur = _r("neutral")
        cards.append(
            {
                "id": "",
                "kind": "stat",
                "stat_key": "overall",
                "text": (
                    f"整体情感分布：正面 {pn} 条（{pr:.0f}%）、负面 {nn} 条（{nr:.0f}%）、"
                    f"中性 {un} 条（{ur:.0f}%）；共 {total} 条，平均分 {summary.get('avg_score', 0)}。"
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
    # 平台统计卡
    for pid, v in (summary.get("platforms") or {}).items():
        posts = int(v.get("posts") or 0)
        cards.append(
            {
                "id": "",
                "kind": "stat",
                "stat_key": f"platform:{pid}",
                "text": (
                    f"平台「{platform_cn(pid)}」：帖子 {posts} 条，平均分 {v.get('avg_score', 0)}，"
                    f"正面/负面/中性 = {v.get('positive', 0)}/{v.get('negative', 0)}/{v.get('neutral', 0)}。"
                ),
                "platform": pid,
                "date": "",
                "dimension": "",
                "sentiment": "",
                "judge": "stat",
                "need_review": False,
                "n": posts,
            }
        )
    # 维度统计卡（与 summary 同口径）
    for did, v in (summary.get("dimensions") or {}).items():
        count = int(v.get("count") or 0)
        neg = int(v.get("negative") or 0)
        if count < MIN_DIM_RATING:
            rate_txt = "样本不足（n<3），未评级"
        else:
            rate_txt = f"负面率 {v.get('negative_rate', 0) * 100:.0f}%"
        cards.append(
            {
                "id": "",
                "kind": "stat",
                "stat_key": f"dimension:{did}",
                "text": (
                    f"维度「{dimension_cn(did)}」：讨论 {count} 条，负面 {neg} 条，{rate_txt}。"
                ),
                "platform": "",
                "date": "",
                "dimension": did,
                "sentiment": "",
                "judge": "stat",
                "need_review": False,
                "n": count,
            }
        )
    # 走势统计卡
    trend = summary.get("trend") or {}
    if trend:
        # 2026-08-21：无日期文本（WebSearch 等）不进入时间轴，避免"讨论高峰在 未知"
        dated = {d: v for d, v in trend.items() if d != "未知"}
        unknown_n = int(trend.get("未知", {}).get("count") or 0)
        dates = sorted(dated.keys())
        if len(dates) >= 2:
            half = len(dates) // 2
            a = _avg_trend(dates[:half], trend)
            b = _avg_trend(dates[half:], trend)
            direction = "上升" if b > a + 0.05 else ("下降" if b < a - 0.05 else "平稳")
            peak = max(dates, key=lambda d: trend[d]["count"])
            worst = max(dates, key=lambda d: trend[d]["negative"])
            tail = f"；另有 {unknown_n} 条无日期未计入趋势" if unknown_n else ""
            cards.append(
                {
                    "id": "",
                    "kind": "stat",
                    "stat_key": "trend",
                    "text": (
                        f"情感走势整体{direction}：前半段均分 {a} → 后半段均分 {b}；"
                        f"讨论高峰在 {peak}（{trend[peak]['count']} 条），"
                        f"负面量峰值在 {worst}（{trend[worst]['negative']} 条）{tail}。"
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
    # 强度统计卡
    intensity = summary.get("intensity_distribution") or {}
    if intensity:
        counts = {int(k): int(v) for k, v in intensity.items()}
        strong = counts.get(4, 0) + counts.get(5, 0)
        total_i = sum(counts.values()) or 1
        cards.append(
            {
                "id": "",
                "kind": "stat",
                "stat_key": "intensity",
                "text": (
                    f"情绪强度：1～5 级分布为 {counts.get(1,0)}/{counts.get(2,0)}/"
                    f"{counts.get(3,0)}/{counts.get(4,0)}/{counts.get(5,0)}；"
                    f"强情绪（4～5 级）共 {strong} 条，占比 {strong / total_i * 100:.1f}%。"
                ),
                "platform": "",
                "date": "",
                "dimension": "",
                "sentiment": "",
                "judge": "stat",
                "need_review": False,
                "n": total_i,
            }
        )
    return cards


def _avg_trend(keys: list[str], trend: dict) -> float:
    if not keys:
        return 0.0
    return round(sum(trend[k]["avg_score"] for k in keys) / len(keys), 3)


def build_evidence(
    items: list,
    summary: dict,
    exclude_ad: bool = False,
    max_per_dim: int = 3,
    max_topic: int = 3,
    max_overall: int = 5,
) -> list[dict]:
    """证据卡抽取：原文卡（维度级 → 话题反查 → 整体负面 TOP）+ 统计卡（确定性规则）。"""
    if not items or not summary.get("total_items"):
        return []
    stat = _stat_items(items, exclude_ad)
    cards = _dimension_cards_for(stat, max_per_dim)
    used = {c["text_id"] for c in cards}
    cards.extend(_topic_cards(stat, summary, used, max_topic))
    cards.extend(_overall_negative_cards(stat, summary, used, max_overall))
    # 稳定 id：负面维度卡 → 话题卡 → 整体卡
    for i, c in enumerate(sorted(cards, key=_card_sort_key), start=1):
        c["id"] = f"E{i}"
        c["kind"] = "text"
        c["dimension_name"] = dimension_cn(c["dimension"]) if c.get("dimension") else ""
    # 统计卡放在原文卡之后，id 用 S 前缀
    stat_cards = _stat_cards(summary)
    for i, c in enumerate(stat_cards, start=1):
        c["id"] = f"S{i}"
        c["dimension_name"] = dimension_cn(c["dimension"]) if c.get("dimension") else ""
    return cards + stat_cards


def _dim_bucket_stats(evidence: list[dict]) -> dict[str, dict]:
    """按维度聚合证据卡（n 为该维度×判定的样本数，与 summary 同口径）。"""
    by_dim: dict[str, dict[str, Any]] = {}
    for c in evidence:
        if c.get("kind") == "stat":
            continue
        dim = c.get("dimension") or ""
        if not dim:
            continue
        b = by_dim.setdefault(
            dim,
            {"n_neg": 0, "n_pos": 0, "refs_neg": [], "refs_pos": [], "need_review": 0},
        )
        if c["sentiment"] == "negative":
            b["n_neg"] = max(b["n_neg"], int(c.get("n") or 0))
            b["refs_neg"].append(c["id"])
        elif c["sentiment"] == "positive":
            b["n_pos"] = max(b["n_pos"], int(c.get("n") or 0))
            b["refs_pos"].append(c["id"])
        if c.get("need_review"):
            b["need_review"] += 1
    return by_dim


def _finding_claim(
    dim_name: str, n: int, rate: float, mode: str, need_review_n: int = 0
) -> str:
    marker = ""
    if n < MIN_DIM_NORMAL:
        marker = "，样本有限"
    suffix = f"（含待复核样本 {need_review_n} 条）" if need_review_n else ""
    prefix = "疑似" if mode == "lexicon" else ""
    # 负面率 <50% 时不说"集中在"，改"涉及"，避免过度断言（病灶 E 同类问题）
    if rate >= 50.0:
        body = f"{prefix}负面集中在「{dim_name}」相关讨论"
    else:
        body = f"{prefix}负面讨论涉及「{dim_name}」话题"
    return (
        f"{body}（n={n}，负面率 {rate:.0f}%{marker}{suffix}）"
    )


def build_findings(
    evidence: list[dict],
    summary: dict,
    mode: str = "lexicon",
    max_findings: int = FINDINGS_MAX,
) -> list[dict]:
    """规则 findings 生成器（双模式共用内核，零数据返回空）。

    - lexicon：事实性发现（真实数字 + n） + 方向性提示，不出现强动作建议；
    - fallback：LLM 失败兜底，措辞同规则内核，标注模板兜底来源。
    负面占比高于正面时绝不允许出现"中性/正面为主"表述。
    """
    if not summary.get("total_items"):
        return []
    mode = mode if mode in ("lexicon", "fallback", "review") else "lexicon"
    findings: list[dict] = []

    dist = summary.get("sentiment_distribution") or {}
    neg_n = int(dist.get("negative", {}).get("count") or 0)
    pos_n = int(dist.get("positive", {}).get("count") or 0)
    total = int(summary.get("total_items") or 0)

    # 1) 整体舆情发现（始终第一条；数字为算术事实，不需要"疑似"）
    overall_refs = [
        c["id"]
        for c in evidence
        if c.get("kind") == "stat" and c.get("stat_key") == "overall"
    ][:1]
    if neg_n > pos_n:
        overall_claim = (
            f"整体讨论中负面占比高于正面"
            f"（负面 {dist['negative']['ratio'] * 100:.0f}%，n={neg_n}；"
            f"正面 {dist['positive']['ratio'] * 100:.0f}%，n={pos_n}；共 {total} 条）"
        )
    elif pos_n > neg_n:
        overall_claim = (
            f"整体讨论以正面为主"
            f"（正面 {dist['positive']['ratio'] * 100:.0f}%，n={pos_n}；"
            f"负面 {dist['negative']['ratio'] * 100:.0f}%，n={neg_n}；共 {total} 条）"
        )
    else:
        overall_claim = f"整体正负面相当（正面 n={pos_n}，负面 n={neg_n}，共 {total} 条）"
    findings.append(
        {
            "id": "F1",
            "claim": overall_claim,
            "evidence_refs": overall_refs,
            "action": _directional_hint(mode, small_sample=(total < MIN_DIM_NORMAL)),
            "narrative_label": "",
        }
    )

    # 2) 维度发现：n>=10 正常评级优先，3~9 标样本有限且不排前（≤4 条）
    by_dim = _dim_bucket_stats(evidence)
    rows = []
    for dim, b in by_dim.items():
        if b["n_neg"] < MIN_DIM_RATING:
            continue  # n<3 不评级（两档守卫第一档）
        total_dim = b["n_neg"] + b["n_pos"]
        rate = b["n_neg"] / total_dim if total_dim else 0.0
        rows.append(
            {
                "dim": dim,
                "name": dimension_cn(dim),
                "n": b["n_neg"],
                "rate": rate,
                "normal": b["n_neg"] >= MIN_DIM_NORMAL,
                "need_review": b["need_review"],
                "stat_ref": next(
                    (
                        c["id"]
                        for c in evidence
                        if c.get("stat_key") == f"dimension:{dim}"
                    ),
                    "",
                ),
                "refs": b["refs_neg"][:2],
                "score": b["n_neg"] * rate,
            }
        )
    rows.sort(key=lambda r: (-r["normal"], -r["score"], r["dim"]))
    for i, r in enumerate(rows[: FINDINGS_MAX - 1], start=2):
        refs = ([r["stat_ref"]] if r["stat_ref"] else []) + r["refs"]
        findings.append(
            {
                "id": f"F{i}",
                "claim": _finding_claim(
                    r["name"], r["n"], r["rate"] * 100, mode, r["need_review"]
                ),
                "evidence_refs": refs,
                "action": _directional_hint(mode, small_sample=not r["normal"]),
                "narrative_label": "",
            }
        )
    return findings[:max_findings]


def _directional_hint(mode: str, small_sample: bool = False) -> str:
    """方向性提示（词典/兜底模式专用；绝不给危机公关/质检报告/促销策略类强动作）。"""
    if mode == "lexicon":
        hint = "建议开启 LLM 精分析，获取可归因的结论与具体行动建议后再制定下一步。"
    elif mode == "review":
        hint = (
            "结论已按人工复核结果用规则重新生成，仅供参考；"
            "如需 LLM 深度结论，请在结果页重新生成。"
        )
    else:
        hint = "本次深度结论由规则模板生成（LLM 兜底），建议检查 LLM 配置后重新生成。"
    if small_sample:
        hint = "该部分样本有限，建议扩大采集范围后再验证。\n" + hint
    return hint


def findings_to_conclusion(findings: list[dict]) -> str:
    """findings → 结论文本（向后兼容旧 conclusion 消费方）。"""
    lines = []
    for f in findings:
        lines.append(f"{f.get('id', '')} {f.get('claim', '')}")
        action = f.get("action", "")
        if action:
            lines.append(f"建议：{action}")
    return "\n".join(lines)


def findings_section_title(mode: str) -> str:
    """结论区标题随模式变化（词典模式不再叫"按叙事框架"，避免名不副实）。"""
    return {
        "llm": "核心发现与行动建议",
        "lexicon": "数据发现（词典模式）",
        "template_fallback": "核心发现与行动建议（模板兜底）",
        "review_refresh": "数据发现（已按复核结果刷新）",
        "no_data": "结论",
    }.get(mode, "深度结论与建议（按叙事框架）")


_FINDING_ID_RE = re.compile(r"(?<![A-Za-z0-9])F(\d{1,2})(?![A-Za-z0-9])")


def display_finding_id(fid: str | None) -> str:
    """内部发现编号 F1 → 展示文案"发现 1"（内部 id 保持 F1 不变）。"""
    if not fid:
        return ""
    m = re.fullmatch(r"F(\d{1,2})", str(fid))
    return f"发现 {m.group(1)}" if m else str(fid)


def display_action(action: str | None) -> str:
    """行动建议里的发现引用（对应F1）→（对应发现1），仅展示层使用。"""
    if not action:
        return action or ""
    return _FINDING_ID_RE.sub(lambda m: f"发现{m.group(1)}", str(action))


def dimension_evidence_label(cards: list[dict]) -> str:
    """维度负面原文区块的标题后缀：负面样本数 n + 判定来源聚合（LLM/词典/混合）。

    2026-08-18 修订 4：n 是"维度×情感"桶的样本数，属于维度级信息，
    从每条评论旁移到维度标题上，避免重复刷屏与误读。
    """
    text_cards = [c for c in cards if c.get("kind") != "stat"]
    n = max((int(c.get("n") or 0) for c in text_cards), default=0)
    judges = {c.get("judge") for c in text_cards if c.get("judge")}
    if judges == {"llm"}:
        j = "LLM 判定"
    elif judges == {"lexicon"}:
        j = "词典判定 · 仅供参考"
    elif judges:
        j = "判定混合（" + " / ".join(sorted(judges)) + "）"
    else:
        j = ""
    return f"负面样本 n={n}" + (f" · {j}" if j else "")


def _llm_finding_valid(f: Any, valid_ids: set[str], evidence: list[dict]) -> bool:
    """单条 LLM finding 校验（2026-08-18 放宽版）：

    - 含非空 claim / action，action 长度 ≥12（避免空泛短语）；
    - evidence_refs 必须全部来自给定证据清单（存在性校验，防幻觉引文）；
    - action 必须含具体渠道/平台标记（验收标准 2 的可自动检查部分）
      或引用发现编号/证据编号。
    - 结论若提到某个维度/平台名，引用的证据里必须包含该维度/平台的卡
      （统计卡或原文卡均可，防止跨主题引用）。

    说明：action 与发现编号的"挂接"由 JSON 结构天然满足（action 在 findings
    对象内）；不再强制把 F#/E# 写进 action 正文——真实 LLM 输出常省略，
    强制只会把高质量结果误杀降级（罗技/OPPO 验收样本即此问题）。
    """
    if not isinstance(f, dict):
        return False
    claim = str(f.get("claim") or "").strip()
    action = str(f.get("action") or "").strip()
    refs = f.get("evidence_refs") or []
    if not claim or len(action) < 12:
        return False
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        return False
    if refs and not all(r in valid_ids for r in refs):
        return False
    fid = str(f.get("id") or "")
    linked = bool(fid and fid in action) or any(r in action for r in refs)
    has_channel = any(marker in action for marker in CHANNEL_MARKERS)
    # 结论提到维度/平台名 → 引用必须包含对应卡（维度/平台一致性）
    ref_cards = [c for c in evidence if c.get("id") in refs]
    ref_dims = {c.get("dimension_name") for c in ref_cards if c.get("dimension_name")}
    mentioned_dims = [
        dn
        for dn in {c.get("dimension_name") for c in evidence if c.get("dimension_name")}
        if dn and dn in claim
    ]
    if mentioned_dims and not any(dn in ref_dims for dn in mentioned_dims):
        return False
    ref_platforms = {
        platform_cn(c.get("platform")) for c in ref_cards if c.get("platform")
    }
    mentioned_platforms = [
        pn
        for pn in {platform_cn(c.get("platform")) for c in evidence if c.get("platform")}
        if pn and pn in claim
    ]
    if mentioned_platforms and not any(pn in ref_platforms for pn in mentioned_platforms):
        return False
    # 必须满足：挂接（action 含 F#/E#）或有具体渠道标记，两者至少其一
    return linked or has_channel


def validate_llm_findings(findings: Any, evidence: list[dict]) -> bool:
    """整组 LLM findings 校验（≤ FINDINGS_MAX 且每条都过单条校验）。"""
    if not isinstance(findings, list) or not findings:
        return False
    if len(findings) > FINDINGS_MAX:
        return False
    valid_ids = {c.get("id") for c in evidence}
    return all(_llm_finding_valid(f, valid_ids, evidence) for f in findings)


def filter_llm_findings(
    findings: Any, evidence: list[dict], max_findings: int = FINDINGS_MAX
) -> tuple[list[dict], int]:
    """按条过滤非法 finding（坏条丢弃，不连累整组），返回 (保留, 丢弃数)。"""
    if not isinstance(findings, list):
        return [], 0
    valid_ids = {c.get("id") for c in evidence}
    kept = [
        f for f in findings
        if _llm_finding_valid(f, valid_ids, evidence)
    ][:max_findings]
    return kept, max(len(findings) - len(kept), 0)
