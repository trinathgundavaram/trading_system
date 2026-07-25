# Trayd-MCP Integration - Quick Start (5 Minutes)

**What is trayd?** A hosted MCP that lets you trade Robinhood via natural language or code (buy, sell, limit orders, shorts, etc.)

**Time needed:** ~5 minutes setup + 5 minutes testing

---

## Step 1: Link Your Robinhood Account (2 min)

Go to **https://mcp.trayd.ai** and click "Connect Robinhood" (or "Add Connector")

1. Sign in with Google
2. Approve notification on your phone
3. Confirm "Connected"

✅ Done

---

## Step 2: Update .env (1 min)

Open `.env` and add ONE line:

```
TRAYD_MCP_URL=https://mcp.trayd.ai/mcp
```

✅ Done

---

## Step 3: Copy trayd_mcp.py (30 sec)

The file `mcp_clients/trayd_mcp.py` is already in your project.

✅ Done

---

## Step 4: Test It (2 min)

Run this test:

```bash
python3 << 'EOF'
from mcp_clients.trayd_mcp import TraydMCP

trayd = TraydMCP()
portfolio = trayd.get_portfolio()
print(f"✓ Connected! Portfolio: ${portfolio.get('total_value', 'N/A')}")
EOF
```

Should print: `✓ Connected! Portfolio: $12345.67` (or your actual value)

✅ Done

---

## Step 5: Try a Trade (Optional, 2 min)

```python
from mcp_clients.trayd_mcp import TraydMCP

trayd = TraydMCP()

# BUY 10 shares of AAPL
trayd.place_order("BUY", "AAPL", quantity=10)

# BUY at a limit price (safer for testing)
trayd.place_order("BUY", "TSLA", quantity=1, limit_price=100)  # Won't fill

# SELL 5 shares of MSFT
trayd.place_order("SELL", "MSFT", quantity=5)

# Check open orders
orders = trayd.get_orders()
print(f"Open orders: {len(orders)}")
```

✅ You're trading!

---

## Key Methods

```python
from mcp_clients.trayd_mcp import TraydMCP
trayd = TraydMCP()

# READ ACCOUNT
trayd.get_portfolio()           # Total value, buying power, etc
trayd.get_positions()           # All holdings
trayd.get_quotes(['AAPL'])     # Real-time prices
trayd.get_orders()              # Open orders

# TRADE
trayd.place_order("BUY", "AAPL", quantity=10)
trayd.place_order("SELL", "TSLA", quantity=5, limit_price=250)
trayd.place_ladder_order("NVDA", "BUY", 5, 180, 170, quantity_each=10)

# CANCEL
trayd.cancel_order(order_id)
trayd.cancel_all_orders()
```

---

## Common Patterns

### Buy stocks
```python
# At market price
trayd.place_order("BUY", "AAPL", quantity=10)

# With limit price
trayd.place_order("BUY", "TSLA", quantity=5, limit_price=250)

# By dollar amount
trayd.place_order("BUY", "MSFT", notional=1000)  # $1000 worth
```

### Sell stocks
```python
trayd.place_order("SELL", "AAPL", quantity=10)
trayd.place_order("SELL", "TSLA", quantity=5, limit_price=300)
```

### Ladder orders (advanced)
```python
# Buy NVDA in 5 tranches from $180 down to $170
trayd.place_ladder_order(
    symbol="NVDA",
    action="BUY",
    num_orders=5,
    start_price=180,
    end_price=170,
    quantity_each=10
)
```

### Short sell
```python
trayd.place_order("SHORT", "GME", quantity=10)
```

### Check if stock is shortable
```python
info = trayd.check_short_availability("GME")
if info.get('available'):
    print(f"Borrow rate: {info['borrow_rate']}%")
```

---

## Troubleshooting

**Connection refused?**
- Check internet connection
- Verify `TRAYD_MCP_URL=https://mcp.trayd.ai/mcp` in `.env`

**Authentication failed?**
- Re-link your account at https://mcp.trayd.ai

**Order rejected?**
- Make sure you have required params (symbol, quantity or notional)
- Check you have enough buying power

**Slow responses?**
- Trayd may be rate-limited (auto-recovery in 5 min)

---

## Files Added/Updated

```
✅ mcp_clients/trayd_mcp.py                 (ready to use)
✅ TRAYD_QUICK_START.md                     (this file)
✅ TRAYD_MCP_SETUP.md                       (full docs)
✅ TRAYD_INTEGRATION_CHECKLIST.md           (step-by-step)
✅ trayd_usage_examples.py                  (code patterns)
✅ .env.template                            (config reference)
```

---

## What's Next?

- **Now:** Test with small trades (use limit orders to be safe)
- **Soon:** Integrate into your scheduler/main code
- **Later:** Build trading logic on top of trayd

Example integration:
```python
# In scheduler.py or main.py
from mcp_clients.trayd_mcp import TraydMCP

trayd = TraydMCP()

# Your trading logic
portfolio = trayd.get_portfolio()
positions = trayd.get_positions()

if needs_to_buy:
    trayd.place_order("BUY", symbol, quantity=qty)
```

---

## Learn More

- **Full Setup Guide:** `TRAYD_MCP_SETUP.md`
- **Code Examples:** `trayd_usage_examples.py`
- **Step-by-Step:** `TRAYD_INTEGRATION_CHECKLIST.md`
- **Trayd Repo:** https://github.com/trayders/trayd-mcp
- **Support:** team@trayd.ai

---

**Status:** ✅ You're ready to trade!
