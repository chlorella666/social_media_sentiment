# 数码 3C 领域提案证据（2026-08-14，agent-reach/Exa 调研）

> 用途：2.5 领域扩展提案引擎首个案例的维度候选证据源（`--evidence` 输入）。
> 方法：agent-reach 集成搜索（Exa web_search_exa，中文+英文查询）+ 公开评测/投诉平台。

## 一、证据来源

1. **Trusted Reviews — Insta360 X5 评测**（https://www.trustedreviews.com/reviews/insta360-x5）：
   维度信号 = 画质（低光）、操作易用性、做工/防水（IP68）、续航（8K 掉电快）、
   可换镜头配件、重量尺寸、音频。
2. **Techarc 智能手机买家研究**（2026，9000+ 负面评论，
   https://techarc.net/techarc-identifies-areas-of-improvement-across-segments-to-elevate-customer-delight-for-smartphones-in-2026/）：
   产品可靠性是首要问题（90%+ 反馈集中于电池/性能/相机/屏幕）；电池/充电是跨品牌最高频
   不满；高端用户关注屏幕/相机/电池，中低端关注性能/电池/基础可靠性；服务类问题次要。
3. **MDPI 3C 商品网络评论可信度研究**（https://www.mdpi.com/2079-9292/13/7/1346）：
   3C 评论研究的维度框架成熟（标题/内容/态度分类），佐证 3C 口碑分析是标准研究领域。
4. **远瞻慧库 — 运动相机行业深度分析**（https://www.baogaobox.com/insights/250909000020123.html）：
   大疆用户最看重视频清晰度（17.3%）与防水性能；影石用户看重 VLOG 功能（18.46%）与
   便携性（提及率 41%）；GoPro 用户关注防抖（13.11%）与配件生态（提及率 38%）。
5. **黑猫投诉（新浪消费者服务平台）**（https://tousu.sina.com.cn/）：
   手机投诉高频词 = 机身发热、电池/快充、信号差、卡顿/死机、售后推脱/维修、
   游戏性能、翻新机/以次充好。

## 二、合成维度候选（physical 模板 + 领域特有）

| id | 候选维度（关键词） | 模板归属 | 证据 |
|---|---|---|---|
| performance | 性能表现（性能/卡顿/流畅/游戏/发热） | template:physical 功能效果 | Techarc、黑猫投诉 |
| battery | 续航充电（续航/电池/快充/充电/掉电） | template:physical 功能效果 | Techarc（跨品牌最高频）、黑猫 |
| camera | 影像拍摄（拍照/画质/防抖/视频/VLOG/清晰度） | template:physical 功能效果 | Trusted Reviews、远瞻慧库 |
| display | 屏幕显示（屏幕/屏显/刷新率/亮度） | template:physical 功能效果 | Techarc（高端关注） |
| design | 设计与做工（做工/防水/便携/重量/材质） | template:physical 质量材质 | Trusted Reviews、远瞻 |
| software | 系统与软件（系统/App/更新/生态） | domain | 黑猫（系统卡顿）、手机评测语境 |
| safety | 安全合规（充电安全/认证/翻新机/隐私） | template:physical 安全合规 | 黑猫（以次充好） |
| price_value | 价格价值（价格/性价比/促销/保值） | template:physical 价格价值 | 通用 |
| channel_service | 渠道售后（物流/售后/维修/客服/保修） | template:physical 渠道服务 | 黑猫（推脱维修）、Techarc |
| brand_image | 品牌形象（品牌/生态/配件生态/口碑） | template:physical 品牌形象 | 远瞻（配件生态）、情感价值 |
| competitor | 竞品对比（对比/替代/平替/vs） | template:physical 竞品对比 | 行业对比语境 |

## 三、模板缺口（回测输入）

- physical 模板的「功能效果」过粗：3C 需要拆出 性能/续航/影像/屏幕 四个高频独立维度，
  建议 domain 特有维度承载细分（origin=domain），模板标准维度待 2~3 个案例后定稿。
- 3C 特有的「系统与软件」不在 physical 模板，须 domain 补充。
