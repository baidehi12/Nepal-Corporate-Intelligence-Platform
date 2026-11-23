#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import html
import json
import hashlib
import re
import sys
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
NULL_VALUES = {"", "n/a", "na", "null", "none", "-", "--", "nan", "tbd", "unknown", "not available"}
DEFAULT_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y",
                        "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

LINEAGE_COLS = ["record_id", "target", "field", "raw_value", "clean_value", "status", "issue",
                "source_url", "html_line", "html_column", "xpath", "disposition"]
CHANGE_COLS = ["record_id", "target", "field", "raw_value", "clean_value", "source_url", "xpath"]
REJECT_COLS = ["record_id", "target", "page", "item_index", "source_url", "reason", "details", "raw_values"]


def safe_file_stem(value):
    """Create a Windows-safe, readable filename from a scraper target name."""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value))
    stem = re.sub(r"\s+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip(" ._")
    return stem[:150] or "target"


def unique_sheet_title(value, used):
    """Return a unique Excel worksheet title (Excel limit: 31 characters)."""
    cleaned = re.sub(r"[:\\/?*\[\]]", "_", str(value)).strip(" '") or "Sheet"
    digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:5]
    candidate = f"{cleaned[:25]}_{digest}"[:31]
    counter = 1
    while candidate.casefold() in used:
        suffix = f"_{digest[:4]}{counter}"
        candidate = f"{cleaned[:31-len(suffix)]}{suffix}"
        counter += 1
    used.add(candidate.casefold())
    return candidate


# ----------------------------------------------------------------------------
# Cleaning operations: each takes (value:str, args:dict) and returns str or None
# Raise ValueError if the value cannot be cleaned.
# ----------------------------------------------------------------------------
def op_strip(v, a): return v.strip()
def op_collapse_ws(v, a): return re.sub(r"\s+", " ", v).strip()
def op_unescape(v, a): return html.unescape(v).replace(" ", " ")
def op_strip_tags(v, a): return re.sub(r"<[^>]+>", " ", v)
def op_lower(v, a): return v.lower()
def op_upper(v, a): return v.upper()
def op_title(v, a): return v.title()
def op_remove_chars(v, a): return re.sub("[" + re.escape(a.get("chars", "")) + "]", "", v)
def op_prefix_strip(v, a): return v[len(a["text"]):] if v.startswith(a["text"]) else v
def op_regex_replace(v, a): return re.sub(a["pattern"], a.get("repl", ""), v)
def op_map(v, a):
    m = {k.lower(): val for k, val in a.get("mapping", {}).items()}
    return m.get(v.lower(), v)
def op_truncate(v, a): return v[: int(a.get("length", 255))]
def op_default(v, a): return v  # handled separately (applied only when value is empty)


OPS = {
    "strip": op_strip, "collapse_ws": op_collapse_ws, "unescape": op_unescape,
    "strip_tags": op_strip_tags, "lower": op_lower, "upper": op_upper, "title": op_title,
    "remove_chars": op_remove_chars, "prefix_strip": op_prefix_strip,
    "regex_replace": op_regex_replace, "map": op_map, "truncate": op_truncate,
}
DEFAULT_PIPELINE = ["unescape", "collapse_ws"]


def to_number(v, decimal="."):
    s = re.sub(r"[^\d.,\-]", "", v)
    if decimal == ",":
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    if not re.search(r"\d", s):
        raise ValueError(f"no number found in '{v}'")
    return float(s)


