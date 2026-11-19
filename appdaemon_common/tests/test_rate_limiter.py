import unittest

from datetime import datetime, timedelta

from appdaemon_common.rate_limiter import RateLimiter

LIMIT = 60
WINDOW_SECONDS = 60


class TestRateLimiter(unittest.TestCase):

    def setUp(self):
        self.limiter = RateLimiter(LIMIT, WINDOW_SECONDS)
        self.t0 = datetime(2026, 4, 15, 12, 0, 0)

    # Commands a millisecond apart, like the runaway loop the limit guards against.
    def _burst(self, count, start=0):
        return [
            self.limiter.record(self.t0 + timedelta(milliseconds=start + i)) for i in range(count)
        ]

    def test_within_limit_is_allowed(self):
        self.assertEqual([False] * LIMIT, self._burst(LIMIT))

    def test_exceeding_limit_trips(self):
        self._burst(LIMIT)
        # One more within the window trips the limit.
        self.assertTrue(self._burst(1, start=LIMIT)[0])

    def test_old_commands_slide_out_of_window(self):
        for i in range(LIMIT):
            self.limiter.record(self.t0 + timedelta(seconds=i))
        # A command well past the window resets the effective count.
        later = self.t0 + timedelta(seconds=WINDOW_SECONDS + 10)
        self.assertFalse(self.limiter.record(later))


if __name__ == "__main__":
    unittest.main()
