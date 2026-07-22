# 🧠 Thalamus — 丘脑路由中枢

> *"每一条信号，都通向正确的皮层。"*

| **Version** | **Status** | **License** | **Python** | **CI** |
|-------------|------------|-------------|------------|--------|
| v4.1.0 | Stable | MIT | 3.10+ | [![CI](https://github.com/sixgodgit/thalamus/actions/workflows/test.yml/badge.svg)](https://github.com/sixgodgit/thalamus/actions/workflows/test.yml) |

[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://python.org)
[![Test Coverage](https://img.shields.io/badge/tests-52%20passed-brightgreen)](tests/)

---

## 家族体系

| 项目 | 语源 | 角色 |
|------|------|------|
| **Thalamus** | 神经科学 | 路由中枢 — 决定谁来做 |
| **Hypnos** | 希腊神话 | 梦境进化 — 夜间认知循环 |
| **Nyx** | 希腊神话 | 记忆感知 — 边缘意识 |
| **Canon** | 拉丁语 | 技能生态 — 什么值得留 |

---

## 概述

Thalamus 是一个**零依赖、纯 Python stdlib** 的智能模型路由中枢，以大脑丘脑的信息中继机制为灵感，将每一条请求自动导向最合适的推理后端。它不是简单的负载均衡器，而是一个具备**多层级联路由、上下文感知分类、进化学习能力**的完整决策系统。

在架构设计上，Thalamus 采用**前额叶规则引擎 → 皮层语义分类 → 脑干默认兜底**的三层级联策略，确保每一次路由都在 **<1ms** 内完成初判，并在失败时沿同类能力链优雅降级。配合 **Per-Route 熔断器、Token-Bucket 限流、主动健康探测**，它能在生产环境中实现自愈与稳态运行。

作为家族体系的**路由核心**，Thalamus 与 Hypnos（夜间认知进化）、Nyx（记忆感知）、Canon（技能生态）协同工作，共同构成完整的智能体基础设施。

---

## 核心特性

| 特性 | 说明 |
|------|------|
| 🖥️ **Web Admin Panel** | 实时仪表盘：路由、密钥、日志、Token 计数器、在线配置编辑 |
| 🔄 **OpenAI-Compatible Proxy** | 完整 `/v1/chat/completions` 兼容，支持流式 + 非流式 |
| 🛡️ **Per-Route Fallback Chains** | 每条路由自带失败链 — 同类能力内降级，而非直接打到默认 |
| 🔗 **Circuit Breaker + Fallback** | 自动故障检测、半开探测、自恢复 — 感知失败链 |
| ⚡ **Streaming SSE** | 原生 `text/event-stream` 透传，零缓冲 |
| 🔧 **Tool Calls Passthrough** | 原始透传 — 不解析、不修改、不丢字段 |
| 🧮 **Multi-Model Parallelism** | `/parallel` 端点同时派发至 3+ 模型 |
| 🧬 **Evolutionary Learning** | `/evolution` 引擎追踪路由决策，持续自优化 |
| 📊 **Real-Time Observability** | `/stats` + `/cost-performance`：按标签/提供方统计 Token、延迟、成本、降级率 |
| 🔢 **Token Counting** | 按标签、按提供方追踪 Token，支持 `/stats/reset` 会话级测量 |
| 🔑 **Login Persistence** | Cookie + localStorage Token 持久化 — 无需重复登录 |
| 🔌 **Zero Dependencies** | 纯 Python stdlib — 无 `pip install`，无 virtualenv，无容器 |
| 🔁 **Circuit Breaker** | 按路由自动故障检测、半开探测、自恢复 |
| 🏷️ **Per-IP Rate Limiting** | Token-Bucket 限流器：60 req/min、最大 10 并发、突发支持 |
| 💓 **Active Health Probing** | 后台 60s 周期探测所有路由端点 |
| 🧠 **Context-Aware Routing** | 多轮对话历史参与路由分类决策 |

---

## 架构

```
User Request
     │
     ▼
┌─────────────────────────────────┐
│         Thalamus Router         │
│                                 │
│  ┌───────────────────────────┐  │
│  │  🧠 Prefrontal Layer      │  │  ← 规则引擎：正则模式匹配，<1ms
│  │  (Rule Engine)            │  │
│  └────────────┬──────────────┘  │
│               │ match?          │
│          yes  │  no             │
│               ▼                 │
│  ┌───────────────────────────┐  │
│  │  🧠 Cortex Layer          │  │  ← 语义分类：TF-IDF / 向量
│  │  (Semantic Classifier)    │  │
│  └────────────┬──────────────┘  │
│               │ match?          │
│          yes  │  no             │
│               ▼                 │
│  ┌───────────────────────────┐  │
│  │  🧠 Brainstem Layer       │  │  ← 默认兜底：始终可用，~2s
│  │  (Default Fallback)       │  │
│  └────────────┬──────────────┘  │
│               │                 │
└───────────────┼─────────────────┘
                │
                ▼
        Target Model API
                │
                ▼
        Response to User
```

每一层采用**瀑布策略 — 首次匹配胜出**。规则引擎未命中则传至语义分类，再未命中则落至脑干默认层，确保**可预测的路由与优雅降级**。

| 层级 | 功能 | 延迟 |
|------|------|------|
| 🧠 **Prefrontal** | 规则引擎 — 正则模式匹配，零成本首判 | **<1ms** |
| 🧠 **Cortex** | 深度推理、分析、代码生成 | **varies** |
| 🧠 **Brainstem** | 默认兜底 — 始终可用，始终快速 | **~2s** |

---

## 快速开始

### 前置条件

- Python 3.8+
- 所选推理提供方的 API Keys

### 安装

```bash
git clone https://github.com/sixgodgit/thalamus.git
cd thalamus
```

### 配置

创建 `keys.json` 并填入提供方凭证：

```json
{
  "provider_alias": {
    "key": "***",
    "endpoint": "https://api.provider.com/v1/chat/completions"
  }
}
```

### 运行

```bash
python3 thalamus.py
```

### 验证

```bash
curl http://127.0.0.1:9880/health
```

---

## 路由规则

路由规则在 `routes.json` 中完全声明式定义。系统采用**瀑布策略 — 首次匹配胜出**：

| 优先级 | 能力域 | 匹配触发词 |
|--------|--------|-----------|
| 🥇 | **Code & Engineering** | `code`, `deploy`, `debug`, `git`, `docker`, `api`, `python`, `error` — 60+ 正则模式 |
| 🥈 | **Analysis & Reasoning** | `analyze`, `compare`, `why`, `root cause`, `strategy`, `architecture`, `review` |
| 🥉 | **Vision & Multimodal** | `image`, `screenshot`, `diagram`, `vision`, `OCR`, `chart` |
| 🏁 | **Default (Catch-all)** | 任何未匹配输入 |

### Fallback Chains (v4.1.0)

每条路由可定义自己的失败链，确保同类能力内失败时降级至相似能力模型，而非直接打到默认：

```json
{
  "label": "Claude Sonnet 5",
  "model": "claude-sonnet-5",
  "fallbacks": [
    {"model": "gpt-4o-mini", "provider": "...", "key_env": "...", "endpoint": "..."},
    {"model": "deepseek-v4-flash", "provider": "deepseek", ...}
  ]
}
```

> 规则支持热重载：编辑 `routes.json` 后通过 Admin Panel 或 `POST /admin/api/reload` 触发重载。

### Context-Aware Classification

与朴素的单消息路由器不同，Thalamus 评估**最近 5 条用户消息**，并加权强调最新一条。这使得诸如 "continue debugging" 或 "same approach for the other module" 这类后续追问能被正确路由 — 这类场景中，单独的最后一条消息携带的信号不足。

### Pattern 技巧

- 正则模式中使用 **管道符 `|`** 作为关键词分隔符（逗号 `,` 被视为普通字符）
- 较短、关键词较少的模式更可预测
- 语义分类作为正则未命中时的二级兜底

---

## API 参考

**Base URL**: `http://127.0.0.1:9880`

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | Web Admin Panel |
| `/v1/chat/completions` | POST | OpenAI 兼容代理（流式 + 非流式） |
| `/task` | POST | 旧版单路由任务派发 |
| `/parallel` | POST | 并行多模型派发，结果聚合 |
| `/analysis` | POST | 多视角深度分析 |
| `/evolution` | GET | 进化学习状态 |
| `/health` | GET | 健康检查 + 运行时状态 |
| `/stats` | GET | 完整可观测性：Token、调用、延迟、成本、熔断器状态 |
| `/stats/reset` | GET | 重置 Token 计数器（返回之前快照） |
| `/cost-performance` | GET | 按路由成本与延迟分析，含 Token 分解 |
| `/admin/*` | GET/POST | 管理操作：配置、密钥、日志、余额 |

---

## 可观测性

Thalamus 开箱即提供多维度可观测性：

```
/health             → 状态、路由、运行时间、错误计数
/stats              → 完整指标：Token、调用、延迟、成本
/stats/reset        → 会话中途重置计数器，实现任务级测量
/cost-performance   → 按路由成本分析，提供方级别分解
/logs               → 颜色编码：ERROR (红)、FALLBACK (黄)、ROUTE (蓝)、CALL (绿)
```

### Token Counting

按路由和按提供方追踪 Token 用量：

```bash
# 查看累计统计
curl http://127.0.0.1:9880/stats

# 特定任务前重置
curl http://127.0.0.1:9880/stats/reset

# 任务后查看 Token 分解
curl http://127.0.0.1:9880/stats | jq '.total_tokens, .token_by_label, .token_by_provider'
```

### Health Probe 输出示例

```
code-engine   ✅  1.07s
analysis      ✅  2.92s
vision        ✅  4.19s
default       ✅  5.26s
```

主动健康探测每 60 秒对所有配置路由运行一次，结果暴露在 `/stats` 中供监控集成。

---

## 限流与熔断

### Rate Limiting

Thalamus 对每个客户端 IP 使用 **Token-Bucket** 限流器：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_requests_per_window` | 300 | 每 60s 窗口最大请求数 |
| `window_seconds` | 60 | 窗口时长 |
| `max_concurrent` | 30 | 每 IP 最大并发请求数 |
| `burst_tokens` | 50 | 突发容量（每秒自动补充 1 个） |

限流请求返回 **HTTP 429** 及原因字符串（`burst`、`window` 或 `concurrent`）。指标通过 `/stats` 和 `/metrics` 暴露。

### Circuit Breaker

按路由熔断器，带自动半开探测：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `consecutive_fail_threshold` | 3 | 连续失败次数后熔断器打开 |
| `half_open_interval` | 60s | 等待探测请求的时间 |
| `recover_success_count` | 1 | 探测成功即关闭熔断器 |

状态流转：**Closed**（正常）→ **Open**（故障，请求跳至 Fallback）→ **Half-Open**（允许探测）→ **Closed**（探测成功）或 **Open**（探测失败）。

---

## 安全部署

### HTTPS（生产环境）

始终部署在 TLS 终止反向代理之后：

```nginx
# nginx reverse proxy
server {
    listen 443 ssl;
    server_name thalamus.example.com;

    ssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:9880;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_buffering off;  # Required for streaming SSE
    }
}

server {
    listen 80;
    server_name thalamus.example.com;
    return 301 https://$host$request_uri;
}
```

```caddy
# Caddy reverse proxy (automatic HTTPS)
thalamus.example.com {
    reverse_proxy 127.0.0.1:9880
    header {
        X-Forwarded-Proto {scheme}
    }
}
```

### API Key 加密

设置 `THALAMUS_MASTER_KEY` 环境变量以启用 `keys.json` 的 Fernet 加密：

```bash
export THALAMUS_MASTER_KEY="your-strong-random-key"
```

不设置此环境变量时，密钥以明文存储（向后兼容）。

### Admin Panel 安全

- Admin Panel 登录：**5 次失败/min** → 限流，**10 次总计** → 封禁 30min
- 暴露于网络（非 localhost）时必须使用 HTTPS
- 登录尝试作为 `AUTH_LOGIN` 事件记录至 `events.jsonl`
- Session Token 24 小时后过期

---

## 故障模式

| 场景 | 行为 | 恢复方式 |
|------|------|----------|
| **所有后端宕机** | 返回 HTTP 502 | 后端恢复后自动重启 |
| **单路由故障** | 熔断器打开 → 激活失败链 → 使用同类能力模型 | 每 60s 半开探测 |
| **API Key 缺失** | 路由跳过 → 尝试下一个 Fallback | 在 events.jsonl 中记录为 WARN |
| **超出限流** | HTTP 429 带原因 | 60s 窗口后自动重置 |
| **流中途故障** | 发送含错误消息的 SSE chunk，终止流 | 客户端重连 |
| **非法 JSON 体** | HTTP 400 | 客户端修正请求 |
| **请求体过大** | HTTP 413 (>10MB) | 拆分请求 |
| **Admin Panel 要求 HTTPS** | HTTP 426 Upgrade Required | 使用 HTTPS 或通过 localhost 访问 |
| **Pre-check 超时** | Pre-check 跳过，请求正常继续 | 无用户侧影响 |
| **输入 > 140K 字符** | 路由分类跳过，直接走 DeepSeek 默认 | 路由精度下降 |

---

## 项目结构

```
thalamus/
├── thalamus.py      # 主守护进程 (v4.1.0, ~2400 行)
├── admin.html       # Web Admin Panel，含 Token 展示 + 登录持久化
├── routes.json      # 声明式路由规则，含失败链
├── keys.json        # 提供方凭证 (gitignored)
├── admin.pwd        # Admin Panel 密码 (gitignored)
├── policies.yaml    # 三级策略配置
├── semantic_router.py  # TF-IDF 语义分类器
├── protocol.md      # 完整协议规范
├── README.md        # 本文件
└── .gitignore
```

---

## 家族生态

| 项目 | 说明 |
|------|------|
| [Thalamus](https://github.com/sixgodgit/thalamus) | 🧠 智能模型路由中枢 |
| [NexSandglass](https://github.com/sixgodgit/NexSandglass-Agent-DedicatedMemory) | ⏳ 19 MCP 工具记忆系统，支持全文/语义/图谱搜索 |
| [Hypnos](https://github.com/sixgodgit/hypnos-dream-system) | 💤 自主夜间认知循环 |
| [Librarian](https://github.com/sixgodgit/skill-ecosystem-librarian) | 📚 140+ 技能生态管理 |

---

## License

MIT — 详见 [LICENSE](https://github.com/sixgodgit/thalamus/blob/master/LICENSE).
