# WebSearch 评论抓取 spike 方案（TapTap / 知乎 · 受控预研）

> 创建：2026-08-18 | 状态：方案定稿，待主对话排期执行（预计 0.5~1 天）
> 关联：todolist 2.10「WebSearch 评论抓取 spike」；与《其他渠道关键词策略优化方案》
> （B站/微博/小红书）相互独立、互不依赖

## 一、背景与三个 Go/No-Go 问题

现状：WebSearch 渠道只取搜索结果**摘要**（5~13 条/查询），没有评论抓取能力；
对于 TapTap/知乎这类"一个帖子里藏着几十条真实用户反馈"的场景，摘要供给率
明显不足。2.10 要做的是**受控预研**：验证匿名直采评论的可行性、频率限制与
封禁风险，产出结论后再决定是否实现正式功能。

本 spike 只回答三个问题，不提前做功能：

| # | Go/No-Go 问题 | 判定依据 |
|---|---|---|
| Q1 | 匿名能否拿到 TapTap 帖子/评测下的评论？ | 评论完整率 + 硬信号率（见 §五） |
| Q2 | 匿名能否拿到知乎回答下的评论？ | 是否 403（签名墙）→ 大概率"需登录态" |
| Q3 | 安全频率窗口与封禁信号是什么？ | 逐请求错误率 + 信号代码（见 §四） |

### 已确认决策（用户拍板，保持不变）

1. **范围**：只做 TapTap 匿名 + 知乎匿名；知乎**不做登录态**（账号高风险），
   也不做签名逆向/验证码突破，只记录"需登录态"；
2. **抓取量硬上限**：3 查询 × 3 帖 × 20 评论/平台（每平台最多 9 帖、180 条）；
3. **错峰**：与真实采集任务完全错峰，单平台探测窗口 ≤ 30 分钟；
4. **产出物**：md + json 双份，落 `data/datasets/spike_websearch_comments/`；
5. **脚本**：独立脚本 `tests/spike_websearch_comments.py`，**不入回归**，手动运行。

## 二、抓取量、节奏与错峰（硬约束）

### 2.1 抓取量上限（脚本参数写死，CLI 可改但不可超出上限）

```text
--queries-per-platform  3     # 每平台 3 个查询
--posts-per-query       3     # 每查询取 3 个帖子
--comments-per-post     20    # 每帖最多 20 条评论
--interval              4.5   # 请求间隔均值（秒），±25% 抖动
```

单平台请求数上限 = 搜索页(≤2/查询 ×3) + 帖子页/评论页(≤3 帖 × 最多 2 次请求/帖)
≈ 6 + 18 = **24 次请求/平台**，两平台合计 ≤ 48 次，一次跑完。

### 2.2 节奏规则

- 间隔复用现有 WebSearch 设施 `jittered_sleep(4.5, 0.25)`（4.5s 均值 ±25% 抖动，
  与 2026-08-13 定下的生产节流一致，避免固定节奏被识别）；
- UA 从现有 `USER_AGENTS` 池随机；每请求带 `Referer`（上一页来源）+
  `Accept-Language: zh-CN,zh;q=0.9`；
- 失败重试：网络类异常重试 1 次（间隔 2s）；HTTP 4xx/风控信号**不重试，直接即停**。

### 2.3 错峰规则（写死）

1. 运行前检查任务状态目录（`data/state/*.json` 的任务锁/冷却标记）：有进行中任务
   或渠道冷却中 → 拒绝运行并提示；
2. 运行中不与其他任务并行（脚本单线程、无后台并发）；
3. 每平台跑完立即写盘部分结果（崩溃可续看）；
4. 全天最多运行 1 次本 spike（用 `data/state/spike_websearch_day.json` 记录日期，
   与 WebSearch 每日关键词上限 24 分开计数，但**同一日历日内两套都要看**——
   若当日 WebSearch 已用 ≥16 个关键词，spike 自动顺延到次日，避免同日双倍压力）。

## 三、平台 × 端点探测表

> 说明：凡标注"探测点"的都是**现场探测目标**，不是已确认事实；脚本按表格顺序
> 逐个尝试，命中即记录并进入下一段流程。探测结果必须原样落 report.json。

### 3.1 TapTap（匿名可行预期高）

