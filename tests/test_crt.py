import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from alerts.crt_chart import build_crt_chart, render_crt_png
from alerts.message_templates import (
    _polymarket_bias, render_crt_lines, render_daily_report, render_execution_lines,
)
from alerts.telegram_bot import COMMANDS
from analysis.macro_score import compute_score
from data.crt import (
    MIXED_SOURCE, XAUS_SOURCE, _collect_one, crt_snapshot, drop_unclosed_bars,
    parse_tv_candles, parse_tv_scan, parse_yahoo_intraday, snapshot_from_data,
)
from data.polymarket import build_snapshot, extract_markets, parse_rate_markets, snapshot_from_data as poly_from_data
from database.models import Database


def ts(day, hour, minute=0):
    return int(datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc).timestamp())


def bar(start, open_, high, low, close):
    """原始小时 K 线：ts 为起始时刻，end_ts/time 为收线时刻。"""
    end = start + 3600
    return {
        "ts": start, "end_ts": end,
        "time": datetime.fromtimestamp(end, tz=timezone.utc).isoformat(),
        "open": open_, "high": high, "low": low, "close": close,
    }


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


def six_bars():
    """P5 高 100 / 低 95.5；P6 扫 P5 高点 103 后收回 P5 影线内（收 100）。"""
    return [
        bar(ts(20, 2), 95, 100, 94, 99),
        bar(ts(20, 3), 99, 101, 95, 99),
        bar(ts(20, 4), 99, 100, 96, 99),
        bar(ts(20, 5), 99, 99.5, 97, 99),
        bar(ts(20, 6), 99, 100, 95.5, 99),
        bar(ts(20, 7), 99, 103, 96, 100),
    ]


def with_sixth(low, high, close):
    rows = six_bars()[:-1]
    rows.append(bar(ts(20, 7), 99, high, low, close))
    return rows


