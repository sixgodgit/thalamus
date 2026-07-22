#!/usr/bin/env python3
"""
Thalamus v3 — OpenAI 兼容透明代理

将请求按内容路由到专业模型，原样转发响应（streaming + 非 streaming）。
不解析、不修改、不丢弃任何字段（包括 tool_calls）。

端点：
  POST /v1/chat/completions   OpenAI 兼容（Hermes custom provider）
  POST /task                   旧版任务接口
  POST /parallel               并行多模型
  GET  /health                 健康检查
  GET  /stats                  调用统计

故障策略：任何环节失败 → 自动 fallback 到 DeepSeek
"""

import json
import os
import re
import secrets
import signal
import socket
import ssl
import sys
import time
import uuid
import hashlib
import threading
import traceback
import urllib.parse
import urllib.request
import http.client
import subprocess
import socks
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from cryptography.fernet import Fernet, InvalidToken

# ══════════════════════════════════════════
# 语义路由（v6 hybrid：TF-IDF 语义 + 正则兜底）
# ══════════════════════════════════════════
import sys
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
from semantic_router import classify_semantic

# ══════════════════════════════════════════
# 配置
# ══════════════════════════════════════════

LOG_PATH = Path(os.environ.get("THALAMUS_LOG", "/root/.hermes/logs/thalamus.log"))
ROUTES_PATH = Path(os.environ.get("THALAMUS_ROUTES", "/root/thalamus/routes.json"))
KEYS_PATH = Path("/root/thalamus/keys.json")
KEYS_PATH_ENC = Path("/root/thalamus/keys.json.enc")
ADMIN_HTML_PATH = Path("/root/thalamus/admin.html")
ADMIN_PWD_PATH = Path("/root/thalamus/admin.pwd")
ADMIN_TOKEN = {}  # {token: expiry}

# 加载路由配置
def _load_routes():
    global ROUTES, DEFAULT_MODEL, DEFAULT_PROVIDER, DEFAULT_ENDPOINT, DEFAULT_KEY_ENV
    global FALLBACK_MODEL, FALLBACK_PROVIDER, FALLBACK_ENDPOINT, FALLBACK_KEY_ENV
    global PRECHECK_ENABLED, PRECHECK_MODEL, PRECHECK_PROVIDER, PRECHECK_ENDPOINT, PRECHECK_KEY_ENV
    global PRECHECK_MAX_TOKENS, PRECHECK_TEMPERATURE

    try:
        with open(ROUTES_PATH) as f:
            cfg = json.load(f)

        # 路由规则 — 瀑布式，第一个命中即停止
        ROUTES = []
        for r in cfg.get("routes", []):
            fbs = r.get("fallbacks", [])
            ROUTES.append((
                re.compile(r["pattern"], re.IGNORECASE),
                r["model"],
                r["provider"],
                r["endpoint"],
                r["key_env"],
                r["label"],
                r.get("proxy", False),
                fbs,  # fallbacks list added
            ))

        # 默认路由
        d = cfg.get("default", {})
        DEFAULT_MODEL = d.get("model", "deepseek-chat")
        DEFAULT_PROVIDER = d.get("provider", "deepseek")
        DEFAULT_ENDPOINT = d.get("endpoint", "https://api.deepseek.com/v1/chat/completions")
        DEFAULT_KEY_ENV = d.get("key_env", "DEEPSEEK_API_KEY")

        # Fallback
        fb = cfg.get("fallback", {})
        FALLBACK_MODEL = fb.get("model", "mimo-v2-flash")
        FALLBACK_PROVIDER = fb.get("provider", "xiaomi")
        FALLBACK_ENDPOINT = fb.get("endpoint", "https://token-plan-cn.xiaomimimo.com/v1/chat/completions")
        FALLBACK_KEY_ENV = fb.get("key_env", "XIAOMI_API_KEY")

        # Pre-check
        pc = cfg.get("precheck", {})
        PRECHECK_ENABLED = pc.get("enabled", True)
        PRECHECK_MODEL = pc.get("model", DEFAULT_MODEL)
        PRECHECK_PROVIDER = pc.get("provider", DEFAULT_PROVIDER)
        PRECHECK_ENDPOINT = pc.get("endpoint", DEFAULT_ENDPOINT)
        PRECHECK_KEY_ENV = pc.get("key_env", DEFAULT_KEY_ENV)
        PRECHECK_MAX_TOKENS = pc.get("max_tokens", 512)
        PRECHECK_TEMPERATURE = pc.get("temperature", 0.3)
        # 任务1: 哪些 label 需要 precheck（默认代码类）
        global PRECHECK_TRIGGER_LABELS, PRECHECK_DEFAULT_ROUTE_PRECHECK
        PRECHECK_TRIGGER_LABELS = set(pc.get("trigger_labels", ["code", "coder", "dev", "glm"]))
        PRECHECK_DEFAULT_ROUTE_PRECHECK = pc.get("default_route_precheck", False)
        # 任务2: precheck 专用快速模型和超时
        global PRECHECK_TIMEOUT, PRECHECK_FAST_MODEL, PRECHECK_FAST_PROVIDER
        global PRECHECK_FAST_ENDPOINT, PRECHECK_FAST_KEY_ENV
        PRECHECK_TIMEOUT = pc.get("timeout", 5)  # 默认5秒
        PRECHECK_FAST_MODEL = pc.get("fast_model", PRECHECK_MODEL)
        PRECHECK_FAST_PROVIDER = pc.get("fast_provider", PRECHECK_PROVIDER)
        PRECHECK_FAST_ENDPOINT = pc.get("fast_endpoint", PRECHECK_ENDPOINT)
        PRECHECK_FAST_KEY_ENV = pc.get("fast_key_env", PRECHECK_KEY_ENV)

        log(f"Loaded {len(ROUTES)} routes from {ROUTES_PATH}")
    except Exception as e:
        log(f"WARNING: failed to load {ROUTES_PATH}: {e}")
        raise

# 默认值（_load_routes 之前可用）
ROUTES = []
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_PROVIDER = "deepseek"
DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_KEY_ENV = "DEEPSEEK_API_KEY"
FALLBACK_MODEL = "mimo-v2-flash"
FALLBACK_PROVIDER = "xiaomi"
FALLBACK_ENDPOINT = "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"
FALLBACK_KEY_ENV = "XIAOMI_API_KEY"
PRECHECK_ENABLED = True
PRECHECK_MODEL = DEFAULT_MODEL
PRECHECK_PROVIDER = DEFAULT_PROVIDER
PRECHECK_ENDPOINT = DEFAULT_ENDPOINT
PRECHECK_KEY_ENV = DEFAULT_KEY_ENV
PRECHECK_MAX_TOKENS=512
PRECHECK_TEMPERATURE=0.3
PRECHECK_TRIGGER_LABELS={"code", "coder", "dev", "glm"}
PRECHECK_DEFAULT_ROUTE_PRECHECK=False
# 任务2: precheck 快速模型和超时默认值
PRECHECK_TIMEOUT=5
PRECHECK_FAST_MODEL="deepseek-v4-flash"
PRECHECK_FAST_PROVIDER="deepseek"
PRECHECK_FAST_ENDPOINT="https://api.deepseek.com/v1/chat/completions"
PRECHECK_FAST_KEY_ENV="deepseek"

# 密钥存储 — 统一管理 key + endpoint
# keys.json 格式: {"ALIAS": {"key": "sk-...", "endpoint": "https://..."}}
KEYS = {}  # alias -> {"key": str, "endpoint": str}

# ══════════════════════════════════════════
# 密钥加密 (Fernet) — 惰性初始化
# ══════════════════════════════════════════

import base64 as _base64

_MASTER_KEY = os.environ.get("THALAMUS_MASTER_KEY", "")
_FERNET_INSTANCE = None

def _init_crypto():
    """Lazy-init Fernet from THALAMUS_MASTER_KEY env var."""
    global _FERNET_INSTANCE, _MASTER_KEY
    if _FERNET_INSTANCE is not None:
        return True
    if not _MASTER_KEY:
        return False
    try:
        _key_bytes = _base64.urlsafe_b64encode(hashlib.sha256(_MASTER_KEY.encode()).digest())
        _FERNET_INSTANCE = Fernet(_key_bytes)
        log("KEY CRYPTO: Fernet initialized from THALAMUS_MASTER_KEY")
        return True
    except Exception as e:
        log(f"WARNING: failed to init Fernet: {e}")
        _FERNET_INSTANCE = None
        return False

def _encrypt_keys_file(data: dict) -> bool:
    """Encrypt keys dict to keys.json.enc. Returns True on success."""
    inst = _init_crypto()
    if not inst:
        return False
    try:
        payload = json.dumps(data, ensure_ascii=False, default=str).encode()
        encrypted = _FERNET_INSTANCE.encrypt(payload)
        KEYS_PATH_ENC.write_bytes(encrypted)
        return True
    except Exception as e:
        log(f"KEY CRYPTO: encrypt failed: {e}")
        return False

def _decrypt_keys_file() -> dict | None:
    """Read and decrypt keys.json.enc. Returns dict or None."""
    inst = _init_crypto()
    if not inst or not KEYS_PATH_ENC.exists():
        return None
    try:
        encrypted = KEYS_PATH_ENC.read_bytes()
        payload = _FERNET_INSTANCE.decrypt(encrypted)
        return json.loads(payload)
    except InvalidToken:
        log("KEY CRYPTO: invalid token or wrong master key (keys.json.enc ignored)")
        return None
    except Exception as e:
        log(f"KEY CRYPTO: decrypt failed: {e}")
        return None

def _save_keys(data: dict):
    """Save keys dict — encrypted when master key available, fallback to plaintext keys.json.
    This is the single write path for all key CRUD operations."""
    # Always write plaintext keys.json for backward compatibility
    try:
        with open(KEYS_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"KEY WRITE: failed to write {KEYS_PATH}: {e}")
    # Also write encrypted if master key available
    _encrypt_keys_file(data)

def _load_keys():
    global KEYS
    try:
        # 1. Try encrypted file first
        decrypted = _decrypt_keys_file()
        if decrypted is not None:
            KEYS = {}
            for alias, val in decrypted.items():
                if isinstance(val, dict):
                    KEYS[alias] = val
                else:
                    KEYS[alias] = {"key": val, "endpoint": "https://api.deepseek.com/v1/chat/completions"}
            log(f"Loaded {len(KEYS)} API keys from {KEYS_PATH_ENC}")
            return
        # 2. Fallback to plaintext keys.json
        if KEYS_PATH.exists():
            with open(KEYS_PATH) as f:
                raw = json.load(f)
            KEYS = {}
            for alias, val in raw.items():
                if isinstance(val, dict):
                    KEYS[alias] = val
                else:
                    # Legacy: {"alias": "sk-..."} — migrate on load
                    KEYS[alias] = {"key": val, "endpoint": "https://api.deepseek.com/v1/chat/completions"}
            log(f"Loaded {len(KEYS)} API keys from {KEYS_PATH}")
        else:
            KEYS = {}
    except Exception as e:
        log(f"WARNING: failed to load keys: {e}")
        KEYS = {}

def _resolve_key(key_env: str) -> str:
    """Resolve key_env to an API key.
    
    key_env can be:
    - A KEYS alias: resolves to KEYS[alias]["key"]
    - An environment variable name: resolves to os.environ[key_env]
    
    Returns empty string if not found.
    """
    if not key_env:
        return ""
    # First check KEYS dict (for key aliases from keys.json)
    if key_env in KEYS:
        return KEYS[key_env]["key"]
    # Then fallback to direct environment variable
    return os.environ.get(key_env, "")

def _resolve_endpoint(key_env: str) -> str:
    """Resolve key_env to an endpoint URL.
    
    Prefers KEYS dict, falls back to empty string (caller supplies default).
    """
    if not key_env:
        return ""
    if key_env in KEYS:
        return KEYS[key_env].get("endpoint", "")
    return ""

# ─── 模型能力标签 ───

_CAPABILITY_PATTERNS = [
    ("multi", "vision|image|gemini|pixtral|vl|omni|multimodal"),
    ("reason", "reasoning|deepseek-r[12]|o[13]|thinking|claude-sonnet|claude-opus|gemini-thinking|gemini-3-pro|gemini-3[.5]|kimi-k2"),
    ("code", "codex|coder|deepseek-coder|qwen-coder|north"),
    ("fast", "flash|mini|small|8b|7b|1[.5]b|3b|free|lite|nano"),
]

def _tag_model(mid: str) -> list:
    m = mid.lower()
    tags = []
    for tag_name, pattern in _CAPABILITY_PATTERNS:
        if re.search(pattern, m):
            tags.append(tag_name)
    return tags

def _get_admin_password() -> str:
    try:
        if ADMIN_PWD_PATH.exists():
            return ADMIN_PWD_PATH.read_text().strip()
    except Exception:
        pass
    return ""

# ══════════════════════════════════════════
# 全局状态（线程安全）
# ══════════════════════════════════════════

START_TIME = time.time()
_spend_lock = threading.Lock()
_spend = {"total": 0.0, "by_provider": {}, "calls": 0, "fallbacks": 0, "errors": 0, "total_tokens": 0, "token_by_provider": {}, "token_by_label": {}}
# 任务6: 成本性能日志，记录每次调用的延迟和预估成本，不做路由选择（现有 routes.json 一个 label 只有一个候选）
_COST_PERF_LOG = []  # [{label, provider, model, latency, input_tok, output_tok, est_cost, ts}]
_COST_PERF_LOG_LOCK = threading.Lock()
_COST_PERF_LOG_MAX = 200

