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
import socks
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ══════════════════════════════════════════
# 配置
# ══════════════════════════════════════════

LOG_PATH = Path(os.environ.get("THALAMUS_LOG", "/root/.hermes/logs/thalamus.log"))
ROUTES_PATH = Path(os.environ.get("THALAMUS_ROUTES", "/root/thalamus/routes.json"))
KEYS_PATH = Path("/root/thalamus/keys.json")
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
            ROUTES.append((
                re.compile(r["pattern"]),
                r["model"],
                r["provider"],
                r["endpoint"],
                r["key_env"],
                r["label"],
                r.get("proxy", False),
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
PRECHECK_MAX_TOKENS = 512
PRECHECK_TEMPERATURE = 0.3

# 密钥存储 — 统一管理 key + endpoint
# keys.json 格式: {"ALIAS": {"key": "sk-...", "endpoint": "https://..."}}
KEYS = {}  # alias -> {"key": str, "endpoint": str}

def _load_keys():
    global KEYS
    try:
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
_spend = {"total": 0.0, "by_provider": {}, "calls": 0, "fallbacks": 0, "errors": 0, "total_tokens": 0, "token_by_provider": {}}
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
    """从最后一条用户消息提取文本做正则分类"""
    # 只取最后一条 user 消息（system prompt 含大量关键词会误触发）
    for m in reversed(messages):
        content = m.get("content", "")
        if isinstance(content, str) and m.get("role") == "user":
            text = content
            break
    else:
        # 尝试取所有 user 消息
        text = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str) else ""
            for m in messages if m.get("role") == "user"
        )
    
    if not text:
        return None

    for pattern, model, provider, endpoint, key_env, label, proxy in ROUTES:
        if pattern.search(text):
            log(f"ROUTE: regex → {label}")
            return (endpoint, model, key_env, provider, label, proxy)

    log("ROUTE: default → DeepSeek")
    return None


# ══════════════════════════════════════════
# Pre-check：先查能不能不写代码（Ponytail 理念）
# ══════════════════════════════════════════

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


