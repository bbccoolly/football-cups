"""Isolated research pipeline.

Research records never qualify as formal collector data. Historical and model
artifact records are retrospective; shadow events are current-match research
facts that remain excluded from formal product gates.
"""

SCHEMA_VERSION = 1

HISTORICAL_RESEARCH_FLAGS = {
    "research_only": True,
    "research_kind": "historical",
    "backfill": True,
    "strict_backtest_eligible": False,
    "cutoff_eligible": False,
}

MODEL_ARTIFACT_FLAGS = {
    "research_only": True,
    "research_kind": "model_artifact",
    "backfill": True,
    "strict_backtest_eligible": False,
    "cutoff_eligible": False,
}

SHADOW_EVENT_FLAGS = {
    "research_only": True,
    "research_kind": "shadow_event",
    "backfill": False,
    "strict_backtest_eligible": False,
    "cutoff_eligible": False,
}

# Backwards-compatible alias used by existing historical normalization code.
RESEARCH_FLAGS = HISTORICAL_RESEARCH_FLAGS


def research_flags(kind: str) -> dict[str, object]:
    if kind == "historical":
        return dict(HISTORICAL_RESEARCH_FLAGS)
    if kind == "model_artifact":
        return dict(MODEL_ARTIFACT_FLAGS)
    if kind == "shadow_event":
        return dict(SHADOW_EVENT_FLAGS)
    raise ValueError(f"unsupported research kind: {kind}")
