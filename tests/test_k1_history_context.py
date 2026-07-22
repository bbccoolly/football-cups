from __future__ import annotations

from pathlib import Path

from football_cups.research.k1_history_context import (
    _gap_bin,
    _line_bucket,
    _normalize_history_row,
    _probability_bin,
    _select_cohort,
    _total_bucket,
    _wilson,
    load_k1_analysis_presentation,
)


ROOT = Path(__file__).resolve().parents[1]


def _raw_row(*, home: float, draw: float, away: float, asian: float = -0.5) -> dict:
    return {
        "source_fixture_key": "fixture",
        "season": "2025",
        "features": {
            "fixture_id": "fixture", "kickoff": "2025-02-15T12:00:00+08:00",
            "label_available_at": "2025-02-15T18:00:00+08:00",
            "home": "Home", "away": "Away", "home_goals": "1", "away_goals": "0",
            "actual_direction": "homeWin", "implied_home": str(home),
            "implied_draw": str(draw), "implied_away": str(away),
            "asian_line": str(asian), "asian_line_delta": "0.25",
            "euro_home_delta": "0.04", "euro_away_delta": "-0.02",
            "total_line": "2.25", "total_line_delta": "0", "euro_iqr": "0.08",
        },
    }


def test_presentation_contract_and_bins() -> None:
    presentation = load_k1_analysis_presentation(ROOT)
    assert presentation.version == "k1-analysis-presentation-v2"
    assert _probability_bin(0.40) == "0.40-0.45"
    assert _gap_bin(0.10) == "0.10-0.20"
    assert _line_bucket(0.5) == "shallow"
    assert _line_bucket(-0.25) == "unsupported"
    assert _total_bucket(2.25) == "2.25"


def test_history_normalization_uses_top_outcome_and_favorite_perspective() -> None:
    home = _normalize_history_row(_raw_row(home=0.50, draw=0.28, away=0.22, asian=-0.5))
    assert home["top_outcome"] == "home"
    assert home["favorite_line"] == 0.5
    assert home["favorite_line_delta"] == -0.25
    assert home["favorite_odds_delta"] == 0.04
    draw = _normalize_history_row(_raw_row(home=0.32, draw=0.36, away=0.32, asian=0))
    assert draw["top_outcome"] == "draw"
    assert draw["draw_is_top"] is True
    assert draw["favorite_line"] is None


def test_cohort_selection_falls_back_deterministically() -> None:
    shape = {
        "top_outcome": "home", "probability_bin": "0.45-0.50",
        "gap_bin": "0.10-0.20", "line_bucket": "shallow", "total_bucket": "2.25",
        "draw_is_top": False,
    }
    rows = [dict(shape, fixture_id=str(index)) for index in range(29)]
    rows.extend(dict(shape, fixture_id=f"fallback-{index}", line_bucket="level") for index in range(5))
    level, filters, selected, fallback = _select_cohort(rows, shape, 30)
    assert level == "L3"
    assert filters == {"probability_bin": "0.45-0.50", "gap_bin": "0.10-0.20", "total_bucket": "2.25"}
    assert len(selected) == 34
    assert fallback is True


def test_wilson_interval_contains_observed_rate() -> None:
    interval = _wilson(47, 100, 0.90)
    assert interval is not None
    assert interval["lower"] < 0.47 < interval["upper"]
