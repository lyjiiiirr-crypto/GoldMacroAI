import unittest

from analysis.data_cleaner import age_days, change_by_days, pct_change
from data.fedwatch import change_pp
from data.fred import parse_fred_csv, parse_fred_html


class DataCleanerTests(unittest.TestCase):
    def test_change_by_days_uses_past_not_future(self):
        rows = [
            {"date": "2026-11-20", "value": 1.80},
            {"date": "2026-11-13", "value": 2.00},
            {"date": "2026-11-06", "value": 2.10},
        ]
        change = change_by_days(rows, days=7)
        self.assertAlmostEqual(change["delta"], -0.20, places=4)
        self.assertEqual(change["previous_date"], "2026-11-13")

    def test_missing_history_returns_none(self):
        self.assertIsNone(change_by_days([{"date": "2026-11-20", "value": 1.8}], days=7))

    def test_pct_change(self):
        change = {"delta": -1.0, "previous": 100.0}
        self.assertAlmostEqual(pct_change(change), -1.0)

    def test_age_days(self):
        self.assertEqual(age_days("2026-11-20") >= 0, True)

    def test_fred_csv_parse_without_api_key(self):
        text = "observation_date,DFII10\n2026-11-20,1.80\n2026-11-19,.\n2026-11-18,1.85\n"
        rows = parse_fred_csv(text, "DFII10")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {"date": "2026-11-20", "value": 1.80})

    def test_fred_html_fallback_parse(self):
        text = "<div>2026-11-19: 2.88</div><div>2026-11-18: 2.88</div>"
        rows = parse_fred_html(text)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["value"], 2.88)

    def test_fedwatch_change(self):
        snaps = [
            {"snapshot_date": "2026-11-22", "cut_probability": 65.0},
            {"snapshot_date": "2026-11-15", "cut_probability": 50.0},
        ]
        info = change_pp(snaps, days=7)
        self.assertAlmostEqual(info["change_pp"], 15.0)


if __name__ == "__main__":
    unittest.main()
