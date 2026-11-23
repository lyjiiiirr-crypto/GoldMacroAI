import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from alerts.message_templates import render_crt_lines, render_daily_report, render_execution_lines
from alerts.telegram_bot import COMMANDS
from analysis.macro_score import compute_score
from data.crt import (
    aggregate_4h, crt_snapshot, parse_yahoo_intraday, snapshot_from_data,
)
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


def four_hour_candles():
    """两根已收盘的 4H K 线（历史日期，不会被 drop_unclosed 过滤）。"""
    bars = [
        bar(ts(20, 0), 95, 100, 94, 99),
        bar(ts(20, 1), 99, 100, 93, 96),
        bar(ts(20, 2), 96, 98, 92, 93),
        bar(ts(20, 3), 93, 97, 90, 91),
        # 当前已收盘 4H：扫前低后强阳反转
        bar(ts(20, 4), 89, 96, 87, 94),
        bar(ts(20, 5), 94, 99, 90, 98),
        bar(ts(20, 6), 98, 101, 95, 100),
        bar(ts(20, 7), 100, 101, 96, 100),
    ]
    return aggregate_4h(bars)


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

    def test_aggregate_4h_groups_utc_buckets(self):
        bars = [bar(ts(20, h), 10 + h, 12 + h, 9 + h, 11 + h) for h in range(8)]
        candles = aggregate_4h(bars, drop_unclosed=False)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0]["open"], 10)
        self.assertEqual(candles[0]["high"], 15)
        self.assertEqual(candles[0]["low"], 9)
        self.assertEqual(candles[0]["close"], 14)
        self.assertEqual(candles[0]["bars"], 4)

    def test_aggregate_4h_drops_current_unclosed_bucket(self):
        bars = [bar(ts(20, h), 10, 11, 9, 10) for h in range(4)]
        self.assertEqual(len(aggregate_4h(bars, drop_unclosed=True)), 1)

    def test_crt_reversal_after_sweeping_prior_low(self):
        snap = crt_snapshot("GC=F", four_hour_candles())
        self.assertEqual(snap["current_high"], 101.0)
        self.assertEqual(snap["current_low"], 87.0)
        self.assertEqual(snap["prev_high"], 100.0)
        self.assertEqual(snap["prev_low"], 90.0)
        self.assertTrue(snap["swept_prev_low"])
        self.assertEqual(snap["scenario"], "反转")
        self.assertEqual(snap["bias"], "偏多")
        self.assertEqual(snap["target"], 100.0)
        self.assertEqual(snap["stop_ref"], 90.0)

    def test_crt_continuation_bearish_without_sweep(self):
        bars = [
            bar(ts(20, 0), 95, 100, 94, 99),
            bar(ts(20, 1), 99, 100, 90, 91),
            bar(ts(20, 4), 99, 99.8, 91, 92),
            bar(ts(20, 5), 92, 99.8, 91, 91),
        ]
        snap = crt_snapshot("GC=F", aggregate_4h(bars, drop_unclosed=False))
        self.assertEqual(snap["scenario"], "延续")
        self.assertEqual(snap["bias"], "偏空")
        self.assertFalse(snap["swept_prev_high"])
        self.assertFalse(snap["swept_prev_low"])

    def test_crt_needs_two_closed_candles(self):
        with self.assertRaises(ValueError):
            crt_snapshot("GC=F", [bar(ts(20, 0), 1, 2, 1, 1.5)])


class CrtImportTests(unittest.TestCase):
    def test_snapshot_from_chart_payload(self):
        bars = [
            bar(ts(20, h), 95, 100, 90, 96) for h in range(4)
        ] + [
            bar(ts(20, h), 96, 101, 87, 100) for h in range(4, 8)
        ]
        snap = snapshot_from_data("GC=F", chart_payload(bars))
        self.assertEqual(snap["current_high"], 101.0)
        self.assertEqual(snap["current_low"], 87.0)

    def test_snapshot_from_direct_high_low(self):
        snap = snapshot_from_data("GC=F", {
            "as_of": "2026-11-20T08:00:00+00:00",
            "current_high": 3620.5, "current_low": 3580.2,
            "prev_high": 3610.0, "prev_low": 3575.4,
            "current_open": 3585.0, "current_close": 3615.0,
        })
        self.assertEqual(snap["current_high"], 3620.5)
        self.assertEqual(snap["equilibrium"], 3600.35)
        self.assertEqual(snap["bias"], "偏多")

    def test_snapshot_from_direct_can_override_scenario(self):
        snap = snapshot_from_data("GC=F", {
            "current_high": 10, "current_low": 9,
            "prev_high": 10.5, "prev_low": 8.5,
            "scenario": "待确认", "bias": "中性",
        })
        self.assertEqual(snap["scenario"], "待确认")
        self.assertEqual(snap["bias"], "中性")

    def test_snapshot_rejects_missing_fields(self):
        with self.assertRaises(ValueError):
            snapshot_from_data("GC=F", {"current_high": 10})


