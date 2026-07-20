from __future__ import annotations

import copy
import io
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin

import pandas as pd
from lxml import html

from . import SCHEMA_VERSION
from .config import CollectorConfig
from .http import ObservedResponse, RateLimitedHttpClient
from .storage import DataStore, stable_record_id
from .timeutil import iso_utc, parse_iso


ODDS_BASE_URL = "https://odds.500.com"
MIN_VALID_EXCEL_BYTES = 2048
BLOCKED_MARKERS = ("请选择正确数据", "验证码", "登录/注册", "passport.500.com", "用户登录")
PARSER_VERSION = "500-market-v2"
NORMALIZATION_VERSION = 2
MIN_COMPLETE_BOOKMAKERS = 3
SUMMARY_BOOKMAKERS = frozenset({"最高值", "最低值", "平均值", "离散值"})
MOJIBAKE_MARKERS = ("脳卯", "脝陆", "脢脰", "赂脽", "碌脥", "脕垄", "脧茫")
META_CHARSET_PATTERN = re.compile(
    br"<meta[^>]+charset\s*=\s*['\"]?\s*([a-zA-Z0-9._-]+)", re.IGNORECASE
)
CONTENT_TYPE_CHARSET_PATTERN = re.compile(r"charset\s*=\s*([a-zA-Z0-9._-]+)", re.IGNORECASE)


class MarketCollectionError(RuntimeError):
    error_type = "market_failure"


class SourceMarketUnavailable(MarketCollectionError):
    error_type = "source_market_unavailable"


class InvalidExcel(MarketCollectionError):
    error_type = "invalid_excel"


class BlockedResponse(MarketCollectionError):
    error_type = "blocked_response"


