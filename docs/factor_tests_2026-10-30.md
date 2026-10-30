# Factor tests on S&P 500 stocks (2026-10-30)

`research/stock_factors.py` - top 40, equal weight, monthly, 5 bps, point-in-time membership. Free prices cover only
~63% of historical members (the missing ones are mostly the losers), so absolute returns are optimistic; the fair
comparison is each factor against the equal-weight universe of the same eligible stocks.

| Factor (no SPY rule) | Full CAGR | Max drawdown | Sharpe | vs universe (annualised) |
|---|---|---|---|---|
| 3/6/12m composite momentum | 11.1% | -52% | 0.66 | +0.4% |
| 12-1 momentum | 9.9% | -53% | 0.59 | -0.7% |
| Low 63-day volatility | 9.9% | -33% | 0.86 | -1.8% |
| 1-month reversal | 8.0% | -70% | 0.44 | -1.0% |
| Near 52-week high | 6.6% | -46% | 0.55 | -4.7% |
| 12-1 momentum + low vol | 9.9% | -39% | 0.79 | -1.6% |
| Equal-weight universe | 11.0% | -53% | 0.69 | - |
| SPY | 11.0% | -51% | 0.76 | - |

With the SPY 200-day risk-off rule (cash 0%): composite momentum 12.1% / -19.6% / Sharpe 0.81; low vol 8.7% / -9.6% / 0.91;
12-1 momentum + low vol 9.8% / -9.7% / 0.93.

Reading it: no price-only factor beats the equal-weight universe by a meaningful margin, so most of the stock sleeve's return
is market exposure plus the SPY rule's drawdown protection. Low volatility trades ~2 points of return for about half the
drawdown; it is the more efficient base for leverage. Value and quality need point-in-time fundamentals and were not tested.
`stock_momentum_sleeve.selection: mom_lowvol` switches the live paper sleeve to that selection (default stays `momentum`).
