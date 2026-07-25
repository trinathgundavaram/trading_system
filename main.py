"""Entry point + rich terminal dashboard (default mode) or FastAPI/WebSocket web
UI (--ui flag). Reads SQLite only (build note #15) - all MCP/network work happens
in scheduler.py, run either as this process's background thread (terminal mode)
or as its own separate process alongside `python3 main.py --ui` (web mode - see
run.sh). This process just renders what's already been written to output/trading.db.

Terminal mode keyboard: [R] run a cycle now  [C] copy trade_prompt.md to clipboard
          (pbcopy)  [O] open trade_prompt.md (macOS `open`)  [P] pause/resume
          [Q] quit

Web mode: `python3 main.py --ui` serves http://localhost:8080 (see server.py).
Run `python3 scheduler.py` in a second process for the actual scan loop -
main.py --ui does not start a scheduler thread itself.
"""
import argparse
import os
import re
import subprocess
import sys
import threading
import time

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

import scheduler as scheduler_module
from storage.database import Database

console = Console()
db = Database()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# §38.2 - see storage/paths.py.
from storage.paths import trade_prompt_path

TRADE_PROMPT_PATH = str(trade_prompt_path())

POSIX = sys.platform != "win32"
if POSIX:
    import select
    import termios
    import tty


class KeyReader:
    """Non-blocking single-keypress reader for POSIX terminals - avoids the
    `keyboard` package, which needs root/accessibility permissions to catch
    global key events. On Windows, shortcuts are disabled; use Ctrl+C to quit."""

    def __init__(self):
        self.enabled = POSIX and sys.stdin.isatty()
        self._old_settings = None

    def __enter__(self):
        if self.enabled:
            self._old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *exc):
        if self.enabled and self._old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old_settings)

    def poll(self, timeout=0.1):
        if not self.enabled:
            return None
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read(1) if r else None


