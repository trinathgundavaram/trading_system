# Trayd-MCP Integration Guide

## What is Trayd-MCP?

**Trayd** is a hosted MCP server that enables **trading** on Robinhood through natural language. Unlike the read-only `robinhood-mcp` your platform currently uses, trayd provides:

- **Portfolio Analysis** - "What's my portfolio worth?" / "Which positions are up today?"
- **Real-time Quotes** - Works 24/7, even after market hours
- **Buy & Sell** - "Buy 10 shares of AAPL" / "Sell my TSLA position"
- **Limit Orders** - "Place a limit order for TSLA at $400"
- **Ladder Orders** - "Set 5 ladder buys for NVDA from $180 to $175"
- **Short Selling** - "Short 10 shares of GME"
- **Batch Orders** - "Buy $500 worth of each: AAPL, GOOGL, MSFT"
- **Cancel Orders** - "Cancel all my open orders"
- **Multi-account Support** - Manage multiple Robinhood accounts from one connection

**Key Advantage Over Local Robinhood-MCP:**
- Trayd is HOSTED (no local install needed)
- Includes **TRADING** capabilities (not just read-only)
- Works via HTTP (remote server at `https://mcp.trayd.ai/mcp`)
- Already handles authentication with Robinhood OAuth
- No API keys to manage - just Robinhood login

---

## Integration Steps

### **Step 1: Update requirements.txt** (if not already present)

The `mcp>=1.0.0` dependency is already in your requirements - no changes needed. The integration uses the existing MCP SDK.

### **Step 2: Manual Setup - Link Your Robinhood Account with Trayd**

Before the code can work, you need to authenticate with trayd and authorize it to access your Robinhood account.

**Option A: Quick Setup via Web (Recommended)**
1. Open: **https://mcp.trayd.ai** (or follow any link in their docs)
2. Click **"Connect Robinhood"** or similar
3. Sign in with your Google account
4. Approve the notification on your phone when prompted
5. You'll be given a **session token** or confirmation - note it

**Option B: Via Claude Desktop (Alternative)**
1. Open Claude Desktop
2. Go to **Settings** > **Connectors** > **Add custom connector**
3. Name: `trayd` | URL: `https://mcp.trayd.ai/mcp`
4. Click **Add** > **Connect** > sign in with Google
5. Say: *"Link my Robinhood account"*
6. Approve notification on your phone

### **Step 3: Add Trayd MCP Client to Your Project**

Copy the included `trayd_mcp.py` file to your `mcp_clients/` directory:

```bash
cp trayd_mcp.py mcp_clients/trayd_mcp.py
```

This wrapper follows the same pattern as your existing `robinhood_mcp.py`:
- Spawns the trayd server via HTTP
- Handles failures gracefully
- Includes circuit breaker protection
- Caches results appropriately

### **Step 4: Update .env Configuration**

Add trayd settings to your `.env` file:

```bash
# --- Trayd MCP (optional) - trading capabilities ---
# HTTP endpoint for the trayd-mcp server
TRAYD_MCP_URL=https://mcp.trayd.ai/mcp
# Optional: if trayd requires an API key or auth token in the future
TRAYD_AUTH_TOKEN=
```

If you're running trayd locally (for development), you can change the URL to `http://localhost:5000` (adjust port as needed).

### **Step 5: Update .env.template** 

Add to your `.env.template` for other developers:

```bash
# --- Trayd MCP (optional) - trading capabilities ---
# Remote HTTP MCP server for Robinhood trading (not just read-only)
# Handles: buying, selling, limit orders, ladder orders, shorts, batch orders
# Setup: https://github.com/trayders/trayd-mcp#setup
TRAYD_MCP_URL=https://mcp.trayd.ai/mcp
TRAYD_AUTH_TOKEN=
```

### **Step 6: Integrate into Your Main Code**

In your `main.py` or scheduler, import and use the trayd client:

```python
from mcp_clients.trayd_mcp import TraydMCP

# Initialize the client
trayd = TraydMCP()

# Check if configured
if trayd.configured():
    # Get portfolio info
    portfolio = trayd.get_portfolio()
    
    # Get positions
    positions = trayd.get_positions()
    
    # Place an order (example)
    result = trayd.place_order("BUY", "AAPL", quantity=10)
```

See `trayd_mcp.py` for full method documentation.

---

## Available Methods

### Account Data (Read)
- `get_portfolio()` - Total value, equity, buying power, day change
- `get_positions()` - All holdings with current value and P&L
- `get_quotes(tickers)` - Real-time price data (list of tickers)
- `get_orders()` - All open orders

