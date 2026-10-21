"""A paper-trading ledger for one sleeve, stored in its own tables
(`{prefix}_account`, `{prefix}_positions`, `{prefix}_state`, `{prefix}_equity_log`).

It exposes the same method names engine/sector_momentum_paper.py already calls on
Database (get_sm_account, open_sm_position, ...), so a second sleeve reuses that
engine unchanged by passing a SleeveLedger instead of the Database. Strategies
never share a table: the prefix is the isolation boundary.
"""
import re
import sqlite3
from datetime import datetime


class SleeveLedger:
    def __init__(self, db, prefix: str):
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,15}", prefix):
            raise ValueError(f"bad ledger prefix {prefix!r}")
        self.db, self.p = db, prefix
        with db._conn() as conn:
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {prefix}_account (
                id INTEGER PRIMARY KEY CHECK (id = 1), starting_cash REAL NOT NULL, cash REAL NOT NULL,
                realized_pnl REAL DEFAULT 0, created_at TEXT, updated_at TEXT)""")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {prefix}_positions (
                id SERIAL PRIMARY KEY, ticker TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                entry_date TEXT NOT NULL, entry_price REAL NOT NULL, shares REAL NOT NULL,
                exit_date TEXT, exit_price REAL, realized_pnl REAL, created_at TEXT, updated_at TEXT)""")
            conn.execute(f"CREATE TABLE IF NOT EXISTS {prefix}_state (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {prefix}_equity_log (
                as_of_date TEXT PRIMARY KEY, total_value REAL NOT NULL, spy_close REAL NOT NULL)""")

    def _conn(self):
        return self.db._conn()

    # ---- account ----
    def get_sm_account(self):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(f"SELECT * FROM {self.p}_account WHERE id = 1").fetchone()
            return dict(row) if row else None

    def init_sm_account(self, starting_cash: float):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(f"""INSERT INTO {self.p}_account (id, starting_cash, cash, realized_pnl, created_at, updated_at)
                             VALUES (1, ?, ?, 0, ?, ?) ON CONFLICT(id) DO NOTHING""", (starting_cash, starting_cash, now, now))
        return self.get_sm_account()

    def adjust_sm_cash(self, delta: float, realized_pnl_delta: float = 0.0):
        with self._conn() as conn:
            conn.execute(f"UPDATE {self.p}_account SET cash = cash + ?, realized_pnl = realized_pnl + ?, updated_at = ? WHERE id = 1",
                         (delta, realized_pnl_delta, datetime.utcnow().isoformat()))

    # ---- positions ----
    def get_open_sm_positions(self):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM {self.p}_positions WHERE status = 'open' ORDER BY ticker").fetchall()]

    def open_sm_position(self, ticker: str, entry_date: str, entry_price: float, shares: float) -> int:
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            row = conn.execute(f"""INSERT INTO {self.p}_positions (ticker, status, entry_date, entry_price, shares, created_at, updated_at)
                                   VALUES (?, 'open', ?, ?, ?, ?, ?) RETURNING id""",
                               (ticker, entry_date, entry_price, shares, now, now)).fetchone()
        return row[0] if not isinstance(row, dict) else row.get("id")

    def update_sm_position_shares(self, position_id: int, shares: float):
        with self._conn() as conn:
            conn.execute(f"UPDATE {self.p}_positions SET shares = ?, updated_at = ? WHERE id = ?",
                         (shares, datetime.utcnow().isoformat(), position_id))

    def update_sm_position_entry_price(self, position_id: int, entry_price: float):
        with self._conn() as conn:
            conn.execute(f"UPDATE {self.p}_positions SET entry_price = ?, updated_at = ? WHERE id = ?",
                         (entry_price, datetime.utcnow().isoformat(), position_id))

    def close_sm_position(self, position_id: int, exit_date: str, exit_price: float, realized_pnl: float):
        with self._conn() as conn:
            conn.execute(f"""UPDATE {self.p}_positions SET status = 'closed', exit_date = ?, exit_price = ?,
                             realized_pnl = ?, updated_at = ? WHERE id = ?""",
                         (exit_date, exit_price, realized_pnl, datetime.utcnow().isoformat(), position_id))

    # ---- state + equity log ----
    def get_sm_state(self, key: str, default=None):
        with self._conn() as conn:
            row = conn.execute(f"SELECT value FROM {self.p}_state WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default

    def set_sm_state(self, key: str, value: str):
        with self._conn() as conn:
            conn.execute(f"""INSERT INTO {self.p}_state (key, value) VALUES (?, ?)
                             ON CONFLICT (key) DO UPDATE SET value = excluded.value""", (key, value))

    def log_sm_equity(self, as_of_date: str, total_value: float, spy_close: float):
        with self._conn() as conn:
            conn.execute(f"""INSERT INTO {self.p}_equity_log (as_of_date, total_value, spy_close) VALUES (?, ?, ?)
                             ON CONFLICT (as_of_date) DO UPDATE SET total_value = excluded.total_value, spy_close = excluded.spy_close""",
                         (as_of_date, total_value, spy_close))

    def get_sm_equity_log(self):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                f"SELECT as_of_date, total_value, spy_close FROM {self.p}_equity_log ORDER BY as_of_date").fetchall()]
