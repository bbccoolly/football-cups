#!/usr/bin/env python3
"""Create an auditable research derivative from an OddsHarvester JSON sample."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def american_to_decimal(value: str | int | float) -> float:
    american = float(value)
    if american == 0:
        raise ValueError("American odds cannot be zero")
    decimal = 1 + (american / 100 if american > 0 else 100 / abs(american))
    return round(decimal, 6)


def correct_history_timestamp(
    raw_value: str,
    kickoff_utc: datetime,
    source_timezone: ZoneInfo,
    max_age: timedelta,
) -> tuple[str | None, str]:
    parsed = datetime.fromisoformat(raw_value)
    candidates = []
    for year in (kickoff_utc.year, kickoff_utc.year - 1):
        try:
            local_value = parsed.replace(year=year, tzinfo=source_timezone)
        except ValueError:
            continue
        candidate_utc = local_value.astimezone(UTC)
        age = kickoff_utc - candidate_utc
        if timedelta(0) <= age <= max_age:
            candidates.append((age, candidate_utc))

    if not candidates:
        return None, "quarantined"

    _, selected = min(candidates, key=lambda item: item[0])
    selected_local_year = selected.astimezone(source_timezone).year
    status = "year_corrected" if selected_local_year != parsed.year else "validated"
    return selected.isoformat().replace("+00:00", "Z"), status


def add_decimal_field(container: dict[str, Any], key: str) -> bool:
    value = container.get(key)
    if value is None or isinstance(value, bool):
        return False
    try:
        decimal = american_to_decimal(value)
    except (TypeError, ValueError):
        return False
    container[f"{key}_raw"] = value
    container[f"{key}_decimal"] = decimal
    return True


def derive_record(
    record: dict[str, Any],
    source_timezone: ZoneInfo,
    max_age: timedelta,
    record_index: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], int, int]:
    derived = deepcopy(record)
    kickoff_utc = datetime.strptime(record["match_date"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=UTC)
    quarantined: list[dict[str, Any]] = []
    odds_converted = 0
    timestamps_corrected = 0

    for market_key, market_rows in derived.items():
        if not market_key.endswith("_market") or not isinstance(market_rows, list):
            continue
        for row_index, row in enumerate(market_rows):
            if not isinstance(row, dict):
                continue
            for key in tuple(row):
                if key in {"bookmaker_name", "period", "odds_history_data"}:
                    continue
                odds_converted += int(add_decimal_field(row, key))

            history_groups = row.get("odds_history_data", [])
            for group_index, group in enumerate(history_groups):
                points = list(group.get("odds_history", []))
                if isinstance(group.get("opening_odds"), dict):
                    points.append(group["opening_odds"])
                for point_index, point in enumerate(points):
                    odds_converted += int(add_decimal_field(point, "odds"))
                    raw_timestamp = point.get("timestamp")
                    if not raw_timestamp:
                        continue
                    corrected, status = correct_history_timestamp(
                        raw_timestamp,
                        kickoff_utc,
                        source_timezone,
                        max_age,
                    )
                    point["timestamp_raw"] = raw_timestamp
                    point["timestamp_corrected_utc"] = corrected
                    point["timestamp_correction_status"] = status
                    if status == "year_corrected":
                        timestamps_corrected += 1
                    elif status == "quarantined":
                        quarantined.append(
                            {
                                "record_index": record_index,
                                "path": f"{market_key}/{row_index}/odds_history_data/{group_index}/{point_index}",
                                "raw_timestamp": raw_timestamp,
                                "reason": "no candidate timestamp is before kickoff and within max age",
                            }
                        )

    derived["_research_derivation"] = {
        "backfill": True,
        "strict_backtest_eligible": False,
        "source_odds_format": "american",
        "source_timezone": source_timezone.key,
    }
    return derived, quarantined, odds_converted, timestamps_corrected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-timezone", default="Europe/Rome")
    parser.add_argument("--max-age-days", type=int, default=180)
    args = parser.parse_args()

    raw_records = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(raw_records, list):
        raise ValueError("Expected a top-level JSON array")

    timezone = ZoneInfo(args.source_timezone)
    max_age = timedelta(days=args.max_age_days)
    records = []
    quarantine = []
    odds_converted = 0
    timestamps_corrected = 0
    for index, record in enumerate(raw_records):
        derived, rejected, converted_count, corrected_count = derive_record(
            record, timezone, max_age, index
        )
        records.append(derived)
        quarantine.extend(rejected)
        odds_converted += converted_count
        timestamps_corrected += corrected_count

    result = {
        "derivation": {
            "source_file": str(args.input),
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "records": len(records),
            "odds_converted": odds_converted,
            "timestamps_year_corrected": timestamps_corrected,
            "timestamps_quarantined": len(quarantine),
        },
        "records": records,
        "quarantine": quarantine,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["derivation"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