### Order Execution (Write)
- `place_order(action, symbol, quantity, limit_price=None, order_type="immediate")` 
  - `action`: "BUY" or "SELL"
  - `order_type`: "immediate", "limit", "market"
- `place_ladder_order(symbol, action, num_orders, start_price, end_price, quantity_each)`
- `cancel_order(order_id)`
- `cancel_all_orders()`

### Advanced Features
- `get_account_list()` - List all Robinhood accounts
- `switch_account(account_id)` - Change active account
- `check_short_availability(symbol)` - Can you short this stock?

---

## Configuration Options

### Using a Local Trayd Instance (Development)

If you want to run trayd locally instead of using the hosted version:

```bash
# Install trayd (requires Node.js)
npm install -g trayd-mcp

# Run locally
trayd-mcp --port 5000
```

Then update `.env`:
```
TRAYD_MCP_URL=http://localhost:5000
```

### With API Authentication

If trayd requires authentication tokens:

```bash
# Set in .env
TRAYD_AUTH_TOKEN=your-token-here
```

The `trayd_mcp.py` wrapper will automatically include this in all requests.

---

## Testing the Integration

Once set up, test that everything works:

```python
from mcp_clients.trayd_mcp import TraydMCP

trayd = TraydMCP()

if trayd.configured():
    print("✓ Trayd configured and connected")
    
    portfolio = trayd.get_portfolio()
    print(f"Portfolio value: ${portfolio.get('total_value', 'N/A')}")
    
    positions = trayd.get_positions()
    print(f"Open positions: {len(positions)}")
else:
    print("✗ Trayd not configured - check TRAYD_MCP_URL in .env")
```

---

## Comparison: Robinhood-MCP vs Trayd-MCP

| Feature | robinhood-mcp (Local) | trayd-mcp (Remote) |
|---------|------|-------|
| Installation | Local (uvx) | Hosted |
| Authentication | Read-only session | OAuth 2.1 |
| Order Execution | ❌ None (read-only) | ✅ Full trading |
| Portfolio Queries | ✅ Yes | ✅ Yes |
| Real-time Quotes | ✅ Yes | ✅ Yes (24/7) |
| Setup Complexity | Medium | Low |
| Network | Local stdio | HTTP |

**Recommendation:** Keep both in your platform:
- **robinhood-mcp** for read-only account reconciliation (already working)
- **trayd-mcp** for trading execution (new)

They don't conflict - robinhood-mcp only reads, trayd-mcp handles all writes.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Connection refused" | Check TRAYD_MCP_URL is correct and internet connection is active |
| "Authentication failed" | Re-run the setup step (link account via trayd website/Claude) |
| "Order rejected" | Make sure to include limit_price for limit orders |
| "Account not found" | Use `get_account_list()` to verify account exists, then `switch_account()` |
| Slow responses | Check circuit breaker status - trayd may be rate-limited (will auto-recover) |

---

## Security Considerations

✅ **Safe:**
- Credentials are handled by trayd's OAuth flow - your platform never stores Robinhood passwords
- HTTPS-only communication with trayd servers
- No local API keys to accidentally commit

⚠️ **Best Practices:**
- Keep `.env` in `.gitignore` (it already is)
- Don't share your trayd auth token if one is issued
- Test with limit orders that won't fill before running live trades

---

## Deprecation Note

The `robinhood-mcp` (read-only, local) wrapper can remain for:
- Portfolio reconciliation
- Cost-basis tracking
- Account verification before trades

But for all **trading actions**, use trayd. Your platform already prevents local order execution via `confirm_fill.py` - trayd just moves that execution to a production-grade hosted MCP instead of requiring manual Claude Desktop intervention.

---

## Next Steps

1. ✅ Update `.env` with `TRAYD_MCP_URL=https://mcp.trayd.ai/mcp`
2. ✅ Add `trayd_mcp.py` to `mcp_clients/`
3. ✅ Link your Robinhood account at https://mcp.trayd.ai
4. ✅ Test with: `python -c "from mcp_clients.trayd_mcp import TraydMCP; trayd = TraydMCP(); print('Connected!' if trayd.configured() else 'Not configured')"`
5. ✅ Integrate calls into your main scheduler/engine

---

## Support

- **Trayd Docs:** https://github.com/trayders/trayd-mcp
- **Trayd Issues:** https://github.com/trayders/trayd-mcp/issues
- **Trayd Support Email:** team@trayd.ai
