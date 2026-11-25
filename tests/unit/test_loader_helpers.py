"""Unit tests for etl/load/load_postgres.py helper functions (no database needed).

Run from the project root:   python -m unittest discover -s tests/unit -v
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("load_postgres", ROOT / "etl" / "load" / "load_postgres.py")
lp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lp)


class SplitName(unittest.TestCase):
    def test_company_and_kind(self):
        self.assertEqual(lp.split_name("clean_Nabil_Bank_Limited_company_news"),
                         ("Nabil Bank Limited", "company_news"))

    def test_financials_kind(self):
        self.assertEqual(lp.split_name("clean_Unilever_Nepal_Limited_financials"),
                         ("Unilever Nepal Limited", "financials"))

    def test_parentheses_in_company_name(self):
        self.assertEqual(
            lp.split_name("clean_Nepal_Doorsanchar_Company_Limited_(Nepal_Telecom)_company_market_data"),
            ("Nepal Doorsanchar Company Limited (Nepal Telecom)", "company_market_data"))

    def test_unknown_file_is_not_a_kind(self):
        self.assertEqual(lp.split_name("clean_company_snapshot"), (None, None))

    def test_all_nine_kinds_are_recognised(self):
        for kind in lp.KINDS:
            company, found = lp.split_name(f"clean_Some_Company_{kind}")
            self.assertEqual((company, found), ("Some Company", kind))


class Sanitize(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(lp.sanitize("Page Title", set()), "page_title")

    def test_reserved_names_get_suffix(self):
        for name in ("id", "company", "run_id", "scraped_date"):
            self.assertEqual(lp.sanitize(name, set()), name + "_src")

    def test_duplicates_are_numbered(self):
        used = set()
        self.assertEqual(lp.sanitize("Price", used), "price")
        self.assertEqual(lp.sanitize("price", used), "price_2")

    def test_leading_digit(self):
        self.assertEqual(lp.sanitize("1st Value", set()), "c_1st_value")


class InferType(unittest.TestCase):
    def test_numeric(self):
        self.assertEqual(lp.infer_type("eps", ["1", "2.5", "-3.25", ""]), "numeric")

    def test_leading_zeros_stay_text(self):
        self.assertEqual(lp.infer_type("code", ["007", "012"]), "text")

    def test_date(self):
        self.assertEqual(lp.infer_type("d", ["2026-11-23", "2026-01-05"]), "date")

    def test_invalid_date_is_text(self):
        self.assertEqual(lp.infer_type("d", ["2026-13-45"]), "text")

    def test_boolean(self):
        self.assertEqual(lp.infer_type("flag", ["True", "false"]), "boolean")

    def test_page_columns_are_bigint(self):
        self.assertEqual(lp.infer_type("page", ["1", "2"]), "bigint")

    def test_mixed_is_text(self):
        self.assertEqual(lp.infer_type("x", ["12", "n/a"]), "text")

    def test_all_empty_is_text(self):
        self.assertEqual(lp.infer_type("x", ["", ""]), "text")


class Fits(unittest.TestCase):
    def test_numeric(self):
        self.assertTrue(lp.fits("numeric", "12.5"))
        self.assertTrue(lp.fits("numeric", "1e5"))
        self.assertFalse(lp.fits("numeric", "abc"))

    def test_empty_always_fits(self):
        self.assertTrue(lp.fits("numeric", ""))

    def test_date(self):
        self.assertTrue(lp.fits("date", "2026-11-23"))
        self.assertFalse(lp.fits("date", "23/11/2026"))

    def test_text_accepts_anything(self):
        self.assertTrue(lp.fits("text", "anything at all"))


if __name__ == "__main__":
    unittest.main()
