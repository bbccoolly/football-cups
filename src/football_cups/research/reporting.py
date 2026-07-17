from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from football_cups.collector.storage import make_run_id

from .config import ResearchConfig
from .storage import ResearchStore


OUTCOMES = ("home", "draw", "away")


def _competition_label(record: dict[str, Any]) -> str:
    payload = record.get("source_payload")
    if isinstance(payload, dict) and payload.get("country") and payload.get("league"):
        return f"{payload['country']} / {str(payload['league']).strip()}"
    return str(record.get("competition") or "unknown")


def load_records(config: ResearchConfig) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(config.normalized_dir.rglob("*.jsonl")) if config.normalized_dir.is_dir() else []:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            record_id = record.get("record_id")
            if not isinstance(record_id, str) or not record_id:
                raise ValueError(f"{path}:{line_number}: missing record_id")
            prior = records.get(record_id)
            if prior is not None and prior != record:
                raise ValueError(f"conflicting normalized record: {record_id}")
            records[record_id] = record
    return records


def coverage_report(config: ResearchConfig) -> tuple[Path, dict[str, Any]]:
    records = load_records(config)
    by_type = Counter(record["record_type"] for record in records.values())
    fixtures = [record for record in records.values() if record["record_type"] == "ResearchFixture"]
    markets = [
        record for record in records.values() if record["record_type"] == "ResearchMarketObservation"
    ]
    features = [record for record in records.values() if record["record_type"] == "ResearchFeatureRow"]
    by_source = Counter(record.get("source_id", "unknown") for record in fixtures + features)
    by_year = Counter(str(record.get("match_date", ""))[:4] for record in fixtures + features)
    by_competition = Counter(_competition_label(record) for record in fixtures + features)
    by_cohort = Counter(record.get("cohort", "unknown") for record in markets + features)
    by_market = Counter(record.get("market", "unknown") for record in markets)
    eligible_results = sum(
        1 for record in fixtures + features if record.get("result_eligible") is True
    )
    payload = {
        "schema_version": 1,
        "research_only": True,
        "dataset_record_count": len(records),
        "record_types": dict(sorted(by_type.items())),
        "fixtures_and_feature_rows": len(fixtures) + len(features),
        "eligible_results": eligible_results,
        "by_source": dict(sorted(by_source.items())),
        "by_year": dict(sorted(by_year.items())),
        "by_competition": dict(sorted(by_competition.items())),
        "by_cohort": dict(sorted(by_cohort.items())),
        "by_market": dict(sorted(by_market.items())),
    }
    run_id = make_run_id()
    path = ResearchStore(config).write_report("coverage", run_id, payload)
    return path, payload


def _outcome(home_goals: int, away_goals: int) -> int:
    return 0 if home_goals > away_goals else 1 if home_goals == away_goals else 2


def _devig(values: dict[str, Any]) -> tuple[float, float, float] | None:
    try:
        inverse = [1.0 / float(values[key]) for key in OUTCOMES]
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    total = sum(inverse)
    if total <= 0 or not all(math.isfinite(value) for value in inverse):
        return None
    return tuple(value / total for value in inverse)  # type: ignore[return-value]


def _average_probabilities(items: Iterable[tuple[float, float, float]]) -> tuple[float, float, float] | None:
    values = list(items)
    if not values:
        return None
    return tuple(sum(item[index] for item in values) / len(values) for index in range(3))  # type: ignore[return-value]


def _metric_rows(points: list[dict[str, Any]], probability_key: str) -> dict[str, Any]:
    if not points:
        return {"sample_size": 0}
    losses: list[float] = []
    briers: list[float] = []
    rps_values: list[float] = []
    confidence: list[tuple[float, int]] = []
    for point in points:
        probabilities = point[probability_key]
        actual = point["actual"]
        losses.append(-math.log(max(1e-15, probabilities[actual])))
        briers.append(sum((probabilities[index] - (1 if index == actual else 0)) ** 2 for index in range(3)))
        cumulative_prediction = (probabilities[0], probabilities[0] + probabilities[1])
        cumulative_actual = (1.0 if actual == 0 else 0.0, 1.0 if actual <= 1 else 0.0)
        rps_values.append(sum((a - b) ** 2 for a, b in zip(cumulative_prediction, cumulative_actual)) / 2)
        predicted = max(range(3), key=lambda index: probabilities[index])
        confidence.append((max(probabilities), 1 if predicted == actual else 0))
    confidence.sort(key=lambda item: item[0])
    bins = min(10, len(confidence))
    ece = 0.0
    for index in range(bins):
        start = index * len(confidence) // bins
        end = (index + 1) * len(confidence) // bins
        bucket = confidence[start:end]
        if not bucket:
            continue
        mean_confidence = sum(item[0] for item in bucket) / len(bucket)
        accuracy = sum(item[1] for item in bucket) / len(bucket)
        ece += len(bucket) / len(confidence) * abs(mean_confidence - accuracy)
    return {
        "sample_size": len(points),
        "log_loss": sum(losses) / len(losses),
        "brier": sum(briers) / len(briers),
        "rps": sum(rps_values) / len(rps_values),
        "ece": ece,
        "ece_bins": bins,
        "calibration_conclusion": "available" if len(points) >= 100 else "insufficient_sample",
    }


