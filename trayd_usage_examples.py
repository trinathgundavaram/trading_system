"""
Trayd MCP Usage Examples
========================

This file demonstrates how to integrate trayd-mcp into your trading platform.
Copy/adapt these examples into your main.py, scheduler.py, or engine code.

Setup: See TRAYD_MCP_SETUP.md first (link Robinhood account, update .env)
"""

from mcp_clients.trayd_mcp import TraydMCP

# Initialize the client (reads TRAYD_MCP_URL + TRAYD_AUTH_TOKEN from .env)
trayd = TraydMCP()


# ==============================================================================
# 1. CHECK CONFIGURATION
# ==============================================================================

def check_setup():
    """Verify trayd is properly configured."""
    if not trayd.configured():
        print("⚠️  Trayd not configured - TRAYD_MCP_URL missing from .env")
        print("   See TRAYD_MCP_SETUP.md for setup instructions")
        return False
    print("✓ Trayd configured")
    return True


# ==============================================================================
# 2. GET ACCOUNT DATA (READ-ONLY)
# ==============================================================================

def get_portfolio_overview():
    """Get your total portfolio value, buying power, day's P&L."""
    portfolio = trayd.get_portfolio()
    if portfolio:
        print(f"Portfolio Value: ${portfolio.get('total_value', 'N/A')}")
        print(f"Buying Power: ${portfolio.get('buying_power', 'N/A')}")
        print(f"Day Change: {portfolio.get('day_change_pct', 'N/A')}%")
        return portfolio
    else:
        print("Could not fetch portfolio (trayd unavailable)")
        return None


def list_positions():
    """Get all current holdings."""
    positions = trayd.get_positions()
    if positions:
        print(f"Open positions: {len(positions)}")
        for pos in positions:
            symbol = pos.get('symbol', 'N/A')
            qty = pos.get('quantity', 0)
            current_val = pos.get('current_value', 0)
            pnl = pos.get('pnl', 0)
            print(f"  {symbol}: {qty} shares @ ${current_val} (P&L: ${pnl})")
        return positions
    else:
        print("No positions or unable to fetch (trayd unavailable)")
        return []


def check_quotes(symbols: list):
    """Get real-time prices for multiple symbols."""
    quotes = trayd.get_quotes(symbols)
    if quotes:
        for symbol, quote in quotes.items():
            price = quote.get('price', 'N/A')
            change = quote.get('change_pct', 'N/A')
            print(f"{symbol}: ${price} ({change}%)")
        return quotes
    else:
        print("Could not fetch quotes")
        return None


def list_open_orders():
    """Show all pending orders."""
    orders = trayd.get_orders()
    if orders:
        print(f"Open orders: {len(orders)}")
        for order in orders:
            symbol = order.get('symbol', 'N/A')
            action = order.get('action', 'N/A')
            qty = order.get('quantity', 0)
            price = order.get('price', 'N/A')
            status = order.get('status', 'N/A')
            print(f"  {action} {qty} {symbol} @ ${price} ({status})")
        return orders
    else:
        print("No open orders")
        return []


def list_accounts():
    """Get all Robinhood accounts linked to your trayd session."""
    accounts = trayd.get_account_list()
    if accounts:
        for acc in accounts:
            acc_id = acc.get('id', 'N/A')
            acc_type = acc.get('type', 'N/A')
            print(f"  Account {acc_id} ({acc_type})")
        return accounts
    else:
        print("No accounts found")
        return []


# ==============================================================================
# 3. PLACE ORDERS (TRADING)
# ==============================================================================

def buy_shares(symbol: str, quantity: int):
    """Buy a specific number of shares at market price."""
    result = trayd.place_order("BUY", symbol, quantity=quantity)
    if result:
        order_id = result.get('order_id', 'N/A')
        print(f"✓ BUY order placed: {order_id}")
        print(f"  {quantity} shares of {symbol}")
        return result
    else:
        print(f"✗ Failed to place BUY order for {symbol}")
        return None