def xaus_payload(hours=8):
    """当前小时之前 hours 根完整 1H，最后一根结束于当前整点。"""
    base = (int(time.time()) // 3600) * 3600
    points = [{
        "t": base - (hours - i) * 3600, "o": 100 + i,
        "h": 102 + i, "l": 99 + i, "c": 101 + i, "v": 10,
    } for i in range(hours)]
    return {
        "points": points,
        "data_state": {
            "status": "fresh",
            "as_of": datetime.now(timezone.utc).isoformat(),
            "source": "upstream", "age_seconds": 0,
        },
    }


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

    def test_drop_unclosed_bars_rejects_in_progress_hour(self):
        bars = [bar(ts(20, h), 10 + h, 12 + h, 9 + h, 11 + h) for h in range(1, 4)]
        with patch("data.crt.time.time", return_value=ts(20, 3, 30)):
            rows = drop_unclosed_bars(bars)
        self.assertEqual([b["end_ts"] for b in rows], [ts(20, 2), ts(20, 3)])

    def test_collect_prefers_xaus_realtime_source(self):
        with patch("data.crt.fetch_chart", return_value=xaus_payload()), \
                patch("data.crt.fetch_json", return_value={"data": []}):
            snap = _collect_one(None, "XAUUSD")
        self.assertEqual(snap["source"], XAUS_SOURCE)
        self.assertEqual(snap["timeframe"], "1h")
        self.assertEqual(len(snap["candles"]), 6)
        self.assertEqual(snap["current_close"], 108.0)
        self.assertNotIn("close_source", snap["candles"][-1])

    def test_collect_overrides_p6_with_closed_tv_offset(self):
        # 免费 TV scanner 常见响应：当前未完成根 + `[1]` 已收盘 P6
        tv_payload = {"data": [{"d": [
            3530.16, 3537.635, 3527.675, 3529.17,
            3523.39, 3540.5, 3512.58, 3530.27,
            None, None, None, None, None, None, None, None,
        ]}]}
        with patch("data.crt.fetch_chart", return_value=xaus_payload()), \
                patch("data.crt.fetch_json", return_value=tv_payload):
            snap = _collect_one(None, "XAUUSD")
        self.assertEqual(snap["source"], MIXED_SOURCE)
        self.assertEqual(snap["range_high"], 108.0)
        self.assertEqual(snap["range_low"], 101.0)
        self.assertEqual(snap["p6_low"], 3512.58)
        self.assertEqual(snap["p6_close"], 3530.27)
        self.assertEqual(snap["candles"][-1]["source"], MIXED_SOURCE)
        self.assertEqual(snap["candles"][-1]["close_source"], "TradingView 已收盘 [1]")

    def test_collect_rejects_stale_xaus_and_falls_back(self):
        from data.common import FetchError
        yahoo = chart_payload([bar(ts(20, h), 95, 100, 94, 99) for h in range(12)])
        with patch("data.crt.fetch_chart", side_effect=FetchError("stale")), \
                patch("data.crt.fetch_json", return_value={"data": []}), \
                patch("data.crt.fetch_yahoo_texts", return_value=[json.dumps(yahoo)]):
            snap = _collect_one(None, "XAUUSD")
        self.assertEqual(snap["timeframe"], "1h")
        self.assertEqual(len(snap["candles"]), 6)

    def test_crt_sweep_signal_high_then_reclaim_is_bearish(self):
        snap = crt_snapshot("XAUUSD", six_bars())
        self.assertEqual(snap["rule_version"], 2)
        # P1~P5 最高/最低只作背景区间
        self.assertEqual(snap["range_high"], 101.0)
        self.assertEqual(snap["range_low"], 94.0)
        self.assertEqual(snap["signal_label"], "P5")
        self.assertEqual(snap["signal_index"], 5)
        self.assertEqual(snap["signal_high"], 100.0)
        self.assertEqual(snap["signal_low"], 95.5)
        self.assertTrue(snap["swept_high"])
        self.assertFalse(snap["swept_low"])
        self.assertEqual(snap["swept_side"], "高")
        self.assertTrue(snap["reclaimed_wick"])
        self.assertTrue(snap["crt_valid"])
        self.assertEqual(snap["scenario"],
                         "P6 扫 P5 高点后收回该根影线内 · CRT 成立 · 偏空反转确认")
        self.assertEqual(snap["bias"], "偏空")
        self.assertEqual(snap["target"], 95.5)
        self.assertEqual(snap["stop_ref"], 103.0)
        self.assertEqual(snap["dist_p6_high_to_signal_high"], 3.0)
        self.assertEqual(snap["dist_p6_low_to_signal_low"], 0.5)
        # 旧字段别名仍指向主信号根
        self.assertTrue(snap["p6_swept_range_high"])
        self.assertTrue(snap["p6_reclaimed_range"])
        self.assertEqual(snap["dist_p6_high_to_range_high"], 3.0)
        self.assertEqual(len(snap["candles"]), 6)
        self.assertEqual(snap["timeframe"], "1h")

    def test_crt_sweep_signal_low_then_reclaim_is_bullish(self):
        snap = crt_snapshot("XAUUSD", with_sixth(92, 100, 97))
        self.assertEqual(snap["signal_label"], "P5")
        self.assertTrue(snap["swept_low"])
        self.assertTrue(snap["reclaimed_wick"])
        self.assertTrue(snap["crt_valid"])
        self.assertEqual(snap["scenario"],
                         "P6 扫 P5 低点后收回该根影线内 · CRT 成立 · 偏多反转确认")
        self.assertEqual(snap["bias"], "偏多")
        self.assertEqual(snap["target"], 100.0)
        self.assertEqual(snap["stop_ref"], 92.0)

    def test_crt_sweep_high_without_reclaim_stays_invalid(self):
        snap = crt_snapshot("XAUUSD", with_sixth(98, 104, 102))
        self.assertEqual(snap["signal_label"], "P5")
        self.assertTrue(snap["swept_high"])
        self.assertFalse(snap["reclaimed_wick"])
        self.assertFalse(snap["crt_valid"])
        self.assertEqual(snap["bias"], "偏多")
        self.assertIsNone(snap["target"])
        self.assertEqual(snap["stop_ref"], 100.0)

    def test_crt_sweep_low_without_reclaim_stays_invalid(self):
        snap = crt_snapshot("XAUUSD", with_sixth(90, 100, 93))
        self.assertEqual(snap["signal_label"], "P5")
        self.assertTrue(snap["swept_low"])
        self.assertFalse(snap["crt_valid"])
        self.assertEqual(snap["bias"], "偏空")
        self.assertEqual(snap["stop_ref"], 95.5)

    def test_crt_both_sides_swept_is_neutral(self):
        snap = crt_snapshot("XAUUSD", with_sixth(92, 103, 97))
        self.assertEqual(snap["signal_label"], "P5")
        self.assertTrue(snap["swept_high"])
        self.assertTrue(snap["swept_low"])
        self.assertEqual(snap["swept_side"], "双")
        self.assertTrue(snap["crt_valid"])
        self.assertEqual(snap["bias"], "中性")
        self.assertIsNone(snap["target"])
        self.assertIn("同时扫 P5 高低点", snap["summary"])

    def test_crt_pending_when_no_candle_swept(self):
        # P6 高 99.5 / 低 97：不越过任何一根 P1~P5 的高/低点
        snap = crt_snapshot("XAUUSD", with_sixth(97, 99.5, 98))
        self.assertIsNone(snap["signal_label"])
        self.assertFalse(snap["swept_high"])
        self.assertFalse(snap["swept_low"])
        self.assertFalse(snap["crt_valid"])
        self.assertIn("未扫 P1-P5 任一 K 线高低点", snap["scenario"])
        self.assertEqual(snap["bias"], "中性")
        self.assertIsNone(snap["target"])
        self.assertIsNone(snap["dist_p6_high_to_signal_high"])

    def test_crt_prefers_closest_qualifying_candle_p5_over_p4(self):
        # P5 与 P4 均被扫且收回；P5 离 P6 最近，应作为主信号
        snap = crt_snapshot("XAUUSD", with_sixth(96, 103, 99))
        self.assertEqual(snap["signal_label"], "P5")
        self.assertIn("P6 扫 P5 高点", snap["scenario"])
        self.assertNotIn("P4", snap["scenario"])

    def test_crt_falls_back_to_p4_when_p5_not_swept(self):
        # P5 高 100/低 95.5 未被扫；P4 高 99.5 被扫且收 98 收回影线内
        snap = crt_snapshot("XAUUSD", with_sixth(97, 99.8, 98))
        self.assertEqual(snap["signal_label"], "P4")
        self.assertEqual(snap["signal_index"], 4)
        self.assertEqual(snap["signal_high"], 99.5)
        self.assertEqual(snap["signal_low"], 97.0)
        self.assertTrue(snap["crt_valid"])
        self.assertEqual(snap["bias"], "偏空")
        self.assertEqual(snap["target"], 97.0)
        self.assertEqual(snap["stop_ref"], 99.8)

    def test_crt_needs_six_closed_candles(self):
        with self.assertRaises(ValueError):
            crt_snapshot("XAUUSD", six_bars()[:5])


class TradingViewCrtTests(unittest.TestCase):
    def test_parse_tv_candles_returns_closed_bars(self):
        values = [
            100, 101, 99, 100,             # 当前（未完成），不用
            99, 100, 92, 93,              # [1] = P6，10:00 收线
            99, 101, 95, 99,              # [2] = 09:00
            95, 100, 94, 99,              # [3] = 08:00
        ]
        candles = parse_tv_candles(
            {"data": [{"d": values}]}, now_ts=ts(20, 10, 1)
        )
        self.assertEqual(len(candles), 3)
        self.assertEqual([c["time"][11:16] for c in candles],
                         ["08:00", "09:00", "10:00"])
        self.assertEqual(candles[0]["high"], 100.0)
        self.assertEqual(candles[-1]["low"], 92.0)

    def test_parse_tv_candles_accepts_partial_closed_offset(self):
        values = [
            100, 101, 99, 100,
            3523.39, 3540.5, 3512.58, 3530.27,  # [1] = 最近已收盘 P6
        ] + [None] * 8
        candles = parse_tv_candles(
            {"data": [{"d": values}]}, now_ts=ts(20, 10, 1)
        )
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0]["time"][11:16], "10:00")
        self.assertEqual(candles[0]["low"], 3512.58)
        self.assertEqual(candles[0]["close"], 3530.27)

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
        self.assertEqual(bar["time"][11:16], "11:00")  # 当前根预计收线
        self.assertEqual(bar["open"], 100.0)
        self.assertEqual(bar["high"], 105.0)


