# 自定义维度参与统计 + 全量 LLM 路由执行方案

> 创建：2026-08-18 | 状态：**已执行完成（2026-08-18）** | 来源：侧线讨论（主对话移交）
> 关联：todolist 2.8（自定义维度）、2.6 其他品类提准（模块化分类）、
> 抽样与标注规范 §十四（锚定品牌/文本自身口径）

## 一、问题与背景

### 1.1 现状问题

1. **自定义维度已失效**：模块化向导改造后（M1，2026-08-17），`custom_dim`
   只在 `app/main.py` ① 页被收集进 session_state，**没有任何下游消费**——
   不进 `build_plan`、不进采集、不进统计。UI 上"自定义维度仅用于补充采集，
   不参与维度统计"这句话已过时（实际上现在什么都不做）。
2. **词典直判维度失真**：词典直判文本的维度情感只来自词典兜底
   （`match_dimension_sentiments` 关键词命中 + 极性聚合）；评测记录显示
   词典直判模式维度微平均 F1 约 **0.12~0.17**，混合（LLM）模式约 **0.42~0.65**。
3. **目标**：用户针对特定品牌临时增加自定义维度，且希望这些维度**参与维度统计**；
   同时把"LLM 开启 = 全量 LLM + 自动相关性复核"确立为新的质量口径，消除
   词典直判带来的维度失真，减少用户决策负担。

### 1.2 决策边界

- 自定义维度为**任务级临时参与**，不做长期保留/转正通道（2.5 提案引擎仍保留，
  但不为本需求新增入口）。
- 接受 LLM 全量 + 自动相关性复核带来的**费用与耗时上升**（受控且有记录）。

## 二、已确认决策

1. 自定义维度由 **LLM 判定**（不依赖词典兜底；词典仅在 LLM 失败/未启用时降级）。
2. 未选模块时，自定义维度**允许单独参与统计**（schema = 仅自定义维度）。
3. 自定义维度上限 **≤4 个**、名称 ≤8 字、关键词 2~5 个；
   输入格式"维度名：关键词1,关键词2,…"。
4. **LLM 精分析开启 → 所有文本全量送 LLM**（不再按 0.8 置信度阈值截留）；
   维度情感以 LLM 输出为准，不再用词典兜底。
5. **LLM 相关性复核随 LLM 自动开启**，侧边栏移除该开关（用户不再决策）。
6. **含自定义维度的任务强制全量 LLM**；在自定义维度输入处明确提醒用户；
   未配置 API Key 时降级为词典兜底，并明确提示"分析仅供参考"。
7. 报告/图表中自定义维度带 **"自定义"标记**，与预置维度区分。

## 三、路由矩阵（新口径）

| 任务形态 | 整条情感 | 维度情感 | 相关性复核 |
|---|---|---|---|
| LLM 精分析 **关**（无 Key/离线） | 词典 | 词典兜底（弱，明示"离线模式维度仅供参考"） | 不启用 |
| LLM 精分析 **开** | **全量 LLM**（不再阈值截留） | **全量 LLM**（词典仅 LLM 失败时降级） | **自动启用** |
| 含自定义维度（无论 LLM 开关） | **强制全量 LLM**（无 Key 降级+提示） | 同上，自定义维度进 LLM 维度清单 | 自动启用 |

## 四、执行清单（文件级）

### A. 路由口径（核心）

**A1 `app/coding/coder.py`（约 186 行 `need_llm`）**
- 判定改为：`llm_available and (force_llm or plan.llm_enabled or
  plan.custom_dimensions or confidence < 0.8)`。

**A2 `tests/benchmark_golden.py`（约 134 行 `direct`）**
- 混合模式（`--llm`）下 `direct=False`（全量 LLM，与生产新口径一致）；
  词典模式（回归硬门槛）不变。

**A3 `app/main.py` 侧边栏（约 979 行）**
- 移除 `relevance_check_enabled` 开关；
- 提交时 `relevance_check_enabled = llm_enabled`（自动开）；
- 确认页"LLM 相关性复核"行改为"开（随 LLM 自动开启）"；
- 费用预估（`estimate_cost`）自动带上该开关。

### B. 自定义维度（任务级）

**B1 `app/main.py` ① 品牌和分析维度确认页（约 1174 行）**
- 输入解析"维度名：关键词1,关键词2,…"，校验 ≤4 个、名称 ≤8 字、关键词 2~5 个；
- 提醒文案："自定义维度将由 LLM 判定，本任务将强制全量 LLM（费用/耗时上升）；
  未配置 API Key 时降级为词典兜底，分析仅供参考"；
- 提交逻辑：含自定义维度且有 Key → 强制 `llm_enabled=True`；
  无 Key → 保持关闭并在确认页显著提示"词典兜底模式，仅供参考"。