@dataclass(frozen=True)
class MarketCapture:
    market: str
    status: str
    raw_blobs: list[dict[str, Any]]
    snapshot: dict[str, Any] | None
    rows: list[dict[str, Any]]
    normalization: dict[str, Any] | None = None
    error_type: str | None = None
    error: str | None = None


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _canonical_encoding(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().strip("'\"").lower().replace("_", "-")
    aliases = {
        "gb2312": "gb18030",
        "gbk": "gb18030",
        "gb-2312": "gb18030",
        "utf8": "utf-8",
        "latin-1": "iso-8859-1",
        "latin1": "iso-8859-1",
    }
    return aliases.get(normalized, normalized)


def _declared_encoding(content: bytes) -> str | None:
    match = META_CHARSET_PATTERN.search(content[:8192])
    if not match:
        return None
    return _canonical_encoding(match.group(1).decode("ascii", errors="ignore"))


def _header_encoding(response: ObservedResponse) -> str | None:
    match = CONTENT_TYPE_CHARSET_PATTERN.search(response.headers.get("content-type", ""))
    return _canonical_encoding(match.group(1)) if match else None


def _validate_decoded_market_page(text: str) -> None:
    if "\ufffd" in text:
        raise MarketCollectionError("decoded market page contains replacement characters")
    if any(marker in text for marker in MOJIBAKE_MARKERS):
        raise MarketCollectionError("decoded market page contains known mojibake markers")
    if "xls" not in text or "row" not in text:
        raise MarketCollectionError("decoded market page has no export table structure")


def decode_page_with_evidence(response: ObservedResponse) -> tuple[str, dict[str, str | None]]:
    declared = _declared_encoding(response.content)
    header = _header_encoding(response)
    inferred = _canonical_encoding(response.source_encoding)
    candidates: list[tuple[str, str]] = []
    for source, encoding in (
        ("meta", declared),
        ("content_type", header),
        ("gb18030_fallback", "gb18030"),
        ("utf8_fallback", "utf-8"),
        ("requests_inference", inferred),
    ):
        if not encoding or any(existing == encoding for _, existing in candidates):
            continue
        if encoding == "iso-8859-1":
            continue
        candidates.append((source, encoding))
    errors: list[str] = []
    for source, encoding in candidates:
        try:
            text = response.content.decode(encoding)
            _validate_decoded_market_page(text)
            return text, {
                "declared_encoding": declared,
                "header_encoding": header,
                "inferred_encoding": inferred,
                "selected_encoding": encoding,
                "encoding_source": source,
            }
        except (LookupError, UnicodeDecodeError, MarketCollectionError) as exc:
            errors.append(f"{source}:{encoding}:{exc}")
    raise MarketCollectionError("cannot safely decode market page: " + "; ".join(errors))


def decode_page(response: ObservedResponse) -> str:
    return decode_page_with_evidence(response)[0]


def content_looks_blocked(content: bytes) -> str | None:
    head = content[:8192]
    for encoding in ("utf-8", "gb18030", "gb2312"):
        text = head.decode(encoding, errors="ignore")
        for marker in BLOCKED_MARKERS:
            if marker in text:
                return marker
    return None


def has_hidden_style(node: html.HtmlElement) -> bool:
    style = (node.get("style") or "").replace(" ", "").lower()
    return (
        "display:none" in style
        or "visibility:hidden" in style
        or (node.tag.lower() == "input" and (node.get("type") or "").lower() == "hidden")
        or node.get("hidden") is not None
    )


def is_hidden(node: html.HtmlElement) -> bool:
    current: html.HtmlElement | None = node
    while current is not None:
        if has_hidden_style(current):
            return True
        current = current.getparent()
    return False


def class_contains(node: html.HtmlElement, class_name: str) -> bool:
    return class_name in (node.get("class") or "").split()


def text_for_row_node(node: html.HtmlElement, strip_hidden_descendants: bool) -> str:
    select_nodes = node.xpath(".//select")
    if select_nodes:
        selected = select_nodes[0].xpath(".//option[@selected]")
        option = selected[0] if selected else (select_nodes[0].xpath(".//option") or [None])[0]
        text = option.text_content() if option is not None else ""
    else:
        node_copy = copy.deepcopy(node)
        if strip_hidden_descendants:
            for hidden_node in list(node_copy.xpath(".//*")):
                if has_hidden_style(hidden_node):
                    parent = hidden_node.getparent()
                    if parent is not None:
                        parent.remove(hidden_node)
        text = node_copy.text_content()
    return normalize_space(text).replace("↑", "").replace("↓", "")


def collect_download_table_data(page: str, kind: str) -> dict[str, str]:
    tree = html.fromstring(page)
    datalist: dict[str, list[str]] = {}
    for container in tree.xpath("//*[@xls]"):
        if is_hidden(container):
            continue
        input_name = container.get("xls")
        if not input_name:
            continue
        datalist.setdefault(input_name, [])
        standard: list[str] = []
        opening: list[str] = []
        for row_node in container.xpath(".//*[@row]"):
            try:
                repeat_count = int(row_node.get("row") or "1")
            except ValueError:
                repeat_count = 1
            for _ in range(max(repeat_count, 1)):
                parent = row_node.getparent()
                if is_hidden(row_node) or (parent is not None and is_hidden(parent)):
                    continue
                value = text_for_row_node(row_node, strip_hidden_descendants=input_name == "row")
                parent_is_opening = parent is not None and class_contains(parent, "td_show_cp")
                if class_contains(row_node, "td_show_cp") or parent_is_opening:
                    opening.append(value)
                else:
                    standard.append(value)
        if opening:
            if kind == "rangqiu":
                opening.insert(0, standard[1] if len(standard) > 1 else "")
                opening.insert(0, standard[0] if standard else "")
            else:
                opening.insert(0, standard[0] if standard else "")
            datalist[input_name].append("|".join(opening))
        datalist[input_name].append("|".join(standard))
    body: dict[str, str] = {}
    names = ["header", "row"] if kind == "rangqiu" else ["header", "row", "footer"]
    for name in names:
        values = [value for value in datalist.get(name, []) if value]
        if values:
            body[name] = "$".join(values)
    if "header" not in body or "row" not in body:
        raise SourceMarketUnavailable("page contains no exportable header/row market data")
    return body


def parse_export_form(page: str) -> tuple[str, dict[str, str]]:
    tree = html.fromstring(page)
    forms = tree.xpath("//form[contains(@action, 'xls.php') or contains(@action, 'rangqiu_xls.php')]")
    if not forms:
        raise SourceMarketUnavailable("page has no Excel export form")
    form = forms[0]
    fields = {
        str(node.get("name")): node.get("value") or ""
        for node in form.xpath(".//input[@name]")
    }
    return urljoin(ODDS_BASE_URL, form.get("action") or ""), fields


def validate_workbook(content: bytes) -> pd.DataFrame:
    marker = content_looks_blocked(content)
    if marker:
        raise BlockedResponse(f"blocked marker: {marker}")
    if len(content) < MIN_VALID_EXCEL_BYTES:
        raise InvalidExcel(f"Excel response too small: {len(content)} bytes")
    try:
        workbook = pd.ExcelFile(io.BytesIO(content))
        if not workbook.sheet_names:
            raise InvalidExcel("workbook has no sheets")
        frame = pd.read_excel(io.BytesIO(content), sheet_name=workbook.sheet_names[0], header=None)
    except InvalidExcel:
        raise
    except Exception as exc:  # noqa: BLE001 - converted to a stable collector error.
        raise InvalidExcel(f"cannot read workbook: {type(exc).__name__}: {exc}") from exc
    frame = frame.dropna(how="all").dropna(axis=1, how="all")
    if frame.empty:
        raise InvalidExcel("workbook contains no data")
    return frame


def _raw_cell(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return str(value).strip()


def _decimal(value: Any) -> str | None:
    raw = _raw_cell(value)
    if raw is None or not raw:
        return None
    try:
        return format(Decimal(raw.replace(",", "")), "f")
    except InvalidOperation:
        return None


def _value(value: Any) -> dict[str, str | None]:
    return {"raw": _raw_cell(value), "decimal": _decimal(value)}


HANDICAP_BASE_VALUES: dict[str, Decimal] = {
    "平手": Decimal("0"),
    "半球": Decimal("0.5"),
    "一球": Decimal("1"),
    "球半": Decimal("1.5"),
    "两球": Decimal("2"),
    "两球半": Decimal("2.5"),
    "三球": Decimal("3"),
    "三球半": Decimal("3.5"),
    "四球": Decimal("4"),
    "四球半": Decimal("4.5"),
    "五球": Decimal("5"),
}


def normalize_company_name(value: Any) -> str | None:
    raw = _raw_cell(value)
    if raw is None:
        return None
    return normalize_space(unicodedata.normalize("NFKC", raw)) or None


def market_row_role(name: str | None) -> str:
    if not name:
        return "unknown"
    if name in SUMMARY_BOOKMAKERS:
        return "summary"
    if name == "竞彩官方":
        return "official"
    return "bookmaker"


def normalize_handicap_line(value: Any) -> tuple[dict[str, str | None], str, str | None]:
    raw = _raw_cell(value)
    result: dict[str, str | None] = {"raw": raw, "decimal": None}
    if not raw:
        return result, "none", "missing"
    text = normalize_space(unicodedata.normalize("NFKC", raw))
    movement = "none"
    movement_match = re.search(r"(?:\s*)(升|降)$", text)
    if movement_match:
        movement = "up" if movement_match.group(1) == "升" else "down"
        text = text[: movement_match.start()].strip()
    receiving = text.startswith("受")
    if receiving:
        text = text[1:]
    try:
        numeric = Decimal(text)
    except InvalidOperation:
        numeric = None
    if numeric is not None:
        if numeric < -10 or numeric > 10:
            return result, movement, "handicap_line_out_of_range"
        result["decimal"] = format(numeric, "f")
        return result, movement, None
    parts = [part.strip() for part in text.split("/")]
    if not parts or any(part not in HANDICAP_BASE_VALUES for part in parts):
        return result, movement, "unknown_handicap_line"
    magnitude = sum((HANDICAP_BASE_VALUES[part] for part in parts), Decimal("0")) / len(parts)
    normalized = magnitude if receiving else -magnitude
    result["decimal"] = format(normalized, "f")
    return result, movement, None


def normalize_total_line(value: Any) -> tuple[dict[str, str | None], str | None]:
    raw = _raw_cell(value)
    result: dict[str, str | None] = {"raw": raw, "decimal": None}
    if not raw:
        return result, "missing"
    text = normalize_space(unicodedata.normalize("NFKC", raw))
    try:
        parts = [Decimal(part.strip()) for part in text.split("/")]
    except InvalidOperation:
        return result, "unknown_total_line"
    if len(parts) not in {1, 2}:
        return result, "unknown_total_line"
    if len(parts) == 2 and abs(parts[0] - parts[1]) != Decimal("0.5"):
        return result, "invalid_total_split"
    normalized = sum(parts, Decimal("0")) / len(parts)
    if normalized < 0 or normalized > 20:
        return result, "total_line_out_of_range"
    result["decimal"] = format(normalized, "f")
    return result, None


def _source_time(raw: Any, kickoff_at: str | None, timezone_name: str) -> dict[str, str | None]:
    text = _raw_cell(raw)
    result: dict[str, str | None] = {
        "raw": text,
        "parsed": None,
        "inference": None,
    }
    if not text or not kickoff_at or not re.fullmatch(r"\d{2}-\d{2}\s+\d{2}:\d{2}", text):
        return result
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    kickoff = parse_iso(kickoff_at)
    month, day, hour, minute = map(int, re.split(r"[- :]+", text))
    candidate = dt(kickoff.year, month, day, hour, minute, tzinfo=ZoneInfo(timezone_name))
    if candidate.astimezone(kickoff.tzinfo) > kickoff + timedelta(days=1):
        candidate = candidate.replace(year=candidate.year - 1)
    result["parsed"] = iso_utc(candidate)
    result["inference"] = "year_inferred_from_kickoff"
    return result


def _complete_bookmaker_row(market: str, row: dict[str, Any]) -> bool:
    if row.get("row_role") != "bookmaker":
        return False
    opening = row.get("opening") or {}
    current = row.get("current") or {}
    keys = {
        "ouzhi": ("home", "draw", "away"),
        "yazhi": ("home", "line", "away"),
        "daxiao": ("over", "line", "under"),
    }.get(market, ())
    return bool(keys) and all(
        isinstance(container.get(key), dict) and container[key].get("decimal") is not None
        for container in (opening, current)
        for key in keys
    )


def _build_normalization(
    *,
    snapshot_record_id: str,
    fixture_id: str,
    market: str,
    target: str,
    normalized_at: datetime,
    source_page_sha256: str | None,
    source_workbook_sha256: str | None,
    source_page_observed_at: datetime | None,
    snapshot_observed_at: datetime,
    rows: list[dict[str, Any]],
    reprocessed: bool,
    decoding: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    if source_page_observed_at and source_page_observed_at > snapshot_observed_at:
        raise MarketCollectionError("source page observation is later than market snapshot")
    valid_names = {
        str(row["source_bookmaker_name"])
        for row in rows
        if _complete_bookmaker_row(market, row) and row.get("source_bookmaker_name")
    }
    bookmaker_rows = [row for row in rows if row.get("row_role") == "bookmaker"]
    source_time_rows = sum(
        1 for row in bookmaker_rows if (row.get("source_event_time") or {}).get("parsed")
    )
    line_failures = sum(
        1
        for row in rows
        if row.get("row_role") == "bookmaker"
        and market in {"yazhi", "daxiao"}
        and any(
            (row.get(section) or {}).get("line", {}).get("decimal") is None
            for section in ("opening", "current")
        )
    )
    source_hash = source_page_sha256 or source_workbook_sha256 or "none"
    record_id = stable_record_id(
        "market_normalization", snapshot_record_id, NORMALIZATION_VERSION, source_hash
    )
    reasons: list[str] = []
    if not rows:
        reasons.append("no_rows")
    if len(valid_names) < MIN_COMPLETE_BOOKMAKERS and market in {"ouzhi", "yazhi", "daxiao"}:
        reasons.append("insufficient_complete_bookmakers")
    if line_failures:
        reasons.append("line_parse_failures")
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "MarketNormalization",
        "record_id": record_id,
        "fixture_id": fixture_id,
        "snapshot_record_id": snapshot_record_id,
        "market": market,
        "target": target,
        "normalization_version": NORMALIZATION_VERSION,
        "parser_version": PARSER_VERSION,
        "normalized_at": iso_utc(normalized_at),
        "status": "accepted" if rows else "rejected",
        "valid_bookmaker_rows": len(valid_names),
        "bookmaker_rows": len(bookmaker_rows),
        "source_event_time_rows": source_time_rows,
        "line_parse_failure_count": line_failures,
        "source_page_sha256": source_page_sha256,
        "source_workbook_sha256": source_workbook_sha256,
        "source_page_observed_at": iso_utc(source_page_observed_at) if source_page_observed_at else None,
        "snapshot_observed_at": iso_utc(snapshot_observed_at),
        "quality_reasons": reasons,
        "decoding": decoding or {},
        "reprocessed": reprocessed,
        "event_origin": "reprocess" if reprocessed else "live",
    }


def parse_market_workbook(
    content: bytes,
    *,
    fixture_id: str,
    market: str,
    target: str,
    observed_at: datetime,
    kickoff_at: str | None,
    timezone_name: str,
    raw_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    frame = validate_workbook(content)
    matrix = [[_raw_cell(value) for value in row] for row in frame.itertuples(index=False, name=None)]
    rows: list[dict[str, Any]] = []

    if market in {"yazhi", "daxiao"}:
        labels = ("home", "line", "away") if market == "yazhi" else ("over", "line", "under")
        for index, values in enumerate(frame.itertuples(index=False, name=None)):
            if not values or _raw_cell(values[0]) in {None, "", "False"}:
                continue
            cells = list(values)
            if len(cells) < 4 or _decimal(cells[1]) is None or _decimal(cells[3]) is None:
                continue
            current = {
                labels[position]: _value(cells[position + 1] if len(cells) > position + 1 else None)
                for position in range(3)
            }
            opening = {
                labels[position]: _value(cells[position + 5] if len(cells) > position + 5 else None)
                for position in range(3)
            }
            record = {
                "schema_version": SCHEMA_VERSION,
                "record_type": "BookmakerMarketRow",
                "record_id": stable_record_id(
                    "bookmaker_row", fixture_id, market, target, iso_utc(observed_at), index, raw_sha256
                ),
                "fixture_id": fixture_id,
                "market": market,
                "target": target,
                "observed_at": iso_utc(observed_at),
                "source_bookmaker_name": _raw_cell(cells[0]),
                "source_bookmaker_id": None,
                "current": current,
                "opening": opening,
                "source_event_time": _source_time(
                    cells[4] if len(cells) > 4 else None, kickoff_at, timezone_name
                ),
                "opening_source_event_time": _source_time(
                    cells[8] if len(cells) > 8 else None, kickoff_at, timezone_name
                ),
                "corrected_at": None,
                "raw_cells": [_raw_cell(value) for value in cells],
            }
            rows.append(record)
    elif market == "ouzhi":
        index = 0
        values_list = list(frame.itertuples(index=False, name=None))
        while index < len(values_list):
            cells = list(values_list[index])
            company = _raw_cell(cells[0]) if cells else None
            if index >= 2 and company not in {None, "", "False"}:
                next_cells = list(values_list[index + 1]) if index + 1 < len(values_list) else []
                rows.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "record_type": "BookmakerMarketRow",
                        "record_id": stable_record_id(
                            "bookmaker_row", fixture_id, market, target, iso_utc(observed_at), index, raw_sha256
                        ),
                        "fixture_id": fixture_id,
                        "market": market,
                        "target": target,
                        "observed_at": iso_utc(observed_at),
                        "source_bookmaker_name": company,
                        "source_bookmaker_id": None,
                        "current": {
                            "home": _value(cells[1] if len(cells) > 1 else None),
                            "draw": _value(cells[2] if len(cells) > 2 else None),
                            "away": _value(cells[3] if len(cells) > 3 else None),
                        },
                        "opening": {
                            "home": _value(next_cells[1] if len(next_cells) > 1 else None),
                            "draw": _value(next_cells[2] if len(next_cells) > 2 else None),
                            "away": _value(next_cells[3] if len(next_cells) > 3 else None),
                        },
                        "source_event_time": {"raw": None, "parsed": None, "inference": None},
                        "opening_source_event_time": {"raw": None, "parsed": None, "inference": None},
                        "corrected_at": None,
                        "raw_cells": [_raw_cell(value) for value in cells],
                        "opening_raw_cells": [_raw_cell(value) for value in next_cells],
                    }
                )
                index += 2
                continue
            index += 1
    else:
        for index, cells in enumerate(matrix):
            if index < 1 or not any(value not in {None, "", "False"} for value in cells):
                continue
            rows.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "record_type": "BookmakerMarketRow",
                    "record_id": stable_record_id(
                        "market_row", fixture_id, market, target, iso_utc(observed_at), index, raw_sha256
                    ),
                    "fixture_id": fixture_id,
                    "market": market,
                    "target": target,
                    "observed_at": iso_utc(observed_at),
                    "source_bookmaker_name": None,
                    "source_bookmaker_id": None,
                    "row_role": "handicap_index",
                    "source_event_time": {"raw": None, "parsed": None, "inference": None},
                    "corrected_at": None,
                    "raw_cells": cells,
                }
            )

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "MarketSnapshot",
        "record_id": stable_record_id(
            "market_snapshot", fixture_id, market, target, iso_utc(observed_at), raw_sha256
        ),
        "fixture_id": fixture_id,
        "market": market,
        "target": target,
        "observed_at": iso_utc(observed_at),
        "ingested_at": None,
        "source_event_time": None,
        "corrected_at": None,
        "source_url": f"{ODDS_BASE_URL}/fenxi/{market}-{fixture_id}.shtml",
        "raw_sha256": raw_sha256,
        "row_count": len(rows),
        "bookmaker_count": sum(bool(row.get("source_bookmaker_name")) for row in rows),
        "parser_version": "500-market-v1",
        "parse_status": "success",
        "source_market_available": True,
        "raw_matrix": matrix,
    }
    return snapshot, rows


