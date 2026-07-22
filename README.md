# 🧠 Thalamus — Intelligent Model Routing Hub

> *"Thalamus: the brain's sensory relay station — every signal routed to the right cortical region."*

**Version**: v4.1.0 | **Status**: Stable | **License**: MIT | **Dependencies**: Zero (Python stdlib only)
[![CI](https://github.com/sixgodgit/thalamus/actions/workflows/test.yml/badge.svg)](https://github.com/sixgodgit/thalamus/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://python.org)
[![Test Coverage](https://img.shields.io/badge/tests-52%20passed-brightgreen)](tests/)

---

## Architecture

Thalamus implements a **cascading multi-layer routing architecture**, inspired by the brain's hierarchical information processing:

| Layer | Function | Latency |
|-------|----------|---------|
| 🧠 **Prefrontal** | Rule engine — regex pattern matching, zero-cost first pass | **<1ms** |
| 🧠 **Cortex** | Heavy reasoning, analysis, code generation | **varies** |
| 🧠 **Brainstem** | Default fallback — always available, always fast | **~2s** |

Each request cascades through the layers: if the rule engine doesn't match, it passes to semantic classification, and if that also fails, it defaults to the brainstem layer. This ensures **predictable routing with graceful degradation**.

---

## Core Features

| Feature | Description |
|---------|-------------|
| 🖥️ **Web Admin Panel** | Real-time dashboard: routing, keys, logs, token counters, live config editing |
| 🔄 **OpenAI-Compatible Proxy** | Full `/v1/chat/completions` compliance, streaming + non-streaming |
| 🛡️ **Per-Route Fallback Chains** | Each route carries its own fallback list — degrade within the same category before hitting default |
| 🔗 **Circuit Breaker + Fallback** | Automatic per-route failure detection, half-open probing, self-recovery — with fallback chain awareness |
| ⚡ **Streaming SSE** | Native `text/event-stream` passthrough with zero buffering |
| 🔧 **Tool Calls Passthrough** | Raw passthrough — no parsing, no modification, no field dropping |
| 🧮 **Multi-Model Parallelism** | `/parallel` endpoint dispatches to 3+ models simultaneously |
| 🧬 **Evolutionary Learning** | `/evolution` engine tracks routing decisions, self-optimizes over time |
| 📊 **Real-Time Observability** | `/stats` + `/cost-performance`: tokens by label/provider, latency, cost, fallback rate |
| 🔢 **Token Counting** | Per-label and per-provider token tracking with `/stats/reset` for session-level measurement |
| 🔑 **Login Persistence** | Cookie + localStorage token persistence — no repeated logins |
| 🔌 **Zero Dependencies** | Pure Python stdlib — no `pip install`, no virtualenv, no containers |
| 🔁 **Circuit Breaker** | Automatic per-route failure detection, half-open probing, self-recovery |
| 🏷️ **Per-IP Rate Limiting** | Token-bucket rate limiter: 60 req/min, 10 concurrent max, burst support |
| 💓 **Active Health Probing** | Background 60s-cycle probes for every route endpoint |
| 🧠 **Context-Aware Routing** | Multi-turn conversation history considered for route classification |

---

## Quick Start

### Prerequisites
- Python 3.8+
- API keys for your chosen inference providers

### Installation

```bash
git clone https://github.com/sixgodgit/thalamus.git
cd thalamus
```

### Configuration

Create `keys.json` with your provider credentials:

```json
{
  "provider_alias": {
    "key": "***",
    "endpoint": "https://api.provider.com/v1/chat/completions"
  }
}
```

### Run

```bash
python3 thalamus.py
```

### Verify

```bash
curl http://127.0.0.1:9880/health
```

---

## Routing

Routing rules are fully declarative in `routes.json`. The system applies a waterfall strategy — **first match wins**:

| Priority | Capability Domain | Match Triggers |
|----------|------------------|----------------|
| 🥇 | **Code & Engineering** | `code`, `deploy`, `debug`, `git`, `docker`, `api`, `python`, `error` — 60+ regex patterns |
| 🥈 | **Analysis & Reasoning** | `analyze`, `compare`, `why`, `root cause`, `strategy`, `architecture`, `review` |
| 🥉 | **Vision & Multimodal** | `image`, `screenshot`, `diagram`, `vision`, `OCR`, `chart` |
| 🏁 | **Default (Catch-all)** | Any unmatched input |

### Fallback Chains (v4.1.0)

Each route can define its own fallback chain, ensuring failure within a category degrades to a similar-capability model, not directly to the default:

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

> Rules are hot-reloadable: edit `routes.json` and trigger reload via the admin panel or `POST /admin/api/reload`.

### Context-Aware Classification

Unlike naive single-message routers, Thalamus evaluates the **last 5 user messages** with weighted emphasis on the most recent. This enables correct routing for follow-ups like "continue debugging" or "same approach for the other module" — cases where the final message alone carries insufficient signal.

### Pattern Tips

- Use **pipe `|`** as keyword separator in regex patterns (comma `,` is treated as a literal character)
- Shorter patterns with fewer keywords are more predictable
- Semantic classification acts as a secondary fallback when no regex matches

---

## API Reference

**Base URL**: `http://127.0.0.1:9880`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web admin panel |
| `/v1/chat/completions` | POST | OpenAI-compatible proxy (streaming + non-streaming) |
| `/task` | POST | Legacy single-route task dispatch |
| `/parallel` | POST | Parallel multi-model dispatch with result aggregation |
| `/analysis` | POST | Multi-perspective deep analysis |
| `/evolution` | GET | Evolutionary learning state |
| `/health` | GET | Health check + runtime status |
| `/stats` | GET | Full observability: tokens, calls, latency, cost, circuit breaker state |
| `/stats/reset` | GET | Reset token counters (returns previous snapshot) |
| `/cost-performance` | GET | Per-route cost and latency analysis with token breakdown |
| `/admin/*` | GET/POST | Admin operations: config, keys, logs, balances |

---

## Observability

Thalamus provides multi-dimensional observability out of the box:

```
/health             → Status, routes, uptime, error counts
/stats              → Full metrics: tokens, calls, latency, cost
/stats/reset        → Reset counters mid-session for task-level measurement
/cost-performance   → Per-route cost analysis with provider-level breakdown
/logs               → Color-coded: ERROR (red), FALLBACK (yellow), ROUTE (blue), CALL (green)
```

### Token Counting

Track token usage per-route and per-provider:

```bash
# View cumulative stats
curl http://127.0.0.1:9880/stats

# Reset before a specific task
curl http://127.0.0.1:9880/stats/reset

# View token breakdown after task
curl http://127.0.0.1:9880/stats | jq '.total_tokens, .token_by_label, .token_by_provider'
```

### Health Probe Output (example)

```
code-engine   ✅  1.07s
analysis      ✅  2.92s
vision        ✅  4.19s
default       ✅  5.26s
```

Active health probes run every 60 seconds across all configured routes. Results are exposed in `/stats` for monitoring integration.

---

## Rate Limiting & Circuit Breaker

### Rate Limiting

Thalamus uses a **token-bucket** rate limiter per client IP:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_requests_per_window` | 300 | Max requests per 60s window |
| `window_seconds` | 60 | Window duration |
| `max_concurrent` | 30 | Max concurrent requests per IP |
| `burst_tokens` | 50 | Burst capacity (auto-refills at 1/sec) |

Rate limited requests return **HTTP 429** with a reason string (`burst`, `window`, or `concurrent`). Metrics are exposed via `/stats` and `/metrics`.

### Circuit Breaker

Per-route circuit breaker with automatic half-open probing:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `consecutive_fail_threshold` | 3 | Failures before circuit opens |
| `half_open_interval` | 60s | Wait before probe request |
| `recover_success_count` | 1 | Probe success closes circuit |

States: **Closed** (normal) → **Open** (failing, requests skip to fallback) → **Half-Open** (probe allowed) → **Closed** (probe succeeded) or **Open** (probe failed).

---

## Failure Modes

| Scenario | Behavior | Recovery |
|----------|----------|----------|
| **All backends down** | Returns HTTP 502 | Auto-restart when backends recover |
| **Single route failing** | Circuit breaker opens → fallback chain activated → same-category model used | Half-open probe every 60s |
| **API key missing** | Route skipped → next fallback tried | Logged as WARN in events.jsonl |
| **Rate limit exceeded** | HTTP 429 with reason | Auto-resets after 60s window |
| **Mid-stream failure** | SSE chunk with error message sent, stream terminated | Client reconnects |
| **Invalid JSON body** | HTTP 400 | Client fixes request |
| **Request too large** | HTTP 413 (>10MB) | Split request |
| **Admin panel HTTPS required** | HTTP 426 Upgrade Required | Use HTTPS or access via localhost |
| **Pre-check timeout** | Pre-check skipped, request proceeds normally | No user-facing impact |
| **Input > 140K chars** | Route classification skipped, goes directly to DeepSeek default | Degraded routing accuracy |

---

## Security & Deployment

### HTTPS (Production)

Always deploy behind a TLS-terminating reverse proxy:

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

### API Key Encryption

Set `THALAMUS_MASTER_KEY` environment variable to enable Fernet encryption of `keys.json`:

```bash
export THALAMUS_MASTER_KEY="your-strong-random-key"
```

Without this env var, keys are stored in plaintext (backward compatible).

### Admin Panel Security

- Admin panel login: **5 failed attempts/min** → rate limited, **10 total** → 30min ban
- HTTPS access required when exposed to network (non-localhost)
- Login attempts logged to `events.jsonl` as `AUTH_LOGIN` events
- Session tokens expire after 24 hours

---

## Project Structure

```
thalamus/
├── thalamus.py      # Main daemon (v4.1.0, ~2400 lines)
├── admin.html       # Web admin panel with token display + login persistence
├── routes.json      # Declarative routing rules with fallback chains
├── keys.json        # Provider credentials (gitignored)
├── admin.pwd        # Admin panel password (gitignored)
├── policies.yaml    # Three-tier strategy config
├── semantic_router.py  # TF-IDF semantic classifier
├── protocol.md      # Full protocol spec
├── README.md        # This file
└── .gitignore
```

---

## Ecosystem

| Project | Description |
|---------|-------------|
| [Thalamus](https://github.com/sixgodgit/thalamus) | 🧠 Intelligent model routing hub |
| [NexSandglass](https://github.com/sixgodgit/NexSandglass-Agent-DedicatedMemory) | ⏳ 19 MCP-tool memory system with full-text/semantic/graph search |
| [Hypnos](https://github.com/sixgodgit/hypnos-dream-system) | 💤 Autonomous nightly cognitive cycle |
| [Librarian](https://github.com/sixgodgit/skill-ecosystem-librarian) | 📚 140+ skill ecosystem management |

---

## License

MIT — see [LICENSE](https://github.com/sixgodgit/thalamus/blob/master/LICENSE).
