#!/usr/bin/env python3
"""
load_postgres.py - loads the ETL output into PostgreSQL.

No pip install needed: it talks to PostgreSQL through `psql`, by default inside your
Docker container (nci_postgres), so your passwords never leave Docker.

Loads from an ETL output folder:
    clean_<target>.csv  -> table <target>   (e.g. clean_company_snapshot.csv -> company_snapshot)
    lineage.csv         -> etl_lineage
    rejects.csv         -> etl_rejects
    changes.csv         -> etl_changes
Every row is stamped with the run_id of this load (table etl_runs).

Usage (from the project root, Docker running):
    python etl/load/load_postgres.py data/processed/mero --migrate database/migrations
    python etl/load/load_postgres.py data/processed/mero            # later runs
    python etl/load/load_postgres.py data/processed/mero --dry-run  # print SQL only

Re-running on the same day updates company_snapshot instead of duplicating it.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import subprocess
import sys
from pathlib import Path

# Tables that must not get duplicate rows: conflict columns for ON CONFLICT ... DO UPDATE
CONFLICT_KEYS = {"company_snapshot": ("symbol", "scraped_date")}
# Columns the loader fills itself or that live only in the database
SKIP_COLUMNS = {"id", "run_id", "scraped_date"}
QUIET_COLUMNS = {"target", "page", "item_index"}   # ETL bookkeeping columns, not stored in clean tables
AUDIT_FILES = {"lineage.csv": "etl_lineage", "rejects.csv": "etl_rejects", "changes.csv": "etl_changes"}
TEXT_TYPES = {"text", "character varying", "character"}


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
        self.password = env.get("POSTGRES_PASSWORD", "")
        base = ["-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-U", self.user, "-d", self.db]
        if args.mode == "docker":
            self.cmd = ["docker", "exec", "-i", args.container, "psql"] + base
        else:
            self.cmd = ["psql", "-h", args.host, "-p", str(args.port)] + base
        self.run_env = dict(os.environ, PGPASSWORD=self.password, PGCLIENTENCODING="UTF8")
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
            sys.exit(f"PostgreSQL error:\n{r.stderr.strip() or r.stdout.strip()}")
        return r.stdout.strip()


def quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def sql_str(s):
    return "'" + str(s).replace("'", "''") + "'"


def table_columns(db, table):
    out = db.run(
        "SELECT column_name || '|' || data_type FROM information_schema.columns "
        f"WHERE table_schema='public' AND table_name={sql_str(table)} ORDER BY ordinal_position;")
    cols = {}
    for line in out.splitlines():
        if "|" in line:
            n, t = line.split("|", 1)
            cols[n] = t
    return cols


def load_csv(db, csv_path, table, run_id):
    """Returns (rows_loaded, note)."""
    types = table_columns(db, table) if not db.dry else None
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        rows = list(reader)
    if not rows:
        return 0, "no rows"
    if types is None:   # dry run: assume everything is text
        types = {h: "text" for h in header}
    if not types:
        return 0, f"SKIPPED: table '{table}' does not exist (create it in database/migrations first)"

    cols = [c for c in header if c in types and c not in SKIP_COLUMNS]
    ignored = [c for c in header if c not in types and c not in SKIP_COLUMNS and c not in QUIET_COLUMNS]
    if not cols:
        return 0, f"SKIPPED: no CSV column matches a column of table '{table}'"

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") for c in cols])
    csv_text = buf.getvalue()

    stg_cols = ", ".join(f"{quote_ident(c)} TEXT" for c in cols)
    sel = []
    for c in cols:
        t = types[c]
        sel.append(quote_ident(c) if t in TEXT_TYPES else f"{quote_ident(c)}::{t}")
    ins_cols = [quote_ident(c) for c in cols]
    has_run = "run_id" in types
    if has_run:
        ins_cols.append("run_id")
        sel.append(str(int(run_id)))

    conflict = ""
    key = CONFLICT_KEYS.get(table)
    if key and all(k in types for k in key):
        upd = [c for c in cols if c not in key]
        sets = ", ".join(f"{quote_ident(c)} = EXCLUDED.{quote_ident(c)}" for c in upd + (["run_id"] if has_run else []))
        conflict = f" ON CONFLICT ({', '.join(quote_ident(k) for k in key)}) DO UPDATE SET {sets}"

    sql = (
        "BEGIN;\n"
        f"CREATE TEMP TABLE _stg ({stg_cols}) ON COMMIT DROP;\n"
        f"COPY _stg ({', '.join(quote_ident(c) for c in cols)}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL '');\n"
        f"{csv_text}\\.\n"
        f"WITH ins AS (INSERT INTO {quote_ident(table)} ({', '.join(ins_cols)}) "
        f"SELECT {', '.join(sel)} FROM _stg{conflict} RETURNING 1) SELECT count(*) FROM ins;\n"
        "COMMIT;\n")
    out = db.run(sql)
    n = int(out.splitlines()[-1]) if out and out.splitlines()[-1].isdigit() else len(rows)
    note = f"ignored CSV columns not in table: {', '.join(ignored)}" if ignored else ""
    return n, note


def main(argv=None):
    ap = argparse.ArgumentParser(description="Load ETL output into PostgreSQL via psql.")
    ap.add_argument("folder", help="ETL output folder (contains clean_*.csv, lineage.csv, rejects.csv, changes.csv)")
    ap.add_argument("--env", default="infrastructure/docker/.env", help="path to the .env with POSTGRES_* values")
    ap.add_argument("--mode", choices=["docker", "host"], default="docker",
                    help="docker: psql inside the container (default); host: psql installed on this computer")
    ap.add_argument("--container", default="nci_postgres")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=5432)
    ap.add_argument("--user"); ap.add_argument("--db")
    ap.add_argument("--migrate", metavar="DIR", help="run all .sql files in DIR first (safe to repeat)")
    ap.add_argument("--no-audit", action="store_true", help="skip lineage/rejects/changes tables")
    ap.add_argument("--dry-run", action="store_true", help="print the SQL instead of running it")
    args = ap.parse_args(argv)

    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"Folder not found: {folder}")
    env = read_env(args.env)
    if not env and not (args.user and args.db):
        print(f"Note: no values found in {args.env}; using --user/--db or defaults.")
    db = Psql(args, env)

    if args.migrate:
        for f in sorted(Path(args.migrate).glob("*.sql")):
            print(f"Applying {f.name}")
            db.run(f.read_text(encoding="utf-8"))

    clean_files = sorted(folder.glob("clean_*.csv"))
    audit_files = [] if args.no_audit else [(folder / n, t) for n, t in AUDIT_FILES.items() if (folder / n).exists()]
    if not clean_files and not audit_files:
        sys.exit(f"No clean_*.csv files in {folder}")

    run_id = db.run(f"INSERT INTO etl_runs (source_dir) VALUES ({sql_str(str(folder))}) RETURNING run_id;") if not args.dry_run else 0
    run_id = int(run_id.splitlines()[0]) if isinstance(run_id, str) and run_id else 0
    print(f"Run id: {run_id}")

    summary = []
    for f in clean_files:
        table = f.stem[len("clean_"):]
        n, note = load_csv(db, f, table, run_id)
        summary.append((f.name, table, n, note))
    for f, table in audit_files:
        n, note = load_csv(db, f, table, run_id)
        summary.append((f.name, table, n, note))

    print("\nLoaded:")
    for fname, table, n, note in summary:
        print(f"  {fname:34} -> {table:18} {n:>6} rows  {note}")
    skipped = [s for s in summary if s[3].startswith("SKIPPED")]
    if skipped:
        print(f"\n{len(skipped)} file(s) skipped (see notes above).")


if __name__ == "__main__":
    main()
