#!/usr/bin/env python3
"""
Thalamus — Hermes 模型调度中枢
瀑布式多模型调度器，基于大脑分区架构：

  前额叶：规则引擎（正则匹配，0延迟）
  左脑：  MiMo 2.5 Pro（代码/部署）
  右脑：  OpenRouter Auto（复杂推理）
  小脑：  MiMo v2 Omni（视觉）
  脑干：  DeepSeek V4（日常默认）

运行: python3 thalamus.py
端口: 9880
"""

import json
import os
import re
import ssl
import time
import threading
import urllib.request
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

# ─── 加载配置 ───
CONFIG_PATH = Path(__file__).parent / "policies.yaml"
AUDIT_LOG = Path(os.environ.get("THALAMUS_LOG", "/root/.hermes/logs/thalamus.log"))

# ─── 简化配置加载（硬编码关键值） ───
ROUTES = [
    {
        "pattern": re.compile(
            r"(代码|编程|写.*程序|修.*bug|重构|部署|deploy|运维|server|服务器|nginx|docker|"
            r"systemctl|ssh|git|commit|PR|pull.request|config|配置.*文件|yaml|json.*修改|"
            r"shell|bash|脚本|写.*代码|实现.*功能|修复|报错|error|exception|traceback|"
            r"日志.*分析|排查|调试|debug|cron|备份|快照)"
        ),
        "model": "mimo-v2.5-pro",
        "provider": "xiaomi",
        "endpoint": "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
        "key_env": "XIAOMI_API_KEY",
        "label": "MiMo 2.5 Pro (代码/部署)"
    },
    {
        "pattern": re.compile(
            r"(分析|对比|比较|推理|为什么|原因|根因|深度|论证|评估|策略|方案|"
            r"设计.*架构|优缺点|权衡|决策)"
        ),
        "model": "openrouter/auto",
        "provider": "openrouter",
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "label": "OpenRouter Auto (复杂推理)"
    },
    {
        "pattern": re.compile(r"(图片|截图|照片|图像|看图|OCR|识别.*图|vision|视觉)"),
        "model": "mimo-v2-omni",
        "provider": "xiaomi",
        "endpoint": "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
        "key_env": "XIAOMI_API_KEY",
        "label": "MiMo v2 Omni (视觉)"
    },
]

DEFAULT_ROUTE = {
    "model": "deepseek-chat",
    "provider": "deepseek",
    "endpoint": "https://api.deepseek.com/v1/chat/completions",
    "key_env": "DEEPSEEK_API_KEY",
    "label": "DeepSeek V4 (默认)"
}

# ─── 策略 ───
POLICIES = {
    "safe":     {"max_tokens": 4096,  "spend_cap": 2.0},
    "standard": {"max_tokens": 16384, "spend_cap": 10.0},
    "yolo":     {"max_tokens": 65536, "spend_cap": 50.0},
}

# ─── 状态 ───
spend_tracker = {"total": 0.0, "by_provider": {}}
spend_lock = threading.Lock()


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def route(text: str) -> dict:
    """第1层：规则引擎匹配"""
    for rule in ROUTES:
        if rule["pattern"].search(text):
            log(f"ROUTE: regex → {rule['label']}")
            return rule
    # 未命中 → 默认（第3层）
    # 第2层（轻量分类）暂不实现：需要额外 API 调用，增加延迟
    log(f"ROUTE: default → {DEFAULT_ROUTE['label']}")
    return DEFAULT_ROUTE


def call_model(route_info: dict, messages: list, policy: str = "standard",
               max_tokens: int = None) -> dict:
    """调用模型 API"""
    endpoint = route_info["endpoint"]
    model = route_info["model"]
    key_env = route_info["key_env"]
    
    key = os.environ.get(key_env, "")
    if not key:
        return {"error": f"API key not found: {key_env}"}
    
    pol = POLICIES.get(policy, POLICIES["standard"])
    actual_max_tokens = max_tokens or pol["max_tokens"]
    
    # 设置 NO_PROXY
    domain = endpoint.split("/")[2]
    proxy_env = os.environ.copy()
    no_proxy = proxy_env.get("NO_PROXY", "")
    if domain not in no_proxy:
        proxy_env["NO_PROXY"] = f"{no_proxy},{domain}" if no_proxy else domain
    
    t_start = time.time()
    
    try:
        req_data = {
            "model": model,
            "messages": messages,
            "max_tokens": actual_max_tokens,
        }
        
        # MiMo 需要额外参数
        if route_info["provider"] == "xiaomi":
            req_data["temperature"] = 0.7
        
        ctx = ssl.create_default_context()
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(req_data).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
        )
        resp = urllib.request.urlopen(req, timeout=120, context=ctx)
        data = json.loads(resp.read())
        
        latency = time.time() - t_start
        content = data["choices"][0]["message"].get("content", "")
        actual_model = data.get("model", model)
        usage = data.get("usage", {})
        
        # 估算费用
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        est_cost = _estimate_cost(route_info["provider"], model, prompt_tokens, completion_tokens)
        
        with spend_lock:
            spend_tracker["total"] += est_cost
            spend_tracker["by_provider"][route_info["provider"]] = (
                spend_tracker["by_provider"].get(route_info["provider"], 0) + est_cost
            )
        
        log(f"CALL: {route_info['label']} | latency={latency:.2f}s | "
            f"tokens={prompt_tokens}+{completion_tokens} | cost=¥{est_cost:.4f} | "
            f"model={actual_model}")
        
        return {
            "success": True,
            "content": content,
            "model": actual_model,
            "provider": route_info["provider"],
            "label": route_info["label"],
            "latency_s": round(latency, 2),
            "tokens": {"prompt": prompt_tokens, "completion": completion_tokens, "total": prompt_tokens + completion_tokens},
            "cost_cny": round(est_cost, 4),
        }
        
    except Exception as e:
        latency = time.time() - t_start
        log(f"ERROR: {route_info['label']} | {e} | latency={latency:.2f}s")
        return {
            "success": False,
            "error": str(e),
            "provider": route_info["provider"],
            "label": route_info["label"],
            "latency_s": round(latency, 2),
        }


