"""Integration test for the SQL parser (database/migrations/002) and the ranking view (003).

Needs the Docker database running and the migrations applied. It inserts test rows inside a
transaction and ROLLS BACK at the end, so your real data is never changed.
Skipped automatically if the database or the parser is not available.

Run from the project root:   python -m unittest discover -s tests/integration -v

Settings (optional environment variables):
  NCI_PG_USER (default Baidehi)   NCI_PG_DB (default nepse_intel)   NCI_PG_CONTAINER (default nci_postgres)
  NCI_PG_MODE=docker|host         NCI_PG_HOST / NCI_PG_PORT when mode=host
"""
import argparse
import importlib.util
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("load_postgres", ROOT / "etl" / "load" / "load_postgres.py")
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)


def make_text(symbol, price, pct, last_traded, eps, pe, dividend):
    return (f"Sector Commercial Banks | Shares Outstanding 1,000,000.00 | Market Price {price} | % Change {pct} | "
            f"Last Traded On {last_traded} | 52 Weeks High - Low 1,300.00-900.00 | 180 Day Average 1,100.00 | "
            f"120 Day Average 1,150.00 | 1 Year Yield 8.70% | EPS {eps} | P/E Ratio {pe} | Book Value 500.00 | "
            f"PBV 2.50 | % Dividend {dividend} | # Fiscal Year Value 1. x | % Bonus 5.00 (FY:082-083) | "
            f"30-Day Avg Volume 46,801.00 | Market Capitalization 1,250,500,000.00 | Symbol {symbol} | "
            f"Company Name Test {symbol} Ltd | Sector Commercial Banks | Listed Shares 1,000,000.00 | "
            f"Paidup Value 100.00 | Total Paidup Value 100,000,000.00")


TEXTS = {
    "TESTNORM": make_text("TESTNORM", "1,250.50", "-1.25 %", "2026/11/23 11:10:58",
                          "1,234.56 (FY:082-083, Q:4)", "1.01", "20.00 (FY:082-083)"),
    "TESTNEG": make_text("TESTNEG", "338.10", "0.50 %", "2026/11/23 11:10:58",
                         "-12.86 (FY:083-084, Q:1)", "-26.29", ""),
    "TESTDEAD": make_text("TESTDEAD", "0.00", "0 %", "1900/01/01 12:00:00",
                          "0.00 (FY:, Q:0)", "0.00", ""),
}


def connect():
    args = argparse.Namespace(
        mode=os.environ.get("NCI_PG_MODE", "docker"), container=os.environ.get("NCI_PG_CONTAINER", "nci_postgres"),
        host=os.environ.get("NCI_PG_HOST", "localhost"), port=int(os.environ.get("NCI_PG_PORT", "5432")),
        user=os.environ.get("NCI_PG_USER", "Baidehi"), db=os.environ.get("NCI_PG_DB", "nepse_intel"), dry_run=False)
    return lp.Psql(args, {"POSTGRES_PASSWORD": os.environ.get("NCI_PG_PASSWORD", "")})


class ParserSql(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.db = connect()
            cls.db.run("SELECT 1 FROM company_market_data LIMIT 1;")
            if cls.db.run("SELECT to_regproc('refresh_company_snapshot') IS NOT NULL;") != "t":
                raise lp.PsqlError("refresh_company_snapshot() is missing")
        except (lp.PsqlError, SystemExit) as e:
            raise unittest.SkipTest(f"database/parser not available: {e}")
        values = ",\n".join(f"('Test {s}', 'https://x/CompanyDetail.aspx?symbol={s}', $t${t}$t$)" for s, t in TEXTS.items())
        has_view = cls.db.run("SELECT to_regclass('v_company_ranking') IS NOT NULL;") == "t"
        script = (
            "BEGIN;\n"
            f"INSERT INTO company_market_data (company, source_url, table_rows) VALUES\n{values};\n"
            "SELECT 'N|' || refresh_company_snapshot();\n"
            "SELECT 'S|' || concat_ws('|', symbol, company_name, sector, listed_shares, paidup_value, market_price, pct_change, "
            "last_traded_on, week52_high, week52_low, eps, eps_fiscal_year, pe_ratio, book_value, avg_volume_30_day, "
            "market_cap, dividend_pct, dividend_fiscal_year, bonus_pct, quality) FROM company_snapshot "
            "WHERE symbol LIKE 'TEST%' ORDER BY symbol;\n"
            + ("SELECT 'V|' || concat_ws('|', symbol, coalesce(pe_valid::text, 'NULL'), coalesce(dividend_yield_pct::text, 'NULL')) "
               "FROM v_company_ranking WHERE symbol LIKE 'TEST%' ORDER BY symbol;\n" if has_view else "")
            + "ROLLBACK;\n")
        out = cls.db.run(script).splitlines()
        cls.has_view = has_view
        cls.snap = {l.split("|")[1]: l.split("|")[1:] for l in out if l.startswith("S|")}
        cls.view = {l.split("|")[1]: l.split("|")[1:] for l in out if l.startswith("V|")}

    def test_all_three_rows_parsed(self):
        self.assertEqual(set(self.snap), set(TEXTS))

    def test_normal_row_numbers_with_commas(self):
        row = self.snap["TESTNORM"]
        self.assertIn("1250.50", row)           # market price from "1,250.50"
        self.assertIn("-1.25", row)             # % change
        self.assertIn("1234.56", row)           # EPS from "1,234.56 (FY:...)"
        self.assertIn("082-083", row)           # fiscal year extracted
        self.assertIn("1300.00", row)           # 52-week high
        self.assertIn("900.00", row)            # 52-week low
        self.assertIn("2026-11-23", row)        # last traded date
        self.assertIn("20.00", row)             # dividend %
        self.assertIn("5.00", row)              # bonus %
        self.assertIn("1000000.00", row)        # listed shares
        self.assertEqual(row[-1], "complete")

    def test_negative_eps_is_parsed(self):
        self.assertIn("-12.86", self.snap["TESTNEG"])
        self.assertEqual(self.snap["TESTNEG"][-1], "complete")

    def test_delisted_row_is_inactive(self):
        row = self.snap["TESTDEAD"]
        self.assertEqual(row[-1], "inactive")
        self.assertNotIn("1900-01-01", row)     # placeholder date is dropped

    def test_view_excludes_inactive_and_hides_invalid_pe(self):
        if not self.has_view:
            self.skipTest("v_company_ranking not created (migration 003)")
        self.assertNotIn("TESTDEAD", self.view)
        self.assertEqual(self.view["TESTNEG"][1], "NULL")      # negative EPS -> no valid P/E
        self.assertEqual(self.view["TESTNORM"][1], "1.01")
        self.assertEqual(self.view["TESTNORM"][2], "1.60")     # 20% of 100 / 1250.50 * 100 = 1.60

    def test_rollback_left_no_test_data(self):
        self.assertEqual(self.db.run("SELECT count(*) FROM company_snapshot WHERE symbol LIKE 'TEST%';"), "0")


if __name__ == "__main__":
    unittest.main()