def _git_head(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _market_points(records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    fixtures = {
        record_id: record
        for record_id, record in records.items()
        if record["record_type"] == "ResearchFixture"
        and record.get("result_eligible") is True
        and record.get("home_goals") is not None
        and record.get("away_goals") is not None
    }
    observations: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records.values():
        if record["record_type"] == "ResearchMarketObservation" and record.get("market") == "1x2":
            observations[(record["fixture_record_id"], record["cohort"])].append(record)
    points: list[dict[str, Any]] = []
    for (fixture_id, cohort), rows in observations.items():
        fixture = fixtures.get(fixture_id)
        if not fixture:
            continue
        preferred = [row for row in rows if row.get("bookmaker") == "Avg"] or [
            row for row in rows if row.get("bookmaker") not in {"Max", "Min"}
        ]
        probabilities = _average_probabilities(
            probability
            for probability in (_devig(row["values"]) for row in preferred)
            if probability is not None
        )
        if probabilities is None:
            continue
        points.append(
            {
                "fixture_id": fixture_id,
                "source_id": fixture["source_id"],
                "competition": _competition_label(fixture),
                "cohort": cohort,
                "match_date": fixture["match_date"],
                "actual": _outcome(int(fixture["home_goals"]), int(fixture["away_goals"])),
                "market": probabilities,
            }
        )
    return points


def _feature_points(records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    label_map = {"homeWin": 0, "draw": 1, "awayWin": 2}
    for record in records.values():
        if record["record_type"] != "ResearchFeatureRow" or not record.get("result_eligible"):
            continue
        features = record["features"]
        try:
            probabilities = tuple(
                float(features[key]) for key in ("implied_home", "implied_draw", "implied_away")
            )
            actual = label_map[features["actual_direction"]]
        except (KeyError, TypeError, ValueError):
            continue
        total = sum(probabilities)
        if total <= 0:
            continue
        points.append(
            {
                "fixture_id": record["record_id"],
                "source_id": record["source_id"],
                "competition": record["competition"],
                "cohort": record["cohort"],
                "match_date": record["match_date"],
                "actual": actual,
                "market": tuple(value / total for value in probabilities),
            }
        )
    return points


def _add_priors(points: list[dict[str, Any]]) -> None:
    competition_counts: dict[str, list[int]] = defaultdict(lambda: [1, 1, 1])
    for point in sorted(points, key=lambda value: (value["match_date"], value["fixture_id"])):
        counts = competition_counts[point["competition"]]
        total = sum(counts)
        point["uniform"] = (1 / 3, 1 / 3, 1 / 3)
        point["prior"] = tuple(value / total for value in counts)
        if str(point["match_date"])[:4] == "2025":
            counts[point["actual"]] += 1


def evaluate_baseline(config: ResearchConfig) -> tuple[Path, dict[str, Any]]:
    records = load_records(config)
    points = _market_points(records) + _feature_points(records)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        grouped[(point["source_id"], point["cohort"])].append(point)
    for group in grouped.values():
        _add_priors(group)

    metrics: list[dict[str, Any]] = []
    for (source_id, cohort), group in sorted(grouped.items()):
        dimensions = {("all", "all"): group}
        for year in ("2025", "2026"):
            dimensions[(year, "all")] = [point for point in group if point["match_date"][:4] == year]
        for competition in sorted({point["competition"] for point in group}):
            dimensions[("all", competition)] = [
                point for point in group if point["competition"] == competition
            ]
            for year in ("2025", "2026"):
                dimensions[(year, competition)] = [
                    point
                    for point in group
                    if point["competition"] == competition
                    and point["match_date"][:4] == year
                ]
        for (year, competition), selected in dimensions.items():
            for baseline in ("uniform", "prior", "market"):
                metrics.append(
                    {
                        "source_id": source_id,
                        "cohort": cohort,
                        "year": year,
                        "competition": competition,
                        "baseline": baseline,
                        **_metric_rows(selected, baseline),
                    }
                )

    record_ids = sorted(records)
    dataset_hash = hashlib.sha256("\n".join(record_ids).encode("ascii")).hexdigest()
    train_ids = sorted(
        {point["fixture_id"] for point in points if point["match_date"][:4] == "2025"}
    )
    evaluation_ids = sorted(
        {point["fixture_id"] for point in points if point["match_date"][:4] == "2026"}
    )
    if set(train_ids) & set(evaluation_ids):
        raise ValueError("training and time-separated evaluation fixtures overlap")
    payload = {
        "schema_version": 1,
        "research_only": True,
        "strict_backtest_eligible": False,
        "evaluation_kind": "retrospective_time_separated_not_blind",
        "dataset_hash": dataset_hash,
        "code_commit": _git_head(config.workspace),
        "configuration": {
            "fit_year": 2025,
            "evaluation_year": 2026,
            "ece_bins": 10,
            "baselines": ["uniform", "competition_prior", "devig_market"],
        },
        "training_fixture_ids": train_ids,
        "evaluation_fixture_ids": evaluation_ids,
        "metrics": metrics,
    }
    run_id = make_run_id()
    path = ResearchStore(config).write_report("evaluation", run_id, payload)
    return path, payload
