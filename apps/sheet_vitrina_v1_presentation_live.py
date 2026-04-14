"""Локальный live presentation pass для новой Google Sheets-витрины V1."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]

TARGET_SPREADSHEET_ID = "1ltgE8GltN3Rk8qP1UiaT2NPEwQyPKZ-1tuIqV7EC1NE"
TARGET_SCRIPT_ID = "1QalhdgdmpxekaTMbNEZM1ubLSPKkTYZ53SHacqBU9HRVJQgEKRdHkgSf"
TARGET_SPREADSHEET_NAME = "WB Core Vitrina V1"

DATA_SHEET_NAME = "DATA_VITRINA"
STATUS_SHEET_NAME = "STATUS"

DATA_LABEL_WIDTH = 280
DATA_KEY_WIDTH = 220
DATA_DATE_WIDTH = 96
STATUS_WIDTHS = {
    "A": 190,
    "B": 110,
    "C": 110,
    "D": 110,
    "E": 110,
    "F": 110,
    "G": 110,
    "H": 120,
    "I": 120,
    "J": 150,
    "K": 260,
}

HEADER_BACKGROUND = "#ffffff"
HEADER_FONT_COLOR = "#000000"
DATE_PATTERN = "dd.mm.yyyy"
PERCENT_PATTERN = "0.0%"
INTEGER_PATTERN = "#,##0"
DECIMAL_PATTERN = "#,##0.00"
TEXT_PATTERN = "@"


def _load_clasp_config() -> dict[str, Any]:
    return json.loads((ROOT / ".clasp.json").read_text(encoding="utf-8"))


def _load_clasp_profile() -> dict[str, Any]:
    config = json.loads((Path.home() / ".clasprc.json").read_text(encoding="utf-8"))
    profile = config.get("tokens", {}).get("default")
    if not isinstance(profile, dict):
        raise ValueError("missing default clasp profile in ~/.clasprc.json")
    return profile


def _verify_target_config() -> None:
    config = _load_clasp_config()
    if config.get("scriptId") != TARGET_SCRIPT_ID:
        raise ValueError("unexpected scriptId in .clasp.json")
    if config.get("parentId") != TARGET_SPREADSHEET_ID:
        raise ValueError("unexpected parentId in .clasp.json")
    if config.get("rootDir") != "gas/sheet_vitrina_v1":
        raise ValueError("unexpected rootDir in .clasp.json")


def _run_command(args: list[str], *, input_text: str | None = None) -> str:
    completed = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        input=input_text,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def _curl_json(url: str, *, method: str = "GET", token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    args = ["curl", "-sS", "-X", method, "-H", f"Authorization: Bearer {token}"]
    if payload is not None:
        args.extend(["-H", "Content-Type: application/json; charset=utf-8", "--data-binary", json.dumps(payload, ensure_ascii=False)])
    args.extend(["-w", "\nHTTP_STATUS:%{http_code}", url])
    raw = _run_command(args)
    body, status = raw.rsplit("\nHTTP_STATUS:", 1)
    code = int(status)
    if code >= 400:
        raise RuntimeError(f"HTTP {code}: {body.strip()}")
    if not body.strip():
        return {}
    return json.loads(body)


def _refresh_access_token() -> str:
    profile = _load_clasp_profile()
    payload = json.loads(
        _run_command(
            [
                "curl",
                "-sS",
                "https://oauth2.googleapis.com/token",
                "-d",
                f"client_id={profile['client_id']}",
                "-d",
                f"client_secret={profile['client_secret']}",
                "-d",
                f"refresh_token={profile['refresh_token']}",
                "-d",
                "grant_type=refresh_token",
            ]
        )
    )
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError(f"unable to refresh access token: {payload}")
    return access_token


def _rgb_from_hex(value: str) -> dict[str, float]:
    stripped = value.removeprefix("#")
    return {
        "red": int(stripped[0:2], 16) / 255,
        "green": int(stripped[2:4], 16) / 255,
        "blue": int(stripped[4:6], 16) / 255,
    }


def _sheet_range(sheet_id: int, *, start_row: int | None = None, end_row: int | None = None, start_column: int | None = None, end_column: int | None = None) -> dict[str, int]:
    result: dict[str, int] = {"sheetId": sheet_id}
    if start_row is not None:
        result["startRowIndex"] = start_row
    if end_row is not None:
        result["endRowIndex"] = end_row
    if start_column is not None:
        result["startColumnIndex"] = start_column
    if end_column is not None:
        result["endColumnIndex"] = end_column
    return result


def _number_format(pattern: str, number_type: str) -> dict[str, str]:
    return {"type": number_type, "pattern": pattern}


def _column_name(index: int) -> str:
    out = ""
    current = index
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        out = chr(65 + remainder) + out
    return out


def _get_spreadsheet_metadata(token: str) -> dict[str, Any]:
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{TARGET_SPREADSHEET_ID}"
        f"?fields=properties.title,sheets.properties(sheetId,title,gridProperties.frozenRowCount,gridProperties.frozenColumnCount)"
    )
    payload = _curl_json(url, token=token)
    title = payload.get("properties", {}).get("title")
    if title != TARGET_SPREADSHEET_NAME:
        raise AssertionError(f"unexpected spreadsheet title: {title}")
    sheets = payload.get("sheets", [])
    by_name = {}
    for sheet in sheets:
        properties = sheet.get("properties", {})
        name = properties.get("title")
        if isinstance(name, str):
            by_name[name] = properties
    for name in (DATA_SHEET_NAME, STATUS_SHEET_NAME):
        if name not in by_name:
            raise AssertionError(f"missing required sheet: {name}")
    return by_name


def _batch_get_values(token: str) -> dict[str, list[list[Any]]]:
    ranges = [f"{DATA_SHEET_NAME}!A1:ZZ", f"{STATUS_SHEET_NAME}!A1:ZZ"]
    query = "&".join(f"ranges={quote(item, safe='!A:Z0-9')}" for item in ranges)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{TARGET_SPREADSHEET_ID}/values:batchGet?{query}"
        "&majorDimension=ROWS"
        "&valueRenderOption=UNFORMATTED_VALUE"
        "&dateTimeRenderOption=SERIAL_NUMBER"
    )
    payload = _curl_json(url, token=token)
    out: dict[str, list[list[Any]]] = {}
    for item in payload.get("valueRanges", []):
        range_name = item.get("range", "")
        if isinstance(range_name, str) and "!" in range_name:
            out[range_name.split("!", 1)[0]] = item.get("values", [])
    return out


def _build_presentation_requests(sheet_props: dict[str, Any], values: dict[str, list[list[Any]]]) -> list[dict[str, Any]]:
    data_values = values[DATA_SHEET_NAME]
    status_values = values[STATUS_SHEET_NAME]

    data_sheet_id = sheet_props[DATA_SHEET_NAME]["sheetId"]
    status_sheet_id = sheet_props[STATUS_SHEET_NAME]["sheetId"]

    data_last_row = len(data_values)
    data_last_col = max(len(row) for row in data_values)
    status_last_row = len(status_values)
    status_last_col = max(len(row) for row in status_values)

    if data_last_row < 2 or data_last_col < 3:
        raise AssertionError("unexpected DATA_VITRINA shape")
    if status_last_row < 2 or status_last_col < len(STATUS_WIDTHS):
        raise AssertionError("unexpected STATUS shape")

    header_format = {
        "backgroundColor": _rgb_from_hex(HEADER_BACKGROUND),
        "textFormat": {
            "foregroundColor": _rgb_from_hex(HEADER_FONT_COLOR),
            "bold": True,
        },
        "verticalAlignment": "MIDDLE",
        "horizontalAlignment": "LEFT",
    }

    requests: list[dict[str, Any]] = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": data_sheet_id,
                    "gridProperties": {
                        "frozenColumnCount": 2,
                    },
                },
                "fields": "gridProperties.frozenColumnCount",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": status_sheet_id,
                    "gridProperties": {
                        "frozenRowCount": 1,
                    },
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": data_sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 1,
                },
                "properties": {"pixelSize": DATA_LABEL_WIDTH},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": data_sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 1,
                    "endIndex": 2,
                },
                "properties": {"pixelSize": DATA_KEY_WIDTH},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": data_sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 2,
                    "endIndex": data_last_col,
                },
                "properties": {"pixelSize": DATA_DATE_WIDTH},
                "fields": "pixelSize",
            }
        },
        {
            "repeatCell": {
                "range": _sheet_range(data_sheet_id, start_row=1, end_row=data_last_row, start_column=0, end_column=data_last_col),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _rgb_from_hex("#ffffff"),
                        "textFormat": {
                            "foregroundColor": _rgb_from_hex("#000000"),
                            "bold": False,
                        },
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": _sheet_range(data_sheet_id, start_row=0, end_row=1, start_column=0, end_column=data_last_col),
                "cell": {"userEnteredFormat": header_format},
                "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,horizontalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": _sheet_range(data_sheet_id, start_row=0, end_row=1, start_column=2, end_column=data_last_col),
                "cell": {
                    "userEnteredFormat": {
                        "horizontalAlignment": "CENTER",
                        "numberFormat": _number_format(DATE_PATTERN, "DATE"),
                    }
                },
                "fields": "userEnteredFormat(horizontalAlignment,numberFormat)",
            }
        },
        {
            "repeatCell": {
                "range": _sheet_range(data_sheet_id, start_row=1, end_row=data_last_row, start_column=0, end_column=2),
                "cell": {
                    "userEnteredFormat": {
                        "horizontalAlignment": "LEFT",
                    }
                },
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        },
        {
            "repeatCell": {
                "range": _sheet_range(data_sheet_id, start_row=1, end_row=data_last_row, start_column=2, end_column=data_last_col),
                "cell": {
                    "userEnteredFormat": {
                        "horizontalAlignment": "RIGHT",
                    }
                },
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        },
        {
            "repeatCell": {
                "range": _sheet_range(status_sheet_id, start_row=0, end_row=1, start_column=0, end_column=status_last_col),
                "cell": {"userEnteredFormat": header_format},
                "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,horizontalAlignment)",
            }
        },
    ]

    for column_index, width in enumerate(STATUS_WIDTHS.values()):
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": status_sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": column_index,
                        "endIndex": column_index + 1,
                    },
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            }
        )

    requests.extend(
        [
            {
                "repeatCell": {
                    "range": _sheet_range(status_sheet_id, start_row=1, end_row=status_last_row, start_column=0, end_column=1),
                    "cell": {
                        "userEnteredFormat": {
                            "horizontalAlignment": "LEFT",
                        }
                    },
                    "fields": "userEnteredFormat.horizontalAlignment",
                }
            },
            {
                "repeatCell": {
                    "range": _sheet_range(status_sheet_id, start_row=1, end_row=status_last_row, start_column=1, end_column=2),
                    "cell": {
                        "userEnteredFormat": {
                            "horizontalAlignment": "CENTER",
                            "textFormat": {"bold": True},
                        }
                    },
                    "fields": "userEnteredFormat(horizontalAlignment,textFormat.bold)",
                }
            },
            {
                "repeatCell": {
                    "range": _sheet_range(status_sheet_id, start_row=1, end_row=status_last_row, start_column=2, end_column=7),
                    "cell": {
                        "userEnteredFormat": {
                            "horizontalAlignment": "CENTER",
                            "numberFormat": _number_format(DATE_PATTERN, "DATE"),
                        }
                    },
                    "fields": "userEnteredFormat(horizontalAlignment,numberFormat)",
                }
            },
            {
                "repeatCell": {
                    "range": _sheet_range(status_sheet_id, start_row=1, end_row=status_last_row, start_column=7, end_column=9),
                    "cell": {
                        "userEnteredFormat": {
                            "horizontalAlignment": "CENTER",
                            "numberFormat": _number_format(INTEGER_PATTERN, "NUMBER"),
                        }
                    },
                    "fields": "userEnteredFormat(horizontalAlignment,numberFormat)",
                }
            },
            {
                "repeatCell": {
                    "range": _sheet_range(status_sheet_id, start_row=1, end_row=status_last_row, start_column=9, end_column=status_last_col),
                    "cell": {
                        "userEnteredFormat": {
                            "horizontalAlignment": "LEFT",
                        }
                    },
                    "fields": "userEnteredFormat.horizontalAlignment",
                }
            },
        ]
    )

    for row_index, row in enumerate(data_values[1:], start=1):
        key = str(row[1]) if len(row) > 1 else ""
        label = str(row[0]) if row else ""
        if not label and not key:
            requests.append(
                {
                    "repeatCell": {
                        "range": _sheet_range(data_sheet_id, start_row=row_index, end_row=row_index + 1, start_column=2, end_column=data_last_col),
                        "cell": {
                            "userEnteredFormat": {
                                "horizontalAlignment": "LEFT",
                                "numberFormat": _number_format(TEXT_PATTERN, "TEXT"),
                            }
                        },
                        "fields": "userEnteredFormat(horizontalAlignment,numberFormat)",
                    }
                }
            )
            continue
        if _is_block_key(key):
            requests.append(
                {
                    "repeatCell": {
                        "range": _sheet_range(data_sheet_id, start_row=row_index, end_row=row_index + 1, start_column=0, end_column=data_last_col),
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {"bold": True},
                            }
                        },
                        "fields": "userEnteredFormat.textFormat.bold",
                    }
                }
            )
            requests.append(
                {
                    "repeatCell": {
                        "range": _sheet_range(data_sheet_id, start_row=row_index, end_row=row_index + 1, start_column=2, end_column=data_last_col),
                        "cell": {
                            "userEnteredFormat": {
                                "horizontalAlignment": "LEFT",
                                "numberFormat": _number_format(TEXT_PATTERN, "TEXT"),
                            }
                        },
                        "fields": "userEnteredFormat(horizontalAlignment,numberFormat)",
                    }
                }
            )
            continue
        pattern = _resolve_data_pattern(key)
        requests.append(
            {
                "repeatCell": {
                    "range": _sheet_range(data_sheet_id, start_row=row_index, end_row=row_index + 1, start_column=2, end_column=data_last_col),
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": pattern,
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
        )

    return requests


def _resolve_data_pattern(key: str) -> dict[str, str]:
    metric_key = _normalize_metric_key(key)
    if _is_percent_metric(metric_key):
        return _number_format(PERCENT_PATTERN, "PERCENT")
    if _is_decimal_metric(metric_key) or _is_currency_metric(metric_key):
        return _number_format(DECIMAL_PATTERN, "NUMBER")
    return _number_format(INTEGER_PATTERN, "NUMBER")


def _is_block_key(key: str) -> bool:
    return bool(re.match(r"^(TOTAL|GROUP:[^|]+|SKU:[^|]+)$", key))


def _normalize_metric_key(key: str) -> str:
    normalized = key.strip()
    if "|" in normalized:
        return normalized.rsplit("|", 1)[-1].strip()
    return normalized


def _is_percent_metric(metric_key: str) -> bool:
    return metric_key in {"ctr", "ctr_current", "localizationPercent", "localization_percent"} or bool(
        re.search(r"(_pct|_percent)$", metric_key)
    )


def _is_decimal_metric(metric_key: str) -> bool:
    return metric_key == "position_avg"


def _is_currency_metric(metric_key: str) -> bool:
    return metric_key.endswith("_rub")


def _apply_batch_update(token: str, requests: list[dict[str, Any]]) -> dict[str, Any]:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{TARGET_SPREADSHEET_ID}:batchUpdate"
    return _curl_json(url, method="POST", token=token, payload={"requests": requests})


def _get_format_snapshot(token: str, values: dict[str, list[list[Any]]]) -> dict[str, Any]:
    data_last_row = len(values[DATA_SHEET_NAME])
    data_last_col = max(len(row) for row in values[DATA_SHEET_NAME])
    status_last_row = len(values[STATUS_SHEET_NAME])
    status_last_col = max(len(row) for row in values[STATUS_SHEET_NAME])
    fields = (
        "properties.title,"
        "sheets(properties(sheetId,title,gridProperties.frozenRowCount,gridProperties.frozenColumnCount),"
        "data(startRow,startColumn,columnMetadata(pixelSize),rowData(values(formattedValue,effectiveValue,userEnteredFormat(numberFormat,backgroundColor,textFormat,horizontalAlignment)))))"
    )
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{TARGET_SPREADSHEET_ID}"
        f"?includeGridData=true&fields={quote(fields, safe='(),.=')}"
        f"&ranges={quote(f'{DATA_SHEET_NAME}!A1:{_column_name(data_last_col)}{data_last_row}', safe='!A:Z0-9')}"
        f"&ranges={quote(f'{STATUS_SHEET_NAME}!A1:{_column_name(status_last_col)}{status_last_row}', safe='!A:Z0-9')}"
    )
    payload = _curl_json(url, token=token)
    title = payload.get("properties", {}).get("title")
    if title != TARGET_SPREADSHEET_NAME:
        raise AssertionError(f"unexpected spreadsheet title in snapshot: {title}")
    return payload


def _hex_from_color(color: dict[str, Any] | None) -> str | None:
    if not isinstance(color, dict):
        return None
    red = round(float(color.get("red", 0)) * 255)
    green = round(float(color.get("green", 0)) * 255)
    blue = round(float(color.get("blue", 0)) * 255)
    return f"#{red:02x}{green:02x}{blue:02x}"


def _snapshot_by_name(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {sheet["properties"]["title"]: sheet for sheet in snapshot.get("sheets", [])}


def _get_cell(sheet_snapshot: dict[str, Any], row_index: int, column_index: int) -> dict[str, Any]:
    values = sheet_snapshot["data"][0]["rowData"][row_index]["values"]
    return values[column_index]


def _get_number_format(cell: dict[str, Any]) -> str | None:
    return cell.get("userEnteredFormat", {}).get("numberFormat", {}).get("pattern")


def _get_alignment(cell: dict[str, Any]) -> str | None:
    return cell.get("userEnteredFormat", {}).get("horizontalAlignment", "").lower() or None


def _get_font_weight(cell: dict[str, Any]) -> str | None:
    return "bold" if cell.get("userEnteredFormat", {}).get("textFormat", {}).get("bold") else "normal"


def _assert_equal_values(before: dict[str, list[list[Any]]], after: dict[str, list[list[Any]]]) -> None:
    for sheet_name in (DATA_SHEET_NAME, STATUS_SHEET_NAME):
        if before[sheet_name] != after[sheet_name]:
            raise AssertionError(f"{sheet_name} values changed during presentation pass")


def _assert_data_vitrina(snapshot: dict[str, Any], values: list[list[Any]]) -> None:
    sheet = _snapshot_by_name(snapshot)[DATA_SHEET_NAME]
    props = sheet["properties"]["gridProperties"]
    if props.get("frozenColumnCount") != 2:
        raise AssertionError(f"DATA_VITRINA frozen columns mismatch: {props}")

    column_metadata = sheet["data"][0]["columnMetadata"]
    if column_metadata[0]["pixelSize"] != DATA_LABEL_WIDTH or column_metadata[1]["pixelSize"] != DATA_KEY_WIDTH:
        raise AssertionError(f"DATA_VITRINA base widths mismatch: {column_metadata[:2]}")

    header_left = _get_cell(sheet, 0, 0)
    header_date = _get_cell(sheet, 0, 2)
    if _hex_from_color(header_left.get("userEnteredFormat", {}).get("backgroundColor")) != HEADER_BACKGROUND:
        raise AssertionError(f"DATA_VITRINA header background mismatch: {header_left}")
    if _hex_from_color(header_left.get("userEnteredFormat", {}).get("textFormat", {}).get("foregroundColor")) != HEADER_FONT_COLOR:
        raise AssertionError(f"DATA_VITRINA header font color mismatch: {header_left}")
    if _get_font_weight(header_left) != "bold":
        raise AssertionError(f"DATA_VITRINA header weight mismatch: {header_left}")
    if _get_alignment(header_date) != "center":
        raise AssertionError(f"DATA_VITRINA date header alignment mismatch: {header_date}")
    if _get_number_format(header_date) != DATE_PATTERN:
        raise AssertionError(f"DATA_VITRINA date header pattern mismatch: {header_date}")

    header_mode = "matrix" if values and values[0][:2] == ["дата", "key"] else "flat"
    percent_row = _find_row_index(values, r"^(ctr|ctr_current)$", normalized=True)
    decimal_row = _find_row_index(values, r"^position_avg$", normalized=True)
    integer_row = _find_row_index(values, r"^view_count$", normalized=True)

    percent_cell = _get_cell(sheet, percent_row, 2)
    decimal_cell = _get_cell(sheet, decimal_row, 2)
    integer_cell = _get_cell(sheet, integer_row, 2)

    if header_mode == "matrix":
        section_row = _find_row_index(values, r"^TOTAL$")
        section_cell = _get_cell(sheet, section_row, 2)
        if _get_number_format(section_cell) != TEXT_PATTERN:
            raise AssertionError(f"DATA_VITRINA section format mismatch: {section_cell}")
        if _get_alignment(section_cell) != "left":
            raise AssertionError(f"DATA_VITRINA section alignment mismatch: {section_cell}")
    if _get_number_format(percent_cell) != PERCENT_PATTERN:
        raise AssertionError(f"DATA_VITRINA percent format mismatch: {percent_cell}")
    if _get_number_format(decimal_cell) != DECIMAL_PATTERN:
        raise AssertionError(f"DATA_VITRINA decimal format mismatch: {decimal_cell}")
    if _get_number_format(integer_cell) != INTEGER_PATTERN:
        raise AssertionError(f"DATA_VITRINA integer format mismatch: {integer_cell}")


def _assert_status(snapshot: dict[str, Any]) -> None:
    sheet = _snapshot_by_name(snapshot)[STATUS_SHEET_NAME]
    props = sheet["properties"]["gridProperties"]
    if props.get("frozenRowCount") != 1:
        raise AssertionError(f"STATUS frozen rows mismatch: {props}")

    column_metadata = sheet["data"][0]["columnMetadata"]
    for index, (column_name, width) in enumerate(STATUS_WIDTHS.items()):
        actual = column_metadata[index]["pixelSize"]
        if actual != width:
            raise AssertionError(f"STATUS width mismatch for {column_name}: {actual}")

    header = _get_cell(sheet, 0, 0)
    if _hex_from_color(header.get("userEnteredFormat", {}).get("backgroundColor")) != HEADER_BACKGROUND:
        raise AssertionError(f"STATUS header background mismatch: {header}")
    if _hex_from_color(header.get("userEnteredFormat", {}).get("textFormat", {}).get("foregroundColor")) != HEADER_FONT_COLOR:
        raise AssertionError(f"STATUS header font color mismatch: {header}")
    if _get_font_weight(header) != "bold":
        raise AssertionError(f"STATUS header weight mismatch: {header}")

    kind = _get_cell(sheet, 1, 1)
    freshness = _get_cell(sheet, 2, 2)
    requested = _get_cell(sheet, 1, 7)
    covered = _get_cell(sheet, 1, 8)

    if _get_alignment(kind) != "center":
        raise AssertionError(f"STATUS kind alignment mismatch: {kind}")
    if _get_font_weight(kind) != "bold":
        raise AssertionError(f"STATUS kind font weight mismatch: {kind}")
    if _get_number_format(freshness) != DATE_PATTERN:
        raise AssertionError(f"STATUS date format mismatch: {freshness}")
    if _get_alignment(freshness) != "center":
        raise AssertionError(f"STATUS date alignment mismatch: {freshness}")
    if _get_number_format(requested) != INTEGER_PATTERN:
        raise AssertionError(f"STATUS requested_count format mismatch: {requested}")
    if _get_number_format(covered) != INTEGER_PATTERN:
        raise AssertionError(f"STATUS covered_count format mismatch: {covered}")


def _find_row_index(values: list[list[Any]], pattern: str, *, normalized: bool = False) -> int:
    for index, row in enumerate(values[1:], start=1):
        key = str(row[1]) if len(row) > 1 else ""
        if normalized:
            key = _normalize_metric_key(key)
        if re.search(pattern, key):
            return index
    raise AssertionError(f"unable to find DATA_VITRINA row for pattern: {pattern}")


def main() -> None:
    _verify_target_config()
    token = _refresh_access_token()
    sheet_props = _get_spreadsheet_metadata(token)
    before = _batch_get_values(token)
    requests = _build_presentation_requests(sheet_props, before)
    result = _apply_batch_update(token, requests)
    after = _batch_get_values(token)
    snapshot = _get_format_snapshot(token, after)

    if result.get("spreadsheetId") != TARGET_SPREADSHEET_ID:
        raise AssertionError(f"presentation result spreadsheet id mismatch: {result}")

    _assert_equal_values(before, after)
    _assert_data_vitrina(snapshot, after[DATA_SHEET_NAME])
    _assert_status(snapshot)

    print("delivery-path: direct-google-sheets-api")
    print("DATA_VITRINA: presentation ok")
    print("STATUS: presentation ok")
    print("presentation-live-check passed")


if __name__ == "__main__":
    main()
