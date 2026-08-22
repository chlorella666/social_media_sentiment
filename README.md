# 社交媒体情感分析器（Social Media Sentiment Analyzer）

![CI](https://github.com/chlorella666/social_media_sentiment/actions/workflows/ci.yml/badge.svg)

一个**开箱即用的本地舆情分析工具**：输入品牌或产品名，自动采集微博 / 小红书 / B站 /
全网搜索的真实讨论，用 AI 判断每条内容的情感（正面 / 负面 / 中性），并生成带图表的
分析报告（Excel + HTML + Word）。

> English: A local-first social media sentiment analysis tool. Enter a brand or keyword,
> it collects real posts from Weibo / Xiaohongshu / Bilibili / WebSearch, classifies
> sentiment with a lexicon + optional LLM pipeline, and produces charts and reports
> (Excel / HTML / Word). No data leaves your computer unless you enable LLM analysis.

## 亮点

- **工程纪律**：38 个单元/UI 测试 + 黄金集准确率门槛 + 双平台 CI，每次改动都有回归保护；
- **分析质量**：内置词典 + 可选大模型（LLM）精分析，配有专门的评测体系（黄金集 / 边界集 / 维度级情感）；
- **完整产品链路**：向导式操作 → 后台任务队列 → 图表报告 → 数据导出，小白也能 5 分钟出一份报告。

## 合规说明

本项目仅用于合规的个人数据分析和学习研究；用户须遵守平台条款与适用法律；
不包含任何绕过平台防护的技术。

- 渠道采集均基于用户**自备凭据**（如微博 Cookie）或公开接口，内置采集上限与风控冷却；
- 仓库内评估集（`tests/fixtures/*.csv`）为**脱敏版**：真实评论原文中的链接、@提及、
  邮箱、手机号、身份证号与微博用户名归属已替换为占位符
  （见 [scripts/desensitize_public_fixtures.py](scripts/desensitize_public_fixtures.py)）；
- 运行产物（`data/`）不入库，密钥/凭据本地加密存储，仅用于本机任务；
- 许可证：[AGPL-3.0](LICENSE)（个人学习/研究自由使用；商用请遵守其条款）。

## 快速开始（小白版）

> 时间预算：约 10 分钟。你只需要会「双击」或「复制粘贴命令」。

### 选择你的系统

> 发布时提供 **Windows 版** 与 **macOS 版** 两个下载包，请对照你的电脑选择；
> 也可以直接用下面的命令行方式从源码运行。

### 🪟 Windows 版（推荐 Windows 10/11）

> ⚠️ **系统要求：仅支持 Windows 10/11。** 应用内「一键安装 Node.js / opencli」依赖
> 系统内置的 winget（Windows 10/11 自带）；Windows 7/8 无法一键安装，
> 需手动安装 Python / Node.js，并手动执行 `npm install -g @jackwener/opencli`。

1. **安装 Python**：打开 https://www.python.org/downloads/ 下载安装包，
   安装时**务必勾选 "Add Python to PATH"**（否则双击 `run.bat` 会提示找不到 Python）；
2. 把本项目文件夹解压/克隆到任意目录；
3. **双击 `run.bat`**：
   - 第一次运行会自动安装依赖（约 1~2 分钟，耐心等待）；
   - 装好后会自动启动后台任务进程，并打开浏览器进入应用；
4. 浏览器看到「① 品牌和分析维度」页面 = 启动成功。

### 🍎 macOS 版

> 系统要求：macOS（Intel / Apple Silicon 均可）+ Python 3.10+。
> 推荐先安装 [Homebrew](https://brew.sh)（应用内一键安装 Node.js 依赖它）。

打开「终端」，逐条复制粘贴：

```bash
# 1. 进入项目目录（把 <路径> 换成你的项目位置）
cd <路径>

# 2. 创建独立运行环境（虚拟环境 = 给本项目单独开一个小房间，不污染系统）
python3 -m venv .venv

# 3. 激活它
source .venv/bin/activate

# 4. 安装依赖（第一次需要，约 1~2 分钟）
pip install -r requirements.txt

# 5. 启动后台任务进程（另开一个终端，先激活 .venv 再执行）
python app/worker.py

# 6. 启动应用
python -m streamlit run app/main.py
```

看到 `Local URL: http://localhost:8501` 就成功了，浏览器会自动打开（没自动开就手动访问这个地址）。

**macOS 版差异说明：**

- 小红书渠道的 opencli 可在应用内一键安装（③ 渠道页勾选小红书后出现）；若未装 Node.js，
  应用会用 Homebrew 自动安装，装完重启应用即可；
- 「保存 API Key / 微博 Cookie 到本机」在 macOS 上使用仅当前用户可读的本地文件保存
  （Windows 版为加密存储）；更在意安全可每次使用时临时粘贴；
- 词云与报告的中文字体已适配 macOS 自带字体（PingFang SC 等），无需额外配置。

### 第一次体验：5 分钟出一份报告

1. 在「① 品牌和分析维度」输入品牌名（比如「云朵咖啡」），勾选演示数据渠道；
2. 一路点「下一步」，渠道只勾选 **「演示数据」**（不需要任何账号）；
3. 点「确认运行」，等 1 分钟左右；
4. 完成后自动进入结果页：结论、图表、下载区都在这里。

> 「演示数据」是内置的模拟帖子，用来跑通全流程；想分析真实平台数据，看下面的「渠道能力」表。

## 使用流程（向导 6 步）

| 步骤 | 做什么 |
|------|--------|
| ① 品牌和分析维度 | 输入品牌/产品名，选择分析维度（数字产品 / 有形实物 / 服务内容，可多选） |
| ② 关键词确认 | 核对采集关键词（可手动修改） |
| ③ 渠道与时间 | 勾选采集渠道、设置时间范围、采集上限 |
| ④ 确认运行 | 查看计划摘要（采集量/耗时/费用估算）并启动 |
| ⑤ 后台执行 | 任务在后台运行，关掉页面也不中断，可在「后台任务中心」看进度 |
| ⑥ 结果 | 看结论与图表，下载 Excel / HTML / Word 报告 |

## 渠道能力（每个渠道需要什么）

| 渠道 | 需要准备 | 说明 |
|------|----------|------|
| 演示数据 | 无 | 内置模拟数据，跑通流程用 |
| B站 | 无 | 公开接口，零登录，最省心 |
| WebSearch | 无 | 搜索摘要供给（不支持抓取评论），直接可用，风控下采集量可能偏少 |
| 微博 | 自己的微博 Cookie | 页面有图文指引；建议用小号，避免账号风险 |
| 小红书 | Chrome 登录态 + opencli | 门槛最高：需要浏览器保持登录 + 额外安装 opencli；采集量受平台风控影响 |

**加量建议**：提高单关键词上限容易触发平台风控，正确做法是**增加关键词**（应用内有「关键词优化」开关帮你扩词）。

## 大模型精分析（可选，更准）

默认不开 LLM，完全离线运行（词典模式，免费、不发送任何数据）。想更准可以开启：

1. 侧边栏「大模型设置」选择服务商（推荐 DeepSeek，便宜）；
2. 粘贴你的 API Key（获取指引在应用内有图文版），点「测试连接」；
3. 开启后，低置信度文本会发给所选服务商做精分析；不开也完全能用。

> 开启 LLM 时，界面会明确提示哪些文本将被发送到第三方；Key 以加密方式只存在本机。

## 常见问题（FAQ）

**Q：安装依赖很慢 / 卡住？**
A：首次安装 streamlit/plotly 等约 1~2 分钟属正常；网络差可换国内 pip 镜像：
`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`。

**Q：双击 run.bat 提示找不到 Python？**
A：Python 没装或没勾选 "Add to PATH"。重装 Python 并勾选该选项，或改用方式二命令行。

**Q：启动后页面空白 / 打不开？**
A：确认终端里显示 `Local URL: http://localhost:8501`，手动在浏览器访问该地址；
若是端口被占用，改端口启动：`python -m streamlit run app/main.py --server.port 8502`。

**Q：选了微博/小红书，但采集到 0 条？**
A：通常是凭据问题：微博需要有效 Cookie（过期会弹窗提示重粘）；小红书需要 Chrome 登录态
且最容易被风控。先用「演示数据」或「B站」确认流程正常，再加真实渠道。

**Q：会用掉很多钱吗？**
A：不开 LLM 完全免费；开 LLM 按调用量计费（DeepSeek 单次分析通常几分钱到几毛钱），
任务开始前会显示费用估算。

**Q：我的数据安全吗？**
A：默认完全本地运行，数据、报告、密钥都只存在你自己电脑上；只有在你主动开启 LLM 时，
低置信度文本才会发给所选服务商。

**Q：可以商用吗？**
A：项目采用 AGPL-3.0 许可证：个人学习/研究自由使用；商用/再分发需遵守 AGPL 条款
（见 [LICENSE](LICENSE)）。

## 开发者 / 进阶

### 后台任务队列

- 任务提交后写入本地 SQLite（`data/app.db`），由独立常驻进程执行，关浏览器任务照常跑；
- 代码更新后 worker 自动检测版本变化并重启；
- 应用顶部「后台任务中心」可查看运行中与历史任务、回看报告、一键重跑；
- 停止后台进程：双击 `stop_worker.bat`。

### 错误排查

- 任务失败时页面会指出**失败步骤、原因与建议**（如更新微博 Cookie、降低采集上限、
  检查 LLM 配置、扩大关键词），并附该任务的执行日志；
- 结构化日志本地留存：`data/logs/app.jsonl`（JSONL，自动轮转）。

### 渠道安全与配额

- 各渠道**每关键词条数上限**有默认值与封顶（B站 20/50、微博 20/30、小红书 10/10、
  WebSearch 13/13）；加量建议增加关键词而非调大上限；
- 「渠道/时间」步骤提供每日上限、暂停/恢复、解除风控冷却；
- 检测到平台风控时渠道自动冷却（10 分钟起步，同日翻倍至 60 分钟封顶）。

### 关键词优化

- 「渠道/时间」步骤提供各渠道**关键词优化开关**（默认关）：WebSearch 自动追加
  「评价」等后缀并支持策略词；B站/微博/小红书按渠道策略展开查询组合；
- 结果页有**消费者声音占比**指标，帮助判断报告里真实消费者声音的占比。

### 人工相关性筛选

- 可开启**人工筛选相关性**：采集并清洗后任务暂停，进入审核页逐条过帖子
  （二级结构，可展开看评论），剔除的内容进入丢弃明细，LLM 编码只作用于筛选后文本；
- 同时开启 LLM 相关性复核时，LLM 判定仅作为「建议徽标」，最终以人工为准。

### 数据管理（1.7）

- 侧边栏「数据管理」显示各类占用（报告/归档/数据集/日志/回归/数据库/密钥）；
- **自动归档**：不在最近 30 个且超 7 天且终态且无人工筛选快照的报告目录，一键移入归档；
- **显式清理**（删除不可恢复，优先回收站）：超期归档、超 30 天日志、旧回归报告；
- **永不自动触碰**：`data/secrets/`（API Key/Cookie）与 `data/state/`（确认记录）。

### 评测中心（开发者工具）

- 双击 `eval.bat` 打开评测仪表盘：黄金集按渠道/领域/情感类别/类型/语言现象细分跑分、
  准确率趋势、混淆矩阵、两次运行对比、错误样本下钻、关键词效果；
- 侧边栏**改动类型引导**：按改动类型（领域级/模块级/通用规则/采集清洗）推荐数据集组合并一键应用；
- 冻结基线：`tests/fixtures/baseline_lexicon.json`（词典，回归门槛）。

### 关键词策略调优（2.2）

- 用关键词效果数据反推后缀与同义词：`python app/core/keyword_effects.py --scan-dir data/reports --candidates`；
- 候选经人工确认后写入 `app/channels/keyword_strategy.json`，WebSearch 下次采集生效。

### 测试与一键回归

- 一键回归（38 个单元/UI 测试 + 黄金集词典门槛）：双击 `regress.bat`（POSIX 用
  `./regress.sh`），等价于 `python tests/run_regression.py`；
- 黄金集词典直判准确率跌破基线 1.0pp 即 FAIL，需 `--accept-baseline` 显式接受新基线；
- 混合 LLM 评测（需 API Key）：`python tests/run_regression.py --llm`（仅记录不设门槛）；
- CI：`.github/workflows/ci.yml`（windows-latest + ubuntu-latest 双平台），push 后自动生效；
- 扩展接口契约：`test_contracts.py` 强制校验渠道注册表与领域 JSON schema。

### 项目结构

详见 [docs/项目方案.md](docs/项目方案.md)（技术规格与产品方案）、
[docs/决策日志.md](docs/决策日志.md)（历史决策）与 [docs/README.md](docs/README.md)（文档索引）。
