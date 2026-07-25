"""Runtime paths, overridable so several versions can run side by side (§38.2).

Previously every module derived its own paths from ``__file__``:
scheduler.py did ``OUTPUT_DIR = os.path.join(BASE_DIR, "output")`` and
storage/log_setup.py did the same for logs. With one git worktree per installed
version (§38) that arrangement would (a) put logs and database files inside a
directory git is watching and (b) give every newly installed version an empty
history, since its output/ would be a fresh empty directory in a fresh checkout.

``TP_OUTPUT_DIR`` moves the data out of the tree and makes it explicitly
per-version: ``scripts/tp run`` points each version at ~/tp/data/<tag>/output.

Defaults to ``<repo>/output``, so behaviour is unchanged when the variable is
unset. Existing scripts, tests and the current launchd services keep working
with no edit - which is the only reason this can be done in Phase 0, before the
test suite is restored.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent


def _ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def output_dir() -> Path:
    """Root of all runtime data: logs, prompts, backtest artefacts, exports."""
    return _ensure(Path(os.getenv("TP_OUTPUT_DIR", REPO_DIR / "output")))


def logs_dir() -> Path:
    return _ensure(output_dir() / "logs")


def pending_dir() -> Path:
    return _ensure(output_dir() / "pending_prompts")


def backtest_dir() -> Path:
    return _ensure(output_dir() / "backtest_results")


def archive_dir() -> Path:
    return _ensure(output_dir() / "archive")


def validation_dir() -> Path:
    """Where run_backtest.py writes the live-arm validation receipt (§2/§23)."""
    return _ensure(output_dir() / "validation")


def validation_receipt_path() -> Path:
    """The receipt engine/live_trader.py's _validation_current() requires
    before live execution may arm. Per-version, like everything else under
    output_dir(): a receipt earned by v1.2.0's backtest must not silently
    authorise v1.3.0 to trade.

    Does NOT create the directory, unlike its siblings above. This is on the
    read path - is_live_mode() calls it - and a read should not have a
    filesystem side effect. The writer (run_backtest.py) calls
    validation_dir() first."""
    return output_dir() / "validation" / "live_arm_receipt.json"


def trade_prompt_path() -> Path:
    """Not a directory - the generated prompt file main.py opens."""
    return output_dir() / "trade_prompt.md"


def describe() -> str:
    """One line for a startup log. Which data directory is this process using?

    Worth logging on every start: the single most confusing failure mode of a
    side-by-side setup is looking at the wrong version's logs.
    """
    overridden = "TP_OUTPUT_DIR" in os.environ
    return (f"output_dir={output_dir()} "
            f"({'TP_OUTPUT_DIR' if overridden else 'default: <repo>/output'})")
