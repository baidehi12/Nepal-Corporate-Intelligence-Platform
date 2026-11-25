#!/usr/bin/env python3
"""
load_postgres.py - loads the ETL output into PostgreSQL (no pip install needed).

It talks to PostgreSQL through `psql`, by default inside your Docker container (nci_postgres).

FILE NAMES -> TABLES
  clean_<Company_Name>_<kind>.csv  ->  table <kind>, with a `company` column
  where <kind> is one of the nine kinds below, so all companies share one table per kind:

      company_profiles   company_market_data   company_share_prices   company_news
      company_board      company_management    company_staff          company_locations
      financials

  lineage.csv -> etl_lineage     rejects.csv -> etl_rejects     changes.csv -> etl_changes
  Any other clean_<name>.csv -> table <name> (must already exist, e.g. company_snapshot)

TABLES ARE BUILT FROM THE CSV HEADERS
  - A kind table that does not exist is created from the CSV columns.
  - New columns appearing later are added automatically (ALTER TABLE ADD COLUMN).
  - Column types are inferred from ALL the values: boolean, date (YYYY-MM-DD), numeric, else text.
    (page and item_index are bigint; codes with leading zeros stay text.)
  - If a later file does not fit an existing column's type, that kind is skipped with a clear message.

RE-RUNNING
  Loading the same company again on the same day replaces that day's rows (no duplicates).
  Other days are kept, so you build up history.

Usage (project root, Docker running):
    python etl/load/load_postgres.py data/processed/company_scraper --migrate database/migrations --user Baidehi --db nepse_intel
    python etl/load/load_postgres.py data/processed/company_scraper --user Baidehi --db nepse_intel
    python etl/load/load_postgres.py data/processed/company_scraper --dry-run
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import subprocess
import sys
from collections import OrderedDict, defaultdict
from datetime import date
from pathlib import Path

KINDS = ["company_board", "company_locations", "company_management", "company_market_data",
         "company_news", "company_profiles", "company_share_prices", "company_staff", "financials"]
AUDIT_FILES = {"lineage.csv": "etl_lineage", "rejects.csv": "etl_rejects", "changes.csv": "etl_changes"}
DROP_COLUMNS = {"target"}                                  # ETL bookkeeping, not stored in kind tables
RESERVED = {"id", "company", "run_id", "scraped_date"}     # columns the loader owns
INTEGER_COLS = {"page", "item_index"}
CONFLICT_KEYS = {"company_snapshot": ("symbol", "scraped_date")}   # legacy single-snapshot table
SKIP_COLUMNS = {"id", "run_id", "scraped_date"}
QUIET_COLUMNS = {"target", "page", "item_index"}
TEXT_TYPES = {"text", "character varying", "character"}
NUMERIC_TYPES = {"numeric", "integer", "bigint", "smallint", "double precision", "real"}

INT_RE = re.compile(r"^-?(0|[1-9]\d{0,17})$")
NUM_RE = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?$")
NUM_LOOSE = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PsqlError(Exception):
    pass


# ----------------------------------------------------------------------------
# psql access
# ----------------------------------------------------------------------------
def read_env(path):
    env = {}
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Psql:
    def __init__(self, args, env):
        self.user = args.user or env.get("POSTGRES_USER", "postgres")
        self.db = args.db or env.get("POSTGRES_DB", "postgres")
        base = ["-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-U", self.user, "-d", self.db]
        if args.mode == "docker":
            self.cmd = ["docker", "exec", "-i", args.container, "psql"] + base
        else:
            self.cmd = ["psql", "-h", args.host, "-p", str(args.port)] + base
        self.run_env = dict(os.environ, PGPASSWORD=env.get("POSTGRES_PASSWORD", ""), PGCLIENTENCODING="UTF8")
        self.dry = args.dry_run

    def run(self, sql):
        if self.dry:
            print("-- DRY RUN SQL --\n" + sql + "\n")
            return ""
        try:
            r = subprocess.run(self.cmd, input=sql, text=True, encoding="utf-8",
                               capture_output=True, env=self.run_env)
        except FileNotFoundError as e:
            sys.exit(f"Cannot run '{self.cmd[0]}': {e}. Is Docker (or psql) installed and on PATH?")
        if r.returncode != 0:
            raise PsqlError((r.stderr or r.stdout).strip())
        return r.stdout.strip()


def qi(name):
    return '"' + name.replace('"', '""') + '"'


def sql_str(s):
    return "'" + str(s).replace("'", "''") + "'"


def table_columns(db, table):
    out = db.run("SELECT column_name || '|' || data_type FROM information_schema.columns "
                 f"WHERE table_schema='public' AND table_name={sql_str(table)} ORDER BY ordinal_position;")
    cols = OrderedDict()
    for line in out.splitlines():
        if "|" in line:
            n, t = line.split("|", 1)
            cols[n] = t
    return cols


# ----------------------------------------------------------------------------
# Column names and type inference
# ----------------------------------------------------------------------------
def sanitize(name, used):
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip()).strip("_").lower()[:55] or "col"
    if s[0].isdigit():
        s = "c_" + s
    if s in RESERVED:
        s += "_src"
    base, i = s, 2
    while s in used:
        s = f"{base}_{i}"
        i += 1
    used.add(s)
    return s


def valid_date(v):
    try:
        date.fromisoformat(v)
        return True
    except ValueError:
        return False


def infer_type(col, values):
    vals = [v for v in values if v != ""]
    if not vals:
        return "text"
    if col in INTEGER_COLS and all(INT_RE.match(v) for v in vals):
        return "bigint"
    if all(v.lower() in ("true", "false") for v in vals):
        return "boolean"
    if all(DATE_RE.match(v) and valid_date(v) for v in vals):
        return "date"
    if all(NUM_RE.match(v) for v in vals):
        return "numeric"
    return "text"


def fits(pg_type, v):
    if v == "":
        return True
    if pg_type in TEXT_TYPES:
        return True
    if pg_type == "bigint" or pg_type in ("integer", "smallint"):
        return bool(re.match(r"^-?\d+$", v))
    if pg_type in NUMERIC_TYPES:
        return bool(NUM_LOOSE.match(v))
    if pg_type == "date":
        return bool(DATE_RE.match(v)) and valid_date(v)
    if pg_type == "boolean":
        return v.lower() in ("true", "false", "t", "f", "yes", "no", "1", "0")
    return True          # unknown type: let PostgreSQL decide


def cast_expr(col, pg_type):
    if pg_type in TEXT_TYPES:
        return qi(col)
    return f"{qi(col)}::{pg_type}"


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def csv_text(cols, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") for c in cols])
    return buf.getvalue()


# ----------------------------------------------------------------------------
# Kind tables (one table per kind, all companies together)
# ----------------------------------------------------------------------------
def split_name(stem):
    """clean_<Company>_<kind> -> (company, kind) or (None, None)."""
    name = stem[len("clean_"):]
    for kind in KINDS:
        if name.endswith("_" + kind) and len(name) > len(kind) + 1:
            return name[: -len(kind) - 1].replace("_", " "), kind
    return None, None


def load_kind(db, kind, items, run_id):
    """items: list of (company, path). Returns (rows_loaded, companies, new_columns, note)."""
    used, colmap = set(), OrderedDict()           # raw header -> clean column name
    combined, empty = [], []
    for company, path in items:
        header, rows = read_csv(path)
        if not rows:
            empty.append(company)
            continue
        for h in header:
            if h not in DROP_COLUMNS and h not in colmap:
                colmap[h] = sanitize(h, used)
        for r in rows:
            row = {"company": company}
            for h, c in colmap.items():
                row[c] = r.get(h, "")
            combined.append(row)
    if not combined:
        return 0, 0, [], "no rows in any file"

    data_cols = list(colmap.values())
    existing = table_columns(db, kind) if not db.dry else OrderedDict()
    types, bad = {}, []
    for c in data_cols:
        values = [r.get(c, "") for r in combined]
        if c in existing:
            types[c] = existing[c]
            offenders = [v for v in values if not fits(existing[c], v)]
            if offenders:
                bad.append(f"column '{c}' is {existing[c]} in the database but the files contain "
                           f"{offenders[0]!r}" + (f" (+{len(offenders) - 1} more)" if len(offenders) > 1 else ""))
        else:
            types[c] = infer_type(c, values)
    if bad:
        return 0, 0, [], "SKIPPED: " + "; ".join(bad)

    new_cols = [c for c in data_cols if c not in existing]
    ddl = []
    if not existing:
        col_defs = ["id BIGSERIAL PRIMARY KEY", "company TEXT NOT NULL"] + [f"{qi(c)} {types[c]}" for c in data_cols] + \
                   ["scraped_date DATE NOT NULL DEFAULT CURRENT_DATE", "run_id BIGINT REFERENCES etl_runs(run_id)"]
        ddl.append(f"CREATE TABLE {qi(kind)} ({', '.join(col_defs)});")
    else:
        for c in new_cols:
            ddl.append(f"ALTER TABLE {qi(kind)} ADD COLUMN {qi(c)} {types[c]};")
    ddl.append(f"CREATE INDEX IF NOT EXISTS {qi('idx_' + kind + '_company')} ON {qi(kind)} (company, scraped_date);")

    stg_cols = ["company"] + data_cols
    sql = (
        "BEGIN;\n" + "\n".join(ddl) + "\n"
        f"CREATE TEMP TABLE _stg ({', '.join(qi(c) + ' TEXT' for c in stg_cols)}) ON COMMIT DROP;\n"
        f"COPY _stg ({', '.join(qi(c) for c in stg_cols)}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL '');\n"
        f"{csv_text(stg_cols, combined)}\\.\n"
        f"DELETE FROM {qi(kind)} WHERE scraped_date = CURRENT_DATE AND company IN (SELECT DISTINCT company FROM _stg);\n"
        f"WITH ins AS (INSERT INTO {qi(kind)} (company, {', '.join(qi(c) for c in data_cols)}, run_id) "
        f"SELECT company, {', '.join(cast_expr(c, types[c]) for c in data_cols)}, {int(run_id)} FROM _stg RETURNING 1) "
        "SELECT count(*) FROM ins;\n"
        "COMMIT;\n")
    out = db.run(sql)
    last = out.splitlines()[-1] if out else ""
    n = int(last) if last.isdigit() else len(combined)
    note = ("new table" if not existing else (f"added columns: {', '.join(new_cols)}" if new_cols else ""))
    if empty:
        note += ("; " if note else "") + f"empty files: {', '.join(empty)}"
    return n, len({r['company'] for r in combined}), new_cols, note


# ----------------------------------------------------------------------------
# Audit tables and other pre-existing tables (created by the migration)
# ----------------------------------------------------------------------------
def load_existing_table(db, csv_path, table, run_id):
    header, rows = read_csv(csv_path)
    if not rows:
        return 0, "no rows"
    types = table_columns(db, table) if not db.dry else {h: "text" for h in header}
    if not types:
        return 0, f"SKIPPED: table '{table}' does not exist"
    cols = [c for c in header if c in types and c not in SKIP_COLUMNS]
    ignored = [c for c in header if c not in types and c not in SKIP_COLUMNS and c not in QUIET_COLUMNS]
    if not cols:
        return 0, f"SKIPPED: no CSV column matches a column of table '{table}'"
    bad = [c for c in cols if not all(fits(types[c], r.get(c, "")) for r in rows)]
    if bad:
        return 0, f"SKIPPED: values in {', '.join(bad)} do not fit the column types"
    ins_cols, sel = [qi(c) for c in cols], [cast_expr(c, types[c]) for c in cols]
    has_run = "run_id" in types
    if has_run:
        ins_cols.append("run_id")
        sel.append(str(int(run_id)))
    conflict = ""
    key = CONFLICT_KEYS.get(table)
    if key and all(k in types for k in key):
        upd = [c for c in cols if c not in key] + (["run_id"] if has_run else [])
        conflict = (f" ON CONFLICT ({', '.join(qi(k) for k in key)}) DO UPDATE SET "
                    + ", ".join(f"{qi(c)} = EXCLUDED.{qi(c)}" for c in upd))
    sql = (
        "BEGIN;\n"
        f"CREATE TEMP TABLE _stg ({', '.join(qi(c) + ' TEXT' for c in cols)}) ON COMMIT DROP;\n"
        f"COPY _stg ({', '.join(qi(c) for c in cols)}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL '');\n"
        f"{csv_text(cols, rows)}\\.\n"
        f"WITH ins AS (INSERT INTO {qi(table)} ({', '.join(ins_cols)}) SELECT {', '.join(sel)} FROM _stg{conflict} "
        "RETURNING 1) SELECT count(*) FROM ins;\nCOMMIT;\n")
    out = db.run(sql)
    last = out.splitlines()[-1] if out else ""
    return (int(last) if last.isdigit() else len(rows)), (f"ignored columns: {', '.join(ignored)}" if ignored else "")


# ----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Load ETL output into PostgreSQL via psql.")
    ap.add_argument("folder", help="ETL output folder (clean_*.csv, lineage.csv, rejects.csv, changes.csv)")
    ap.add_argument("--env", default="infrastructure/docker/.env", help="path to the .env with POSTGRES_* values")
    ap.add_argument("--mode", choices=["docker", "host"], default="docker")
    ap.add_argument("--container", default="nci_postgres")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=5432)
    ap.add_argument("--user")
    ap.add_argument("--db")
    ap.add_argument("--migrate", metavar="DIR", help="run all .sql files in DIR first (safe to repeat)")
    ap.add_argument("--no-audit", action="store_true", help="skip lineage/rejects/changes tables")
    ap.add_argument("--dry-run", action="store_true", help="print the SQL instead of running it")
    args = ap.parse_args(argv)

    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"Folder not found: {folder}")
    env = read_env(args.env)
    if not env and not (args.user and args.db):
        print(f"Note: no values found in {args.env}; pass --user and --db.")
    db = Psql(args, env)

    try:
        if args.migrate:
            for f in sorted(Path(args.migrate).glob("*.sql")):
                print(f"Applying {f.name}")
                db.run(f.read_text(encoding="utf-8"))
        run_out = db.run(f"INSERT INTO etl_runs (source_dir) VALUES ({sql_str(str(folder))}) RETURNING run_id;")
    except PsqlError as e:
        hint = "\nHint: run once with --migrate database/migrations to create the base tables." \
            if "etl_runs" in str(e) else ""
        sys.exit(f"PostgreSQL error:\n{e}{hint}")
    run_id = int(run_out.splitlines()[0]) if run_out else 0
    print(f"Run id: {run_id}")

    by_kind, others = defaultdict(list), []
    for f in sorted(folder.glob("clean_*.csv")):
        company, kind = split_name(f.stem)
        if kind:
            by_kind[kind].append((company, f))
        else:
            others.append((f, f.stem[len("clean_"):]))
    audit = [] if args.no_audit else [(folder / n, t) for n, t in AUDIT_FILES.items() if (folder / n).exists()]
    if not by_kind and not others and not audit:
        sys.exit(f"No clean_*.csv files in {folder}")

    print("\nLoaded:")
    problems = 0
    for kind in KINDS:
        if kind not in by_kind:
            continue
        try:
            n, ncomp, _, note = load_kind(db, kind, by_kind[kind], run_id)
        except PsqlError as e:
            n, ncomp, note = 0, 0, "FAILED: " + str(e).splitlines()[0]
        problems += note.startswith(("SKIPPED", "FAILED"))
        print(f"  {kind:22} {len(by_kind[kind]):>3} files, {ncomp:>2} companies -> {n:>6} rows  {note}")
    for f, table in others + audit:
        try:
            n, note = load_existing_table(db, f, table, run_id)
        except PsqlError as e:
            n, note = 0, "FAILED: " + str(e).splitlines()[0]
        problems += note.startswith(("SKIPPED", "FAILED"))
        print(f"  {table:22} {f.name:34} {n:>6} rows  {note}")
    if "company_market_data" in by_kind:
        try:
            n = db.run("SELECT refresh_company_snapshot();")
            print(f"  {'company_snapshot':22} parsed from company_market_data -> {n or '?':>6} rows")
        except PsqlError as e:
            problems += 1
            msg = str(e).splitlines()[0]
            hint = " Run once with --migrate database/migrations to create the parser." \
                if "refresh_company_snapshot" in msg else ""
            print(f"  {'company_snapshot':22} SKIPPED: {msg}.{hint}")
    if problems:
        print(f"\n{problems} problem(s) above; the other tables were loaded.")


if __name__ == "__main__":
    main()