**B2 `app/core/models.py`（约 63 行 AnalysisPlan）**
- 新增 `custom_dimensions: list[Dimension] = []`（可选，向后兼容，
  旧任务反序列化缺省空）。

**B3 `app/core/planner.py`（约 49 行 build_plan）**
- 透传 `custom_dimensions`。

**B4 `app/core/pipeline.py`（约 905 行 schema 加载）**
- `load_domain(plan.domain_id)` 后把 `plan.custom_dimensions` 合并进 schema
  （id 加 `custom_` 前缀防冲突）；
- 未选模块时 schema = 仅自定义维度；
- 合并后的 schema 交给 Coder（LLM 维度清单 / 词典兜底 / sanitize valid_ids
  自动覆盖）。

**B5 `app/output/excel_writer.py`（约 112 行 dim_names）+ 报告链路**
- dim_names 合并 `plan.custom_dimensions`（抽 `task_schema(plan)` helper
  供 Excel/报告共用）；
- HTML/Word/图表维度中文名链路逐一核对，自定义维度显示名称 + "自定义"标记。

### C. 测试

- `test_custom_dimension_stats`：任务含自定义维度 → item.dimension_sentiments
  含 custom id、summary/报告含该维度、Excel 有列、LLM 提示词含其名称/关键词；
- `test_routing`：llm_enabled / custom_dimensions 触发全量 LLM（真分析器 stub）；
  无 LLM 时降级不崩溃；
- UI 冒烟：① 页解析 / 超 4 个拦截 / 提醒文案、侧边栏无相关性复核开关、
  确认页摘要；
- benchmark 契约：混合模式 `direct_rate=0` 断言；
- 全量回归（28+ 项）+ 词典硬门槛 PASS。

### D. 验证与费用

- 主集/边界/3C 混合各 1 轮确认不回退（当前这些域已是全量 LLM，预期无变化，
  约 ¥0.9）；
- 真实任务冒烟：demo + 自定义维度任务跑通，核对费用预估与"自定义"标记；
- 费用/耗时上升已确认接受；每次 LLM 跑分 token 照常落盘记录。

## 五、交付物

- 向导（① 页自定义维度解析/校验/提醒）、路由（全量 LLM + 自动相关性复核）、
  报告（自定义标记）全部生效；
- 文档更新：todolist 2.8（改为"已落地：任务级自定义维度统计"）、决策日志、
  执行方案文档、README 中"不参与统计"相关文案。

## 六、风险与注意

1. **need_review**：全量 LLM 后"词典直判必标"分支失效（direct=False），
   v3 的低置信/反讽/黑话信号仍生效——回归确认不报错、不漏标。
2. **费用**：LLM 全量 + 自动相关性复核使单任务费用约 +30~60%，确认页明示；
   已有费用记录机制。
3. **无 Key 降级**：自定义维度任务在无 Key 时词典兜底大量缺失维度，
   必须显著提示"仅供参考"，不静默。
4. **兼容**：`AnalysisPlan.custom_dimensions` 为可选字段，旧任务重跑不受影响。
5. **质量**：自定义维度未经验证，LLM 判定质量低于预置维度，报告标记 +
   文案提示为主要缓解。

## 七、执行顺序

1. 代码 + 测试（A→B→C）；
2. 全量回归 + 词典门槛；
3. LLM 验证轮（主集/边界/3C 各 1 轮 + demo 自定义维度任务冒烟）；
4. 文档收口 + 提交。

## 八、执行结果（2026-08-18）

- 代码 + 测试全部落地：A（coder 路由 / benchmark direct_rate=0 / 侧边栏开关移除）、
  B（parse_custom_dimensions → AnalysisPlan.custom_dimensions → task_schema 合并
  custom_ 前缀 + 「自定义」标记 → 编码/汇总/Excel 全链路；
  names.register_custom_dim_names 供图表/洞察中文名）、
  C（tests/test_custom_dimension.py 4 项 + test_ui_flow UI 冒烟；
  test_composer/test_custom_dimension 纳入 run_regression，全量 29 项绿 +
  词典硬门槛 PASS）。
- 验证：主集混合 86.86%（基线 84.0%）、边界集 88.64%（基线 89.09%）、
  3C 80.27%（基线 80.2%）——各 1 轮均在噪声内、无回退（这些域本已全量 LLM，
  预期无变化）；demo 自定义维度任务冒烟通过（summary/维度情感含 custom_*）。
- 费用：3 轮验证约 ¥0.6（token 落盘记录）；自定义维度任务费用 +30~60% 已在
  确认页明示（方案决策 6）。
- 遗留：P2 工单② display_confidence 单测仍待补（词典路径保留，无 Key 降级仍走
  词典直判封顶 0.6）。
