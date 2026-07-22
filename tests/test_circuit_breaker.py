"""Test Thalamus circuit breaker state machine."""
import sys, os, time, unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

with patch("thalamus.ROUTES_PATH", new=MagicMock()), \
     patch("thalamus.KEYS_PATH", new=MagicMock()), \
     patch("thalamus.ADMIN_PWD_PATH", new=MagicMock()), \
     patch("thalamus.LOG_PATH", new=MagicMock()), \
     patch("thalamus.EVENTS_LOG_PATH", new=MagicMock()):
    import thalamus


class TestCircuitBreaker(unittest.TestCase):

    def setUp(self):
        thalamus._CIRCUIT_BREAKER.clear()
        thalamus._CIRCUIT_BREAKER_TRIGGERED = 0

    def test_initially_closed(self):
        """熔断器初始状态应为 closed"""
        state = thalamus._circuit_get_state("test_route")
        self.assertFalse(state["is_open"])

    def test_three_failures_trips(self):
        """连续3次失败应触发熔断"""
        for _ in range(3):
            thalamus._circuit_record("test_route", False)
        state = thalamus._circuit_get_state("test_route")
        self.assertTrue(state["is_open"])

    def test_one_failure_not_trip(self):
        """1次失败不触发熔断"""
        thalamus._circuit_record("test_route", False)
        state = thalamus._circuit_get_state("test_route")
        self.assertFalse(state["is_open"])
        self.assertEqual(state["fail_count"], 1)

    def test_success_resets_counter(self):
        """成功应减少失败计数"""
        thalamus._circuit_record("test_route", False)
        thalamus._circuit_record("test_route", False)
        self.assertEqual(thalamus._circuit_get_state("test_route")["fail_count"], 2)
        thalamus._circuit_record("test_route", True)
        self.assertEqual(thalamus._circuit_get_state("test_route")["fail_count"], 1)

    def test_circuit_is_tripped(self):
        """熔断后应被拦截"""
        for _ in range(3):
            thalamus._circuit_record("test_route", False)
        self.assertTrue(thalamus._circuit_is_tripped("test_route"))

    def test_circuit_not_tripped(self):
        """未熔断应放行"""
        self.assertFalse(thalamus._circuit_is_tripped("test_route"))

    def test_half_open_probe(self):
        """熔断60秒后应允许一次探测请求"""
        for _ in range(3):
            thalamus._circuit_record("test_route", False)
        self.assertTrue(thalamus._circuit_is_tripped("test_route"))
        # 模拟时间推移
        state = thalamus._circuit_get_state("test_route")
        state["last_fail_ts"] = 0  # 远早于现在
        # 应放行探测
        self.assertFalse(thalamus._circuit_is_tripped("test_route"))

    def test_half_open_success_closes(self):
        """探测成功应关闭熔断，fail_count归零"""
        for _ in range(3):
            thalamus._circuit_record("test_route", False)
        # 半开
        state = thalamus._circuit_get_state("test_route")
        state["last_fail_ts"] = 0
        thalamus._circuit_is_tripped("test_route")  # 放行探测
        # 探测成功 — 应完全关闭熔断
        thalamus._circuit_record("test_route", True)
        state = thalamus._circuit_get_state("test_route")
        self.assertFalse(state["is_open"])
        # 半开成功应重置fail_count到0（见_circuit_record: is_open时成功→fail_count=0）
        self.assertEqual(state["fail_count"], 0)

    def test_multiple_routes_independent(self):
        """不同路由的熔断状态应独立"""
        for _ in range(3):
            thalamus._circuit_record("route_A", False)
        thalamus._circuit_record("route_B", True)
        self.assertTrue(thalamus._circuit_is_tripped("route_A"))
        self.assertFalse(thalamus._circuit_is_tripped("route_B"))

    def test_triggered_counter(self):
        """熔断触发计数应递增"""
        self.assertEqual(thalamus._CIRCUIT_BREAKER_TRIGGERED, 0)
        for _ in range(3):
            thalamus._circuit_record("test_route", False)
        self.assertEqual(thalamus._CIRCUIT_BREAKER_TRIGGERED, 1)