def _estimate_cost(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """粗略费用估算（元/百万token）"""
    rates = {
        "deepseek": (0.007, 0.014),    # DeepSeek V4: ¥1/1M in, ¥2/1M out
        "xiaomi": (0.02, 0.02),        # MiMo Token Plan: ~¥2/1M
        "openrouter": (0.02, 0.04),    # OpenRouter 平均
    }
    ri, ro = rates.get(provider, (0.02, 0.04))
    return (prompt_tokens / 1_000_000 * ri) + (completion_tokens / 1_000_000 * ro)


def call_parallel(tasks: list, policy: str = "standard") -> list:
    """并行调用多个模型（用于多模态场景）"""
    results = [None] * len(tasks)
    threads = []
    
    def worker(idx, route_info, messages):
        results[idx] = call_model(route_info, messages, policy)
    
    for i, task in enumerate(tasks):
        route_info = route(task.get("goal", ""))
        messages = [{"role": "user", "content": task.get("prompt", task.get("goal", ""))}]
        t = threading.Thread(target=worker, args=(i, route_info, messages))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join(timeout=180)
    
    return results


# ─── HTTP Server ───
class RouterHandler(BaseHTTPRequestHandler):
    """处理来自 Hermes execute_code 的 HTTP 请求"""
    
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, {"error": "Invalid JSON"})
            return
        
        if self.path == "/task":
            self._handle_task(data)
        elif self.path == "/parallel":
            self._handle_parallel(data)
        elif self.path == "/health":
            self._respond(200, {"status": "ok", "version": "1.0.0"})
        elif self.path == "/stats":
            self._respond(200, spend_tracker)
        else:
            self._respond(404, {"error": "Not found"})
    
    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok", "version": "1.0.0", "uptime": round(time.time() - START_TIME, 0)})
        elif self.path == "/stats":
            self._respond(200, spend_tracker)
        else:
            self._respond(200, {
                "name": "thalamus",
                "version": "1.0.0",
                "layers": ["regex_engine", "default"],
                "routes": [r["label"] for r in ROUTES] + [DEFAULT_ROUTE["label"]],
                "policies": list(POLICIES.keys()),
                "endpoints": ["/task", "/parallel", "/health", "/stats"]
            })
    
    def _handle_task(self, data):
        prompt = data.get("prompt", "") or data.get("goal", "")
        if not prompt:
            self._respond(400, {"error": "Missing 'prompt' or 'goal'"})
            return
        
        policy = data.get("policy", "standard")
        messages = data.get("messages", [{"role": "user", "content": prompt}])
        
        # 路由
        route_info = route(prompt)
        
        # 调用
        result = call_model(route_info, messages, policy)
        
        self._respond(200, {
            "routing": {"method": "regex", "label": route_info["label"]},
            "result": result,
        })
    
    def _handle_parallel(self, data):
        tasks = data.get("tasks", [])
        policy = data.get("policy", "standard")
        
        if not tasks:
            self._respond(400, {"error": "Missing 'tasks'"})
            return
        
        results = call_parallel(tasks, policy)
        self._respond(200, {"results": results})
    
    def _respond(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    
    def log_message(self, format, *args):
        pass  # 抑制默认 HTTP 日志


START_TIME = time.time()


def main():
    host = "127.0.0.1"
    port = 9880
    
    # 检查端口
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.close()
    except OSError:
        log(f"WARN: Port {port} already in use, trying 9881")
        port = 9881
    
    log(f"START: thalamus v1.0.0 on {host}:{port}")
    log(f"ROUTES: {len(ROUTES)} regex rules + default")
    log(f"POLICIES: {list(POLICIES.keys())}")
    
    server = HTTPServer((host, port), RouterHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("STOP: shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
