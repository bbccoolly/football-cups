from __future__ import annotations

import copy
import io
import math
import re
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
    error_type: str | None = None
    error: str | None = None


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def decode_page(response: ObservedResponse) -> str:
    for encoding in (response.source_encoding, "utf-8", "gb18030", "gb2312"):
        if not encoding or encoding == "unknown":
            continue
        try:
            return response.content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return response.content.decode("utf-8", errors="replace")


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
            else:
                page_url = f"{ODDS_BASE_URL}/fenxi/{market}-{fixture_id}.shtml"
                page_response = self.http.request("GET", page_url)
                raw_blobs.append(self.data.store_response(page_response, default_extension="html"))
                if not page_response.ok:
                    raise MarketCollectionError(f"HTTP {page_response.status_code}")
                page = decode_page(page_response)
                table_data = collect_download_table_data(page, market)
                try:
                    action, fields = parse_export_form(page)
                except SourceMarketUnavailable:
                    if market != "rangqiu":
                        raise
                    action = f"{ODDS_BASE_URL}/fenxi1/rangqiu_xls.php"
                    fields = {
                        "name": f"{fixture.get('home_team_name', '')}VS{fixture.get('away_team_name', '')}"
                    }
                fields.update(table_data)
                workbook_response = self.http.request(
                    "POST", action, headers={"Referer": page_url}, data=fields
                )
                raw_blobs.append(self.data.store_response(workbook_response, default_extension="xls"))
                if not workbook_response.ok:
                    raise MarketCollectionError(f"HTTP {workbook_response.status_code}")

            raw_sha = raw_blobs[-1]["sha256"]
            snapshot, rows = parse_market_workbook(
                workbook_response.content,
                fixture_id=fixture_id,
                market=market,
                target=target,
                observed_at=workbook_response.response_received_at,
                kickoff_at=fixture.get("kickoff_at"),
                timezone_name=self.config.timezone_name,
                raw_sha256=raw_sha,
            )
            return MarketCapture(market, "success", raw_blobs, snapshot, rows)
        except MarketCollectionError as exc:
            return MarketCapture(market, "failed", raw_blobs, None, [], exc.error_type, str(exc))
        except Exception as exc:  # noqa: BLE001 - one market must not abort the fixture batch.
            return MarketCapture(
                market,
                "failed",
                raw_blobs,
                None,
                [],
                "parser_failure",
                f"{type(exc).__name__}: {exc}",
            )