| 环节 | 候选端点（按尝试顺序） | 请求形态 | 期望响应 | 标注 |
|---|---|---|---|---|
| 帖子发现 | 复用现有 WebSearch 搜索（360 → cn.bing 兜底），查询 `"{品牌} TapTap"`，域名过滤 `taptap.cn` / `taptap.com` | GET HTML | 搜索页 → 取前 3 个 TapTap 域名 URL | 现有设施 |
| 评论解析 A | 帖子/评测详情页直取：`https://www.taptap.com/moment/{id}` 或 `https://www.taptap.com/review/{id}`（以搜索结果 URL 为准） | GET HTML，UA + Referer | HTML 含评论列表块 | 探测点 |
| 评论解析 B | webapiv2 应用评测接口：`https://www.taptap.com/webapiv2/review/v2/by-app?app_id={app_id}` | GET JSON，带 `X-UA` / `X-Requested-With` / `Referer` | JSON `data.list[]` | 探测点（2021 年公开样例有效，现形态需现场确认） |

解析规则：评论块按"作者 + 正文 + 时间"三元组抽取，HTML 与 JSON 双解析器；
正文去重后计数，不足 20 条不补（宁缺勿滥，硬上限不可破）。

### 3.2 知乎（匿名预期失败：签名墙）

| 环节 | 候选端点（按尝试顺序） | 请求形态 | 期望响应 | 标注 |
|---|---|---|---|---|
| 帖子发现 | WebSearch 搜索 `"{品牌} 知乎"`，域名过滤 `zhihu.com` | GET HTML | 搜索页 → 取前 3 个知乎回答 URL | 现有设施 |
| 根评论 | `https://www.zhihu.com/api/v4/answers/{answer_id}/root_comments?limit=20&offset=0` | GET JSON，带 `x-requested-with: fetch` + `Referer` + **匿名裸请求**（不带任何 Cookie、不带签名头） | JSON `data[]` 或 403 `{"msg":"invalid zse"}` | 探测点（**预期 403**） |
| 子评论 | `https://www.zhihu.com/api/v4/comments/{comment_id}/child_comments?limit=20&offset=0` | 同上 | 同上 | 仅根评论成功后才尝试 |
| 备选 HTML | 回答页 `https://www.zhihu.com/question/{qid}/answer/{aid}` | GET HTML，匿名裸请求 | HTML 含 `__INITIAL_STATE__` / 评论区块 | 探测点（若 JSON 全挂，试 HTML 是否仍可公开读取） |

### 3.3 预研证据（已查实，写进报告引用）

- 知乎评论类接口强依赖动态签名头 `x-zse-96`（新版为 `x-zse-86`），签名生成依赖
  前端 JS 加密与**时间戳**，且 `d_c0` Cookie 参与签名；缺失任一即 **403**，
  响应体特征为 `{"msg":"invalid zse"}` 或直接空响应。匿名探测**预期必然失败**，
  这正是即停协议要验证的核心信号，不代表脚本故障。
- TapTap `webapiv2/review/v2/by-app` 有 2021 年公开可用样例（参数含 `app_id`、
  `X-UA`、`VID` 等），匿名抓应用评测可行；社区帖子/评论端点现形态需现场探测。

## 四、即停协议（硬规则，逐条可执行）

### 4.1 信号表：检测方式 → 动作

| 信号代码 | 检测方式（响应体/状态码/HTML 特征） | 动作 |
|---|---|---|
| `ZH_403_ZSE` | HTTP 403 且响应体含 `invalid zse` / `zse` | **立即停**知乎全部剩余请求；记录"需登录态" |
| `ZH_403_EMPTY` | HTTP 403 且空响应/非 JSON | 立即停知乎；记录状态码与响应前 200 字符 |
| `CAPTCHA` | HTML 含 `验证码`/`安全验证`/`captcha`/`滑动验证`（复用 `HARD_RISK_MARKERS`） | 立即停该平台全部剩余请求 |
| `RATE_LIMIT` | HTTP 429，或 403 且响应含 `频繁`/`limit` | 立即停该平台；记 `retry-after`（如有） |
| `EMPTY_2X` | 同一平台连续 2 次请求返回空结果/空评论块 | 停该查询，跳到下一查询；记录信号 |
| `NET_ERR` | 网络异常（连接/超时） | 重试 1 次（间隔 2s）；再失败停该端点 |

