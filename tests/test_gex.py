import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alerts.message_templates import render_daily_report, render_gex_lines
from analysis.macro_score import compute_score
from data.gex import (
    bs_gamma, build_snapshot, gamma_flip, gex_map, hedging_pressure,
    net_gex_at_spot, parse_cboe_chain, parse_option_chain,
)
from database.models import Database
from scheduler.jobs import build_market


def chain_payload(spot=100.0, calls=(), puts=(), exp_days=90):
    """构造 Yahoo 期权链响应；calls/puts 为 (strike, oi, iv) 列表。"""
    exp = int((datetime.now(timezone.utc) + timedelta(days=exp_days)).timestamp())

    def rows(items):
        return [
            {"strike": s, "openInterest": oi, "impliedVolatility": iv}
            for s, oi, iv in items
        ]

    return {"optionChain": {"result": [{
        "quote": {"regularMarketPrice": spot},
        "options": [{"expirationDate": exp, "calls": rows(calls), "puts": rows(puts)}],
        "expirationDates": [exp],
    }]}}


def series_from(calls=(), puts=(), spot=100.0, exp_days=90):
    payload = chain_payload(spot=spot, calls=calls, puts=puts, exp_days=exp_days)
    _, series = parse_option_chain(payload)
    return series


class GexMathTests(unittest.TestCase):
    def test_bs_gamma_positive_and_atm_larger_than_wing(self):
        atm = bs_gamma(100, 100, 0.25, 0.2)
        wing = bs_gamma(100, 140, 0.25, 0.2)
        self.assertGreater(atm, 0)
        self.assertGreater(atm, wing)
        self.assertAlmostEqual(bs_gamma(100, 100, 0.25, 0.2, 0.01),
                               bs_gamma(100, 100, 0.25, 0.2, 0.01))

    def test_bs_gamma_invalid_inputs_return_zero(self):
        for args in ((0, 100, 0.25, 0.2), (100, 0, 0.25, 0.2),
                     (100, 100, 0, 0.2), (100, 100, 0.25, 0)):
            self.assertEqual(bs_gamma(*args), 0.0)

    def test_parse_option_chain_filters_noise(self):
        payload = chain_payload(
            spot=100.0,
            calls=[(100, 500, 0.2), (110, 0, 0.2), (120, 100, 1e-5), (130, 100, 6.0)],
            puts=[(90, 300, 0.25)],
        )
        spot, series = parse_option_chain(payload)
        self.assertEqual(spot, 100.0)
        self.assertEqual(len(series), 2)
        self.assertEqual({r["sign"] for r in series}, {1, -1})
        self.assertTrue(all(0.005 <= r["iv"] <= 5.0 for r in series))
        self.assertTrue(all(r["oi"] > 0 for r in series))

    def test_parse_option_chain_expiry_and_malformed(self):
        long_dated = chain_payload(calls=[(100, 10, 0.2)], exp_days=400)
        _, series = parse_option_chain(long_dated, expiry_limit_days=180)
        self.assertEqual(series, [])
        _, kept = parse_option_chain(chain_payload(calls=[(100, 10, 0.2)]),
                                     expiry_limit_days=180)
        self.assertEqual(len(kept), 1)
        self.assertEqual(parse_option_chain({"optionChain": {"result": []}}), (0.0, []))
        self.assertEqual(parse_option_chain({}), (0.0, []))

    def test_gamma_flip_finds_crossing_between_call_and_put_walls(self):
        # 高行权价 call 与低行权价 put 令净 GEX 随现货价由负转正，flip 位于两者之间
        series = series_from(calls=[(110, 1000, 0.2)], puts=[(90, 1000, 0.2)])
        self.assertLess(net_gex_at_spot(series, 80, 0.0), 0)
        self.assertGreater(net_gex_at_spot(series, 120, 0.0), 0)
        flip = gamma_flip(series, 100, 0.0)
        self.assertIsNotNone(flip)
        self.assertTrue(90 < flip < 110)
        self.assertLess(abs(flip - 100), 10)

    def test_gamma_flip_none_when_no_crossing(self):
        calls_only = series_from(calls=[(100, 500, 0.2), (110, 500, 0.2)])
        self.assertIsNone(gamma_flip(calls_only, 100, 0.0))
        self.assertIsNone(gamma_flip([], 100, 0.0))

    def test_gex_map_aggregates_by_strike_and_side(self):
        series = series_from(
            calls=[(100, 200, 0.2), (100, 300, 0.2), (110, 100, 0.2)],
            puts=[(90, 400, 0.2)],
        )
        mapping = gex_map(series, 100, 0.0)
        self.assertEqual([r["strike"] for r in mapping], [90, 100, 110])
        row_100 = next(r for r in mapping if r["strike"] == 100)
        self.assertGreater(row_100["call_gex"], 0)
        self.assertEqual(row_100["put_gex"], 0)
        self.assertEqual(row_100["net_gex"], row_100["call_gex"])
        self.assertLess(mapping[0]["net_gex"], 0)  # put 侧为负

    def test_hedging_pressure_directions(self):
        positive = hedging_pressure(1e8, 105, 100)
        self.assertEqual(positive["regime"], "正Gamma")
        self.assertIn("压涨", positive["pressure"])
        self.assertIn("托底", positive["pressure"])
        self.assertEqual(positive["distance_pct"], 5.0)
        self.assertIn("flip", positive["summary"])

        negative = hedging_pressure(-1e8, 95, 100)
        self.assertEqual(negative["regime"], "负Gamma")
        self.assertIn("助推", negative["pressure"])
        self.assertIn("杀跌", negative["pressure"])
        self.assertEqual(negative["distance_pct"], -5.0)

        neutral = hedging_pressure(0.0, 100, None)
        self.assertEqual(neutral["regime"], "中性Gamma")
        self.assertIsNone(neutral["distance_pct"])

    def test_build_snapshot_shape(self):
        series = series_from(calls=[(110, 1000, 0.2)], puts=[(90, 1000, 0.2)])
        snap = build_snapshot("TEST", series, 100, 0.0, as_of="2026-11-23")
        for key in ("symbol", "as_of", "spot", "net_gex", "gamma_flip",
                    "call_wall", "put_wall", "regime", "summary",
                    "gex_map", "disclaimer"):
            self.assertIn(key, snap)
        self.assertEqual(snap["as_of"], "2026-11-23")
        self.assertIn("非交易信号", snap["disclaimer"])
        with self.assertRaises(ValueError):
            build_snapshot("TEST", [], 100, 0.0)


def cboe_payload(spot=100.0, options=None, now=None):
    """构造 Cboe 延迟期权报价响应；options 为 (symbol, iv, oi, gamma) 列表。"""
    return {"symbol": "TEST", "data": {
        "current_price": spot, "close": spot,
        "options": [
            {"option": sym, "iv": iv, "open_interest": oi, "gamma": gamma}
            for sym, iv, oi, gamma in (options or [])
        ],
    }}


class CboeParserTests(unittest.TestCase):
    def test_parse_cboe_chain_decodes_symbol_and_fields(self):
        payload = cboe_payload(spot=100.0, options=[
            ("TEST261204C00100000", 0.2, 500, 0.02),   # strike 100, call
            ("TEST261204P00090000", 0.25, 300, 0.018),  # strike 90, put
            ("TEST261204C00000000", 0.2, 100, 0.01),   # strike 0 -> invalid
            ("MALFORMED", 0.2, 100, 0.01),              # bad symbol
            ("TEST261204C00110000", 0.2, 0, 0.01),     # zero OI
        ])
        spot, series = parse_cboe_chain(payload, expiry_limit_days=90)
        self.assertEqual(spot, 100.0)
        self.assertEqual(len(series), 2)
        self.assertEqual({r["strike"] for r in series}, {100.0, 90.0})
        call = next(r for r in series if r["sign"] > 0)
        put = next(r for r in series if r["sign"] < 0)
        self.assertEqual(call["oi"], 500)
        self.assertEqual(call["gamma"], 0.02)
        self.assertAlmostEqual(put["iv"], 0.25)

    def test_parse_cboe_chain_filters_expiry_and_malformed(self):
        payload = cboe_payload(options=[
            ("TEST280616C00100000", 0.2, 100, 0.01),  # 远超 90 天
        ])
        _, series = parse_cboe_chain(payload, expiry_limit_days=90)
        self.assertEqual(series, [])
        self.assertEqual(parse_cboe_chain({}), (0.0, []))

    def test_gex_from_cboe_gamma_fallback_when_iv_missing(self):
        payload = cboe_payload(spot=100.0, options=[
            ("TEST261204C00100000", 0.0, 500, 0.02),
        ])
        _, series = parse_cboe_chain(payload)
        snap = build_snapshot("TEST", series, 100, 0.0)
        # IV 为 0 时回退到 Cboe gamma，净 GEX 仍为非零
        self.assertNotEqual(snap["net_gex"], 0)


class GexDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "gex.db")
        self.db.init_schema()
        self.series = series_from(calls=[(110, 1000, 0.2)], puts=[(90, 1000, 0.2)])
        self.snap = build_snapshot("GLD", self.series, 100, 0.0, as_of="2026-11-23")

    def tearDown(self):
        self.tmp.cleanup()

    def _save(self, source: str):
        self.db.upsert_gex_snapshot(
            source=source, symbol=self.snap["symbol"], date=self.snap["as_of"],
            spot=self.snap["spot"], net_gex=self.snap["net_gex"],
            gamma_flip=self.snap["gamma_flip"], call_wall=self.snap["call_wall"],
            put_wall=self.snap["put_wall"], regime=self.snap["regime"],
            summary=self.snap["summary"], payload=self.snap,
            raw_json='{"series": []}',
        )

    def test_upsert_idempotent_and_market_context(self):
        for _ in range(3):
            self._save(self.snap["source"])
        rows = self.db.gex_snapshots()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["symbol"], "GLD")
        self.assertIsInstance(rows[0]["payload"], dict)

        market, inputs = build_market(self.db)
        self.assertEqual(market["gex"][0]["symbol"], "GLD")
        self.assertNotIn("gex", inputs)  # GEX 不参与宏观评分

    def test_manual_source_coexists_with_auto(self):
        self._save(self.snap["source"])
        self._save("manual")
        rows = self.db.gex_snapshots()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["source"] for r in rows}, {self.snap["source"], "manual"})

    def test_symbol_filter(self):
        self._save(self.snap["source"])
        self.assertEqual(len(self.db.gex_snapshots(symbol="GLD")), 1)
        self.assertEqual(self.db.gex_snapshots(symbol="SPY"), [])


class GexReportTests(unittest.TestCase):
    def setUp(self):
        self.snap = {
            "source": "manual", "symbol": "GLD", "as_of_date": "2026-11-23",
            "spot": 322.87, "net_gex": 1.85e8, "gamma_flip": 318.4,
            "distance_pct": 1.4,
            "call_wall": 330.0, "put_wall": 315.0, "regime": "正Gamma",
            "summary": "净GEX +185M/1%，flip 318.40，现价高于 flip 1.40%；上涨中卖出对冲。",
            "payload": {
                "label": "GLD 黄金ETF代理", "as_of": "2026-11-23",
                "hedging_pressure": "上涨中卖出对冲（压涨）",
                "disclaimer": "GEX 为延迟近似估算，非交易信号。",
            },
        }

    def test_render_gex_lines_includes_key_fields(self):
        lines = render_gex_lines([self.snap])
        text = "\n".join(lines)
        self.assertIn("GLD 黄金ETF代理", text)
        self.assertIn("正Gamma", text)
        self.assertIn("318.40", text)
        self.assertIn("非交易信号", text)

    def test_render_gex_lines_adds_bias_label_and_reason(self):
        text = "\n".join(render_gex_lines([self.snap]))
        self.assertIn("🟢偏多", text)
        self.assertIn("现价高于 flip 1.40%", text)
        self.assertIn("未回测", text)
        self.assertIn("仅作背景", text)

    def test_render_gex_lines_missing_distance_is_neutral(self):
        row = dict(self.snap)
        row.pop("distance_pct")
        text = "\n".join(render_gex_lines([row]))
        self.assertIn("⚪中性", text)
        self.assertIn("flip 距离未知，未判定偏向", text)

    def test_render_gex_lines_does_not_mutate_row_or_macro_score(self):
        from analysis.macro_score import compute_score

        row = dict(self.snap)
        before = dict(row)
        score = compute_score({})
        total = score.total
        render_gex_lines([row])
        render_daily_report(score, {"text": "测试", "source": "rule"}, {"gex": [row]})
        self.assertEqual(row, before)
        self.assertEqual(score.total, total)

    def test_render_gex_lines_empty_state(self):
        self.assertIn("暂无 GEX", "\n".join(render_gex_lines([])))

    def test_daily_report_has_gex_section(self):
        score = compute_score({})
        report = render_daily_report(score, {"text": "测试", "source": "rule"},
                                     {"gex": [self.snap]})
        self.assertIn("GEX / Gamma 翻转点", report)
        self.assertIn("GLD 黄金ETF代理", report)


if __name__ == "__main__":
    unittest.main()
