# Trayd MCP Integration Checklist

Complete these steps in order to integrate trayd-mcp into your trading platform.

---

## ✅ Phase 1: One-Time Manual Setup

### 1. Link Your Robinhood Account with Trayd
- [ ] Go to **https://mcp.trayd.ai** (or https://github.com/trayders/trayd-mcp#setup)
- [ ] Click "Connect Robinhood" / "Add Connector"
- [ ] Sign in with Google (if prompted)
- [ ] Authorize trayd to access your Robinhood account
- [ ] Approve the notification on your phone when prompted
- [ ] Confirm that you see "Connected" or similar

**Alternative (via Claude Desktop):**
- [ ] Open Claude Desktop
- [ ] Settings → Connectors → Add custom connector
- [ ] Name: `trayd` | URL: `https://mcp.trayd.ai/mcp`
- [ ] Click Add → Connect → sign in with Google
- [ ] Say: "Link my Robinhood account"

---

## ✅ Phase 2: Code Integration

### 2. Add Trayd MCP Client File
- [ ] Copy `trayd_mcp.py` to `mcp_clients/` directory
- [ ] Verify: `mcp_clients/trayd_mcp.py` exists

### 3. Update Environment Variables
- [ ] Open `.env` (or create from `.env.template`)
- [ ] Add: `TRAYD_MCP_URL=https://mcp.trayd.ai/mcp`
- [ ] (Optional) Add: `TRAYD_AUTH_TOKEN=` (leave blank if not needed)
- [ ] Save `.env`

**Verify:**
```bash
grep TRAYD_MCP_URL .env
# Should output: TRAYD_MCP_URL=https://mcp.trayd.ai/mcp
```

### 4. Update .env.template for Other Developers
- [ ] `.env.template` already updated with trayd config
- [ ] Commit to git for team visibility

### 5. Review Documentation
- [ ] Read `TRAYD_MCP_SETUP.md` (full integration guide)
- [ ] Skim `trayd_usage_examples.py` (code patterns)
- [ ] Check trayd README: https://github.com/trayders/trayd-mcp

---

## ✅ Phase 3: Testing

### 6. Test the Connection
Run this quick test to verify trayd is working:

```bash
python3 << 'EOF'
from mcp_clients.trayd_mcp import TraydMCP

trayd = TraydMCP()

if trayd.configured():
    print("✓ Trayd configured and ready")
    
    # Try fetching portfolio (read-only, safe)
    portfolio = trayd.get_portfolio()
    if portfolio:
        print(f"✓ Connected! Portfolio: {portfolio}")
    else:
        print("✗ Trayd not responding (may need to re-authenticate)")
else:
    print("✗ TRAYD_MCP_URL not in .env")
EOF
```

**Expected output:**
```
✓ Trayd configured and ready
✓ Connected! Portfolio: {'total_value': 12345.67, ...}
```

### 7. Test Read-Only Operations
```bash
python3 trayd_usage_examples.py
```

Should show:
- ✓ Trayd configured
- Portfolio value
- Open positions
- Current quotes
- Open orders

### 8. Test a Small Trade (Optional - Use Limit Orders)
For safety, place a limit order that won't fill immediately:

```python
from mcp_clients.trayd_mcp import TraydMCP

trayd = TraydMCP()

# This will NOT fill (price is way below current market)
result = trayd.place_order("BUY", "AAPL", quantity=1, limit_price=10.00)
print(f"Order placed (should not fill): {result}")

# Check it's visible in open orders
orders = trayd.get_orders()
print(f"Open orders: {len(orders)}")

# Cancel it
if result and 'order_id' in result:
    trayd.cancel_order(result['order_id'])
    print("Order cancelled")
```

---

## ✅ Phase 4: Integration into Your Platform

### 9. Integrate into Main/Scheduler Code

In your `scheduler.py` or `main.py`, add:

```python
from mcp_clients.trayd_mcp import TraydMCP

# Initialize once at startup
trayd = TraydMCP()

# Later in your trading logic:
if trayd.configured():
    # Get fresh account state before making decisions
    portfolio = trayd.get_portfolio()
    positions = trayd.get_positions()
    
    # Check if you can afford a trade
    buying_power = portfolio.get('buying_power', 0)
    
    # Place orders as needed
    if buying_power > 500:
        trayd.place_order("BUY", "AAPL", notional=500)
```