def _upgrade_bookmaker_rows_v2(
    rows: list[dict[str, Any]],
    *,
    snapshot_record_id: str,
    market: str,
    source_hash: str,
    normalized_at: datetime,
    source_page_sha256: str | None,
    source_workbook_sha256: str | None,
    source_page_observed_at: datetime | None,
    snapshot_observed_at: datetime,
    reprocessed: bool,
) -> list[dict[str, Any]]:
    upgraded: list[dict[str, Any]] = []
    for index, source in enumerate(rows):
        row = copy.deepcopy(source)
        name = normalize_company_name(row.get("source_bookmaker_name"))
        row["record_id"] = stable_record_id(
            "bookmaker_row_v2", snapshot_record_id, NORMALIZATION_VERSION, index, source_hash
        )
        row["source_bookmaker_name"] = name
        row["row_role"] = market_row_role(name)
        row["parser_version"] = PARSER_VERSION
        row["normalization_version"] = NORMALIZATION_VERSION
        row["normalized_at"] = iso_utc(normalized_at)
        row["source_snapshot_record_id"] = snapshot_record_id
        row["source_page_sha256"] = source_page_sha256
        row["source_workbook_sha256"] = source_workbook_sha256
        row["source_page_observed_at"] = (
            iso_utc(source_page_observed_at) if source_page_observed_at else None
        )
        row["snapshot_observed_at"] = iso_utc(snapshot_observed_at)
        row["source_row_index"] = index
        row["reprocessed"] = reprocessed
        row["event_origin"] = "reprocess" if reprocessed else "live"
        movements: dict[str, str] = {}
        if market == "yazhi":
            for section in ("opening", "current"):
                value, movement, _ = normalize_handicap_line((row.get(section) or {}).get("line", {}).get("raw"))
                row[section]["line"] = value
                movements[section] = movement
        elif market == "daxiao":
            for section in ("opening", "current"):
                value, _ = normalize_total_line((row.get(section) or {}).get("line", {}).get("raw"))
                row[section]["line"] = value
        row["line_movement"] = movements or None
        upgraded.append(row)
    return upgraded