### 4.2 铁律（违反即 spike 作废）

1. **不绕过**：不做验证码识别/打码、不做 `x-zse` 签名逆向、不加载登录态
   （不含 `z_c0`/`d_c0`）、不用代理池/IP 轮换；
2. **即停优先于拿数据**：命中 `CAPTCHA`/`ZH_403_ZSE`/`RATE_LIMIT` 任一，
   该平台剩余请求全部作废，已抓部分照常落盘；
3. **不重试风控响应**：4xx + 风控特征一律不重试；
4. 每次请求前后各打一行日志（`时间戳 | 平台 | 端点 | 状态码 | 信号`），
   保证任何一步都能复盘。

## 五、指标与验收矩阵（写死判定）

### 5.1 指标定义

| 指标 | 定义 | 达标线 |
|---|---|---|
| 请求成功率 | 非 4xx/5xx 且解析不空的请求 / 该平台总请求 | ≥ 80% |
| 解析成功率 | 成功抽取到 ≥1 条评论的帖子 / 尝试解析的帖子 | ≥ 80% |
| 评论完整率 | 去重后实际评论数 / 计划评论数（单平台 180） | ≥ 80% 为"充分" |
| 硬信号率 | 命中 §4.1 硬信号的请求 / 总请求 | 0 为"干净" |
| 单帖去重评论 | 一帖去重后评论数（上限 20） | 记录实际值 |

### 5.2 验收矩阵

| 场景 | 判定 | 结论与后续动作 |
|---|---|---|
| TapTap 评论完整率 ≥80% 且硬信号率 =0 | **Go** | WebSearch 增强可行（匿名直采）；回主对话立项正式功能 |
| TapTap 完整率 50~79% 或有 `RATE_LIMIT` | **有条件** | 记录节流档位，回主对话定降量方案后复测 |
| TapTap 完整率 <50% 或出现 `CAPTCHA` | **No-Go** | 记录证据，评论抓取不做 TapTap；沿用摘要供给 |
| 知乎 `ZH_403_ZSE` | **记录"需登录态"** | 不突破、不立项匿名抓取；登记登录态路线为待评估项 |
| 知乎匿名意外成功且完整率 ≥80% | **Go（谨慎）** | 意外之喜，仍需在正式功能里保留即停协议 |

最终报告按上表输出一行**结论**：`TapTap=Go/有条件/No-Go，知乎=需登录态/Go`，
以及支撑该结论的三个数字（请求成功率 / 解析成功率 / 硬信号率）。

## 六、产出物与 report.json schema

目录：`data/datasets/spike_websearch_comments/`

```text
spike_websearch_comments/
├─ report.json          # 机器可读全量明细（schema 见下）
└─ report.md            # 人类可读摘要（结论 + 信号表 + 样本样例）
```

### report.json 结构（草案，字段写死）

```jsonc
{
  "spike": {
    "id": "spike_websearch_comments_20260818_1430",
    "created_at": "2026-08-18T14:30:00+08:00",
    "script": "tests/spike_websearch_comments.py",
    "cli_args": { "queries_per_platform": 3, "posts_per_query": 3,
                  "comments_per_post": 20, "interval_s": 4.5 }
  },
  "platforms": [
    {
      "platform": "taptap",
      "queries": ["恋与深空 TapTap", "恋与深空 评价 TapTap"],
      "requests_total": 24,
      "request_success_rate": 0.96,
      "parse_success_rate": 0.89,
      "comment_completeness": 0.85,
      "hard_signal_rate": 0.0,
      "stop_signal": null,
      "conclusion": "go",
      "requests": [
        {
          "seq": 1,
          "phase": "search",
          "query": "恋与深空 TapTap",
          "url": "https://www.so.com/s?q=...",
          "http_status": 200,
          "first_bytes": 512,
          "risk_signals": [],
          "parsed_items": 8,
          "comments": [
            { "author": "玩家A", "content": "……", "url": "https://www.taptap.com/moment/123" }
          ],
          "elapsed_s": 3.1
        }
      ]
    }
  ],
  "findings": [
    "知乎 root_comments 匿名请求 403 invalid zse → 需登录态（未突破）"
  ],
  "conclusion": "TapTap=go，知乎=需登录态"
}
```