### 10. Reconciliation with Robinhood-MCP (Optional)
You can keep BOTH clients for:
- **robinhood-mcp**: Read-only account verification
- **trayd-mcp**: All trading execution

No conflicts - robinhood-mcp only reads, trayd executes writes.

```python
from mcp_clients.robinhood_mcp import RobinhoodMCP
from mcp_clients.trayd_mcp import TraydMCP

rh = RobinhoodMCP()  # Read-only
trayd = TraydMCP()    # Full trading

# Verify before trade
positions_rh = rh.get_positions()
portfolio_trayd = trayd.get_portfolio()
```

---

## ✅ Phase 5: Deployment/Production

### 11. Secure Your .env
- [ ] `.env` is in `.gitignore` (already is)
- [ ] Never commit `.env` to git
- [ ] Never share your `.env` file
- [ ] Keep `.env.template` updated for team onboarding

### 12. Error Handling in Production
Trayd includes automatic failover:
- Circuit breaker automatically opens if trayd is down
- All calls return empty dict/list instead of raising exceptions
- Your platform continues running (degrades gracefully)
- Platform won't hammer a dead service

Monitor in your logs:
```bash
grep "trayd:" logs/output.log
```

### 13. Monitoring (Optional)
Keep an eye on:
- Is trayd accessible? (test portfolio fetch weekly)
- Are orders executing as expected?
- Any authentication errors? (usually need to re-link account)

---

## ✅ Troubleshooting

| Problem | Solution |
|---------|----------|
| "TRAYD_MCP_URL not in .env" | Add `TRAYD_MCP_URL=https://mcp.trayd.ai/mcp` to `.env` |
| Connection refused / timeout | Check internet connection, trayd server status |
| Orders being rejected | Include all required params (symbol, quantity or notional, etc.) |
| "Authentication failed" | Re-link account at https://mcp.trayd.ai |
| Can't get portfolio | Check TRAYD_MCP_URL is correct and trayd server is responding |
| Slow responses | Normal if trayd is busy; circuit breaker will open after 3 failures |

---

## ✅ Quick Reference

### Most Common Operations

```python
from mcp_clients.trayd_mcp import TraydMCP
trayd = TraydMCP()

# Check portfolio
portfolio = trayd.get_portfolio()
print(f"Buying power: ${portfolio['buying_power']}")

# Get positions
positions = trayd.get_positions()
for pos in positions:
    print(f"{pos['symbol']}: {pos['quantity']} shares")

# Get quotes
quotes = trayd.get_quotes(['AAPL', 'TSLA'])

# Buy shares
trayd.place_order("BUY", "AAPL", quantity=10)

# Buy with limit price
trayd.place_order("BUY", "TSLA", quantity=5, limit_price=250)

# Sell
trayd.place_order("SELL", "MSFT", quantity=3)

# Cancel order
trayd.cancel_order(order_id)

# Ladder order
trayd.place_ladder_order("NVDA", "BUY", 5, 180, 170, quantity_each=10)
```

---

## ✅ Files Modified/Added

```
✓ TRAYD_MCP_SETUP.md                 (this file)
✓ TRAYD_INTEGRATION_CHECKLIST.md     (step-by-step checklist)
✓ mcp_clients/trayd_mcp.py           (MCP client wrapper)
✓ trayd_usage_examples.py            (code examples)
✓ .env.template                      (updated with trayd config)
✓ .env                               (add TRAYD_MCP_URL manually)
```

---

## ✅ Next Steps

1. **NOW**: Complete Phase 1 (link Robinhood account)
2. **NOW**: Complete Phase 2 (add code files + update .env)
3. **SOON**: Complete Phase 3 (run tests)
4. **LATER**: Integrate into scheduler.py/main.py
5. **DEPLOY**: Test in production with small trades first

---

## Questions?

- **Trayd Docs**: https://github.com/trayders/trayd-mcp
- **Trayd Support**: team@trayd.ai
- **Trayd Issues**: https://github.com/trayders/trayd-mcp/issues