class CrtImportTests(unittest.TestCase):
    def test_snapshot_from_chart_payload(self):
        bars = [bar(ts(20, h), 95 + h, 100 + h, 90 + h, 96 + h)
                for h in range(2, 10)]
        snap = snapshot_from_data("XAUUSD", chart_payload(bars))
        self.assertEqual(len(snap["candles"]), 6)
        self.assertEqual(snap["as_of"][11:16], "10:00")

    def test_snapshot_from_six_candle_fields(self):
        fields = {}
        for i, item in enumerate(six_bars(), start=1):
            fields[f"candle{i}"] = {
                "open": item["open"], "high": item["high"],
                "low": item["low"], "close": item["close"],
                "time": item["time"],
            }
        snap = snapshot_from_data("XAUUSD", fields)
        self.assertEqual(snap["range_high"], 101.0)
        self.assertEqual(snap["p6_high"], 103.0)
        self.assertTrue(snap["crt_valid"])

    def test_snapshot_from_legacy_three_candle_fields(self):
        snap = snapshot_from_data("XAUUSD", {
            "candle1": {"open": 95, "high": 100, "low": 94, "close": 99},
            "candle2": {"open": 99, "high": 101, "low": 95, "close": 99},
            "candle3": {"open": 99, "high": 103, "low": 92, "close": 97,
                        "time": "2026-11-20T07:00:00+00:00"},
            "scenario": "旧版结论",
        })
        self.assertTrue(snap.get("legacy_import"))
        self.assertEqual(len(snap["candles"]), 6)
        self.assertEqual(snap["scenario"], "旧版结论")

    def test_snapshot_from_legacy_range_fields(self):
        snap = snapshot_from_data("XAUUSD", {
            "as_of": "2026-11-20T07:00:00+00:00",
            "range_high": 3629.6, "range_low": 3567.8,
            "sweep_high": 3636.4, "sweep_low": 3560.2,
            "current_high": 3640.1, "current_low": 3590.0,
            "current_close": 3630.0,
        })
        self.assertTrue(snap.get("legacy_import"))
        self.assertEqual(len(snap["candles"]), 6)
        self.assertEqual(snap["current_high"], 3640.1)

    def test_snapshot_from_legacy_two_candle_fields(self):
        snap = snapshot_from_data("XAUUSD", {
            "as_of": "2026-11-20T07:00:00+00:00",
            "current_high": 3620.5, "current_low": 3580.2,
            "prev_high": 3610.0, "prev_low": 3575.4,
            "current_open": 3585.0, "current_close": 3615.0,
        })
        self.assertTrue(snap.get("legacy_import"))
        self.assertEqual(snap["current_high"], 3620.5)
        self.assertEqual(len(snap["candles"]), 6)

    def test_snapshot_rejects_missing_fields(self):
        with self.assertRaises(ValueError):
            snapshot_from_data("XAUUSD", {"current_high": 10})


