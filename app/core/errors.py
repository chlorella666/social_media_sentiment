"""错误分类与排查建议（1.3）：规则映射、离线可控，与现有降级提示口径一致。

分层设计（1.4 渠道安全可直接复用判定层）：
- classify_error(text) -> category：错误文本 → 类别；
- suggest_fixes(task) -> list[str]：任务 → 建议（按确定性排序、去重、兜底）。
建议措辞统一使用"可能原因 / 建议尝试"，不做绝对化承诺，避免误导小白用户。
"""

from __future__ import annotations

import re
from typing import Any

STEP_CN = {
    "collect": "采集数据",
    "clean": "清洗与去重",
    "lexicon": "词典预筛",
    "llm": "LLM 精分析",
    "narrative": "叙事/归因分析",
    "report": "生成报告",
    "worker": "后台进程中断",
    "unknown": "未知步骤",
}

# 类别即建议展示顺序（高确定性在前）
CATEGORY_ORDER = [
    "cookie", "llm", "ratelimit", "no_data", "channel_unavailable",
    "worker", "output", "degraded",
]

SUGGESTIONS = {
    "cookie": (
        "可能原因：微博登录态（Cookie）缺失或已过期。"
        "建议尝试：在渠道设置中更新微博 Cookie 后重试。"
    ),
    "llm": (
        "可能原因：LLM 服务连接失败（API Key / Base URL / 模型名 / 网络 / 余额）。"
        "建议尝试：在侧边栏用「测试连接」验证；也可以关闭 LLM 精分析改用词典模式。"
    ),
    "ratelimit": (
        "可能原因：平台风控或频率限制（微博/小红书采集过快）。"
        "建议尝试：把该渠道每关键词上限调低（微博 ≤20、小红书 ≤10），"
        "两次分析之间留出间隔（建议 ≥10 分钟）。"
    ),
    "no_data": (
        "可能原因：没有采集到足够有效数据。"
        "建议尝试：扩大关键词、渠道或时间段后重试；也可先用演示数据验证流程。"
    ),
    "worker": (
        "可能原因：后台执行进程未运行或异常中断。"
        "建议尝试：通过 run.bat 启动后台执行进程；中断的任务可一键重跑。"
    ),
    "output": (
        "可能原因：结果文件写入失败。"
        "建议尝试：检查磁盘空间与 data/reports 目录写入权限后重试；"
        "侧边栏「数据管理」可查看占用并归档/清理旧报告。"
    ),
    "degraded": (
        "部分渠道降级：其余渠道的结果仍可用。"
        "建议尝试：降低失败渠道的采集上限后重跑，或更新该渠道的登录状态。"
    ),
    "channel_unavailable": (
        "可能原因：所选渠道处于暂停、风控冷却或配额用尽状态。"
        "建议尝试：在「渠道/时间」的渠道安全设置中恢复渠道、解除冷却或调整每日上限后重试。"
    ),
}

FALLBACK_SUGGESTION = (
    "暂时无法自动判断原因。建议尝试：查看上方执行日志定位报错，"
    "或一键重跑（保留原计划）。"
)

# 判定规则（按优先级检查，首条命中即返回）
_RULES: list[tuple[str, re.Pattern]] = [
    ("cookie", re.compile(r"Cookie|登录态|需要更新.*Cookie", re.I)),
    ("llm", re.compile(r"LLM|OpenAI|DeepSeek|API ?Key|api.?key|余额不足|模型不存在|401|连接失败|超时", re.I)),
    ("channel_unavailable", re.compile(r"渠道均不可用|配额已用尽|渠道已暂停|冷却中", re.I)),
    ("ratelimit", re.compile(r"风控|频率限制|验证码|频繁|rate.?limit|429|限制", re.I)),
    ("no_data", re.compile(r"未采集到任何有效数据|样本量不足|样本量较少|样本量偏少|样本量一般|无数据", re.I)),
    ("worker", re.compile(r"后台执行进程未运行|排队等待|任务中断|worker", re.I)),
    ("output", re.compile(r"输出落盘失败|磁盘|写入失败|权限", re.I)),
]

_CHANNEL_PREFIX = re.compile(r"^\s*\[")

# failed_step → 兜底类别（error/warnings 无命中时使用）
_STEP_CATEGORY = {
    "collect": "degraded",
    "llm": "llm",
    "narrative": "llm",
    "report": "output",
    "clean": "no_data",
    "worker": "worker",
}


def step_label(step: str | None) -> str:
    """步骤 id → 中文名（UI 错误视图共用）。"""
    return STEP_CN.get(step or "", step or "未知步骤")


def classify_error(text: str) -> str:
    """错误文本 → 类别（unknown 表示无法判定）。"""
    if not text:
        return "unknown"
    # 渠道前缀错误（如 "[B站] 采集异常…"）只匹配渠道类规则，
    # 避免"网络/超时"等通用词被 LLM 类规则误判
    is_channel = bool(_CHANNEL_PREFIX.match(text))
    for cat, rx in _RULES:
        if is_channel and cat == "llm":
            continue
        if rx.search(text):
            return cat
    if is_channel:
        return "degraded"
    return "unknown"


def is_ratelimit(text: str) -> bool:
    """风控信号判定（1.4 渠道退避复用；与 classify_error 同一规则源）。"""
    return classify_error(text) == "ratelimit"


def suggest_fixes(task: dict[str, Any]) -> list[str]:
    """任务 → 建议列表：合并 error/warnings/failed_step，按确定性排序、去重。"""
    texts = []
    if task.get("error"):
        texts.append(str(task["error"]))
    texts.extend(str(w) for w in (task.get("warnings") or []))

    cats: list[str] = []
    for t in texts:
        c = classify_error(t)
        if c != "unknown" and c not in cats:
            cats.append(c)

    step_cat = _STEP_CATEGORY.get(task.get("failed_step") or "")
    if step_cat and step_cat not in cats:
        cats.append(step_cat)

    ordered = [c for c in CATEGORY_ORDER if c in cats]
    fixes = [SUGGESTIONS[c] for c in ordered]
    return fixes or [FALLBACK_SUGGESTION]