def to_date(v, formats, out_fmt):
    for f in formats:
        try:
            return datetime.strptime(v.strip(), f).strftime(out_fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised date '{v}'")


def to_bool(v):
    s = v.strip().lower()
    if s in ("true", "yes", "y", "1", "in stock", "available"):
        return True
    if s in ("false", "no", "n", "0", "out of stock", "unavailable"):
        return False
    raise ValueError(f"cannot read '{v}' as yes/no")


# ----------------------------------------------------------------------------
# Field cleaning
# ----------------------------------------------------------------------------
def clean_field(raw, rule):
    """Returns (clean_value, issue). clean_value None means 'no usable value'."""
    if raw is None:
        return None, None
    v = str(raw)
    null_values = {x.lower() for x in rule.get("null_values", NULL_VALUES)}

    pipeline = list(DEFAULT_PIPELINE)
    for step in rule.get("ops", []):
        pipeline.append(step)
    try:
        for step in pipeline:
            name, args = (step, {}) if isinstance(step, str) else (step["op"], step)
            if name not in OPS:
                raise ValueError(f"unknown op '{name}'")
            v = OPS[name](v, args)
            if v is None:
                break
    except (ValueError, KeyError, re.error) as e:
        return None, f"CLEAN_FAILED: {e}"

    if v is None or v.strip().lower() in null_values:
        if "default" in rule:
            return rule["default"], "DEFAULT_APPLIED"
        return None, None

    typ = rule.get("type", "text")
    try:
        if typ in ("float", "int"):
            n = to_number(v, rule.get("decimal", "."))
            if typ == "int":
                if n != int(n):
                    raise ValueError(f"'{v}' is not a whole number")
                n = int(n)
            else:
                n = round(n, rule.get("round", 6))
            val = n
        elif typ == "date":
            val = to_date(v, rule.get("formats", DEFAULT_DATE_FORMATS), rule.get("date_format", "%Y-%m-%d"))
        elif typ == "bool":
            val = to_bool(v)
        else:
            val = v
    except ValueError as e:
        return None, f"INVALID_TYPE: {e}"

    # Validation
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if "min" in rule and val < rule["min"]:
            return None, f"OUT_OF_RANGE: {val} < min {rule['min']}"
        if "max" in rule and val > rule["max"]:
            return None, f"OUT_OF_RANGE: {val} > max {rule['max']}"
    if isinstance(val, str):
        if "max_length" in rule and len(val) > rule["max_length"]:
            return None, f"TOO_LONG: {len(val)} > {rule['max_length']} chars"
        if "pattern" in rule and not re.fullmatch(rule["pattern"], val):
            return None, f"PATTERN_MISMATCH: '{val}' does not match {rule['pattern']}"
        if "allowed" in rule and val.lower() not in [a.lower() for a in rule["allowed"]]:
            return None, f"NOT_ALLOWED: '{val}' not in allowed values"
    return val, None


# ----------------------------------------------------------------------------
# Extract
# ----------------------------------------------------------------------------
def extract(path):
    p = Path(path)
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        return data["rows"] if isinstance(data, dict) else data
    with open(p, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def truthy(v):
    return str(v).strip().lower() in ("true", "1", "yes")


# ----------------------------------------------------------------------------
# Transform
# ----------------------------------------------------------------------------
def transform(rows, cfg):
    """Returns dict of results for load()."""
    tcfgs = {t["name"]: t for t in cfg.get("targets", [])}
    groups = OrderedDict()
    for r in rows:
        key = (r["target"], str(r["page"]), r.get("source_url", ""), str(r.get("item_index", "")))
        groups.setdefault(key, []).append(r)

    clean, rejects, changes, lineage = OrderedDict(), [], [], []
    stats = {"input_rows": len(rows), "records_in": len(groups), "records_clean": 0,
             "records_partial": 0, "records_rejected": 0, "duplicates": 0,
             "values_changed": 0, "issues": Counter(), "null_by_field": Counter(),
             "total_by_field": Counter()}
    seen = {}  # (target, dedupe key) -> record_id
    counters = Counter()

    for (target, page, src, item), grows in groups.items():
        tcfg = tcfgs.get(target, {})
        frules = {f["name"]: f for f in tcfg.get("fields", [])}
        counters[target] += 1
        rid = f"{target}-{counters[target]:05d}"

        # Group rows by field (multiple=true gives several rows per field)
        byfield = OrderedDict()
        for r in grows:
            byfield.setdefault(r["field"], []).append(r)
        for fname in frules:                      # fields expected by config but never scraped
            byfield.setdefault(fname, [])

        rec = OrderedDict(record_id=rid, target=target, page=page, item_index=item, source_url=src)
        issues, required_fail, optional_fail = [], [], []
        pending_lineage, pending_changes = [], []

        for fname, frows in byfield.items():
            rule = frules.get(fname, {})
            out_name = rule.get("rename", fname)
            required = rule["required"] if "required" in rule else (
                truthy(frows[0].get("required", "True")) if frows else True)
            stats["total_by_field"][(target, out_name)] += 1

            values, field_issue, raw_join = [], None, []
            first = frows[0] if frows else {}
            if not frows:
                field_issue = "MISSING_FIELD: not present in scraper output"
            for r in frows:
                raw_join.append(r.get("value", ""))
                if r.get("status", "OK") != "OK":
                    field_issue = field_issue or f"UPSTREAM_{r['status']}: {r.get('reason', '')}".strip()
                    continue
                v, issue = clean_field(r.get("value", ""), rule)
                if issue and not issue.startswith("DEFAULT_APPLIED"):
                    field_issue = field_issue or issue
                if v is not None:
                    values.append(v)

            if len(values) > 1:
                value = rule.get("join", " | ").join(str(x) for x in values)
            else:
                value = values[0] if values else None
            if value is None and "default" in rule and not any(
                    fr.get("status", "OK") != "OK" for fr in frows):
                value = rule["default"]

            rec[out_name] = value
            if value is None:
                stats["null_by_field"][(target, out_name)] += 1
                (required_fail if required else optional_fail).append(out_name)
            if field_issue:
                issues.append(f"{out_name}: {field_issue}")
                stats["issues"][field_issue.split(":")[0]] += 1

            raw_str = " | ".join(raw_join)
            if value is not None and frows and str(value) != raw_str:
                stats["values_changed"] += 1
                pending_changes.append((fname, out_name, first, value, raw_str))
            pending_lineage.append({
                "record_id": rid, "target": target, "field": out_name, "raw_value": raw_str,
                "clean_value": "" if value is None else value,
                "status": "OK" if value is not None else ("FLAGGED" if required else "FLAGGED_OPTIONAL"),
                "issue": field_issue or "", "source_url": first.get("source_url", src),
                "html_line": first.get("html_line", ""), "html_column": first.get("html_column", ""),
                "xpath": first.get("xpath", ""),
            })

        # Classify
        reason = details = None
        if required_fail:
            reason, details = "REQUIRED_FIELD_MISSING", ", ".join(required_fail)
        else:
            renames = {f["name"]: f.get("rename", f["name"]) for f in tcfg.get("fields", [])}
            dkey = [renames.get(k, k) for k in tcfg.get("dedupe_on", [])]  # accept original or renamed names
            if dkey:
                kv = tuple(rec.get(k) for k in dkey)
                if None not in kv:
                    if (target, kv) in seen:
                        reason, details = "DUPLICATE", f"same {dkey} as {seen[(target, kv)]}"
                        stats["duplicates"] += 1
                    else:
                        seen[(target, kv)] = rid

        if reason:
            disposition = "rejected"
            stats["records_rejected"] += 1
            rejects.append({"record_id": rid, "target": target, "page": page, "item_index": item,
                            "source_url": src, "reason": reason, "details": details,
                            "raw_values": json.dumps({lr["field"]: lr["raw_value"] for lr in pending_lineage},
                                                     ensure_ascii=False)})
        else:
            disposition = "partial" if (optional_fail or issues) else "complete"
            stats["records_partial" if disposition == "partial" else "records_clean"] += 1
            rec["quality"] = disposition
            rec["issues"] = "; ".join(issues)
            clean.setdefault(target, []).append(rec)

        for lr in pending_lineage:
            lr["disposition"] = disposition
            lineage.append(lr)
        if disposition != "rejected":
            for item_ in pending_changes:
                fname, out_name, r, v = item_[:4]
                raw_s = item_[4] if len(item_) > 4 else r.get("value", "")
                changes.append({"record_id": rid, "target": target, "field": out_name, "raw_value": raw_s,
                                "clean_value": v, "source_url": r.get("source_url", src), "xpath": r.get("xpath", "")})

    return {"clean": clean, "rejects": rejects, "changes": changes, "lineage": lineage, "stats": stats}


# ----------------------------------------------------------------------------
# Load
# ----------------------------------------------------------------------------
def csv_safe(v):
    if isinstance(v, str) and v[:1] in ("=", "+", "@", "\t", "\r"):
        return "'" + v
    return v


def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: csv_safe("" if r.get(c) is None else r.get(c)) for c in cols})


