# 实施交接指南（HANDOFF）

> 本文档是给**实施 agent / 开发者**的唯一权威交接材料。
> 目标：将 `design_entries/ui-designer/` 下的视觉方案落地到正式应用，做到"照着做、不用猜"。
> 设计意图问题一律以三个 HTML 原型为准；令牌数值以 `design_tokens.css` 为准（单一事实来源）。

---

## 一、项目背景（30 秒版）

- 项目：社交媒体情感分析器（本地 Streamlit 应用），6 步向导（采集→清洗→词典预筛→LLM 精分析→叙事/归因→生成报告）+ 结果页 + HTML/Word 报告交付。
- 用户画像：不会写代码的小白用户，应用必须"不吓人"；同时报告要能拿去汇报。
- 本次任务：视觉升级竞稿已定稿（ui-designer 方案），现需**把原型视觉落地到正式代码**。只改视觉层，不改业务逻辑。

## 二、材料清单（都在 `design_entries/ui-designer/`）

| 文件 | 角色 | 实施时怎么用 |
|---|---|---|
| `design_tokens.css` | **令牌单一事实来源** | 所有颜色/字号/间距/圆角/阴影数值从这里取；禁止新增裸 hex |
| `prototype_app.html` | 命题 A：主应用主题原型 | 浏览器打开对照；侧边栏/向导步骤条/三态（空/错/加载）的视觉基准 |
| `prototype_result.html` | 命题 B：结果页"结论优先"首屏原型 | 首屏信息架构与布局基准 |
| `prototype_report.html` | 命题 C：HTML 报告视觉稿 | 报告封面/指标卡/图表融合/表格排版基准 |
| `streamlit_config.toml` | P0 落地件 | 直接复制为 `.streamlit/config.toml`（项目当前无此目录） |
| `streamlit_global.css` | P0/P1 落地件 | 注入主应用的全局样式草案，含 Streamlit DOM 选择器 |
| `README.md` | 设计说明 | 风格关键词、令牌速查、取舍理由 |

## 三、硬约束（违反任何一条 = 返工）

1. **语义锁定**
   - 品牌蓝 `#2B7BD6` 及衍生：只用于按钮/链接/导航/焦点，**绝不表情感**。
   - 情感三色（`--pos-500` / `--neg-500` / `--neu-500`）及衍生：只用于图表/标签/徽章，**绝不上按钮、绝不做页面主色**。
   - `--warning-500`（琥珀）= 数据/流程警示（如采集不足）；`--neg-500`（红）= 负面情绪。**两者不得混用**——现状里"采集不足"和"负面情绪"共用红是本次要修掉的语义混淆。
2. **情感信息永不单靠颜色**：正/负/中性标签必须带 `✓ / ✗ / ～` 符号（原型里的"情感标签三件套"：符号+色点+文字）。
3. **悬停只允许亮度/透明度变化**，禁止位移、缩放、变色、阴影浮起。
4. **可访问性（WCAG AA）**
   - 正文/背景对比 ≥ 4.5:1；已知边界：品牌蓝底白字 4.3:1，处理策略 = 字重 700 + 字号 ≥15px + hover 加深至 `--brand-600`，不改令牌。
   - 按钮/触控目标最小高度 44px。
   - 保留键盘焦点环（`:focus-visible`，`--brand-500` 描边）。
   - 动画遵守 `prefers-reduced-motion`。
5. **图标去 emoji 化**：界面 chrome（导航/步骤/按钮）用线性 SVG 图标（stroke 1.5px，`currentColor`）；emoji 仅允许保留在用户数据内容里。注意：现有 `STEP_ICONS` 的 ⏳🔄✅⏭❌ 是本次替换对象（步骤状态改用 SVG+语义色）。
6. **数字排版**：所有统计数字 `font-variant-numeric: tabular-nums`。
7. **产品名**：统一用"社交媒体情感分析器"（`st.set_page_config` 已是此名），如后续改名需全局替换。

## 四、文件落点映射（改哪里）