`report.md` 摘要包含：结论行、两平台信号时间线、TapTap 单帖样本 3~5 条原文、
失败响应前 200 字符（脱敏，不含 Cookie）、对主对话的建议（下一步是否立项）。

## 七、不撞车检查

| 现有能力/在途项 | 本 spike 的处理 |
|---|---|
| 2.2 WebSearch 关键词策略与开关 | 不动；spike 用现有 `build_query` 复用子渠道提示，不写策略文件 |
| 2.9 实际查询串透明度 | 不涉及；spike 是独立预研脚本，不进正式报告 |
| 其他渠道关键词策略方案（B站/微博/小红书） | 完全独立：spike 只碰 TapTap/知乎匿名探测，不改任何渠道策略 |
| 2.8 自定义维度 + 全量 LLM | 不涉及：spike 无 LLM 调用、无评测/黄金集影响 |
| 2.6 其他品类提准（标注卷/裁判） | 不涉及：spike 产出物在独立目录，不进入标注/裁判流水线 |
| 真实采集任务与风控冷却 | 错峰规则（§2.3）保证不并行、不同日叠加压力 |
| 回归测试 | 独立脚本**不入回归**；不触碰 `tests/run_regression.py` 覆盖文件 |

## 八、执行清单

1. 写 `tests/spike_websearch_comments.py`：
   - 复用 `app/channels/websearch.py` 的 `build_query` / `_fetch` / `USER_AGENTS` /
     `HARD_RISK_MARKERS` / `jittered_sleep`（只 import，不改动生产文件）；
   - 新增 TapTap HTML 评论解析器、TapTap webapiv2 JSON 解析器、
     知乎 root_comments/child_comments 探测函数；
   - 实现 §4.1 信号表、§5.1 指标计算、§6 report.json/report.md 落盘；
2. 冒烟：用 1 查询 1 帖 5 评论跑通（间隔照常 4.5s），确认落盘结构；
3. 正式跑：TapTap → 知乎，全程不与其他任务并行；知乎预期 403 即停；
4. 人工读 `report.md`：对照 §5.2 矩阵出结论；
5. 结论带回主对话：Go → 立项"WebSearch 评论抓取"正式功能；
   No-Go/需登录态 → 更新 todolist 2.10 与决策日志，登记延后/登录态路线；
6. 清理：spike 脚本保留（复测用），状态文件 `spike_websearch_day.json` 保留。

预估：0.5~1 天（含冒烟与正式跑）；无新增 LLM 费用；新增网络请求 ≤ 48 次。

## 九、风险矩阵

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| 知乎签名墙导致 Q2 直接失败 | 极高（有公开证据） | 无——本就把"记录需登录态"当预期结论 | 即停协议 + 报告如实记录，不突破 |
| TapTap webapiv2 现形态与 2021 样例不符 | 中 | 评论解析 A 兜底（HTML） | 双解析器 + 探测点标注 |
| 触发 TapTap 限流/封禁信号 | 低（24 次请求/平台、4.5s 节流） | 影响当日后续采集 | 错峰 + 即停 + 冷却标记写入 `data/state` |
| 误伤真实任务数据 | 低 | 同日关键词总量叠加 | §2.3 双上限检查，顺延次日 |
| 脚本 bug 导致请求失控 | 低 | 超上限请求 | 循环内硬校验 `requests_total` ≤ 24/平台，超限立即抛错退出 |
| 产出物含敏感内容（原文） | 中 | 报告泄露用户原文 | 与运行报告同规：目录说明"仅本机、勿同步"；样本 ≤5 条 |

## 十、待主对话拍板点

1. 冒烟阶段是否也按正式节奏跑（4.5s 间隔，冒烟约 2~3 分钟）——建议是；
2. 若 TapTap 为"有条件"（完整率 50~79%），降量方案（如每查询 2 帖/每帖 15 条）
   是否直接授权执行，还是回主对话再定——建议回主对话；
3. 知乎若意外匿名成功，是否授权用 HTML 备选端点继续验证——建议授权
   （仍受即停协议约束），避免白跑一轮。
