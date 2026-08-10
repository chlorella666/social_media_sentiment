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

from app.coding import lexicon

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
CONFIDENCE_THRESHOLD = 0.8  # 预筛置信度低于此值才调用 LLM（0.8 更保守，送 LLM 占比更高）
BATCH_SIZE = 10  # 单请求文本数（越小单请求越快、进度越平滑）
NARRATIVE_BATCH_SIZE = 10

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
    ) -> list[dict]:
        """输入文本列表，返回 [{sentiment, score, confidence, keywords}]。"""
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

    def generate_insights(self, descriptors: dict) -> dict:
        """基于统计描述生成图表解析与深度结论。"""
        raise NotImplementedError


class MockAnalyzer(BaseAnalyzer):
    """无 API Key 时的降级实现：词典规则。"""

    def analyze_batch(
        self,
        texts: list[str],
        on_batch_progress: "Callable[[int, int], None] | None" = None,
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

    def generate_insights(self, descriptors: dict) -> dict:
        from app.coding.insights import template_insights

        return template_insights(descriptors)


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
            result = [by_index.get(i) for i in range(n)]
            if any(r is None for r in result):
                raise ValueError("模型返回条目不完整")
            return result  # type: ignore[return-value]
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
    ) -> list[dict]:
        """单批结构化 LLM 请求的统一处理：截断检测 + 拆小批重试 + 兜底。

        - finish_reason=="length"（输出被 max_tokens 截断）→ 抛"条目不完整"；
        - 截断且批次 > min_split → 对半拆分递归重试，分批后更易完整返回；
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
                )
            self._errors.append(
                f"{error_label}（{len(texts)} 条）：{self._friendly_error(exc)}，{error_suffix}"
            )
            return fallback_fn(texts)

    def _request_batch(self, texts: list[str]) -> list[dict]:
        system = (
            "你是中文社交媒体情感分析专家。对输入的每条文本输出情感判断。"
            "注意：中文网络语境常有反讽/阴阳怪气/反话（表面褒义实为贬义，"
            "例如'真棒啊''像XX一样''厉害了我的XX''呵呵'等），"
            "务必结合语境识别真实情感，不要只看表面褒义词。"
            "注意：官方公告/宣发/新品发布/更新公告/纯转发类内容，如无明确的"
            "个人褒贬观点，一律判为 neutral；只有公告本身带有明确争议或用户"
            "评价时才按实际情感判断。"
            '输出 JSON：{"items":[{"index":0,"sentiment":"positive|negative|neutral",'
            '"score":-1到1的浮点数,"confidence":0到1的浮点数,"keywords":[最多3个情感关键词]}]}。'
            "items 长度必须与输入条数一致，index 从 0 开始。只输出 JSON，不要其他文字。"
        )
        return self._structured_batch(
            texts,
            system=system,
            user_fn=lambda chunk: json.dumps({"texts": chunk}, ensure_ascii=False),
            max_tokens_fn=lambda n: min(2000, 150 + 100 * n),
            parse_fn=self._parse_batch,
            sanitize_fn=lambda items: items,
            fallback_fn=self._lexicon_fallback,
            error_label="LLM 批量请求失败",
            error_suffix="该批次已用词典结果兜底",
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
    ) -> list[dict]:
        """真批量请求：BATCH_SIZE 条文本一次请求，最多 max_workers 批并发。"""
        self._errors = []
        results: list[dict] = [None] * len(texts)  # type: ignore[list-item]

        def _send(batch: list[str]) -> list[dict]:
            todo_idx: list[int] = []
            todo_texts: list[str] = []
            out: list[dict] = [None] * len(batch)  # type: ignore[list-item]
            for j, t in enumerate(batch):
                with self._lock:
                    hit = self._cache.get(t)
                if hit is not None:
                    out[j] = hit
                else:
                    todo_idx.append(j)
                    todo_texts.append(t)
            if todo_texts:
                parsed = self._request_batch(todo_texts)
                for j, t, item in zip(todo_idx, todo_texts, parsed):
                    with self._lock:
                        self._cache[t] = item
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

    def generate_insights(self, descriptors: dict) -> dict:
        """综合所有图表信息，生成解析文字与按叙事框架的深度结论。"""
        from app.coding.insights import template_insights

        try:
            system = (
                "你是资深的社交媒体舆情分析师。基于给定的统计描述，完成两件事：\n"
                "1) 为每个图表写一段 80~150 字的中文解析，解读数字含义、趋势变化和潜在风险；\n"
                "2) 综合所有图表信息，按五种叙事框架（冲突/人情味/责任归因/经济后果/道德）"
                "输出 300~500 字的深度结论与行动建议，每条建议要具体、可执行。\n"
                '输出 JSON：{"chart_insights":{"overall":"...","platform":"...","trend":"...",'
                '"dimensions":"...","heatmap":"...","words":"...","intensity":"...",'
                '"radar":"...","platform_dim":"...","date_dim":"...","wordcloud":"...",'
                '"cooccurrence":"..."},"conclusion":"..."}。'
                "只输出 JSON，不要其他文字。"
            )
            user = json.dumps({"统计描述": descriptors}, ensure_ascii=False)
            content = self._chat(system, user, max_tokens=3500, timeout=120)
            data = json.loads(content)
            chart_insights = data.get("chart_insights", {})
            conclusion = data.get("conclusion", "")
            if not isinstance(chart_insights, dict) or not conclusion:
                raise ValueError("深度洞察 JSON 解析失败")
            return {"chart_insights": chart_insights, "conclusion": conclusion}
        except Exception as exc:
            self._errors.append(
                f"深度洞察生成失败：{self._friendly_error(exc)}，已用模板兜底"
            )
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