# 任务4: per-IP 速率限制
# 格式: {ip: {"count": N, "window_start": ts, "tokens": N}}
_RATE_LIMIT = {}
_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_CONFIG = {
    "max_requests_per_window": 300,  # 每个时间窗口最多请求数（提高以支持多子Agent并行）
    "window_seconds": 60,             # 窗口大小（秒）
    "max_concurrent": 30,             # 单 IP 最大并发（提高以支持多子Agent并行）
    "burst_tokens": 50,               # 突发令牌数（修复：旧值10太紧，子Agent并行瞬间爆）
}
# 管理面板登录限流（扩展 _RATE_LIMIT 结构）
# 格式: {ip: {"fail_count": N, "window_start": ts, "banned_until": ts}}
_LOGIN_RATE_LIMIT = {}
_LOGIN_RATE_LIMIT_LOCK = threading.Lock()
_LOGIN_RATE_LIMIT_CONFIG = {
    "max_fail_per_window": 5,      # 每窗口最多失败次数
    "window_seconds": 60,           # 窗口大小（秒）
    "ban_threshold": 10,            # 累计失败次数触发封禁
    "ban_duration": 1800,           # 封禁时长（秒）= 30分钟
}
# 主动健康探测
_HEALTH_PROBE_RESULTS = {}  # {label: {"ok": bool, "latency": float, "last_probe": ts}}
_HEALTH_PROBE_LOCK = threading.Lock()
_startup_errors = []

# 代理白名单 — 启动时一次性设置，避免多线程竞态
_TARGET_DOMAINS = {"token-plan-cn.xiaomimimo.com", "api.deepseek.com", "openrouter.ai"}
_no_proxy_configured = False

def _setup_noproxy():
    """一次性设置 NO_PROXY，覆盖所有目标域名"""
    global _no_proxy_configured
    if _no_proxy_configured:
        return
    existing = os.environ.get("NO_PROXY", "")
    parts = set(p.strip() for p in existing.split(",") if p.strip())
    needed = _TARGET_DOMAINS - parts
    if needed:
        os.environ["NO_PROXY"] = existing + ("," if existing else "") + ",".join(needed)
        log(f"NO_PROXY configured: added {needed}")
    _no_proxy_configured = True


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True, file=sys.stderr)


# ══════════════════════════════════════════
# 结构化日志 (JSON Lines)
# ══════════════════════════════════════════

EVENTS_LOG_PATH = Path("/root/thalamus/events.jsonl")
_EVENTS_LOG_LOCK = threading.Lock()

def _structured_log(level: str, event: str, data: dict | None = None):
    """写入结构化 JSON 日志到 events.jsonl.
    
    level: INFO | WARN | ERROR
    event: ROUTE | CALL | ERROR | FALLBACK | CIRCUIT | PRECHECK | AUTH_LOGIN
    data: 可选字段 dict，支持 latency_ms, label, provider, error, tokens 等
    """
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "level": level,
        "event": event,
    }
    if data:
        entry.update(data)
    try:
        EVENTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _EVENTS_LOG_LOCK:
            with open(EVENTS_LOG_PATH, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════
# Prometheus 指标（纯 stdlib 实现）
# ══════════════════════════════════════════

_PROMETHEUS_BUCKETS = [0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, float("inf")]

_METRICS_LOCK = threading.Lock()
_METRICS = {
    "requests_total": {},       # (method, endpoint, status, label, provider) -> int
    "duration_bucket_counts": {}, # (label, provider, bucket_idx) -> int
    "duration_sum": {},         # (label, provider) -> float (seconds)
    "duration_count": {},       # (label, provider) -> int
    "tokens_prompt": {},        # (label, provider) -> int
    "tokens_completion": {},    # (label, provider) -> int
    "fallbacks_total": {},      # (label, target) -> int
    "rate_limit_blocks": 0,
}

def _metrics_record_request(method: str, endpoint: str, status: int, label: str, provider: str):
    with _METRICS_LOCK:
        key = (method, endpoint, status, label, provider)
        _METRICS["requests_total"][key] = _METRICS["requests_total"].get(key, 0) + 1

def _metrics_record_duration(label: str, provider: str, seconds: float):
    with _METRICS_LOCK:
        key = (label, provider)
        _METRICS["duration_sum"][key] = _METRICS["duration_sum"].get(key, 0.0) + seconds
        _METRICS["duration_count"][key] = _METRICS["duration_count"].get(key, 0) + 1
        # 记录到 bucket
        for bi, bound in enumerate(_PROMETHEUS_BUCKETS):
            if seconds <= bound:
                bk = (label, provider, bi)
                _METRICS["duration_bucket_counts"][bk] = _METRICS["duration_bucket_counts"].get(bk, 0) + 1
                break

def _metrics_record_tokens(label: str, provider: str, prompt_tok: int, completion_tok: int):
    with _METRICS_LOCK:
        lpk = (label, provider)
        _METRICS["tokens_prompt"][lpk] = _METRICS["tokens_prompt"].get(lpk, 0) + prompt_tok
        _METRICS["tokens_completion"][lpk] = _METRICS["tokens_completion"].get(lpk, 0) + completion_tok

def _metrics_record_fallback(label: str, target: str):
    with _METRICS_LOCK:
        key = (label, target)
        _METRICS["fallbacks_total"][key] = _METRICS["fallbacks_total"].get(key, 0) + 1

def _metrics_record_rate_limit_block():
    with _METRICS_LOCK:
        _METRICS["rate_limit_blocks"] += 1


def _generate_prometheus_metrics() -> str:
    """手动拼 Prometheus exposition format (text/plain; version=0.0.4)"""
    lines = []
    lines.append("# HELP thalamus_requests_total Total requests handled")
    lines.append("# TYPE thalamus_requests_total counter")
    with _METRICS_LOCK:
        for (method, endpoint, status, label, provider), count in _METRICS["requests_total"].items():
            lines.append(f'thalamus_requests_total{{method="{method}",endpoint="{endpoint}",status="{status}",label="{label}",provider="{provider}"}} {count}')

    lines.append("# HELP thalamus_request_duration_seconds Request latency distribution")
    lines.append("# TYPE thalamus_request_duration_seconds histogram")
    with _METRICS_LOCK:
        # buckets
        bucketed = {}
        for (label, provider, bi), count in _METRICS["duration_bucket_counts"].items():
            bound = _PROMETHEUS_BUCKETS[bi]
            bound_str = "+Inf" if bound == float("inf") else str(bound)
            lines.append(f'thalamus_request_duration_seconds_bucket{{label="{label}",provider="{provider}",le="{bound_str}"}} {count}')
        # sum + count
        for (label, provider), total_s in _METRICS["duration_sum"].items():
            count = _METRICS["duration_count"].get((label, provider), 0)
            lines.append(f'thalamus_request_duration_seconds_sum{{label="{label}",provider="{provider}"}} {total_s}')
            lines.append(f'thalamus_request_duration_seconds_count{{label="{label}",provider="{provider}"}} {count}')

    lines.append("# HELP thalamus_tokens_total Total tokens (prompt + completion)")
    lines.append("# TYPE thalamus_tokens_total counter")
    with _METRICS_LOCK:
        for (label, provider), count in _METRICS["tokens_prompt"].items():
            lines.append(f'thalamus_tokens_total{{label="{label}",provider="{provider}",type="prompt"}} {count}')
        for (label, provider), count in _METRICS["tokens_completion"].items():
            lines.append(f'thalamus_tokens_total{{label="{label}",provider="{provider}",type="completion"}} {count}')

    lines.append("# HELP thalamus_circuit_breaker_info Circuit breaker state per label")
    lines.append("# TYPE thalamus_circuit_breaker_info gauge")
    with _CIRCUIT_BREAKER_LOCK:
        seen = set()
        for label, state in _CIRCUIT_BREAKER.items():
            is_open = state.get("is_open", False)
            lines.append(f'thalamus_circuit_breaker_info{{label="{label}",state="open"}} {1 if is_open else 0}')
            lines.append(f'thalamus_circuit_breaker_info{{label="{label}",state="closed"}} {0 if is_open else 1}')
            seen.add(label)
        # 确保每个 label 都有两行
        for label in seen:
            pass  # already above

    lines.append("# HELP thalamus_fallbacks_total Total fallback attempts")
    lines.append("# TYPE thalamus_fallbacks_total counter")
    with _METRICS_LOCK:
        for (label, target), count in _METRICS["fallbacks_total"].items():
            lines.append(f'thalamus_fallbacks_total{{label="{label}",target="{target}"}} {count}')

    lines.append("# HELP thalamus_rate_limit_blocks_total Total rate limit blocks")
    lines.append("# TYPE thalamus_rate_limit_blocks_total counter")
    with _METRICS_LOCK:
        lines.append(f'thalamus_rate_limit_blocks_total {_METRICS["rate_limit_blocks"]}')

    return "\n".join(lines) + "\n"


# ══════════════════════════════════════════
# 延迟百分位数跟踪（最近 1000 条）
# ══════════════════════════════════════════

_LATENCIES = []          # list of (label: str, latency_seconds: float)
_LATENCIES_LOCK = threading.Lock()
_LATENCIES_MAX = 1000

def _record_latency(label: str, seconds: float):
    with _LATENCIES_LOCK:
        _LATENCIES.append((label, seconds))
        if len(_LATENCIES) > _LATENCIES_MAX:
            _LATENCIES.pop(0)

def _compute_percentiles(values: list[float]) -> dict:
    if not values:
        return {"p50": 0, "p90": 0, "p95": 0, "p99": 0}
    sv = sorted(values)
    n = len(sv)
    def p(percent):
        idx = max(0, min(n - 1, int(n * percent / 100)))
        return round(sv[idx], 3)
    return {
        "p50": p(50),
        "p90": p(90),
        "p95": p(95),
        "p99": p(99),
    }

def _get_latency_percentiles() -> dict:
    """返回全局和按 label 分组的百分位数"""
    with _LATENCIES_LOCK:
        all_vals = [v for _, v in _LATENCIES]
        by_label = {}
        for lbl, val in _LATENCIES:
            by_label.setdefault(lbl, []).append(val)
    result = {
        "overall": _compute_percentiles(all_vals),
        "by_label": {lbl: _compute_percentiles(vals) for lbl, vals in by_label.items()},
        "sample_count": len(all_vals),
    }
    return result


# ══════════════════════════════════════════
# HTTP 请求（对后端）
# ══════════════════════════════════════════

def _set_noproxy(domain: str):
    """已废弃 — 改为启动时 _setup_noproxy() 全局设置。保留空实现避免调用出错。"""
    pass


def _make_request(endpoint: str, body: dict, key: str, stream: bool = False, timeout: int = 60, use_socks: bool = False):
    """
    向后端发 HTTP 请求。
    stream=False → 返回完整响应 dict
    stream=True  → 返回 (http.client.HTTPResponse, dict_info)
                   调用方负责读取 chunks 并关闭
    use_socks=True → 通过 SOCKS5 代理转发（默认 127.0.0.1:1080）
    """
    parsed = urllib.parse.urlparse(endpoint)
    host = parsed.hostname or ""
    if not host:
        raise RuntimeError(f"Invalid endpoint: {endpoint}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "Thalamus/3.0",
    }

    if use_socks:
        proxy_host = os.environ.get("THALAMUS_PROXY_HOST", "127.0.0.1")
        proxy_port = int(os.environ.get("THALAMUS_PROXY_PORT", "1080"))

        class _ProxyConn(http.client.HTTPSConnection):
            def connect(self):
                self.sock = socks.socksocket()
                self.sock.set_proxy(socks.SOCKS5, proxy_host, proxy_port)
                self.sock.settimeout(timeout)
                self.sock.connect((self.host, self.port))
                # TLS 握手
                ctx = ssl.create_default_context()
                self.sock = ctx.wrap_socket(self.sock, server_hostname=self.host)

        conn = _ProxyConn(host, port, timeout=timeout)
    else:
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)

    try:
        conn.request("POST", path, body=json.dumps(body).encode(), headers=headers)
        resp = conn.getresponse()

        if resp.status != 200:
            err_body = resp.read().decode(errors="replace")[:500]
            raise RuntimeError(f"Backend {resp.status}: {err_body}")

        if stream:
            return (conn, resp)
        else:
            raw = resp.read()
            conn.close()
            data = json.loads(raw)
            return data
    except Exception:
        conn.close()
        raise


def _read_stream_response(conn, resp, timeout=10):
    """从流式响应中读取 SSE chunks，作为 generator"""
    try:
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _guess_provider(endpoint: str) -> str:
    if "deepseek" in endpoint: return "deepseek"
    if "xiaomimimo" in endpoint: return "xiaomi"
    if "openrouter" in endpoint: return "openrouter"
    return "unknown"


def _estimate_cost(provider: str, prompt_tok: int, comp_tok: int) -> float:
    rates = {
        "deepseek": (0.007, 0.014),
        "xiaomi": (0.02, 0.02),
        "openrouter": (0.02, 0.04),
    }
    ri, ro = rates.get(provider, (0.02, 0.04))
    return (prompt_tok / 1_000_000 * ri) + (comp_tok / 1_000_000 * ro)


# ══════════════════════════════════════════
# 路由分类
# ══════════════════════════════════════════

