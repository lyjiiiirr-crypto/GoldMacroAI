import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from data.technical import (
    atr, build_report, macd, parse_yahoo_ohlc, rsi, sma, support_resistance,
)
from database.models import Database
from scheduler.jobs import build_market, prefer_fresh_gold_technical


def ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


def rising_candles(count: int = 80) -> list[dict]:
    start = date(2026, 9, 1)
    rows = []
    for i in range(count):
        base = 100.0 + i
        rows.append({
            "date": (start + timedelta(days=i)).isoformat(),
            "open": base - 1.0, "high": base + 2.0,
            "low": base - 3.0, "close": base, "volume": 100.0,
        })
    return rows


class TechnicalParserTests(unittest.TestCase):
    def test_parse_yahoo_ohlc_skips_nulls_and_sorts(self):
        payload = {"chart": {"result": [{
            "timestamp": [ms("2026-11-21"), ms("2026-11-20")],
            "indicators": {"quote": [{
                "open": [101, 100], "high": [103, 102], "low": [100, 99],
                "close": [None, 101], "volume": [11, 10],
            }]},
        }]}}
        rows = parse_yahoo_ohlc(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], {
            "date": "2026-11-20", "open": 100.0, "high": 102.0,
            "low": 99.0, "close": 101.0, "volume": 10.0,
        })
        self.assertEqual(parse_yahoo_ohlc({"chart": {"result": []}}), [])

    def test_parse_yahoo_ohlc_seconds_timestamps(self):
        ts = int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp())
        payload = {"chart": {"result": [{
            "timestamp": [ts],
            "indicators": {"quote": [{
                "open": [100], "high": [102], "low": [99],
                "close": [101], "volume": [10],
            }]},
        }]}}
        self.assertEqual(parse_yahoo_ohlc(payload)[0]["date"], "2026-11-20")

    def test_indicators(self):
        self.assertEqual(sma([1, 2, 3, 4, 5], 3), 4.0)
        self.assertIsNone(sma([1, 2], 3))
        self.assertEqual(rsi(list(range(1, 20))), 100.0)
        self.assertEqual(rsi(list(range(19, 0, -1))), 0.0)
        self.assertIsNone(rsi([1, 2, 3], 14))
        self.assertGreater(macd(list(range(1, 45)))["histogram"], 0)
        self.assertLess(macd(list(range(44, 0, -1)))["histogram"], 0)
        self.assertIsNone(macd([1, 2, 3]))

    def test_atr_uses_true_range(self):
        candles = [{
            "date": f"d{i}", "open": 100.0, "high": 101.0,
            "low": 99.0, "close": 100.0,
        } for i in range(15)]
        self.assertEqual(atr(candles, 14), 2.0)
        self.assertIsNone(atr(candles[:5], 14))

    def test_build_report_bias_and_levels(self):
        candles = rising_candles()
        report = build_report("GC=F", candles)
        self.assertEqual(report["bias"], "偏多")
        self.assertGreaterEqual(report["score"], 2)
        self.assertEqual(report["symbol"], "GC=F")
        self.assertEqual(report["label"], "COMEX黄金期货")
        self.assertIn("非 Trading Central", report["disclaimer"])
        self.assertIn("支撑", report["summary"])
        self.assertTrue(report["levels"]["support"])
        self.assertIsNotNone(report["indicators"]["sma50"])
        self.assertIsNotNone(report["indicators"]["rsi14"])
        self.assertTrue(report["signals"])
        with self.assertRaises(ValueError):
            build_report("GC=F", candles[:5])

    def test_support_resistance_around_close(self):
        candles = rising_candles(40)
        levels = support_resistance(candles, candles[-1]["close"])
        self.assertTrue(all(v < candles[-1]["close"] for v in levels["support"]))
        self.assertTrue(all(v > candles[-1]["close"] for v in levels["resistance"]))


class TechnicalSourcePreferenceTests(unittest.TestCase):
    def test_xaus_hides_stale_yahoo_gold_but_keeps_manual(self):
        rows = [
            {"source": "XAUS 公开实时行情 + 本地规则计算", "symbol": "GC=F",
             "report_date": "2026-11-24"},
            {"source": "Yahoo Finance 公开行情 + 本地规则计算", "symbol": "GC=F",
             "report_date": "2026-11-25"},
            {"source": "manual", "symbol": "GC=F", "report_date": "2026-11-23"},
            {"source": "Yahoo Finance 公开行情 + 本地规则计算", "symbol": "DX-Y.NYB",
             "report_date": "2026-11-25"},
        ]
        kept = prefer_fresh_gold_technical(rows)
        self.assertEqual([r["source"] for r in kept], [
            "XAUS 公开实时行情 + 本地规则计算", "manual",
            "Yahoo Finance 公开行情 + 本地规则计算",
        ])

    def test_yahoo_kept_when_xaus_unavailable(self):
        rows = [{"source": "Yahoo Finance 公开行情 + 本地规则计算",
                 "symbol": "GC=F", "report_date": "2026-11-25"}]
        self.assertEqual(prefer_fresh_gold_technical(rows), rows)

    def test_report_shows_actual_technical_source(self):
        from alerts.message_templates import render_daily_report
        from analysis.macro_score import compute_score

        market = {"technical": [{
            "source": "XAUS 公开实时行情 + 本地规则计算",
            "symbol": "GC=F", "report_date": "2026-11-24",
            "bias": "偏空", "summary": "测试摘要",
            "payload": {"source": "XAUS 公开实时行情 + 本地规则计算",
                        "indicators": {}, "levels": {}},
        }]}
        text = render_daily_report(
            compute_score({}), {"text": "测试", "source": "rule"}, market
        )
        self.assertIn("来源：XAUS 公开实时行情 + 本地规则计算", text)
        self.assertNotIn("来源：Yahoo Finance", text)


class TechnicalDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "technical.db")
        self.db.init_schema()
        self.report = build_report("GC=F", rising_candles())

    def tearDown(self):
        self.tmp.cleanup()

    def _save(self, source: str):
        self.db.upsert_technical_report(
            source=source, symbol="GC=F", date=self.report["as_of"],
            report_type="daily", title="COMEX黄金期货 技术面日报",
            bias=self.report["bias"], summary=self.report["summary"],
            payload=self.report, raw_json='{"candles": []}',
        )

    def test_upsert_idempotent_and_market_context(self):
        for _ in range(3):
            self._save(self.report["source"])
        rows = self.db.technical_reports()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["symbol"], "GC=F")
        market, inputs = build_market(self.db)
        self.assertEqual(market["technical"][0]["symbol"], "GC=F")
        self.assertNotIn("technical", inputs)  # 技术面不参与宏观评分

    def test_manual_source_coexists_with_auto(self):
        self._save(self.report["source"])
        self._save("manual")
        rows = self.db.technical_reports()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["source"] for r in rows},
                         {self.report["source"], "manual"})


if __name__ == "__main__":
    unittest.main()