def buy_with_limit(symbol: str, quantity: int, limit_price: float):
    """Buy shares at a limit price."""
    result = trayd.place_order(
        "BUY",
        symbol,
        quantity=quantity,
        limit_price=limit_price
    )
    if result:
        order_id = result.get('order_id', 'N/A')
        print(f"✓ LIMIT BUY order placed: {order_id}")
        print(f"  {quantity} shares of {symbol} @ ${limit_price} (extended trading)")
        return result
    else:
        print(f"✗ Failed to place limit order")
        return None


def buy_notional(symbol: str, dollar_amount: float):
    """Buy shares up to a specific dollar amount (instead of quantity)."""
    result = trayd.place_order("BUY", symbol, notional=dollar_amount)
    if result:
        order_id = result.get('order_id', 'N/A')
        print(f"✓ BUY order placed: {order_id}")
        print(f"  ${dollar_amount} worth of {symbol}")
        return result
    else:
        print(f"✗ Failed to place order")
        return None


def sell_shares(symbol: str, quantity: int):
    """Sell a specific number of shares."""
    result = trayd.place_order("SELL", symbol, quantity=quantity)
    if result:
        order_id = result.get('order_id', 'N/A')
        print(f"✓ SELL order placed: {order_id}")
        print(f"  {quantity} shares of {symbol}")
        return result
    else:
        print(f"✗ Failed to place SELL order")
        return None


def sell_with_limit(symbol: str, quantity: int, limit_price: float):
    """Sell shares at a limit price."""
    result = trayd.place_order(
        "SELL",
        symbol,
        quantity=quantity,
        limit_price=limit_price
    )
    if result:
        order_id = result.get('order_id', 'N/A')
        print(f"✓ LIMIT SELL order placed: {order_id}")
        print(f"  {quantity} shares of {symbol} @ ${limit_price}")
        return result
    else:
        print(f"✗ Failed to place limit order")
        return None


def batch_buy(purchases: dict):
    """Buy multiple symbols in one go.

    Args:
        purchases: Dict like {'AAPL': 10, 'TSLA': 5, 'MSFT': 8}
                   (symbol -> quantity)
    """
    results = {}
    for symbol, quantity in purchases.items():
        result = trayd.place_order("BUY", symbol, quantity=quantity)
        results[symbol] = result
        if result:
            print(f"✓ {symbol}: {quantity} shares")
        else:
            print(f"✗ {symbol}: failed")
    return results


# ==============================================================================
# 4. ADVANCED: LADDER ORDERS
# ==============================================================================

def place_ladder_order_example():
    """Set up a ladder of buy orders at decreasing prices.

    Example: "Buy NVDA in 5 tranches from $180 down to $170"
    """
    result = trayd.place_ladder_order(
        symbol="NVDA",
        action="BUY",
        num_orders=5,
        start_price=180,
        end_price=170,
        quantity_each=10  # 10 shares per order = 50 total
    )
    if result:
        print(f"✓ Ladder order placed")
        print(f"  5 buy orders for NVDA: $180 → $170 (10 shares each)")
        return result
    else:
        print("✗ Failed to place ladder order")
        return None


def place_dollar_ladder():
    """Set up ladder orders using dollar amounts instead of shares.

    Example: "Buy $100 worth of TSLA at each of 5 price points"
    """
    result = trayd.place_ladder_order(
        symbol="TSLA",
        action="BUY",
        num_orders=5,
        start_price=300,
        end_price=280,
        notional_each=100  # $100 per order
    )
    if result:
        print("✓ Ladder order placed")
        return result
    else:
        print("✗ Failed to place ladder order")
        return None


# ==============================================================================
# 5. CANCEL ORDERS
# ==============================================================================

