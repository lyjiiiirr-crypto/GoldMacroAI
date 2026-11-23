import unittest

from alerts.alert_rules import evaluate
from analysis.macro_score import compute_score


def score_with(total: float, bias: str):
    result = compute_score({
        "fed_policy": {"change_pp": 0, "stale": False},
        "dollar": {"pct": 0, "stale": False},
        "real_rate": {"delta_bp": total * 0.8, "stale": False},
    })
    result.total, result.bias = total, bias
    return result


class AlertRuleTests(unittest.TestCase):
    def test_bias_change_alert(self):
        current = score_with(40, "温和偏多")
        previous = score_with(0, "中性震荡")
        alerts = evaluate(current, previous, {})
        self.assertTrue(any(a.kind == "bias_change" for a in alerts))

    def test_no_repeat_when_bias_unchanged(self):
        current = score_with(40, "温和偏多")
        previous = score_with(30, "温和偏多")
        alerts = evaluate(current, previous, {})
        self.assertFalse(any(a.kind == "bias_change" for a in alerts))

    def test_market_move_alerts(self):
        market = {
            "fedwatch": {"change_pp": -10.0, "cut_probability": 40.0, "as_of": "2026-11-22", "compared_with": "2026-11-15"},
            "DFII10": {"delta": 0.12, "date": "2026-11-22"},
            "DTWEXBGS": {"pct": 1.1, "date": "2026-11-22"},
        }
        kinds = {a.kind for a in evaluate(score_with(0, "中性震荡"), None, market)}
        self.assertIn("fedwatch_shift", kinds)
        self.assertIn("real_rate_move", kinds)
        self.assertIn("dollar_move", kinds)


if __name__ == "__main__":
    unittest.main()
