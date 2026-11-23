import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from alerts.crt_chart import build_crt_chart, render_crt_png
from alerts.message_templates import (
    _polymarket_bias, render_crt_lines, render_daily_report, render_execution_lines,
)
from alerts.telegram_bot import COMMANDS
from analysis.macro_score import compute_score
from data.crt import (
    aggregate_4h, crt_snapshot, parse_tv_candles, parse_tv_scan,
    parse_yahoo_intraday, snapshot_from_data,
)
from data.polymarket import build_snapshot, extract_markets, parse_rate_markets, snapshot_from_data as poly_from_data
from database.models import Database


def ts(day, hour, minute=0):
    return int(datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc).timestamp())


def bar(start, open_, high, low, close):
    return {"ts": start, "time": datetime.fromtimestamp(
        start, tz=timezone.utc).isoformat(),
        "open": open_, "high": high, "low": low, "close": close}


def chart_payload(bars):
    return {"chart": {"result": [{
        "timestamp": [b["ts"] for b in bars],
        "indicators": {"quote": [{
            "open": [b["open"] for b in bars],
            "high": [b["high"] for b in bars],
            "low": [b["low"] for b in bars],
            "close": [b["close"] for b in bars],
        }]},
    }]}}


def power_of_three_bars():
    """P1 设区间 → P2 扫高后收回 → P3 向下扩张，4H 收线 UTC 02/06/10。"""
    return [
        bar(ts(20, 2), 95, 100, 94, 99),   # P1 区间 100 / 94
        bar(ts(20, 6), 99, 101, 95, 99),   # P2 扫高 101，收在区间内
        bar(ts(20, 10), 99, 100, 92, 93),  # P3 向下扩张
    ]


def bullish_bars():
    return [
        bar(ts(21, 2), 95, 100, 94, 96),   # P1 区间 100 / 94
        bar(ts(21, 6), 96, 99, 92, 98),    # P2 扫低 92，收在区间内
        bar(ts(21, 10), 98, 103, 96, 102), # P3 向上扩张
    ]


class CrtMathTests(unittest.TestCase):
    def test_parse_yahoo_intraday_keeps_hourly_timestamps(self):
        bars = [bar(ts(20, 0), 10, 12, 9, 11), bar(ts(20, 1), 11, 13, 10, 12)]
        rows = parse_yahoo_intraday(chart_payload(bars))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["ts"], ts(20, 1))
        self.assertEqual(rows[1]["close"], 12)

    def test_parse_yahoo_intraday_filters_bad_payload(self):
        self.assertEqual(parse_yahoo_intraday({}), [])
        self.assertEqual(parse_yahoo_intraday({"chart": {"result": []}}), [])

    def test_aggregate_4h_aligns_to_tradingview_close_times(self):
        bars = [bar(ts(20, h), 10 + h, 12 + h, 9 + h, 11 + h) for h in range(8)]
        candles = aggregate_4h(bars, drop_unclosed=False)
        self.assertEqual(len(candles), 3)
        self.assertEqual([c["time"][11:16] for c in candles], ["02:00", "06:00", "10:00"])
        self.assertEqual(candles[0]["open"], 10)
        self.assertEqual(candles[0]["high"], 13)
        self.assertEqual(candles[0]["low"], 9)
        self.assertEqual(candles[0]["bars"], 2)

    def test_aggregate_4h_drops_in_progress_bucket(self):
        from unittest.mock import patch
        bars = [bar(ts(20, h), 10 + h, 12 + h, 9 + h, 11 + h) for h in range(12)]
        with patch("data.crt.time.time", return_value=ts(20, 10, 30)):
            candles = aggregate_4h(bars)
        self.assertEqual([c["time"][11:16] for c in candles], ["02:00", "06:00", "10:00"])

    def test_crt_bearish_power_of_three(self):
        snap = crt_snapshot("XAUUSD", power_of_three_bars())
        self.assertEqual(snap["range_high"], 100.0)
        self.assertEqual(snap["range_low"], 94.0)
        self.assertEqual(snap["sweep_high"], 101.0)
        self.assertTrue(snap["swept_prev_high"])
        self.assertEqual(snap["sweep_side"], "高")
        self.assertEqual(snap["scenario"], "Power of Three · 偏空确认")
        self.assertEqual(snap["bias"], "偏空")
        self.assertEqual(snap["target"], 94.0)      # P1 远端
        self.assertEqual(snap["stop_ref"], 101.0)   # P2 影线外
        self.assertEqual(snap["dist_high_to_resistance"], 0.0)
        self.assertEqual(snap["dist_low_to_support"], -2.0)
        self.assertEqual(len(snap["candles"]), 3)

    def test_crt_bullish_power_of_three(self):
        snap = crt_snapshot("XAUUSD", bullish_bars())
        self.assertEqual(snap["sweep_side"], "低")
        self.assertEqual(snap["scenario"], "Power of Three · 偏多确认")
        self.assertEqual(snap["bias"], "偏多")
        self.assertEqual(snap["target"], 100.0)
        self.assertEqual(snap["stop_ref"], 92.0)
        self.assertEqual(snap["dist_sweep_low_to_support"], -2.0)

    def test_crt_pending_when_no_expansion(self):
        bars = [
            bar(ts(20, 2), 95, 100, 94, 99),
            bar(ts(20, 6), 99, 101, 95, 99),
            bar(ts(20, 10), 99, 100, 96, 99.5),
        ]
        snap = crt_snapshot("XAUUSD", bars)
        self.assertIn("待第3根扩张", snap["scenario"])
        self.assertEqual(snap["bias"], "中性")

    def test_crt_needs_three_closed_candles(self):
        with self.assertRaises(ValueError):
            crt_snapshot("XAUUSD", power_of_three_bars()[:2])