class Dashboard:
    def __init__(self):
        self.paused = False
        self.running = True
        self._last_prompt_mtime = None

    # ---------- market pulse (parsed from the last matching log line - dashboard
    # never calls an MCP/network source directly, per build note #15) ----------
    def _market_pulse(self):
        for ts, level, msg in reversed(db.recent_logs(50)):
            m = re.search(r"F&G=(\d+), VIX=([\d.]+)", msg)
            if m:
                return {"fg": m.group(1), "vix": m.group(2), "at": ts[11:16]}
        return None

    def render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="market", size=4),
            Layout(name="signals", size=10),
            Layout(name="positions", size=6),
            Layout(name="trades", size=6),
            Layout(name="log", size=8),
            Layout(name="footer", size=3),
        )

        status = "PAUSED" if self.paused else "RUNNING"
        prompt_ready = os.path.exists(TRADE_PROMPT_PATH)
        prompt_note = "Prompt ready - press C to copy" if prompt_ready else "No prompt yet"
        layout["header"].update(Panel(
            f"[bold]TRADING PLATFORM[/bold]   Status: [yellow]{status}[/yellow] | "
            f"Cycle: {scheduler_module.cycle_count} | {prompt_note}",
            style="bold blue",
        ))

        pulse = self._market_pulse()
        if pulse:
            layout["market"].update(Panel(
                f"Fear & Greed: {pulse['fg']}/100   VIX: {pulse['vix']}   (as of {pulse['at']})",
                title="MARKET PULSE",
            ))
        else:
            layout["market"].update(Panel("(no data yet - waiting for first cycle)", title="MARKET PULSE"))

        sig_table = Table(expand=True)
        for col in ["TICKER", "SIGNAL", "SCORE", "TIME"]:
            sig_table.add_column(col)
        seen = set()
        for row in db.get_recent_signals(50):
            if row["ticker"] in seen:
                continue
            seen.add(row["ticker"])
            color = {"BUY": "green", "SELL": "red", "HOLD": "yellow"}.get(row["signal"], "white")
            sig_table.add_row(
                row["ticker"], f"[{color}]{row['signal']}[/{color}]",
                f"{row['buy_pct']:.0f}%" if row.get("buy_pct") is not None else "-",
                row["timestamp"][11:16],
            )
        layout["signals"].update(Panel(sig_table, title="SIGNALS (most recent per ticker)"))

        pos_table = Table(expand=True)
        for col in ["TICKER", "ENTRY", "SHARES", "TRAIL HIGH"]:
            pos_table.add_column(col)
        for p in db.get_all_positions():
            pos_table.add_row(p["ticker"], f"${p['entry_price']:.2f}", f"{p['shares']:.4f}",
                               f"${p['trail_high']:.2f}" if p.get("trail_high") else "-")
        layout["positions"].update(Panel(pos_table, title="OPEN POSITIONS"))

        trade_table = Table(expand=True)
        for col in ["TIME", "SIDE", "TICKER", "AMOUNT", "STATUS"]:
            trade_table.add_column(col)
        for t in db.get_recent_trades(5):
            trade_table.add_row(t["timestamp"][11:16], t["side"].upper(), t["ticker"],
                                 f"${t['amount']:.2f}" if t.get("amount") else "-", t["status"] or "-")
        layout["trades"].update(Panel(trade_table, title="TRADE HISTORY"))

        log_lines = "\n".join(f"{ts[11:16]} [{lvl}] {msg}" for ts, lvl, msg in db.recent_logs(6))
        layout["log"].update(Panel(log_lines or "(no log entries yet)", title="LIVE LOG"))

        layout["footer"].update(Panel(
            "[R] Run now   [C] Copy prompt   [O] Open prompt   [P] Pause   [Q] Quit",
            style="dim",
        ))
        return layout

    # ---------- actions ----------
    def copy_prompt(self):
        if not os.path.exists(TRADE_PROMPT_PATH):
            console.print("[yellow]No trade_prompt.md yet - run a cycle first.[/yellow]")
            return
        try:
            with open(TRADE_PROMPT_PATH) as f:
                content = f.read()
            subprocess.run(["pbcopy"], input=content.encode(), check=True)
            console.print("[green]Copied output/trade_prompt.md to clipboard.[/green]")
        except (FileNotFoundError, subprocess.CalledProcessError):
            console.print("[yellow]pbcopy not available (not on macOS?) - open the file manually.[/yellow]")

    def open_prompt(self):
        if not os.path.exists(TRADE_PROMPT_PATH):
            console.print("[yellow]No trade_prompt.md yet - run a cycle first.[/yellow]")
            return
        try:
            subprocess.run(["open", TRADE_PROMPT_PATH], check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            console.print(f"[yellow]Could not auto-open. Path: {TRADE_PROMPT_PATH}[/yellow]")

    def run(self):
        with KeyReader() as keys, Live(self.render(), console=console, refresh_per_second=2) as live:
            last_refresh = time.time()
            while self.running:
                key = keys.poll(timeout=0.2)
                if key:
                    if key.lower() == "q":
                        self.running = False
                    elif key.lower() == "r":
                        threading.Thread(target=scheduler_module.run_cycle, daemon=True).start()
                    elif key.lower() == "c":
                        live.stop()
                        self.copy_prompt()
                        live.start()
                    elif key.lower() == "o":
                        live.stop()
                        self.open_prompt()
                        live.start()
                    elif key.lower() == "p":
                        self.paused = not self.paused

                if time.time() - last_refresh > 1.0:
                    live.update(self.render())
                    last_refresh = time.time()


def _run_scheduler_background():
    """The scheduler's start() uses a BlockingScheduler and calls run_cycle()
    immediately - runs in its own thread so the Rich dashboard owns the main
    thread's stdin/stdout for keyboard shortcuts."""
    try:
        scheduler_module.start()
    except Exception as e:
        db.log("ERROR", f"Scheduler thread crashed: {e}")


def main():
    console.print("[bold blue]TRADING PLATFORM - STARTUP[/bold blue]")
    cfg = scheduler_module.load_config()
    console.print(f"Watchlist: {cfg['watchlist']}")
    console.print(f"Auto-trade: {'ON' if cfg['trading']['auto_trade'] else 'OFF'} "
                   f"(edit config.yaml -> trading.auto_trade to change)")
    console.print("Trades are never placed from Python - paste output/trade_prompt.md "
                   "into Claude Desktop to review/execute via the robinhood-trading MCP.")

    threading.Thread(target=_run_scheduler_background, daemon=True).start()

    dashboard = Dashboard()
    try:
        dashboard.run()
    except KeyboardInterrupt:
        pass
    console.print("Shutting down.")


def run_ui():
    """Serves the FastAPI + WebSocket dashboard (server.py) - does NOT start a
    scheduler thread; run `python3 scheduler.py` separately (see run.sh --ui)."""
    import uvicorn
    from server import app
    console.print("[bold blue]TRADING PLATFORM - WEB UI[/bold blue]")
    console.print("Serving http://localhost:8080  (Ctrl+C to stop)")
    console.print("This process does NOT run the scan loop - start "
                   "`python3 scheduler.py` separately, or use `./run.sh --ui`.")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui", action="store_true", help="Start the FastAPI web UI (port 8080) instead of the terminal dashboard")
    args = parser.parse_args()

    if args.ui:
        run_ui()
    else:
        main()
