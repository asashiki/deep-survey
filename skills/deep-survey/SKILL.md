---
name: deep-survey
description: 大调查（Deep Survey）— 多源官方原生检索与深度聚合调研技能（替代单路联网调研与客户端 Harness 浅层搜索）。并发调度已登录订阅的 Codex（OpenAI 服务端多跳 web_search）、Grok（xAI 原生 X/Twitter 推文与讨论串搜索）、Antigravity（Google Search Grounding）、GitHub 官方索引（Star 热度与停更/低星玩具过滤校准）及 Hacker News 官方高赞讨论，支持扩展外部检索 API 与远程 MCP，落盘隔离长文并输出交叉核验报告。适用于前置全面调研、技术选型、开源轮子摸底、X 实时舆情与真实踩坑核查。
---

# 大调查（Deep Survey）

将任务途中的“顺手小查”与“前置深度大调查”分层：
- **小检索（不调用本技能）**：已知明确网址、查单个 API 参数或字面报错，直接用当前 Agent Harness 自带的轻量网页搜索。
- **深度大调查（使用本技能）**：新任务摸底、技术/架构选型、找开源方案、查 X (Twitter) / Hacker News 实时开发者口碑与踩坑、跨源事实核查，直接运行 `deep-survey`，避免单视角 Harness 搜索信息残缺或把大量网页原文塞爆主对话上下文。

## 官方原生底座与容错机制

入口程序：`~/.local/bin/deep-survey`（默认纯走已登录订阅与官方开放索引，**不消耗个人 OpenAI 按量 API 余额**，也不修改 `~/.codex/config.toml` 默认工作配置）：

1. **`codex`**：复用个人 ChatGPT 订阅（`--ignore-user-config --search`），触发 OpenAI 官方服务端多跳 `web_search` 与 `open_page`。
2. **`grok`**：复用 Grok 订阅，触发 xAI 服务端原生 **X 搜索**（实时推文、讨论串、作者）与网页检索（若配了 `XAI_API_KEY`，CLI 限额或无头环境下自动无缝切官方 API）。
3. **`agy`**：复用 Antigravity 订阅，触发 Google Search Grounding（若配了 `GEMINI_API_KEY`，无头或失败时自动切官方 Gemini Grounding API）。
4. **`github`**：复用已登录 `gh` CLI 直查官方索引，按真实 Star 数、创建时间、最近推送日期和归档状态自动分级（区分主流成熟高 Star、近期高潜新秀、高星停更老项目与极冷门未验证玩具）。
5. **`hn`**：直查 Hacker News 官方 Algolia 索引，按 Points 与评论数校准一线工程师真实战报与吐槽（零 Key 消耗）。
6. **可插拔扩展 API**：在 `~/.config/deep-survey/config.json` 配置后可按需启用 `perplexity`、`tavily`、`exa`、`brave`、`xai_api`、`gemini_api`。

**自动容错降级**：任一订阅到期、限额或超时不会中断任务；只要至少一路存活即可产出汇总报告，并在报告头部如实列出各通道状态与原生检索次数。

## 调用档位

按问题复杂度选档，优先把汇总报告写入当前任务目录或读取返回的 `report_file`：

```bash
# 1. 标准五路并发调研 + 仓库真实性核验 + 交叉汇总（日常首选）
~/.local/bin/deep-survey fast "调研主题、背景与重点关注维度" --out outputs/survey.md

# 2. 两阶段深度调查：五路并发 + 二阶段仓库真实性审计与 X 口碑追查
~/.local/bin/deep-survey deep "复杂技术选型或陌生领域全面摸底要求" --out outputs/deep-survey.md --wait 180

# 3. 单点专项快查：只查 X 实时推文、GitHub 生态热度或 HN 工程师讨论
~/.local/bin/deep-survey x "某工具/模型在 X 上的真实开发者反馈与踩坑"
~/.local/bin/deep-survey github "某方向的开源实现对比与维护活跃度"
~/.local/bin/deep-survey hn "某技术在 Hacker News 上的生产踩坑讨论"
```

## 后台续查与上下文隔离

Codex 首轮检索默认最多运行 10 分钟（`fast`）或 20 分钟（`deep`）。可用 `--timeout 秒数` 覆盖本次一阶段检索的时限。

- 若长任务超过 `--wait` 秒数仍在后台运行，命令会返回 `run_id`（不会丢失已完成的通道结果），继续等待同一 `run_id`，不要重复发起新任务：

  ```bash
  ~/.local/bin/deep-survey status <run_id> --wait 60
  ```

- **保护主 Agent 上下文**：先读汇总后的 `report.md`。仅在需要深挖某条具体细节时，再按报告头部的路径去读 `channels/*.md` 分报告，不要把各路原始长文一次性全塞进上下文。
- **核验原则**：工具返回成功不替代证据核查；关注报告顶部的通道执行状态（如原生搜索次数、X 帖子链接数、GitHub 真实星数与最近推送日期），不要把几颗 Star 且无外部印证的冷门仓库当作主流成熟方案推荐给用户。
- 诊断各底座就绪状态：`~/.local/bin/deep-survey doctor`。