def build_report(result, source):
    s = result["stats"]
    fields = []
    for (t, f), total in sorted(s["total_by_field"].items()):
        nulls = s["null_by_field"].get((t, f), 0)
        fields.append({"target": t, "field": f, "records": total, "blank": nulls,
                       "blank_pct": round(100 * nulls / total, 1) if total else 0})
    return {
        "source": str(source), "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_rows": s["input_rows"], "records_in": s["records_in"],
        "records_complete": s["records_clean"], "records_partial": s["records_partial"],
        "records_rejected": s["records_rejected"], "duplicates_removed": s["duplicates"],
        "values_changed_by_cleaning": s["values_changed"],
        "issue_counts": dict(s["issues"]), "fields": fields,
        "reject_reasons": dict(Counter(r["reason"] for r in result["rejects"])),
    }


def write_xlsx(path, result, report):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    head = PatternFill("solid", fgColor="D9E2F3")
    amber = PatternFill("solid", fgColor="FFF3CD")

    def sheet(ws, cols, rows, flag_col=None):
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = Font(bold=True), head
        for r in rows:
            ws.append([_ILLEGAL.sub("", v) if isinstance(v, str) else ("" if v is None else v)
                       for v in (r.get(c) for c in cols)])
            for c in ws[ws.max_row]:
                if isinstance(c.value, str) and c.value[:1] in ("=", "+", "@"):
                    c.data_type = "s"
            if flag_col and r.get(flag_col) == "partial":
                for c in ws[ws.max_row]:
                    c.fill = amber
        ws.freeze_panes = "A2"
        for i, col in enumerate(cols, 1):
            width = max([len(str(col))] + [len(str(c.value)) for c in ws[ws.cell(1, i).column_letter][1:200]
                                           if c.value is not None])
            ws.column_dimensions[ws.cell(1, i).column_letter].width = min(width + 2, 50)

    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    for k, v in report.items():
        if k != "fields":
            ws.append([k, json.dumps(v) if isinstance(v, (dict, list)) else v])
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 80
    sheet(wb.create_sheet("Field quality"), ["target", "field", "records", "blank", "blank_pct"], report["fields"])
    used_sheet_names = {"report", "field quality", "rejects", "changes", "lineage"}
    for target, recs in result["clean"].items():
        cols = list(recs[0].keys())
        title = unique_sheet_title(f"clean_{target}", used_sheet_names)
        sheet(wb.create_sheet(title), cols, recs, flag_col="quality")
    sheet(wb.create_sheet("Rejects"), REJECT_COLS, result["rejects"])
    sheet(wb.create_sheet("Changes"), CHANGE_COLS, result["changes"])
    sheet(wb.create_sheet("Lineage"), LINEAGE_COLS, result["lineage"])
    wb.save(path)