class TradingViewCrtTests(unittest.TestCase):
    def test_parse_tv_candles_returns_three_closed_bars(self):
        # 列顺序：当前、[1]、[2]、[3]；[1] 是最近一根已收盘
        values = [
            100, 101, 99, 100,             # 当前（未完成），不用
            99, 100, 92, 93,              # [1] = P3，10:00 收线
            99, 101, 95, 99,              # [2] = P2，06:00 收线
            95, 100, 94, 99,              # [3] = P1，02:00 收线
        ]
        candles = parse_tv_candles(
            {"data": [{"d": values}]}, now_ts=ts(20, 10, 1)
        )
        self.assertEqual(len(candles), 3)
        self.assertEqual([c["time"][11:16] for c in candles], ["02:00", "06:00", "10:00"])
        self.assertEqual(candles[0]["high"], 100.0)
        self.assertEqual(candles[-1]["low"], 92.0)
        snap = crt_snapshot("XAUUSD", candles)
        self.assertEqual(snap["bias"], "偏空")

    def test_parse_tv_candles_rejects_bad_payload(self):
        self.assertEqual(parse_tv_candles({}), [])
        self.assertEqual(parse_tv_candles({"data": [{"d": [1, 2, 3]}]}), [])
        self.assertEqual(parse_tv_candles({"data": [{"d": [None] * 16}]}), [])

    def test_parse_tv_scan_uses_tradingview_close_alignment(self):
        bar = parse_tv_scan(
            {"data": [{"d": [100.0, 105.0, 98.0, 103.5]}]},
            now_ts=ts(20, 10, 1),
        )
        self.assertIsNotNone(bar)
        self.assertEqual(bar["time"][11:16], "14:00")  # 当前根预计收线
        self.assertEqual(bar["open"], 100.0)
        self.assertEqual(bar["high"], 105.0)


class CrtImportTests(unittest.TestCase):
    def test_snapshot_from_chart_payload(self):
        # 需跨越 3 个 4H 桶：[2,6)/[6,10)/[10,14)，收线 06/10/14
        bars = [bar(ts(20, h), 95 + h, 100 + h, 90 + h, 96 + h)
                for h in range(2, 14)]
        snap = snapshot_from_data("XAUUSD", chart_payload(bars))
        self.assertEqual(len(snap["candles"]), 3)
        self.assertEqual(snap["as_of"][11:16], "14:00")

    def test_snapshot_from_three_candle_fields(self):
        snap = snapshot_from_data("XAUUSD", {
            "candle1": {"open": 95, "high": 100, "low": 94, "close": 99},
            "candle2": {"open": 99, "high": 101, "low": 95, "close": 99},
            "candle3": {"open": 99, "high": 100, "low": 92, "close": 93,
                        "time": "2026-11-20T10:00:00+00:00"},
        })
        self.assertEqual(snap["range_high"], 100.0)
        self.assertEqual(snap["target"], 94.0)

    def test_snapshot_from_legacy_two_candle_fields(self):
        snap = snapshot_from_data("XAUUSD", {
            "as_of": "2026-11-20T10:00:00+00:00",
            "current_high": 3620.5, "current_low": 3580.2,
            "prev_high": 3610.0, "prev_low": 3575.4,
            "current_open": 3585.0, "current_close": 3615.0,
        })
        self.assertTrue(snap.get("legacy_import"))
        self.assertEqual(snap["current_high"], 3620.5)
        self.assertEqual(len(snap["candles"]), 3)

    def test_snapshot_rejects_missing_fields(self):
        with self.assertRaises(ValueError):
            snapshot_from_data("XAUUSD", {"current_high": 10})


