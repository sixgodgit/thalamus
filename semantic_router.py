"""
Semantic Router for Thalamus
=============================
TF-IDF cosine similarity classifier, used as fallback only.

Routes are defined in routes.json (regex-based, highest priority).
Semantic classification only kicks in when no regex matches.

Architecture (priority order):
  1. regex_match from routes.json → immediate route (no semantic override)
  2. semantic fallback (TF-IDF, threshold 0.28) → used only if regex failed
  3. None → default route (deepseek-v4-flash)
"""

import os, json, logging
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Route profiles: example queries per route label
# ──────────────────────────────────────────────
# Each route gets a list of example queries. More examples = better coverage.
# The embedding is computed lazily on first request.

_DEFAULT_PROFILES = {
    "mimo": [
        "帮我写一个 Python 排序算法",
        "修复这个 bug",
        "部署 nginx 配置文件",
        "写一个 React 组件",
        "这个代码报错了，帮我看看",
        "用 golang 实现一个 http server",
        "调试这个 Dockerfile",
        "写一个 SQL 查询",
        "重构这段代码",
        "这个函数怎么优化性能",
        "实现一个 REST API 端点",
        "帮我写单元测试",
        "git 合并冲突怎么解决",
    ],
    "claude": [
        "分析一下当前的市场趋势",
        "这篇文章的核心观点是什么",
        "比较这两种方案的优劣",
        "为什么这个设计决策是合理的",
        "预测一下未来的发展方向",
        "帮我分析这个商业案例",
        "这个问题的根本原因是什么",
        "论证一下这个观点的正确性",
        "深度分析这个技术方案",
        "写一篇分析报告",
    ],
    "ds": [
        "今天天气怎么样",
        "你好",
        "谢谢你",
        "再见",
        "吃了吗",
        "讲个笑话",
        "今天星期几",
        "帮我查一下",
        "你叫什么名字",
        "晚安",
        "周末愉快",
    ],
    "ops_restaurant": [
        "帮我翻译这个菜单成荷兰语",
        "BTW税率是多少",
        "这周的食材成本分析",
        "帮我写一个Instagram推广文案",
        "这个月的利润报表",
        "NVWA卫生检查要求",
        "荷兰最低工资2026标准",
        "回复这个差评",
        "Thuisbezorgd 佣金是多少",
        "设计一个特价套餐",
        "这个月营业额怎么样",
        "客人投诉菜凉了怎么回复",
    ],
    "translate": [
        "把这个菜单翻译成荷兰语",
        "番茄炒蛋用荷兰语怎么说",
        "帮我翻译这段中文到英文",
        "这句话翻成荷兰语",
        "英文菜单翻译",
        "中餐菜品荷兰语翻译",
        "用荷兰语写个菜单描述",
    ],
    "龙猫": [
        "帮我写一个 Python 排序算法",
        "修复这个 bug，报错信息是 IndexError",
        "部署 nginx 配置文件到服务器",
        "用 golang 实现一个 http server",
        "调试这个 Dockerfile",
        "写一个 SQL 查询，联表统计",
        "重构这段代码，性能优化",
        "git 合并冲突怎么解决",
        "这个代码报错了，帮我看看",
        "帮我写单元测试覆盖这个函数",
        "配置一下 Jenkins CI/CD 流水线",
        "写一个 bash 脚本批量处理日志",
        "这个 Kubernetes pod 起不来，排查一下",
    ],
}

# Thresholds
_SEMANTIC_CONFIDENCE_THRESHOLD = 0.28  # minimum cosine sim to use semantic result

# Lazy init
_vectorizer = None
_route_vectors = None
_route_labels = []
_profile_cache = None


def _init():
    """Lazy initialization of TF-IDF model and route vectors."""
    global _vectorizer, _route_vectors, _route_labels, _profile_cache
    
    if _vectorizer is not None:
        return
    
    # 1. Build profile texts
    profile_texts = []
    _route_labels = []
    for label, examples in _DEFAULT_PROFILES.items():
        for ex in examples:
            profile_texts.append(ex)
            _route_labels.append(label)
    
    # 2. Fit TF-IDF
    _vectorizer = TfidfVectorizer(
        max_features=20000,
        stop_words=None,
        sublinear_tf=True,
        norm='l2',
        analyzer='char_wb',  # character n-grams help with CJK and mixed languages
        ngram_range=(2, 5)
    )
    _route_vectors = _vectorizer.fit_transform(profile_texts)
    
    _profile_cache = {}
    logger.info(
        f"Semantic router initialized: {len(set(_route_labels))} routes, "
        f"{len(profile_texts)} profile examples"
    )


def classify_semantic(text: str) -> tuple[str, float] | None:
    """
    Classify text using TF-IDF cosine similarity.
    Used as fallback when regex (routes.json) doesn't match.
    
    Args:
        text: The user's query text
    
    Returns:
        (label, confidence) or None if below threshold
    """
    _init()
    if not text or not text.strip():
        return None
    
    # Vectorize the query
    query_vec = _vectorizer.transform([text])
    
    # Compute cosine similarity against all profile examples
    similarities = cosine_similarity(query_vec, _route_vectors).flatten()
    
    # Aggregate by route label: max similarity per route
    route_scores = {}
    for i, label in enumerate(_route_labels):
        score = float(similarities[i])
        if label not in route_scores or score > route_scores[label]:
            route_scores[label] = score
    
    # Sort by score descending
    sorted_routes = sorted(route_scores.items(), key=lambda x: x[1], reverse=True)
    
    if not sorted_routes:
        return None
    
    best_label, best_score = sorted_routes[0]
    
    # Check threshold
    if best_score < _SEMANTIC_CONFIDENCE_THRESHOLD:
        return None
    
    return (best_label, best_score)


def get_debug_info(text: str) -> dict:
    """Return debugging info about the semantic classification."""
    _init()
    if not text:
        return {"error": "empty text"}
    
    query_vec = _vectorizer.transform([text])
    similarities = cosine_similarity(query_vec, _route_vectors).flatten()
    
    route_scores = {}
    for i, label in enumerate(_route_labels):
        score = float(similarities[i])
        if label not in route_scores or score > route_scores[label]:
            route_scores[label] = score
    
    sorted_routes = sorted(route_scores.items(), key=lambda x: x[1], reverse=True)
    
    return {
        "top_3": sorted_routes[:3],
        "all_routes": sorted_routes,
        "profile_count": len(_route_labels),
        "vectorizer_params": {
            "analyzer": "char_wb",
            "ngram_range": (2, 5),
            "max_features": 20000,
        }
    }
