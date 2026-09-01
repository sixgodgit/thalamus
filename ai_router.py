"""
AI Router for Thalamus
======================
Uses a cheap LLM to classify user messages into route labels, replacing the
TF-IDF semantic fallback with real semantic understanding.

Config (in routes.json):
    "ai_routing": {
        "enabled": true,
        "mode": "fallback",     # fallback = only when regex misses (default)
                                # always   = every request goes through AI
        "model": "deepseek-v4-flash",
        "provider": "deepseek",
        "key_env": "deepseek",
        "endpoint": "https://api.deepseek.com/v1/chat/completions",
        "timeout": 10,
        "categories": {
            "ds": "编程、代码编写、调试、部署、系统运维等技术操作",
            "gpt": "深度分析、逻辑推理、对比评估、复杂决策",
            "qwen": "图片处理、截图识别、OCR、视觉、多媒体",
            "general": "日常对话、简单问答、闲聊、翻译、其他"
        }
    }

Flow in classify():
    regex match (routes.json) → immediate route
    regex miss → AI classify → label found in routes? → route
              → AI fail / label unknown → TF-IDF semantic → default
"""

import hashlib
import json
import logging
import re
import time

logger = logging.getLogger(__name__)

# ── LRU cache: same text → same route, avoid burning tokens ──
_CACHE: dict = {}
_CACHE_MAX = 512
_CACHE_TTL = 3600  # seconds

_SYSTEM_PROMPT = (
    "You are a message router for a model gateway. Based on the user message, "
    "choose the single most appropriate route category from the list below. "
    "Reply with ONLY a JSON object, no other text: {{\"route\": \"<label>\"}}\n\n"
    "Categories:\n{categories}\n\n"
    "Rules:\n"
    "- If the message asks for code, debugging, deployment, or system ops → pick the code category\n"
    "- If it needs deep analysis, reasoning, comparison, evaluation → pick the reasoning category\n"
    "- If it involves images, screenshots, OCR, vision → pick the vision category\n"
    "- Anything else (chat, simple Q&A, translation, casual) → general\n"
)


def _cache_key(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()


def _cache_get(key: str) -> str | None:
    entry = _CACHE.get(key)
    if not entry:
        return None
    ts, label = entry
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return label


def _cache_set(key: str, label: str) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        # drop oldest
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (time.time(), label)


def _strip_json(text: str) -> str:
    """Extract JSON object from model output (tolerates code fences)."""
    text = text.strip()
    # remove markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{[^{}]*\}", text)
    return m.group(0) if m else text


def classify_with_ai(text: str, cfg: dict) -> tuple[str | None, float]:
    """
    Ask an LLM which route label fits the message.
    Returns (label, confidence) — confidence is 1.0 on success, 0.0 on failure.
    """
    if not cfg or not cfg.get("enabled"):
        return None, 0.0

    text = (text or "").strip()
    if not text:
        return None, 0.0

    # Cache hit?
    key = _cache_key(text)
    cached = _cache_get(key)
    if cached:
        logger.info("ai_router: cache hit → %s", cached)
        return (None if cached == "general" else cached), 1.0

    categories = cfg.get("categories", {})
    cat_text = "\n".join(f"- {label}: {desc}" for label, desc in categories.items())

    prompt = _SYSTEM_PROMPT.format(categories=cat_text)
    model = cfg.get("model", "deepseek-v4-flash")
    endpoint = cfg.get("endpoint", "https://api.deepseek.com/v1/chat/completions")
    key_env = cfg.get("key_env", "deepseek")
    api_key = _get_api_key(key_env)
    timeout = float(cfg.get("timeout", 10))

    if not api_key:
        logger.warning("ai_router: no API key for key_env=%s", key_env)
        return None, 0.0

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text[:2000]},  # cap length
        ],
        "max_tokens": 16,
        "temperature": 0,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        import urllib.request

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        label = body["choices"][0]["message"]["content"]
        parsed = json.loads(_strip_json(label))
        route = str(parsed.get("route", "")).strip().lower()

        if not route:
            return None, 0.0
        _cache_set(key, route)
        logger.info("ai_router: %s → %s", text[:40], route)
        return (None if route == "general" else route), 1.0
    except Exception as e:
        logger.debug("ai_router: classification failed: %s", e)
        return None, 0.0


def _get_api_key(key_env: str) -> str:
    """Load API key from env var or keys.json."""
    import os

    val = os.getenv(key_env.upper()) or os.getenv(key_env)
    if val:
        return val
    # fall back to keys.json
    try:
        from pathlib import Path

        keys_path = Path("/root/thalamus/keys.json")
        if keys_path.exists():
            with open(keys_path) as f:
                keys = json.load(f)
            entry = keys.get(key_env)
            if isinstance(entry, dict):
                return entry.get("key", "")
            if isinstance(entry, str):
                return entry
    except Exception:
        pass
    return ""


def get_debug_stats() -> dict:
    """Return cache stats for admin panel / debugging."""
    return {"cache_size": len(_CACHE), "cache_max": _CACHE_MAX, "cache_ttl": _CACHE_TTL}
