"""LLM 费用估算（DeepSeek deepseek-chat）。

⚠️ 价格会随时变动，以下单价为 2026-08-28 核实的 DeepSeek 官方 deepseek-chat 价
（输入 ¥2/百万、输出 ¥8/百万；2026-08-17 峰谷调价仅针对 V4 系列，deepseek-chat
保持不变）；实际以 DeepSeek 官方定价页为准，本文件常量可手动更新。预估只是粗算，
实际以接口返回的 usage 为准。
"""

from __future__ import annotations

import math

# 单价：元 / 百万 token（DeepSeek deepseek-chat，2026-08-28 核实官方价）
INPUT_YUAN_PER_M = 2.0
OUTPUT_YUAN_PER_M = 8.0

# 预估假设（与 coder/llm_analyzer/coding_workflow 的实际请求结构对应）
ROUTED_RATIO = 0.7  # 阈值 0.8 下约 70% 文本送 LLM 逐条精分析（基准实测）
AVG_TEXT_CHARS = 60  # 平均单条文本长度（字符）
CHARS_PER_TOKEN = 0.7  # 中文约 0.7 token/字符（含 prompt 结构开销）
OUTPUT_TOKENS_PER_TEXT = 50  # 精分析每条约 50 token 输出（JSON items）
NARRATIVE_PROMPT_OVERHEAD = 40  # 叙事请求的系统提示等开销（字符）
NARRATIVE_OUTPUT_PER_TEXT = 40
RELEVANCE_PROMPT_OVERHEAD = 30
RELEVANCE_OUTPUT_PER_TEXT = 10

# R-005（2026-08-28）：F-021 编码工作流 + F-027 结论生成 token 建模。
# 实测参考：277 条演示数据 ≈ 16.7 万 prompt tokens（约 ¥0.54），旧估算低估约 10 倍。
CODING_BATCH_SIZE = 200  # 与 coding_workflow.BATCH_SIZE 一致
CODING_CHARS_PER_ITEM = 750  # 编码工作流全量文本 + JSON 包装（约 525 token/条）
CODING_SYSTEM_CHARS = 600
CODING_OUTPUT_PER_BATCH = 3000  # 与 run_coding_workflow max_tokens 一致
INSIGHT_PROMPT_CHARS = 12000  # F-027 结论生成：descriptors 全维度 + 证据清单
INSIGHT_OUTPUT_TOKENS = 4000  # 与 generate_insights max_tokens 一致


def estimate_cost(
    text_count: int,
    narrative_enabled: bool = False,
    relevance_enabled: bool = False,
    llm_enabled: bool = True,
) -> dict:
    """按文本量粗估 token 与费用。返回含假设说明的字典。"""
    if not llm_enabled:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "estimated_cost": 0.0,
            "text_count": text_count,
            "routed_texts": 0,
            "assumptions": "词典模式不调用 LLM，费用为 0；开启 LLM 精分析后才会产生费用。",
        }
    routed = int(text_count * ROUTED_RATIO)
    # 逐条精分析
    prompt_chars = routed * (AVG_TEXT_CHARS + 40)
    completion_tokens = routed * OUTPUT_TOKENS_PER_TEXT
    # F-021（2026-08-26）：编码工作流——coded_items 文本全量分批做主题编码
    coding_batches = max(1, math.ceil(text_count / CODING_BATCH_SIZE))
    prompt_chars += CODING_SYSTEM_CHARS + text_count * CODING_CHARS_PER_ITEM
    completion_tokens += coding_batches * CODING_OUTPUT_PER_BATCH
    # F-027（2026-08-26）：结论生成（descriptors 全维度 + 证据清单，LLM 模式固定一次）
    prompt_chars += INSIGHT_PROMPT_CHARS
    completion_tokens += INSIGHT_OUTPUT_TOKENS
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
            f"约 {ROUTED_RATIO:.0%} 文本送 LLM 逐条精分析；平均 {AVG_TEXT_CHARS} 字/条；"
            f"F-021 编码工作流按全量文本分批（每批 {CODING_BATCH_SIZE} 条，约 "
            f"{CODING_CHARS_PER_ITEM} 字符/条）；F-027 结论生成按全维度统计描述 + "
            f"证据清单估算；中文约 {CHARS_PER_TOKEN} token/字；单价 输入 "
            f"¥{INPUT_YUAN_PER_M}/百万、输出 ¥{OUTPUT_YUAN_PER_M}/百万"
            f"（DeepSeek deepseek-chat 官方价，2026-08-28 核实；2026-08-17 峰谷调价"
            f"仅针对 V4 系列，deepseek-chat 不变）"
        ),
    }


def cost_from_usage(prompt_tokens: int, completion_tokens: int) -> float:
    """按接口实际 usage 计算费用（元）。"""
    return round(
        prompt_tokens / 1_000_000 * INPUT_YUAN_PER_M
        + completion_tokens / 1_000_000 * OUTPUT_YUAN_PER_M,
        4,
    )