def load(result, source, outdir, formats):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    report = build_report(result, source)
    for target, recs in result["clean"].items():
        cols = list(OrderedDict.fromkeys(k for r in recs for k in r))
        p = out / f"clean_{safe_file_stem(target)}.csv"
        write_csv(p, cols, recs)
        written.append(p)
    for name, cols, rows in (("rejects", REJECT_COLS, result["rejects"]),
                             ("changes", CHANGE_COLS, result["changes"]),
                             ("lineage", LINEAGE_COLS, result["lineage"])):
        p = out / f"{name}.csv"
        write_csv(p, cols, rows)
        written.append(p)
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    written.append(out / "report.json")
    if "xlsx" in formats:
        write_xlsx(out / "clean.xlsx", result, report)
        written.append(out / "clean.xlsx")
    return report, written


def print_report(report, written):
    print(f"\nRecords in: {report['records_in']}  |  complete: {report['records_complete']}  "
          f"partial: {report['records_partial']}  rejected: {report['records_rejected']}  "
          f"(duplicates: {report['duplicates_removed']})")
    print(f"Values changed by cleaning: {report['values_changed_by_cleaning']}")
    if report["reject_reasons"]:
        print("Reject reasons:", report["reject_reasons"])
    if report["issue_counts"]:
        print("Field issues:", report["issue_counts"])
    blanks = [f for f in report["fields"] if f["blank"]]
    if blanks:
        print("Blank fields:")
        for f in blanks:
            print(f"  {f['target']}.{f['field']}: {f['blank']}/{f['records']} blank ({f['blank_pct']}%)")
    print("Wrote: " + ", ".join(str(w) for w in written))


# ----------------------------------------------------------------------------
# Agent runner
# ----------------------------------------------------------------------------
def run_once(path, cfg, outdir, formats):
    rows = extract(path)
    if not rows:
        print(f"{path}: no rows, nothing to do")
        return
    missing = {"target", "page", "field", "value", "status"} - set(rows[0].keys())
    if missing:
        sys.exit(f"{path} does not look like scraper output (missing columns: {sorted(missing)})")
    result = transform(rows, cfg)
    report, written = load(result, path, outdir, formats)
    print_report(report, written)


def load_config(path):
    if not path:
        return {"targets": []}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"Cannot read config '{path}': {e}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="ETL agent that cleans scraper output.")
    ap.add_argument("input", nargs="?", help="scraper results .csv or .json")
    ap.add_argument("--config", help="etl config JSON (optional; sensible defaults without it)")
    ap.add_argument("--outdir", default="clean")
    ap.add_argument("--formats", default="csv,xlsx", help="csv always written; add xlsx")
    ap.add_argument("--watch", metavar="DIR", help="agent mode: process new/changed results files in DIR")
    ap.add_argument("--interval", type=int, default=30, help="seconds between checks in watch mode")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    formats = {f.strip().lower() for f in args.formats.split(",")}

    if args.watch:
        folder = Path(args.watch)
        state = {}
        print(f"Watching {folder} every {args.interval}s (Ctrl+C to stop)")
        try:
            while True:
                for p in sorted(list(folder.glob("*.csv")) + list(folder.glob("*.json"))):
                    m = p.stat().st_mtime
                    if state.get(p) != m:
                        state[p] = m
                        print(f"\n== Processing {p.name}")
                        run_once(p, cfg, Path(args.outdir) / p.stem, formats)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    if not args.input:
        ap.error("give an input file, or use --watch DIR")
    run_once(args.input, cfg, args.outdir, formats)


if __name__ == "__main__":
    main()