def classify(messages: list) -> tuple | None:
    """
    路由分类：routes.json 优先，语义分类仅做兜底。
    
    规则：
    1. 正则匹配 → 直接走 routes.json 配的路由（语义无权覆盖）
    2. 正则未匹配 → 用语义猜测，语义 label 匹配到 routes.json 才用
    3. 全都没匹配 → 默认路由
    
    改进：拼接所有 user 消息作为上下文，让路由能感知对话历史。
    """
    # 拼接所有 user 消息中的纯文本（最近5条，加权最后一条）
    parts = []
    user_count = 0
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                text = " ".join(text_parts)
            else:
                continue
            if text.strip():
                # 最后一条 user 消息重复一次以强化权重
                if user_count == 0:
                    parts.insert(0, text)
                    parts.insert(0, text)
                else:
                    parts.insert(0, text)
                user_count += 1
                if user_count >= 5:
                    break
    
    text = "\n".join(parts)

    if not text:
        return None

    # ── 第1步：正则匹配（routes.json 配置优先）──
    for pattern, model, provider, endpoint, key_env, label, proxy, fallbacks in ROUTES:
        if pattern.search(text):
            log(f"ROUTE: regex → {label} ({model})")
            return (endpoint, model, key_env, provider, label, proxy, fallbacks)

    # ── 第2步：语义兜底（仅正则没匹配时）──
    semantic_result = classify_semantic(text)
    if semantic_result is not None:
        sem_label, sem_conf = semantic_result
        # 找到 routes.json 中对应 label 的路由
        for pattern, model, provider, endpoint, key_env, label, proxy, fallbacks in ROUTES:
            if label == sem_label:
                log(f"ROUTE: semantic → {sem_label} (conf={sem_conf:.2f})")
                return (endpoint, model, key_env, provider, label, proxy, fallbacks)
        log(f"ROUTE: semantic → {sem_label} (conf={sem_conf:.2f}, label not in routes — using default)")

    # ── 全都没匹配 → 默认路由 ──
    log("ROUTE: default route")
    return None


# ══════════════════════════════════════════
# Pre-check：先查能不能不写代码（Ponytail 理念）
# ══════════════════════════════════════════

# 任务3: precheck 内存缓存
_PRECHECK_CACHE = {}  # text_hash -> {result, ts}
_PRECHECK_CACHE_TTL = 300  # 默认5分钟
_PRECHECK_CACHE_MAX = 100
_PRECHECK_CACHE_HITS = 0
_PRECHECK_CACHE_MISSES = 0
_PRECHECK_CACHE_LOCK = threading.Lock()

# 任务4: Sandglass — 启动时一次性 import，不为每次请求重复 sys.path 操作
_SANDBOX_IMPORTED = False
_SANDBOX_SEARCH = None

def _init_sandglass():
    global _SANDBOX_IMPORTED, _SANDBOX_SEARCH
    if _SANDBOX_IMPORTED:
        return
    try:
        import sys as _sys
        for _p in ["/root/nexsandglass", "/root/.hermes/NexSandglass"]:
            if _p not in _sys.path:
                _sys.path.insert(0, _p)
        import os as _os
        _os.environ.setdefault("NEXSANDBASE_HOME", "/root/.hermes/nexsandglass")
        from sandglass_sqlite import search as _sg_search
        _SANDBOX_SEARCH = _sg_search
        _SANDBOX_IMPORTED = True
        log("SANDBOX: initialized at startup")
    except Exception as e:
        log(f"SANDBOX: init failed (non-fatal): {e}")

def _sandglass_query(text: str, max_time: float = 0.2) -> str:
    """查询 Sandglass 历史记录，超时不超过 max_time 秒。不影响主流程。"""
    if not _SANDBOX_IMPORTED or _SANDBOX_SEARCH is None:
        return ""
    _stopwords = {"用", "在", "的", "了", "吗", "吧", "呢", "啊", "什么", "怎么", "如何",
                  "哪个", "哪些", "可以", "能", "要", "是", "有", "给", "把", "被",
                  "from", "to", "in", "on", "at", "for", "of", "the", "a", "an",
                  "and", "or", "do", "is", "it", "with", "很", "太", "不", "没", "还",
                  "说", "话", "对", "那", "这", "我", "你", "他", "她"}
    import re as _re
    import threading as _t
    # 提取英文/数字关键词（技术词汇）
    _tech = [w.lower() for w in _re.findall(r"[a-zA-Z0-9_]{2,}", text)
             if w.lower() not in _stopwords]
    if _tech:
        _kw = " OR ".join(_tech[:3])
    else:
        _words = [w for w in text.split() if len(w) > 1]
        _kw = " ".join(_words[:2]) if _words else ""

    if not _kw:
        return ""

    _result_lines = []

    def _do_query():
        try:
            _results = _SANDBOX_SEARCH(_kw, limit=3)
            if _results:
                for _rid, _ts, _rtext in _results[:2]:
                    _short = _rtext.replace("\n", " ").strip()[:100]
                    _result_lines.append(f"  [{_ts}] {_short}")
        except Exception:
            pass

    _t_handle = _t.Thread(target=_do_query, daemon=True)
    _t_handle.start()
    _t_handle.join(timeout=max_time)

    if not _result_lines:
        return ""
    return "\n📖 **之前类似需求的记录：**\n" + "\n".join(_result_lines)

def _precheck_cache_key(text: str) -> str:
    return hashlib.md5(text.strip()[-200:].encode()).hexdigest()

def _precheck_cache_get(key: str) -> dict | None:
    global _PRECHECK_CACHE_HITS, _PRECHECK_CACHE_MISSES
    entry = _PRECHECK_CACHE.get(key)
    if entry and (time.time() - entry["ts"]) < _PRECHECK_CACHE_TTL:
        _PRECHECK_CACHE_HITS += 1
        return entry["result"]
    if entry:
        # 过期删除
        del _PRECHECK_CACHE[key]
    _PRECHECK_CACHE_MISSES += 1
    return None

