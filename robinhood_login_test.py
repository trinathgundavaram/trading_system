#!/usr/bin/env python3
"""One-time INTERACTIVE Robinhood login tester / session warmer (2026-07-17).

Why this exists: `uvx robinhood-mcp` runs headless, so when its login fails it
can only say "Login returned empty result" - the actual reason from Robinhood
(wrong credentials / account locked / 2FA challenge / API change) is swallowed.
This script runs the SAME robin_stocks login in your terminal, where you can
see the real response and answer interactive prompts (SMS code, app approval).

The payoff: a successful login here caches the session in
~/.tokens/robinhood.pickle - the SAME cache robinhood-mcp and live_trader use.
Warm it once here and every headless consumer works without re-authenticating.

Usage:
    python3 robinhood_login_test.py

Requires: pip install robin_stocks pyotp  (pyotp only if you use an
authenticator app and have ROBINHOOD_TOTP_SECRET set).
"""
import os
import sys

# Same minimal .env loader the platform uses.
from mcp_clients.market_data import _load_dotenv

_load_dotenv()


def main() -> int:
    user = os.getenv("ROBINHOOD_USERNAME", "").strip()
    pw = os.getenv("ROBINHOOD_PASSWORD", "").strip()
    totp_secret = os.getenv("ROBINHOOD_TOTP_SECRET", "").strip()

    if not user or not pw:
        print("ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD not set in .env")
        return 1

    print(f"Username: {user[:3]}***{user[-6:]} ({len(user)} chars)")
    if "@" not in user:
        print("  WARNING: Robinhood login expects the account EMAIL address - "
              "this doesn't look like one.")
    print(f"Password: {'*' * 8} ({len(pw)} chars)")
    totp_note = "set" if totp_secret else \
        "not set (fine unless you use an authenticator app for 2FA)"
    print(f"TOTP secret: {totp_note}")

    pickle_path = os.path.expanduser("~/.tokens/robinhood.pickle")
    print(f"Session cache: {pickle_path} "
          f"({'EXISTS' if os.path.isfile(pickle_path) else 'not present'})")

    mfa_code = None
    if totp_secret:
        try:
            import pyotp
            mfa_code = pyotp.TOTP(totp_secret).now()
            print(f"Generated TOTP code: {mfa_code}")
        except Exception as e:
            print(f"TOTP generation FAILED: {e}")
            print("  (secret must be base32: letters A-Z and digits 2-7 only; "
                  "remove it from .env entirely if you don't use an authenticator app)")
            return 1

    try:
        import robin_stocks.robinhood as rh
    except ImportError:
        print("robin_stocks not installed - run: pip install robin_stocks")
        return 1

    print("\nAttempting login (answer any prompt that appears - SMS code or "
          "approve in the Robinhood app)...\n")
    try:
        kwargs = {"store_session": True}
        if mfa_code:
            kwargs["mfa_code"] = mfa_code
        result = rh.login(user, pw, **kwargs)
    except Exception as e:
        print(f"\nLOGIN RAISED: {type(e).__name__}: {e}")
        print("\nMost common causes, in order:")
        print("  1. Wrong ROBINHOOD_USERNAME - must be the exact email you log "
              "into Robinhood with")
        print("  2. Wrong password (special chars in .env are fine - no quoting needed)")
        print("  3. Authenticator-app 2FA enabled but no ROBINHOOD_TOTP_SECRET in .env")
        print("  4. Account locked/flagged - try logging into the Robinhood app first")
        return 1

    if not result:
        print("\nLogin returned EMPTY - same failure the MCP sees. Robinhood "
              "rejected the attempt silently (usually credentials or a flagged "
              "device). Log into robinhood.com in a browser first, then retry.")
        return 1

    print("\nLOGIN OK - raw response keys:", sorted(result.keys())
          if isinstance(result, dict) else type(result).__name__)
    try:
        profile = rh.profiles.load_account_profile() or {}
        print(f"Account check: buying_power=${float(profile.get('buying_power') or 0):,.2f}")
    except Exception as e:
        print(f"Logged in, but profile read failed: {e}")

    print(f"\nSession cached at {pickle_path}.")
    print("The MCP and live trader will now reuse it - verify with:")
    print("    python3 robinhood_sync.py status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
