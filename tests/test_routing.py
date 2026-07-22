"""Test Thalamus routing — regex + semantic classification."""
import sys, os, json, re, unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock routes.json before importing thalamus
TEST_ROUTES = {
    "routes": [
        {
            "label": "longcat",
            "pattern": "代码|编程|重构|部署|docker|python|go|rust|修复|报错|debug",
            "model": "longcat-v2",
            "provider": "meituan",
            "endpoint": "https://api.longcat.chat/v1",
            "key_env": "longcat",
            "proxy": False,
            "fallbacks": [{"model": "deepseek-v4-flash", "provider": "deepseek", "key_env": "deepseek", "endpoint": "https://api.deepseek.com/v1"}],
        },
        {
            "label": "claude",
            "pattern": "分析|对比|推理|为什么|原因|评估|策略|方案|设计|优缺点|权衡",
            "model": "claude-sonnet-5",
            "provider": "token173",
            "endpoint": "https://token173.com/v1",
            "key_env": "token173",
            "proxy": True,
            "fallbacks": [],
        },
        {
            "label": "vision",
            "pattern": "图片|截图|照片|vision|ocr|OCR|视觉|多媒体|image",
            "model": "gpt-4o-mini",
            "provider": "token173",
            "endpoint": "https://token173.com/v1",
            "key_env": "token173",
            "proxy": True,
            "fallbacks": [],
        },
    ],
    "default": {"model": "deepseek-v4-flash", "provider": "deepseek", "endpoint": "https://api.deepseek.com/v1", "key_env": "DEEPSEEK_API_KEY"},
    "fallback": {"model": "longcat-v2", "provider": "meituan", "endpoint": "https://api.longcat.chat/v1", "key_env": "longcat"},
    "precheck": {"enabled": False},
}


@patch("thalamus.ROUTES_PATH", new=MagicMock())
@patch("thalamus.KEYS_PATH", new=MagicMock())
@patch("thalamus.ADMIN_PWD_PATH", new=MagicMock())
@patch("thalamus.LOG_PATH", new=MagicMock())
@patch("thalamus.EVENTS_LOG_PATH", new=MagicMock())
class TestRouting(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Import after all patching
        import importlib
        global thalamus
        # Ensure fresh import
        for mod in list(sys.modules.keys()):
            if 'thalamus' in mod and 'test_' not in mod:
                del sys.modules[mod]
        import thalamus as _t
        global thalamus
        thalamus = _t
        # Set up test routes
        thalamus.ROUTES = []
        for r in TEST_ROUTES["routes"]:
            fbs = r.get("fallbacks", [])
            thalamus.ROUTES.append((
                re.compile(r["pattern"], re.IGNORECASE),
                r["model"], r["provider"], r["endpoint"], r["key_env"],
                r["label"], r.get("proxy", False), fbs,
            ))
        thalamus.DEFAULT_MODEL = TEST_ROUTES["default"]["model"]
        thalamus.DEFAULT_PROVIDER = TEST_ROUTES["default"]["provider"]
        thalamus.DEFAULT_ENDPOINT = TEST_ROUTES["default"]["endpoint"]
        thalamus.DEFAULT_KEY_ENV = TEST_ROUTES["default"]["key_env"]
        thalamus.PRECHECK_ENABLED = False

    def test_regex_code_route(self):
        """代码类请求应命中 longcat 路由"""
        result = thalamus.classify([{"role": "user", "content": "帮我写一个 Python 排序算法"}])
        self.assertIsNotNone(result)
        self.assertEqual(result[4], "longcat")  # label

    def test_regex_reasoning_route(self):
        """分析类请求应命中 claude 路由"""
        result = thalamus.classify([{"role": "user", "content": "分析一下这个方案的技术优缺点"}])
        self.assertIsNotNone(result)
        self.assertEqual(result[4], "claude")

    def test_regex_vision_route(self):
        """图片类请求应命中 vision 路由"""
        result = thalamus.classify([{"role": "user", "content": "帮我看一下这张图片里的文字"}])
        self.assertIsNotNone(result)
        self.assertEqual(result[4], "vision")

    def test_default_route(self):
        """没有匹配正则的应返回 None（走默认）"""
        result = thalamus.classify([{"role": "user", "content": "今天天气怎么样"}])
        self.assertIsNone(result)

    def test_multiline_input(self):
        """多行输入应正常匹配"""
        result = thalamus.classify([{"role": "user", "content": "帮我写一个 Dockerfile\n部署 nginx"}])
        self.assertIsNotNone(result)
        self.assertEqual(result[4], "longcat")

    def test_last_message_weighted(self):
        """最近一条用户消息权重更高"""
        result = thalamus.classify([
            {"role": "user", "content": "今天天气怎么样"},
            {"role": "assistant", "content": "今天晴天"},
            {"role": "user", "content": "帮我修一下这个Python代码"},
        ])
        self.assertIsNotNone(result)
        self.assertEqual(result[4], "longcat")

    def test_empty_messages(self):
        """空消息应返回 None"""
        result = thalamus.classify([])
        self.assertIsNone(result)

    def test_no_user_messages(self):
        """只有 assistant 消息应返回 None"""
        result = thalamus.classify([{"role": "assistant", "content": "你好"}])
        self.assertIsNone(result)

    def test_mixed_content_type(self):
        """混图片+文本的请求，文本部分应被提取"""
        result = thalamus.classify([
            {"role": "user", "content": [
                {"type": "text", "text": "帮我分析这段代码性能问题"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
            ]}
        ])
        self.assertIsNotNone(result)
        # 文本提取后应匹配代码路由
        self.assertEqual(result[4], "longcat")

    def test_keyword_in_tool_result(self):
        """工具结果中的内容不应影响路由"""
        result = thalamus.classify([
            {"role": "user", "content": "帮我看看"},
            {"role": "tool", "content": "代码执行结果: 报错信息如下..."},
        ])
        # 仅 user 消息参与路由
        self.assertIsNone(result)

    def test_fallback_list_in_route(self):
        """路由应包含 fallbacks"""
        result = thalamus.classify([{"role": "user", "content": "帮我写个python程序"}])
        self.assertIsNotNone(result)
        # 7-tuple: (endpoint, model, key_env, provider, label, proxy, fallbacks)
        self.assertEqual(len(result), 7)
        self.assertIsInstance(result[6], list)
