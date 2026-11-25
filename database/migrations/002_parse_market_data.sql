-- 002_parse_market_data.sql
-- Turns the raw Merolagani text in company_market_data.table_rows into proper columns in company_snapshot.
-- Safe to run more than once. After it exists, the loader calls refresh_company_snapshot() automatically.

ALTER TABLE company_snapshot ADD COLUMN IF NOT EXISTS dividend_pct          NUMERIC;
ALTER TABLE company_snapshot ADD COLUMN IF NOT EXISTS dividend_fiscal_year  TEXT;
ALTER TABLE company_snapshot ADD COLUMN IF NOT EXISTS bonus_pct             NUMERIC;
ALTER TABLE company_snapshot ADD COLUMN IF NOT EXISTS bonus_fiscal_year     TEXT;

-- Value that follows a label in text like "Market Price 550.10 | EPS 33.89 (FY:083-084, Q:1) | ..."
CREATE OR REPLACE FUNCTION mp_field(tr text, label text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT NULLIF(btrim(substring(tr from '(?:^|\| )' || label || ' ([^|]*)')), '')
$$;

-- First number in a text value: "1,234.56 (FY:..)" -> 1234.56 ; "0.02 %" -> 0.02 ; "-12.86" -> -12.86
CREATE OR REPLACE FUNCTION mp_num(v text) RETURNS numeric
LANGUAGE sql IMMUTABLE AS $$
  SELECT replace(substring(v from '^-?[0-9][0-9,]*(?:\.[0-9]+)?'), ',', '')::numeric
$$;

-- Number after the dash in a range: "581.00-485.00" -> 485.00
CREATE OR REPLACE FUNCTION mp_num_last(v text) RETURNS numeric
LANGUAGE sql IMMUTABLE AS $$
  SELECT replace(substring(v from '-([0-9][0-9,]*(?:\.[0-9]+)?)$'), ',', '')::numeric
$$;

-- "2026/11/23 11:10:58" -> date. The placeholder 1900/01/01 (never traded) and bad dates -> NULL.
CREATE OR REPLACE FUNCTION mp_date(v text) RETURNS date
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
  IF v ~ '^[0-9]{4}/[0-9]{2}/[0-9]{2}' AND substring(v, 1, 4) <> '1900' THEN
    RETURN to_date(substring(v, 1, 10), 'YYYY/MM/DD');
  END IF;
  RETURN NULL;
EXCEPTION WHEN others THEN
  RETURN NULL;
END $$;

-- Parses every company_market_data row (one per company per day) into company_snapshot.
-- Re-running updates the same (symbol, day) rows. Returns the number of rows written.
CREATE OR REPLACE FUNCTION refresh_company_snapshot() RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE n integer;
BEGIN
  WITH latest AS (
    SELECT DISTINCT ON (upper(substring(source_url from 'symbol=([A-Za-z0-9]+)')), scraped_date) *
    FROM company_market_data
    WHERE source_url ~ 'symbol=[A-Za-z0-9]+' AND table_rows IS NOT NULL
    ORDER BY upper(substring(source_url from 'symbol=([A-Za-z0-9]+)')), scraped_date, id DESC
  ),
  p AS (
    SELECT
      upper(substring(l.source_url from 'symbol=([A-Za-z0-9]+)'))                  AS symbol,
      coalesce(mp_field(l.table_rows, 'Company Name'), l.company)                  AS company_name,
      mp_field(l.table_rows, 'Sector')                                             AS sector,
      coalesce(mp_num(mp_field(l.table_rows, 'Listed Shares')),
               mp_num(mp_field(l.table_rows, 'Shares Outstanding')))               AS listed_shares,
      mp_num(mp_field(l.table_rows, 'Paidup Value'))                               AS paidup_value,
      mp_num(mp_field(l.table_rows, 'Total Paidup Value'))                         AS total_paidup_value,
      mp_num(mp_field(l.table_rows, 'Market Price'))                               AS market_price,
      mp_num(mp_field(l.table_rows, '% Change'))                                   AS pct_change,
      mp_date(mp_field(l.table_rows, 'Last Traded On'))                            AS last_traded_on,
      mp_num(mp_field(l.table_rows, '52 Weeks High - Low'))                        AS week52_high,
      mp_num_last(mp_field(l.table_rows, '52 Weeks High - Low'))                   AS week52_low,
      mp_num(mp_field(l.table_rows, '180 Day Average'))                            AS avg_180_day,
      mp_num(mp_field(l.table_rows, '120 Day Average'))                            AS avg_120_day,
      mp_num(mp_field(l.table_rows, '1 Year Yield'))                               AS yield_1_year_pct,
      mp_num(mp_field(l.table_rows, 'EPS'))                                        AS eps,
      substring(mp_field(l.table_rows, 'EPS') from 'FY:\s*([^,)]+)')               AS eps_fiscal_year,
      mp_num(mp_field(l.table_rows, 'P/E Ratio'))                                  AS pe_ratio,
      mp_num(mp_field(l.table_rows, 'Book Value'))                                 AS book_value,
      mp_num(mp_field(l.table_rows, 'PBV'))                                        AS pbv,
      mp_num(mp_field(l.table_rows, '30-Day Avg Volume'))                          AS avg_volume_30_day,
      mp_num(mp_field(l.table_rows, 'Market Capitalization'))                      AS market_cap,
      mp_num(mp_field(l.table_rows, '% Dividend'))                                 AS dividend_pct,
      substring(mp_field(l.table_rows, '% Dividend') from 'FY:\s*([^,)]+)')        AS dividend_fiscal_year,
      mp_num(mp_field(l.table_rows, '% Bonus'))                                    AS bonus_pct,
      substring(mp_field(l.table_rows, '% Bonus') from 'FY:\s*([^,)]+)')           AS bonus_fiscal_year,
      l.source_url, l.record_id, l.scraped_date, l.run_id
    FROM latest l
  ),
  q AS (
    SELECT p.*,
      concat_ws(', ',
        CASE WHEN market_price  IS NULL THEN 'market_price'  END,
        CASE WHEN eps           IS NULL THEN 'eps'           END,
        CASE WHEN book_value    IS NULL THEN 'book_value'    END,
        CASE WHEN listed_shares IS NULL THEN 'listed_shares' END) AS missing
    FROM p
  ),
  up AS (
    INSERT INTO company_snapshot (
      symbol, company_name, sector, listed_shares, paidup_value, total_paidup_value, market_price, pct_change,
      last_traded_on, week52_high, week52_low, avg_180_day, avg_120_day, yield_1_year_pct, eps, eps_fiscal_year,
      pe_ratio, book_value, pbv, avg_volume_30_day, market_cap, dividend_pct, dividend_fiscal_year, bonus_pct,
      bonus_fiscal_year, quality, issues, source_url, record_id, scraped_date, run_id)
    SELECT
      symbol, company_name, sector, listed_shares, paidup_value, total_paidup_value, market_price, pct_change,
      last_traded_on, week52_high, week52_low, avg_180_day, avg_120_day, yield_1_year_pct, eps, eps_fiscal_year,
      pe_ratio, book_value, pbv, avg_volume_30_day, market_cap, dividend_pct, dividend_fiscal_year, bonus_pct,
      bonus_fiscal_year,
      CASE WHEN market_price = 0 OR last_traded_on IS NULL THEN 'inactive'      -- never traded / delisted
           WHEN missing = '' THEN 'complete' ELSE 'partial' END,
      CASE WHEN market_price = 0 OR last_traded_on IS NULL THEN 'no trading data (price 0 or no last-traded date)'
           WHEN missing <> '' THEN 'missing: ' || missing END,
      source_url, record_id, scraped_date, run_id
    FROM q
    ON CONFLICT (symbol, scraped_date) DO UPDATE SET
      company_name = EXCLUDED.company_name, sector = EXCLUDED.sector, listed_shares = EXCLUDED.listed_shares,
      paidup_value = EXCLUDED.paidup_value, total_paidup_value = EXCLUDED.total_paidup_value,
      market_price = EXCLUDED.market_price, pct_change = EXCLUDED.pct_change, last_traded_on = EXCLUDED.last_traded_on,
      week52_high = EXCLUDED.week52_high, week52_low = EXCLUDED.week52_low, avg_180_day = EXCLUDED.avg_180_day,
      avg_120_day = EXCLUDED.avg_120_day, yield_1_year_pct = EXCLUDED.yield_1_year_pct, eps = EXCLUDED.eps,
      eps_fiscal_year = EXCLUDED.eps_fiscal_year, pe_ratio = EXCLUDED.pe_ratio, book_value = EXCLUDED.book_value,
      pbv = EXCLUDED.pbv, avg_volume_30_day = EXCLUDED.avg_volume_30_day, market_cap = EXCLUDED.market_cap,
      dividend_pct = EXCLUDED.dividend_pct, dividend_fiscal_year = EXCLUDED.dividend_fiscal_year,
      bonus_pct = EXCLUDED.bonus_pct, bonus_fiscal_year = EXCLUDED.bonus_fiscal_year, quality = EXCLUDED.quality,
      issues = EXCLUDED.issues, source_url = EXCLUDED.source_url, record_id = EXCLUDED.record_id,
      run_id = EXCLUDED.run_id
    RETURNING 1
  )
  SELECT count(*) INTO n FROM up;
  RETURN n;
END $$;
