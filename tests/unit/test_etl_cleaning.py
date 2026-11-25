"""Unit tests for the ETL cleaning rules (etl.py). Skipped automatically if etl.py is not found.

Run from the project root:   python -m unittest discover -s tests/unit -v
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANDIDATES = [ROOT / "etl" / "etl.py", ROOT / "etl.py", ROOT / "etl" / "transform" / "etl.py"]
ETL_PATH = next((p for p in CANDIDATES if p.exists()), None)

etl = None
if ETL_PATH:
    spec = importlib.util.spec_from_file_location("etl_module", ETL_PATH)
    etl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(etl)


@unittest.skipUnless(etl, "etl.py not found")
class CleanField(unittest.TestCase):
    def test_currency_and_commas_to_float(self):
        self.assertEqual(etl.clean_field("$1,299.00", {"type": "float"}), (1299.0, None))

    def test_whitespace_collapsed(self):
        self.assertEqual(etl.clean_field("  Blue   Mug ", {}), ("Blue Mug", None))

    def test_html_entities_decoded(self):
        self.assertEqual(etl.clean_field("Tom &amp; Jerry", {}), ("Tom & Jerry", None))

    def test_placeholders_become_none(self):
        for raw in ("N/A", "n/a", "-", "null", "  ", ""):
            self.assertEqual(etl.clean_field(raw, {}), (None, None), raw)

    def test_below_minimum_is_rejected_not_changed(self):
        value, issue = etl.clean_field("-5", {"type": "float", "min": 0})
        self.assertIsNone(value)
        self.assertTrue(issue.startswith("OUT_OF_RANGE"))

    def test_date_is_normalised(self):
        self.assertEqual(etl.clean_field("23/11/2026", {"type": "date"}), ("2026-11-23", None))

    def test_unparseable_date_is_flagged(self):
        value, issue = etl.clean_field("not a date", {"type": "date"})
        self.assertIsNone(value)
        self.assertTrue(issue.startswith("INVALID_TYPE"))

    def test_bool(self):
        self.assertEqual(etl.clean_field("In Stock", {"type": "bool"}), (True, None))

    def test_default_is_reported(self):
        self.assertEqual(etl.clean_field("", {"default": "x"}), ("x", "DEFAULT_APPLIED"))

    def test_placeholder_date_can_be_nulled(self):
        rule = {"type": "date", "formats": ["%Y/%m/%d %H:%M:%S"], "null_values": ["1900/01/01 12:00:00"]}
        self.assertEqual(etl.clean_field("1900/01/01 12:00:00", rule), (None, None))


def row(target, item, field, value, status="OK", required="True", url="https://x/p"):
    return {"target": target, "page": "1", "item_index": str(item), "field": field, "value": value,
            "status": status, "reason": "", "source_url": url, "html_line": "1", "html_column": "1",
            "xpath": "/html", "required": required}


@unittest.skipUnless(etl, "etl.py not found")
class Transform(unittest.TestCase):
    CFG = {"targets": [{"name": "shop", "dedupe_on": ["title", "price"], "fields": [
        {"name": "title", "required": True},
        {"name": "price", "type": "float", "required": True, "min": 0}]}]}

    def run_rows(self, rows):
        return etl.transform(rows, self.CFG)

    def test_good_record_is_kept(self):
        res = self.run_rows([row("shop", 1, "title", "Mug"), row("shop", 1, "price", "$9.50")])
        self.assertEqual(len(res["clean"]["shop"]), 1)
        self.assertEqual(res["clean"]["shop"][0]["price"], 9.5)
        self.assertEqual(res["rejects"], [])

    def test_missing_required_field_goes_to_rejects(self):
        res = self.run_rows([row("shop", 1, "title", "Mug"), row("shop", 1, "price", "N/A")])
        self.assertEqual(len(res["rejects"]), 1)
        self.assertEqual(res["rejects"][0]["reason"], "REQUIRED_FIELD_MISSING")
        self.assertNotIn("shop", res["clean"])

    def test_upstream_flag_is_carried_through(self):
        res = self.run_rows([row("shop", 1, "title", "Mug"),
                             row("shop", 1, "price", "", status="NOT_FOUND")])
        self.assertEqual(res["rejects"][0]["reason"], "REQUIRED_FIELD_MISSING")

    def test_duplicate_is_rejected(self):
        rows = [row("shop", 1, "title", "Mug"), row("shop", 1, "price", "9"),
                row("shop", 2, "title", "Mug"), row("shop", 2, "price", "9.00")]
        res = self.run_rows(rows)
        self.assertEqual(len(res["clean"]["shop"]), 1)
        self.assertEqual(res["rejects"][0]["reason"], "DUPLICATE")

    def test_lineage_keeps_source_location(self):
        res = self.run_rows([row("shop", 1, "title", "Mug"), row("shop", 1, "price", "9")])
        self.assertTrue(all(l["xpath"] == "/html" and l["source_url"] == "https://x/p" for l in res["lineage"]))


if __name__ == "__main__":
    unittest.main()