class CrtDatabaseTests(unittest.TestCase):
    def test_upsert_and_read_crt_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            db.init_schema()
            snap = crt_snapshot("XAUUSD", six_bars())
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
            self.assertEqual(rows[0]["payload"]["bias"], "偏空")
            self.assertEqual(len(rows[0]["payload"]["candles"]), 6)

    def test_manual_and_auto_sources_coexist(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            db.init_schema()
            snap = crt_snapshot("XAUUSD", six_bars())
            for source in ("auto", "manual"):
                db.upsert_crt_snapshot(
                    source=source, symbol="XAUUSD", as_of=snap["as_of"],
                    timeframe="1h", current_high=snap["current_high"],
                    current_low=snap["current_low"], prev_high=snap["prev_high"],
                    prev_low=snap["prev_low"], equilibrium=snap["equilibrium"],
                    scenario=snap["scenario"], bias=snap["bias"],
                    summary=snap["summary"], payload=snap,
                )
            self.assertEqual(len(db.crt_snapshots()), 2)


class CrtReportTests(unittest.TestCase):
    def setUp(self):
        self.snap = crt_snapshot("XAUUSD", six_bars())

    def test_execution_lines_focus_on_p6_vs_signal_candle(self):
        text = "\n".join(render_execution_lines({"crt": [self.snap]}))
        self.assertIn("1H CRT 六根结构", text)
        self.assertIn("P1-P5 背景区间", text)
        self.assertIn("P6 高低点距信号根边界", text)
        self.assertIn("高点距该根高点", text)
        self.assertIn("低点距该根低点", text)
        self.assertIn("信号根 P5（最靠近 P6，逐根判定）", text)
        self.assertIn("P6 收盘", text)
        self.assertIn("CRT 成立", text)
        self.assertIn("失效参考", text)
        self.assertIn("1H/15M", text)
        self.assertIn("见随后消息", text)
        self.assertNotIn("P2 区间（当前参考）", text)
        self.assertNotIn("目标看 P1 远端", text)
        self.assertNotIn("CRT 均衡位", text)

    def test_execution_lines_report_reclaim_and_target(self):
        text = "\n".join(render_execution_lines({"crt": [self.snap]}))
        self.assertIn("收盘收回 P5 影线内（95.50~100.00）", text)
        self.assertIn("偏空反转确认", text)
        self.assertIn("目标 95.50", text)

    def test_execution_lines_do_not_use_distance_to_current_price(self):
        text = "\n".join(render_execution_lines({"crt": [self.snap]}))
        self.assertNotIn("距现价", text)
        self.assertNotIn("距阻力", text)
        self.assertNotIn("距支撑", text)

    def test_execution_lines_without_data_degrades(self):
        text = "\n".join(render_execution_lines({}))
        self.assertIn("暂无数据", text)
        self.assertIn("6 根已收盘 1H K 线", text)

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
        self.assertIn("1H CRT 六根结构", text)
        self.assertIn("P1-P5 背景区间", text)
        self.assertIn("信号根 P5（最靠近 P6）", text)
        self.assertIn("距信号根高点 +3.00 点", text)
        self.assertIn("P6 收盘", text)
        self.assertIn("收盘收回 P5 影线内（95.50~100.00）", text)
        self.assertIn("扫过 P5 高点", text)
        self.assertIn("非交易信号", text)
        self.assertIn("/crt", COMMANDS)

    def test_crt_lines_recompute_legacy_snapshot_from_candles(self):
        # 旧规则快照缺 rule_version，且情景/方向字段与新规则冲突，应按 6 根重算
        legacy = dict(self.snap)
        legacy.pop("rule_version", None)
        legacy["scenario"] = "旧版结论"
        legacy["bias"] = "旧版方向"
        legacy["signal_label"] = None
        legacy["swept_high"] = None
        text = "\n".join(render_crt_lines([legacy]))
        self.assertIn("信号根 P5（最靠近 P6）", text)
        self.assertIn("偏空反转确认", text)
        self.assertNotIn("旧版结论", text)
        self.assertNotIn("旧版方向", text)


class CrtChartTests(unittest.TestCase):
    def test_render_png_has_valid_signature(self):
        png = render_crt_png(
            six_bars(), range_high=101, range_low=94,
            target=95.5, stop=103, crt_valid=True,
            signal_high=100, signal_low=95.5, signal_index=5,
        )
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(png), 1000)

    def test_build_chart_from_market(self):
        snap = crt_snapshot("XAUUSD", six_bars())
        png = build_crt_chart({"crt": [snap]})
        self.assertIsNotNone(png)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_build_chart_skips_rows_without_candles(self):
        snap = crt_snapshot("XAUUSD", six_bars())
        png = build_crt_chart({"crt": [{"candles": []}, snap]})
        self.assertIsNotNone(png)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_build_chart_degrades_without_candles(self):
        self.assertIsNone(build_crt_chart({}))
        self.assertIsNone(build_crt_chart({"crt": [{"candles": []}]}))