class CrtDatabaseTests(unittest.TestCase):
    def test_upsert_and_read_crt_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            db.init_schema()
            snap = crt_snapshot("GC=F", four_hour_candles())
            db.upsert_crt_snapshot(
                source=snap["source"], symbol="GC=F", as_of=snap["as_of"],
                timeframe=snap["timeframe"], current_high=snap["current_high"],
                current_low=snap["current_low"], prev_high=snap["prev_high"],
                prev_low=snap["prev_low"], equilibrium=snap["equilibrium"],
                scenario=snap["scenario"], bias=snap["bias"],
                summary=snap["summary"], payload=snap,
            )
            rows = db.crt_snapshots()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["current_high"], 101.0)
            self.assertEqual(rows[0]["payload"]["bias"], "偏多")

    def test_manual_and_auto_sources_coexist(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            db.init_schema()
            snap = crt_snapshot("GC=F", four_hour_candles())
            for source in ("auto", "manual"):
                db.upsert_crt_snapshot(
                    source=source, symbol="GC=F", as_of=snap["as_of"],
                    timeframe="4h", current_high=snap["current_high"],
                    current_low=snap["current_low"], prev_high=snap["prev_high"],
                    prev_low=snap["prev_low"], equilibrium=snap["equilibrium"],
                    scenario=snap["scenario"], bias=snap["bias"],
                    summary=snap["summary"], payload=snap,
                )
            self.assertEqual(len(db.crt_snapshots()), 2)


class CrtReportTests(unittest.TestCase):
    def setUp(self):
        self.snap = crt_snapshot("GC=F", four_hour_candles())

    def test_execution_lines_include_4h_crt_levels(self):
        text = "\n".join(render_execution_lines({"crt": [self.snap]}))
        self.assertIn("4H CRT", text)
        self.assertIn("高 101.00", text)
        self.assertIn("低 87.00", text)
        self.assertIn("反转", text)
        self.assertIn("均衡位", text)
        self.assertIn("LTF", text)

    def test_execution_lines_without_data_degrades(self):
        text = "\n".join(render_execution_lines({}))
        self.assertIn("暂无数据", text)
        self.assertIn("4H 结构", text)

    def test_execution_lines_include_fusion_conflict_note(self):
        text = "\n".join(render_execution_lines(
            {"crt": [self.snap]}, {"direction": "偏空", "score": -0.8}
        ))
        self.assertIn("融合方向", text)
        self.assertIn("偏空", text)
        self.assertIn("冲突时降级观望", text)

    def test_execution_lines_reflect_actual_macro_bias(self):
        text = "\n".join(render_execution_lines({"crt": [self.snap]}, None, "中性震荡"))
        self.assertIn("宏观中性震荡 ≠ 入场信号", text)
        self.assertNotIn("宏观偏多 ≠ 入场信号", text)

    def test_daily_report_replaces_old_execution_reminder(self):
        report = render_daily_report(
            compute_score({}), {"text": "测试", "source": "rule"},
            {"crt": [self.snap]},
        )
        self.assertIn("交易执行提醒（4H CRT → LTF 执行）", report)
        self.assertIn("高 101.00 / 低 87.00", report)
        self.assertNotIn("等待日线/周线结构确认", report)

    def test_crt_lines_and_command_registered(self):
        text = "\n".join(render_crt_lines([self.snap]))
        self.assertIn("4H CRT 高低点", text)
        self.assertIn("非交易信号", text)
        self.assertIn("/crt", COMMANDS)


if __name__ == "__main__":
    unittest.main()