def cancel_order_by_id(order_id: str):
    """Cancel a specific open order."""
    result = trayd.cancel_order(order_id)
    if result:
        print(f"✓ Order {order_id} cancelled")
        return result
    else:
        print(f"✗ Failed to cancel order {order_id}")
        return None


def cancel_all_pending():
    """Cancel all open orders at once."""
    result = trayd.cancel_all_orders()
    if result:
        cancelled_count = result.get('cancelled_count', 'N/A')
        print(f"✓ Cancelled {cancelled_count} orders")
        return result
    else:
        print("✗ Failed to cancel orders")
        return None


# ==============================================================================
# 6. ADVANCED: NATURAL LANGUAGE
# ==============================================================================

def execute_natural_language_trading(instruction: str):
    """Let trayd parse and execute a natural language instruction.

    Trayd can understand complex instructions like:
    - "Buy 10 shares of AAPL"
    - "Set 5 ladder buys for NVDA from $180 to $175"
    - "Sell my entire TSLA position"
    - "Buy $500 worth each of AAPL, GOOGL, MSFT"
    """
    result = trayd.execute_natural_language(instruction)
    if result:
        print(f"✓ Executed: {instruction}")
        print(f"  Result: {result}")
        return result
    else:
        print(f"✗ Failed to execute instruction")
        return None


# ==============================================================================
# 7. SHORT SELLING
# ==============================================================================

def short_sell(symbol: str, quantity: int):
    """Short a stock (borrow and sell)."""
    # First check if it's shortable
    availability = trayd.check_short_availability(symbol)
    if availability and availability.get('available'):
        borrow_rate = availability.get('borrow_rate', 'N/A')
        print(f"✓ {symbol} is shortable (borrow rate: {borrow_rate}%)")

        # Place short order
        result = trayd.place_order("SHORT", symbol, quantity=quantity)
        if result:
            order_id = result.get('order_id', 'N/A')
            print(f"✓ SHORT order placed: {order_id}")
            print(f"  {quantity} shares of {symbol}")
            return result
    else:
        print(f"✗ {symbol} cannot be shorted or check failed")
    return None


# ==============================================================================
# 8. MULTI-ACCOUNT SUPPORT
# ==============================================================================

def switch_account(account_id: str):
    """Switch to a different Robinhood account."""
    result = trayd.switch_account(account_id)
    if result:
        print(f"✓ Switched to account {account_id}")
        return result
    else:
        print(f"✗ Failed to switch to account {account_id}")
        return None


# ==============================================================================
# MAIN: INTEGRATION EXAMPLE
# ==============================================================================

if __name__ == "__main__":
    # Step 1: Check if trayd is configured
    if not check_setup():
        exit(1)

    print("\n" + "=" * 70)
    print("READ-ONLY EXAMPLES (Account Data)")
    print("=" * 70)

    # Get account overview
    print("\nPortfolio Overview:")
    get_portfolio_overview()

    # List holdings
    print("\nCurrent Holdings:")
    list_positions()

    # Check quotes
    print("\nPrice Check (AAPL, TSLA, NVDA):")
    check_quotes(["AAPL", "TSLA", "NVDA"])

    # List open orders
    print("\nOpen Orders:")
    list_open_orders()

    # List accounts
    print("\nAvailable Accounts:")
    list_accounts()

    print("\n" + "=" * 70)
    print("TRADING EXAMPLES (Place Orders)")
    print("=" * 70)
    print("\n⚠️  These are examples - uncomment to actually trade")

    # Example: buy_shares("AAPL", 10)
    # Example: buy_with_limit("TSLA", 5, 250.00)
    # Example: batch_buy({"AAPL": 10, "MSFT": 5, "GOOGL": 2})
    # Example: place_ladder_order_example()
    # Example: sell_shares("AAPL", 5)
    # Example: execute_natural_language_trading("Buy 10 shares of AAPL at market")

    print("\n✓ Integration examples complete - see code for more patterns")
