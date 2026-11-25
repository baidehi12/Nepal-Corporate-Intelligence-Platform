
# Nepal Corporate Intelligence Platform

## Project Title

Development of an Intelligent Multi-Source Corporate Information Aggregation, Analysis and Visualization Platform for Nepalese Listed Companies.

## About the Project

This project aims to develop an intelligent platform for collecting, aggregating, processing, analysing, and visualizing corporate information about Nepalese listed companies.

The platform uses web scraping and ETL pipelines to collect information from multiple sources, transform raw data into structured datasets, and generate meaningful corporate and financial insights.

## Main Modules

- Web Scraping and Data Collection
- ETL Pipeline
- Database Management
- Corporate and Financial Analytics
- Backend API
- Dashboard and Data Visualization

## System Workflow

Information Sources
↓
Web Scraping
↓
Raw Data Storage
↓
ETL Processing
↓
Central Database
↓
Data Analytics
↓
Backend API
↓
Interactive Dashboard

# Nepal Corporate Intelligence Platform

A data pipeline that collects public information about Nepali listed companies, cleans it, stores it in
PostgreSQL, and turns it into comparable metrics (price, EPS, P/E, price-to-book, dividend yield).

```
Websites -> scraper -> raw files -> ETL (clean + validate) -> clean files -> loader -> PostgreSQL
                                                                                    |
                                                          parser (SQL) -> company_snapshot -> v_company_ranking
```

Every value stays traceable: the pipeline records the **source URL, HTML line and XPath** of each scraped
value (`etl_lineage`), every value the cleaner changed (`etl_changes`) and every record it refused (`etl_rejects`).
Nothing is invented: a value that cannot be pulled or fails validation is left blank and flagged.

## Status

| Part | Status |
|---|---|
| Scraping (11 companies, 9 data categories) | Done |
| ETL cleaning, validation, rejects, lineage | Done |
| PostgreSQL + pgAdmin in Docker, 9 data tables + audit tables | Done |
| Structured market-data parser and ranking view | Done |
| Tests (unit + database integration) | Done for the loader, ETL rules and parser |
| Analytics, API, dashboard, scheduler, CI | Planned |

## Requirements

- Python 3.9+ and a virtual environment (`python -m venv .venv`)
- Docker Desktop (PostgreSQL and pgAdmin run as containers; nothing else to install)

## Setup

1. Create `infrastructure/docker/.env` (never commit this file; it is in `.gitignore`):

   ```
   POSTGRES_USER=your_user
   POSTGRES_PASSWORD=choose-a-password
   POSTGRES_DB=nepse_intel
   PGADMIN_EMAIL=you@example.com
   PGADMIN_PASSWORD=choose-another-password
   ```

2. Start the database and pgAdmin:

   ```powershell
   cd infrastructure\docker
   docker compose up -d
   cd ..\..
   ```

   pgAdmin is at http://localhost:5050. Register a server with host `postgres`, port `5432`, and the
   user and password from `.env`. (Use `localhost:5432` from your own computer.)

## Run order

1. **Scrape:** `<TODO: add the command you use to run the scraper>`
2. **Clean (ETL):** `<TODO: add the command you use to run the ETL>` (writes `data/processed/<run>/clean_*.csv`,
   `rejects.csv`, `changes.csv`, `lineage.csv`)
3. **Load into PostgreSQL** (first time, add `--migrate` to create the tables):

   ```powershell
   python etl\load\load_postgres.py data\processed\company_scraper --user <POSTGRES_USER> --db nepse_intel --migrate database\migrations
   ```

   Later runs: the same command without `--migrate`. Re-loading on the same day replaces that day's rows;
   other days are kept as history. Use `--dry-run` to print the SQL without running it.
4. **Look at the results** in pgAdmin:

   ```sql
   SELECT * FROM v_company_ranking ORDER BY pe_valid NULLS LAST;
   ```

## Database

| Table | Contents |
|---|---|
| `company_profiles`, `company_market_data`, `company_share_prices`, `company_news`, `company_board`, `company_management`, `company_staff`, `company_locations`, `financials` | One table per data category, all companies together (`company` column). Columns are created from the scraped files |
| `company_snapshot` | Structured market data parsed from `company_market_data`: one row per company per day (price, EPS, P/E, book value, market cap, dividends and more). `quality` is `complete`, `partial` or `inactive` (no trading) |
| `v_company_ranking` | Latest snapshot per active company with valid P/E only and dividend yield on market price |
| `etl_runs`, `etl_lineage`, `etl_rejects`, `etl_changes` | Audit trail: which run loaded what, where each value came from, what was refused or changed |

Migrations are in `database/migrations/` and are safe to run more than once.

## Tests

From the project root (no extra packages needed):

```powershell
python -m unittest discover -s tests\unit -v          # loader helpers and ETL cleaning rules
python -m unittest discover -s tests\integration -v   # SQL parser and ranking view (needs Docker database running)
```

The integration test inserts rows inside a transaction and rolls it back, so real data is never changed.
Tests are skipped, not failed, when the database or `etl.py` is not available.

## Known limitations

- Three categories (`company_market_data`, `company_news`, `company_share_prices`) currently come from the
  same Merolagani company page for companies listed there. Only the market data is parsed into columns.
- Some staff and financials pages point at pages without that content. These need corrected source URLs.
- The scraper reads static HTML only; pages built by JavaScript need a headless browser.
- `dividend_pct` is a percentage of paid-up value; the ranking view converts it to a yield on market price.
- Values are a snapshot of what each site showed on the scrape date. This is not investment advice.

## Responsible scraping

The scraper respects `robots.txt`, waits between requests and identifies itself. Check each site's terms
of service before scraping it.

## Project structure

```
scraper/          scraper code and source configs
etl/              cleaning (ETL) and load/ (PostgreSQL loader)
database/         migrations/ (SQL tables, parser, ranking view)
infrastructure/   docker/ (PostgreSQL + pgAdmin)
config/           ETL rules
data/             raw/, staging/, processed/ (not committed)
tests/            unit/ and integration/
analytics/ api/ dashboard/ docs/   planned or in progress
```

