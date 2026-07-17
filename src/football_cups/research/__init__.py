"""Isolated historical research pipeline.

Research records are retrospective and can never qualify as strict collector data.
"""

SCHEMA_VERSION = 1

RESEARCH_FLAGS = {
    "research_only": True,
    "backfill": True,
    "strict_backtest_eligible": False,
    "cutoff_eligible": False,
}