def _precheck_cache_set(key: str, result: dict):
    with _PRECHECK_CACHE_LOCK:
        if len(_PRECHECK_CACHE) >= _PRECHECK_CACHE_MAX:
            # LRU-ish: 删掉最旧的一半
            sorted_keys = sorted(_PRECHECK_CACHE.keys(), key=lambda k: _PRECHECK_CACHE[k]["ts"])
            for k in sorted_keys[:len(sorted_keys)//2]:
                del _PRECHECK_CACHE[k]
        _PRECHECK_CACHE[key] = {"result": result, "ts": time.time()}

PRECHECK_SYSTEM_PROMPT = """你是 Preflight Checker，负责在 AI 编码 Agent 接活前拦住不必要的代码生成。

收到用户需求后，按以下顺序判断：

1. 标准库（stdlib）能搞定且不需要写自定义逻辑吗？ → Python/Node.js/Go 等语言自带的功能
2. 框架/平台内置了吗且不需要额外配置？ → React/Vue/Django 等框架自带功能
3. 有成熟的第三方包吗且不需要写包装代码？ → npm/pip 现成包

核心原则：只有用户的需求能通过**一行 import/require + 已有函数调用**解决时才拦截。
如果用户的需求需要**写完整的业务逻辑、多步处理、状态管理、定时任务、并发控制、自定义算法**等，不要拦截！

⚠️ 注意区分：
- "解析日期" → YES|stdlib （datetime.strptime 一行搞定）
- "写一个自动更新的token轮换服务" → NO （需要定时逻辑+状态管理+异常处理，不是一行 import 能搞定的）
- "用 Python 发 HTTP 请求" → YES|stdlib （urllib.request 一行）
- "写一个完整的 REST API 服务" → NO （需要路由/日志/错误处理等多步）

如果以上任一成立，回答格式：
  YES|{stdlib|framework|package}:{方案描述}
  例如：YES|stdlib: Python datetime.strptime 可以直接解析日期字符串

如果不成立，回答：
  NO|需求涉及自定义逻辑，需要写新代码

不要写代码！不要推荐要写代码的方案！只判断有没有现成的。"""


def precheck(messages: list, route: tuple | None = None) -> dict | None:
    """在路由分类之后执行 pre-check。

    发一条轻量查询给预检模型（默认 DeepSeek Chat），
    如果判定可以用现成方案解决，返回建议方案；
    否则返回 None，走正常路由。
    
    改进：precheck 优先使用路由选中的 endpoint，
    而非硬编码直连 DeepSeek。这样当路由熔断时，
    precheck 也会自动避开熔断的 provider。
    """
    if not PRECHECK_ENABLED:
        return None

    # 取最后一条用户消息
    text = ""
    for m in reversed(messages):
        content = m.get("content", "")
        if isinstance(content, str) and m.get("role") == "user":
            text = content.strip()
            break

    if not text:
        return None

    # 任务3: 查缓存
    _ckey = _precheck_cache_key(text)
    _cached = _precheck_cache_get(_ckey)
    if _cached is not None:
        log(f"PRECHECK CACHE HIT: {_cached.get('intercepted', '?')}")
        _cached["cache_hit"] = True
        return _cached if _cached.get("intercepted") else None

    # 使用路由选中的 endpoint（而非硬编码 DeepSeek）
    if route:
        pc_endpoint, pc_model, pc_key_env, _, _, _ = route
        pc_endpoint = pc_endpoint or PRECHECK_FAST_ENDPOINT
        pc_model = pc_model or PRECHECK_FAST_MODEL
        pc_key_env = pc_key_env or PRECHECK_FAST_KEY_ENV
    else:
        pc_endpoint = PRECHECK_FAST_ENDPOINT
        pc_model = PRECHECK_FAST_MODEL
        pc_key_env = PRECHECK_FAST_KEY_ENV

    # 预检 prompt 很轻：只发用户消息 + system prompt，不传全上下文
    pre_body = {
        "model": pc_model,
        "messages": [
            {"role": "system", "content": PRECHECK_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "max_tokens": PRECHECK_MAX_TOKENS,
        "temperature": PRECHECK_TEMPERATURE,
        "stream": False,
    }

    key = _resolve_key(pc_key_env)
    if not key:
        log(f"PRECHECK WARNING: key not set: {PRECHECK_FAST_KEY_ENV}")
        return None

    t0 = time.time()
    try:
        data = _make_request(pc_endpoint, pre_body, key, stream=False, timeout=PRECHECK_TIMEOUT)
        latency = time.time() - t0
        choice = data.get("choices", [{}])[0]
        reply = choice.get("message", {}).get("content", "").strip()
        log(f"PRECHECK: {latency:.2f}s | {reply[:80]}")

        # 解析结果
        if reply.startswith("YES|"):
            parts = reply.split("|", 2)
            if len(parts) >= 3:
                category = parts[1]  # stdlib / framework / package
                suggestion = parts[2]
            elif len(parts) == 2 and ":" in parts[1]:
                # 兼容格式：YES|stdlib: 描述（DeepSeek 爱用冒号）
                sub = parts[1].split(":", 1)
                category = sub[0].strip()
                suggestion = sub[1].strip()
            else:
                category = "stdlib"
                suggestion = parts[1] if len(parts) > 1 else reply
            log(f"PRECHECK: intercepted → {category}: {suggestion}")
            # 查询 Sandglass：用户以前做过类似需求吗？（异步 + 200ms 超时，不阻塞主流程）
            sandglass_hint = _sandglass_query(text, max_time=0.2)
            if sandglass_hint:
                log(f"PRECHECK: sandglass hit for text[:60]={text[:60]!r}")

            _result = {
                "intercepted": True,
                "category": category,
                "suggestion": suggestion,
                "sandglass_hint": sandglass_hint,
                "latency": round(latency, 2),
            }
            _precheck_cache_set(_ckey, _result)
            return _result

        # NO 或无法解析 → 放行
        _result_no = {"intercepted": False}
        _precheck_cache_set(_ckey, _result_no)
        return _result_no
    except (socket.timeout, TimeoutError) as _te:
        _spend["precheck_timeouts"] = _spend.get("precheck_timeouts", 0) + 1
        log(f"PRECHECK TIMEOUT ({PRECHECK_TIMEOUT}s): {_te}")
        return None  # 超时不阻塞正常流程
    except Exception as e:
        _spend["precheck_errors"] = _spend.get("precheck_errors", 0) + 1
        log(f"PRECHECK ERROR: {e}")
        return None  # precheck 失败不阻塞正常流程


# ══════════════════════════════════════════
# 速率限制
# ══════════════════════════════════════════

def _rate_limit_check(client_ip: str) -> tuple[bool, str]:
    """检查 client_ip 是否超过速率限制。
    
    Returns: (allowed: bool, reason: str)
    """
    now = time.time()
    with _RATE_LIMIT_LOCK:
        entry = _RATE_LIMIT.get(client_ip)
        if not entry or (now - entry["window_start"]) > _RATE_LIMIT_CONFIG["window_seconds"]:
            # 新窗口
            _RATE_LIMIT[client_ip] = {
                "count": 1,
                "window_start": now,
                "concurrent": 1,
                "tokens": _RATE_LIMIT_CONFIG["burst_tokens"] - 1,
                "last_refill": now,
            }
            return (True, "")
        
        # ── 令牌补充：按时间差补充令牌（每 1 秒恢复 1 个）──
        last_refill = entry.get("last_refill", entry["window_start"])
        elapsed = now - last_refill
        if elapsed >= 1.0:
            refill = int(elapsed)  # 每秒补 1 个
            max_tokens = _RATE_LIMIT_CONFIG["burst_tokens"]
            entry["tokens"] = min(max_tokens, entry.get("tokens", max_tokens) + refill)
            entry["last_refill"] = now
        
        # 检查突发令牌
        if entry.get("tokens", _RATE_LIMIT_CONFIG["burst_tokens"]) <= 0:
            return (False, "rate_limit: burst exceeded")
        
        # 检查窗口请求数
        if entry["count"] >= _RATE_LIMIT_CONFIG["max_requests_per_window"]:
            return (False, "rate_limit: window exceeded")
        
        # 检查并发
        entry["concurrent"] = entry.get("concurrent", 0) + 1
        if entry["concurrent"] > _RATE_LIMIT_CONFIG["max_concurrent"]:
            entry["concurrent"] -= 1
            return (False, "rate_limit: concurrent exceeded")
        
        entry["count"] += 1
        # 消耗一个令牌
        entry["tokens"] = entry.get("tokens", _RATE_LIMIT_CONFIG["burst_tokens"]) - 1
        return (True, "")

def _rate_limit_release(client_ip: str):
    """请求完成后释放并发槽位"""
    with _RATE_LIMIT_LOCK:
        entry = _RATE_LIMIT.get(client_ip)
        if entry:
            entry["concurrent"] = max(0, entry.get("concurrent", 0) - 1)


# ══════════════════════════════════════════
# 主动健康探测
# ══════════════════════════════════════════

def _health_probe_worker():
    """后台线程：每 60 秒探测所有可用 provider 的健康状态"""
    while True:
        time.sleep(60)
        try:
            now = time.time()
            for pattern, model, provider_name, endpoint, key_env, label, proxy in ROUTES:
                key = _resolve_key(key_env)
                if not key or not endpoint:
                    continue
                probe_body = {
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                    "stream": False,
                }
                t0 = now
                try:
                    _make_request(endpoint, probe_body, key, stream=False, timeout=10)
                    latency = time.time() - t0
                    with _HEALTH_PROBE_LOCK:
                        _HEALTH_PROBE_RESULTS[label] = {
                            "ok": True,
                            "latency": round(latency, 2),
                            "last_probe": now,
                        }
                    log(f"HEALTH PROBE: {label} ✅ ({latency:.2f}s)")
                except Exception as e:
                    with _HEALTH_PROBE_LOCK:
                        _HEALTH_PROBE_RESULTS[label] = {
                            "ok": False,
                            "error": str(e)[:60],
                            "last_probe": now,
                        }
                    log(f"HEALTH PROBE: {label} ❌ ({str(e)[:60]})")
        except Exception:
            pass


# ══════════════════════════════════════════
# 核心处理
# ══════════════════════════════════════════

def process(body: dict) -> tuple[str, bool, dict]:
    """
    处理一个请求，返回 (response_mode, is_streaming, result_or_generator)

    response_mode: "json" | "stream"
    is_streaming: 请求是否要求 streaming
    result_or_generator: JSON dict（非流式）或 generator（流式）
    """
    messages = body.get("messages", [])
    is_stream = body.get("stream", False)

    # 0. Input length guard: 如果总输入 > 140K 字符，跳过 TF-IDF 语义路由但保留正则匹配
    total_input_chars = sum(
        len(m.get("content", "")) if isinstance(m.get("content"), str) else 0
        for m in messages
    )
    long_input = total_input_chars > 140_000
    if long_input:
        log(f"INPUT TOO LONG ({total_input_chars} chars): skipping semantic classification, regex-only routing")
        # 只从最后一条 user 消息提取文本做正则匹配（TF-IDF 在超长文本上失效）
        last_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str):
                    last_text = content
                elif isinstance(content, list):
                    text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                    last_text = " ".join(text_parts)
                break
        route = None
        if last_text:
            for pattern, model, provider_name, endpoint, key_env, label, proxy, fallbacks in ROUTES:
                if pattern.search(last_text):
                    log(f"ROUTE (long input): regex → {label} ({model})")
                    route = (endpoint, model, key_env, provider_name, label, proxy, fallbacks)
                    break
        if not route:
            # 正则没匹配到 → 默认路由
            endpoint, model, key_env, provider, label, proxy = (
                DEFAULT_ENDPOINT, DEFAULT_MODEL, DEFAULT_KEY_ENV, DEFAULT_PROVIDER, "DeepSeek (long input)", False
            )
            route = (endpoint, model, key_env, provider, label, proxy, [])

    # 1. 先分类（非长输入时走完整 classify，含语义兜底）
    if not long_input:
        route = classify(messages)
    # 注意：long_input 的 route 已在上面设好，此处不覆盖

    # 2. 只在代码类路由才跑 precheck
    should_precheck = False
    precheck_label = ""
    if route:
        _label = route[4]  # label in (endpoint, model, key_env, provider, label, proxy)
        precheck_label = _label
        if any(trig in _label.lower() for trig in PRECHECK_TRIGGER_LABELS):
            should_precheck = True
    else:
        # 没命中任何路由 → 走默认，按开关决定是否 precheck
        precheck_label = "DeepSeek (default)"
        if PRECHECK_DEFAULT_ROUTE_PRECHECK:
            should_precheck = True

    if should_precheck:
        pc_result = precheck(messages, route=route)
        if pc_result and pc_result.get("intercepted"):
            log(f"PRECHECK: bypassed routing for label={precheck_label}, task solved without code generation")
            suggestion = pc_result["suggestion"]
            # 构造一个直接建议的响应，不走模型路由
            reply_text = f"✅ **{pc_result['category'].upper()} 方案可用**\\n\\n{suggestion}"
            if pc_result.get("sandglass_hint"):
                reply_text += pc_result["sandglass_hint"]
            response = {
            "id": f"thalamus-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": PRECHECK_MODEL,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": reply_text,
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": len(reply_text), "total_tokens": 0},
            "_thalamus": {
                "routed_to": f"precheck-{pc_result['category']}",
                "provider": PRECHECK_PROVIDER,
                "latency_s": pc_result["latency"],
                "intercepted": True,
            },
        }
            return ("json", False, response)

    # 3. 构建转发 body（替换 model 为路由目标，保留其他所有参数）
    if route:
        endpoint, model, key_env, provider, label, proxy = route[:6]
    else:
        endpoint, model, key_env = DEFAULT_ENDPOINT, DEFAULT_MODEL, DEFAULT_KEY_ENV
        provider = DEFAULT_PROVIDER
        label = "DeepSeek (default)"
        proxy = False

    # 任务5: 路由熔断检查 — 在发起请求之前拦截
    if route and _circuit_is_tripped(label):
        log(f"CIRCUIT: {label} is OPEN, trying fallbacks before default")
        _spend["circuit_breaker_skips"] = _spend.get("circuit_breaker_skips", 0) + 1
        # 不直接跳 DeepSeek，先走路由自身的 fallback 链
        try:
            return _try_route_fallbacks(route, body, is_stream, label)
        except Exception:
            # 全部 fallback 都失败 → 最后防线 DeepSeek
            return _fallback_to_deepseek(body, is_stream)

    key = _resolve_key(key_env)
    if not key:
        log(f"ERROR: API key not set: {key_env}")
        raise RuntimeError(f"API key not set: {key_env}")

    # 构建转发请求体
    fwd_body = dict(body)
    fwd_body["model"] = model
    # 确保 stream 参数传给后端
    fwd_body["stream"] = is_stream

    # 4. 域名代理处理
    domain = urllib.parse.urlparse(endpoint).hostname or ""
    if not domain:
        raise RuntimeError(f"Invalid endpoint domain: {endpoint}")
    _set_noproxy(domain)

    t0 = time.time()

    # 5. 请求后端
    try:
        use_socks = proxy
        if is_stream:
            conn, resp = _make_request(endpoint, fwd_body, key, stream=True, use_socks=use_socks)
            # 记录路由延迟在 generator 的元数据中
            latency = time.time() - t0
            _record_stats(provider, label, latency, is_stream)
            log(f"STREAM: {label} | {latency:.2f}s")

            def stream_gen():
                try:
                    for chunk in _read_stream_response(conn, resp):
                        yield chunk
                except Exception as e:
                    log(f"STREAM ERROR (mid-stream): {label} | {e}")
                    # mid-stream 无法 fallback，只能终止
                    yield f'data: {{"error": "Stream interrupted"}}\\n\\n'.encode()
                    yield "data: [DONE]\\n\\n".encode()
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            return ("stream", True, stream_gen())
        else:
            data = _make_request(endpoint, fwd_body, key, stream=False, use_socks=use_socks)
            latency = time.time() - t0
            _record_stats(provider, label, latency, is_stream)

        # 提取用量信息
        usage = data.get("usage", {})
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        if pt or ct:
            with _spend_lock:
                _spend["total_tokens"] += pt + ct
                _spend["token_by_provider"][provider] = _spend["token_by_provider"].get(provider, 0) + pt + ct
                _spend["token_by_label"][label] = _spend["token_by_label"].get(label, 0) + pt + ct
        choice = data.get("choices", [{}])[0]
        actual_model = data.get("model", model)
        choice_msg = choice.get("message", {})
        content_len = len(choice_msg.get("content", "") or "")
        has_tools = "tool_calls" in choice_msg

        log(f"CALL: {label} | {latency:.2f}s | "
            f"tok={usage.get('prompt_tokens',0)}+{usage.get('completion_tokens',0)} | "
            f"content={content_len}c tool_calls={'Y' if has_tools else 'N'} | "
            f"model={actual_model}")

        # 任务6: 成本性能日志（仅记录，不做路由选择）
        est_cost = _estimate_cost(provider, pt, ct)
        with _COST_PERF_LOG_LOCK:
            _COST_PERF_LOG.append({
                "label": label,
                "provider": provider,
                "model": actual_model,
                "latency": round(latency, 2),
                "input_tok": pt,
                "output_tok": ct,
                "est_cost": round(est_cost, 6),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            if len(_COST_PERF_LOG) > _COST_PERF_LOG_MAX:
                _COST_PERF_LOG.pop(0)

        # 原样返回，不修改 choice 结构
        response = {
            "id": data.get("id", f"thalamus-{uuid.uuid4().hex[:12]}"),
            "object": "chat.completion",
            "created": data.get("created", int(time.time())),
            "model": actual_model,
            "choices": [{
                "index": 0,
                "message": choice_msg,
                "finish_reason": choice.get("finish_reason", "stop"),
            }],
            "usage": usage,
            "_thalamus": {
                "routed_to": label,
                "provider": provider,
                "latency_s": round(latency, 2),
            },
        }
        evolution_learn(label, latency, success=True)
        return ("json", False, response)

    except Exception as e:
        latency = time.time() - t0
        _record_error(provider, label, str(e), latency)

        # 6. 路由降级链：先试同级 fallback → 再试默认 DeepSeek → 最后 MiMo
        if route:
            # 6a. 尝试路由自身 fallbacks（优先保持同类别能力）
            try:
                return _try_route_fallbacks(route, body, is_stream, label)
            except Exception:
                pass
            # 6b. 全部 fallback 失败 → 默认 DeepSeek
            log(f"FALLBACK: {label} fallbacks all failed → DeepSeek")
            evolution_learn(label, latency, success=False)
            return _fallback_to_deepseek(body, is_stream)
        else:
            # DeepSeek 自身失败 → fallback 到 MiMo Flash
            log(f"FALLBACK: DeepSeek failed ({e}) → MiMo Flash")
            evolution_learn("DeepSeek (default)", latency, success=False)
            return _fallback_to_mimo(body, is_stream)


def _try_route_fallbacks(route: tuple, body: dict, is_stream: bool, label: str) -> tuple:
    """尝试路由自身的 fallback 链，全部失败则抛出异常"""
    import uuid as _uuid
    fallbacks = route[6] if len(route) > 6 else []
    if not fallbacks:
        raise RuntimeError("no fallbacks configured")
    for fb in fallbacks:
        fb_model = fb.get("model", "")
        fb_provider = fb.get("provider", "")
        fb_key = fb.get("key_env", "")
        fb_ep = fb.get("endpoint", "")
        fb_label = f"{label} (fallback: {fb_model})"
        log(f"FALLBACK: {label} failed, trying {fb_model}")
        fb_key_val = _resolve_key(fb_key)
        if not fb_key_val:
            continue
        fb_domain = urllib.parse.urlparse(fb_ep).hostname or ""
        if not fb_domain:
            continue
        _set_noproxy(fb_domain)
        fb_body = dict(body)
        fb_body["model"] = fb_model
        fb_data = _make_request(fb_ep, fb_body, fb_key_val, stream=False, use_socks=False)
        fb_usage = fb_data.get("usage", {})
        pt = fb_usage.get("prompt_tokens", 0)
        ct = fb_usage.get("completion_tokens", 0)
        if pt or ct:
            with _spend_lock:
                _spend["total_tokens"] += pt + ct
                _spend["token_by_provider"][fb_provider] = _spend["token_by_provider"].get(fb_provider, 0) + pt + ct
                _spend["token_by_label"][fb_label] = _spend["token_by_label"].get(fb_label, 0) + pt + ct
        fb_choice = fb_data.get("choices", [{}])[0]
        fb_actual = fb_data.get("model", fb_model)
        log(f"CALL (fallback): {fb_label} | model={fb_actual} tok={pt}+{ct}")
        response = {
            "id": f"thalamus-{_uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": fb_actual,
            "choices": [{"index": 0, "message": fb_choice.get("message", {}), "finish_reason": fb_choice.get("finish_reason", "stop")}],
        }
        return ("json", False, response)
    raise RuntimeError(f"all {len(fallbacks)} fallback(s) failed")


def _fallback_to_deepseek(body: dict, is_stream: bool) -> tuple:
    """Fallback 到 DeepSeek"""
    with _spend_lock:
        _spend["fallbacks"] += 1
        fb_num = _spend["fallbacks"]

    key = _resolve_key(DEFAULT_KEY_ENV)
    if not key:
        raise RuntimeError(f"Fallback key not set: {DEFAULT_KEY_ENV}")

    fwd_body = dict(body)
    fwd_body["model"] = DEFAULT_MODEL
    fwd_body["stream"] = is_stream

    t0 = time.time()
    domain = urllib.parse.urlparse(DEFAULT_ENDPOINT).hostname or ""
    if not domain:
        raise RuntimeError(f"Invalid fallback endpoint domain: {DEFAULT_ENDPOINT}")
    _set_noproxy(domain)

    try:
        if is_stream:
            conn, resp = _make_request(DEFAULT_ENDPOINT, fwd_body, key, stream=True)
            latency = time.time() - t0
            _record_stats("deepseek", "DeepSeek (fallback)", latency, True)
            log(f"STREAM (fallback #{fb_num}): DeepSeek | {latency:.2f}s")

            def stream_gen():
                try:
                    for chunk in _read_stream_response(conn, resp):
                        yield chunk
                except Exception as e:
                    yield f'data: {{"error": "Even fallback stream failed"}}\n\n'.encode()
                    yield "data: [DONE]\n\n".encode()
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            return ("stream", True, stream_gen())
        else:
            data = _make_request(DEFAULT_ENDPOINT, fwd_body, key, stream=False)
            latency = time.time() - t0
            _record_stats("deepseek", "DeepSeek (fallback)", latency, False)

            choice = data.get("choices", [{}])[0]
            usage = data.get("usage", {})
            log(f"CALL (fallback #{fb_num}): DeepSeek | {latency:.2f}s | "
                f"tok={usage.get('prompt_tokens',0)}+{usage.get('completion_tokens',0)}")

            return ("json", False, {
                "id": data.get("id", f"thalamus-fb-{uuid.uuid4().hex[:8]}"),
                "object": "chat.completion",
                "created": data.get("created", int(time.time())),
                "model": data.get("model", DEFAULT_MODEL),
                "choices": [{
                    "index": 0,
                    "message": choice.get("message", {}),
                    "finish_reason": choice.get("finish_reason", "stop"),
                }],
                "usage": usage,
                "_thalamus": {
                    "routed_to": "DeepSeek (fallback #{})".format(fb_num),
                    "provider": "deepseek",
                    "latency_s": round(latency, 2),
                },
            })
    except Exception as e:
        latency = time.time() - t0
        _record_error("deepseek", "DeepSeek (fallback)", str(e), latency)
        log(f"CRITICAL: fallback also failed: {e}")
        raise


def _fallback_to_mimo(body: dict, is_stream: bool) -> tuple:
    """DeepSeek 失败时 fallback 到 MiMo Flash"""
    with _spend_lock:
        _spend["fallbacks"] += 1
        fb_num = _spend["fallbacks"]

    key = _resolve_key(FALLBACK_KEY_ENV)
    if not key:
        log(f"ERROR: MiMo fallback key not set: {FALLBACK_KEY_ENV}")
        raise RuntimeError(f"MiMo fallback key not set: {FALLBACK_KEY_ENV}")

    fwd_body = dict(body)
    fwd_body["model"] = FALLBACK_MODEL
    fwd_body["stream"] = is_stream

    t0 = time.time()
    domain = urllib.parse.urlparse(FALLBACK_ENDPOINT).hostname or ""
    if not domain:
        raise RuntimeError(f"Invalid fallback endpoint domain: {FALLBACK_ENDPOINT}")
    _set_noproxy(domain)

    try:
        if is_stream:
            conn, resp = _make_request(FALLBACK_ENDPOINT, fwd_body, key, stream=True)
            latency = time.time() - t0
            _record_stats("xiaomi", "MiMo Flash (fallback)", latency, True)
            log(f"STREAM (fallback #{fb_num}): MiMo Flash | {latency:.2f}s")

            def stream_gen():
                try:
                    for chunk in _read_stream_response(conn, resp):
                        yield chunk
                except Exception as e:
                    yield f'data: {{"error": "MiMo fallback stream failed"}}\n\n'.encode()
                    yield "data: [DONE]\n\n".encode()
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            return ("stream", True, stream_gen())
        else:
            data = _make_request(FALLBACK_ENDPOINT, fwd_body, key, stream=False)
            latency = time.time() - t0
            _record_stats("xiaomi", "MiMo Flash (fallback)", latency, False)

            choice = data.get("choices", [{}])[0]
            usage = data.get("usage", {})
            log(f"CALL (fallback #{fb_num}): MiMo Flash | {latency:.2f}s | "
                f"tok={usage.get('prompt_tokens',0)}+{usage.get('completion_tokens',0)}")

            return ("json", False, {
                "id": data.get("id", f"thalamus-fb-{uuid.uuid4().hex[:8]}"),
                "object": "chat.completion",
                "created": data.get("created", int(time.time())),
                "model": data.get("model", FALLBACK_MODEL),
                "choices": [{
                    "index": 0,
                    "message": choice.get("message", {}),
                    "finish_reason": choice.get("finish_reason", "stop"),
                }],
                "usage": usage,
                "_thalamus": {
                    "routed_to": "MiMo Flash (fallback #{})".format(fb_num),
                    "provider": "xiaomi",
                    "latency_s": round(latency, 2),
                },
            })
    except Exception as e:
        latency = time.time() - t0
        _record_error("xiaomi", "MiMo Flash (fallback)", str(e), latency)
        log(f"CRITICAL: MiMo fallback also failed: {e}")
        raise


# ══════════════════════════════════════════
# 多模型分析（从 multi-model-analysis 合并）
# ══════════════════════════════════════════

_ANALYSIS_DOMAINS = {"token-plan-cn.xiaomimimo.com", "api.deepseek.com", "openrouter.ai"}

_ANALYSIS_PERSONAS = {
    "code": {
        "hint": "claude",  # 走 claude-haiku 路由 (token173, 可用)
        "system": "你是一名资深软件架构师。以清晰、严谨的风格分析代码问题。",
    },
    "reasoning": {
        "hint": "gpt",  # 走 gpt-5.4-mini 路由 (token173, 可用)
        "system": "你是一名逻辑分析师。从多个角度审视问题，给出全面的推理链。",
    },
    "creative": {
        "hint": "deepseek",
        "system": "你是一名创意顾问。提供新颖的视角和创新的解决方案。",
    },
}

MULTI_MODEL_CACHE = {}  # 轻量内存缓存，避免重复分析
MULTI_MODEL_CACHE_MAX = 50


def multi_analysis(prompt: str, max_tokens: int = 4096) -> dict:
    """
    平行调用多个模型分析同一问题，返回多视角综合报告。
    缓存相同 prompt 的结果（最多 50 条）。
    """
    cache_key = prompt[:100]
    if cache_key in MULTI_MODEL_CACHE:
        log(f"ANALYSIS CACHE HIT: {cache_key[:40]}...")
        return MULTI_MODEL_CACHE[cache_key]

    perspectives = {}
    errors = {}
    lock = threading.Lock()

    def _call_perspective(name: str, cfg: dict):
        try:
            hint = cfg.get("hint", "")
            endpoint = DEFAULT_ENDPOINT
            model = DEFAULT_MODEL
            key_env = DEFAULT_KEY_ENV
            provider = DEFAULT_PROVIDER

            # 通过 hint 匹配路由：先精确匹配 label/model，再模糊匹配
            for p, m, prov, ep, ke, label, proxy in ROUTES:
                if hint and (hint in label.lower() or hint in m.lower() or hint in prov.lower()):
                    endpoint = ep
                    model = m
                    key_env = ke
                    provider = prov
                    break

            key = _resolve_key(key_env)
            if not key:
                raise RuntimeError(f"Key not found: {key_env}")

            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": cfg["system"]},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "stream": False,
            }
            data = _make_request(endpoint, body, key, stream=False)
            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")

            with lock:
                perspectives[name] = content
        except Exception as e:
            with lock:
                errors[name] = str(e)
            log(f"ANALYSIS ERROR ({name}): {e}")

    # 平行调用
    threads = []
    for name, cfg in _ANALYSIS_PERSONAS.items():
        t = threading.Thread(target=_call_perspective, args=(name, cfg), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=120)

    result = {
        "prompt": prompt[:200],
        "perspectives": perspectives,
        "errors": errors,
        "count": len(perspectives),
        "error_count": len(errors),
    }

    # 缓存
    if len(MULTI_MODEL_CACHE) < MULTI_MODEL_CACHE_MAX:
        MULTI_MODEL_CACHE[cache_key] = result

    return result


# ══════════════════════════════════════════
# 进化学习（从 routing-self-evolution 合并）
# ══════════════════════════════════════════

_EVOLUTION_PATTERNS = []  # 成功模式库
_EVOLUTION_LOG = []  # 路由决策日志
_EVOLUTION_SCORE = 0  # 积分

EVOLUTION_PERSIST_PATH = Path(os.environ.get("THALAMUS_EVOLUTION",
    "/root/.hermes/data/thalamus-evolution.json"))

# 任务5: 路由熔断
# _CIRCUIT_BREAKER[label] = {"fail_count", "last_fail_ts", "last_probe_ts", "is_open"}
_CIRCUIT_BREAKER = {}
_CIRCUIT_BREAKER_LOCK = threading.Lock()
_CIRCUIT_CONFIG = {
    "consecutive_fail_threshold": 3,   # 连续 N 次失败 → 熔断
    "half_open_interval": 60,          # N 秒后允许探测请求
    "recover_success_count": 1,         # 连续成功 N 次 → 恢复
}
_CIRCUIT_BREAKER_TRIGGERED = 0  # 统计熔断触发次数

def _circuit_get_state(label: str) -> dict:
    """获取路由的熔断状态"""
    with _CIRCUIT_BREAKER_LOCK:
        state = _CIRCUIT_BREAKER.get(label)
        if state is None:
            state = {"fail_count": 0, "last_fail_ts": 0, "last_probe_ts": 0, "is_open": False}
            _CIRCUIT_BREAKER[label] = state
        return state

def _circuit_is_tripped(label: str) -> bool:
    """
    检查路由是否被熔断。
    如果熔断且距上次失败超过半开间隔，允许一次探测请求。
    返回 True = 熔断中，应跳过此路由。
    """
    state = _circuit_get_state(label)
    if not state["is_open"]:
        return False
    now = time.time()
    # 半开恢复: 距上次失败超过 half_open_interval 秒，允许探测
    if now - state["last_fail_ts"] > _CIRCUIT_CONFIG["half_open_interval"]:
        state["last_probe_ts"] = now
        state["is_open"] = False  # 临时关闭 + 标记为探测
        log(f"CIRCUIT: half-open probe allowed for {label}")
        return False  # 放行探测请求
    return True

def _circuit_record(label: str, success: bool):
    """
    记录路由调用结果，更新熔断状态。
    集成在 evolution_learn 中调用。
    """
    global _CIRCUIT_BREAKER_TRIGGERED
    state = _circuit_get_state(label)
    now = time.time()
    if success:
        # 检测是否为半开探测成功：last_probe_ts 在 2 秒内设定过
        half_open_success = (now - state.get("last_probe_ts", 0)) < 2.0
        if state["is_open"] or half_open_success:
            # 半开状态下成功 → 关闭熔断
            state["is_open"] = False
            state["fail_count"] = 0
            log(f"CIRCUIT: closed for {label} (probe succeeded)")
        else:
            state["fail_count"] = max(0, state["fail_count"] - 1)
    else:
        state["fail_count"] += 1
        state["last_fail_ts"] = now
        if state["fail_count"] >= _CIRCUIT_CONFIG["consecutive_fail_threshold"]:
            if not state["is_open"]:
                state["is_open"] = True
                _CIRCUIT_BREAKER_TRIGGERED += 1
                log(f"CIRCUIT: TRIPPED for {label} (consecutive failures: {state['fail_count']})")


def evolution_learn(label: str, latency: float, success: bool):
    """
    记录路由决策，积累成功模式。
    success=True → 积分+1，记录模式
    success=False → 积分-3，记录失败
    """
    global _EVOLUTION_SCORE
    entry = {
        "route": label,
        "latency": round(latency, 2),
        "success": success,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _EVOLUTION_LOG.append(entry)
    # 只保留最近 500 条
    if len(_EVOLUTION_LOG) > 500:
        _EVOLUTION_LOG.pop(0)

    if success:
        _EVOLUTION_SCORE += 1
        # 任务5: 路由熔断 — 成功时记录
        _circuit_record(label, True)
        # 记录模式：同样的 prompt 前缀命中同一个路由 -> 成功模式
        if label not in [e.get("route") for e in _EVOLUTION_PATTERNS]:
            _EVOLUTION_PATTERNS.append({"route": label, "count": 1, "last": entry})
        else:
            for p in _EVOLUTION_PATTERNS:
                if p["route"] == label:
                    p["count"] += 1
                    p["last"] = entry
                    break
    else:
        _EVOLUTION_SCORE = max(_EVOLUTION_SCORE - 3, -50)
        # 任务5: 路由熔断 — 失败时记录
        _circuit_record(label, False)

    # 持久化
    _evolution_persist()


def _evolution_persist():
    try:
        EVOLUTION_PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EVOLUTION_PERSIST_PATH, "w") as f:
            json.dump({
                "score": _EVOLUTION_SCORE,
                "patterns": _EVOLUTION_PATTERNS[-20:],
                "total_decisions": len(_EVOLUTION_LOG),
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _evolution_load():
    global _EVOLUTION_SCORE, _EVOLUTION_PATTERNS, _EVOLUTION_LOG
    try:
        if EVOLUTION_PERSIST_PATH.exists():
            with open(EVOLUTION_PERSIST_PATH) as f:
                data = json.load(f)
                _EVOLUTION_SCORE = data.get("score", 0)
                _EVOLUTION_PATTERNS = data.get("patterns", [])
    except Exception:
        pass


# 合并 Expert Delegation Protocol 的精髓：委托元数据
def _build_delegation_meta(model: str, provider: str, label: str) -> dict:
    return {
        "delegated_to": label,
        "provider": provider,
        "model": model,
        "schema": "expert-delegation-v1",
    }


# ══════════════════════════════════════════
# 统计
# ══════════════════════════════════════════

def _record_stats(provider: str, label: str, latency: float, is_stream: bool):
    with _spend_lock:
        _spend["calls"] += 1
        _spend["by_provider"][provider] = _spend["by_provider"].get(provider, 0) + 1


def _record_error(provider: str, label: str, error: str, latency: float):
    with _spend_lock:
        _spend["errors"] += 1
    log(f"ERROR: {label} | {error} | {latency:.2f}s")


# ══════════════════════════════════════════
# HTTP Server
# ══════════════════════════════════════════

class ThalamusHandler(BaseHTTPRequestHandler):

    MAX_BODY_SIZE = 10 * 1024 * 1024  # 10MB

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        if length > self.MAX_BODY_SIZE:
            raise ValueError(f"Request body too large ({length} > {self.MAX_BODY_SIZE})")
        raw = self.rfile.read(length)
        return json.loads(raw)

    def _send_json(self, code: int, data: dict, extra_headers: dict | None = None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                # HTTP headers must be latin-1 safe; encode non-ASCII values
                if isinstance(v, str):
                    v = v.encode("latin-1", errors="replace").decode("latin-1")
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: int, message: str):
        self._send_json(code, {"error": message})

    def do_GET(self):
        if self.path in ("/", ""):
            # Root → admin panel
            self._serve_admin_html()
        elif self.path in ("/terminal", "/terminal/"):
            # Terminal hacker view
            self._serve_terminal_html()
        elif self.path in ("/health", "/health/"):
            uptime = int(time.time() - START_TIME)
            self._send_json(200, {
                "status": "ok",
                "name": "thalamus",
                "version": "4.0.0",
                "uptime_seconds": uptime,
                "calls": _spend["calls"],
                "fallbacks": _spend["fallbacks"],
                "errors": _spend["errors"],
                "routes": [{"label": r[5], "provider": r[2]} for r in ROUTES],
                "default": "deepseek-chat",
            })
        elif self.path in ("/semantic-debug", "/semantic-debug/"):
            self._serve_semantic_debug()
        elif self.path in ("/stats", "/stats/"):
            self._send_json(200, {
                "uptime_seconds": int(time.time() - START_TIME),
                "calls": _spend["calls"],
                "fallbacks": _spend["fallbacks"],
                "errors": _spend["errors"],
                "by_provider": dict(_spend["by_provider"]),
                "total_tokens": _spend["total_tokens"],
                "token_by_provider": dict(_spend["token_by_provider"]),
                "token_by_label": dict(_spend["token_by_label"]),
                "evolution_score": _EVOLUTION_SCORE,
                "evolution_decisions": len(_EVOLUTION_LOG),
                "evolution_patterns": len(_EVOLUTION_PATTERNS),
                "precheck_timeouts": _spend.get("precheck_timeouts", 0),
                "precheck_errors": _spend.get("precheck_errors", 0),
                "precheck_cache_hits": _PRECHECK_CACHE_HITS,
                "precheck_cache_misses": _PRECHECK_CACHE_MISSES,
                "circuit_breaker_skips": _spend.get("circuit_breaker_skips", 0),
                "circuit_breaker_triggered": _CIRCUIT_BREAKER_TRIGGERED,
                "circuit_breaker_open_routes": [
                    {"label": k, "fail_count": v["fail_count"]}
                    for k, v in _CIRCUIT_BREAKER.items() if v.get("is_open")
                ],
                "berserker_calls": _spend.get("berserker_calls", 0),
                "berserker_tokens": _spend.get("berserker_tokens", 0),
                "berserker_perspectives": _spend.get("berserker_perspectives", 0),
                "berserker_total_chars": _spend.get("berserker_total_chars", 0),
                "rate_limit_config": _RATE_LIMIT_CONFIG,
                "health_probes": dict(_HEALTH_PROBE_RESULTS) if _HEALTH_PROBE_RESULTS else None,
                "latency_percentiles": _get_latency_percentiles(),
            })
        elif self.path in ("/metrics", "/metrics/"):
            prometheus_text = _generate_prometheus_metrics()
            body = prometheus_text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/stats/reset", "/stats/reset/"):
            with _spend_lock:
                _snapshot = {
                    "total_tokens": _spend["total_tokens"],
                    "token_by_provider": dict(_spend["token_by_provider"]),
                    "token_by_label": dict(_spend["token_by_label"]),
                    "calls": _spend["calls"],
                }
                with _COST_PERF_LOG_LOCK:
                    _COST_PERF_LOG.clear()
                _spend["total_tokens"] = 0
                _spend["token_by_provider"] = {}
                _spend["token_by_label"] = {}
                _spend["calls"] = 0
            self._send_json(200, {"status": "reset", "previous": _snapshot})
        elif self.path in ("/cost-performance", "/cost-performance/"):
            with _COST_PERF_LOG_LOCK:
                _summary = {}
                _grand_total_cost = 0.0
                _grand_total_tokens = 0
                for entry in _COST_PERF_LOG:
                    key = entry["label"]
                    if key not in _summary:
                        _summary[key] = {"calls": 0, "total_cost": 0.0, "total_latency": 0.0, "total_tokens": 0}
                    _summary[key]["calls"] += 1
                    _summary[key]["total_cost"] += entry["est_cost"]
                    _summary[key]["total_latency"] += entry["latency"]
                    _summary[key]["total_tokens"] += entry["input_tok"] + entry["output_tok"]
                    _grand_total_cost += entry["est_cost"]
                    _grand_total_tokens += entry["input_tok"] + entry["output_tok"]
                _est_weekly = round(_grand_total_cost * 7 * (1 if len(_COST_PERF_LOG) == 0 else 200 / max(len(_COST_PERF_LOG), 1)), 6)
                _est_monthly = round(_grand_total_cost * 30 * (1 if len(_COST_PERF_LOG) == 0 else 200 / max(len(_COST_PERF_LOG), 1)), 6)
                for k, v in _summary.items():
                    c = v["calls"]
                    v["avg_latency"] = round(v["total_latency"] / c, 2)
                    v["avg_cost"] = round(v["total_cost"] / c, 6)
                    v["cost_per_1k_tokens"] = round(v["total_cost"] / (v["total_tokens"] / 1000), 6) if v["total_tokens"] > 0 else 0
                    del v["total_cost"]
                    del v["total_latency"]
                data = {
                    "summary": _summary,
                    "recent": _COST_PERF_LOG[-20:],
                    "estimates": {
                        "total_cost_current_window": round(_grand_total_cost, 6),
                        "total_tokens_current_window": _grand_total_tokens,
                        "estimated_weekly_cost": _est_weekly,
                        "estimated_monthly_cost": _est_monthly,
                    },
                }
            self._send_json(200, data)
        elif self.path.startswith("/semantic-debug"):
            from semantic_router import get_debug_info
            import urllib.parse
            query = urllib.parse.parse_qs(self.path.split('?', 1)[1]).get('q', [''])[0] if '?' in self.path else ''
            if not query:
                self._send_json(400, {"error": "missing ?q= parameter"})
                return
            debug = get_debug_info(query)
            self._send_json(200, debug)
        elif self.path in ("/analysis", "/analysis/"):
            self._send_json(200, {
                "endpoint": "/analysis (POST)",
                "description": "Multi-model parallel analysis — calls 3 models simultaneously",
                "usage": 'POST /analysis with {"prompt": "your question"}',
                "examples": {
                    "code": "分析了代码问题 → MiMo 2.5 Pro",
                    "reasoning": "逻辑推理 → OpenRouter Auto",
                    "creative": "创意方案 → DeepSeek Chat",
                },
            })
        elif self.path in ("/evolution", "/evolution/"):
            self._send_json(200, {
                "score": _EVOLUTION_SCORE,
                "patterns": _EVOLUTION_PATTERNS[-10:],
                "total_decisions": len(_EVOLUTION_LOG),
            })
        elif self.path.startswith("/admin"):
            self._handle_admin_get()
        elif self.path in ("/v1/models", "/v1/models/"):
            # Return OpenAI-compatible model list for context-length discovery
            models = []
            for r in ROUTES:
                models.append({
                    "id": r[1],
                    "object": "model",
                    "created": int(START_TIME),
                    "owned_by": r[2],
                    "max_tokens": 1048576,  # 1M context
                })
            # Also add common models
            seen = {m["id"] for m in models}
            for mid, prov in [("deepseek-v4-flash", "deepseek"), ("deepseek-v4-pro", "deepseek")]:
                if mid not in seen:
                    models.append({"id": mid, "object": "model", "created": 1740000000, "owned_by": prov, "max_tokens": 1048576})
            self._send_json(200, {"data": models, "object": "list"})
        else:
            self._send_json(200, {
                "name": "thalamus",
                "version": "4.0.0",
                "endpoints": {
                    "/v1/chat/completions": "OpenAI-compatible proxy (streaming + non-streaming)",
                    "/task": "Legacy task endpoint",
                    "/parallel": "Parallel multi-model",
                    "/health": "Health check",
                    "/stats": "Call statistics",
                },
                "fallback": "deepseek-chat",
            })

    def do_POST(self):
        # 解析请求体
        try:
            body = self._read_body()
        except json.JSONDecodeError:
            self._send_error(400, "Invalid JSON body")
            return
        except ValueError as e:
            self._send_error(413, str(e))
            return
        except Exception:
            self._send_error(400, "Failed to read request body")
            return

        # 速率限制检查（仅限 chat 和 task 端点）
        if self.path in ("/v1/chat/completions", "/v1/chat/completions/", "/task", "/task/"):
            client_ip = self.client_address[0]
            allowed, reason = _rate_limit_check(client_ip)
            if not allowed:
                log(f"RATE LIMIT: {client_ip} blocked: {reason}")
                self._send_error(429, f"Too many requests: {reason}")
                return
            # 完成后释放并发槽（函数末尾或异常处理）
            try:
                if self.path in ("/v1/chat/completions", "/v1/chat/completions/"):
                    self._handle_chat_completion(body)
                else:
                    self._handle_task(body)
            finally:
                _rate_limit_release(client_ip)
            return
        elif self.path in ("/parallel", "/parallel/"):
            self._handle_parallel(body)
        elif self.path in ("/analysis", "/analysis/"):
            self._handle_analysis(body)
        elif self.path in ("/v1/berserker", "/v1/berserker/"):
            self._handle_berserker(body)
        elif self.path.startswith("/admin"):
            self._handle_admin_post(body)
        else:
            self._send_error(404, "Not found")

    def _handle_chat_completion(self, body: dict):
        """处理 /v1/chat/completions — 透明代理"""
        try:
            mode, is_stream, result = process(body)
        except Exception as e:
            # 所有路由+fallback 失败
            traceback.print_exc(file=sys.stderr)
            log(f"FATAL: all routes exhausted: {e}")
            if body.get("stream", False):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(
                    f'data: {{"error": "Thalamus: all backends unavailable: {e}"}}\n\n'.encode()
                )
                self.wfile.write("data: [DONE]\n\n".encode())
            else:
                self._send_error(502, f"All backends unavailable: {e}")
            return

        if is_stream:
            # ─── 流式响应 ───
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Thalamus-Version", "3.0.0")
            self.end_headers()

            try:
                chunk_count = 0
                for chunk in result:
                    data = chunk if isinstance(chunk, bytes) else chunk.encode()
                    self.wfile.write(data)
                    self.wfile.flush()
                    chunk_count += 1
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"STREAM WRITE ERROR: {e}")
            # 流式模式下不发送额外数据
        else:
            # ─── 非流式响应 ───
            self._send_json(200, result, extra_headers={
                "X-Thalamus-Version": "3.0.0",
                "X-Thalamus-Route": result.get("_thalamus", {}).get("routed_to", "unknown"),
            })

    def _handle_task(self, body: dict):
        """旧版 /task 接口"""
        prompt = body.get("prompt", "")
        if not prompt:
            self._send_error(400, "Missing 'prompt' field")
            return

        req_body = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": body.get("max_tokens", 16384),
            "temperature": body.get("temperature", 0.7),
            "stream": False,
        }

        try:
            mode, is_stream, result = process(req_body)
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            thalamus_meta = result.get("_thalamus", {})
            self._send_json(200, {
                "routing": {
                    "label": thalamus_meta.get("routed_to", "unknown"),
                    "provider": thalamus_meta.get("provider", "unknown"),
                },
                "result": {
                    "model": result.get("model", "?"),
                    "content": content,
                },
            })
        except Exception as e:
            self._send_error(502, str(e))

    def _handle_parallel(self, body: dict):
        """旧版 /parallel 接口"""
        tasks = body.get("tasks", [])
        if not tasks:
            self._send_error(400, "Missing 'tasks' list")
            return

        MAX_PARALLEL = 10
        if len(tasks) > MAX_PARALLEL:
            self._send_error(400, f"Too many tasks ({len(tasks)} > {MAX_PARALLEL})")
            return

        results = [None] * len(tasks)
        errors = [None] * len(tasks)
        threads = []

        def worker(idx, task):
            try:
                req_body = {
                    "messages": [{"role": "user", "content": task.get("prompt", task.get("goal", ""))}],
                    "max_tokens": task.get("max_tokens", 16384),
                    "temperature": task.get("temperature", 0.7),
                    "stream": False,
                }
                mode, is_stream, result = process(req_body)
                results[idx] = result
            except Exception as e:
                errors[idx] = str(e)
                results[idx] = None

        for i, t in enumerate(tasks):
            th = threading.Thread(target=worker, args=(i, t), daemon=True)
            threads.append(th)
            th.start()

        for th in threads:
            th.join(timeout=180)

        self._send_json(200, {
            "results": results,
            "errors": errors,
        })

    def _handle_analysis(self, body: dict):
        """多模型平行分析 — /analysis 端点"""
        prompt = body.get("prompt", "")
        if not prompt:
            self._send_error(400, "Missing 'prompt' field")
            return

        try:
            result = multi_analysis(prompt, max_tokens=body.get("max_tokens", 4096))
            self._send_json(200, result)
        except Exception as e:
            self._send_error(502, str(e))

    def _handle_berserker(self, body: dict):
        """狂暴模式 — 多模型平行分析 + Aggregator 汇总"""
        prompt = body.get("prompt", "")
        if not prompt:
            self._send_error(400, "Missing 'prompt' field")
            return

        confirm = body.get("confirm", False)

        try:
            if not confirm:
                # 阶段 1: 估算 Token
                est_chars = len(prompt) * 6  # 3 models × 2 responses each
                est_tokens = max(1000, est_chars // 2)
                est_cost = est_tokens / 1_000_000 * 0.014  # deepseek rate
                self._send_json(200, {
                    "stage": "estimate",
                    "prompt": prompt[:200],
                    "estimated_tokens": est_tokens,
                    "estimated_cost_usd": round(est_cost, 6),
                    "estimated_time_s": 45,
                    "confirm_required": True,
                    "confirm_url": "/v1/berserker",
                    "message": f"该请求预计消耗 ~{est_tokens} tokens (≈${est_cost:.6f})，约 45s，发送相同请求并带 confirm=true 确认执行"
                })
                return

            # 阶段 2: 执行多模型分析
            log(f"BERSERKER: starting multi-analysis for '{prompt[:60]}...'")
            analysis_result = multi_analysis(prompt, max_tokens=body.get("max_tokens", 2048))

            if analysis_result.get("error_count", 0) > 0:
                self._send_json(502, {"error": "Multi-analysis failed", "details": analysis_result.get("errors", {})})
                return

            # 阶段 3: Aggregator 汇总
            log("BERSERKER: aggregating perspectives...")
            perspectives = analysis_result["perspectives"]
            agg_content = "\n\n".join(
                f"## {label.upper()}\n{perspectives[label]}"
                for label in ["code", "reasoning", "creative"]
            )

            agg_prompt = f"""你是一个资深的策略汇总专家。以下是对同一个问题从 3 个不同角度分析的结论。

你的任务：综合 3 份分析，写出一份结构清晰、没有冗余、有深度但没有废话的最终报告。
不要逐条列"角度A说..."，而是融合它们，按主题组织。

## 分析主题
{prompt}

{agg_content}

请输出综合报告（不需要再分段展示各视角的内容）："""

            agg_body = {
                "model": DEFAULT_MODEL,
                "messages": [
                    {"role": "system", "content": "你是一个资深的策略汇总专家。擅长融合多角度观点，输出简洁、有洞察力的综合报告。"},
                    {"role": "user", "content": agg_prompt}
                ],
                "max_tokens": body.get("max_tokens", 3072) or 3072,
                "stream": False,
            }

            agg_key = _resolve_key(DEFAULT_KEY_ENV)
            agg_data = _make_request(DEFAULT_ENDPOINT, agg_body, agg_key, stream=False)
            agg_content = agg_data.get("choices", [{}])[0].get("message", {}).get("content", "")

            total_tokens = (
                analysis_result.get("count", 0) +
                agg_data.get("usage", {}).get("total_tokens", 0)
            )

            # 统计
            with _spend_lock:
                _spend["berserker_calls"] = _spend.get("berserker_calls", 0) + 1
                _spend["berserker_tokens"] = _spend.get("berserker_tokens", 0) + total_tokens
                _spend["berserker_perspectives"] = _spend.get("berserker_perspectives", 0) + analysis_result.get("count", 0)
                _spend["berserker_total_chars"] = _spend.get("berserker_total_chars", 0) + len(agg_content)

            log(f"BERSERKER: done — {analysis_result.get('count', 0)} perspectives, {len(agg_content)} chars aggregated")
            self._send_json(200, {
                "stage": "complete",
                "prompt": prompt[:200],
                "perspectives": {
                    k: {"content": v, "chars": len(v)}
                    for k, v in perspectives.items()
                },
                "aggregated": agg_content,
                "total_perspectives": analysis_result.get("count", 0),
                "aggregated_chars": len(agg_content),
                "estimated_tokens": body.get("_estimated_tokens", 0),
            })

        except Exception as e:
            log(f"BERSERKER ERROR: {e}")
            import traceback
            log(traceback.format_exc())
            self._send_error(502, f"Berserker failed: {e}")

    # ─── Admin panel handlers ───

    def _check_admin_auth(self) -> bool:
        now = time.time()
        # Clean expired tokens
        expired = [t for t, exp in list(ADMIN_TOKEN.items()) if exp < now]
        for t in expired:
            ADMIN_TOKEN.pop(t, None)

        # Check Authorization header first
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] in ADMIN_TOKEN:
            return True

        # Fallback: check cookie
        cookie = self.headers.get("Cookie", "")
        for pair in cookie.split(";"):
            pair = pair.strip()
            if pair.startswith("admin_token="):
                tok = pair[12:]
                if tok in ADMIN_TOKEN:
                    return True
        return False

    def _check_https_admin(self) -> bool:
        """Enforce HTTPS for admin endpoints. Returns True if HTTPS or loopback."""
        # Allow localhost without HTTPS
        client_ip = self.client_address[0]
        if client_ip in ("127.0.0.1", "::1", "localhost"):
            return True
        # Check X-Forwarded-Proto header
        proto = self.headers.get("X-Forwarded-Proto", "").lower()
        if proto == "https":
            return True
        # Not HTTPS → send 426 Upgrade Required
        self.send_response(426)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Upgrade", "TLS/1.2, HTTP/1.1")
        self.send_header("Connection", "Upgrade")
        body = json.dumps({"error": "HTTPS required", "code": 426}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        _structured_log("WARN", "AUTH_LOGIN", {"event": "https_blocked", "ip": client_ip, "path": self.path})
        return False

    def _check_login_rate_limit(self, client_ip: str) -> tuple[bool, str | None]:
        """Check login rate limit for client_ip.
        Returns: (allowed: bool, error_message: str | None)
        """
        now = time.time()
        with _LOGIN_RATE_LIMIT_LOCK:
            entry = _LOGIN_RATE_LIMIT.get(client_ip)
            # Check ban
            if entry and entry.get("banned_until", 0) > now:
                remaining = int(entry["banned_until"] - now)
                return (False, f"Account temporarily locked. Try again in {remaining}s.")
            if not entry or (now - entry.get("window_start", 0)) > _LOGIN_RATE_LIMIT_CONFIG["window_seconds"]:
                # New window
                _LOGIN_RATE_LIMIT[client_ip] = {
                    "fail_count": 0,
                    "window_start": now,
                    "banned_until": 0,
                }
                return (True, None)
            return (True, None)

    def _record_login_failure(self, client_ip: str):
        """Record a login failure and check if ban should be applied."""
        now = time.time()
        with _LOGIN_RATE_LIMIT_LOCK:
            entry = _LOGIN_RATE_LIMIT.get(client_ip)
            if not entry or (now - entry.get("window_start", 0)) > _LOGIN_RATE_LIMIT_CONFIG["window_seconds"]:
                entry = {
                    "fail_count": 1,
                    "window_start": now,
                    "banned_until": 0,
                }
                _LOGIN_RATE_LIMIT[client_ip] = entry
            else:
                entry["fail_count"] += 1
            # Check window limit
            if entry["fail_count"] >= _LOGIN_RATE_LIMIT_CONFIG["ban_threshold"]:
                entry["banned_until"] = now + _LOGIN_RATE_LIMIT_CONFIG["ban_duration"]
                log(f"LOGIN RATE LIMIT: {client_ip} banned for {_LOGIN_RATE_LIMIT_CONFIG['ban_duration']}s after {entry['fail_count']} failures")
            elif entry["fail_count"] >= _LOGIN_RATE_LIMIT_CONFIG["max_fail_per_window"]:
                # Reset window to prevent more attempts in this window
                entry["window_start"] = now
                log(f"LOGIN RATE LIMIT: {client_ip} rate limited after {entry['fail_count']} failures in window")

    def _serve_admin_html(self):
        try:
            html = ADMIN_HTML_PATH.read_text(encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode())))
            self.end_headers()
            self.wfile.write(html.encode())
        except Exception as e:
            self._send_error(500, f"Failed to load admin page: {e}")

    TERMINAL_HTML_PATH = Path("/root/api-hvh-terminal/index.html")

    def _serve_terminal_html(self):
        try:
            html = self.TERMINAL_HTML_PATH.read_text(encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode())))
            self.end_headers()
            self.wfile.write(html.encode())
        except Exception as e:
            self._send_error(500, f"Failed to load terminal page: {e}")

    def _handle_admin_get(self):
        # HTTPS enforcement for all admin endpoints
        if not self._check_https_admin():
            return
        if self.path in ("/admin/", "/admin"):
            self._serve_admin_html()
        elif self.path in ("/admin/api/config", "/admin/api/config/"):
            if not self._check_admin_auth():
                self._send_error(401, "Unauthorized")
                return
            cfg = json.loads(ROUTES_PATH.read_text())
            # Add version
            cfg["version"] = "4.0.0"
            # Split keys into custom (manually added in keys.json) and env (from environment variables)
            custom_keys = {}
            env_keys = {}
            # All keys from keys.json are custom_keys — they have stored values
            for k, v in KEYS.items():
                custom_keys[k] = {
                    "key": v.get("key", ""),
                    "endpoint": v.get("endpoint", ""),
                }

            # Scan environment for all API-key/token/credential env vars not already in KEYS
            seen_aliases = set(KEYS.keys())
            # Auto-detect all env vars that look like API keys / tokens / secrets
            KEY_ENV_PATTERN = re.compile(
                r'(API_KEY|APITOKEN|_TOKEN|_SECRET|_PASSWORD|_CREDENTIAL|CLIENT_SECRET|'
                r'ACCESS_TOKEN|_KEY|BOT_TOKEN|WEBHOOK_SECRET|VERIFICATION_TOKEN|'
                r'APP_SECRET|SERVICE_ACCOUNT|PRIVATE_KEY)',
                re.IGNORECASE
            )
            for env_name in sorted(os.environ.keys()):
                val = os.environ[env_name]
                if not val or len(val) < 6 or val == "***":
                    continue
                if not KEY_ENV_PATTERN.search(env_name):
                    continue
                # Derive a friendly alias
                alias = env_name.lower().replace('_api_key', '').replace('_token', '').replace('_secret', '').replace('_key', '')
                if alias in seen_aliases:
                    continue
                env_keys[alias] = {
                    "endpoint": "",
                    "prefix": val[:4] + "****" + val[-4:],
                    "env_var": env_name,
                }
                seen_aliases.add(alias)
                if len(env_keys) >= 30:  # cap at 30 to avoid flooding the panel
                    break

            cfg["custom_keys"] = custom_keys
            cfg["env_keys"] = env_keys
            self._send_json(200, cfg)
        elif self.path in ("/admin/api/stats", "/admin/api/stats/"):
            if not self._check_admin_auth():
                self._send_error(401, "Unauthorized")
                return
            self._send_json(200, {
                "uptime_seconds": int(time.time() - START_TIME),
                "calls": _spend["calls"],
                "fallbacks": _spend["fallbacks"],
                "errors": _spend["errors"],
                "by_provider": dict(_spend["by_provider"]),
                "total_tokens": _spend["total_tokens"],
                "token_by_provider": dict(_spend["token_by_provider"]),
                "token_by_label": dict(_spend["token_by_label"]),
            })
        elif self.path in ("/admin/api/balances", "/admin/api/balances/"):
            if not self._check_admin_auth():
                self._send_error(401, "Unauthorized")
                return
            balances = {}
            # DeepSeek — read key from .env (different from .credentials)
            try:
                env_path = Path("/root/.hermes/.env")
                dk = ""
                if env_path.exists():
                    for line in env_path.read_text().splitlines():
                        if line.startswith("DEEPSEEK_API_KEY") and "=" in line:
                            dk = line.split("=", 1)[1].strip()
                            break
                if dk and dk != "***" and len(dk) > 20:
                    req = urllib.request.Request("https://api.deepseek.com/user/balance",
                        headers={"Authorization": f"Bearer {dk}"})
                    resp = urllib.request.urlopen(req, timeout=10)
                    data = json.loads(resp.read())
                    balances["deepseek"] = data
            except Exception as e:
                balances["deepseek"] = {"error": str(e)[:100]}
            # OpenRouter
            try:
                ork = os.environ.get("OPENROUTER_API_KEY", "")
                if ork:
                    req = urllib.request.Request("https://openrouter.ai/api/v1/auth/key",
                        headers={"Authorization": f"Bearer {ork}"})
                    resp = urllib.request.urlopen(req, timeout=10)
                    data = json.loads(resp.read())
                    balances["openrouter"] = data.get("data", {})
            except Exception as e:
                balances["openrouter"] = {"error": str(e)[:100]}
            # Token173 — tokens from local tracking (no external balance API)
            balances["token173"] = {
                "total_tokens": _spend.get("token_by_provider", {}).get("token173", 0),
            }
            self._send_json(200, balances)
        elif self.path in ("/admin/api/logs", "/admin/api/logs/"):
            if not self._check_admin_auth():
                self._send_error(401, "Unauthorized")
                return
            try:
                lines_count = 100
                if LOG_PATH.exists():
                    with open(LOG_PATH) as f:
                        all_lines = f.readlines()
                        tail = all_lines[-lines_count:]
                    self._send_json(200, {"lines": tail, "total": len(all_lines)})
                else:
                    self._send_json(200, {"lines": [], "total": 0})
            except Exception as e:
                self._send_error(500, str(e))
        else:
            self._send_error(404, "Not found")

    def _handle_admin_post(self, body: dict):
        path = self.path.rstrip("/")

        # HTTPS enforcement for all admin endpoints
        if not self._check_https_admin():
            return

        if path == "/admin/api/login":
            client_ip = self.client_address[0]
            # Rate limit check
            allowed, err_msg = self._check_login_rate_limit(client_ip)
            if not allowed:
                log(f"LOGIN RATE LIMIT: {client_ip} denied: {err_msg}")
                self._send_error(429, err_msg or "Too many login attempts")
                _structured_log("WARN", "AUTH_LOGIN", {"event": "login_rate_limited", "ip": client_ip})
                return
            pwd = body.get("password", "")
            stored = _get_admin_password()
            if not stored:
                _structured_log("ERROR", "AUTH_LOGIN", {"event": "login_failed", "ip": client_ip, "reason": "no_password_configured"})
                self._send_error(500, "No admin password configured")
                return
            # Simple timing-safe comparison
            if secrets.compare_digest(pwd.encode(), stored.encode()):
                token = secrets.token_urlsafe(32)
                ADMIN_TOKEN[token] = time.time() + 86400  # 24h expiry
                _structured_log("INFO", "AUTH_LOGIN", {"event": "login_success", "ip": client_ip})
                log(f"Admin login success: {client_ip}")
                self._send_json(200, {"token": token}, extra_headers={
                    "Set-Cookie": f"admin_token={token}; Path=/; Max-Age=604800; SameSite=Lax"
                })
            else:
                self._record_login_failure(client_ip)
                _structured_log("WARN", "AUTH_LOGIN", {"event": "login_failed", "ip": client_ip, "reason": "invalid_password"})
                log(f"Admin login failed: {client_ip}")
                self._send_error(401, "Invalid password")
            return

        if not self._check_admin_auth():
            self._send_error(401, "Unauthorized")
            return

        # ── 密钥管理 API ──
        if path in ("/admin/api/keys/add", "/admin/api/keys/add/"):
            return self._handle_key_add(body)
        if path in ("/admin/api/keys/delete", "/admin/api/keys/delete/"):
            return self._handle_key_delete(body)
        if path in ("/admin/api/keys/probe", "/admin/api/keys/probe/"):
            return self._handle_key_probe(body)

        # ── 路由/系统配置 API (旧版嵌套 action 兼容) ──
        if path in ("/admin/api/config", "/admin/api/config/"):
            return self._handle_config_update(body)

        if path in ("/admin/api/reload", "/admin/api/reload/"):
            try:
                _load_routes()
                _load_keys()
                log("Admin: configuration reloaded")
                self._send_json(200, {"status": "ok", "message": "Configuration reloaded"})
            except Exception as e:
                log(f"Admin reload error: {e}")
                self._send_error(500, str(e))
            return

        # ── 重启 Thalamus 服务 ──
        if path in ("/admin/api/restart", "/admin/api/restart/"):
            log("Admin: restart requested via admin panel")
            self._send_json(200, {"status": "ok", "message": "Restarting..."})
            # Small delay so the response has time to reach the browser
            threading.Thread(target=lambda: (
                time.sleep(1),
                os.kill(os.getpid(), 9 if hasattr(signal, 'SIGKILL') else 15)
            )).start()
            return

        self._send_error(404, "Not found")

    # ─── 密钥 CRUD ───

    def _handle_key_add(self, body: dict):
        name = body.get("name", "").strip()
        key = body.get("key", "").strip()
        endpoint = body.get("endpoint", "").strip()
        if not name or not key:
            self._send_error(400, "Missing required fields: name, key")
            return
        KEYS[name] = {"key": key, "endpoint": endpoint or "https://api.deepseek.com/v1/chat/completions"}
        _save_keys(KEYS)
        log(f"Admin: saved API key '{name}'")
        self._send_json(200, {"status": "ok", "keys": list(KEYS.keys())})

    def _handle_key_delete(self, body: dict):
        name = body.get("name", "").strip()
        if not name or name not in KEYS:
            self._send_error(404, f"Key '{name}' not found")
            return
        # Check cascade — which routes/configs use this key?
        affected = []
        try:
            cfg = json.loads(ROUTES_PATH.read_text())
            for i, r in enumerate(cfg.get("routes", [])):
                if r.get("key_env") == name:
                    affected.append({"type": "route", "name": r.get("label", f"route#{i}")})
            for section in ["default", "fallback", "precheck"]:
                sec = cfg.get(section, {})
                if sec.get("key_env") == name:
                    affected.append({"type": "config", "name": section})
        except Exception:
            pass

        del KEYS[name]
        _save_keys(KEYS)
        log(f"Admin: deleted API key '{name}'")
        self._send_json(200, {"status": "ok", "affected": affected})

    def _handle_key_probe(self, body: dict):
        """Probe a key's endpoint for available models. 60s throttle."""
        name = body.get("name", "").strip()
        if not name or name not in KEYS:
            self._send_error(404, f"Key '{name}' not found")
            return
        entry = KEYS[name]
        # Build /v1/models URL safely (removesuffix, not replace — avoid eating /v1 in path)
        ep = entry["endpoint"].rstrip("/")
        for suffix in ("/chat/completions", "/v1"):
            if ep.endswith(suffix):
                ep = ep[:-len(suffix)]
        endpoint_base = ep + "/v1/models"

        # Throttle
        now = time.time()
        cache_key = f"_probe_{name}"
        last_probe = _spend.get(cache_key, 0)
        if now - last_probe < 60:
            self._send_error(429, "Probe cooldown (60s). Please wait.")
            return

        # Use curl for probe (urllib's TLS fingerprint gets blocked by nginx/cloudflare)
        models = []
        try:
            result = subprocess.run(
                ["curl", "-s", "--max-time", "15", endpoint_base,
                 "-H", f"Authorization: Bearer {entry['key']}"],
                capture_output=True, text=True, timeout=20
            )
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    if mid:
                        tags = _tag_model(mid)
                        models.append({"id": mid, "object": m.get("object", "model"), "tags": tags})
        except Exception as e:
            log(f"Probe failed for '{name}': {e}")

        if models:
            _spend[cache_key] = now
            self._send_json(200, {"models": models})
        else:
            self._send_error(502, "Probe failed: no models returned")

    # ─── 路由/系统配置 ───

    def _handle_config_update(self, body: dict):
        action = body.get("action", "")
        try:
            cfg = json.loads(ROUTES_PATH.read_text())
            if action == "upsert_route":
                route = body.get("route", {})
                pattern_raw = route.get("pattern", "")
                # Validate regex
                try:
                    re.compile(pattern_raw)
                except re.error as e:
                    self._send_error(400, f"Invalid regex pattern: {e}")
                    return
                entry = {
                    "label": route.get("label", ""),
                    "pattern": pattern_raw,
                    "model": route.get("model", ""),
                    "provider": route.get("provider", ""),
                    "endpoint": route.get("endpoint", ""),
                    "key_env": route.get("key_env", ""),
                }
                routes = cfg["routes"]
                idx = body.get("index")
                if idx is not None and 0 <= idx < len(routes):
                    routes[idx] = entry
                else:
                    routes.append(entry)
                cfg["routes"] = routes
            elif action == "delete_route":
                idx = body.get("index", -1)
                routes = cfg.get("routes", [])
                if 0 <= idx < len(routes):
                    routes.pop(idx)
                cfg["routes"] = routes
            elif action == "upsert_key":
                name = body.get("key_name", "")
                value = body.get("key_value", "")
                endpoint = body.get("endpoint", "")
                if not name or not value:
                    self._send_error(400, "Missing key_name or key_value")
                    return
                KEYS[name] = {"key": value, "endpoint": endpoint or "https://api.deepseek.com/v1/chat/completions"}
                _save_keys(KEYS)
                log(f"Admin: saved API key '{name}'")
                self._send_json(200, {"status": "ok", "keys": list(KEYS.keys())})
                return
            elif action == "delete_key":
                name = body.get("key_name", "")
                if name in KEYS:
                    KEYS.pop(name)
                    _save_keys(KEYS)
                    log(f"Admin: deleted API key '{name}'")
                self._send_json(200, {"status": "ok"})
                return
            elif action == "update_defaults":
                if "routes" in body:
                    cfg["routes"] = body["routes"]
                if "default" in body:
                    d = body["default"]
                    current_def = cfg.get("default", {})
                    cfg["default"] = {
                        "model": d.get("model", current_def.get("model", "deepseek-chat")),
                        "provider": d.get("provider", current_def.get("provider", "deepseek")),
                        "endpoint": d.get("endpoint", current_def.get("endpoint", "https://api.deepseek.com/v1/chat/completions")),
                        "key_env": d.get("key_env", current_def.get("key_env", "DEEPSEEK_API_KEY")),
                    }
                if "fallback" in body:
                    fb = body["fallback"]
                    current_fb = cfg.get("fallback", {})
                    cfg["fallback"] = {
                        "model": fb.get("model", current_fb.get("model", "mimo-v2-flash")),
                        "provider": fb.get("provider", current_fb.get("provider", "xiaomi")),
                        "endpoint": fb.get("endpoint", current_fb.get("endpoint", "https://token-plan-cn.xiaomimimo.com/v1/chat/completions")),
                        "key_env": fb.get("key_env", current_fb.get("key_env", "XIAOMI_API_KEY")),
                    }
                if "precheck" in body:
                    pc = body["precheck"]
                    current_pc = cfg.get("precheck", {})
                    cfg["precheck"] = {
                        "enabled": pc.get("enabled", current_pc.get("enabled", True)),
                        "model": pc.get("model", current_pc.get("model", cfg["default"]["model"])),
                        "provider": pc.get("provider", current_pc.get("provider", cfg["default"]["provider"])),
                        "endpoint": pc.get("endpoint", current_pc.get("endpoint", cfg["default"]["endpoint"])),
                        "key_env": pc.get("key_env", current_pc.get("key_env", cfg["default"]["key_env"])),
                    }
            else:
                self._send_error(400, f"Unknown action: {action}")
                return

            # Write routes.json
            with open(ROUTES_PATH, "w") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            _load_routes()  # 热加载到内存
            log(f"Admin: configuration updated (action={action})")
            self._send_json(200, {"status": "ok"})
        except Exception as e:
            log(f"Admin config error: {e}")
            self._send_error(500, str(e))

    def log_message(self, format, *args):
        pass  # 静默内置 HTTP 日志


