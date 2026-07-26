# Archived analysis

Moved out of the repo root on 2026-07-26 during a cleanup pass. These are NOT
superseded and were deliberately not deleted — each contains findings that were
never implemented, so throwing them away would discard open work rather than
dead weight. They are here because the root directory had accumulated a dozen
loose documents, not because their contents are settled.

Nothing in the codebase references them, which is the reason they were flagged
as stale candidates in the first place. Absence of references is a poor proxy
for irrelevance in a repo whose analysis lives in Markdown.

## `ticker_selection_research.md` (2026-07-22)

Why nothing was scoring above ~48% against a 50% TURBO threshold, across 249
scored ticker-cycles. States plainly at the top: *"Research findings only —
nothing below has been implemented yet."* Four compounding causes, of which at
least the first has since changed independently (`finviz_screen` is now
`enabled: false` in config.yaml, which sidesteps rather than fixes the missing
MCP server it describes):

1. finviz's MCP server not installed/runnable — zeroing ~21% of the EXTERNAL
   bucket every cycle
2. a data-starvation trap: candidates are scored "lite" unless they already
   score near the bar, and cannot reach the bar without the data being withheld
3. the live buy bar being pushed to 57% by the same weak breadth that caps the
   scores — one root cause charged twice
4. relative-volume data reading 0.0–0.1x for liquid names in mid-afternoon,
   which is not plausible and quietly drags VOLUME_PA toward zero

Item 4 in particular is a data-correctness claim that either is or is not still
true, and is worth re-checking before the next screener change.

## `position_tier_evaluation.md` (2026-07-24)

Evaluates adding a third "short-term hold" tier beyond DAY/SWING. Concludes
HYBRID is a router rather than a third behaviour, and that the contained version
of the feature is to reuse the SWING entry engine with different post-entry
treatment, following `_classify_hybrid_leg()` in `scheduler.py`.

Worth keeping for its argument against the larger version: the DAY/HYBRID
rebuild introduced a real bug — EV lookups querying a `"HYBRID"` mode key that
never gets written — that survived until a dedicated audit found it. Every
additional mode-keyed path is another site for that same bug class.
