import unittest
from datetime import datetime, timedelta, timezone

from scheduler.market_hours import is_market_closed, market_closed_status


def bj(year, month, day, hour, minute=0) -> datetime:
    """Build a UTC datetime from Beijing wall-clock time."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=8)))


class MarketHoursTests(unittest.TestCase):
    def test_weekend_closure_beijing(self):
        # 2026-11-27 is Friday, 2026-11-28 Saturday, 2026-11-30 Monday.
        cases = [
            (bj(2026, 11, 27, 23, 59), False),
            (bj(2026, 11, 28, 5, 59), False),
            (bj(2026, 11, 28, 6, 0), True),
            (bj(2026, 11, 29, 12, 0), True),
            (bj(2026, 11, 30, 0, 1), True),
            (bj(2026, 11, 30, 5, 59), True),
            (bj(2026, 11, 30, 6, 0), False),
            (bj(2026, 12, 1, 1, 0), False),
        ]
        for moment, expected in cases:
            with self.subTest(moment=moment.isoformat()):
                self.assertEqual(is_market_closed(moment), expected)

    def test_reason_mentions_weekend_closure(self):
        closed, reason = market_closed_status(bj(2026, 11, 29, 12, 0))
        self.assertTrue(closed)
        self.assertIn("周末休市", reason)
        self.assertIn("周六", reason)
        self.assertIn("周一", reason)

    def test_open_reason(self):
        closed, reason = market_closed_status(bj(2026, 11, 30, 6, 0))
        self.assertFalse(closed)
        self.assertIn("交易", reason)


if __name__ == "__main__":
    unittest.main()