class CrtPushGateTests(unittest.TestCase):
    def test_run_once_send_silent_when_crt_not_valid(self):
        from scheduler import jobs

        market = {
            "crt": [{
                "crt_valid": False, "signal_label": "P5", "scenario": "未成立",
                "payload": {"crt_valid": False, "candles": six_bars()},
            }],
            "technical": [], "gex": [], "calendar": [],
        }
        inputs = {"real_rate": {"delta_bp": -1, "stale": False}}
        score = type("S", (), {"total": 0.0, "bias": "中性震荡", "confidence": 0.5,
                               "signals": [], "warnings": [], "sufficient": True,
                               "as_dict": lambda self=None: {}})()
        sent = []

        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            db.init_schema()
            with patch.object(jobs, "collect_all", return_value={}), \
                    patch.object(jobs, "build_market", return_value=(market, inputs)), \
                    patch.object(jobs, "compute_score", return_value=score), \
                    patch.object(jobs, "generate_analysis", return_value={"text": "t", "source": "rule"}), \
                    patch.object(jobs, "render_daily_report", return_value="report"), \
                    patch.object(jobs, "notify", side_effect=lambda *a, **k: sent.append("notify") or (True, "telegram")), \
                    patch.object(jobs, "notify_photo", side_effect=lambda *a, **k: sent.append("photo") or (True, "telegram")):
                result = jobs.run_once(db=db, send=True, collect_data=False)
        self.assertFalse(result["crt_valid"])
        self.assertEqual(result["sent"], [])
        self.assertEqual(sent, [])

    def test_run_once_send_pushes_when_crt_valid(self):
        from scheduler import jobs

        market = {
            "crt": [{
                "crt_valid": True, "signal_label": "P5", "scenario": "成立",
                "payload": {"crt_valid": True, "candles": six_bars()},
            }],
            "technical": [], "gex": [], "calendar": [],
        }
        inputs = {"real_rate": {"delta_bp": -1, "stale": False}}
        score = type("S", (), {"total": 0.0, "bias": "中性震荡", "confidence": 0.5,
                               "signals": [], "warnings": [], "sufficient": True,
                               "as_dict": lambda self=None: {"total": 0.0}})()
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            db.init_schema()
            with patch.object(jobs, "collect_all", return_value={}), \
                    patch.object(jobs, "build_market", return_value=(market, inputs)), \
                    patch.object(jobs, "compute_score", return_value=score), \
                    patch.object(jobs, "generate_analysis", return_value={"text": "t", "source": "rule"}), \
                    patch.object(jobs, "render_daily_report", return_value="report"), \
                    patch.object(jobs, "notify", return_value=(True, "telegram")), \
                    patch.object(jobs, "evaluate", return_value=[]), \
                    patch.object(jobs, "send_alerts", return_value=[]), \
                    patch.object(jobs, "build_crt_chart", return_value=b"\x89PNG\r\n\x1a\n"), \
                    patch.object(jobs, "notify_photo", return_value=(True, "telegram")):
                result = jobs.run_once(db=db, send=True, collect_data=False)
        self.assertTrue(result["crt_valid"])
        self.assertIn("daily_report(telegram)", result["sent"])
        self.assertIn("crt_chart(telegram)", result["sent"])


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
        items = parse_rate_markets([{
            "question": "Fed hikes rates?", "outcomes": ["No", "Yes"],
            "outcomePrices": ["0.9", "0.1"], "volume24hr": 100,
        }])
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
