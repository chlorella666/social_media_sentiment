# 给实施 Agent 的任务指令（直接复制粘贴使用）

把下面整段话作为任务发给你的实施 agent：

---

我需要你把一套已定稿的 UI 视觉方案落地到我的正式应用中。**只改视觉层，不改任何业务逻辑。**

## 项目

- 路径：`D:/app/codex/social_media_sentence`
- 技术栈：Streamlit 主应用（`app/main.py`，约 3000 行，6 步向导+结果页）+ Plotly 图表（`app/output/html_report.py`）+ Jinja2 HTML 报告模板（`app/output/templates/report.html.j2`）

## 设计材料（先全部读完再动手）

按此顺序阅读 `design_entries/ui-designer/` 下：

1. `HANDOFF.md` —— **实施交接指南，你的主要任务书**（背景、硬约束、文件落点、分阶段任务、验收清单全在里面）
2. `design_tokens.css` —— 所有颜色/字号/间距/圆角/阴影的**唯一数值来源**，禁止新增裸 hex
3. `prototype_app.html` —— 主应用主题基准（侧边栏/向导步骤条/空态/错误态/加载态），浏览器打开对照
4. `prototype_result.html` —— 结果页"结论优先"首屏基准
5. `prototype_report.html` —— HTML 报告视觉基准（封面/指标卡/图表融合/表格）
6. `streamlit_config.toml` 和 `streamlit_global.css` —— 可直接使用的落地件（config 复制为 `.streamlit/config.toml`；CSS 作为全局注入基础，按当前 Streamlit 版本修选择器）

## 执行要求

1. 按 `HANDOFF.md` 第五节的任务顺序做：先 P0（主题止血：config.toml + 全局 CSS + 报告模板换令牌色），验收通过后再做 P1（结果页首屏重排 + 步骤条 SVG 图标 + 三态规范 + 图表配色），P2 可选。
2. 每完成一个阶段，对照 `HANDOFF.md` 第六节的验收清单自查，给我一份逐项打勾的结果。
3. 硬约束（语义锁定、✓/✗/～ 符号、悬停只变亮度、WCAG AA、44px 触控目标）写在 HANDOFF.md 第三节，**违反任何一条都要返工**。
4. 改动 Streamlit 相关 CSS 时注意：`streamlit_global.css` 里的 data-testid 选择器可能随版本失效，逐个在真实 DOM 上验证，失效的换成可靠方案。
5. 遇到原型没覆盖、HANDOFF 也没写的视觉决策，不要自己发挥——列出来问我。
6. 不动的东西：业务逻辑、渠道层、分析流程、数据结构、向导步骤顺序与命名。

## 完成标志

- 应用启动后无默认紫红主题残留
- 结果页首屏（不滚动）能看到一句话结论 + 4 指标卡
- 生成的 HTML 报告与 `prototype_report.html` 观感一致
- 验收清单全部打勾

---
