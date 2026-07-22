"""Test Thalamus key resolution — KEYS dict, env vars, fallback."""
import sys, os, unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

with patch("thalamus.ROUTES_PATH", new=MagicMock()), \
     patch("thalamus.KEYS_PATH", new=MagicMock()), \
     patch("thalamus.ADMIN_PWD_PATH", new=MagicMock()), \
     patch("thalamus.LOG_PATH", new=MagicMock()), \
     patch("thalamus.EVENTS_LOG_PATH", new=MagicMock()):
    import thalamus


class TestKeys(unittest.TestCase):

    def setUp(self):
        thalamus.KEYS.clear()

    def test_empty_key_env_returns_empty(self):
        """空 key_env 返回空字符串"""
        self.assertEqual(thalamus._resolve_key(""), "")

    def test_key_from_keys_dict(self):
        """KEYS 中的别名应正确解析"""
        thalamus.KEYS["test_key"] = {"key": "sk-test123", "endpoint": "https://test.com/v1"}
        self.assertEqual(thalamus._resolve_key("test_key"), "sk-test123")

    def test_key_from_env_fallback(self):
        """KEYS 中没有时从环境变量读取"""
        with patch.dict(os.environ, {"MY_API_KEY": "sk-env-key"}):
            self.assertEqual(thalamus._resolve_key("MY_API_KEY"), "sk-env-key")

    def test_key_not_found_returns_empty(self):
        """不存在的 key 返回空字符串"""
        self.assertEqual(thalamus._resolve_key("NONEXISTENT_KEY"), "")

    def test_endpoint_from_keys_dict(self):
        """endpoint 应从 KEYS 字典解析"""
        thalamus.KEYS["test_key"] = {"key": "sk-test", "endpoint": "https://custom.com/v1"}
        self.assertEqual(thalamus._resolve_endpoint("test_key"), "https://custom.com/v1")

    def test_endpoint_not_found_returns_empty(self):
        """找不到 endpoint 返回空"""
        self.assertEqual(thalamus._resolve_endpoint("NONEXISTENT"), "")

    def test_endpoint_empty_key_env(self):
        """空 key_env 返回空 endpoint"""
        self.assertEqual(thalamus._resolve_endpoint(""), "")
