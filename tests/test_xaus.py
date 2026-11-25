import unittest
from datetime import datetime, timedelta, timezone

from data.xaus import (
    data_as_of, is_fresh, parse_history, parse_intraday, parse_ohlc,
)


def utc_now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


class XausFreshnessTests(unittest.TestCase):
    def payload(self, **overrides):
        payload = {
            "updated_at": utc_now_iso(),
            "data_state": {
                "status": "fresh",
                "as_of": utc_now_iso(-60),
                "source": "upstream",
                "age_seconds": 60,
            },
        }
        payload.update(overrides)
        return payload

    def test_fresh_payload_accepted(self):
        self.assertTrue(is_fresh(self.payload(), max_age_seconds=300))

    def test_stale_status_rejected(self):
        payload = self.payload()
        payload["data_state"]["status"] = "stale"
        self.assertFalse(is_fresh(payload, max_age_seconds=300))

    def test_stale_flag_rejected(self):
        self.assertFalse(is_fresh(self.payload(stale=True), max_age_seconds=300))

    def test_unavailable_status_rejected(self):
        payload = self.payload()
        payload["data_state"]["status"] = "unavailable"
        self.assertFalse(is_fresh(payload, max_age_seconds=300))

    def test_old_as_of_rejected(self):
        payload = self.payload()
        payload["data_state"]["as_of"] = utc_now_iso(-3600)
        self.assertFalse(is_fresh(payload, max_age_seconds=300))

    def test_future_as_of_rejected(self):
        payload = self.payload()
        payload["data_state"]["as_of"] = utc_now_iso(600)
        self.assertFalse(is_fresh(payload, max_age_seconds=300))

    def test_missing_as_of_rejected(self):
        payload = self.payload()
        payload["data_state"] = {"status": "fresh"}
        self.assertFalse(is_fresh(payload, max_age_seconds=300))

    def test_data_as_of_prefers_state(self):
        payload = {
            "updated_at": utc_now_iso(-10),
            "data_state": {"as_of": utc_now_iso(-100)},
        }
        moment = data_as_of(payload)
        self.assertIsNotNone(moment)
        self.assertLess((datetime.now(timezone.utc) - moment).total_seconds(), 200)


class XausParserTests(unittest.TestCase):
    def test_parse_ohlc_drops_unclosed_and_sorts(self):
        now = int(datetime.now(timezone.utc).timestamp())
        payload = {"points": [
            {"t": now + 3600, "o": 10, "h": 12, "l": 9, "c": 11, "v": 5},
            {"t": now - 7200, "o": 8, "h": 10, "l": 7, "c": 9, "v": 3},
            {"t": now - 3600, "o": 9, "h": 11, "l": 8, "c": 10, "v": 4},
        ]}
        rows = parse_ohlc(payload, interval_seconds=3600)
        self.assertEqual([row["close"] for row in rows], [9.0, 10.0])
        self.assertEqual(rows[1]["ts"], now - 3600)
        self.assertEqual(rows[0]["end_ts"], now - 3600)
        self.assertEqual(rows[0]["high"], 10.0)
        self.assertEqual(rows[0]["low"], 7.0)

    def test_parse_ohlc_can_keep_unclosed(self):
        now = int(datetime.now(timezone.utc).timestamp())
        payload = {"points": [{"t": now, "o": 9, "h": 11, "l": 8, "c": 10, "v": 4}]}
        rows = parse_ohlc(payload, interval_seconds=3600, drop_unclosed=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 10.0)

    def test_parse_history_drops_in_progress_day(self):
        today = datetime.now(timezone.utc).date()
        yesterday = (today - timedelta(days=1)).isoformat()
        before = (today - timedelta(days=2)).isoformat()
        payload = {"points": [
            {"d": today.isoformat(), "c": 100.0, "h": 105.0, "l": 99.0},
            {"d": yesterday, "c": 98.0, "h": 101.0, "l": 95.0},
            {"d": before, "c": 95.0, "h": 99.0, "l": 92.0},
        ]}
        rows = parse_history(payload)
        self.assertEqual([row["date"] for row in rows], [before, yesterday])
        self.assertEqual(rows[-1]["close"], 98.0)
        self.assertEqual(rows[-1]["open"], 98.0)  # history 无 open，用 close 兜底
        self.assertEqual(rows[-1]["high"], 101.0)
        self.assertEqual(rows[-1]["low"], 95.0)

    def test_parse_history_respects_limit(self):
        today = datetime.now(timezone.utc).date()
        points = [{
            "d": (today - timedelta(days=i + 1)).isoformat(),
            "c": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i,
        } for i in range(5)]
        rows = parse_history({"points": points}, limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["close"], 100.0)  # 最近日线为 100.0

    def test_parse_intraday_sorts_and_filters(self):
        payload = {"points": [
            {"t": 200, "p": 12}, {"t": 100, "p": 11},
            {"t": 300, "p": None}, {"t": 400, "p": 0},
        ]}
        rows = parse_intraday(payload)
        self.assertEqual(rows, [{"ts": 100, "price": 11.0}, {"ts": 200, "price": 12.0}])


if __name__ == "__main__":
    unittest.main()