class CrtDatabaseTests(unittest.TestCase):
    def test_upsert_and_read_crt_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            db.init_schema()
            snap = crt_snapshot("XAUUSD", bullish_bars())
            db.upsert_crt_snapshot(
                source=snap["source"], symbol="XAUUSD", as_of=snap["as_of"],
                timeframe=snap["timeframe"], current_high=snap["current_high"],
                current_low=snap["current_low"], prev_high=snap["prev_high"],
                prev_low=snap["prev_low"], equilibrium=snap["equilibrium"],
                scenario=snap["scenario"], bias=snap["bias"],
                summary=snap["summary"], payload=snap,
            )
            rows = db.crt_snapshots()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["payload"]["bias"], "偏多")
            self.assertEqual(len(rows[0]["payload"]["candles"]), 3)

    def test_manual_and_auto_sources_coexist(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            db.init_schema()
            snap = crt_snapshot("XAUUSD", bullish_bars())
            for source in ("auto", "manual"):
                db.upsert_crt_snapshot(
                    source=source, symbol="XAUUSD", as_of=snap["as_of"],
                    timeframe="4h", current_high=snap["current_high"],
                    current_low=snap["current_low"], prev_high=snap["prev_high"],
                    prev_low=snap["prev_low"], equilibrium=snap["equilibrium"],
                    scenario=snap["scenario"], bias=snap["bias"],
                    summary=snap["summary"], payload=snap,
                )
            self.assertEqual(len(db.crt_snapshots()), 2)


class CrtReportTests(unittest.TestCase):
    def setUp(self):
        self.snap = crt_snapshot("XAUUSD", power_of_three_bars())

    def test_execution_lines_include_three_candle_levels(self):
        text = "\n".join(render_execution_lines({"crt": [self.snap]}))
        self.assertIn("4H CRT 三根结构", text)
        self.assertIn("P1 区间（阻力/支撑）", text)
        self.assertIn("P2 扫高并收回区间", text)
        self.assertIn("P3 高低点距关键位", text)
        self.assertIn("高点距阻力", text)
        self.assertIn("低点距支撑", text)
        self.assertIn("目标看 P1 远端", text)
        self.assertIn("4H/15M", text)
        self.assertIn("见随后消息", text)

    def test_execution_lines_do_not_use_distance_to_current_price(self):
        text = "\n".join(render_execution_lines({"crt": [self.snap]}))
        self.assertNotIn("距现价", text)

    def test_execution_lines_without_data_degrades(self):
        text = "\n".join(render_execution_lines({}))
        self.assertIn("暂无数据", text)
        self.assertIn("3 根已收盘 4H K 线", text)

    def test_execution_lines_include_fusion_conflict_note(self):
        text = "\n".join(render_execution_lines(
            {"crt": [self.snap]}, {"direction": "偏多", "score": 0.8}
        ))
        self.assertIn("融合方向", text)
        self.assertIn("冲突时降级观望", text)

    def test_execution_lines_reflect_actual_macro_bias(self):
        text = "\n".join(render_execution_lines({"crt": [self.snap]}, None, "中性震荡"))
        self.assertIn("宏观（中性震荡）≠ 入场信号", text)

    def test_daily_report_marks_macro_bias_and_drops_fomc(self):
        market = {
            "DFII10": {"value": 2.1, "date": "2026-11-23", "delta": 0.12,
                       "change_text": "5日 +12 bp", "stale": False},
            "fedwatch": {"text": "下次会议加息概率上升", "dovish_pp": -5.0},
            "polymarket": {
                "markets": [{"question": "Fed cuts rates at next meeting?",
                             "yes_probability": 68.0, "volume": 1000}],
                "disclaimer": "预测市场赔率，仅供参考。",
            },
            "fomc": {"documents": [{"url": "https://example.com", "doc_type": "Statement",
                                    "doc_date": "2026-11-23"}]},
            "crt": [self.snap],
        }
        report = render_daily_report(
            compute_score({}), {"text": "测试", "source": "rule"}, market
        )
        self.assertIn("关键市场数据（对 XAUUSD 偏向）", report)
        self.assertIn("XAUUSD 🔴偏空", report)
        self.assertIn("Polymarket", report)
        self.assertIn("XAUUSD 🟢偏多", report)
        self.assertNotIn("FOMC 官方文件", report)

    def test_crt_lines_and_command_registered(self):
        text = "\n".join(render_crt_lines([self.snap]))
        self.assertIn("4H CRT 三根结构", text)
        self.assertIn("P2 扫荡", text)
        self.assertIn("非交易信号", text)
        self.assertIn("/crt", COMMANDS)


class CrtChartTests(unittest.TestCase):
    def test_render_png_has_valid_signature(self):
        png = render_crt_png(bullish_bars(), range_high=100, range_low=94,
                             target=100, stop=92)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(png), 1000)

    def test_build_chart_from_market(self):
        snap = crt_snapshot("XAUUSD", bullish_bars())
        png = build_crt_chart({"crt": [snap]})
        self.assertIsNotNone(png)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_build_chart_skips_rows_without_candles(self):
        snap = crt_snapshot("XAUUSD", bullish_bars())
        png = build_crt_chart({"crt": [{"candles": []}, snap]})
        self.assertIsNotNone(png)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_build_chart_degrades_without_candles(self):
        self.assertIsNone(build_crt_chart({}))
        self.assertIsNone(build_crt_chart({"crt": [{"candles": []}]}))


