#!/usr/bin/env python3
"""
Thalamus Lite — 精简版 AI 路由网关
====================================
只保留两个核心能力：
  1. AI 路由：按内容把请求路由到专业模型（正则优先 + AI 语义兜底 + 默认路由）
  2. API Key 管理：通过 admin 接口添加/删除/列出后端密钥（密码鉴权 + Fernet 加密存储）

已移除（相对完整版 v4）：precheck(Ponytail)、sandglass、熔断器、进化学习、
速率限制、multi_analysis/parallel/task/berserker、Prometheus 指标、成本估算、
TF-IDF 语义、上下文压缩、健康探测、admin HTML 面板、SOCKS 代理。

端点：
  POST /v1/chat/completions   OpenAI 兼容透明代理（stream + 非 stream）
  GET  /v1/models             列出可用模型
  GET  /health                健康检查
  POST /admin/api/login       登录获取管理 token
  POST /admin/api/keys/add    添加后端 API Key
  POST /admin/api/keys/delete 删除后端 API Key
  GET  /admin/api/keys/list   列出已保存的 Key 别名
  POST /admin/api/reload      热重载 routes.json
"""

import base64
import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import signal
import ssl
import sys
import threading
import time
import traceback
import urllib.parse
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None
    InvalidToken = None

try:
    from ai_router import classify_with_ai
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ai_router import classify_with_ai

