"""UI 共享常量（main.py 拆分，2026-08-22）。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

REPORTS_DIR = ROOT / "data" / "reports"
SENTIMENT_NAMES = {"positive": "正面", "negative": "负面", "neutral": "中性"}

STEP_DEFS = [
    ("collect", "采集数据"),
    ("clean", "清洗与去重"),
    ("lexicon", "词典预筛"),
    ("llm", "LLM 精分析"),
    ("narrative", "叙事/归因分析"),
    ("report", "生成报告"),
]
STEP_ICONS = {
    "pending": "⏳",
    "running": "🔄",
    "done": "✅",
    "skipped": "⏭",
    "failed": "❌",
}

# P1-2：线性 SVG 状态图标（stroke=currentColor，颜色由外层样式控制），替换步骤 emoji
_SVG_WRAP = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
    'stroke-linejoin="round">{body}</svg>'
)
_STEP_STATE_SVG = {
    "pending": _SVG_WRAP.format(body='<circle cx="12" cy="12" r="9"/>'),
    "running": _SVG_WRAP.format(
        body='<path d="M21 12a9 9 0 1 1-6.2-8.5"/>'
    ),
    "done": _SVG_WRAP.format(body='<path d="M20 6 9 17l-5-5"/>'),
    "skipped": _SVG_WRAP.format(
        body='<polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19"/>'
    ),
    "failed": _SVG_WRAP.format(
        body='<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>'
    ),
}
_WIZARD_STEP_ICONS = [
    # 品牌和维度 / 关键词 / 渠道时间 / 确认运行 / 后台执行 / 结果
    _SVG_WRAP.format(
        body=('<rect x="3" y="3" width="7" height="7" rx="1"/>'
              '<rect x="14" y="3" width="7" height="7" rx="1"/>'
              '<rect x="3" y="14" width="7" height="7" rx="1"/>'
              '<rect x="14" y="14" width="7" height="7" rx="1"/>')
    ),
    _SVG_WRAP.format(
        body='<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>'
    ),
    _SVG_WRAP.format(
        body=('<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/>'
              '<path d="M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20"/>')
    ),
    _SVG_WRAP.format(
        body=('<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>'
              '<rect x="8" y="2" width="8" height="4" rx="1"/>'
              '<path d="M9 14l2 2 4-4"/>')
    ),
    _SVG_WRAP.format(
        body=('<path d="M12 2v4M12 18v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8"/>'
              '<path d="M2 12h4M18 12h4M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8"/>')
    ),
    _SVG_WRAP.format(
        body='<path d="M3 3v18h18"/><path d="M7 15v-4M12 15V8M17 15v-6"/>'
    ),
]

CHANNEL_LIMIT_DEFAULTS = {
    "demo": 10,
    "bilibili": 10,
    "websearch": 13,
    "websearch_zhihu": 13,
    "websearch_tieba": 13,
    "websearch_taptap": 13,
    "weibo": 10,
    "xiaohongshu": 10,
}
# 各渠道每关键词条数上限：默认值 + 封顶（UI 与渠道层双重限制）。
# 加量建议增加关键词（策略加词），而非调大上限；demo 为确定性演示数据，限额不影响产量。
CHANNEL_LIMIT_MAX = {
    "demo": 200,
    "bilibili": 50,
    "websearch": 13,
    "websearch_zhihu": 13,
    "websearch_tieba": 13,
    "websearch_taptap": 13,
    "weibo": 30,
    "xiaohongshu": 10,
}
CHANNEL_LIMIT_HELP = {
    "bilibili": "B站公开 API、零登录最安全：默认 10、上限 50；加量建议加关键词",
    "weibo": "微博账号级风控最严：默认 20、上限 30，单关键词约 500 条封顶",
    "xiaohongshu": "小红书反爬最严（xsec_token+签名）：默认/封顶 10，单次建议 ≤10",
    "demo": "演示数据固定 2 条/平台/关键词，限额不影响产量",
}

# 1（2026-08-19 UX 调整）：小白获取 API Key 的操作指引（侧边栏大模型设置内展示）
API_KEY_GUIDE = (
    "**DeepSeek（推荐，便宜）**\n"
    "1. 打开 https://platform.deepseek.com 并注册/登录；\n"
    "2. 左侧菜单进入「API Keys」→ 点「创建 API Key」；\n"
    "3. 复制生成的 Key（只显示一次，关掉就看不到了）；\n"
    "4. 粘贴到上方输入框并「保存到本机」。\n"
    "5. 首次使用需在「充值」页面充值（最低 ¥10 起），否则会报余额不足。\n\n"
    "**OpenAI**\n"
    "1. 打开 https://platform.openai.com 并登录；\n"
    "2. 右上角头像 →「API keys」→「Create new secret key」；\n"
    "3. 复制 Key（只显示一次）后粘贴到上方输入框。\n\n"
    "⚠️ Key 相当于付款凭证：只粘贴到你自己的电脑，程序会加密保存在本机，"
    "不会上传或写入报告。"
)
WS_PROBE_STATUS_CN = {
    "ok": "✅ 正常",
    "degraded": "⚠️ 降级",
    "risk": "❌ 风控",
    "error": "❌ 失败",
    "empty": "⚠️ 空结果",
}
HEALTH_LEVEL_CN = {
    "ok": "✅ 可用",
    "warn": "⚠️ 存疑",
    "error": "🔴 不可用",
}
# 广告/官方与人工复核：三选一（方案 A，2026-08-16）
AD_REVIEW_MODES = [
    "自动（广告/官方计入统计）",
    "自动（按规则剔除广告/官方）",
    "人工复核（采集后暂停，相关性+广告/官方一起审，标记的广告/官方剔除统计）",
]
AD_REVIEW_SHORT = {
    AD_REVIEW_MODES[0]: "自动（广告计入统计）",
    AD_REVIEW_MODES[1]: "自动（规则剔除广告）",
    AD_REVIEW_MODES[2]: "人工复核（相关性+广告/官方，标记剔除）",
}
COMMENT_FETCH_SECONDS = 0.3  # 每条评论抓取耗时粗估（阶段 2 按实测校准）
