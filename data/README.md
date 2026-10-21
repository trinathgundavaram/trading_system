`sp500_membership.csv` - S&P 500 membership intervals (ticker, start_date, end_date; blank end = current member),
from https://github.com/fja05680/sp500 (MIT licence), downloaded 2026-10-20. Used by the stock-momentum research
(research/stock_data.py) and by the live stock sleeve's universe (rules/stock_momentum.py).
Refresh periodically (index changes a few names every quarter): re-download the file from the repo above.