# ══════════════════════════════════════════
# 配置
# ══════════════════════════════════════════
THIS_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
ROUTES_PATH = Path(os.environ.get("THALAMUS_ROUTES", str(THIS_DIR / "routes.json")))
KEYS_PATH = THIS_DIR / "keys.json"
KEYS_PATH_ENC = THIS_DIR / "keys.json.enc"
ADMIN_PWD_PATH = THIS_DIR / "admin.pwd"
MASTER_KEY = os.environ.get("THALAMUS_MASTER_KEY", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("thalamus-lite")

START_TIME = time.time()
_LOGGER_LOCK = threading.Lock()

# 路由规则
ROUTES = []  # (pattern, model, provider, endpoint, key_env, label, proxy)
ROUTE_FALLBACKS = {}  # label -> [{model, provider, endpoint, key_env}, ...]
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_PROVIDER = "deepseek"
DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_KEY_ENV = "DEEPSEEK_API_KEY"
AI_ROUTING = {}  # ai_routing config from routes.json

# ══════════════════════════════════════════
# Vision 额度保护：防止调用过量
# ══════════════════════════════════════════
_VISION_COUNTER = {"count": 0, "window_start": time.time()}
_VISION_LIMIT = int(os.environ.get("THALAMUS_VISION_LIMIT", "50"))  # 每小时最多50次
_VISION_WINDOW = 3600  # 1小时窗口
_VISION_FALLBACK_MODEL = os.environ.get("THALAMUS_VISION_FALLBACK", "gpt-5.6-luna")
_VISION_FALLBACK_ENDPOINT = os.environ.get("THALAMUS_VISION_FALLBACK_ENDPOINT", "https://opencode.ai/zen/go/v1/chat/completions")
_VISION_FALLBACK_KEY = os.environ.get("THALAMUS_VISION_FALLBACK_KEY_ENV", "opencode")
 
# 密钥存储
KEYS = {}  # alias -> {"key": str, "endpoint": str}

# Admin 会话 token
ADMIN_TOKEN = {}  # token -> expiry
_ADMIN_TOKEN_LOCK = threading.Lock()


def log_msg(msg: str):
    with _LOGGER_LOCK:
        log.info(msg)


# ══════════════════════════════════════════
# 密钥加密 (Fernet, 惰性初始化)
# ══════════════════════════════════════════
_FERNET_INSTANCE = None


def _init_crypto() -> bool:
    global _FERNET_INSTANCE
    if _FERNET_INSTANCE is not None:
        return True
    if not MASTER_KEY or Fernet is None:
        return False
    try:
        key_bytes = base64.urlsafe_b64encode(hashlib.sha256(MASTER_KEY.encode()).digest())
        _FERNET_INSTANCE = Fernet(key_bytes)
        return True
    except Exception as e:
        log_msg(f"KEY CRYPTO: init failed: {e}")
        _FERNET_INSTANCE = None
        return False


def _save_keys(data: dict):
    """单一写入路径：明文 keys.json 兼容 + 有主密钥时加密。"""
    try:
        with open(KEYS_PATH, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log_msg(f"KEY WRITE: failed {KEYS_PATH}: {e}")
    if _init_crypto():
        try:
            payload = json.dumps(data, ensure_ascii=False, default=str).encode()
            KEYS_PATH_ENC.write_bytes(_FERNET_INSTANCE.encrypt(payload))
        except Exception as e:
            log_msg(f"KEY WRITE: encrypt failed: {e}")


def _load_keys():
    global KEYS
    KEYS = {}
    # 1. 尝试加密文件
    if _init_crypto() and KEYS_PATH_ENC.exists():
        try:
            payload = _FERNET_INSTANCE.decrypt(KEYS_PATH_ENC.read_bytes())
            raw = json.loads(payload)
            for alias, val in raw.items():
                KEYS[alias] = val if isinstance(val, dict) else {"key": val, "endpoint": DEFAULT_ENDPOINT}
            log_msg(f"Loaded {len(KEYS)} keys from {KEYS_PATH_ENC.name}")
            return
        except InvalidToken:
            log_msg("KEY CRYPTO: wrong master key, encrypted file ignored")
        except Exception as e:
            log_msg(f"KEY CRYPTO: decrypt failed: {e}")
    # 2. 明文回退
    if KEYS_PATH.exists():
        try:
            raw = json.loads(KEYS_PATH.read_text())
            for alias, val in raw.items():
                KEYS[alias] = val if isinstance(val, dict) else {"key": val, "endpoint": DEFAULT_ENDPOINT}
            log_msg(f"Loaded {len(KEYS)} keys from {KEYS_PATH.name}")
        except Exception as e:
            log_msg(f"KEY LOAD: failed {KEYS_PATH}: {e}")


def _resolve_key(key_env: str) -> str:
    """key_env 优先命中 KEYS 别名，其次环境变量。"""
    if not key_env:
        return ""
    if key_env in KEYS:
        return KEYS[key_env].get("key", "")
    return os.environ.get(key_env, "")


def _resolve_endpoint(key_env: str) -> str:
    if key_env in KEYS:
        return KEYS[key_env].get("endpoint", "")
    return ""


# ══════════════════════════════════════════
# 路由加载
# ══════════════════════════════════════════
def _load_routes():
    global ROUTES, ROUTE_FALLBACKS, DEFAULT_MODEL, DEFAULT_PROVIDER, DEFAULT_ENDPOINT, DEFAULT_KEY_ENV, AI_ROUTING
    with open(ROUTES_PATH) as f:
        cfg = json.load(f)

    ROUTES = []
    ROUTE_FALLBACKS = {}
    for r in cfg.get("routes", []):
        ROUTES.append((
            re.compile(r["pattern"], re.IGNORECASE),
            r["model"],
            r["provider"],
            r["endpoint"],
            r["key_env"],
            r["label"],
            r.get("proxy", False),
        ))
        ROUTE_FALLBACKS[r["label"]] = r.get("fallbacks", [])

    d = cfg.get("default", {})
    DEFAULT_MODEL = d.get("model", "deepseek-chat")
    DEFAULT_PROVIDER = d.get("provider", "deepseek")
    DEFAULT_ENDPOINT = d.get("endpoint", "https://api.deepseek.com/v1/chat/completions")
    DEFAULT_KEY_ENV = d.get("key_env", "DEEPSEEK_API_KEY")

    AI_ROUTING = cfg.get("ai_routing", {}) or {}
    log_msg(f"Loaded {len(ROUTES)} routes from {ROUTES_PATH.name}")


# ══════════════════════════════════════════
# 路由分类：正则优先 → AI 语义兜底 → 默认
# ══════════════════════════════════════════
def _extract_text(messages) -> str:
    parts = []
    user_count = 0
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
        else:
            continue
        if text.strip():
            # 最后一条 user 消息重复一次强化权重
            if user_count == 0:
                parts.insert(0, text)
                parts.insert(0, text)
            else:
                parts.insert(0, text)
            user_count += 1
            if user_count >= 5:
                break
    return "\n".join(parts)


def _has_image(messages) -> bool:
    for m in messages:
        c = m.get("content")
        if isinstance(c, str) and ("data:image" in c or "image_url" in c):
            return True
        if isinstance(c, list):
            for p in c:
                if not isinstance(p, dict):
                    continue
                if p.get("type") in ("image_url", "image", "input_image"):
                    return True
                if "data:image" in str(p.get("image_url") or "") or "data:image" in str(p.get("image") or ""):
                    return True
    return False


def classify(messages) -> tuple | None:
    """返回 (endpoint, model, key_env, provider, label, proxy) 或 None(用默认)。"""
    # 0. 图片请求 → 强制走视觉路由
    if _has_image(messages):
        for pattern, model, provider, endpoint, key_env, label, proxy in ROUTES:
            if label == "vision":
                log_msg(f"ROUTE: image → vision ({model})")
                return (endpoint, model, key_env, provider, label, proxy)
    text = _extract_text(messages)
    if not text:
        return None

    # 1. 正则优先
    for pattern, model, provider, endpoint, key_env, label, proxy in ROUTES:
        if pattern.search(text):
            log_msg(f"ROUTE: regex → {label} ({model})")
            return (endpoint, model, key_env, provider, label, proxy)

    # 2. AI 语义兜底
    try:
        ai_label, ai_conf = classify_with_ai(text, AI_ROUTING)
        if ai_label is not None:
            for _p, _m, _pr, _e, _k, _l, _px in ROUTES:
                if _l == ai_label:
                    log_msg(f"ROUTE: ai → {ai_label} (conf={ai_conf:.2f})")
                    return (_e, _m, _k, _pr, _l, _px)
            log_msg(f"ROUTE: ai → {ai_label} (label not in routes — default)")
        elif AI_ROUTING.get("enabled"):
            log_msg("ROUTE: ai → general (using default)")
    except Exception as e:
        log_msg(f"WARNING: ai_router failed: {e}")

    # 3. 默认
    log_msg("ROUTE: default route")
    return None


# ══════════════════════════════════════════
# 后端请求转发
# ══════════════════════════════════════════
def _make_request(endpoint: str, body: dict, key: str, stream: bool = False, timeout: int = 180, use_socks: bool = False):
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
        "User-Agent": "Thalamus-Lite/1.0",
    }
    ctx = ssl.create_default_context()
    if use_socks:
        try:
            import socks
        except ImportError:
            socks = None
        if socks is None:
            raise RuntimeError("PySocks not installed")
        _proxy_port = int(os.environ.get("THALAMUS_PROXY_PORT", "1080"))
        _proxy_host = os.environ.get("THALAMUS_PROXY_HOST", "127.0.0.1")

        class _ProxyConn(http.client.HTTPSConnection):
            def connect(self):
                self.sock = socks.socksocket()
                self.sock.set_proxy(socks.SOCKS5, _proxy_host, _proxy_port)
                self.sock.settimeout(timeout)
                self.sock.connect((self.host, self.port))
                self.sock = ctx.wrap_socket(self.sock, server_hostname=self.host)

        conn = _ProxyConn(host, port, timeout=timeout)
    else:
        conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)
    try:
        conn.request("POST", path, body=json.dumps(body).encode(), headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            err_body = resp.read().decode(errors="replace")[:500]
            conn.close()
            raise RuntimeError(f"Backend {resp.status}: {err_body}")
        if stream:
            return conn, resp
        raw = resp.read()
        conn.close()
        return json.loads(raw)
    except Exception:
        conn.close()
        raise


def _read_stream_response(conn, resp):
    """流式 SSE chunks，作为 generator。"""
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


def process(body: dict) -> tuple:
    """返回 (is_stream, result_or_generator, meta)。"""
    messages = body.get("messages", [])
    is_stream = body.get("stream", False)

    route = classify(messages)

    if route:
        endpoint, model, key_env, provider, label, proxy = route
        
        # ══════════════════════════════════════════
        # Vision 额度保护：超限自动降级到便宜模型
        # ══════════════════════════════════════════
        if label == "vision":
            now = time.time()
            # 检查窗口是否过期
            if now - _VISION_COUNTER["window_start"] > _VISION_WINDOW:
                _VISION_COUNTER["count"] = 0
                _VISION_COUNTER["window_start"] = now
                log_msg("VISION: counter reset (new window)")
            
            _VISION_COUNTER["count"] += 1
            if _VISION_COUNTER["count"] > _VISION_LIMIT:
                log_msg(f"VISION: limit exceeded ({_VISION_COUNTER['count']}/{_VISION_LIMIT}), falling back to {_VISION_FALLBACK_MODEL}")
                # 降级到便宜模型
                endpoint = _VISION_FALLBACK_ENDPOINT
                model = _VISION_FALLBACK_MODEL
                key_env = _VISION_FALLBACK_KEY
                provider = "opencode"
    else:
        endpoint, model, key_env, provider, label = DEFAULT_ENDPOINT, DEFAULT_MODEL, DEFAULT_KEY_ENV, DEFAULT_PROVIDER, "default"
        proxy = False

    key = _resolve_key(key_env)
    if not key:
        raise RuntimeError(f"API key not set: {key_env}")

    start = time.time()

    # 组装尝试列表：主后端 + per-route fallbacks（失败按序降级）
    attempts = [(endpoint, model, key_env, bool(proxy))]
    for fb in ROUTE_FALLBACKS.get(label, []):
        attempts.append((fb.get("endpoint"), fb.get("model"), fb.get("key_env"), bool(fb.get("proxy", False))))

    last_err = None
    for en, mo, ke, usx in attempts:
        if not (en and mo and ke):
            continue
        k = _resolve_key(ke)
        if not k:
            continue
        fwd_body = dict(body)
        fwd_body["model"] = mo
        try:
            if is_stream:
                conn, resp = _make_request(en, fwd_body, k, stream=True, use_socks=usx, timeout=int(os.environ.get("THALAMUS_STREAM_TIMEOUT", "600")))
                return (True, _read_stream_response(conn, resp), {"routed_to": label, "provider": provider, "model": mo, "latency_s": 0})
            result = _make_request(en, fwd_body, k, stream=False, use_socks=usx)
            latency = round(time.time() - start, 3)
            result["_thalamus"] = {"routed_to": label, "provider": provider, "model": mo, "latency_s": latency}
            return (False, result, result["_thalamus"])
        except Exception as e:
            last_err = e
            log_msg(f"route '{label}' backend {mo} failed: {e}; trying fallback...")

    raise RuntimeError(f"all backends unavailable (route={label}): {last_err}")


# ══════════════════════════════════════════
# HTTP Handler
# ══════════════════════════════════════════
class ThalamusLiteHandler(BaseHTTPRequestHandler):
    MAX_BODY_SIZE = 20 * 1024 * 1024  # 20MB

    # ── 基础工具 ──
    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        if length > self.MAX_BODY_SIZE:
            raise ValueError(f"Body too large ({length})")
        raw = self.rfile.read(length)
        return json.loads(raw)

    def _send_json(self, code: int, data: dict, extra_headers: dict | None = None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Thalamus-Version", "lite-1.0")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: int, message: str):
        self._send_json(code, {"error": message})

    def log_message(self, fmt, *args):
        pass  # 用 logging 替代

    # ── 认证 ──
    def _check_admin_auth(self) -> bool:
        now = time.time()
        # 清理过期
        expired = [t for t, exp in list(ADMIN_TOKEN.items()) if exp < now]
        for t in expired:
            ADMIN_TOKEN.pop(t, None)
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] in ADMIN_TOKEN:
            return True
        cookie = self.headers.get("Cookie", "")
        for pair in cookie.split(";"):
            pair = pair.strip()
            if pair.startswith("admin_token="):
                tok = pair[len("admin_token="):]
                if tok in ADMIN_TOKEN:
                    return True
        return False

    def _get_admin_password(self) -> str:
        try:
            if ADMIN_PWD_PATH.exists():
                return ADMIN_PWD_PATH.read_text().strip()
        except Exception:
            pass
        return ""

    # ── GET ──
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/health", "/health/"):
            self._send_json(200, {
                "status": "ok",
                "name": "thalamus-lite",
                "version": "1.0.0",
                "uptime_seconds": int(time.time() - START_TIME),
                "routes": [{"label": r[5], "provider": r[2]} for r in ROUTES],
                "default": DEFAULT_MODEL,
                "ai_routing_enabled": bool(AI_ROUTING.get("enabled")),
            })
        elif path in ("/v1/models", "/v1/models/"):
            models = []
            seen = set()
            for _p, m, _pr, _e, _k, _l, _px in ROUTES:
                if m not in seen:
                    seen.add(m)
                    models.append({"id": m, "object": "model", "routed_labels": [r[5] for r in ROUTES if r[1] == m]})
            if DEFAULT_MODEL not in seen:
                models.append({"id": DEFAULT_MODEL, "object": "model"})
            self._send_json(200, {"object": "list", "data": models})
        elif path.startswith("/admin/api"):
            if path in ("/admin/api/keys/list", "/admin/api/keys/list/"):
                if not self._check_admin_auth():
                    self._send_error(401, "Unauthorized")
                    return
                self._send_json(200, {"status": "ok", "keys": sorted(KEYS.keys())})
            elif path in ("/admin/api/routes/list", "/admin/api/routes/list/"):
                if not self._check_admin_auth():
                    self._send_error(401, "Unauthorized")
                    return
                self._handle_routes_list()
            elif path in ("/admin", "/admin/") or path.startswith("/admin/index"):
                self._serve_admin_html()
            else:
                self._send_error(404, "Not found")
        elif path in ("/admin", "/admin/"):
            self._serve_admin_html()
        else:
            self._send_error(404, "Not found")

    # ── POST ──
    def do_POST(self):
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

        path = self.path.rstrip("/")

        if path in ("/v1/chat/completions", "/v1/chat/completions/"):
            self._handle_chat_completion(body)
        elif path == "/admin/api/login":
            self._handle_login(body)
        elif path.startswith("/admin/api"):
            if not self._check_admin_auth():
                self._send_error(401, "Unauthorized")
                return
            if path in ("/admin/api/keys/add",):
                self._handle_key_add(body)
            elif path in ("/admin/api/keys/delete",):
                self._handle_key_delete(body)
            elif path in ("/admin/api/routes/add",):
                self._handle_route_add(body)
            elif path in ("/admin/api/routes/delete",):
                self._handle_route_delete(body)
            elif path in ("/admin/api/models/probe",):
                self._handle_model_probe(body)
            elif path == "/admin/api/reload":
                try:
                    _load_routes()
                    _load_keys()
                    self._send_json(200, {"status": "ok", "message": "Configuration reloaded"})
                except Exception as e:
                    self._send_error(500, str(e))
            else:
                self._send_error(404, "Not found")
        else:
            self._send_error(404, "Not found")

    def _handle_login(self, body: dict):
        pwd = body.get("password", "")
        stored = self._get_admin_password()
        if not stored:
            self._send_error(500, "No admin password configured (create admin.pwd)")
            return
        if secrets.compare_digest(pwd.encode(), stored.encode()):
            token = secrets.token_urlsafe(32)
            ADMIN_TOKEN[token] = time.time() + 86400  # 24h
            log_msg("Admin login success")
            self._send_json(200, {"token": token})
        else:
            self._send_error(401, "Invalid password")

    def _handle_key_add(self, body: dict):
        name = body.get("name", "").strip()
        key = body.get("key", "").strip()
        endpoint = body.get("endpoint", "").strip()
        if not name or not key:
            self._send_error(400, "Missing required fields: name, key")
            return
        KEYS[name] = {"key": key, "endpoint": endpoint or DEFAULT_ENDPOINT}
        _save_keys(KEYS)
        log_msg(f"Admin: saved API key '{name}'")
        self._send_json(200, {"status": "ok", "keys": sorted(KEYS.keys())})

    def _handle_key_delete(self, body: dict):
        name = body.get("name", "").strip()
        if not name or name not in KEYS:
            self._send_error(404, f"Key '{name}' not found")
            return
        del KEYS[name]
        _save_keys(KEYS)
        log_msg(f"Admin: deleted API key '{name}'")
        self._send_json(200, {"status": "ok", "keys": sorted(KEYS.keys())})

    # ── 路由管理（控制台新增） ──
    def _load_routes_cfg(self) -> dict:
        """读取 routes.json 原始 dict。"""
        with open(ROUTES_PATH) as f:
            return json.load(f)

    def _save_routes_cfg(self, cfg: dict):
        with open(ROUTES_PATH, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        _load_routes()

    def _handle_routes_list(self):
        """列出全部路由 + 默认 + AI 路由配置 + key 状态。"""
        cfg = self._load_routes_cfg()
        routes = []
        for r in cfg.get("routes", []):
            label = r.get("label", "")
            key_env = r.get("key_env", "")
            routes.append({
                "label": label,
                "model": r.get("model", ""),
                "provider": r.get("provider", ""),
                "endpoint": r.get("endpoint", ""),
                "usage": r.get("usage", ""),
                "pattern": r.get("pattern", ""),
                "proxy": r.get("proxy", False),
                "has_key": bool(_resolve_key(key_env)),
            })
        ai = cfg.get("ai_routing", {})
        self._send_json(200, {
            "status": "ok",
            "routes": routes,
            "default": cfg.get("default", {}),
            "ai_routing": {
                "enabled": bool(ai.get("enabled")),
                "mode": ai.get("mode", "fallback"),
                "model": ai.get("model", ""),
                "categories": ai.get("categories", {}),
            },
            "keys": sorted(KEYS.keys()),
        })

    # ── 模型探测（控制台新增） ──
    def _handle_model_probe(self, body: dict):
        """用 endpoint+key 探测 OpenAI 兼容服务的模型列表。

        接受完整 chat completions 端点(自动换成 /models)或 /models 端点。
        body: {endpoint, key}  key 可选(可从既有 key_env 复用)。
        """
        endpoint = body.get("endpoint", "").strip()
        key = body.get("key", "").strip()
        key_env = body.get("key_env", "").strip()
        if not endpoint:
            self._send_error(400, "Missing endpoint")
            return
        # key 优先级: body.key > key_env 解析 > 空
        if not key and key_env:
            key = _resolve_key(key_env)
        if not key:
            self._send_error(400, "Missing API key (需提供 key 或 key_env)")
            return

        # 规整为 /models 端点
        models_url = endpoint.rstrip("/")
        if models_url.endswith("/chat/completions"):
            models_url = models_url[: -len("/chat/completions")] + "/models"
        elif models_url.endswith("/v1"):
            models_url = models_url + "/models"

        parsed = urllib.parse.urlparse(models_url)
        host = parsed.hostname or ""
        if not host:
            self._send_error(400, f"Invalid endpoint: {models_url}")
            return
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "Thalamus-Lite/1.0",
        }
        try:
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=15)
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode(errors="replace")
            conn.close()
            if resp.status != 200:
                self._send_error(502, f"Probe {resp.status}: {raw[:300]}")
                return
            data = json.loads(raw)
            # OpenAI 兼容: {data: [{id, object, owned_by, created}, ...]}
            items = data.get("data", [])
            if not isinstance(items, list):
                self._send_error(502, "Unexpected /models response shape")
                return
            models = []
            for it in items:
                mid = it.get("id") if isinstance(it, dict) else str(it)
                if mid:
                    models.append({
                        "id": mid,
                        "owned_by": (it.get("owned_by") if isinstance(it, dict) else "") or "",
                        "created": (it.get("created") if isinstance(it, dict) else 0) or 0,
                    })
            models.sort(key=lambda m: m["id"].lower())
            log_msg(f"Admin: probed {len(models)} models from {host}")
            self._send_json(200, {"status": "ok", "models": models, "count": len(models), "probed_from": models_url})
        except json.JSONDecodeError:
            self._send_error(502, "Probe returned non-JSON (端点可能不是 OpenAI 兼容)")
        except Exception as e:
            self._send_error(502, f"Probe failed: {e}")

    def _handle_route_add(self, body: dict):
        label = body.get("label", "").strip()
        model = body.get("model", "").strip()
        endpoint = body.get("endpoint", "").strip()
        usage = body.get("usage", "").strip()
        key = body.get("key", "").strip()
        pattern = body.get("pattern", "").strip()
        proxy = bool(body.get("proxy", False))
        provider = body.get("provider", "").strip() or label
        if not label or not model or not endpoint:
            self._send_error(400, "Missing required: label, model, endpoint")
            return

        # 1. 保存 API Key
        if key:
            KEYS[label] = {"key": key, "endpoint": endpoint}
            _save_keys(KEYS)

        # 2. 更新 routes.json
        cfg = self._load_routes_cfg()
        routes = cfg.get("routes", [])
        routes = [r for r in routes if r.get("label") != label]  # 覆盖同名
        routes.append({
            "label": label,
            "model": model,
            "provider": provider,
            "key_env": label,
            "endpoint": endpoint,
            "pattern": pattern or "\\b" + label + "\\b",  # 兜底：命中 label 名
            "usage": usage or model,
            "proxy": proxy,
        })
        cfg["routes"] = routes

        # 3. 更新 AI 路由分类（关键：让 AI 路由认识新模型）
        ai = cfg.setdefault("ai_routing", {})
        cats = ai.setdefault("categories", {})
        cats[label] = usage or f"{provider} 模型 {model}"
        ai["enabled"] = True
        ai["mode"] = ai.get("mode", "fallback")

        self._save_routes_cfg(cfg)
        _load_keys()
        log_msg(f"Admin: added route '{label}' → {model}")
        self._send_json(200, {"status": "ok", "message": f"Route '{label}' added", "routes": len(routes)})

    def _handle_route_delete(self, body: dict):
        label = body.get("label", "").strip()
        if not label:
            self._send_error(400, "Missing label")
            return
        cfg = self._load_routes_cfg()
        routes = [r for r in cfg.get("routes", []) if r.get("label") != label]
        if len(routes) == len(cfg.get("routes", [])):
            self._send_error(404, f"Route '{label}' not found")
            return
        cfg["routes"] = routes
        # 同步删掉 AI 分类
        cats = cfg.setdefault("ai_routing", {}).setdefault("categories", {})
        cats.pop(label, None)
        self._save_routes_cfg(cfg)
        # 删除对应 key
        if label in KEYS:
            del KEYS[label]
            _save_keys(KEYS)
        log_msg(f"Admin: deleted route '{label}'")
        self._send_json(200, {"status": "ok", "message": f"Route '{label}' deleted"})

    # ── 控制台页面 ──
    ADMIN_HTML_PATH = Path("/root/thalamus/admin.html")

    def _serve_admin_html(self):
        try:
            html = self.ADMIN_HTML_PATH.read_text()
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Thalamus-Version", "lite-1.0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._send_error(500, "admin.html not found in /root/thalamus/")

    def _handle_chat_completion(self, body: dict):
        try:
            is_stream, result, meta = process(body)
        except Exception as e:
            log_msg(f"FATAL: all routes exhausted: {e}")
            traceback.print_exc(file=sys.stderr)
            if body.get("stream", False):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(f'data: {{"error": "Thalamus-Lite: all backends unavailable: {e}"}}\n\n'.encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self._send_error(502, f"All backends unavailable: {e}")
            return

        if is_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Thalamus-Version", "lite-1.0")
            self.end_headers()
            try:
                for chunk in result:
                    data = chunk if isinstance(chunk, bytes) else chunk.encode()
                    self.wfile.write(data)
                    self.wfile.flush()
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                try:
                    self.wfile.write(b"data: [DONE]\n\n")
                except Exception:
                    pass
            except Exception as e:
                log_msg(f"STREAM WRITE ERROR: {e}")
                try:
                    self.wfile.write(b'data: {"error": "Thalamus stream interrupted"}\n\ndata: [DONE]\n\n')
                    self.wfile.flush()
                except Exception:
                    pass
        else:
            self._send_json(200, result, extra_headers={
                "X-Thalamus-Route": meta.get("routed_to", "unknown"),
            })


def main():
    host = "127.0.0.1"
    port = int(os.environ.get("THALAMUS_PORT", "9880"))

    _load_routes()
    _load_keys()

    log_msg("=" * 50)
    log_msg("Thalamus Lite — 精简 AI 路由网关")
    log_msg(f"Listening: {host}:{port}")
    log_msg(f"Routes: {len(ROUTES)} | AI routing: {'ON' if AI_ROUTING.get('enabled') else 'OFF'}")
    log_msg(f"Endpoints: /v1/chat/completions /v1/models /health /admin/api/*")

    server = ThreadingHTTPServer((host, port), ThalamusLiteHandler)
    server.daemon_threads = True

    def shutdown(signum, frame):
        log_msg(f"SHUTDOWN: signal {signum}")

        def _do_shutdown():
            try:
                server.shutdown()
            except Exception as e:
                log_msg(f"SHUTDOWN: error: {e}")

        def _failsafe():
            time.sleep(3)
            log_msg("SHUTDOWN: failsafe hard-exit")
            os._exit(0)

        threading.Thread(target=_do_shutdown, daemon=True).start()
        threading.Thread(target=_failsafe, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_msg("SHUTDOWN: SIGINT")
    finally:
        log_msg("SHUTDOWN: complete")


if __name__ == "__main__":
    main()