def precheck(messages: list) -> dict | None:
    """在路由分类之前执行 pre-check。

    发一条轻量查询给预检模型（默认 DeepSeek Chat），
    如果判定可以用现成方案解决，返回建议方案；
    否则返回 None，走正常路由。
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

    # 预检 prompt 很轻：只发用户消息 + system prompt，不传全上下文
    pre_body = {
        "model": PRECHECK_MODEL,
        "messages": [
            {"role": "system", "content": PRECHECK_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "max_tokens": PRECHECK_MAX_TOKENS,
        "temperature": PRECHECK_TEMPERATURE,
        "stream": False,
    }

    key = _resolve_key(PRECHECK_KEY_ENV)
    if not key:
        log(f"PRECHECK WARNING: key not set: {PRECHECK_KEY_ENV}")
        return None

    t0 = time.time()
    try:
        data = _make_request(PRECHECK_ENDPOINT, pre_body, key, stream=False)
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
            # 查询 Sandglass：用户以前做过类似需求吗？
            sandglass_hint = ""
            try:
                import sys as _sys
                # sandglass_sqlite 依赖 sandglass_paths._NB，后者读 NEXSANDBASE_HOME
                import os as _os
                _os.environ.setdefault("NEXSANDBASE_HOME", "/root/.hermes/nexsandglass")
                _sand_paths = ["/root/nexsandglass", "/root/.hermes/NexSandglass"]
                for _p in _sand_paths:
                    if _p not in _sys.path:
                        _sys.path.insert(0, _p)
                from sandglass_sqlite import search as _sg_search
                # 取用户消息前几个关键词搜索——先精确后宽松
                _stopwords = {"用", "在", "的", "了", "吗", "吧", "呢", "啊", "什么", "怎么", "如何",
                              "哪个", "哪些", "可以", "能", "要", "是", "有", "给", "把", "被",
                              "from", "to", "in", "on", "at", "for", "of", "the", "a", "an",
                              "and", "or", "do", "is", "it", "with", "很", "太", "不", "没", "还",
                              "说", "话", "对", "那", "这", "我", "你", "他", "她"}
                import re as _re
                # 提取英文/数字关键词（技术词汇）
                _tech = [w.lower() for w in _re.findall(r"[a-zA-Z0-9_]{2,}", text)
                         if w.lower() not in _stopwords]
                if _tech:
                    # OR 搜索：任何技术词命中就算
                    _kw = " OR ".join(_tech[:3])
                    _results = _sg_search(_kw, limit=3)
                else:
                    # 纯中文查询——取前2个词做 AND 搜索
                    _words = [w for w in text.split() if len(w) > 1]
                    _kw = " ".join(_words[:2]) if _words else ""
                    _results = _sg_search(_kw, limit=3) if _kw else []
                if _results:
                    _lines = []
                    for _rid, _ts, _rtext in _results[:2]:
                        _short = _rtext.replace("\n", " ").strip()[:100]
                        _lines.append(f"  [{_ts}] {_short}")
                    if _lines:
                        sandglass_hint = "\n📖 **之前类似需求的记录：**\n" + "\n".join(_lines)
                    log(f"PRECHECK: sandglass hit {len(_results)} for '{_kw}'")
                else:
                    log(f"PRECHECK: sandglass no hit for '{_kw}'")
            except Exception as _e:
                log(f"PRECHECK: sandglass query failed: {_e}")
                import traceback as _tb
                log(f"PRECHECK: sandglass traceback: {_tb.format_exc()}")

            return {
                "intercepted": True,
                "category": category,
                "suggestion": suggestion,
                "sandglass_hint": sandglass_hint,
                "latency": round(latency, 2),
            }

        # NO 或无法解析 → 放行
        return {"intercepted": False}
    except Exception as e:
        log(f"PRECHECK ERROR: {e}")
        return None  # precheck 失败不阻塞正常流程


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

    # 0. Input length guard: 如果总输入 > 140K 字符，跳过 token173 路由直接走 DeepSeek
    total_input_chars = sum(
        len(m.get("content", "")) if isinstance(m.get("content"), str) else 0
        for m in messages
    )
    long_input = total_input_chars > 140_000
    if long_input:
        log(f"INPUT TOO LONG ({total_input_chars} chars): skipping route classification, going straight to DeepSeek")
        endpoint, model, key_env, provider, label, proxy = (
            DEFAULT_ENDPOINT, DEFAULT_MODEL, DEFAULT_KEY_ENV, DEFAULT_PROVIDER, "DeepSeek (long input)", False
        )
        route = (endpoint, model, key_env, provider, label, proxy)

    # 1. Pre-check：先查能不能不写代码
    pc_result = precheck(messages)
    if pc_result and pc_result.get("intercepted"):
        log(f"PRECHECK: bypassed routing, task solved without code generation")
        suggestion = pc_result["suggestion"]
        # 构造一个直接建议的响应，不走模型路由
        reply_text = f"✅ **{pc_result['category'].upper()} 方案可用**\n\n{suggestion}"
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

    # 1. 分类
    if not long_input:
        route = classify(messages)
    else:
        route = route  # 保留 input length guard 设置的路由

    # 2. 构建转发 body（替换 model 为路由目标，保留其他所有参数）
    if route:
        endpoint, model, key_env, provider, label, proxy = route
    else:
        endpoint, model, key_env = DEFAULT_ENDPOINT, DEFAULT_MODEL, DEFAULT_KEY_ENV
        provider = DEFAULT_PROVIDER
        label = "DeepSeek (default)"
        proxy = False

    key = _resolve_key(key_env)
    if not key:
        log(f"ERROR: API key not set: {key_env}")
        raise RuntimeError(f"API key not set: {key_env}")

    # 构建转发请求体
    fwd_body = dict(body)
    fwd_body["model"] = model
    # 确保 stream 参数传给后端
    fwd_body["stream"] = is_stream

    # 3. 域名代理处理
    domain = urllib.parse.urlparse(endpoint).hostname or ""
    if not domain:
        raise RuntimeError(f"Invalid endpoint domain: {endpoint}")
    _set_noproxy(domain)

    t0 = time.time()

    # 4. 请求后端
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
        choice = data.get("choices", [{}])[0]
        actual_model = data.get("model", model)
        choice_msg = choice.get("message", {})
        content_len = len(choice_msg.get("content", "") or "")
        has_tools = "tool_calls" in choice_msg

        log(f"CALL: {label} | {latency:.2f}s | "
            f"tok={usage.get('prompt_tokens',0)}+{usage.get('completion_tokens',0)} | "
            f"content={content_len}c tool_calls={'Y' if has_tools else 'N'} | "
            f"model={actual_model}")

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

        # 5. 如果不是默认路由，fallback 到 DeepSeek
        if route:
            log(f"FALLBACK: {label} failed ({e}) → DeepSeek")
            evolution_learn(label, latency, success=False)
            return _fallback_to_deepseek(body, is_stream)
        else:
            # DeepSeek 自身失败 → fallback 到 MiMo Flash
            log(f"FALLBACK: DeepSeek failed ({e}) → MiMo Flash")
            evolution_learn("DeepSeek (default)", latency, success=False)
            return _fallback_to_mimo(body, is_stream)


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
            for p, m, prov, ep, ke, label in ROUTES:
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
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: int, message: str):
        self._send_json(code, {"error": message})

    def do_GET(self):
        if self.path in ("/", ""):
            # Root → admin panel
            self._serve_admin_html()
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
        elif self.path in ("/stats", "/stats/"):
            self._send_json(200, {
                "uptime_seconds": int(time.time() - START_TIME),
                "calls": _spend["calls"],
                "fallbacks": _spend["fallbacks"],
                "errors": _spend["errors"],
                "by_provider": dict(_spend["by_provider"]),
                "evolution_score": _EVOLUTION_SCORE,
                "evolution_decisions": len(_EVOLUTION_LOG),
                "evolution_patterns": len(_EVOLUTION_PATTERNS),
            })
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

        if self.path in ("/v1/chat/completions", "/v1/chat/completions/"):
            self._handle_chat_completion(body)
        elif self.path in ("/task", "/task/"):
            self._handle_task(body)
        elif self.path in ("/parallel", "/parallel/"):
            self._handle_parallel(body)
        elif self.path in ("/analysis", "/analysis/"):
            self._handle_analysis(body)
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

    # ─── Admin panel handlers ───

    def _check_admin_auth(self) -> bool:
        now = time.time()
        # Clean expired tokens
        expired = [t for t, exp in list(ADMIN_TOKEN.items()) if exp < now]
        for t in expired:
            ADMIN_TOKEN.pop(t, None)
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] in ADMIN_TOKEN:
            return True
        return False

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

    def _handle_admin_get(self):
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

        if path == "/admin/api/login":
            pwd = body.get("password", "")
            stored = _get_admin_password()
            if not stored:
                self._send_error(500, "No admin password configured")
                return
            # Simple timing-safe comparison
            if secrets.compare_digest(pwd.encode(), stored.encode()):
                token = secrets.token_urlsafe(32)
                ADMIN_TOKEN[token] = time.time() + 86400  # 24h expiry
                self._send_json(200, {"token": token})
            else:
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
        with open(KEYS_PATH, "w") as f:
            json.dump(KEYS, f, indent=2)
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
        with open(KEYS_PATH, "w") as f:
            json.dump(KEYS, f, indent=2)
        log(f"Admin: deleted API key '{name}'")
        self._send_json(200, {"status": "ok", "affected": affected})

    def _handle_key_probe(self, body: dict):
        """Probe a key's endpoint for available models. 60s throttle."""
        name = body.get("name", "").strip()
        if not name or name not in KEYS:
            self._send_error(404, f"Key '{name}' not found")
            return
        entry = KEYS[name]
        endpoint_base = entry["endpoint"].replace("/chat/completions", "").replace("/v1", "")
        if endpoint_base.endswith("/"):
            endpoint_base = endpoint_base[:-1]
        endpoint_base += "/v1/models"

        # Throttle
        now = time.time()
        cache_key = f"_probe_{name}"
        last_probe = _spend.get(cache_key, 0)
        if now - last_probe < 60:
            self._send_error(429, "Probe cooldown (60s). Please wait.")
            return

        try:
            req = urllib.request.Request(endpoint_base)
            req.add_header("Authorization", f"Bearer {entry['key']}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            models = []
            for m in data.get("data", []):
                mid = m.get("id", "")
                if mid:
                    tags = _tag_model(mid)
                    models.append({"id": mid, "object": m.get("object", "model"), "tags": tags})
            _spend[cache_key] = now
            self._send_json(200, {"models": models})
        except Exception as e:
            log(f"Probe failed for '{name}': {e}")
            self._send_error(502, f"Probe failed: {e}")

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
                with open(KEYS_PATH, "w") as f:
                    json.dump(KEYS, f, indent=2)
                log(f"Admin: saved API key '{name}'")
                self._send_json(200, {"status": "ok", "keys": list(KEYS.keys())})
                return
            elif action == "delete_key":
                name = body.get("key_name", "")
                if name in KEYS:
                    KEYS.pop(name)
                    with open(KEYS_PATH, "w") as f:
                        json.dump(KEYS, f, indent=2)
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

    log("=" * 50)
    log("Thalamus v4.0.0 — OpenAI-compatible transparent proxy + Pre-check")
    log(f"Listening: {host}:{port}")
    log(f"Endpoints: /v1/chat/completions /task /parallel /analysis /evolution /health /stats /admin")
    log(f"Routes: {len(ROUTES)} rules (from {ROUTES_PATH})")
    log(f"Pre-check: {'ON' if PRECHECK_ENABLED else 'OFF'} (model={PRECHECK_MODEL})")
    log(f"Fallback: {DEFAULT_MODEL} ({DEFAULT_PROVIDER}) → {FALLBACK_MODEL} ({FALLBACK_PROVIDER})")
    log(f"Features: streaming SSE, tool_calls pass-through, pre-check, multi_analysis, evolution, ThreadingHTTPServer")
    log("=" * 50)

    server = ThreadingHTTPServer((host, port), ThalamusHandler)
    server.daemon_threads = True

    def shutdown(signum, frame):
        log(f"SHUTDOWN: received signal {signum}")
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("SHUTDOWN: received SIGINT")
        server.shutdown()
        log("SHUTDOWN: complete")


if __name__ == "__main__":
    main()
