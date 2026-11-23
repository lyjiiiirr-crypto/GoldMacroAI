import io
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from data.cot import parse_cot_zip
from data.dxy import compute_dxy, parse_frankfurter, parse_yahoo_chart
from data.etf import parse_flows, parse_holdings
from data.fedwatch import change_pp, format_snapshot
from data.fedwatch_auto import parse_daily_snapshot
from data.fomc import parse_calendar_html
from data.macro_auto import parse_bls_payload
from database.models import Database
from scheduler.jobs import build_market


def ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


class ParserTests(unittest.TestCase):
    def test_fedwatch_daily_snapshot(self):
        payload = {
            "snapshot_date": "2026-11-22",
            "meetings": [{
                "meeting_date": "2026-12-09",
                "summary": {"ease": 0, "no_change": 11.4, "hike": 88.6},
            }],
        }
        snap = parse_daily_snapshot(payload)
        self.assertEqual(snap["cut"], 0.0)
        self.assertEqual(snap["hike"], 88.6)
        self.assertEqual(snap["meeting"], "2026-12-09")
        self.assertIn("88.6", snap["raw_json"])
        self.assertIsNone(parse_daily_snapshot({"meetings": []}))

    def test_fedwatch_dovish_metric(self):
        snapshots = [
            {"snapshot_date": "2026-11-22", "cut_probability": 0.0,
             "hike_probability": 88.6},
            {"snapshot_date": "2026-11-14", "cut_probability": 0.0,
             "hike_probability": 100.0},
        ]
        info = change_pp(snapshots, days=7)
        self.assertEqual(info["change_pp"], 0.0)
        self.assertEqual(info["dovish_pp"], 11.4)
        self.assertEqual(info["metric_pp"], 11.4)
        self.assertIn("鹰鸽净变化", format_snapshot(info))

    def test_fedwatch_manual_snapshot_without_hike(self):
        snapshots = [
            {"snapshot_date": "2026-11-22", "cut_probability": 65.0},
            {"snapshot_date": "2026-11-14", "cut_probability": 55.0},
        ]
        info = change_pp(snapshots, days=7)
        self.assertIsNone(info["dovish_pp"])
        self.assertEqual(info["metric_pp"], 10.0)
        self.assertIn("降息概率变化", format_snapshot(info))

    def test_yahoo_chart_parse(self):
        payload = {"chart": {"result": [{
            "timestamp": [ms("2026-11-20"), ms("2026-11-21")],
            "indicators": {"quote": [{"close": [100.1, None]}]},
        }]}}
        rows = parse_yahoo_chart(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-11-20")
        self.assertEqual(rows[0]["value"], 100.1)

    def test_frankfurter_dxy_fallback(self):
        rates = {"EUR": 0.90992, "JPY": 158.64, "GBP": 0.76192,
                 "CAD": 1.4307, "SEK": 10.2025, "CHF": 0.83922}
        value = compute_dxy(rates)
        self.assertIsNotNone(value)
        self.assertGreater(value, 95)
        self.assertLess(value, 110)
        payload = {"rates": {"2026-11-19": rates, "2026-11-20": rates}}
        rows = parse_frankfurter(payload)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["date"], "2026-11-20")
        self.assertIsNone(compute_dxy({"EUR": 0}))

    def test_bls_derived_series(self):
        payload = {
            "status": "REQUEST_SUCCEEDED",
            "Results": {"series": [
                {"seriesID": "CUSR0000SA0", "data": [
                    {"year": "2026", "period": "M10", "value": "110.0"},
                    {"year": "2025", "period": "M10", "value": "100.0"},
                    {"year": "2026", "period": "M13", "value": "105.0"},
                ]},
                {"seriesID": "CES0000000001", "data": [
                    {"year": "2026", "period": "M10", "value": "159293"},
                    {"year": "2026", "period": "M09", "value": "159128"},
                ]},
            ]},
        }
        parsed = parse_bls_payload(payload)
        self.assertEqual(parsed["CPI_YOY"], [{"date": "2026-10-01", "value": 10.0}])
        self.assertEqual(parsed["NFP_CHG"], [{"date": "2026-10-01", "value": 165.0}])

    def test_cot_zip_parse(self):
        buffer = io.BytesIO()
        header = ["Market_and_Exchange_Names", "FutOnly_or_Combined",
                  "Report_Date_as_YYYY-MM-DD", "M_Money_Positions_Long_All",
                  "M_Money_Positions_Short_All", "Open_Interest_All"]
        row = ["GOLD - COMMODITY EXCHANGE INC.", "FutOnly", "2026-11-17",
               "121994", "19052", "407834"]
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("f_year.txt", ",".join(header) + "\n" + ",".join(row))
        parsed = parse_cot_zip(buffer.getvalue())
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["value"], 102942)
        self.assertEqual(parsed[0]["long"], 121994)

    def test_wgc_holdings_and_flows(self):
        cols = ["Date", "North America", "Europe", "Asia", "Other", "Gold, US$/oz"]
        day = "2026-11-13"
        holdings_payload = {"chartData": {"asOfDate": day, "data": {"Weekly": {
            "tonnes": {"columns": cols, "set": [[ms(day), 100, 200, 50, 20, 4000]]},
            "usd": {"columns": cols, "set": [[ms(day), 10, 20, 5, 2, 4000]]},
        }}}}
        holdings = parse_holdings(holdings_payload)
        self.assertEqual(holdings[0]["tonnes"], 370.0)
        self.assertEqual(holdings[0]["regions"]["Europe"], 200.0)

        flows_payload = {"chartData": {"data": {"Weekly": {"series": {
            "tonnes": [{"name": "Europe", "data": [[ms(day), 2.5]]}],
            "usd": [{"name": "Europe", "data": [[ms(day), 1000]]}],
        }}}}}
        flows = parse_flows(flows_payload)
        self.assertEqual(flows[day]["tonnes"], 2.5)
        self.assertEqual(flows[day]["usd"], 1000.0)

    def test_fomc_calendar_parse(self):
        html = (
            '<a href="/newsevents/pressreleases/monetary20260128a.htm">Statement</a>'
            '<a href="/monetarypolicy/fomcminutes20260128.htm">Minutes</a>'
            '<a href="/monetarypolicy/fomcpresconf20260128.htm">Press</a>'
            '<a href="/monetarypolicy/fomcprojtabl20260318.htm">SEP</a>'
        )
        docs = parse_calendar_html(html)
        self.assertEqual(len(docs), 4)
        self.assertEqual({d["doc_type"] for d in docs},
                         {"Statement", "Minutes", "Press Conference", "SEP/Dot Plot"})
        self.assertTrue(docs[0]["url"].startswith("https://www.federalreserve.gov"))


class DatabaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_adds_new_columns(self):
        with self.db._connect() as conn:
            conn.executescript("""
                CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_name TEXT, actual REAL, expected REAL, previous REAL,
                    scale REAL, release_date TEXT, created_at TEXT);
                CREATE TABLE flows (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    flow_type TEXT, as_of_date TEXT, value REAL, scale REAL, created_at TEXT);
                CREATE TABLE fedwatch_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_date TEXT UNIQUE, cut_probability REAL, hold_probability REAL,
                    hike_probability REAL, meeting_date TEXT, source TEXT, created_at TEXT);
            """)
        self.db.init_schema()
        self.db.upsert_event("CPI", 2.7, 2.8, 3.0, 0.3, "2026-11-12", direction="normal")
        self.db.upsert_flow("COT", "2026-11-17", 102942, 30000, source="CFTC")
        self.assertEqual(len(self.db.events()), 1)
        self.assertEqual(len(self.db.flows()), 1)

    def test_upserts_are_idempotent(self):
        self.db.init_schema()
        for _ in range(3):
            self.db.upsert_flow("ETF", "2026-11-13", 4268.23, 50,
                                raw_json='{"weekly_flow_tonnes": 2.5}')
            self.db.upsert_event("初请失业金", 250000, 200000, 210000, 25000,
                                 "2026-11-19", direction="inverse")
        self.assertEqual(len(self.db.flows()), 1)
        self.assertEqual(len(self.db.events()), 1)
        self.assertEqual(self.db.flows()[0]["raw_json"],
                         '{"weekly_flow_tonnes": 2.5}')

    def test_inverse_direction_flips_surprise(self):
        self.db.init_schema()
        self.db.upsert_event("初请失业金", 250000, 200000, 210000, 25000,
                             "2026-11-19", direction="inverse")
        market, inputs = build_market(self.db)
        self.assertEqual(market["events"][0]["surprise"], -50000.0)
        self.assertEqual(inputs["econ_data"][0]["surprise"], -50000.0)


if __name__ == "__main__":
    unittest.main()