# ══════════════════════════════════════════
# 入口
# ══════════════════════════════════════════

def main():
    host = "127.0.0.1"
    port = 9880

    _setup_noproxy()
    _evolution_load()
    _load_routes()
    _load_keys()
    _init_sandglass()  # 启动时一次性加载 sandglass，不走请求关键路径

    log("=" * 50)
    log("Thalamus v4.0.0 — OpenAI-compatible transparent proxy + Pre-check")
    log(f"Listening: {host}:{port}")
    log(f"Endpoints: /v1/chat/completions /task /parallel /analysis /evolution /health /stats /admin")
    log(f"Routes: {len(ROUTES)} rules (from {ROUTES_PATH})")
    log(f"Pre-check: {'ON' if PRECHECK_ENABLED else 'OFF'} (model={PRECHECK_MODEL})")
    log(f"Fallback: {DEFAULT_MODEL} ({DEFAULT_PROVIDER}) → {FALLBACK_MODEL} ({FALLBACK_PROVIDER})")
    log(f"Features: streaming SSE, tool_calls pass-through, pre-check, multi_analysis, evolution, ThreadingHTTPServer")
    
    # 启动主动健康探测后台线程
    _hp_thread = threading.Thread(target=_health_probe_worker, daemon=True)
    _hp_thread.start()
    log("HEALTH PROBE: background worker started")
    
    server = ThreadingHTTPServer((host, port), ThalamusHandler)
    server.daemon_threads = True

    def shutdown(signum, frame):
        # 🔴 关键：绝不能在信号处理器（主线程）里直接调用 server.shutdown()。
        # shutdown() 会阻塞等待 serve_forever() 循环退出，但该循环正跑在主线程，
        # 而主线程此刻卡在信号处理器里 → 死锁 → 进程挂死、socket 不释放（幽灵进程）。
        # 解法：① 在独立线程里调用 shutdown()（打破死锁）；
        #       ② 失败保险：无论如何 3 秒后硬退出，保证 socket 被释放、systemd 能重启。
        log(f"SHUTDOWN: received signal {signum}")

        def _do_shutdown():
            try:
                server.shutdown()
            except Exception as e:
                log(f"SHUTDOWN: server.shutdown() error: {e}")

        def _failsafe():
            time.sleep(3)
            log("SHUTDOWN: failsafe hard-exit (shutdown stalled)")
            os._exit(0)

        threading.Thread(target=_do_shutdown, daemon=True).start()
        threading.Thread(target=_failsafe, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("SHUTDOWN: received SIGINT")
    finally:
        log("SHUTDOWN: complete")


if __name__ == "__main__":
    main()
