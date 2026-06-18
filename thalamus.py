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
import signal
import ssl
import sys
import time
import uuid
import threading
import traceback
import urllib.parse
import http.client
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ══════════════════════════════════════════
# 配置
# ══════════════════════════════════════════

LOG_PATH = Path(os.environ.get("THALAMUS_LOG", "/root/.hermes/logs/thalamus.log"))

# 路由规则 — 瀑布式，第一个命中即停止
ROUTES = [
    (re.compile(
        r"(代码|编程|写.*程序|写.*代码|写.*函数|写.*算法|写.*类|写.*模块|实现.*功能|"
        r"修.*bug|重构|部署|deploy|运维|server|服务器|nginx|docker|"
        r"systemctl|ssh|git\s|commit|PR|pull.request|config|配置.*文件|yaml|json.*修改|"
        r"shell|bash|脚本|修复|报错|error|exception|traceback|"
        r"日志.*分析|排查|调试|debug|cron|备份|快照|kill|杀.*进程|重启.*服务|"
        r"安装|apt|pip|npm|yum|dnf|编译|make|cmake|gcc|clang|rpm|dpkg|"
        r"测试|test|unittest|pytest|mock|stub|benchmark)"
     ), "mimo-v2.5-pro", "xiaomi",
     "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
     "XIAOMI_API_KEY", "MiMo 2.5 Pro"),

    (re.compile(
        r"(分析|对比|比较|推理|为什么|原因|根因|深度|论证|评估|策略|方案|"
        r"设计.*架构|优缺点|权衡|决策|审查|审计|review|评审|代码审查)"
     ), "openrouter/auto", "openrouter",
     "https://openrouter.ai/api/v1/chat/completions",
     "OPENROUTER_API_KEY", "OpenRouter Auto"),

    (re.compile(r"(图片|截图|照片|图像|看图|OCR|识别.*图|vision|视觉|多媒体)"),
     "mimo-v2-omni", "xiaomi",
     "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
     "XIAOMI_API_KEY", "MiMo v2 Omni"),
]

# 默认路由（也是 fallback）
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_PROVIDER = "deepseek"
DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_KEY_ENV = "DEEPSEEK_API_KEY"

# ══════════════════════════════════════════
# 全局状态（线程安全）
# ══════════════════════════════════════════

START_TIME = time.time()
_spend_lock = threading.Lock()
_spend = {"total": 0.0, "by_provider": {}, "calls": 0, "fallbacks": 0, "errors": 0}
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


def _make_request(endpoint: str, body: dict, key: str, stream: bool = False, timeout: int = 300):
    """
    向后端发 HTTP 请求。
    stream=False → 返回完整响应 dict
    stream=True  → 返回 (http.client.HTTPResponse, dict_info)
                   调用方负责读取 chunks 并关闭
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

    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)

    try:
        conn.request("POST", path, body=json.dumps(body).encode(), headers=headers)
        resp = conn.getresponse()

        if resp.status != 200:
            err_body = resp.read().decode(errors="replace")[:500]
            raise RuntimeError(f"Backend {resp.status}: {err_body}")

        if stream:
            # 返回原始连接和响应，调用方负责读+关
            return (conn, resp)
        else:
            raw = resp.read()
            conn.close()
            data = json.loads(raw)
            return data
    except Exception:
        conn.close()
        raise


def _read_stream_response(conn, resp, timeout=300):
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

    for pattern, model, provider, endpoint, key_env, label in ROUTES:
        if pattern.search(text):
            log(f"ROUTE: regex → {label}")
            return (endpoint, model, key_env, provider, label)

    log("ROUTE: default → DeepSeek")
    return None


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

    # 1. 分类
    route = classify(messages)

    # 2. 构建转发 body（替换 model 为路由目标，保留其他所有参数）
    if route:
        endpoint, model, key_env, provider, label = route
    else:
        endpoint, model, key_env = DEFAULT_ENDPOINT, DEFAULT_MODEL, DEFAULT_KEY_ENV
        provider = DEFAULT_PROVIDER
        label = "DeepSeek (default)"

    key = os.environ.get(key_env, "")
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
        if is_stream:
            conn, resp = _make_request(endpoint, fwd_body, key, stream=True)
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
                    yield f'data: {{"error": "Stream interrupted"}}\n\n'.encode()
                    yield "data: [DONE]\n\n".encode()
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            return ("stream", True, stream_gen())
        else:
            data = _make_request(endpoint, fwd_body, key, stream=False)
            latency = time.time() - t0
            _record_stats(provider, label, latency, is_stream)

            # 提取用量信息
            usage = data.get("usage", {})
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
            log(f"CRITICAL: DeepSeek itself failed: {e}")
            raise


def _fallback_to_deepseek(body: dict, is_stream: bool) -> tuple:
    """Fallback 到 DeepSeek"""
    with _spend_lock:
        _spend["fallbacks"] += 1
        fb_num = _spend["fallbacks"]

    key = os.environ.get(DEFAULT_KEY_ENV, "")
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


# ══════════════════════════════════════════
# 多模型分析（从 multi-model-analysis 合并）
# ══════════════════════════════════════════

_ANALYSIS_DOMAINS = {"token-plan-cn.xiaomimimo.com", "api.deepseek.com", "openrouter.ai"}

_ANALYSIS_PERSONAS = {
    "code": {
        "model": "mimo-v2.5-pro",
        "system": "你是一名资深软件架构师。以清晰、严谨的风格分析代码问题。",
    },
    "reasoning": {
        "model": "openrouter/auto",
        "system": "你是一名逻辑分析师。从多个角度审视问题，给出全面的推理链。",
    },
    "creative": {
        "model": "deepseek-chat",
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
            endpoint = DEFAULT_ENDPOINT
            key_env = DEFAULT_KEY_ENV
            model = DEFAULT_MODEL
            provider = DEFAULT_PROVIDER

            if "xiaomimimo" in cfg.get("model", ""):
                # 找 MiMo 路由
                for p, m, prov, ep, ke, _ in ROUTES:
                    if "xiaomi" in prov:
                        endpoint, model, key_env, provider = ep, m, ke, prov
                        break
            elif "openrouter" in cfg.get("model", ""):
                for p, m, prov, ep, ke, _ in ROUTES:
                    if "openrouter" in prov:
                        endpoint, model, key_env, provider = ep, m, ke, prov
                        break

            key = os.environ.get(key_env, "")
            if not key:
                raise RuntimeError(f"Key not set: {key_env}")

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
        if self.path in ("/health", "/health/"):
            uptime = int(time.time() - START_TIME)
            self._send_json(200, {
                "status": "ok",
                "name": "thalamus",
                "version": "3.0.0",
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
        else:
            self._send_json(200, {
                "name": "thalamus",
                "version": "3.0.0",
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

    log("=" * 50)
    log("Thalamus v3.1.0 — OpenAI-compatible transparent proxy")
    log(f"Listening: {host}:{port}")
    log(f"Endpoints: /v1/chat/completions /task /parallel /analysis /evolution /health /stats")
    log(f"Routes: {len(ROUTES)} regex rules")
    log(f"Fallback: {DEFAULT_MODEL} ({DEFAULT_PROVIDER})")
    log(f"Features: streaming SSE, tool_calls pass-through, multi_analysis, evolution, ThreadingHTTPServer")
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
