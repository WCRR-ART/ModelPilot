# ModelPilot

开源智能 LLM 网关，支持自动模型路由、Provider 回退、成本优化和基于性能的模型选择。

> V0.1 专注于可运行的最小基础：进程内确定性路由和内存请求日志。当前版本不包含持久化、鉴权、计费、流式输出或分布式健康检查。

[English](README.md)

## 为什么需要 ModelPilot

应用直接绑定单一模型时，也会同时绑定该模型的故障、延迟和价格特征。ModelPilot 提供一个 OpenAI 兼容入口，对已配置模型进行评分，并在自动路由失败时尝试下一个 Provider。

## V0.1 功能

- FastAPI 服务与 `GET /health`
- OpenAI 兼容的 `POST /v1/chat/completions`
- OpenAI、Gemini、DeepSeek Provider 适配器
- `model: "auto"` 自动选择入口
- 基于质量、成本、延迟、可靠性的加权评分
- 按评分顺序执行 Provider fallback
- `GET /v1/logs` 返回有上限的内存结构化请求日志
- 响应式 Next.js Dashboard

## 技术栈

- Python、FastAPI、Pydantic、httpx
- Next.js、React、TypeScript
- pytest、Ruff、ESLint
- GitHub Actions

## 架构

```text
客户端
  -> FastAPI /v1/chat/completions
  -> 确定性的加权路由器
  -> OpenAI | Gemini | DeepSeek 适配器
  -> OpenAI 兼容响应
  -> 有上限的内存请求日志
```

Provider HTTP 转换集中在 `backend/src/modelpilot/providers/`。V0.1 使用静态基础评分，不采集实时 benchmark。

## 快速开始

复制 `.env.example` 为 `.env`，只填写需要启用的 Provider 密钥。未配置密钥的 Provider 会自动禁用。

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

API 默认地址为 `http://localhost:8000`，Dashboard 默认地址为 `http://localhost:3000`。
Dashboard 通过只读 Metrics API 展示摘要、Provider metrics、最近路由决策和最近失败；
各区域分别显示 loading、empty、unavailable 和 error 状态，单个接口失败不会遮蔽其他区域。

在 V0.2 开发分支中，已完成的 Provider 尝试默认写入 `./data/modelpilot.db`。可通过
`MODELPILOT_METRICS_DB` 修改路径，父目录会自动创建。数据库不会保存提示词、补全内容或凭据。
当存在精确匹配的配置价格和真实输入/输出 token usage 时，ModelPilot 会保存 Decimal 成本估算。
该值不是 Provider 账单；缺少任一输入时成本保持不可用。

## `model: "auto"` 的含义

自动路由会过滤未配置 API Key 的 Provider，按照 quality、cost、latency、reliability 的权重计算分数，再从高到低尝试候选模型。某次请求失败时会继续尝试下一候选。V0.1 的分数是静态归一化估计，不是 benchmark 数据。

## ModelPilot 响应扩展

在 V0.2 开发分支中，自动路由成功响应继续保留 OpenAI-compatible 的 `id`、`object`、
`model`、`choices` 和 `usage` 字段，并在 ModelPilot 专属的顶层 `modelpilot` 扩展中返回路由依据：

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
      "candidates": [
        {
          "provider": "deepseek",
          "model": "deepseek-chat",
          "rank": 1,
          "selected": true,
          "final_score": 0.88,
          "sources": {
            "quality": "configured",
            "latency": "blended",
            "reliability": "blended",
            "cost": "static_unavailable"
          }
        }
      ]
    }
  }
}
```

selected candidate 表示 Router 的第一选择；`served_by` 表示经过 fallback 后实际返回响应的
Provider。分数是内部路由依据，来自配置的 baseline 与当前 ModelPilot 实例采集的本地 metrics，
不是实时 Provider benchmark。显式模型请求只返回 `served_by`，不会伪造自动路由 explanation。

## 只读 Metrics API

V0.2 开发分支通过四个只读接口提供本地持久化 metrics：

- `GET /v1/metrics/summary?hours=24`：distinct 请求、attempt、成功/失败、延迟、估算成本、
  Provider、模型与路由决策汇总；窗口 hours 限制为 1–168
- `GET /v1/metrics/providers`：最近七天且最多 100 次 attempt 的 Provider/模型快照；
  支持精确的 `provider` 和 `model` 可选过滤
- `GET /v1/metrics/routing-decisions?limit=20`：已持久化的结构化路由依据
- `GET /v1/metrics/failures?limit=20`：最近的标准化失败类别

决策和失败记录的 limit 上限均为 100。Decimal 成本以无精度损失的定点字符串返回，
不可用时保持 `null`。成本来自配置价格与 Provider 报告的真实 usage，是估算值而非账单。
这些接口不会返回 prompt、completion、凭据、Authorization header 或 Provider 原始错误信息，
也不提供 metrics 或 pricing 写接口。

## 环境变量

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `MODELPILOT_CORS_ORIGINS` | Dashboard 允许的来源，多个值用逗号分隔 | `http://localhost:3000` |
| `MODELPILOT_REQUEST_LOG_LIMIT` | 内存请求日志最大条数 | `500` |
| `MODELPILOT_METRICS_DB` | Provider 尝试记录使用的 SQLite 文件 | `./data/modelpilot.db` |
| `OPENAI_API_KEY` | 启用 OpenAI Provider | 未设置 |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | OpenAI 地址与自动路由模型 | 官方地址 / `gpt-4o-mini` |
| `GEMINI_API_KEY` | 启用 Gemini Provider | 未设置 |
| `GEMINI_BASE_URL` / `GEMINI_MODEL` | Gemini 地址与自动路由模型 | 官方地址 / `gemini-2.0-flash` |
| `DEEPSEEK_API_KEY` | 启用 DeepSeek Provider | 未设置 |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | DeepSeek 地址与自动路由模型 | 官方地址 / `deepseek-chat` |
| `NEXT_PUBLIC_MODELPILOT_API_URL` | Dashboard 使用的后端地址 | `http://localhost:8000` |

不要提交填写过真实密钥的 `.env` 文件。

## 验证

```bash
cd backend
python -m pytest
python -m ruff check .

cd ../frontend
npm run lint
npm run typecheck
npm run build
```

## 版本边界

V0.1 只支持非流式聊天补全、静态路由评分和进程内请求日志。它不包含数据库、用户系统、鉴权、计费、动态指标采集、流式代理或管理写接口。

## Roadmap

V0.1 先稳定网关协议与 Provider 边界。后续可独立评估动态指标、持久化日志、访问控制和流式输出，但这些能力当前均未实现，也未承诺具体版本。

## 贡献

开发环境、测试和 Pull Request 规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT
