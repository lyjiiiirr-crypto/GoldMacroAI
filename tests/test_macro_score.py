import unittest

from analysis.macro_score import compute_score, classify


def neutral_context(**overrides):
    base = {
        "fed_policy": {"change_pp": 0.0, "stale": False},
        "dollar": {"pct": 0.0, "stale": False},
    }
    base.update(overrides)
    return base


class MacroScoreTests(unittest.TestCase):
    def test_real_rate_fall_is_bullish_for_gold(self):
        result = compute_score(neutral_context(real_rate={"delta_bp": -20.0, "stale": False}))
        self.assertGreater(result.total, 0)
        self.assertEqual(result.bias, "温和偏多")
        self.assertAlmostEqual(result.signals[1].contribution, 25.0, places=1)

    def test_real_rate_rise_is_bearish_for_gold(self):
        result = compute_score(neutral_context(real_rate={"delta_bp": 20.0, "stale": False}))
        self.assertLess(result.total, 0)
        self.assertEqual(result.bias, "温和偏空")

    def test_no_data_returns_insufficient(self):
        result = compute_score({})
        self.assertEqual(result.total, 0)
        self.assertEqual(result.bias, "数据不足")
        self.assertFalse(result.sufficient)
        self.assertEqual(result.confidence, 0)

    def test_grade_boundaries(self):
        self.assertEqual(classify(60), "强烈偏多")
        self.assertEqual(classify(25), "温和偏多")
        self.assertEqual(classify(0), "中性震荡")
        self.assertEqual(classify(-25), "温和偏空")
        self.assertEqual(classify(-61), "强烈偏空")

    def test_stale_data_does_not_drive_direction(self):
        result = compute_score(neutral_context(real_rate={"delta_bp": -50.0, "stale": True}))
        self.assertEqual(result.total, 0)
        self.assertTrue(any("过期" in w for w in result.warnings))


if __name__ == "__main__":
    unittest.main()
