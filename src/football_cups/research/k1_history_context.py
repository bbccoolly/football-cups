from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist, median
from typing import Any, Iterable, Mapping

from .k1_guardrail import K1_DATASET_SHA256, K1_INPUT_HASH, K1_METADATA_SHA256, K1GuardrailError


@dataclass(frozen=True)
class K1AnalysisPresentation:
    payload: dict[str, Any]
    version: str
    minimum_cohort_size: int
    minimum_season_size: int
    ece_minimum_size: int
    example_count: int
    wilson_confidence: float
    bootstrap_confidence: float
    bootstrap_iterations: int
    probability_bins: tuple[float, ...]
    direction_gap_bins: tuple[float, ...]
    rule_labels: dict[str, str]
    status_labels: dict[str, str]
    file_sha256: str
    canonical_sha256: str


def load_k1_analysis_presentation(workspace: Path) -> K1AnalysisPresentation:
    path = workspace.resolve() / "config" / "research-k1-analysis-presentation.json"
    content = path.read_bytes()
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise K1GuardrailError(f"invalid K1 analysis presentation: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise K1GuardrailError("unsupported K1 analysis presentation schema")
    if payload.get("presentation_version") != "k1-analysis-presentation-v1":
        raise K1GuardrailError("unsupported K1 analysis presentation version")
    integers = {}
    for name in ("minimum_cohort_size", "minimum_season_size", "ece_minimum_size", "example_count", "bootstrap_iterations"):
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise K1GuardrailError(f"{name} must be a positive integer")
        integers[name] = value
    if integers["example_count"] > 5 or integers["bootstrap_iterations"] != 10000:
        raise K1GuardrailError("K1 analysis example/bootstrap contract mismatch")
    confidences = []
    for name in ("wilson_confidence", "bootstrap_confidence"):
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) != 0.9:
            raise K1GuardrailError(f"{name} must equal 0.90")
        confidences.append(float(value))
    probability_bins = tuple(float(value) for value in payload.get("probability_bins") or [])
    gap_bins = tuple(float(value) for value in payload.get("direction_gap_bins") or [])
    if probability_bins != (0.4, 0.45, 0.5, 0.55) or gap_bins != (0.05, 0.1, 0.2):
        raise K1GuardrailError("K1 analysis bin contract mismatch")
    rule_labels = payload.get("rule_labels")
    status_labels = payload.get("status_labels")
    if not isinstance(rule_labels, dict) or len(rule_labels) != 8:
        raise K1GuardrailError("all K1 rule labels are required")
    if status_labels != {"matched": "触发", "not_matched": "未触发", "not_evaluable": "不可评估"}:
        raise K1GuardrailError("invalid K1 status labels")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return K1AnalysisPresentation(
        payload=payload, version=payload["presentation_version"],
        minimum_cohort_size=integers["minimum_cohort_size"],
        minimum_season_size=integers["minimum_season_size"],
        ece_minimum_size=integers["ece_minimum_size"], example_count=integers["example_count"],
        wilson_confidence=confidences[0], bootstrap_confidence=confidences[1],
        bootstrap_iterations=integers["bootstrap_iterations"], probability_bins=probability_bins,
        direction_gap_bins=gap_bins, rule_labels=dict(rule_labels), status_labels=dict(status_labels),
        file_sha256=hashlib.sha256(content).hexdigest(),
        canonical_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise K1GuardrailError("historical timestamp must include timezone")
    return parsed.astimezone(UTC)


def _float(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise K1GuardrailError(f"invalid historical feature: {name}") from exc
    if not math.isfinite(result):
        raise K1GuardrailError(f"non-finite historical feature: {name}")
    return result


def _probability_bin(value: float) -> str:
    if value < 0.40:
        return "<0.40"
    if value < 0.45:
        return "0.40-0.45"
    if value < 0.50:
        return "0.45-0.50"
    if value < 0.55:
        return "0.50-0.55"
    return ">=0.55"


def _gap_bin(value: float) -> str:
    if value < 0.05:
        return "<0.05"
    if value < 0.10:
        return "0.05-0.10"
    if value < 0.20:
        return "0.10-0.20"
    return ">=0.20"


def _line_bucket(value: float | None) -> str | None:
    if value is None:
        return None
    if value < 0:
        return "unsupported"
    if value == 0:
        return "level"
    if value in {0.25, 0.5}:
        return "shallow"
    if value in {0.75, 1.0}:
        return "medium"
    return "deep"


def _total_bucket(value: float) -> str:
    if value <= 2.0:
        return "<=2.00"
    if value == 2.25:
        return "2.25"
    if value == 2.5:
        return "2.50"
    return ">=2.75"


def _normalize_history_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    features = dict(raw["features"])
    probabilities = [_float(features[f"implied_{name}"], f"implied_{name}") for name in ("home", "draw", "away")]
    total = sum(probabilities)
    if total <= 0:
        raise K1GuardrailError("historical probabilities have a non-positive sum")
    probabilities = [value / total for value in probabilities]
    outcomes = ("home", "draw", "away")
    top_index = max(range(3), key=lambda index: probabilities[index])
    ordered = sorted(probabilities, reverse=True)
    top_outcome = outcomes[top_index]
    directional = "home" if probabilities[0] >= probabilities[2] else "away"
    draw_is_top = top_outcome == "draw"
    asian_line = _float(features["asian_line"], "asian_line")
    asian_delta = _float(features["asian_line_delta"], "asian_line_delta")
    favorite_line = None if draw_is_top else (-asian_line if top_outcome == "home" else asian_line)
    favorite_line_delta = None if draw_is_top else (-asian_delta if top_outcome == "home" else asian_delta)
    favorite_odds_delta = None if draw_is_top else _float(
        features["euro_home_delta" if top_outcome == "home" else "euro_away_delta"],
        "favorite_odds_delta",
    )
    actual = {"homeWin": "home", "draw": "draw", "awayWin": "away"}[features["actual_direction"]]
    total_line = _float(features["total_line"], "total_line")
    return {
        "fixture_id": str(raw["source_fixture_key"]), "season": str(raw["season"]),
        "kickoff": _utc(features["kickoff"]), "label_available_at": _utc(features["label_available_at"]),
        "home": str(features["home"]), "away": str(features["away"]),
        "home_goals": int(features["home_goals"]), "away_goals": int(features["away_goals"]),
        "probabilities": dict(zip(outcomes, probabilities)), "top_outcome": top_outcome,
        "top_probability": probabilities[top_index], "top_probability_gap": ordered[0] - ordered[1],
        "directional_favorite": directional, "draw_is_top": draw_is_top,
        "favorite_line": favorite_line, "favorite_line_delta": favorite_line_delta,
        "favorite_odds_delta": favorite_odds_delta, "total_line": total_line,
        "total_line_delta": _float(features["total_line_delta"], "total_line_delta"),
        "euro_iqr": _float(features["euro_iqr"], "euro_iqr"), "actual_outcome": actual,
        "probability_bin": _probability_bin(probabilities[top_index]),
        "gap_bin": _gap_bin(ordered[0] - ordered[1]),
        "line_bucket": _line_bucket(favorite_line), "total_bucket": _total_bucket(total_line),
    }


def _wilson(successes: int, count: int, confidence: float) -> dict[str, float] | None:
    if not count:
        return None
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    rate = successes / count
    denominator = 1 + z * z / count
    center = (rate + z * z / (2 * count)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / count + z * z / (4 * count * count)) / denominator
    return {"confidence": confidence, "lower": max(0.0, center - margin), "upper": min(1.0, center + margin)}


def _block_bootstrap(rows: list[dict[str, Any]], *, iterations: int, confidence: float, seed: int) -> dict[str, Any]:
    weeks: dict[str, list[float]] = {}
    for row in rows:
        iso = row["kickoff"].isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        residual = (1.0 if row["actual_outcome"] == row["top_outcome"] else 0.0) - row["top_probability"]
        weeks.setdefault(key, []).append(residual)
    if not weeks:
        return {"count": 0, "iterations": iterations, "confidence": confidence}
    generator = random.Random(seed)
    keys = sorted(weeks)
    samples = []
    for _ in range(iterations):
        values = []
        for _ in keys:
            values.extend(weeks[generator.choice(keys)])
        samples.append(sum(values) / len(values))
    samples.sort()
    alpha = (1 - confidence) / 2
    return {
        "count": len(rows), "iterations": iterations, "confidence": confidence,
        "lower": samples[max(0, int(alpha * len(samples)))],
        "upper": samples[min(len(samples) - 1, int((1 - alpha) * len(samples)))],
    }


def _metrics(rows: list[dict[str, Any]], presentation: K1AnalysisPresentation, cohort_key: str) -> dict[str, Any]:
    if not rows:
        return {"fixture_count": 0, "status": "insufficient_sample"}
    count = len(rows)
    top_hits = sum(row["actual_outcome"] == row["top_outcome"] for row in rows)
    draws = sum(row["actual_outcome"] == "draw" for row in rows)
    directional_wins = sum(row["actual_outcome"] == row["directional_favorite"] for row in rows)
    underdog_wins = sum(row["actual_outcome"] not in {row["directional_favorite"], "draw"} for row in rows)
    losses = []
    briers = []
    rps_values = []
    confidence_rows = []
    for row in rows:
        probabilities = tuple(row["probabilities"][name] for name in ("home", "draw", "away"))
        actual = ("home", "draw", "away").index(row["actual_outcome"])
        losses.append(-math.log(max(probabilities[actual], 1e-15)))
        briers.append(sum((probabilities[index] - (1 if index == actual else 0)) ** 2 for index in range(3)))
        predicted = (probabilities[0], probabilities[0] + probabilities[1])
        observed = (1.0 if actual == 0 else 0.0, 1.0 if actual <= 1 else 0.0)
        rps_values.append(sum((left - right) ** 2 for left, right in zip(predicted, observed)) / 2)
        confidence_rows.append((row["top_probability"], int(row["actual_outcome"] == row["top_outcome"])))
    ece: dict[str, Any] = {"status": "insufficient_sample", "minimum_size": presentation.ece_minimum_size}
    if count >= presentation.ece_minimum_size:
        ordered = sorted(confidence_rows)
        value = 0.0
        for index in range(10):
            bucket = ordered[index * count // 10:(index + 1) * count // 10]
            value += len(bucket) / count * abs(sum(item[0] for item in bucket) / len(bucket) - sum(item[1] for item in bucket) / len(bucket))
        ece = {"status": "available", "bins": 10, "value": value}
    average = sum(row["top_probability"] for row in rows) / count
    seed = int(hashlib.sha256(f"{K1_DATASET_SHA256}|{cohort_key}".encode()).hexdigest()[:16], 16)
    return {
        "fixture_count": count, "status": "available" if count >= presentation.minimum_cohort_size else "insufficient_sample",
        "average_top_probability": average, "actual_top_outcome_hit_rate": top_hits / count,
        "top_outcome_hit_wilson_90": _wilson(top_hits, count, presentation.wilson_confidence),
        "draw_rate": draws / count, "draw_wilson_90": _wilson(draws, count, presentation.wilson_confidence),
        "directional_favorite_win_rate": directional_wins / count, "underdog_win_rate": underdog_wins / count,
        "top_outcome_calibration_residual": top_hits / count - average,
        "calibration_block_bootstrap_90": _block_bootstrap(
            rows, iterations=presentation.bootstrap_iterations,
            confidence=presentation.bootstrap_confidence, seed=seed,
        ),
        "log_loss": sum(losses) / count, "brier": sum(briers) / count,
        "rps": sum(rps_values) / count, "ece": ece,
    }


def _current_shape(analysis: Mapping[str, Any]) -> dict[str, Any]:
    probabilities = analysis["base_probabilities"]
    values = [float(probabilities[name]) for name in ("home", "draw", "away")]
    outcomes = ("home", "draw", "away")
    top_index = max(range(3), key=lambda index: values[index])
    ordered = sorted(values, reverse=True)
    top = outcomes[top_index]
    draw_is_top = top == "draw"
    features = analysis["guardrail"]["raw_features"]
    favorite_line = None if draw_is_top else features.get("current_favorite_line")
    total_line = float(features.get("current_total_line"))
    return {
        "top_outcome": top, "top_probability": values[top_index],
        "top_probability_gap": ordered[0] - ordered[1], "draw_is_top": draw_is_top,
        "probability_bin": _probability_bin(values[top_index]),
        "gap_bin": _gap_bin(ordered[0] - ordered[1]),
        "line_bucket": _line_bucket(float(favorite_line)) if favorite_line is not None else None,
        "total_bucket": _total_bucket(total_line), "favorite_line": favorite_line,
        "total_line": total_line,
    }


def _select_cohort(rows: list[dict[str, Any]], shape: Mapping[str, Any], minimum: int) -> tuple[str, dict[str, Any], list[dict[str, Any]], bool]:
    if shape["draw_is_top"]:
        levels = [
            ("D1", ("top_outcome", "probability_bin", "gap_bin", "total_bucket")),
            ("D2", ("probability_bin", "gap_bin", "total_bucket")),
            ("D3", ("probability_bin", "total_bucket")),
            ("D4", ()),
        ]
    else:
        levels = [
            ("L1", ("top_outcome", "probability_bin", "gap_bin", "line_bucket", "total_bucket")),
            ("L2", ("probability_bin", "gap_bin", "line_bucket", "total_bucket")),
            ("L3", ("probability_bin", "gap_bin", "total_bucket")),
            ("L4", ("probability_bin", "total_bucket")),
            ("L5", ()),
        ]
    for index, (level, fields) in enumerate(levels):
        filters = {field: shape[field] for field in fields}
        selected = [row for row in rows if all(row.get(field) == value for field, value in filters.items())]
        if len(selected) >= minimum or index == len(levels) - 1:
            return level, filters, selected, index > 0
    raise AssertionError("cohort fallback must select a terminal level")


def _proxy_context(rows: list[dict[str, Any]], presentation: K1AnalysisPresentation) -> dict[str, Any]:
    proxies = {
        "r1_shallow_favorite_cooling": (
            "r1_aggregate_odds_partial_proxy",
            [row for row in rows if not row["draw_is_top"] and abs(float(row["favorite_line"])) in {0.25, 0.5} and float(row["favorite_odds_delta"]) >= 0.03],
        ),
        "r2_asian_retreat": (
            "r2_aggregate_retreat_proxy",
            [row for row in rows if not row["draw_is_top"] and row["top_probability"] >= 0.45 and row["top_probability_gap"] >= 0.07 and float(row["favorite_line_delta"]) <= -0.25],
        ),
        "r3_low_total_draw_tail": (
            "r3_final_closing_proxy",
            [row for row in rows if not row["draw_is_top"] and row["total_line"] <= 2.25 and row["probabilities"]["draw"] >= 0.28 and abs(float(row["favorite_line"])) <= 0.5],
        ),
    }
    result = {}
    for rule, (proxy_id, selected) in proxies.items():
        result[rule] = {
            "historical_evaluability": "retrospective_final_closing_proxy",
            "proxy_id": proxy_id, "fixture_count": len(selected),
            "metrics": _metrics(selected, presentation, proxy_id) if len(selected) >= presentation.minimum_cohort_size else {"fixture_count": len(selected), "status": "insufficient_sample"},
        }
    result.update({
        "r0_data_integrity": {"historical_evaluability": "not_applicable"},
        "r2_euro_strong_asian_flat": {"historical_evaluability": "not_available_in_dataset"},
        "r4_handicap_cover_conflict": {"historical_evaluability": "not_available_in_dataset"},
        "r5_live_market_stability": {"historical_evaluability": "not_available_in_dataset"},
    })
    iqr = sorted(row["euro_iqr"] for row in rows)
    percentile = lambda fraction: iqr[min(len(iqr) - 1, int(fraction * (len(iqr) - 1)))] if iqr else None
    result["r6_bookmaker_dispersion"] = {
        "historical_evaluability": "different_metric_distribution_only",
        "metric": "euro_iqr", "median": median(iqr) if iqr else None,
        "p75": percentile(0.75), "p90": percentile(0.90),
    }
    return result


def build_k1_historical_context(
    connection,
    *,
    workspace: Path,
    analysis: Mapping[str, Any],
) -> dict[str, Any]:
    presentation = load_k1_analysis_presentation(workspace)
    rows = connection.execute(
        """
        SELECT feature.source_fixture_key, feature.season, feature.features,
               asset.asset_id, asset.asset_kind, asset.sha256,
               asset.metadata_sha256, asset.input_hash
        FROM research.feature_rows AS feature
        JOIN research.source_assets AS asset ON asset.record_id=feature.source_asset_record_id
        WHERE feature.source_id='k1-derived-core3'
          AND feature.competition='K1'
          AND feature.cohort='derived_closing_features'
          AND feature.feature_schema='opening-closing-v1'
          AND feature.result_scope='regular_time_90'
          AND feature.result_eligible=true
          AND asset.asset_id='k1-core3-features-2025-2026'
          AND asset.asset_kind='derived_feature_dataset'
        ORDER BY feature.source_fixture_key
        """
    ).fetchall()
    assets = {(row["asset_id"], row["asset_kind"], row["sha256"], row["metadata_sha256"], row["input_hash"]) for row in rows}
    expected_asset = {("k1-core3-features-2025-2026", "derived_feature_dataset", K1_DATASET_SHA256, K1_METADATA_SHA256, K1_INPUT_HASH)}
    if assets != expected_asset:
        raise K1GuardrailError("K1 historical source asset contract mismatch")
    normalized = [_normalize_history_row(dict(row)) for row in rows]
    seasons = {season: sum(row["season"] == season for row in normalized) for season in {row["season"] for row in normalized}}
    if len(normalized) != 330 or len({row["fixture_id"] for row in normalized}) != 330 or seasons != {"2025": 228, "2026": 102}:
        raise K1GuardrailError("K1 historical row contract mismatch")
    if min(row["kickoff"] for row in normalized).isoformat() != "2025-02-15T04:00:00+00:00" or max(row["kickoff"] for row in normalized).isoformat() != "2026-07-12T10:30:00+00:00":
        raise K1GuardrailError("K1 historical kickoff range mismatch")
    cutoff = _utc(analysis["prediction_cutoff"])
    fixture_id = str(analysis["fixture_id"])
    available = [row for row in normalized if row["label_available_at"] <= cutoff and row["fixture_id"] != fixture_id]
    shape = _current_shape(analysis)
    level, filters, cohort, fallback = _select_cohort(available, shape, presentation.minimum_cohort_size)
    by_season = {season: _metrics([row for row in cohort if row["season"] == season], presentation, f"{level}|{season}") for season in ("2025", "2026")}
    if all(by_season[season]["fixture_count"] >= presentation.minimum_season_size for season in ("2025", "2026")):
        residuals = [by_season[season]["top_outcome_calibration_residual"] for season in ("2025", "2026")]
        temporal = {"status": "available", "temporal_direction_consistent": residuals[0] == 0 or residuals[1] == 0 or (residuals[0] > 0) == (residuals[1] > 0)}
    else:
        temporal = {"status": "insufficient_sample"}
    examples = []
    for row in sorted(cohort, key=lambda item: (-item["kickoff"].timestamp(), item["fixture_id"]))[:presentation.example_count]:
        examples.append({key: row[key] for key in (
            "fixture_id", "kickoff", "home", "away", "probabilities", "top_outcome",
            "top_probability", "probability_bin", "gap_bin", "line_bucket", "total_bucket",
            "home_goals", "away_goals", "actual_outcome",
        )})
    return {
        "status": "available", "dataset_sha256": K1_DATASET_SHA256,
        "metadata_sha256": K1_METADATA_SHA256, "input_hash": K1_INPUT_HASH,
        "comparison_scope": "final_closing_vs_as_of_cutoff_current", "context_only": True,
        "probability_adjustment": False, "guardrail_action_adjustment": False,
        "historical_available_at": cutoff.isoformat().replace("+00:00", "Z"),
        "available_historical_fixture_count": len(available),
        "latest_historical_label_available_at": max((row["label_available_at"] for row in available), default=None),
        "current_shape": shape,
        "selected_cohort": {"level": level, "filters": filters, "fixture_count": len(cohort), "fallback_applied": fallback},
        "overall_metrics": _metrics(available, presentation, "available-overall"),
        "cohort_metrics": _metrics(cohort, presentation, level),
        "by_season": by_season, "temporal_stability": temporal,
        "rule_proxy_context": _proxy_context(available, presentation), "examples": examples,
        "limitations": [
            "Historical data is final closing while the current market is as-of-cutoff.",
            "Historical context does not change probabilities, guardrail actions, or confidence.",
            "Company support ratios, exact R1/R2, R4, R5, and bookmaker MAD are unavailable.",
        ],
        "presentation_version": presentation.version,
        "presentation_file_sha256": presentation.file_sha256,
        "presentation_canonical_sha256": presentation.canonical_sha256,
    }


def _pct(value: Any) -> str:
    return "不可用" if value is None else f"{float(value) * 100:.2f}%"


def _number(value: Any, digits: int = 4) -> str:
    return "不可用" if value is None else f"{float(value):.{digits}f}"


def _direction_label(value: Any) -> str:
    return {"home": "主胜", "draw": "平局", "away": "客胜", None: "不输出方向"}.get(value, str(value))


def _rule_observation(rule: str, features: Mapping[str, Any]) -> str:
    if rule == "r0_data_integrity":
        counts = features.get("bookmaker_count_by_market") or {}
        return f"欧/亚/大小公司={counts.get('ouzhi', 0)}/{counts.get('yazhi', 0)}/{counts.get('daxiao', 0)}"
    if rule == "r1_shallow_favorite_cooling":
        return f"热门线={_number(features.get('current_favorite_line'), 2)}，概率变化={_pct(features.get('delta_p_favorite_median'))}，赔率变化={_number(features.get('delta_favorite_odds_median'), 3)}，支持率={_pct(features.get('favorite_cooling_support_ratio'))}"
    if rule == "r2_asian_retreat":
        return f"热门概率={_pct(features.get('favorite_probability'))}，方向差={_pct(features.get('prob_gap'))}，退盘={_number(features.get('delta_favorite_line_median'), 2)}，支持率={_pct(features.get('asian_retreat_support_ratio'))}"
    if rule == "r2_euro_strong_asian_flat":
        return f"热门线={_number(features.get('current_favorite_line'), 2)}，增强支持率={_pct(features.get('favorite_strengthening_support_ratio'))}，亚盘未增强={_pct(features.get('asian_not_strengthening_ratio'))}"
    if rule == "r3_low_total_draw_tail":
        probabilities = features.get("probabilities") or {}
        return f"大小球={_number(features.get('current_total_line'), 2)}，平局={_pct(probabilities.get('draw'))}，热门线={_number(features.get('current_favorite_line'), 2)}"
    if rule == "r4_handicap_cover_conflict":
        return f"有效公司={features.get('handicap_index_valid_bookmakers', 0)}，冲突支持率={_pct(features.get('handicap_index_conflict_support_ratio'))}"
    if rule == "r5_live_market_stability":
        return f"响应={features.get('live_observation_count', 0)}，跨度={features.get('live_observation_span_seconds', 0)}秒，盘口范围={_number(features.get('live_line_range'))}，概率范围={_pct(features.get('live_probability_range'))}"
    return f"公司MAD={_number(features.get('bookmaker_dispersion'))}"


def _rule_threshold(rule: str, thresholds: Mapping[str, Any]) -> str:
    values = {
        "r0_data_integrity": "三个核心市场各>=3家且引用无冲突",
        "r1_shallow_favorite_cooling": "浅盘；概率<=-1.5pp；赔率>=+0.03；两类支持率>=60%",
        "r2_asian_retreat": "热门>=45%；方向差>=7pp；退盘<=-0.25；支持率>=60%",
        "r2_euro_strong_asian_flat": "热门增强>=1pp；支持率>=60%；当前平手且亚盘未增强>=60%",
        "r3_low_total_draw_tail": "大小球<=2.25；平局>=28%；热门线绝对值<=0.50",
        "r4_handicap_cover_conflict": "至少3家公司；non-cover与cover差>=10%；支持率>=60%",
        "r5_live_market_stability": "至少3响应；跨度>=1800秒；盘口范围<=0.25；概率范围<=1.5pp",
        "r6_bookmaker_dispersion": "公司MAD>0.035",
    }
    return values[rule]


def render_k1_analysis(result: Mapping[str, Any], *, workspace: Path, summary: bool = False, audit: bool = False) -> str:
    context = result["analysis_context"]
    base = result["base_prediction"]
    history = result["historical_context"]
    guardrail = result["guardrail_assessment"]
    guarded = result["guarded_output"]
    lines = [
        f"# {context['home_team_name']} vs {context['away_team_name']}",
        "",
        f"切点：{context['target']}  |  开球：{context['kickoff_at']}  |  盘口口径：as_of_cutoff_current",
        "",
        "## 正常基础预测",
        "",
        "| 主胜 | 平局 | 客胜 | 基础方向 | 基础置信 |",
        "| ---: | ---: | ---: | --- | --- |",
        f"| {_pct(base['probabilities']['home'])} | {_pct(base['probabilities']['draw'])} | {_pct(base['probabilities']['away'])} | {_direction_label(base['direction'])} | {base['confidence_label']} |",
        "",
        "## 历史证据上下文",
        "",
    ]
    if history.get("status") != "available":
        lines.append(f"历史上下文不可用：{history.get('reason', 'unknown')}。基础预测与护栏结果不受影响。")
    else:
        cohort = history["selected_cohort"]
        metrics = history["cohort_metrics"]
        lines.extend([
            f"可用历史：{history['available_historical_fixture_count']}场；可比组：{cohort['level']}，{cohort['fixture_count']}场；回退：{'是' if cohort['fallback_applied'] else '否'}。",
            "",
            "| 历史口径 | 最高方向命中率 | 平局率 | 校准残差 |",
            "| --- | ---: | ---: | ---: |",
            f"| final_closing（仅作背景） | {_pct(metrics.get('actual_top_outcome_hit_rate'))} | {_pct(metrics.get('draw_rate'))} | {_pct(metrics.get('top_outcome_calibration_residual'))} |",
        ])
    if summary:
        matched = [name for name, value in guardrail["rule_evaluations"].items() if value.get("status") == "matched"]
        unavailable = [name for name, value in guardrail["rule_evaluations"].items() if value.get("status") == "not_evaluable"]
    else:
        presentation = load_k1_analysis_presentation(workspace)
        lines.extend(["## R0-R6执行详情", "", "| 规则 | 名称 | 状态 | 当前证据 | 阈值 | 历史可比性 |", "| --- | --- | --- | --- | --- | --- |"])
        proxies = history.get("rule_proxy_context") or {}
        features = guardrail["features"]
        for rule, evaluation in guardrail["rule_evaluations"].items():
            proxy = proxies.get(rule) or {}
            historical = proxy.get("historical_evaluability", "unavailable")
            if proxy.get("fixture_count") is not None:
                historical += f"（{proxy['fixture_count']}场）"
            rule_id = {"r2_asian_retreat": "R2a", "r2_euro_strong_asian_flat": "R2b"}.get(rule, rule.split("_", 1)[0].upper())
            lines.append(
                f"| {rule_id} | {presentation.rule_labels[rule]} | {presentation.status_labels[evaluation['status']]} | "
                f"{_rule_observation(rule, features)} | {_rule_threshold(rule, guardrail['thresholds'])} | {historical} |"
            )
        if history.get("status") == "available":
            lines.extend(["", "## 分赛季历史表现", "", "| 赛季 | 样本 | 最高方向命中率 | 平局率 | 校准残差 |", "| --- | ---: | ---: | ---: | ---: |"])
            for season in ("2025", "2026"):
                metrics = history["by_season"][season]
                lines.append(f"| {season} | {metrics['fixture_count']} | {_pct(metrics.get('actual_top_outcome_hit_rate'))} | {_pct(metrics.get('draw_rate'))} | {_pct(metrics.get('top_outcome_calibration_residual'))} |")
            lines.extend(["", "## 历史代表样例", "", "| 日期 | 比赛 | Closing概率（主/平/客） | 结构分箱 | 90分钟赛果 |", "| --- | --- | --- | --- | --- |"])
            for example in history["examples"]:
                probabilities = example["probabilities"]
                structure = f"P={example['probability_bin']} / Gap={example['gap_bin']} / AH={example['line_bucket']} / OU={example['total_bucket']}"
                lines.append(
                    f"| {str(example['kickoff'])[:10]} | {example['home']} vs {example['away']} | "
                    f"{_pct(probabilities['home'])}/{_pct(probabilities['draw'])}/{_pct(probabilities['away'])} | {structure} | "
                    f"{example['home_goals']}-{example['away_goals']}（{_direction_label(example['actual_outcome'])}） |"
                )
    lines.extend([
        "",
        "## K1护栏后方案",
        "",
        "| 基础方向 | 基础置信 | 护栏动作 | 护栏后方向 | 护栏后置信 |",
        "| --- | --- | --- | --- | --- |",
        f"| {_direction_label(base['direction'])} | {base['confidence_label']} | {guarded['action_label']} | {_direction_label(guarded['direction'])} | {guarded['confidence_label']} |",
        "",
    ])
    if summary:
        lines.append(f"触发规则：{', '.join(matched) or '无'}；不可评估：{', '.join(unavailable) or '无'}。")
    lines.extend([
        "",
        f"最终说明：{guarded['summary']}",
        "",
        "护栏动作只由当前盘口规则产生；历史数据只提供背景证据，不参与动作计算。",
        "历史数据为final closing，不代表当前正式预测切点。",
    ])
    if audit:
        lines.extend(["", "## 审计引用", "", "```json", json.dumps(result["audit_summary"], ensure_ascii=False, sort_keys=True, indent=2, default=str), "```"])
    return "\n".join(lines) + "\n"
