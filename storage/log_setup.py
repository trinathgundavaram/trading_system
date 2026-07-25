"""Shared log-file setup for scheduler.py and server.py (2026-07-14, in
response to Trinath asking for logs to persist and be traceable in the UI).

Design choices, and why:

- ONE file per process (output/logs/scheduler.log, output/logs/server.log),
  NOT one shared file. Python's RotatingFileHandler rotation (checking size,
  renaming .log -> .log.1, etc.) is only safe within a single process - two
  separate OS processes (scheduler.py and server.py both run standalone, see
  README) rotating the SAME file at once can race and clobber each other.
  Separate files sidesteps that entirely; server.py's /api/logs endpoint
  merges the two by timestamp for the UI so this is invisible to the user.

- RotatingFileHandler, not a DB table. storage/database.py already has a
  `logs` table (Database.log()/recent_logs()), but that's used sparingly -
  only for a handful of high-signal events (kill switch trips, order blocks -
  see engine/executor.py, rules/risk_rules.py). Piping EVERY logger.info()
  call through a SQLite INSERT (with Database's own threading.Lock) would add
  write contention exactly during scheduler.py's per-ticker parallel loop -
  the opposite of what that loop was just optimized for. A rotating file is
  just a buffered OS-level append - effectively free by comparison, so this
  is what "won't hamper performance" means in practice here.

- Bounded size (5MB x 3 backups per process = ~15MB ceiling each) so log
  files can't grow forever unwatched.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# §38.2 - see storage/paths.py. Unset TP_OUTPUT_DIR resolves to <repo>/output.
from storage.paths import logs_dir

LOG_DIR = str(logs_dir())

_configured = set()  # process_name(s) already set up in THIS interpreter - avoids
                      # double-adding handlers if setup_logging() is called twice
                      # (e.g. re-imported in a test)


class _KnownBenignMCPStdioNoise(logging.Filter):
    """2026-07-20 (Trinath flagged the repeated ERROR + traceback in the
    logs): mcp_clients/fear_greed.py spawns `npx -y mcp-server-fear-greed`
    fresh per call (mcp_clients/base.py's StdioMCPClient design), and that
    package - both published versions, 1.0.0 and 1.0.1, there is no newer
    release to pin to instead - prints its own startup banner
    ("fear-greed MCP Server running on stdio") to STDOUT rather than STDERR.
    STDOUT is reserved for JSON-RPC framing, so the MCP SDK's stdout_reader
    tries to parse that banner line as a JSONRPCMessage, fails pydantic
    validation, and logs a full ERROR + traceback - confirmed via
    output/logs to fire on every single fear-greed spawn (dozens/hour), and
    confirmed non-fatal every time: the SDK just discards the one bad line
    and keeps reading, and the real value comes through immediately after
    (the very next log line is always "Market gate OPEN - F&G=...").

    Rather than silence mcp.client.stdio's ERROR level wholesale (which
    would hide a genuinely different parse failure from some other server),
    this matches only that one exact known-safe banner text pulled from the
    exception itself, and drops just that record. Anything else logged by
    mcp.client.stdio - including a JSONRPCMessage validation error with a
    DIFFERENT input_value - still comes through normally."""
    _KNOWN_BENIGN_SIGNATURES = (
        "fear-greed MCP Server running on stdio",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "mcp.client.stdio" or not record.exc_info:
            return True
        exc = record.exc_info[1]
        if exc is None:
            return True
        exc_text = str(exc)
        return not any(sig in exc_text for sig in self._KNOWN_BENIGN_SIGNATURES)


def log_file_path(process_name: str) -> str:
    return os.path.join(LOG_DIR, f"{process_name}.log")


def setup_logging(process_name: str, level: int = logging.INFO):
    """Call once near the top of scheduler.py / server.py. Attaches a
    RotatingFileHandler + a console StreamHandler to the ROOT logger, so every
    module's logger (engine.*, rules.*, analytics.*, ...) is captured via
    normal logging propagation - no per-module changes needed."""
    if process_name in _configured:
        return
    _configured.add(process_name)

    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter(f"%(asctime)s %(levelname)s [{process_name}] %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_file_path(process_name), maxBytes=5_000_000, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)

    # 2026-07-21 (prod-readiness pass): only attach the console handler when
    # stdout is a real interactive terminal. Under launchd (service.sh),
    # stdout is redirected straight into output/logs/launchd_<name>.log -
    # which macOS does NOT rotate - so every line this process logs was
    # being written TWICE: once here to the properly bounded
    # RotatingFileHandler above (5MB x 3 backups), and once via this console
    # handler into that unrotated launchd file, which just grows forever
    # (observed at 5-10MB+ and climbing). Interactive use (./run.sh in a
    # terminal) still gets console output as before, since a real terminal's
    # stdout.isatty() is True.
    if sys.stdout.isatty():
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(fmt)
        root.addHandler(console_handler)

    # See _KnownBenignMCPStdioNoise's docstring - drops exactly one known,
    # confirmed-harmless recurring ERROR (fear-greed's stdout banner),
    # nothing else.
    logging.getLogger("mcp.client.stdio").addFilter(_KnownBenignMCPStdioNoise())


def tail_log_lines(process_name: str, max_lines: int = 300) -> list[str]:
    """Last `max_lines` lines from this process's current log file (does NOT
    read the rotated .log.1/.log.2 backups - recent activity only, which is
    what the UI's Logs tab needs). Cheap: bounded file size (5MB ceiling)
    means worst case is reading a few MB off local disk, not a real cost."""
    path = log_file_path(process_name)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [ln.rstrip("\n") for ln in lines[-max_lines:]]
    except OSError:
        return []
