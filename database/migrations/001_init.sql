-- 001_init.sql : tables for the Nepal Corporate Intelligence Platform
-- Safe to run more than once (IF NOT EXISTS).

-- One row per loader run, so every record can be traced to the run that loaded it.
CREATE TABLE IF NOT EXISTS etl_runs (
    run_id          BIGSERIAL PRIMARY KEY,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_dir      TEXT,
    note            TEXT
);

-- Cleaned company data: one snapshot per company per day (re-loading the same day updates it).
CREATE TABLE IF NOT EXISTS company_snapshot (
    id                  BIGSERIAL PRIMARY KEY,
    symbol              TEXT NOT NULL,
    company_name        TEXT NOT NULL,
    sector              TEXT,
    listed_shares       NUMERIC,
    paidup_value        NUMERIC,
    total_paidup_value  NUMERIC,
    market_price        NUMERIC,
    pct_change          NUMERIC,
    last_traded_on      DATE,
    week52_high         NUMERIC,
    week52_low          NUMERIC,
    avg_180_day         NUMERIC,
    avg_120_day         NUMERIC,
    yield_1_year_pct    NUMERIC,
    eps                 NUMERIC,
    eps_fiscal_year     TEXT,
    pe_ratio            NUMERIC,
    book_value          NUMERIC,
    pbv                 NUMERIC,
    avg_volume_30_day   NUMERIC,
    market_cap          NUMERIC,
    quality             TEXT,            -- complete / partial (from the ETL)
    issues              TEXT,            -- what was blank and why
    source_url          TEXT,            -- page the data came from
    record_id           TEXT,            -- links to etl_lineage
    scraped_date        DATE NOT NULL DEFAULT CURRENT_DATE,
    run_id              BIGINT REFERENCES etl_runs(run_id),
    UNIQUE (symbol, scraped_date)
);
CREATE INDEX IF NOT EXISTS idx_company_snapshot_symbol ON company_snapshot (symbol);

-- Where every value came from: source URL, HTML line/column and XPath.
CREATE TABLE IF NOT EXISTS etl_lineage (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT REFERENCES etl_runs(run_id),
    record_id     TEXT,
    target        TEXT,
    field         TEXT,
    raw_value     TEXT,
    clean_value   TEXT,
    status        TEXT,
    issue         TEXT,
    source_url    TEXT,
    html_line     INTEGER,
    html_column   INTEGER,
    xpath         TEXT,
    disposition   TEXT
);
CREATE INDEX IF NOT EXISTS idx_lineage_run_record ON etl_lineage (run_id, record_id);

-- Records the ETL refused to load, with the reason.
CREATE TABLE IF NOT EXISTS etl_rejects (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT REFERENCES etl_runs(run_id),
    record_id     TEXT,
    target        TEXT,
    page          INTEGER,
    item_index    INTEGER,
    source_url    TEXT,
    reason        TEXT,
    details       TEXT,
    raw_values    JSONB
);

-- Every value the cleaner changed (raw -> clean).
CREATE TABLE IF NOT EXISTS etl_changes (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT REFERENCES etl_runs(run_id),
    record_id     TEXT,
    target        TEXT,
    field         TEXT,
    raw_value     TEXT,
    clean_value   TEXT,
    source_url    TEXT,
    xpath         TEXT
);