def parse_market_workbook_v2(
    content: bytes,
    *,
    fixture_id: str,
    market: str,
    target: str,
    observed_at: datetime,
    kickoff_at: str | None,
    timezone_name: str,
    raw_sha256: str,
    normalized_at: datetime | None = None,
    source_snapshot_record_id: str | None = None,
    source_page_sha256: str | None = None,
    source_page_observed_at: datetime | None = None,
    reprocessed: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    snapshot, legacy_rows = parse_market_workbook(
        content,
        fixture_id=fixture_id,
        market=market,
        target=target,
        observed_at=observed_at,
        kickoff_at=kickoff_at,
        timezone_name=timezone_name,
        raw_sha256=raw_sha256,
    )
    snapshot_record_id = source_snapshot_record_id or str(snapshot["record_id"])
    if source_snapshot_record_id:
        snapshot["record_id"] = source_snapshot_record_id
    normalized = normalized_at or observed_at
    rows = _upgrade_bookmaker_rows_v2(
        legacy_rows,
        snapshot_record_id=snapshot_record_id,
        market=market,
        source_hash=source_page_sha256 or raw_sha256,
        normalized_at=normalized,
        source_page_sha256=source_page_sha256,
        source_workbook_sha256=raw_sha256,
        source_page_observed_at=source_page_observed_at,
        snapshot_observed_at=observed_at,
        reprocessed=reprocessed,
    )
    normalization = _build_normalization(
        snapshot_record_id=snapshot_record_id,
        fixture_id=fixture_id,
        market=market,
        target=target,
        normalized_at=normalized,
        source_page_sha256=source_page_sha256,
        source_workbook_sha256=raw_sha256,
        source_page_observed_at=source_page_observed_at,
        snapshot_observed_at=observed_at,
        rows=rows,
        reprocessed=reprocessed,
    )
    for row in rows:
        row["normalization_record_id"] = normalization["record_id"]
    snapshot["parser_version"] = PARSER_VERSION
    snapshot["normalization_record_id"] = normalization["record_id"]
    return snapshot, rows, normalization


def _split_export_rows(body: dict[str, str], *, include_footer: bool = False) -> list[list[str | None]]:
    sections = [body.get("row", "")]
    if include_footer:
        sections.append(body.get("footer", ""))
    combined = "$".join(section for section in sections if section)
    return [
        [cell.strip() or None for cell in row.split("|")]
        for row in combined.split("$")
        if row
    ]


def _percent_decimal(value: Any) -> str | None:
    raw = _raw_cell(value)
    if not raw:
        return None
    return _decimal(raw.removesuffix("%"))


def parse_market_html_v2(
    response: ObservedResponse,
    *,
    fixture_id: str,
    market: str,
    target: str,
    kickoff_at: str | None,
    timezone_name: str,
    raw_sha256: str,
    normalized_at: datetime | None = None,
    source_snapshot_record_id: str | None = None,
    snapshot_observed_at: datetime | None = None,
    reprocessed: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    page, decoding = decode_page_with_evidence(response)
    body = collect_download_table_data(page, market)
    matrix = _split_export_rows(body, include_footer=market in {"yazhi", "daxiao"})
    observed = snapshot_observed_at or response.response_received_at
    if response.response_received_at > observed:
        raise MarketCollectionError("source page observation is later than market snapshot")
    snapshot_record_id = source_snapshot_record_id or stable_record_id(
        "market_snapshot", fixture_id, market, target, iso_utc(observed), raw_sha256
    )
    normalized = normalized_at or observed
    rows: list[dict[str, Any]] = []
    if market in {"yazhi", "daxiao"}:
        labels = ("home", "line", "away") if market == "yazhi" else ("over", "line", "under")
        for index, cells in enumerate(matrix):
            if len(cells) < 9 or _decimal(cells[1]) is None or _decimal(cells[3]) is None:
                continue
            name = normalize_company_name(cells[0])
            current: dict[str, dict[str, str | None]] = {
                labels[0]: _value(cells[1]),
                labels[2]: _value(cells[3]),
            }
            opening: dict[str, dict[str, str | None]] = {
                labels[0]: _value(cells[5]),
                labels[2]: _value(cells[7]),
            }
            movements: dict[str, str] | None = None
            if market == "yazhi":
                current_line, current_movement, _ = normalize_handicap_line(cells[2])
                opening_line, opening_movement, _ = normalize_handicap_line(cells[6])
                movements = {"current": current_movement, "opening": opening_movement}
            else:
                current_line, _ = normalize_total_line(cells[2])
                opening_line, _ = normalize_total_line(cells[6])
            current["line"] = current_line
            opening["line"] = opening_line
            rows.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "record_type": "BookmakerMarketRow",
                    "record_id": stable_record_id(
                        "bookmaker_row_v2", snapshot_record_id, NORMALIZATION_VERSION, index, raw_sha256
                    ),
                    "fixture_id": fixture_id,
                    "market": market,
                    "target": target,
                    "observed_at": iso_utc(observed),
                    "source_bookmaker_name": name,
                    "source_bookmaker_id": None,
                    "row_role": market_row_role(name),
                    "current": current,
                    "opening": opening,
                    "source_event_time": _source_time(cells[4], kickoff_at, timezone_name),
                    "opening_source_event_time": _source_time(cells[8], kickoff_at, timezone_name),
                    "corrected_at": None,
                    "raw_cells": cells,
                    "parser_version": PARSER_VERSION,
                    "normalization_version": NORMALIZATION_VERSION,
                    "normalized_at": iso_utc(normalized),
                    "source_snapshot_record_id": snapshot_record_id,
                    "source_page_sha256": raw_sha256,
                    "source_workbook_sha256": None,
                    "source_page_observed_at": iso_utc(response.response_received_at),
                    "snapshot_observed_at": iso_utc(observed),
                    "source_row_index": index,
                    "line_movement": movements,
                    "reprocessed": reprocessed,
                    "event_origin": "reprocess" if reprocessed else "live",
                }
            )
    elif market == "rangqiu":
        for index, cells in enumerate(matrix):
            if len(cells) < 12 or _decimal(cells[2]) is None:
                continue
            name = normalize_company_name(cells[0])
            rows.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "record_type": "HandicapIndexRow",
                    "record_id": stable_record_id(
                        "handicap_index_row_v2", snapshot_record_id, NORMALIZATION_VERSION, index, raw_sha256
                    ),
                    "fixture_id": fixture_id,
                    "market": market,
                    "target": target,
                    "observed_at": iso_utc(observed),
                    "source_bookmaker_name": name,
                    "handicap_line": _decimal(cells[1]),
                    "home_index": _decimal(cells[2]),
                    "draw_index": _decimal(cells[3]),
                    "away_index": _decimal(cells[4]),
                    "home_probability": _percent_decimal(cells[5]),
                    "draw_probability": _percent_decimal(cells[6]),
                    "away_probability": _percent_decimal(cells[7]),
                    "return_rate": _percent_decimal(cells[8]),
                    "home_kelly": _decimal(cells[9]),
                    "draw_kelly": _decimal(cells[10]),
                    "away_kelly": _decimal(cells[11]),
                    "raw_cells": cells,
                    "parser_version": PARSER_VERSION,
                    "normalization_version": NORMALIZATION_VERSION,
                    "normalized_at": iso_utc(normalized),
                    "source_snapshot_record_id": snapshot_record_id,
                    "source_page_sha256": raw_sha256,
                    "source_page_observed_at": iso_utc(response.response_received_at),
                    "snapshot_observed_at": iso_utc(observed),
                    "source_row_index": index,
                    "reprocessed": reprocessed,
                    "event_origin": "reprocess" if reprocessed else "live",
                }
            )
    else:
        raise ValueError(f"HTML market parser does not support {market}")
    normalization = _build_normalization(
        snapshot_record_id=snapshot_record_id,
        fixture_id=fixture_id,
        market=market,
        target=target,
        normalized_at=normalized,
        source_page_sha256=raw_sha256,
        source_workbook_sha256=None,
        source_page_observed_at=response.response_received_at,
        snapshot_observed_at=observed,
        rows=rows,
        reprocessed=reprocessed,
        decoding=decoding,
    )
    for row in rows:
        row["normalization_record_id"] = normalization["record_id"]
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "MarketSnapshot",
        "record_id": snapshot_record_id,
        "fixture_id": fixture_id,
        "market": market,
        "target": target,
        "observed_at": iso_utc(observed),
        "ingested_at": None,
        "source_event_time": None,
        "corrected_at": None,
        "source_url": response.url,
        "raw_sha256": raw_sha256,
        "row_count": len(rows),
        "bookmaker_count": len(
            {row.get("source_bookmaker_name") for row in rows if row.get("row_role") == "bookmaker"}
        ),
        "parser_version": PARSER_VERSION,
        "parse_status": "success",
        "source_market_available": True,
        "raw_matrix": matrix,
        "decoding": decoding,
        "normalization_record_id": normalization["record_id"],
    }
    return snapshot, rows, normalization


