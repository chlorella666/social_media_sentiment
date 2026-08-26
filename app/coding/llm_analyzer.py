"""大模型情感分析器。

- 接口统一：analyze_batch / analyze_narrative / ping
- OpenAI 兼容接口（/chat/completions），真批量请求 + 内存缓存
- 未配置 API Key 时自动降级为 MockAnalyzer（词典规则），保证离线可跑
- 失败不静默：错误信息记录到 errors，由管道层上报给用户
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import requests

from app.coding.cleaner import desensitize_text
from app.coding import lexicon

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
CONFIDENCE_THRESHOLD = 0.8  # 预筛置信度低于此值才调用 LLM（0.8 更保守，送 LLM 占比更高）
BATCH_SIZE = 10  # 单请求文本数（越小单请求越快、进度越平滑）
NARRATIVE_BATCH_SIZE = 6  # 叙事/归因批大小（2026-08-16 由 10 调小：输出体量更小，降低截断与 index 漂移概率）
PROMPT_VERSION = 14  # 情感分析提示词版本（v3.8：维度级召回强化，规则 19a~19g + 20）
# 采用"整条情感跟锚定品牌"口径的领域（抽样规范 §十四：品牌口碑场景）。
# 主集/边界集沿用"文本自身情感"口径，不在此列；评测与生产链路据此决定是否传 subject。
SUBJECT_DOMAINS = frozenset({"digital3c"})


def uses_subject_domain(domain_id: str | None) -> bool:
    """是否走「整条情感跟锚定品牌」口径。

    2026-08-17 模块卷标注口径确认（对齐抽样规范 §十四）：
    - digital3c 与 modules_physical / modules_service（及含实物/服务的组合）=
      品牌口碑场景，情感锚定报告/计划主题品牌（比较/站队类文本对目标品牌判褒贬）；
    - modules_content（纯数字内容产品）沿用主集游戏口径 = 文本自身情感。
    生产 coder 与 benchmark 同口径。
    """
    return bool(domain_id) and (
        domain_id in SUBJECT_DOMAINS
        or (domain_id.startswith("modules_") and domain_id != "modules_content")
    )
# 校准 spike（2026-08-17）结论：词典直判置信度不可信（3C conf≥0.8 直判 49% 错误率，
# 贡献 29% 错误；LLM 同桶仅 6% 错）→ 相关领域全量送 LLM（词典只做预筛），
# 费用仍受控。评测与生产链路同口径。
# 2026-08-17 模块化分类：数字产品/有形实物/服务内容对应 game/consumer/digital3c
# 与全部 modules_* 合成领域，全部走全量 LLM（提准优先）。
ALWAYS_LLM_DOMAINS = frozenset({"digital3c", "game", "consumer"})


def is_always_llm_domain(domain_id: str | None) -> bool:
    """模块化分类（2026-08-17）后的全量 LLM 判定：已评测领域 + 模块合成领域。"""
    return bool(domain_id) and (
        domain_id in ALWAYS_LLM_DOMAINS or domain_id.startswith("modules_")
    )

# v3.4 规则开关（拆分归因用）：SMS_V34_RULES 覆盖默认集；
# 置空 = 回到 v3.3+B；单独指定子集 = 单规则消融。
# 2026-08-16 消融结论：v14 主力（+2.3pp）、v12/v13 各 +1.4pp、v11（报道/资讯）
# 净零且过度中性化（pos→neu +4/neg→neu +2，r2 回退 -5.7pp 最可能元凶）→ 默认去掉 11。
_V34_RULES_DEFAULT = "12,13,14"

# v3.5 规则开关（同 v3.4 模式）：SMS_V35_RULES 覆盖默认集；置空 = 回到 v3.4b。
# 2026-08-16 第四轮取证（v3.4b 残余 144 错）：
#   neutral→positive 43（报道/功能陈述/求知问句）、positive→neutral 35（隐晦夸赞/
#   正面报道）、negative→neutral 28（困扰问句被当 neutral）。
_V35_RULES_DEFAULT = "15,16,17"

# v3.6 规则开关（模块化分类 2026-08-17）：SMS_V36_RULES 覆盖默认集；
# 置空 = 回到 v3.5。实物卷取证：negative 漏检 10 条全部为隐晦负面
# （价格过高/功能缺陷/不实用/含蓄差评）→ 规则 18 few-shot 定向补召回。
_V36_RULES_DEFAULT = "18"

# v3.8 规则开关（维度级召回强化，2026-08-21）：SMS_V38_RULES 覆盖默认集；
# 置空 = 回到 v3.7。取证（主集 golden_set_v1，PV=13）：维度微平均 F1 0.561 /
# 召回 0.503，76 条错误中漏检 67 次（角色偏好 16/品牌形象 10/竞品对比 7/渠道服务 7/
# 运营社区 7/剧情氪金各 5…）、多标 35 次（使用体验 9 字面词误触发…）、
# empty_pred 19（长文本整段漏维度）→ 19a 双信号判维 / 19b 长文分段 /
# 19c 游戏侧边界 / 19d 品牌形象语境 / 19e 竞品对比边界 / 19f 渠道服务语境 /
# 19g 字面词去噪 / 19h 页面壳去噪 / 20 emoji 维度依据。
# 消融结论（2026-08-21，主集 n=175）：19b 单独 = 整条 82.9%/维度 F1 0.54（双输）；
# 去掉 19b 后整条 86.9%（> 基线 86.0）+ 维度 F1 0.64/召回 0.58/精度 0.71，
# 全指标优于全开 → **19b 移出默认**（代码保留为可选开关，重做需收窄到
# "明确多话题长文"再消融）。
_V38_RULES_DEFAULT = "19a,19c,19d,19e,19f,19g,19h,20"


def _v35_enabled(rule: str) -> bool:
    return rule in os.environ.get("SMS_V35_RULES", _V35_RULES_DEFAULT).split(",")


def _v36_enabled(rule: str) -> bool:
    return rule in os.environ.get("SMS_V36_RULES", _V36_RULES_DEFAULT).split(",")


def _v38_enabled(rule: str) -> bool:
    return rule in os.environ.get("SMS_V38_RULES", _V38_RULES_DEFAULT).split(",")


def _v34_enabled(rule: str) -> bool:
    return rule in os.environ.get("SMS_V34_RULES", _V34_RULES_DEFAULT).split(",")
# v2.2（PROMPT_VERSION=4）：官方内容一律 neutral、教程/攻略即使含"神器/推荐"也 neutral；
# v3.0（2.4）：整条情感规则沿用 v2.2 原文不动，追加 dimension_sentiments 维度级情感输出
# （只标"明确带情感"的维度；转折句逐维拆解；官方内容维度留空）。
# v3.1（2.5 数码3C 首版裁判 68.8%，失败模式=neutral 被误判情感）：扩充 3C 语体规则——
# 产品介绍/开箱/参数罗列/标题聚合页/提问求助/官方高管言论/活动宣发一律 neutral；
# 竞品对比识别补充（谁更强/对比/差距/平替/参数对比）。
# v3.2（3C 两轮 70.1/72.2% 仍 <75%，剩余错误仍是同一批 neutral→positive）：neutral
# 升为总则（无明确个人立场一律 neutral，提到产品/参数/品牌不等于情感）+ 3C 反例 +
# 维度"选择/支持某品牌→品牌形象"。
# v3.3（3C golden v2 取证 166 错：语体类 72 条占 43%，含标题问句/参数盘点/犹豫权衡/
# 无立场短评）：① 含蓄表达三分（先判有没有立场再判方向）；② 无立场语体补强
# （标题型问句/系列型号介绍/犹豫权衡 → neutral）；③ 多品牌比较无锚定 → 整条 neutral +
# 竞品对比维度按内容标方向；④ 竞品对比维度信号词扩充。
# v3.4（A+B 后残余 154 错取证）：① 报道/资讯/电商页/百科/活动打卡语体 → neutral
# （即使含"点名表扬/遥遥领先/大场面"）；② 行动信号=正面（买了/入手/下单/到店安排），
# 问题未解决/被劝退/吐槽体验=负面（平淡语气也算）；③ 调侃反讽与方向不明（"天壤之别"
# 类）→ neutral，反讽对象为产品 → negative；④ subject 口径强化：夸赞/推荐非锚定品牌
# 时整条不得判 positive（对本品牌 negative 或 neutral）。

VALID_NARRATIVES = {"conflict", "human_interest", "attribution", "economic", "morality"}
VALID_ACTORS = {
    "government", "enterprise", "individual", "system",
    "technology", "society", "nature", "unclear",
}


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL


class BaseAnalyzer:
    """情感分析器接口。"""

    def analyze_batch(
        self,
        texts: list[str],
        on_batch_progress: "Callable[[int, int], None] | None" = None,
        dimension_schema: dict | None = None,
    ) -> list[dict]:
        """输入文本列表，返回 [{sentiment, score, confidence, keywords, dimension_sentiments?}]。"""
        raise NotImplementedError

    def analyze_narrative(
        self,
        texts: list[str],
        on_batch_progress: "Callable[[int, int], None] | None" = None,
    ) -> list[dict]:
        """输入文本列表，返回 [{narrative, attribution}]（可选分析）。"""
        raise NotImplementedError

    @property
    def errors(self) -> list[str]:
        """最近一次分析的错误信息（为空表示无错误）。"""
        return []

    def ping(self) -> tuple[bool, str]:
        """验证配置可用性，返回 (ok, message)。"""
        return True, "词典模式无需连接"

    def generate_insights(
        self, descriptors: dict, evidence: list[dict] | None = None,
        summary: dict | None = None,
    ) -> dict:
        """基于统计描述（+证据清单）生成图表解析与核心发现。"""
        raise NotImplementedError


class MockAnalyzer(BaseAnalyzer):
    """无 API Key 时的降级实现：词典规则。"""

    def analyze_batch(
        self,
        texts: list[str],
        on_batch_progress: "Callable[[int, int], None] | None" = None,
        dimension_schema: dict | None = None,
    ) -> list[dict]:
        results = []
        for t in texts:
            r = lexicon.score_text(t)
            results.append(
                {
                    "sentiment": r["sentiment"],
                    "score": r["score"],
                    "confidence": r["confidence"],
                    "keywords": r["keywords"],
                    "dimension_sentiments": {},
                }
            )
        return results

    def analyze_narrative(
        self,
        texts: list[str],
        on_batch_progress: "Callable[[int, int], None] | None" = None,
    ) -> list[dict]:
        return [{"narrative": None, "attribution": None} for _ in texts]

    def ping(self) -> tuple[bool, str]:
        return True, "词典模式无需连接"

    def generate_insights(
        self, descriptors: dict, evidence: list[dict] | None = None,
        summary: dict | None = None,
    ) -> dict:
        """词典模式：有 summary/evidence 时直接走规则 findings（不再落静态模板）。"""
        from app.coding.insights import template_insights

        if summary is not None:
            from app.core.evidence import build_findings, findings_to_conclusion

            findings = build_findings(evidence or [], summary, mode="lexicon")
            return {
                "chart_insights": template_insights(descriptors)["chart_insights"],
                "conclusion": findings_to_conclusion(findings),
                "findings": findings,
            }
        return template_insights(descriptors)


def schema_dimensions(schema) -> list[dict]:
    """把维度 schema 归一为 [{id, name, keywords, description}]（兼容 DomainSchema/dict）。"""
    if not schema:
        return []
    if hasattr(schema, "dimensions"):
        dims = schema.dimensions
    elif isinstance(schema, dict) and "dimensions" in schema:
        dims = schema["dimensions"]
    elif isinstance(schema, dict):
        dims = [dict(v, id=k) for k, v in schema.items()]
    elif isinstance(schema, (list, tuple)):
        dims = schema
    else:
        return []
    out = []
    for d in dims:
        out.append({
            "id": d.id if hasattr(d, "id") else d.get("id", ""),
            "name": d.name if hasattr(d, "name") else d.get("name", ""),
            "keywords": list(d.keywords) if hasattr(d, "keywords") else list(d.get("keywords", [])),
            "description": (d.description if hasattr(d, "description")
                            else d.get("description", "")) or "",
        })
    return [d for d in out if d["id"]]


def sanitize_dimension_sentiments(items: list[dict], valid_ids: set) -> list[dict]:
    """清洗维度情感：只保留 schema 内维度 id 与 positive/negative 取值，非法值剔除。"""
    for it in items:
        raw = it.get("dimension_sentiments") or {}
        if not isinstance(raw, dict):
            raw = {}
        clean = {}
        for k, v in raw.items():
            k = str(k).strip()
            v = str(v).strip().lower()
            if k in valid_ids and v in ("positive", "negative"):
                clean[k] = v
        it["dimension_sentiments"] = clean
    return items


class IncompleteBatchError(ValueError):
    """批量结果缺失部分 index：携带已返回条目与缺失列表，供定向补全恢复。"""

    def __init__(self, missing: list[int], partial: dict[int, dict]):
        self.missing = sorted(missing)
        self.partial = dict(partial)
        super().__init__(f"模型返回条目不完整（缺失 index {self.missing}）")


class OpenAICompatibleAnalyzer(BaseAnalyzer):
    """OpenAI 兼容接口分析器（requests 直连）。"""

    def __init__(self, config: LLMConfig, max_workers: int = 4, batch_size: int = BATCH_SIZE):
        self.config = config
        self.max_workers = max_workers
        self.batch_size = batch_size
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._errors: list[str] = []
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0

    @property
    def usage(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }

    @property
    def errors(self) -> list[str]:
        return self._errors

    def _post_chat(
        self,
        system: str,
        user: str,
        *,
        use_json: bool = True,
        max_tokens: int | None = None,
        timeout: int = 60,
    ) -> tuple[str, str]:
        """发送一次 chat 请求，返回 (content, finish_reason)。

        finish_reason="length" 表示输出被 max_tokens 截断，调用方据此拆批重试。
        """
        payload: dict = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        # deepseek-reasoner 不支持 temperature / response_format，需省略
        is_reasoner = "reasoner" in self.config.model.lower()
        if not is_reasoner:
            payload["temperature"] = 0.2
        if use_json and not is_reasoner:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        resp = requests.post(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # 累计 token 用量（费用可见性；DeepSeek 返回 usage 字段）
        usage = data.get("usage") or {}
        with self._lock:
            self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.completion_tokens += int(usage.get("completion_tokens") or 0)
        choice = (data.get("choices") or [{}])[0]
        return choice["message"]["content"], choice.get("finish_reason") or ""

    def _chat(
        self,
        system: str,
        user: str,
        *,
        use_json: bool = True,
        max_tokens: int | None = None,
        timeout: int = 60,
    ) -> str:
        content, _ = self._post_chat(
            system, user, use_json=use_json, max_tokens=max_tokens, timeout=timeout
        )
        return content

    def ping(self, timeout: int = 20) -> tuple[bool, str]:
        """发送最小请求，验证 Key / Base URL / 模型名是否可用。"""
        try:
            self._chat(
                "你是情感分析助手。",
                "测试",
                use_json=False,
                max_tokens=4,
                timeout=timeout,
            )
            return True, "连接正常"
        except Exception as exc:
            return False, self._friendly_error(exc)

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        msg = str(exc)
        low = msg.lower()
        if "401" in msg or "authentication" in low or "invalid" in low and "key" in low:
            return "API Key 无效或未通过认证（401）"
        if "402" in msg or "insufficient balance" in low or "balance" in low:
            return "账户余额不足（402），请检查 DeepSeek/OpenAI 账户余额"
        if "404" in msg:
            return "接口地址不存在（404），请检查 Base URL（DeepSeek 为 https://api.deepseek.com）"
        if "model" in low and ("not exist" in low or "not found" in low):
            return "模型名不存在，请检查（DeepSeek 用 deepseek-chat 或 deepseek-reasoner）"
        if "connect" in low or "timeout" in low or "timed out" in low or "sslerror" in low:
            return "网络连接失败或超时，请检查 Base URL、代理与网络"
        if "json" in low:
            return "模型未返回合法 JSON，请重试或更换模型"
        return msg[:200]

    @staticmethod
    def _parse_batch(content: str, n: int) -> list[dict]:
        """解析批量结果：优先 {"items":[...]}，按 index 对齐。"""
        data = json.loads(content)
        items = data.get("items", data if isinstance(data, list) else [])
        if not items:
            raise ValueError("模型返回为空")
        if isinstance(items[0], dict) and "index" in items[0]:
            by_index = {int(it["index"]): it for it in items}
            missing = [i for i in range(n) if i not in by_index]
            if missing:
                raise IncompleteBatchError(missing, by_index)
            return [by_index[i] for i in range(n)]  # type: ignore[return-value]
        return list(items)[:n]

    def _lexicon_fallback(self, texts: list[str]) -> list[dict]:
        results = []
        for t in texts:
            r = lexicon.score_text(t)
            results.append(
                {
                    "sentiment": r["sentiment"],
                    "score": r["score"],
                    "confidence": r["confidence"],
                    "keywords": r["keywords"],
                }
            )
        return results

    def _structured_batch(
        self,
        texts: list[str],
        *,
        system: str,
        user_fn: "Callable[[list[str]], str]",
        max_tokens_fn: "Callable[[int], int]",
        parse_fn: "Callable[[str, int], list[dict]]",
        sanitize_fn: "Callable[[list[dict]], list[dict]]",
        fallback_fn: "Callable[[list[str]], list[dict]]",
        error_label: str,
        error_suffix: str,
        min_split: int = 3,
        complete_missing: bool = False,
    ) -> list[dict]:
        """单批结构化 LLM 请求的统一处理：截断检测 + 拆小批重试 + 兜底。

        - finish_reason=="length"（输出被 max_tokens 截断）→ 抛"条目不完整"；
        - 截断且批次 > min_split → 对半拆分递归重试，分批后更易完整返回；
        - 缺失 index（纯格式失败）且 complete_missing=True 时，小批先做一次
          "只补全缺失条目"的定向二次请求，合并成功则不降级；
        - 小批仍失败 → 记录错误并返回 fallback（不中断主流程）。
        返回与 texts 等长、按 index 对齐的结果列表。
        """
        try:
            content, finish = self._post_chat(
                system,
                user_fn(texts),
                max_tokens=max_tokens_fn(len(texts)),
            )
            if finish == "length":
                raise ValueError("模型返回条目不完整（输出被截断）")
            return sanitize_fn(parse_fn(content, len(texts)))
        except IncompleteBatchError as exc:
            if len(texts) > min_split:
                mid = len(texts) // 2
                return self._structured_batch(
                    texts[:mid],
                    system=system,
                    user_fn=user_fn,
                    max_tokens_fn=max_tokens_fn,
                    parse_fn=parse_fn,
                    sanitize_fn=sanitize_fn,
                    fallback_fn=fallback_fn,
                    error_label=error_label,
                    error_suffix=error_suffix,
                    min_split=min_split,
                    complete_missing=complete_missing,
                ) + self._structured_batch(
                    texts[mid:],
                    system=system,
                    user_fn=user_fn,
                    max_tokens_fn=max_tokens_fn,
                    parse_fn=parse_fn,
                    sanitize_fn=sanitize_fn,
                    fallback_fn=fallback_fn,
                    error_label=error_label,
                    error_suffix=error_suffix,
                    min_split=min_split,
                    complete_missing=complete_missing,
                )
            if complete_missing:
                merged = self._complete_missing_indexes(
                    texts,
                    exc,
                    system=system,
                    user_fn=user_fn,
                    max_tokens_fn=max_tokens_fn,
                    sanitize_fn=sanitize_fn,
                )
                if merged is not None:
                    return merged
            self._errors.append(
                f"{error_label}（{len(texts)} 条）：{self._friendly_error(exc)}，{error_suffix}"
            )
            return fallback_fn(texts)
        except Exception as exc:
            if len(texts) > min_split and "条目不完整" in str(exc):
                mid = len(texts) // 2
                return self._structured_batch(
                    texts[:mid],
                    system=system,
                    user_fn=user_fn,
                    max_tokens_fn=max_tokens_fn,
                    parse_fn=parse_fn,
                    sanitize_fn=sanitize_fn,
                    fallback_fn=fallback_fn,
                    error_label=error_label,
                    error_suffix=error_suffix,
                    min_split=min_split,
                    complete_missing=complete_missing,
                ) + self._structured_batch(
                    texts[mid:],
                    system=system,
                    user_fn=user_fn,
                    max_tokens_fn=max_tokens_fn,
                    parse_fn=parse_fn,
                    sanitize_fn=sanitize_fn,
                    fallback_fn=fallback_fn,
                    error_label=error_label,
                    error_suffix=error_suffix,
                    min_split=min_split,
                    complete_missing=complete_missing,
                )
            self._errors.append(
                f"{error_label}（{len(texts)} 条）：{self._friendly_error(exc)}，{error_suffix}"
            )
            return fallback_fn(texts)

    def _complete_missing_indexes(
        self,
        texts: list[str],
        exc: "IncompleteBatchError",
        *,
        system: str,
        user_fn: "Callable[[list[str]], str]",
        max_tokens_fn: "Callable[[int], int]",
        sanitize_fn: "Callable[[list[dict]], list[dict]]",
    ) -> list[dict] | None:
        """缺失 index 的一次性定向补全：只要求模型补全缺失条目，成功后合并。"""
        missing = exc.missing
        try:
            missing_payload = {
                "items": [{"index": i, "text": texts[i]} for i in missing]
            }
            user_msg = (
                "你上一轮输出的 JSON 缺少以下 index："
                + ", ".join(str(i) for i in missing)
                + "。请只补全这些条目，输出格式与 system 指令完全一致，"
                "index 必须使用原值。只输出 JSON，不要其他文字。缺失条目："
                + json.dumps(missing_payload, ensure_ascii=False)
            )
            content, finish = self._post_chat(
                system, user_msg, max_tokens=max_tokens_fn(len(missing))
            )
            if finish == "length":
                return None
            data = json.loads(content)
            items = data.get("items", data if isinstance(data, list) else [])
            completed: dict[int, dict] = {}
            for it in items:
                if isinstance(it, dict) and "index" in it:
                    try:
                        completed[int(it["index"])] = it
                    except (TypeError, ValueError):
                        continue
            out: list[dict | None] = [exc.partial.get(i) for i in range(len(texts))]
            for i in missing:
                if out[i] is None and i in completed:
                    out[i] = completed[i]
            if any(r is None for r in out):
                return None
            return sanitize_fn(out)  # type: ignore[return-value]
        except Exception:
            return None

    def _request_batch(
        self,
        texts: list[str],
        dimension_schema: dict | None = None,
        subject: str | None = None,
    ) -> list[dict]:
        dims = schema_dimensions(dimension_schema)
        dim_block = ""
        v38_blocks: list[str] = []
        if _v38_enabled("19a"):
            v38_blocks.append(
                "19a. 维度判维双信号（v3.8）：标注维度情感 = ① 文本明确讨论该维度"
                "话题（关键词/语境命中）② 对该话题有明确褒贬判断（夸/贬/认可/吐槽/"
                "批评）；弱情感词不是必需——「口碑败了/营销对垒/被惊到了」这类明确"
                "褒贬判断即可标注对应维度；官方内容/纯报道/纯事实陈述仍不标任何维度。")
        if _v38_enabled("19b"):
            v38_blocks.append(
                "19b. 长文本分段扫描（v3.8）：多话题长文本按话题分段逐段判断，"
                "每个明确褒贬的话题都要标出对应维度，不得因整条情感单一而漏掉其他维度；"
                "整条情感仍按全文整体态度判定。")
        if _v38_enabled("19c"):
            v38_blocks.append(
                "19c. 游戏侧维度边界（v3.8）：角色行为/互动/人设/情感投入"
                "（「和祁煜马车里的那一夜」「真让人心疼」）→角色偏好；剧情内容/演出/"
                "关系线/尺度（「双修好涩」「这期是不是太隐晦了」）→剧情评价；卡池/礼包/"
                "皮肤/抽卡/下池/档期（「后悔没有下池」「神卡诞生」）→氪金体验；社区讨论/"
                "官方下场/争议/带节奏/请愿（「敖尹下线争议」「评论区乱成一锅粥」）→"
                "运营与社区；角色互动与剧情演出不标玩法体验。")
        if _v38_enabled("19d"):
            v38_blocks.append(
                "19d. 品牌形象语境（v3.8）：营销/预热/口碑/热度/公关/黑稿/代言/新设计"
                "亮相/被惊到（「营销对垒口碑却败了」「被大疆新设计惊到了」）→品牌形象，"
                "方向按褒贬（口碑败了=negative、被惊到=positive）。")
        if _v38_enabled("19e"):
            v38_blocks.append(
                "19e. 竞品对比边界（v3.8）：仅当明确比较两个及以上品牌/产品（对比/不如/"
                "更/还是XX好/问题更多/选择哪个/差距）→竞品对比；无对比对象的「最屌/之王/"
                "建议先买入门款」等理性建议不标竞品对比。")
        if _v38_enabled("19f"):
            v38_blocks.append(
                "19f. 渠道服务语境（v3.8）：门店/客流/冷清/员工服务/逛店/线下体验/卖场"
                "（「冷清得可怕，一个客人都没有」「工作人员也看不到」）→渠道服务（或当前"
                "清单中最接近的门店/环境维度），方向按褒贬。")
        if _v38_enabled("19g"):
            v38_blocks.append(
                "19g. 字面词去噪（v3.8）：仅字面含「体验/使用」但无具体感受"
                "（「使用体验分享」「真实体验」），以及官方推广/发布会/代言文案"
                "（「体验官」「发布会圆满举行」）→不标使用体验或任何维度。")
        if _v38_enabled("19h"):
            v38_blocks.append(
                "19h. 页面壳不产生维度情感（v3.8，2026-08-21 拍板）：以下站点框架/"
                "模板内容即使含相关关键词也不标任何维度情感——攻略/教程站"
                "（含'攻略/教程/礼包码/兑换码'）、搜索列表页/聚合页/榜单页"
                "（'搜索 恋与深空'等）、邮箱页脚/自动回复/官方客服模板"
                "（'如有问题请联系客服'）；这些是页面壳而非用户对产品的真实评价。")
        if _v38_enabled("20"):
            v38_blocks.append(
                "20. emoji 维度依据（v3.8）：emoji 不单独构成维度标注依据；与文字冲突时"
                "以文字立场为主（😡😤可强化负面维度；[大笑][笑哭] 不改变文字判断）。")
        if dims:
            dim_lines = "\n".join(
                f"  - {d['id']}（{d['name']}）：关键词 {d['keywords']}；"
                f"说明 {d['description'] or '—'}"
                for d in dims
            )
            dim_block = (
                "维度级情感（2.4 规则，输出 dimension_sentiments 字典）：\n"
                "1. 只对『明确表达正面或负面』且属于下面维度清单的维度标注情感；\n"
                "2. 转折句逐维拆解：「画面好但价格贵」→ art:positive、monetization:negative；\n"
                "3. 只提到没有褒贬的维度不出现；同一维度正负都有且无法定主倾向的不出现；\n"
                "4. 官方内容/宣发/公告/PV/官网介绍一律 dimension_sentiments 为空；\n"
                "5. 键必须来自维度清单，值只允许 positive/negative；没有维度情感输出 {}。\n"
                "6. 竞品对比识别：明确与其他品牌/产品比较（谁更强/对比/差距/平替/"
                "参数对比/别家/更稳/便宜/配件通用/市占率）→ 标「竞品对比」；参数对比文可"
                "同时标「功能效果/使用体验」。\n"
                "7. 品牌倾向识别：明确表达选择/支持某品牌（'更愿意选择影石'"
                "'还是买大疆''支持华为'）→ 标「品牌与营销」（若清单含此维度）。\n"
                "8. 关键词归属冲突路由（v3.7，按语境与当前模块路由，避免多义词误判）：\n"
                "   - 快/慢：物流/配送语境→渠道与售后；出餐/送达语境→响应与时效；"
                "运行/性能语境→性能稳定性；响应语境→响应与时效。\n"
                "   - 贵/便宜：数字产品语境→付费与商业化；实物语境→价格价值；"
                "服务语境→价格与性价比。\n"
                "   - 客服：数字产品语境→运营与客服；实物语境→渠道与售后；"
                "服务语境→售后与投诉处理。\n"
                "   - 态度好/差：只归服务态度，不归专业度。\n"
                "   - 闪退/卡顿：明确'更新后闪退/卡顿'→更新与兼容性；否则→性能稳定性。\n"
                "   - 广告多：数字产品→付费与商业化；实物→品牌与营销；服务不适用。\n"
                "   - 竞品对比：比较言论先按具体关注点归入对应维度"
                "（'续航不如XX'→功能效果/使用体验）；若维度清单含「竞品对比」则同时标注。\n"
                "   以上路由目标以当前维度清单为准；清单中不存在的目标维度按其含义"
                "归入最接近的现有维度。\n"
                + "".join(v38_blocks)
                + ""
                "维度清单：\n"
                f"{dim_lines}\n"
            )
        v34_blocks: list[str] = []
        if _v34_enabled("11"):
            v34_blocks.append(
                "11. 报道/资讯体（v3.4）一律 neutral：新闻/财报/工信部通报/媒体曝光/电商"
                "产品页（'¥499 | 京东自营旗舰店'）/百科知识（'联想笔记本是指…'）/线下"
                "活动打卡（'痛楼/集中打卡时间'）——即使含'点名表扬/遥遥领先/大场面/惊喜'"
                "等词，仍是报道事实而非个人评价。")
        if _v34_enabled("12"):
            v34_blocks.append(
                "12. 行动信号与平淡情绪（v3.4）：明确购买/入手/下单/到店/安排体验"
                "（'已经买了''入手了4''我下单了''给自己安排骑行套装了'）→ positive；"
                "问题未解决/被劝退/吐槽体验（'都试过了,微信打印就正常''搜到差评就买了"
                "别的'）平淡语气也算 negative。")
        if _v34_enabled("13"):
            v34_blocks.append(
                "13. 调侃反讽与方向不明（v3.4）：娱乐性调侃短评（'笑死我了,i7都来了'"
                "'呵呵,天选你就买吧'）无明确产品贬损 → neutral；'天壤之别'类方向不明"
                "比较 → neutral；反讽对象为产品/品牌（'当你不需要一台电脑时,你就很适合"
                "购买华为电脑'）→ negative。")
        if _v34_enabled("14"):
            v34_blocks.append(
                "14. subject 口径强化（v3.4）：锚定品牌受益才判 positive——文本夸赞/"
                "推荐的是非锚定品牌（'还是选大疆的比较好''大疆这边能续上能省一笔'，"
                "锚定=影石时）→ 整条判 negative（若贬本品牌）或 neutral（仅夸竞品），"
                "竞品对比维度按内容标方向；不得因夸竞品判 positive。")
        v35_blocks: list[str] = []
        if _v35_enabled("15"):
            v35_blocks.append(
                "15. 问句二分（v3.5）：求知/信息咨询（'还能吗/会不会/怎么用/支持吗'）"
                "→ neutral；**困扰/后悔/选择困难/不满（'怎么办''买哪个''要不要留''"
                "太纠结了'）→ negative**（情绪性问句按负面口径，如'买成华硕了怎么办?'）。")
        if _v35_enabled("16"):
            v35_blocks.append(
                "16. 报道细分（v3.5）：纯事实陈述/参数罗列/规格盘点/官方发布稿/电商页/"
                "百科/活动打卡 → neutral；**含明确褒贬色彩的报道或事实（'断崖式遥遥领先'"
                "'荣登前两名''估值几千亿''世界级产品'）→ 按褒贬判**，不可一律 neutral。")
        if _v35_enabled("17"):
            v35_blocks.append(
                "17. 功能/能力陈述的评价色彩（v3.5）：明确夸赞产品能力（'机身自带AI剪辑'"
                "'AI自动剪辑很舒服''可玩性更高''100多G内存还没涨价,那选什么一目了然'）"
                "→ positive；纯规格说明（'支持8K''1英寸传感器''10小时续航'）→ neutral。")
        v36_blocks: list[str] = []
        if _v36_enabled("18"):
            v36_blocks.append(
                "18. 实物/3C 隐晦负面（v3.6，2026-08-17 模块卷取证）：以下语境即使无"
                "负面情绪词也判 negative——a) 价格过高/买不起/性价比犹豫后转向竞品"
                "（'买不起的''价格直接干到三千出头,综合性价比我可能还是买XX的二代'）；"
                "b) 功能缺陷/缺失吐槽（'都1寸底了,装个sm卡吧'（缺卡槽）"
                "'模拟门禁卡消失了,重新录入也不行'）；c) 产品不实用/无价值"
                "（'睡觉谁还带手表啊'）；d) 含蓄效果差评（'白天效果很好,晚上早点回家吧'"
                "（暗光差）'属于是买家秀了'）。")
        system = (
            "你是中文社交媒体情感分析专家。对输入的每条文本输出情感判断（prompt v2.2）。"
            "注意：中文网络语境常有反讽/阴阳怪气/反话（表面褒义实为贬义，"
            "例如'真棒啊''像XX一样''厉害了我的XX''呵呵'等），"
            "务必结合语境识别真实情感，不要只看表面褒义词。"
            "总则：没有明确个人立场或情感词的文本一律判 neutral；"
            "仅提到产品/参数/发布/品牌/活动/评测不等于正面或负面，"
            "宁可 neutral 也不要把无观点文本判成 positive/negative。"
            "判例规则："
            "1. 短句信号词（好厉害/太厉害了/笑死/绝了/真的会谢/谢谢您嘞/真棒）：明显夸赞"
            "语境（夸奖偶像/商品/作品且语气正面）判 positive；明显嘲讽/抱怨语境（如"
            "'世界对你太好了'、吐槽对象）判 negative；单独短句无法判断判 neutral。"
            "2. '笑死/笑死我'按语境：娱乐/粉丝/搞笑内容中的'笑死'= 好笑、开心，判 positive；"
            "嘲讽/吐槽语境（被封、被坑、翻车、产品问题、连在一起了）判 negative；"
            "无观点的标题/纯短语判 neutral。"
            "3. 长文本先判断作者对游戏/品牌/产品的整体态度：攻略/教程/科普/解析/排名类"
            "无个人褒贬判 neutral——即使出现'神器/推荐/一键三连/最好'等词仍属介绍性质，"
            "不据此判正面；新闻/报道按实际内容情感判（负面事件→negative、热销/好评→"
            "positive）；同人创作、剧情片段、争议分析中的局部情绪不算对产品的负面；"
            "不要被'氪金/付费/贵/冲突/开盒'等词直接带偏。"
                "4. 官方内容一律判 neutral：官方公告/宣发/PV/活动/官网/公司介绍页/推广文案"
                "（无论是否含'让顾客享受/一站购齐'等宣传词）均判 neutral；只有明确第三方"
                "用户的真实评价/吐槽才按实际情感判断。"
            "5. emoji 不单独构成情感依据，需结合文字语境：愤怒类（😡😤🤮👊）与嘲讽类"
            "（🤣👉🤡）可强化 negative；纯 emoji/无观点短文本判 neutral。"
                "6. 高频词表：退坑=不再玩、翻车=出事、割韭菜=圈钱、无语/好无语=无奈、"
                "低级错误=批评、孝钱=讽刺性消费（以上多带负面）；yyds/顶/好正/好鬼正/咁正/"
                "冇得顶/巴适/安逸/要得=非常好/满意（正面）；方言词若只出现在店名/标题且无观点"
                "判 neutral。"
                "7. 3C 数码语体补充规则（数码产品/手机/相机/电脑/平板等），"
                "以下情况一律判 neutral，不得判 positive/negative："
                "a) 产品介绍/开箱/参数罗列/规格对比/评测开头"
                "（例：'下面我先来简单对影石 Ace Pro 2 做个开箱…是数显屏'"
                "'大疆 osmo 360 II 发布，售价 3299，ai 处理芯片…画质升级不少'）；"
                "b) 疑问句/求助/'值得买吗/怎么样/怎么办' 无明确倾向"
                "（例：'买成华硕了怎么办?'）；"
                "c) 标题页/聚合页/问题页（例：'联想小新Air15 评价怎样-推荐星'"
                "'如何评价影石 X3-知乎'）；"
                "d) 官方贴吧/高管言论/公司新闻稿/品牌活动宣发"
                "（例：'联想官方贴吧活动汇总''杨元庆回应'）；"
                "仅当存在明确个人评价词（喜欢/吐槽/推荐/不推荐/太好/太差）才按实际情感判；"
                "e) 明确与其他品牌/产品比较（谁更强/对比/差距/平替/参数对比/别家）"
                "不改变整条情感判定，但维度情感需标「竞品对比」。"
                "8. 含蓄表达三分（v3.3，先判『有没有立场』再判方向）："
                "a) 明确夸赞（'影石 Luna 系列很猛''Pocket 4P 到底谁更牛'的安利语气）→ positive；"
                "b) 明确不满/吐槽（'除非影石更新，不然大疆明年不会出5p''这价格还要啥自行车'"
                "抱怨语境）→ negative；"
                "c) 无立场/权衡中（'犹豫好久/去线下试了再说''两家棋逢对手，都搭载…'"
                "'我舍不得买'）→ neutral。"
                "9. 无立场语体补强（v3.3）：标题型问句/求助（'还能吗/怎么选/值吗/怎么样/"
                "怎么办'）、系列/型号介绍（'大疆无人机分为御/Mavic 系列…'）、购买决策中的"
                "犹豫权衡（'犹豫/纠结/去线下试试/对比对比'）→ neutral（除非明确站队某产品）。"
                "10. 多品牌比较且无锚定品牌时：整条按对比文判 neutral（不站队），"
                "竞品对比维度按文本内容标方向（夸某品牌=竞品对比 positive、贬=negative）。"
            + "".join(v34_blocks)
            + "".join(v35_blocks)
            + "".join(v36_blocks)
            + dim_block
            + (
                (
                    f"锚定品牌口径（subject 已提供「{subject}」，品牌口碑场景适用）："
                    "整条情感跟锚定品牌判——夸本品牌=positive；贬本品牌=negative；"
                    "夸竞品贬本品牌=negative；夸本品牌贬竞品=positive；"
                    "夸赞/推荐的是非锚定品牌 → 对本品牌 negative 或 neutral（见规则 14）；"
                    "文本未涉及本品牌或无明确立场 → neutral。"
                    if _v34_enabled("14")
                    else
                    f"锚定品牌口径（subject 已提供「{subject}」，品牌口碑场景适用）："
                    "整条情感跟锚定品牌判——夸本品牌=positive；贬本品牌=negative；"
                    "夸竞品贬本品牌=negative；夸本品牌贬竞品=positive；"
                    "文本未涉及本品牌或无明确立场 → neutral。"
                )
                if subject else ""
            )
            + ('输出 JSON：{"items":[{"index":0,"sentiment":"positive|negative|neutral",'
               '"score":-1到1的浮点数,"confidence":0到1的浮点数,"keywords":[最多3个情感关键词],'
               '"dimension_sentiments":{"维度id":"positive|negative"}}]}。'
               "items 长度必须与输入条数一致，index 从 0 开始。只输出 JSON，不要其他文字。"
               if dims else
               '输出 JSON：{"items":[{"index":0,"sentiment":"positive|negative|neutral",'
               '"score":-1到1的浮点数,"confidence":0到1的浮点数,"keywords":[最多3个情感关键词]}]}。'
               "items 长度必须与输入条数一致，index 从 0 开始。只输出 JSON，不要其他文字。")
        )
        valid_dim_ids = {d["id"] for d in dims}
        return self._structured_batch(
            texts,
            system=system,
            user_fn=lambda chunk: json.dumps(
                {"subject": subject, "texts": chunk} if subject
                else {"texts": chunk},
                ensure_ascii=False,
            ),
            max_tokens_fn=lambda n: min(2600, 220 + 140 * n) if dims else min(2000, 150 + 100 * n),
            parse_fn=self._parse_batch,
            sanitize_fn=(lambda items: sanitize_dimension_sentiments(items, valid_dim_ids))
            if dims else (lambda items: items),
            fallback_fn=self._lexicon_fallback,
            error_label="LLM 批量请求失败",
            error_suffix="该批次已用词典结果兜底",
            complete_missing=True,
        )

    def _request_narrative_batch(self, texts: list[str]) -> list[dict]:
        frames = "conflict|human_interest|attribution|economic|morality"
        actors = "government|enterprise|individual|system|technology|society|nature|unclear"
        system = (
            "你是新闻框架分析专家。对输入的每条文本判断叙事框架和归因主体。"
            f'输出 JSON：{{"items":[{{"index":0,"narrative":"{frames}",'
            f'"attribution":"{actors}"}}]}}。'
            "注意：narrative 只能取上述 5 个值之一，attribution 只能取上述 8 个值之一；"
            '"unclear" 只允许出现在 attribution 中，不允许出现在 narrative 中。'
            "items 长度必须与输入条数一致，index 从 0 开始。只输出 JSON，不要其他文字。"
        )
        return self._structured_batch(
            texts,
            system=system,
            user_fn=lambda chunk: json.dumps({"texts": chunk}, ensure_ascii=False),
            max_tokens_fn=lambda n: min(2500, 150 + 200 * n),
            parse_fn=self._parse_batch,
            sanitize_fn=self._sanitize_narrative_items,
            fallback_fn=lambda chunk: [
                {"narrative": None, "attribution": None} for _ in chunk
            ],
            error_label="叙事/归因批量请求失败",
            error_suffix="已自动跳过叙事/归因层，不影响情感编码结果",
            complete_missing=True,
        )

    def _sanitize_narrative_items(self, items: list[dict]) -> list[dict]:
        """校验并清洗叙事/归因取值：模型偶尔会把 unclear 等非法值填错字段。"""
        cleaned: list[dict] = []
        bad_narr: set = set()
        bad_actor: set = set()
        for it in items:
            narr = it.get("narrative")
            attr = it.get("attribution")
            if narr not in VALID_NARRATIVES:
                if narr:
                    bad_narr.add(str(narr))
                narr = None
            if attr not in VALID_ACTORS:
                if attr:
                    bad_actor.add(str(attr))
                attr = None
            cleaned.append({"narrative": narr, "attribution": attr})
        if bad_narr:
            self._errors.append(
                f"模型返回了非法叙事框架取值 {sorted(bad_narr)[:3]}，已自动忽略"
            )
        if bad_actor:
            self._errors.append(
                f"模型返回了非法归因主体取值 {sorted(bad_actor)[:3]}，已自动忽略"
            )
        return cleaned

    def check_relevance(self, subject: str, texts: list[str]) -> list[bool]:
        """LLM 相关性复核（可选步骤，按量计费）：判断文本是否与品牌/主题相关。"""
        if not texts:
            return []
        texts = [desensitize_text(t) for t in texts]  # P1-3：唯一漏点，补脱敏
        system = (
            "你是社交媒体内容审核助手。判断每条文本是否与给定品牌/主题相关："
            "只要涉及对该主题的讨论、评价、体验、对比、求助、吐槽等均算相关；"
            "页面导航文案、广告、纯系统提示、与本主题无关的闲聊不算相关。"
            '输出 JSON：{"items":[{"index":0,"relevant":true}]}。'
            "items 长度必须与输入条数一致，index 从 0 开始。只输出 JSON，不要其他文字。"
        )
        results: list[bool] = []
        for i in range(0, len(texts), self.batch_size):
            chunk = texts[i : i + self.batch_size]
            results.extend(
                self._structured_batch(
                    chunk,
                    system=system,
                    user_fn=lambda c: json.dumps(
                        {"subject": subject, "texts": c}, ensure_ascii=False
                    ),
                    max_tokens_fn=lambda n: min(2000, 150 + 60 * n),
                    parse_fn=self._parse_batch,
                    sanitize_fn=lambda its: [
                        bool(it.get("relevant")) for it in its
                    ],
                    fallback_fn=lambda c: [True] * len(c),
                    error_label="LLM 相关性复核失败",
                    error_suffix="本次跳过复核",
                )
            )
        return results

    def analyze_batch(
        self,
        texts: list[str],
        on_batch_progress: "Callable[[int, int], None] | None" = None,
        dimension_schema: dict | None = None,
        subject: str | None = None,
    ) -> list[dict]:
        """真批量请求：BATCH_SIZE 条文本一次请求，最多 max_workers 批并发。"""
        self._errors = []
        texts = [desensitize_text(t) for t in texts]  # P1-3：LLM 前脱敏（幂等）
        results: list[dict] = [None] * len(texts)  # type: ignore[list-item]

        def _send(batch: list[str]) -> list[dict]:
            todo_idx: list[int] = []
            todo_texts: list[str] = []
            out: list[dict] = [None] * len(batch)  # type: ignore[list-item]
            for j, t in enumerate(batch):
                with self._lock:
                    hit = self._cache.get(f"{subject or ''}|{t}")
                if hit is not None:
                    out[j] = hit
                else:
                    todo_idx.append(j)
                    todo_texts.append(t)
            if todo_texts:
                parsed = self._request_batch(todo_texts, dimension_schema, subject)
                for j, t, item in zip(todo_idx, todo_texts, parsed):
                    with self._lock:
                        self._cache[f"{subject or ''}|{t}"] = item
                    out[j] = item
            return out

        batches = [texts[i : i + self.batch_size] for i in range(0, len(texts), self.batch_size)]
        done_items = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {bi: pool.submit(_send, batch) for bi, batch in enumerate(batches)}
            # 按完成顺序回调，避免首批请求慢时进度长时间不动
            future_to_bi = {fut: bi for bi, fut in futures.items()}
            for fut in as_completed(future_to_bi):
                bi = future_to_bi[fut]
                results[bi * self.batch_size : (bi + 1) * self.batch_size] = fut.result()
                done_items = min(done_items + self.batch_size, len(texts))
                if on_batch_progress:
                    on_batch_progress(done_items, len(texts))
        return results

    def analyze_narrative(
        self,
        texts: list[str],
        on_batch_progress: "Callable[[int, int], None] | None" = None,
    ) -> list[dict]:
        """叙事/归因：并发批量调用（默认每条文本一次请求改为每批一次）。"""
        self._errors = []
        texts = [desensitize_text(t) for t in texts]  # P1-3：LLM 前脱敏（幂等）
        results: list[dict] = [None] * len(texts)  # type: ignore[list-item]

        def _send(batch: list[str]) -> list[dict]:
            todo_idx: list[int] = []
            todo_texts: list[str] = []
            out: list[dict] = [None] * len(batch)  # type: ignore[list-item]
            for j, t in enumerate(batch):
                with self._lock:
                    hit = self._cache.get(f"narr:{t}")
                if hit is not None:
                    out[j] = hit
                else:
                    todo_idx.append(j)
                    todo_texts.append(t)
            if todo_texts:
                parsed = self._request_narrative_batch(todo_texts)
                for j, t, item in zip(todo_idx, todo_texts, parsed):
                    with self._lock:
                        self._cache[f"narr:{t}"] = item
                    out[j] = item
            return out

        batches = [
            texts[i : i + NARRATIVE_BATCH_SIZE]
            for i in range(0, len(texts), NARRATIVE_BATCH_SIZE)
        ]
        done_items = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {bi: pool.submit(_send, batch) for bi, batch in enumerate(batches)}
            future_to_bi = {fut: bi for bi, fut in futures.items()}
            for fut in as_completed(future_to_bi):
                bi = future_to_bi[fut]
                results[
                    bi * NARRATIVE_BATCH_SIZE : (bi + 1) * NARRATIVE_BATCH_SIZE
                ] = fut.result()
                done_items = min(done_items + NARRATIVE_BATCH_SIZE, len(texts))
                if on_batch_progress:
                    on_batch_progress(done_items, len(texts))
        return results

    def generate_insights(
        self, descriptors: dict, evidence: list[dict] | None = None,
        summary: dict | None = None,
    ) -> dict:
        """图表解析 + 发现驱动结论（P1.5）：LLM 只解读给定证据，不自行选材。"""
        from app.coding.insights import template_insights
        from app.core.evidence import findings_to_conclusion

        evidence = evidence or []
        evidence_block = ""
        if evidence:
            lines = []
            for i, c in enumerate(evidence):
                parts = [
                    c["id"],
                    f"维度:{c.get('dimension_name') or '整体'}",
                    f"平台:{c.get('platform') or '未知'}",
                    f"日期:{c.get('date') or '未知'}",
                    f"情感:{c['sentiment']}",
                    f"n={c.get('n') or 0}",
                    f"判定:{c['judge']}",
                ]
                if c.get("need_review"):
                    parts.append("待复核")
                meta = f"- {c['id']}（{'，'.join(parts[1:])}）"
                # 统计卡全文始终给出（短、且统计结论必须引用）；原文卡只给前 20 条
                if c.get("kind") == "stat" or i < 20:
                    meta += f"：「{c['text']}」"
                lines.append(meta)
            evidence_block = "\n".join(lines)

        try:
            system = (
                "你是资深的社交媒体舆情分析师。基于给定的统计描述与证据清单，完成三件事：\n"
                "1) 为每个图表写一段 80~150 字的中文解析，解读数字含义、趋势变化和潜在风险；\n"
                "2) 写「核心发现」，最多 5 条。每条包含：\n"
                "   - claim：一句话结论（必须来自统计描述或证据清单，不得虚构原文/数字）；\n"
                "   - evidence_refs：引用证据清单中存在的编号（如 E1）；无合适证据则留空数组；\n"
                "   - action：一条可执行建议，必须包含动作 + 对象 + 具体渠道（如微博/B站/"
                "知乎/小红书/京东等，至少一个），建议末尾注明对应发现编号（如『（对应F1）』）；"
                "禁止空泛公关话术（如『发布官方声明』『加强品牌建设』而无渠道、无对象）。\n"
                "     正例：『由客服部联合法务部在微博、京东等渠道发布售后政策改进公告，"
                "明确投诉处理流程，并设置专人跟进（对应F2）。』\n"
                "证据引用规则（重要）：\n"
                "   - 证据分两类：原文卡（kind=text，含原文引用）与统计卡（kind=stat，"
                "如『维度「品牌形象」：讨论 28 条，负面 22 条，负面率 79%』『情感走势整体下降…』）；\n"
                "   - 统计/聚合类结论（占比、走势、强度、平台平均分、维度负面率）必须引用"
                "统计卡，禁止用单条原文支撑统计数字；\n"
                "   - 具体文本现象类结论可引用原文卡；\n"
                "   - 引用的证据必须直接支持结论；宁可留空 evidence_refs，"
                "也不要引用不相关或仅沾边的原文。\n"
                "   - narrative_label（可选）：归因视角标签，如 attribution: enterprise / "
                "conflict / human_interest / economic / morality。\n"
                "3) 写「结构化总结文案」（置顶执行摘要，供速览，不与核心发现重复）：\n"
                "   - overall：一句话总结整体倾向（正面/中性/负面占比方向与主要结论），80 字内；\n"
                "   - top_issues：按统计描述中的维度负面率/主题洞察，给出最多 3 个重点问题的"
                "cause（可能原因推测，用「可能/或与…相关」）与 direction（可执行建议，"
                "含渠道/对象）；\n"
                "   - improvements：2~4 条改进建议。\n"
                "   ⚠️ 只写解读文案：不要写任何数字（占比/条数/短语/n=），"
                "系统会从统计自动填充并校验覆盖。\n"
                '输出 JSON：{"chart_insights":{"overall":"...","platform":"...","trend":"...",'
                '"dimensions":"...","heatmap":"...","words":"...","intensity":"...",'
                '"radar":"...","platform_dim":"...","date_dim":"...","wordcloud":"...",'
                '"cooccurrence":"..."},"findings":[{"id":"F1","claim":"...","evidence_refs":'
                '["E1"],"action":"...","narrative_label":"..."}],'
                '"structured_summary":{"overall":"...","top_issues":[{"cause":"...",'
                '"direction":"..."}],"improvements":["..."]}}。'
                "只输出 JSON，不要其他文字。"
            )
            user = json.dumps(
                {"统计描述": descriptors, "证据清单": evidence_block},
                ensure_ascii=False,
            )
            # F-010/F-015（2026-08-26）：max_tokens 4000→4800（新增结构化总结文案 400~600 token）
            content = self._chat(system, user, max_tokens=4800, timeout=120)
            data = json.loads(content)
            chart_insights = data.get("chart_insights", {})
            findings = data.get("findings", [])
            if not isinstance(chart_insights, dict):
                raise ValueError("深度洞察 JSON 解析失败")
            if not isinstance(findings, list):
                findings = []
            conclusion = findings_to_conclusion(findings) if findings else ""
            from app.coding.structured_contract import (
                parse_llm_structured_summary,
                validate_llm_structured_summary,
            )
            ss_text = validate_llm_structured_summary(
                parse_llm_structured_summary(content)
            ) if isinstance(content, str) else None
            return {
                "chart_insights": chart_insights,
                "conclusion": conclusion,
                "findings": findings,
                "structured_summary_text": ss_text,
            }
        except Exception as exc:
            self._errors.append(
                f"深度洞察生成失败：{self._friendly_error(exc)}，已用规则兜底"
            )
            if summary is not None:
                from app.core.evidence import build_findings, findings_to_conclusion

                findings = build_findings(evidence, summary, mode="fallback")
                return {
                    "chart_insights": template_insights(descriptors)["chart_insights"],
                    "conclusion": findings_to_conclusion(findings),
                    "findings": findings,
                }
            return template_insights(descriptors)


def create_analyzer(
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    allow_env: bool = False,
) -> BaseAnalyzer:
    """按配置创建分析器；无 Key 时返回 Mock（词典）分析器。

    allow_env=False（默认）：应用链路不读环境变量，Key 由调用方显式传入
    （worker 只从 DPAPI 读取，见 1.5）；开发/评测脚本可显式传 allow_env=True。
    """
    key = api_key or (os.environ.get("OPENAI_API_KEY", "") if allow_env else "")
    if not key:
        return MockAnalyzer()
    base = base_url or (
        os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL) if allow_env else DEFAULT_BASE_URL
    )
    model_name = model or (
        os.environ.get("OPENAI_MODEL", DEFAULT_MODEL) if allow_env else DEFAULT_MODEL
    )
    return OpenAICompatibleAnalyzer(
        LLMConfig(
            api_key=key,
            base_url=base,
            model=model_name,
        )
    )
