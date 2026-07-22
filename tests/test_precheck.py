"""Test Thalamus precheck logic — caching, timeout, fallback."""
import sys, os, unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

with patch("thalamus.ROUTES_PATH", new=MagicMock()), \
     patch("thalamus.KEYS_PATH", new=MagicMock()), \
     patch("thalamus.ADMIN_PWD_PATH", new=MagicMock()), \
     patch("thalamus.LOG_PATH", new=MagicMock()), \
     patch("thalamus.EVENTS_LOG_PATH", new=MagicMock()):
    import thalamus


class TestPrecheck(unittest.TestCase):

    def setUp(self):
        thalamus.PRECHECK_ENABLED = True
        thalamus._PRECHECK_CACHE.clear()
        thalamus._PRECHECK_CACHE_HITS = 0
        thalamus._PRECHECK_CACHE_MISSES = 0

    def test_precheck_disabled_returns_none(self):
        """precheck 禁用时应返回 None"""
        thalamus.PRECHECK_ENABLED = False
        result = thalamus.precheck([{"role": "user", "content": "写一个排序"}])
        self.assertIsNone(result)

    def test_precheck_empty_text_returns_none(self):
        """空消息应返回 None"""
        result = thalamus.precheck([{"role": "user", "content": ""}])
        self.assertIsNone(result)

    def test_precheck_no_user_message_returns_none(self):
        """无 user 消息应返回 None"""
        result = thalamus.precheck([{"role": "assistant", "content": "hello"}])
        self.assertIsNone(result)

    @patch("thalamus._make_request")
    def test_precheck_cache_hit(self, mock_request):
        """缓存命中时不走网络"""
        text = "用 Python 解析日期字符串"
        key = thalamus._precheck_cache_key(text)
        thalamus._precheck_cache_set(key, {"intercepted": True, "category": "stdlib", "suggestion": "datetime.strptime"})
        result = thalamus.precheck([{"role": "user", "content": text}])
        self.assertIsNotNone(result)
        self.assertTrue(result.get("cache_hit"))
        mock_request.assert_not_called()

    @patch.dict(os.environ, {"deepseek": "sk-test"}, clear=False)
    @patch("thalamus._make_request")
    def test_precheck_cache_miss(self, mock_request):
        """缓存未命中时走网络，intercepted=False应正确返回"""
        mock_request.return_value = {
            "choices": [{"message": {"content": "NO|需要写代码"}}]
        }
        text = "用 Python 解析日期"
        result = thalamus.precheck([{"role": "user", "content": text}])
        # intercepted=False → 返回 dict 但不拦截
        self.assertIsNotNone(result)
        self.assertFalse(result["intercepted"])
        mock_request.assert_called_once()

    @patch("thalamus._make_request")
    def test_precheck_timeout_returns_none(self, mock_request):
        """超时应返回 None（不阻塞）"""
        mock_request.side_effect = TimeoutError("timeout")
        result = thalamus.precheck([{"role": "user", "content": "写个排序算法"}])
        self.assertIsNone(result)

    @patch("thalamus._make_request")
    def test_precheck_error_returns_none(self, mock_request):
        """网络错误应返回 None（不阻塞）"""
        mock_request.side_effect = RuntimeError("connection failed")
        result = thalamus.precheck([{"role": "user", "content": "写个排序算法"}])
        self.assertIsNone(result)

    def test_precheck_intercepted_flow(self):
        """precheck 拦截时返回建议"""
        # 直接测缓存机制
        text = "用标准库排序列表"
        key = thalamus._precheck_cache_key(text)
        cached = {"intercepted": True, "category": "stdlib", "suggestion": "sorted()", "latency": 0.3}
        thalamus._precheck_cache_set(key, cached)
        result = thalamus.precheck([{"role": "user", "content": text}])
        self.assertIsNotNone(result)
        self.assertTrue(result.get("intercepted"))
        self.assertEqual(result["category"], "stdlib")
