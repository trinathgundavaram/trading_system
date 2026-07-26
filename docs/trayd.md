# Trayd MCP

Consolidates four root-level documents that had drifted into near-duplicates
(2026-07-26): `TRAYD_MCP_SETUP.md`, `TRAYD_QUICK_START.md`,
`TRAYD_INTEGRATION_CHECKLIST.md` and `TRAYD_SETUP_SUMMARY.md` — 1,041 lines
between them, all four walking through the same three steps (link the account,
set `TRAYD_MCP_URL`, copy the client) in four different voices, written while
the integration was being built on 2026-07-15. The integration has since
landed, so instructions written as a build plan now describe work already done.
Git history has the originals if you want the older wording.

## Status: integrated, and NOT on any live path

`mcp_clients/trayd_mcp.py` exists and `TRAYD_MCP_URL` is set in `.env`. Nothing
in the scan loop, the order path or the UI calls it. Read that as the default
posture rather than as an oversight — Trayd can place orders, and this
platform's only sanctioned order path is `engine/live_trader.py` behind the
gates in `is_live_mode()` (master switch + validation receipt + EXECUTE +
auto_trade). Wiring Trayd into an automated path would create a second way to
trade that none of those gates cover.

## What it is

A **hosted** MCP server that trades Robinhood over HTTP at
`https://mcp.trayd.ai/mcp`. Distinct from `mcp_clients/robinhood_mcp.py`, which
is local and read-only by construction.

| | `robinhood_mcp.py` | `trayd_mcp.py` |
|---|---|---|
| Location | local subprocess | hosted, HTTP |
| Can place orders | no — server exposes no trading tools | **yes** |
| Auth | `ROBINHOOD_USERNAME`/`PASSWORD` in `.env` | Robinhood OAuth via Trayd |
| Used by the platform today | yes — `robinhood_sync.py`, account reads | no |

Capabilities: portfolio and positions, quotes (including outside market
hours), market/limit/ladder/batch orders, short selling, order cancellation,
and multiple Robinhood accounts on one connection.

## One-time manual setup

The account link cannot be automated — it is an OAuth consent flow, and it
requires approving a push notification on your phone.

1. Open <https://mcp.trayd.ai> and choose **Connect Robinhood**.
2. Sign in with Google.
3. Approve the prompt on your phone.

Alternatively, add it in Claude Desktop under **Settings → Connectors → Add
custom connector** (name `trayd`, URL `https://mcp.trayd.ai/mcp`), connect, and
ask it to link your Robinhood account.

Then confirm `.env` has:

```
TRAYD_MCP_URL=https://mcp.trayd.ai/mcp
```

No API key. `mcp>=1.0.0` is already in `requirements.txt`; nothing else to
install.

## Usage

```python
from mcp_clients.trayd_mcp import TraydMCP
trayd = TraydMCP()

# Reads
trayd.get_portfolio()                # total value, buying power
trayd.get_positions()                # holdings
trayd.get_quotes(["AAPL"])           # works after hours
trayd.get_orders()                   # open orders

# Orders
trayd.place_order("BUY", "AAPL", quantity=10)
trayd.place_order("SELL", "TSLA", quantity=5, limit_price=250)
trayd.place_ladder_order("NVDA", "BUY", 5, 180, 170, quantity_each=10)

# Cancel
trayd.cancel_order(order_id)
trayd.cancel_all_orders()
```

Worked examples: `trayd_usage_examples.py`.

## Testing it without risking a fill

Use a limit order that cannot execute, rather than a small market order:

```python
trayd.place_order("BUY", "AAPL", quantity=1, limit_price=1.00)   # will not fill
trayd.cancel_all_orders()
```

A "small" market order is still a real trade at whatever the book gives you. A
limit far from the market proves the path end to end and fills nothing.

## Security

Trayd holds the Robinhood OAuth grant, so this repo never stores a Robinhood
password for it and there is no API key to leak. Communication is HTTPS only,
and `.env` is gitignored.

The thing to keep in view is not credential handling but authority: a linked
Trayd session can place orders on your account, and that authority lives with
Trayd's session rather than behind this platform's gates. Revoke it at
<https://mcp.trayd.ai> if you stop using it. Check open orders after any test.
