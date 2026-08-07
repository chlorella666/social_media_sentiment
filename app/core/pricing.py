"""LLM 费用估算（DeepSeek deepseek-chat）。

⚠️ 价格会随时变动，以下为示例单价，实际以 DeepSeek 官方定价页为准；
本文件常量可手动更新。预估只是粗算，实际以接口返回的 usage 为准。
"""

from __future__ import annotations

# 单价：元 / 百万 token（示例价，需按官方调整）
INPUT_YUAN_PER_M = 2.0
OUTPUT_YUAN_PER_M = 8.0

# 预估假设（与 coder/llm_analyzer 的实际请求结构对应）
ROUTED_RATIO = 0.7  # 阈值 0.8 下约 70% 文本送 LLM（基准实测）
AVG_TEXT_CHARS = 60  # 平均单条文本长度（字符）
CHARS_PER_TOKEN = 0.7  # 中文约 0.7 token/字符（含 prompt 结构开销）
OUTPUT_TOKENS_PER_TEXT = 50  # 精分析每条约 50 token 输出（JSON items）
NARRATIVE_PROMPT_OVERHEAD = 40  # 叙事请求的系统提示等开销（字符）
NARRATIVE_OUTPUT_PER_TEXT = 40
RELEVANCE_PROMPT_OVERHEAD = 30
RELEVANCE_OUTPUT_PER_TEXT = 10


def estimate_cost(
    text_count: int,
    narrative_enabled: bool = False,
    relevance_enabled: bool = False,
) -> dict:
    """按文本量粗估 token 与费用。返回含假设说明的字典。"""
    routed = int(text_count * ROUTED_RATIO)
    prompt_chars = routed * (AVG_TEXT_CHARS + 40)
    completion_tokens = routed * OUTPUT_TOKENS_PER_TEXT
    if narrative_enabled:
        prompt_chars += routed * (AVG_TEXT_CHARS + NARRATIVE_PROMPT_OVERHEAD)
        completion_tokens += routed * NARRATIVE_OUTPUT_PER_TEXT
    if relevance_enabled:
        prompt_chars += text_count * (AVG_TEXT_CHARS + RELEVANCE_PROMPT_OVERHEAD)
        completion_tokens += text_count * RELEVANCE_OUTPUT_PER_TEXT
    prompt_tokens = int(prompt_chars * CHARS_PER_TOKEN)
    cost = (
        prompt_tokens / 1_000_000 * INPUT_YUAN_PER_M
        + completion_tokens / 1_000_000 * OUTPUT_YUAN_PER_M
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost": round(cost, 3),
        "text_count": text_count,
        "routed_texts": routed,
        "assumptions": (
            f"约 {ROUTED_RATIO:.0%} 文本送 LLM（阈值0.8）；平均 {AVG_TEXT_CHARS} 字/条；"
            f"中文约 {CHARS_PER_TOKEN} token/字；单价 输入 ¥{INPUT_YUAN_PER_M}/百万、"
            f"输出 ¥{OUTPUT_YUAN_PER_M}/百万（示例价，以 DeepSeek 官方为准，可能变动）"
        ),
    }


def cost_from_usage(prompt_tokens: int, completion_tokens: int) -> float:
    """按接口实际 usage 计算费用（元）。"""
    return round(
        prompt_tokens / 1_000_000 * INPUT_YUAN_PER_M
        + completion_tokens / 1_000_000 * OUTPUT_YUAN_PER_M,
        4,
    )