class MarketCollector:
    def __init__(
        self,
        config: CollectorConfig,
        http_client: RateLimitedHttpClient,
        data_store: DataStore,
    ) -> None:
        self.config = config
        self.http = http_client
        self.data = data_store

    def collect(self, fixture: dict[str, Any], market: str, target: str) -> MarketCapture:
        fixture_id = str(fixture["fixture_id"])
        raw_blobs: list[dict[str, Any]] = []
        try:
            if market == "ouzhi":
                response = self.http.request(
                    "POST",
                    f"{ODDS_BASE_URL}/fenxi/europe_xls.php",
                    headers={"Referer": f"{ODDS_BASE_URL}/fenxi/ouzhi-{fixture_id}.shtml"},
                    data={
                        "fixtureid": fixture_id,
                        "excelst": "1",
                        "style": "0",
                        "ctype": "1",
                        "dcid": "",
                        "scid": "",
                        "r": "1",
                    },
                )
                raw_blobs.append(self.data.store_response(response, default_extension="xls"))
                if not response.ok:
                    raise MarketCollectionError(f"HTTP {response.status_code}")
                workbook_response = response
                raw_sha = raw_blobs[-1]["sha256"]
                snapshot, rows, normalization = parse_market_workbook_v2(
                    workbook_response.content,
                    fixture_id=fixture_id,
                    market=market,
                    target=target,
                    observed_at=workbook_response.response_received_at,
                    kickoff_at=fixture.get("kickoff_at"),
                    timezone_name=self.config.timezone_name,
                    raw_sha256=raw_sha,
                )
            else:
                page_url = f"{ODDS_BASE_URL}/fenxi/{market}-{fixture_id}.shtml"
                page_response = self.http.request("GET", page_url)
                raw_blobs.append(self.data.store_response(page_response, default_extension="html"))
                if not page_response.ok:
                    raise MarketCollectionError(f"HTTP {page_response.status_code}")
                raw_sha = raw_blobs[-1]["sha256"]
                snapshot, rows, normalization = parse_market_html_v2(
                    page_response,
                    fixture_id=fixture_id,
                    market=market,
                    target=target,
                    kickoff_at=fixture.get("kickoff_at"),
                    timezone_name=self.config.timezone_name,
                    raw_sha256=raw_sha,
                )
            return MarketCapture(
                market=market,
                status="success",
                raw_blobs=raw_blobs,
                snapshot=snapshot,
                rows=rows,
                normalization=normalization,
            )
        except MarketCollectionError as exc:
            return MarketCapture(
                market=market,
                status="failed",
                raw_blobs=raw_blobs,
                snapshot=None,
                rows=[],
                normalization=None,
                error_type=exc.error_type,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - one market must not abort the fixture batch.
            return MarketCapture(
                market=market,
                status="failed",
                raw_blobs=raw_blobs,
                snapshot=None,
                rows=[],
                normalization=None,
                error_type="parser_failure",
                error=f"{type(exc).__name__}: {exc}",
            )
