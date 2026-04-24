"""Archived Apps Script bridge for the legacy Google Sheets contour."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope

ROOT = Path(__file__).resolve().parents[2]
CLASP_CONFIG_PATH = ROOT / ".clasp.json"
EXPECTED_CLASP_ROOT_DIR = "gas/sheet_vitrina_v1"
CLASP_PROFILE_PATH_ENV = "WB_CORE_CLASPRC_PATH"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCRIPT_RUN_URL_TEMPLATE = "https://script.googleapis.com/v1/scripts/{script_id}:run"
LEGACY_GOOGLE_SHEETS_ARCHIVE_STATUS = "ARCHIVED / DO NOT USE"
LEGACY_GOOGLE_SHEETS_ARCHIVE_MESSAGE = (
    "Legacy Google Sheets contour is archived. "
    "Use the website/operator web-vitrina surface instead."
)


class LegacyGoogleSheetsContourArchivedError(RuntimeError):
    """Raised when a caller tries to use the archived Google Sheets contour."""


def legacy_google_sheets_archive_context() -> dict[str, Any]:
    return {
        "status": LEGACY_GOOGLE_SHEETS_ARCHIVE_STATUS,
        "active": False,
        "write_enabled": False,
        "load_enabled": False,
        "verification_target": False,
        "message": LEGACY_GOOGLE_SHEETS_ARCHIVE_MESSAGE,
    }


def load_sheet_vitrina_ready_snapshot_via_clasp(
    plan: SheetVitrinaV1Envelope,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    del plan
    emit = log or _noop
    emit(f"legacy_google_sheets_contour={LEGACY_GOOGLE_SHEETS_ARCHIVE_STATUS}")
    raise LegacyGoogleSheetsContourArchivedError(LEGACY_GOOGLE_SHEETS_ARCHIVE_MESSAGE)


def resolve_sheet_vitrina_live_spreadsheet_url() -> str:
    raise LegacyGoogleSheetsContourArchivedError(LEGACY_GOOGLE_SHEETS_ARCHIVE_MESSAGE)


def _load_clasp_config() -> dict[str, Any]:
    if not CLASP_CONFIG_PATH.exists():
        raise ValueError(f"missing clasp config: {CLASP_CONFIG_PATH}")
    payload = json.loads(CLASP_CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(".clasp.json must contain a JSON object")
    return payload


def _load_clasp_profile() -> dict[str, str]:
    configured_path = str(os.environ.get(CLASP_PROFILE_PATH_ENV, "") or "").strip()
    profile_path = (
        Path(configured_path).expanduser()
        if configured_path
        else Path.home() / ".clasprc.json"
    )
    if not profile_path.exists():
        raise ValueError(f"missing clasp profile: {profile_path}")

    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(".clasprc.json must contain a JSON object")

    profile = payload.get("tokens", {}).get("default")
    if not isinstance(profile, dict):
        raise ValueError("missing default clasp profile in ~/.clasprc.json")

    normalized: dict[str, str] = {}
    for field_name in ("client_id", "client_secret", "refresh_token"):
        field_value = str(profile.get(field_name, "") or "").strip()
        if not field_value:
            raise ValueError(f"missing {field_name} in ~/.clasprc.json")
        normalized[field_name] = field_value
    return normalized


def _run_apps_script_json_function(
    *,
    script_id: str,
    profile: dict[str, str],
    function_name: str,
    parameters: list[Any],
) -> Any:
    access_token = _refresh_access_token(profile)
    payload = _post_json(
        url=GOOGLE_SCRIPT_RUN_URL_TEMPLATE.format(script_id=script_id),
        payload={
            "function": function_name,
            "parameters": parameters,
            "devMode": True,
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if not isinstance(payload, dict):
        raise ValueError("Apps Script execution API must return a JSON object")

    error_payload = payload.get("error")
    if isinstance(error_payload, dict):
        raise RuntimeError(_format_execution_error(error_payload))

    response_payload = payload.get("response")
    if not isinstance(response_payload, dict) or "result" not in response_payload:
        raise ValueError(f"Apps Script execution API returned unexpected payload: {payload}")
    return _parse_json_result(response_payload["result"])


def _refresh_access_token(profile: dict[str, str]) -> str:
    payload = _post_form(
        GOOGLE_OAUTH_TOKEN_URL,
        {
            "client_id": profile["client_id"],
            "client_secret": profile["client_secret"],
            "refresh_token": profile["refresh_token"],
            "grant_type": "refresh_token",
        },
    )
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError(f"unable to refresh access token: {payload}")
    return access_token.strip()


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib_request.Request(
        url,
        method="POST",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            **(headers or {}),
        },
    )
    return _open_json_request(request)


def _post_form(url: str, fields: dict[str, str]) -> Any:
    encoded = urllib_parse.urlencode(fields).encode("utf-8")
    request = urllib_request.Request(
        url,
        method="POST",
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(encoded)),
        },
    )
    return _open_json_request(request)


def _open_json_request(request: urllib_request.Request) -> Any:
    try:
        with urllib_request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body.strip()}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def _parse_json_result(result: Any) -> Any:
    if isinstance(result, (dict, list)):
        return result
    if isinstance(result, str):
        stripped = result.strip()
        if not stripped:
            raise ValueError("Apps Script returned an empty string")
        return json.loads(stripped)
    raise ValueError(f"Apps Script returned unsupported result type: {type(result).__name__}")


def _format_execution_error(payload: dict[str, Any]) -> str:
    message = str(payload.get("message", "") or "").strip()
    details = payload.get("details")
    if isinstance(details, list):
        for item in details:
            if not isinstance(item, dict):
                continue
            error_message = str(item.get("errorMessage", "") or item.get("message", "")).strip()
            if error_message:
                message = error_message
                break
    return message or json.dumps(payload, ensure_ascii=False)


def _noop(_: str) -> None:
    return
