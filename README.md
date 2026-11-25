# 大调查 (Deep Survey)

**多源官方服务端原生检索 · X / GitHub / HN 社区热度校准 · 容错降级与上下文隔离的 Agent 深度调研引擎（CLI + Skill + Stdio MCP + Remote HTTP MCP）**

大多数 Agent 客户端自带的 `WebSearch`（客户端 Harness 检索）或开源聚合搜索 MCP 只是把浅层网页链接拼在一起：既拿不到 **X (Twitter) 实时帖子与讨论串**，也不会校准 **GitHub 仓库真实星数与停更归档状态**，多抓几个页面还会把数万 Token 脏文本灌爆主 Agent 上下文。

**大调查 (`deep-survey`)** 专为前置全面调研、技术选型、开源轮子摸底与一线踩坑核查设计（与任务途中的轻量单次小搜索互补）：**零第三方 Python 依赖（纯标准库）**，并发调度本机已登录订阅的官方服务端原生检索 CLI 与开放开发者索引，在后台完成取证、二阶段仓库核验与交叉去重，只把高信噪比的结论报告返回给主 Agent。

---

## 核心特性

1. **五路默认原生底座（零额外 API 账单）+ 六路可插拔检索 API**
   - **`codex`**：复用个人 ChatGPT 订阅（`--ignore-user-config --search`），触发 OpenAI 服务端原生多跳 `web_search` 与 `open_page`（不改动默认工作配置）。
   - **`grok`**：复用 Grok 订阅触发 xAI 服务端原生 **`x_search`**（实时推文、讨论串、作者）与网页搜索；若配置了 `XAI_API_KEY`，在 CLI 周额度耗尽（HTTP 402）或无头 VPS 环境下自动无缝故障切换至官方 API。
   - **`agy`**：复用 Antigravity 订阅触发 Google Search Grounding；若配置了 `GEMINI_API_KEY`，可自动切换至 Gemini 官方 Grounding API。
   - **`github`**：通过已登录 `gh` CLI 直查官方索引与高赞 Issues，并基于客观指标强制分级：
     - `🔥 主流高热验证` / `✅ 成熟活跃方案` / `🌱 近期高潜新秀` / `🕰️ 已归档或超1年停更` / `⚠️ 极冷门未验证玩具`（避免把几颗 Star 的废弃玩具当成熟方案推荐）。
   - **`hn`**：直查 Hacker News 官方 Algolia 索引（零 Key），按 Points 与评论数提取一线工程师真实生产反馈。
   - **可插拔外部 API**：在 `~/.config/deep-survey/config.json` 或环境变量中配置后即可接入 `perplexity`、`tavily`、`exa`、`brave`、`xai_api`、`gemini_api`。

2. **两阶段交叉核验（Wave 2 Verification）**
   - 一阶段多路并发采集完成后，二阶段自动提取各路文章提及的所有 `owner/repo`，通过 GitHub REST API 核验真实星数、最近推送日期与归档状态；`deep` 模式下还会将新发现的候选项目回传 Grok 追查 X 实时口碑。

3. **订阅到期/限额自动容错降级 + 三级汇总器故障转移**
   - 任意 1~4 路超时、未安装或订阅触顶（如 HTTP 402/429），其余存活通道仍正常完成调研；汇总器自身支持 `codex -> agy -> grok -> 离线拼接` 自动切换。

4. **主 Agent 上下文隔离 + 全场景接入**
   - 各路原始长文全部落盘保存在 `~/.cache/deep-survey/runs/<run_id>/channels/`，主对话只接收精炼 `report.md`。
   - 同时提供 **CLI + Agent Skill**（Claude Code / Codex / AGY / OMP）、**本地 Stdio MCP**（Claude Desktop）与 **远程 HTTP REST + Streamable HTTP MCP**（`deep-survey serve`，支持部署在 VPS 或通过 Tailscale / Cloudflare Tunnel 跨设备调用）。

---

## 快速安装（macOS / Linux）

```bash
git clone https://github.com/asashiki/deep-survey.git ~/.local/share/deep-survey
~/.local/share/deep-survey/install.sh
deep-survey doctor
```

---

## CLI 用法

```bash
# 1. 五路并发调研 + 仓库真实性核验 + 交叉汇总（日常首选）
deep-survey fast "调研主题、背景与重点关注维度" --out outputs/survey.md

# 2. 两阶段深度调查（含二阶段仓库核验与 X 专项口碑追查）
deep-survey deep "复杂架构选型或陌生领域全面摸底" --wait 180

# 3. 专项快查：单查 X 实时推文 / GitHub 生态热度 / HN 工程师讨论
deep-survey x "某方案在 X 上的真实开发者评价与踩坑"
deep-survey github "某方向开源轮子活跃度与高赞 Issues"
deep-survey hn "某技术在 Hacker News 上的生产事故与讨论"

# 4. 后台异步运行与状态续查
deep-survey deep "长耗时调研主题" --detach
deep-survey status latest --wait 60 --print-report
```

---

## 挂载 MCP（本地 Claude Desktop 与远程 VPS）

### 1. 本地 Stdio MCP（如 Claude Desktop）

在 `claude_desktop_config.json` 中添加：

```json
{
  "mcpServers": {
    "deep-survey": {
      "command": "/Users/<your-username>/.local/bin/deep-survey",
      "args": ["mcp"]
    }
  }
}
```

暴露工具：`deep_survey`、`ask_grok_x`、`survey_github_ecosystem`、`deep_survey_status`。

### 2. 远程 HTTP API & Streamable HTTP MCP（VPS / 跨端共享）

```bash
# 监听 0.0.0.0 时默认强制开启 Bearer Token 鉴权（未指定则自动生成至 ~/.config/deep-survey/server-token）
deep-survey serve --host 0.0.0.0 --port 8788
```

- **MCP Streamable HTTP 端点**：`POST http://<host>:8788/mcp`（Header: `Authorization: Bearer <token>`）
- **REST 提交与查询**：
  - `GET  /health`
  - `GET  /v1/doctor`
  - `POST /v1/survey`（JSON: `{"query": "...", "mode": "fast", "wait_seconds": 90}`）
  - `GET  /v1/survey/<run_id>?wait=60`

---

## 可选外部 API 扩展配置

编辑 `~/.config/deep-survey/config.json`（权限建议 `chmod 600`）：

```json
{
  "auto_enable_configured_apis": false,
  "xai_api_key": "",
  "gemini_api_key": "",
  "perplexity_api_key": "",
  "tavily_api_key": "",
  "exa_api_key": "",
  "brave_api_key": ""
}
```

- 当 `xai_api_key` 或 `gemini_api_key` 存在时，一旦本机 `grok` / `agy` CLI 未安装或周额度耗尽，会自动无缝切换至对应官方原生搜索 API。
- 设 `"auto_enable_configured_apis": true` 时，所有已填 Key 的扩展通道会自动并入 `fast` / `deep` 默认并发池；否则可通过 `--channels codex,github,hn,perplexity,exa` 按需点名调用。
