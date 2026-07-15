#!/usr/bin/env python3
"""Apply idempotent CentroQuote research compatibility fixes to OddsHarvester."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> tuple[str, bool]:
    if new in text:
        return text, False
    if old not in text:
        raise RuntimeError(f"Cannot apply {label}: expected source text was not found")
    return text.replace(old, new, 1), True


def package_root() -> Path:
    spec = importlib.util.find_spec("oddsharvester")
    if spec is None or spec.origin is None:
        raise RuntimeError("OddsHarvester is not installed in this Python environment")
    return Path(spec.origin).parent


def patch_validators(root: Path) -> bool:
    path = root / "cli" / "validators.py"
    text = path.read_text(encoding="utf-8")
    old = 'url_pattern = re.compile(r"https?://www\\.oddsportal\\.com/.+")'
    new = 'url_pattern = re.compile(r"https?://(www\\.oddsportal\\.com|www\\.centroquote\\.it)/.+")'
    text, changed = replace_once(text, old, new, "CentroQuote match-link validator")
    if changed:
        path.write_text(text, encoding="utf-8")
    return changed


def patch_italian_months(root: Path) -> bool:
    path = root / "core" / "base_scraper.py"
    text = path.read_text(encoding="utf-8")
    old_months = '    "dec": 12,\n}'
    new_months = '''    "dec": 12,
    # Italian abbreviations used by the centroquote.it mirror.
    "gen": 1,
    "mag": 5,
    "giu": 6,
    "lug": 7,
    "ago": 8,
    "set": 9,
    "ott": 10,
    "dic": 12,
}'''
    text, month_changed = replace_once(text, old_months, new_months, "Italian month map")

    old_parser = '''            local_dt = datetime.strptime(f"{date_part} {time_part}", "%d %b %Y %H:%M")'''
    new_parser = '''            date_tokens = date_part.split()
            if len(date_tokens) != 3:
                return None
            day_text, month_text, year_text = date_tokens
            month = _MONTH_ABBREV_TO_NUM.get(month_text[:3].lower())
            if month is None:
                return None
            hour_text, minute_text = time_part.split(":", 1)
            local_dt = datetime(
                int(year_text),
                month,
                int(day_text),
                int(hour_text),
                int(minute_text),
            )'''
    text, parser_changed = replace_once(text, old_parser, new_parser, "locale-independent DOM date parser")
    if month_changed or parser_changed:
        path.write_text(text, encoding="utf-8")
    return month_changed or parser_changed


def main() -> int:
    root = package_root()
    changes = {
        "match_link_validator": patch_validators(root),
        "italian_month_parser": patch_italian_months(root),
    }
    print(f"OddsHarvester package: {root}")
    for label, changed in changes.items():
        print(f"{label}: {'patched' if changed else 'already patched'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
