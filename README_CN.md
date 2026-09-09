# ModelPilot

开源智能 LLM 网关，支持自动模型路由、按序回退、成本估算和基于性能的 Provider 选择。

[English](README.md)

## ModelPilot 是什么？

ModelPilot 为应用提供一个 OpenAI 兼容的聊天补全入口，并将 Provider 选择保留在网关内部。
它以确定性方式排序已配置模型，在调用失败时尝试下一候选，并记录本地运行证据，使后续决策可以
使用实测延迟、可靠性和估算成本。

## V0.2 功能

- FastAPI 网关、`GET /health` 与 OpenAI 兼容的 `POST /v1/chat/completions`
- OpenAI、Gemini、DeepSeek Provider 适配器
- `model: "auto"` 路由与确定性的按序 fallback
- Provider 实测延迟、成功/失败结果和 nullable token usage
- 基于配置价格元数据和 Provider 报告 token usage 的 Decimal 成本估算
- 使用 SQLite 持久化 attempts、价格元数据与结构化路由决策
- 对延迟、可靠性和成本信号进行 confidence blending
- quality 始终使用静态配置；不声称具有 benchmark quality 数据
- 区分 Router 首选与实际服务 Provider 的结构化路由解释
- 有界、只读的 Metrics API
- 使用真实本地 metrics 的 Next.js Dashboard
- `GET /v1/logs` 提供有上限的进程内请求日志

## 技术栈

- Python、FastAPI、Pydantic、httpx 与标准库 SQLite
- Next.js、React、TypeScript
- pytest、Ruff、ESLint、GitHub Actions

## 架构

```text
客户端
  -> POST /v1/chat/completions
  -> 确定性的 confidence-blended Router
  -> OpenAI | Gemini | DeepSeek
  -> ProviderOutcome
  -> AttemptRecord + estimated cost
  -> SQLite metrics + RoutingDecision
  -> OpenAI 兼容响应 + modelpilot explanation

SQLite metrics
  -> 只读 Metrics API
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
npm run dev
```

API 和 Dashboard 默认位于 `http://localhost:8000` 与 `http://localhost:3000`。API 地址不同时，
请设置 `NEXT_PUBLIC_MODELPILOT_API_URL`。

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

Router 会排除未配置 API Key 的 Provider，为每个 Provider/模型读取一次冻结快照，应用归一化的
请求偏好，并且每个请求只排序一次。fallback 遵循这个固定顺序，不会在 attempts 之间重新排名。

- **Quality：**V0.2 始终使用配置的静态 baseline。
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
      "routing_version": "v0.2",
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

显式模型请求仍保持确定性并返回 `served_by`，但不会生成自动路由 explanation。

## Metrics 与存储

completed attempts 默认写入 `./data/modelpilot.db`。聚合对每个 Provider/模型最多使用过去 7 天内
最新的 **100 次 attempts**。这是查询窗口而不是数据库自动保留策略；V0.2 不会删除更早的持久化记录。

Provider 未返回或返回无效 usage 时，token 数保持 `null`。失败记录只保存有界且脱敏的错误类别。
当前 schema version 为 2；打开 version 1 数据库时会新增路由决策存储，同时保留 attempts 与 pricing。

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

Dashboard 并行读取 health 与四个 metrics 接口，展示 requests、attempts、成功率、平均延迟、
估算成本、Provider metrics、路由决策和最近失败。loading、empty、error、unavailable 状态互相独立；
手动 Refresh 不会启用轮询。UI 明确标注估算成本，并分别展示 selected 与 served Provider。

## 隐私

Metrics 数据库**不会**持久化 prompt、completion、API Key、Authorization header 或 Provider 原始错误体。
持久化内容仅包括 request ID、Provider/模型、时间、延迟、有界错误类别、nullable usage、成本估算和
路由元数据。Provider 凭据仅保留在进程环境中。

## 环境变量

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `MODELPILOT_CORS_ORIGINS` | Dashboard 允许的来源，多个值用逗号分隔 | `http://localhost:3000` |
| `MODELPILOT_REQUEST_LOG_LIMIT` | 进程内请求日志最大条数 | `500` |
| `MODELPILOT_METRICS_DB` | SQLite metrics 数据库路径 | `./data/modelpilot.db` |
| `OPENAI_API_KEY` | 启用 OpenAI Provider | 未设置 |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | OpenAI 地址与自动候选模型 | 官方地址 / `gpt-4o-mini` |
| `GEMINI_API_KEY` | 启用 Gemini Provider | 未设置 |
| `GEMINI_BASE_URL` / `GEMINI_MODEL` | Gemini 地址与自动候选模型 | 官方地址 / `gemini-2.0-flash` |
| `DEEPSEEK_API_KEY` | 启用 DeepSeek Provider | 未设置 |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | DeepSeek 地址与自动候选模型 | 官方地址 / `deepseek-chat` |
| `NEXT_PUBLIC_MODELPILOT_API_URL` | Dashboard 使用的后端地址 | `http://localhost:8000` |

上述变量名和默认值与 `.env.example` 一致，其中 API Key 特意留空。

## V0.2 限制

V0.2 明确不包含鉴权、多用户或多租户、streaming、billing system、benchmark engine、ML/AI Router、
Redis、PostgreSQL、分布式部署、circuit breaker 或实时价格同步。SQLite 面向单个本地 ModelPilot 实例。
Provider health 来自本地已完成 attempts，而不是分布式主动探测。

## 存储回滚

切换版本前应备份 SQLite 文件。V0.2 在打开数据库时将 schema v1 升级为 v2，并拒绝未知的更高版本；
它不提供向下迁移。应用回滚到 v0.1.0 时无需删除数据库，但 v0.1.0 不会使用 V0.2 metrics 或路由决策。

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

## 贡献

开发环境、测试和 Pull Request 规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT
