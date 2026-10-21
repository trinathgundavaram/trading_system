# Reference data

* `sleeve_ref_returns.csv` - backtest daily returns of the configured sector-momentum sleeve (built by `scripts/build_sleeve_reference.py`; used by the drift monitor).
* `sp500_membership.csv` - S&P 500 membership intervals (ticker, start_date, end_date) from https://github.com/fja05680/sp500 (MIT licence), downloaded 2026-10-20. Used by `research/stock_data.py` for the stock-momentum test.
