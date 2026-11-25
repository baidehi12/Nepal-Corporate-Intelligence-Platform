CREATE OR REPLACE VIEW v_company_ranking AS
SELECT DISTINCT ON (symbol)
       symbol, company_name, sector, market_price, eps,
       CASE WHEN eps > 0 THEN pe_ratio END AS pe_valid,
       pbv,
       round(dividend_pct * paidup_value / 100 / NULLIF(market_price, 0) * 100, 2) AS dividend_yield_pct,
       market_cap, scraped_date
FROM company_snapshot
WHERE quality <> 'inactive'
ORDER BY symbol, scraped_date DESC;