class PolymarketTests(unittest.TestCase):
    def sample_markets(self):
        return [
            {"question": "Fed cuts rates by 25 bps in December?",
             "slug": "fed-cuts-25bp-december", "outcomes": '["Yes","No"]',
             "outcomePrices": '["0.68","0.32"]',
             "volume24hr": 1250000, "endDate": "2026-12-18T00:00:00Z"},
            {"question": "Will Bitcoin hit 100k?",
             "slug": "btc-100k", "outcomes": '["Yes","No"]',
             "outcomePrices": '["0.42","0.58"]', "volume24hr": 900000},
            {"question": "美联储下次会议加息?", "slug": "fed-hike",
             "outcomes": ["Yes", "No"], "outcomePrices": ["0.22", "0.78"],
             "volume24hr": 500000},
        ]

    def test_extract_markets_from_events(self):
        payload = {"events": [{"markets": [{"question": "Fed rate?"}]}]}
        self.assertEqual(extract_markets(payload), [{"question": "Fed rate?"}])

    def test_parse_rate_markets_filters_and_sorts(self):
        items = parse_rate_markets(self.sample_markets())
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["yes_probability"], 68.0)
        self.assertEqual(items[0]["volume"], 1250000.0)
        self.assertTrue(items[0]["url"].startswith("https://polymarket.com/"))
        self.assertTrue(all("Bitcoin" not in item["question"] for item in items))

    def test_parse_rate_markets_skips_closed_and_expired(self):
        markets = [
            {"question": "Fed cuts rates (closed)?", "outcomePrices": ["1", "0"],
             "closed": True, "volume24hr": 999999},
            {"question": "Fed cuts rates (expired)?", "outcomePrices": ["1", "0"],
             "endDate": "2020-01-01", "volume24hr": 888888},
            self.sample_markets()[0],
        ]
        items = parse_rate_markets(markets)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["yes_probability"], 68.0)

    def test_parse_rate_markets_uses_yes_position(self):
        markets = [{
            "question": "Fed hikes rates?", "outcomes": ["No", "Yes"],
            "outcomePrices": ["0.9", "0.1"], "volume24hr": 100,
        }]
        items = parse_rate_markets(markets)
        self.assertEqual(items[0]["yes_probability"], 10.0)
        self.assertEqual(items[0]["label"], "Yes")

    def test_polymarket_bias_does_not_treat_increase_as_easing(self):
        self.assertEqual(
            _polymarket_bias(
                "Will the Fed increase interest rates by 25 bps after the December 2026 meeting?",
                92.5,
            ),
            "偏空",
        )
        self.assertEqual(
            _polymarket_bias("Will the Fed decrease interest rates?", 68.0), "偏多"
        )
        self.assertEqual(_polymarket_bias("Fed rate decision", 20.0), "中性")

    def test_build_snapshot_and_manual_import(self):
        snap = build_snapshot(self.sample_markets())
        self.assertIn("Fed cuts", snap["text"])
        self.assertEqual(len(snap["markets"]), 2)
        self.assertIn("预测市场", snap["disclaimer"])
        manual = poly_from_data({"markets": [self.sample_markets()[0]]})
        self.assertEqual(manual["markets"][0]["yes_probability"], 68.0)
        with self.assertRaises(ValueError):
            poly_from_data({"unexpected": "payload"})

    def test_database_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            db.init_schema()
            snap = build_snapshot(self.sample_markets())
            db.upsert_polymarket_snapshot(
                source=snap["source"], as_of_date=snap["as_of"][:10],
                question=snap["text"],
                yes_probability=snap["markets"][0]["yes_probability"],
                volume=snap["markets"][0]["volume"],
                payload=snap, raw_json=json.dumps(self.sample_markets()),
            )
            rows = db.polymarket_snapshots()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["payload"]["markets"][0]["yes_probability"], 68.0)


if __name__ == "__main__":
    unittest.main()
