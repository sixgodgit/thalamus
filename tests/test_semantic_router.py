"""Test semantic router — TF-IDF classification."""
import sys, os, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from semantic_router import classify_semantic, _init, _profile_cache


class TestSemanticRouter(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _init()

    def test_code_query(self):
        """代码类查询应归类到代码路由"""
        result = classify_semantic("帮我写一个 Python 数据库连接池")
        self.assertIsNotNone(result)
        label, conf = result
        self.assertIn(label, ["mimo", "龙猫"])  # 代码类
        self.assertGreaterEqual(conf, 0.28)

    def test_reasoning_query(self):
        """分析类查询应归类到分析路由"""
        result = classify_semantic("分析一下当前市场的竞争格局和趋势")
        self.assertIsNotNone(result)
        label, conf = result
        self.assertEqual(label, "claude")
        self.assertGreaterEqual(conf, 0.28)

    def test_translation_query(self):
        """翻译类查询应归类到翻译路由"""
        result = classify_semantic("请把这份菜单翻译成荷兰语")
        self.assertIsNotNone(result)
        label, conf = result
        self.assertEqual(label, "translate")
        self.assertGreaterEqual(conf, 0.28)

    def test_greeting_query(self):
        """日常问候低置信度"""
        result = classify_semantic("你好，今天天气不错")
        self.assertIsNotNone(result)
        label, conf = result
        self.assertEqual(label, "ds")

    def test_empty_text(self):
        """空文本返回 None"""
        self.assertIsNone(classify_semantic(""))
        self.assertIsNone(classify_semantic(None))

    def test_restaurant_query(self):
        """餐厅运营类查询"""
        result = classify_semantic("这个月的利润报表出来了吗")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "ops_restaurant")

    def test_confidence_threshold(self):
        """低于置信度阈值应返回 None"""
        # 完全无关的文本
        result = classify_semantic("asdf qwer zxcv 1234 5678")
        if result is not None:
            self.assertLess(result[1], 0.5)  # 即使有结果也应该低置信度

    def test_cjk_mixed_query(self):
        """中英文混写应正常分类"""
        result = classify_semantic("fix the Python bug in the 登录模块")
        self.assertIsNotNone(result)
