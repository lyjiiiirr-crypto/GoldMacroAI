import unittest

from alerts.message_templates import render_daily_report, render_fusion_lines
from alerts.telegram_bot import COMMANDS
from analysis.macro_score import compute_score
from analysis.tech_gex_fusion import fuse, gex_direction, technical_direction


def technical_row(score=None, bias="中性震荡", label="COMEX黄金期货"):
    return {"payload": {"score": score, "bias": bias, "label": label}}


def gex_row(symbol, net_gex, distance_pct, regime, label=None):
    return {
        "symbol": symbol, "net_gex": net_gex, "distance_pct": distance_pct,
        "regime": regime,
        "payload": {"label": label or symbol, "net_gex": net_gex,
                    "distance_pct": distance_pct, "regime": regime},
    }


class TechnicalDirectionTests(unittest.TestCase):
    def test_score_maps_to_direction(self):
        result = technical_direction([technical_row(score=4.0, bias="偏多")])
        self.assertTrue(result["ok"])
        self.assertEqual(result["dir"], 1.0)
        self.assertEqual(result["bias"], "偏多")
        self.assertEqual(result["label"], "COMEX黄金期货")

    def test_bias_used_when_score_missing(self):
        result = technical_direction([technical_row(score=None, bias="偏空")])
        self.assertAlmostEqual(result["dir"], -0.6)
        self.assertIsNone(result["score"])

    def test_score_from_top_level_fallback(self):
        result = technical_direction([{"score": -2.0, "bias": "偏空"}])
        self.assertAlmostEqual(result["dir"], -0.5)

    def test_empty_reports(self):
        result = technical_direction([])
        self.assertFalse(result["ok"])
        self.assertEqual(result["dir"], 0.0)
        self.assertEqual(result["bias"], "无数据")


class GexDirectionTests(unittest.TestCase):
    def test_negative_gamma_below_flip_is_bearish_and_amplifies(self):
        rows = [gex_row("GLD", -400_000_000, -6.4, "负Gamma")]
        result = gex_direction(rows)
        self.assertTrue(result["ok"])
        self.assertLess(result["dir"], 0)
        self.assertGreater(result["factor"], 1.0)
        self.assertEqual(result["details"][0]["symbol"], "GLD")
        self.assertLess(result["details"][0]["dir"], 0)

    def test_positive_gamma_above_flip_is_mildly_bullish_and_dampens(self):
        rows = [gex_row("GLD", 200_000_000, 4.0, "正Gamma")]
        result = gex_direction(rows)
        self.assertGreater(result["dir"], 0)
        self.assertLess(result["factor"], 1.0)

    def test_wrong_side_of_flip_is_neutral(self):
        # 负 Gamma 但现价高于 flip：不构成方向确认
        result = gex_direction([gex_row("GLD", -100, 5.0, "负Gamma")])
        self.assertEqual(result["dir"], 0.0)

    def test_uses_latest_per_symbol_and_sorts_by_weight(self):
        rows = [
            gex_row("SPY", 100, 5.0, "正Gamma"),
            gex_row("GLD", -100, -5.0, "负Gamma"),
        ]
        result = gex_direction(rows)
        self.assertEqual(len(result["details"]), 2)
        self.assertEqual(result["details"][0]["symbol"], "GLD")

    def test_regime_inferred_from_net_gex(self):
        row = {"symbol": "GLD", "net_gex": -100, "distance_pct": -5.0}
        result = gex_direction([row])
        self.assertEqual(result["details"][0]["regime"], "负Gamma")

    def test_empty_snapshots(self):
        result = gex_direction([])
        self.assertFalse(result["ok"])
        self.assertEqual(result["dir"], 0.0)
        self.assertEqual(result["factor"], 1.0)


class FusionTests(unittest.TestCase):
    def test_bearish_technical_and_bearish_gex(self):
        technical = [technical_row(score=-4.0, bias="偏空")]
        gex = [
            gex_row("GLD", -4e8, -6.4, "负Gamma", "GLD 黄金ETF代理"),
            gex_row("SPY", -9.7e9, -6.74, "负Gamma", "SPY 大盘风险偏好"),
        ]
        result = fuse(technical, gex)
        self.assertEqual(result["direction"], "偏空")
        self.assertLess(result["score"], 0)
        self.assertIn("融合分", result["reason"])
        self.assertIn("GLD", result["reason"])
        self.assertIn("SPY", result["reason"])
        self.assertIn("非交易信号", result["disclaimer"])

    def test_bullish_technical_and_bullish_gex(self):
        result = fuse(
            [technical_row(score=4.0, bias="偏多")],
            [gex_row("GLD", 2e8, 5.0, "正Gamma")],
        )
        self.assertEqual(result["direction"], "偏多")
        self.assertGreater(result["score"], 0)

    def test_conflicting_signals_stay_near_neutral(self):
        result = fuse(
            [technical_row(score=4.0, bias="偏多")],
            [gex_row("GLD", -4e8, -6.4, "负Gamma")],
        )
        self.assertGreater(result["score"], 0)  # 技术面权重更高
        self.assertLess(result["score"], 0.5)

    def test_no_data_is_neutral(self):
        result = fuse([], [])
        self.assertEqual(result["direction"], "中性震荡")
        self.assertFalse(result["technical"]["ok"])
        self.assertFalse(result["gex"]["ok"])

    def test_score_is_clamped(self):
        result = fuse(
            [technical_row(score=1e6, bias="偏多")],
            [gex_row("GLD", 1e12, 100.0, "正Gamma")],
        )
        self.assertLessEqual(result["score"], 1.0)
        self.assertGreaterEqual(result["score"], -1.0)

    def test_strength_label_scales_with_score(self):
        strong = fuse([technical_row(score=4.0, bias="偏多")], [])
        weak = fuse([technical_row(score=1.0, bias="偏多")], [])
        self.assertEqual(strong["strength"], "强")
        self.assertEqual(weak["strength"], "弱")


class FusionReportTests(unittest.TestCase):
    def test_daily_report_has_fusion_section(self):
        score = compute_score({})
        report = render_daily_report(
            score, {"text": "测试", "source": "rule"},
            {"technical": [technical_row(score=-4.0, bias="偏空")],
             "gex": [gex_row("GLD", -4e8, -6.4, "负Gamma", "GLD 黄金ETF代理")]},
        )
        self.assertIn("技术面 × GEX 融合方向", report)
        self.assertIn("结论：", report)
        self.assertIn("偏空", report)
        self.assertIn("融合分", report)
        self.assertIn("非交易信号", report)

    def test_fusion_absent_when_no_market_context(self):
        score = compute_score({})
        report = render_daily_report(score, {"text": "测试", "source": "rule"}, {})
        self.assertNotIn("技术面 × GEX 融合方向", report)

    def test_fusion_lines_no_data_state(self):
        text = "\n".join(render_fusion_lines(fuse([], [])))
        self.assertIn("技术面暂无数据", text)
        self.assertIn("GEX 暂无数据", text)
        self.assertIn("中性震荡", text)

    def test_report_does_not_mutate_macro_score(self):
        market = {"technical": [technical_row(score=4.0, bias="偏多")],
                  "gex": [gex_row("GLD", 2e8, 5.0, "正Gamma")]}
        score = compute_score({})
        before = score.total
        render_daily_report(score, {"text": "测试", "source": "rule"}, market)
        self.assertEqual(score.total, before)

    def test_fusion_command_registered(self):
        self.assertIn("/fusion", COMMANDS)
        self.assertIn("融合", COMMANDS["/fusion"])


if __name__ == "__main__":
    unittest.main()