| 原型区块 | 真实代码落点 | 说明 |
|---|---|---|
| 全局主题（配色/字体） | 新建 `.streamlit/config.toml` ← 复制 `streamlit_config.toml` | 项目当前没有 `.streamlit/`，直接创建 |
| 全局 CSS 注入 | `app/main.py` 顶部（`st.set_page_config` 之后）加一个 `st.markdown(<style>…, unsafe_allow_html=True)` 注入函数，内容取 `streamlit_global.css` | 建议抽成 `app/ui/theme.py` 里的 `inject_global_css()`，main.py 调用一次 |
| 侧边栏视觉 | `app/main.py` L1281 起的 `with st.sidebar:` 块 | 结构不动，只改视觉（背景/分组/标题层级） |
| 向导步骤条 | `app/main.py` L2442 附近的 STEP 渲染循环 + `STEP_DEFS`(L231)/`STEP_ICONS`(L239) | 状态图标 emoji → SVG/语义色；步骤名不变 |
| 三态（空/错/加载） | `app/main.py` 各渲染分支（`render_task_center` L564、`render_channel_diag` L331 等） | 用全局 CSS 里的 `.st-emergency` / `.st-empty` / `.st-loading` 语义类规范 |
| 结果页首屏 | `app/main.py` 结果页渲染段（跑完 6 步后的展示区） | 按 `prototype_result.html` 重排：一句话结论→4 指标卡→唯一默认展开主图→视图切换→折叠钻取 |
| HTML 报告 | `app/output/templates/report.html.j2`（389 行）+ `app/output/html_report.py`（1295 行） | 封面/指标卡/表格/图表配色按 `prototype_report.html`；图表颜色改在 html_report.py 各 `*_fig()` 函数的 Plotly 参数里 |

## 五、分阶段实施任务

### P0（半天，主题止血）
- [ ] 创建 `.streamlit/config.toml`（直接用交付件），验证默认紫红全部消失。
- [ ] main.py 注入全局 CSS（用交付件），验证灰阶/字体/间距生效。
- [ ] 报告 `report.html.j2` 内嵌样式替换为令牌色（至少：正文/标题/分隔线/表格斑马纹/情感标签三件套）。

### P1（1-2 天，核心体验）
- [ ] 结果页首屏按 `prototype_result.html` 重排（结论优先）：
  1. 一句话结论（含可信度胶囊：数据量+时间窗）；
  2. 4 张指标卡（总声量/正面占比/负面占比/中性占比，数字 tabular-nums）；
  3. 情感主图**唯一默认展开**，其余 11 图折叠（expander 钻取）；
  4. "只看结论 / 全部图表"视图切换。
- [ ] 向导步骤条：SVG 状态图标 + 语义色（pending 灰 / running 品牌蓝 / done success 绿 / skipped 灰降透明 / failed danger 红）。
- [ ] 空态/错误态/加载态按原型规范统一（`prototype_app.html` 下半部分）。
- [ ] 图表配色：`html_report.py` 各 fig 函数统一用情感三色 + 灰阶坐标轴，去默认 Plotly 蓝紫。

### P2（可选打磨）
- [ ] 报告封面升级为白底封面卡 + 报告编号（见 `prototype_report.html`）。
- [ ] 悬停微调、`prefers-reduced-motion` 审计、深色模式预留（令牌已按变量化设计，`[data-theme="dark"]` 可后加）。

## 六、验收清单（对照打勾）

- [ ] 全局搜索业务代码，无新增裸 hex 颜色（图表 Plotly 参数引用令牌值或集中常量）。
- [ ] 任何按钮上找不到情感三色；任何图表/情感标签上找不到品牌蓝做数据色。
- [ ] "采集不足"提示为琥珀色，"负面情绪"为红色，两者肉眼可区分。
- [ ] 正/负/中性标签全部带 ✓/✗/～ 符号（含报告 HTML、结果页、图表图例）。
- [ ] 结果页打开后第一屏能看到一句话结论 + 4 指标卡，不需要滚动。
- [ ] 键盘 Tab 遍历所有交互控件有可见焦点环。
- [ ] 主按钮：品牌蓝底、白字、粗体、≥15px、≥44px 高、hover 变深不位移。
- [ ] 生成一份 HTML 报告，与 `prototype_report.html` 并排打开，观感一致（封面/指标卡/表格）。
- [ ] `prefers-reduced-motion: reduce` 下无旋转/滑动动画。

## 七、已知边界与注意事项

1. **Streamlit DOM 选择器脆弱性**：`streamlit_global.css` 里的 `[data-testid="stSidebar"]` 类选择器随 Streamlit 版本可能失效，实施后需在当前锁定版本上逐个验证；失效的改用 `st.markdown` 包裹方案。
2. **结果页折叠钻取**：用 Streamlit 原生 `st.expander` 实现即可，不必自造组件；首屏默认展开项只有情感主图一个。
3. **图表改色位置**：Plotly 图颜色不在 CSS 里，在 `html_report.py` 各 `*_fig()` 的 `marker.color` / `colorway` / template 参数中改；建议在文件顶部建 `COLORS = {...}` 常量字典集中管理，值取 `design_tokens.css`。
4. **Word 报告**（`word_report.py`）不在本次范围，但若顺手，其情感色应取同一令牌值。
5. **不改的东西**：业务逻辑、渠道层、分析流程、数据结构、向导步骤顺序与命名，全部不动。

## 八、有问题找谁对照

- 视觉细节不清楚 → 打开对应 HTML 原型（浏览器直接开，无需服务器）。
- 数值不确定 → `design_tokens.css`。
- 取舍理由/风格关键词 → `README.md`。
