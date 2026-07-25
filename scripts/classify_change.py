#!/usr/bin/env python3
"""Suggest the version bump level from the diff (§35).

Advisory, not binding - but it catches the case the human eye reliably misses:
a tiny diff inside the scoring path that changes every decision the system
makes. Deleting ``* b.qual_mult`` from the weighted sum (§19) is a one-line
change that a conventional scheme would call a patch. It re-scores every
candidate in the system, which means pre-change trade history may not be
pooled with post-change history. That is a major bump.

Called by scripts/release.sh, which warns and asks for confirmation when the
suggestion is MAJOR and you asked for something smaller.

Usage:  classify_change.py [base_ref]        # default HEAD~1
Prints one of: MAJOR | MINOR | PATCH
Exit code 0 always - this informs, it does not block.
"""
import subprocess
import sys

# Touching any of these = the decision function moved = major (X.0.0).
# Paths that do not exist yet are listed deliberately: engine/execution_costs.py
# arrives in Phase 4 §22, and the day it does, it must already be classified.
DECISION_PATHS = (
    "rules/swing_buy_rules.py",
    "rules/sell_rules.py",
    "rules/exit_scorer.py",
    "rules/dynamic_thresholds.py",
    "rules/hard_vetoes.py",
    "rules/probabilistic_decision.py",
    "rules/market_filters.py",
    "rules/spread_quality.py",
    "rules/execution_quality.py",
    "engine/stop_state_machine.py",
    "engine/position_sizing.py",
    "engine/execution_costs.py",     # §22, Phase 4 - not yet present
    "engine/regime_engine.py",
    "engine/regime_weight_adaptation.py",
    "engine/ev_engine.py",
    "engine/rules_catalog.py",
)

# A config.yaml diff touching any of these keys is equally a decision change.
DECISION_CONFIG_KEYS = (
    "weights:", "buy_rules:", "sell_rules:", "stop_machine:",
    "position_sizing:", "risk_level:", "thresholds:", "scoring:",
    "execution_quality:", "regime:", "ev_engine:", "qual_mult",
    "max_points", "veto",
)

# Behaviour changes that do not move the decision function = minor (1.X.0).
BEHAVIOUR_PATHS = (
    "scheduler.py",
    "server.py",
    "main.py",
    "engine/paper_trader.py",
    "engine/live_trader.py",
    "engine/position_management.py",
    "engine/portfolio_risk.py",
    "engine/cycle_supervisor.py",
    "engine/learning_loop.py",
    "rules/risk_rules.py",
    "storage/database.py",
)


def _git(*args) -> str:
    """Run git, and FAIL if git failed.

    This used to return .stdout unconditionally. An unknown base ref - a
    mistyped tag, or a release note referring to a tag that has not been cut
    yet - produced an empty file list, no files matched any rule, and the
    function returned PATCH. The most reassuring possible answer, from a
    command that had not managed to read the diff at all.

    A classifier whose failure mode is "everything is fine" is worse than no
    classifier, because release.sh only prompts when the suggestion is MAJOR.
    """
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            f"classify_change: git {' '.join(args)} failed:\n"
            f"{r.stderr.strip()}\n"
            f"Refusing to guess - a bad base ref must not be reported as PATCH.")
    return r.stdout


def classify(base_ref: str) -> str:
    files = _git("diff", "--name-only", base_ref, "HEAD").split()

    if any(f in DECISION_PATHS for f in files):
        return "MAJOR"

    if "config.yaml" in files:
        diff = _git("diff", base_ref, "HEAD", "--", "config.yaml")
        # Only consider real +/- content lines, not the +++/--- file headers.
        changed = [l for l in diff.splitlines()
                   if l[:1] in "+-" and l[1:2] not in ("-", "+")]
        if any(k in l for l in changed for k in DECISION_CONFIG_KEYS):
            return "MAJOR"

    if any(f.startswith("migrations/") for f in files):
        return "MAJOR"

    if any(f in BEHAVIOUR_PATHS for f in files):
        return "MINOR"

    return "PATCH"


if __name__ == "__main__":
    print(classify(sys.argv[1] if len(sys.argv) > 1 else "HEAD~1"))
