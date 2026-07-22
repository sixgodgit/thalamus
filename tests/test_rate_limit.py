"""Test Thalamus rate limiting logic."""
import sys, os, time, threading, unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock paths before import
with patch("thalamus.ROUTES_PATH", new=MagicMock()), \
     patch("thalamus.KEYS_PATH", new=MagicMock()), \
     patch("thalamus.ADMIN_PWD_PATH", new=MagicMock()), \
     patch("thalamus.LOG_PATH", new=MagicMock()), \
     patch("thalamus.EVENTS_LOG_PATH", new=MagicMock()):
    import thalamus


class TestRateLimit(unittest.TestCase):

    def setUp(self):
        """Clear rate limit state before each test"""
        thalamus._RATE_LIMIT.clear()

    def test_first_request_allowed(self):
        """第一个请求应通过"""
        ok, reason = thalamus._rate_limit_check("10.0.0.1")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_burst_exceeded(self):
        """突发超过 burst_tokens 应被拒绝"""
        ip = "10.0.0.2"
        for i in range(thalamus._RATE_LIMIT_CONFIG["burst_tokens"]):
            ok, _ = thalamus._rate_limit_check(ip)
            thalamus._rate_limit_release(ip)
            self.assertTrue(ok, f"请求#{i+1}应通过")
        # 下一个应被拒绝(令牌耗尽)
        ok, reason = thalamus._rate_limit_check(ip)
        self.assertFalse(ok)
        self.assertIn("burst", reason)

    def test_token_refill(self):
        """等待1秒后令牌应恢复"""
        ip = "10.0.0.3"
        # 消耗所有令牌
        for _ in range(thalamus._RATE_LIMIT_CONFIG["burst_tokens"]):
            thalamus._rate_limit_check(ip)
            thalamus._rate_limit_release(ip)
        ok, _ = thalamus._rate_limit_check(ip)
        self.assertFalse(ok, "令牌耗尽后应拒绝")
        # 模拟时间推移 + 强制补充
        entry = thalamus._RATE_LIMIT.get(ip)
        entry["last_refill"] = 0  # 假装很久没补充
        ok, reason = thalamus._rate_limit_check(ip)
        self.assertTrue(ok, "补充后应通过")

    def test_concurrent_limit(self):
        """并发超过 max_concurrent 应拒绝"""
        ip = "10.0.0.4"
        max_conc = thalamus._RATE_LIMIT_CONFIG["max_concurrent"]
        # 消耗所有并发槽（不release）
        for i in range(max_conc):
            ok, _ = thalamus._rate_limit_check(ip)
            self.assertTrue(ok, f"并发#{i+1}应通过")
        # 下一个应被并发限制拒绝
        ok, reason = thalamus._rate_limit_check(ip)
        self.assertFalse(ok)
        self.assertIn("concurrent", reason)

    def test_concurrent_release(self):
        """release 后并发槽应释放"""
        ip = "10.0.0.5"
        max_conc = thalamus._RATE_LIMIT_CONFIG["max_concurrent"]
        for _ in range(max_conc):
            thalamus._rate_limit_check(ip)
        ok, _ = thalamus._rate_limit_check(ip)
        self.assertFalse(ok, "并发满应拒绝")
        thalamus._rate_limit_release(ip)
        ok, _ = thalamus._rate_limit_check(ip)
        self.assertTrue(ok, "release后应通过")

    def test_window_refresh(self):
        """60秒窗口过期后应重置"""
        ip = "10.0.0.6"
        ok, _ = thalamus._rate_limit_check(ip)
        self.assertTrue(ok)
        # 模拟窗口过期
        thalamus._RATE_LIMIT[ip]["window_start"] = 0
        ok, _ = thalamus._rate_limit_check(ip)
        self.assertTrue(ok, "窗口过期应重置")

    def test_concurrent_release_no_crash(self):
        """release 不存在的 IP 不应报错"""
        thalamus._rate_limit_release("nonexistent")
        # 走到这里就是通过
        self.assertTrue(True)

    def test_parallel_agents_not_blocked_indefinitely(self):
        """模拟5个子Agent各3个请求，应全部通过"""
        ip = "10.0.0.7"
        results = []
        lock = threading.Lock()
        def worker(n):
            for _ in range(3):
                ok, _ = thalamus._rate_limit_check(ip)
                if ok:
                    time.sleep(0.005)
                    thalamus._rate_limit_release(ip)
                with lock:
                    results.append(ok)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        passed = sum(results)
        self.assertEqual(passed, 15, f"5子Agent×3请求应全部通过，但仅{passed}/15通过")
