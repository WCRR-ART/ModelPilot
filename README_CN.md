# ModelPilot

开源智能 LLM 网关，支持自动模型路由、按序回退、成本估算和基于性能的 Provider 选择。

[English](README.md)

## ModelPilot 是什么？

ModelPilot 为应用提供一个 OpenAI 兼容的聊天补全入口，并将 Provider 选择保留在网关内部。
它以确定性方式排序已配置模型，在调用失败时尝试下一候选，并记录本地运行证据，使后续决策可以
使用实测延迟、可靠性和估算成本。

## V0.3 功能

当前正式稳定版本为 **0.3.0**，已发布：
[ModelPilot v0.3.0](https://github.com/WCRR-ART/ModelPilot/releases/tag/v0.3.0)。

### Gateway

- FastAPI 网关、`GET /health` 与 OpenAI 兼容的 `POST /v1/chat/completions`
- OpenAI、Gemini、DeepSeek Provider 适配器
- `model: "auto"` 路由与确定性的按序 fallback

### Routing
- Provider 实测延迟、成功/失败结果和 nullable token usage
- 基于配置价格元数据和 Provider 报告 token usage 的 Decimal 成本估算
- 使用 SQLite 持久化 attempts、价格元数据与结构化路由决策
- 对延迟、可靠性和成本信号进行 confidence blending
- quality 始终使用静态配置；不声称具有 benchmark quality 数据
- 区分 Router 首选与实际服务 Provider 的结构化路由解释

### Observability
- 有界、只读的 Metrics API
- 使用真实本地 metrics 的 Next.js Dashboard
- `GET /v1/logs` 提供有上限的进程内请求日志

### Provider Health

- 按 Provider/模型持久化 CLOSED / OPEN / HALF_OPEN 状态
- 可配置连续失败阈值和冷却时间，在 auto 评分前隔离 OPEN 候选
- 单进程内只允许一个恢复探测并发执行，成功后自动恢复
- 包含健康依据及未评分排除项的结构化路由解释
- 只读 Provider Health API 与 Dashboard 健康状态展示

## 技术栈

- Python、FastAPI、Pydantic、httpx 与标准库 SQLite
- Next.js、React、TypeScript
- pytest、Ruff、ESLint、GitHub Actions

## 架构

```text
客户端
  -> POST /v1/chat/completions
  -> circuit eligibility -> 确定性的 confidence-blended Router
  -> OpenAI | Gemini | DeepSeek
  -> ProviderOutcome
  -> AttemptRecord + estimated cost
  -> SQLite metrics + RoutingDecision + ProviderHealth
  -> OpenAI 兼容响应 + modelpilot explanation

SQLite metrics + health / 进程内 probe 状态
  -> 只读 Metrics 与 Provider Health API
  -> Next.js Dashboard
```

Provider HTTP 转换集中在 `backend/src/modelpilot/providers/`。metrics 写入失败会记录 warning，
但不会把已经成功的 Provider 响应变成失败的推理请求。

## 快速开始

复制环境变量模板，只为需要启用的 Provider 填写密钥。不要提交填写后的 `.env` 文件。

```bash
cp .env.example .env
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
python -m uvicorn modelpilot.main:app --reload --env-file ../.env
```

另开终端启动 Dashboard：

```bash
cd frontend
npm ci
export NEXT_PUBLIC_MODELPILOT_API_URL=http://localhost:8000
# PowerShell: $env:NEXT_PUBLIC_MODELPILOT_API_URL="http://localhost:8000"
npm run dev
```

API 和 Dashboard 默认位于 `http://localhost:8000` 与 `http://localhost:3000`。API 地址不同时，
请设置 `NEXT_PUBLIC_MODELPILOT_API_URL`。Next.js 不读取仓库根目录的 `.env`，
需要像上例一样导出该公开 URL，或仅将这一项写入 `frontend/.env.local`。
未设置时前端使用同源 API 路径，需要自行配置反向代理。该 URL 在构建时注入，不能包含凭据。

## API 示例

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello"}]}'
```

路由偏好为可选项；省略时使用均衡默认值。

```json
{
  "model": "auto",
  "messages": [{"role": "user", "content": "Summarize this."}],
  "modelpilot": {
    "preferences": {"quality": 0.4, "cost": 0.3, "latency": 0.2, "reliability": 0.1}
  }
}
```

## 路由如何工作

```text
Cold start
  -> static baseline
  -> 收集 completed attempts
  -> recent measured metrics
  -> confidence blending
  -> deterministic dynamic ranking
```

auto Router 会排除未配置 API Key 的 Provider 和 OPEN circuit，为每个 Provider/模型读取快照，应用归一化的
请求偏好，并且每个请求只排序一次。fallback 遵循固定顺序，不会在 attempts 之间重新排名。
调用前会再次核对健康资格；HALF_OPEN 候选必须先取得进程内 probe lease。

- **Quality：**始终使用配置的静态 baseline。
- **Latency：**数据可用时，将静态 baseline 与实测 p50 延迟混合。
- **Reliability：**将静态 baseline 与经过平滑处理的实测成功率混合。
- **Cost：**存在有价格样本时，将静态 baseline 与 p50 请求估算成本混合。

缺失的维度继续使用静态 baseline。这是确定性的运行数据路由，不是 ML Router，也不是 Provider benchmark。

### 路由置信度

completed attempt 信号与有价格成本样本分别计算 confidence：

- 少于 5 个样本：只使用静态信号
- 5–49 个样本：逐步引入实测数据
- 50 个及以上样本：实测信号达到完整置信度

该渐进机制可避免少量请求或单次失败立即主导路由。

### 路由解释

自动路由成功响应保留 OpenAI-compatible 核心字段，并增加 `modelpilot` 扩展。
`selected_provider` 是 Router 第一选择，`served_provider` 是经过 fallback 后实际返回响应的 Provider。

```json
{
  "modelpilot": {
    "request_id": "req_...",
    "served_by": {"provider": "gemini", "model": "gemini-2.0-flash"},
    "routing": {
      "routing_version": "v0.3",
      "selected_provider": "deepseek",
      "selected_model": "deepseek-chat",
      "served_provider": "gemini",
      "served_model": "gemini-2.0-flash",
      "selected": {
        "final_score": 0.88,
        "measured_sample_count": 12,
        "measured_confidence": 0.155556,
        "sources": {
          "quality": "configured",
          "latency": "blended",
          "reliability": "blended",
          "cost": "static_unavailable"
        }
      }
    }
  }
}
```

显式模型请求仍保持确定性并返回 `served_by`，但不会生成自动路由 explanation，
不进行 auto 健康过滤，也不会偷偷换 Provider。上例是简化结构，不是实测 benchmark。
V0.3 候选还包含 `health` 依据和 `excluded_candidates`，被排除项没有 rank 或 score。

## Circuit Breaker 工作原理

```text
CLOSED -> 计数失败达到阈值 -> OPEN
OPEN -> 冷却结束 -> HALF_OPEN（具备恢复探测资格）
HALF_OPEN -> 探测成功 -> CLOSED
HALF_OPEN -> 计数类探测失败 -> OPEN（重新冷却）
```

默认阈值为 **连续 3 次计数失败**，冷却时间为 **60 秒**，均可通过环境变量配置。
计入的错误：`timeout`、`connection_error`、`rate_limit`、`provider_error`、
`invalid_response`、`unknown_error`。`authentication_error` 仍记录到 attempt，便于观察，
但不增加 circuit failure count，也不会因此打开 circuit。成功调用会重置连续失败数。

auto 路由跳过 OPEN 候选，没有可用候选时返回既有的 503 unavailable。
冷却结束后，由新请求触发恢复探测；同一 Provider/模型只允许一个 probe 在执行，
其他请求跳过它并尝试健康候选。没有后台主动健康检查。

**探测协调仅限单进程。** 多 workers 或多实例之间不共享 probe lease，
这不是分布式 circuit breaker。需要单探测保证时应使用单进程部署。
SQLite 保存的健康状态在重启后保留，进程内 lease 不持久化。
健康数据读取失败时，推理采取 fail-open 并记录脱敏 warning；attempt 与 health 写入故障互相隔离，
不会将已成功的推理变成失败响应。

## 只读 Provider Health API

`GET /v1/health/providers` 返回已配置 Provider/模型的当前有效状态、eligibility、reason、
连续失败数、冷却时间、最近成功/失败时间与 `probe_in_flight`。
它不同于检查服务在线状态的 `GET /health`。已到期的 OPEN 记录展示为有效 HALF_OPEN，
但 GET 不写数据库、不领取 probe。无历史记录时使用 CLOSED / health_unknown；未启用 Provider 不展示。
时间为 UTC，缺失数据保持 null。Store 故障返回 503，不伪造健康状态；查询快照不保证后续请求获得资格。
不提供 reset、手动 open/close 或手动 probe 接口。

## Metrics 与存储

completed attempts 默认写入 `./data/modelpilot.db`。聚合对每个 Provider/模型最多使用过去 7 天内
最新的 **100 次 attempts**。这是查询窗口而不是数据库自动保留策略；不会自动删除更早的持久化记录。

Provider 未返回或返回无效 usage 时，token 数保持 `null`。失败记录只保存有界且脱敏的错误类别。
当前 schema version 为 3，支持 v1 经 v2 升至 v3，以及 v2 升至 v3。
新增 provider health 表，同时保留 attempts、pricing 和 routing decisions。
缺少 health 字段的旧 V0.2 explanation JSON 仍可读取。SQLite 使用 WAL，自动创建存储目录；
相对路径以启动后端的工作目录为基准。

### 估算成本

只有两个输入都存在时才生成估算：

```text
配置的每百万 token 价格元数据
  + Provider 实际报告的输入/输出 token
  = estimated request cost
```

默认服务不内置价格目录，也不执行实时价格同步。价格是通过 `MetricsStore` 提供的配置元数据。
已保存的 Decimal 估算不会因后续价格元数据变化而重算。它是估算值，不是 Provider 实际账单。

## 只读 Metrics API

- `GET /v1/metrics/summary?hours=24`：distinct 请求、attempt、结果、延迟、估算成本、Provider、
  模型与路由决策；`hours` 限制为 1–168
- `GET /v1/metrics/providers`：最近 7 天/100 attempts 的 Provider-模型快照，支持精确的
  `provider` 与 `model` 过滤
- `GET /v1/metrics/routing-decisions?limit=20`：已持久化的结构化路由依据
- `GET /v1/metrics/failures?limit=20`：最近的脱敏失败类别

路由决策和失败记录的 limit 上限均为 100。Decimal 成本以精确 JSON 字符串返回，不可用值保持
`null`。不提供 metrics 或 pricing 的 HTTP 写接口。

## Dashboard

Dashboard 并行读取服务 health、Provider Health 与四个 metrics 接口，展示 requests、attempts、成功率、平均延迟、
估算成本、Provider metrics、路由决策和最近失败。loading、empty、error、unavailable 状态互相独立；
手动 Refresh 不会启用轮询。UI 明确标注估算成本，并分别展示 selected 与 served Provider。
Provider Health 展示 Healthy / Open / Recovering、连续失败数、UTC 冷却截止时间、探测准备或进行状态，
以及最近成功/失败。无历史记录时明确提示，null 时间显示破折号。
同一个 Refresh 同时刷新 metrics 和 health；页面没有状态修改按钮。

## 隐私

Metrics 与 health 数据库**不会**持久化 prompt、completion、API Key、Authorization header 或 Provider 原始错误体。
持久化内容仅包括 request ID、Provider/模型、时间、延迟、有界错误类别、nullable usage、成本估算和
路由和健康元数据。Provider 凭据仅保留在进程环境中；路由与健康解释不暴露凭据或 secret。

## 环境变量

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `MODELPILOT_CORS_ORIGINS` | Dashboard 允许的来源，多个值用逗号分隔 | `http://localhost:3000` |
| `MODELPILOT_REQUEST_LOG_LIMIT` | 进程内请求日志最大条数 | `500` |
| `MODELPILOT_METRICS_DB` | SQLite metrics 数据库路径 | `./data/modelpilot.db` |
| `MODELPILOT_CIRCUIT_FAILURE_THRESHOLD` | 打开 circuit 的连续计数失败阈值；整数 >= 1 | `3` |
| `MODELPILOT_CIRCUIT_COOLDOWN_SECONDS` | 冷却秒数；整数 >= 0 | `60` |
| `OPENAI_API_KEY` | 启用 OpenAI Provider | 未设置 |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | OpenAI 地址与自动候选模型 | 官方地址 / `gpt-4o-mini` |
| `GEMINI_API_KEY` | 启用 Gemini Provider | 未设置 |
| `GEMINI_BASE_URL` / `GEMINI_MODEL` | Gemini 地址与自动候选模型 | 官方地址 / `gemini-2.0-flash` |
| `DEEPSEEK_API_KEY` | 启用 DeepSeek Provider | 未设置 |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | DeepSeek 地址与自动候选模型 | 官方地址 / `deepseek-chat` |
| `NEXT_PUBLIC_MODELPILOT_API_URL` | Dashboard 使用的后端地址 | 模板：`http://localhost:8000`；未设置：同源 |

上述变量名和默认值与 `.env.example` 一致，其中 API Key 特意留空。

健康记录使用每次标准化 ProviderOutcome 的 UTC 完成时间更新。
非法 circuit 配置在加载阶段报错。健康记录与 attempt 记录独立隔离故障，写入失败不改变成功响应。
auto Router 已过滤 OPEN 状态；已有 OPEN/冷却期或时间顺序规则拒绝转换时，保留健康状态并记录 warning，
attempt metrics 仍然照常记录。

## V0.3 限制

V0.3 不包含分布式 circuit breaker、多进程 probe 协调、手动 circuit 控制、circuit 事件历史、
后台健康检查、鉴权、多用户/多租户、计费支付、streaming、benchmark engine、ML/AI Router、
Redis、PostgreSQL、新增 Provider 扩展或实时价格同步。SQLite 面向单个本地 ModelPilot 实例。

## 存储回滚

升级前停止应用，备份 SQLite（包括尚未合并的 WAL 数据）。V0.3 将 schema v1/v2 升级到 v3，
拒绝未知的更高版本，不提供向下迁移。回滚 V0.2 时需恢复升级前备份，V0.2 无法打开 schema-v3 数据库。

## 验证

```bash
cd backend
python -m pytest --tb=short
python -m ruff check .

cd ../frontend
npm ci
npm run lint
npm run typecheck
npm run build
npm audit
```

## V0.4 开发版：Benchmark

当前开发分支提供只读 Benchmark HTTP 查询与显式本地 CLI 执行（可能产生 Provider 费用）。
命令、配置和退出码见 [Benchmark 使用说明](docs/v0.4/BENCHMARKS.md)。开发版使用 schema 4；
升级前请备份数据库，回退 v0.3.0 时须恢复备份。不提供 HTTP 执行入口或 Benchmark Dashboard。

## 贡献

开发环境、测试和 Pull Request 规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT
