"""Focused local proof for bounded Web Vitrina transport diagnostics and RUM."""

from __future__ import annotations

from contextlib import redirect_stderr
import gzip
from http import HTTPStatus
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
import time
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_JOB_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_WEB_VITRINA_PERFORMANCE_PATH,
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    WEB_AUTH_ROLE_ADMIN,
    WEB_AUTH_ROLE_OPERATOR,
    WEB_AUTH_SECTION_VITRINA,
    WEB_VITRINA_UI_TEMPLATE_PATH,
    UI_SYSTEM_CSS_PATH,
    _encode_web_vitrina_page_composition_body,
    _render_sheet_vitrina_web_vitrina_ui,
    _required_section_for_path,
    _sheet_vitrina_ui_system_css,
    _web_vitrina_ui_base_template,
    _write_web_vitrina_page_composition_response,
    _write_html_response,
)
from packages.application.web_vitrina_performance import (  # noqa: E402
    WEB_VITRINA_PERFORMANCE_BYTE_METRICS,
    WEB_VITRINA_PERFORMANCE_MAX_REQUEST_BYTES,
    WEB_VITRINA_PERFORMANCE_METRICS,
    emit_web_vitrina_performance_event,
    normalize_web_vitrina_performance_envelope,
)


def main() -> None:
    _check_single_encode_and_exact_payload_bytes()
    _check_template_cache_and_request_owned_config()
    _check_rum_allowlist_and_bounds()
    print("web_vitrina_performance_smoke: ok")


def _check_single_encode_and_exact_payload_bytes() -> None:
    payload = {
        "composition_name": "web_vitrina_page_composition",
        "meta": {
            "snapshot_id": "synthetic-snapshot",
            "page_composition_diagnostics": {
                "payload_bytes": 0,
                "row_count": 2,
            },
        },
        "table_surface": {
            "rows": [
                {"row_id": "row-1", "values": {"date:1": 0}},
                {"row_id": "row-2", "values": {"date:1": None}},
            ],
            "columns": [{"id": "date:1"}],
        },
    }
    calls = 0

    def counting_dumps(value: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return json.dumps(value, **kwargs)

    body = _encode_web_vitrina_page_composition_body(payload, dumps=counting_dumps)
    parsed = json.loads(body.decode("utf-8"))
    if calls != 1:
        raise AssertionError(f"page composition body must use exactly one JSON traversal, got {calls}")
    if parsed["meta"]["page_composition_diagnostics"]["payload_bytes"] != len(body):
        raise AssertionError("payload_bytes must equal the exact decoded body length")
    expected = json.loads(json.dumps(payload, ensure_ascii=False))
    expected["meta"]["page_composition_diagnostics"]["payload_bytes"] = len(body)
    if parsed != expected:
        raise AssertionError("byte patch must preserve every business JSON value")

    representative = {
        "composition_name": "web_vitrina_page_composition",
        "meta": {"page_composition_diagnostics": {"payload_bytes": 0}},
        "table_surface": {
            "columns": [{"id": f"date:{index}"} for index in range(14)],
            "rows": [
                {
                    "row_id": f"SKU:{index}|view_count",
                    "scope_kind": "SKU",
                    "metric_key": "view_count",
                    "metric_label": "Показы в воронке",
                    "section_id": "sales_funnel",
                    "values": {
                        f"date:{date_index}": (index % 100 if date_index == 13 else None)
                        for date_index in range(14)
                    },
                    "presentation": {
                        f"date:{date_index}": {
                            "state": "unavailable",
                            "reason": "Исторические данные отсутствуют",
                        }
                        for date_index in range(13)
                    },
                }
                for index in range(1000)
            ],
        },
    }
    representative_body = _encode_web_vitrina_page_composition_body(representative)
    compressed = gzip.compress(representative_body, compresslevel=1, mtime=0)
    if len(compressed) > len(representative_body) * 0.10:
        raise AssertionError(
            "representative full-table JSON must meet the <=10% gzip acceptance target"
        )
    if gzip.decompress(compressed) != representative_body:
        raise AssertionError("gzip transport must preserve logical response bytes exactly")

    handler = _FakeHtmlHandler()
    stderr = StringIO()
    with redirect_stderr(stderr):
        _write_web_vitrina_page_composition_response(
            handler,
            HTTPStatus.OK,
            payload,
            request_started_perf=time.perf_counter(),
            build_ms=12.5,
        )
    response_body = handler.wfile.getvalue()
    response_payload = json.loads(response_body)
    if response_payload["meta"]["page_composition_diagnostics"]["payload_bytes"] != len(response_body):
        raise AssertionError("HTTP writer must reuse the exact fixed-point body")
    if handler.response_headers.get("Content-Length") != str(len(response_body)):
        raise AssertionError(f"HTTP writer emitted stale Content-Length: {handler.response_headers}")
    if not handler.response_headers.get("Server-Timing", "").startswith("build;dur=12.500, encode;dur="):
        raise AssertionError(f"HTTP writer emitted invalid Server-Timing: {handler.response_headers}")
    event = json.loads(stderr.getvalue())
    expected_event_fields = {
        "event",
        "route_kind",
        "status",
        "request_ms",
        "build_ms",
        "encode_ms",
        "write_ms",
        "encode_cpu_ms",
        "logical_bytes",
        "disconnected",
    }
    if set(event) != expected_event_fields or event.get("event") != "web_vitrina_http_response_v1":
        raise AssertionError(f"HTTP response event fields are not fixed/sanitized: {event}")
    if event.get("logical_bytes") != len(response_body) or event.get("disconnected") is not False:
        raise AssertionError(f"HTTP response event transport facts mismatch: {event}")


def _check_template_cache_and_request_owned_config() -> None:
    original_read_text = Path.read_text
    reads: dict[Path, int] = {}

    def counting_read_text(path: Path, *args: object, **kwargs: object) -> str:
        resolved = path.resolve()
        if resolved in {WEB_VITRINA_UI_TEMPLATE_PATH.resolve(), UI_SYSTEM_CSS_PATH.resolve()}:
            reads[resolved] = reads.get(resolved, 0) + 1
        return original_read_text(path, *args, **kwargs)

    _web_vitrina_ui_base_template.cache_clear()
    _sheet_vitrina_ui_system_css.cache_clear()
    with mock.patch.object(Path, "read_text", new=counting_read_text):
        admin_html = _render_sheet_vitrina_web_vitrina_ui(
            read_path=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
            operator_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            refresh_path=DEFAULT_SHEET_REFRESH_PATH,
            job_path=DEFAULT_SHEET_JOB_PATH,
            role=WEB_AUTH_ROLE_ADMIN,
            allowed_sections=[WEB_AUTH_SECTION_VITRINA],
        )
        operator_html = _render_sheet_vitrina_web_vitrina_ui(
            read_path="/request-owned-read-path",
            operator_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            refresh_path=DEFAULT_SHEET_REFRESH_PATH,
            job_path=DEFAULT_SHEET_JOB_PATH,
            role=WEB_AUTH_ROLE_OPERATOR,
            allowed_sections=[WEB_AUTH_SECTION_VITRINA],
        )
    expected_reads = {
        WEB_VITRINA_UI_TEMPLATE_PATH.resolve(): 1,
        UI_SYSTEM_CSS_PATH.resolve(): 1,
    }
    if reads != expected_reads:
        raise AssertionError(f"base HTML and shared CSS must each be read once, got {reads}")
    if '"current_role": "admin"' not in admin_html or "/request-owned-read-path" in admin_html:
        raise AssertionError("admin request config was not rendered independently")
    if '"current_role": "operator"' not in operator_html or "/request-owned-read-path" not in operator_html:
        raise AssertionError("operator request config was not rendered independently")
    if '"current_role": "operator"' in admin_html or '"current_role": "admin"' in operator_html:
        raise AssertionError("cached base template leaked role-specific config")

    barrier_calls = 0

    def inject_dynamic_barrier(body: str) -> str:
        nonlocal barrier_calls
        barrier_calls += 1
        return body + f"<!-- request-barrier-{barrier_calls} -->"

    first_handler = _FakeHtmlHandler()
    second_handler = _FakeHtmlHandler()
    with mock.patch(
        "packages.adapters.registry_upload_http_entrypoint._inject_business_data_write_barrier_ui",
        side_effect=inject_dynamic_barrier,
    ):
        _write_html_response(first_handler, HTTPStatus.OK, admin_html)
        _write_html_response(second_handler, HTTPStatus.OK, operator_html)
    if barrier_calls != 2:
        raise AssertionError(f"write barrier must be rendered on every request, got {barrier_calls}")
    if b"request-barrier-1" not in first_handler.wfile.getvalue():
        raise AssertionError("first request barrier was not rendered")
    if b"request-barrier-2" not in second_handler.wfile.getvalue():
        raise AssertionError("second request barrier was not rendered")
    for handler in (first_handler, second_handler):
        if handler.response_headers.get("Cache-Control") != "private, no-store":
            raise AssertionError(f"HTML response must remain private/no-store: {handler.response_headers}")


def _check_rum_allowlist_and_bounds() -> None:
    if _required_section_for_path(DEFAULT_SHEET_WEB_VITRINA_PERFORMANCE_PATH) != WEB_AUTH_SECTION_VITRINA:
        raise AssertionError("RUM endpoint must remain inside existing Vitrina section authorization")
    metrics: dict[str, int | float | None] = {
        name: (128 if name in WEB_VITRINA_PERFORMANCE_BYTE_METRICS else 1.25)
        for name in WEB_VITRINA_PERFORMANCE_METRICS
    }
    valid = {
        "contract_name": "web_vitrina_performance_v1",
        "envelope_kind": "page_load",
        "viewport_bucket": "wide_1440",
        "metrics": metrics,
        "unavailable_metrics": [],
    }
    encoded = json.dumps(valid, separators=(",", ":")).encode("utf-8")
    if len(encoded) > WEB_VITRINA_PERFORMANCE_MAX_REQUEST_BYTES:
        raise AssertionError("valid RUM envelope must fit the 4 KiB transport bound")
    normalized = normalize_web_vitrina_performance_envelope(valid)
    if normalized["viewport_bucket"] != "wide_1440":
        raise AssertionError(f"viewport bucket was not preserved: {normalized}")

    unavailable = json.loads(json.dumps(valid))
    unavailable["metrics"]["table_transfer_bytes"] = None
    unavailable["unavailable_metrics"] = ["table_transfer_bytes"]
    normalized_unavailable = normalize_web_vitrina_performance_envelope(unavailable)
    if normalized_unavailable["metrics"]["table_transfer_bytes"] is not None:
        raise AssertionError("explicit unavailable metric must remain null")

    invalid_payloads = []
    unknown_root = dict(valid)
    unknown_root["url"] = "https://example.invalid/?seller=secret"
    invalid_payloads.append(unknown_root)
    string_metric = json.loads(json.dumps(valid))
    string_metric["metrics"]["shell_ttfb_ms"] = "1.25"
    invalid_payloads.append(string_metric)
    sensitive_metric = json.loads(json.dumps(valid))
    sensitive_metric["metrics"]["seller_id"] = 123
    invalid_payloads.append(sensitive_metric)
    oversized_metric = json.loads(json.dumps(valid))
    oversized_metric["metrics"]["table_transfer_bytes"] = 1_000_000_001
    invalid_payloads.append(oversized_metric)
    for invalid in invalid_payloads:
        try:
            normalize_web_vitrina_performance_envelope(invalid)
        except ValueError:
            continue
        raise AssertionError(f"RUM schema accepted a forbidden payload: {invalid}")

    stderr = StringIO()
    with redirect_stderr(stderr):
        emit_web_vitrina_performance_event(normalized_unavailable)
    journal_payload = json.loads(stderr.getvalue())
    forbidden_keys = {"url", "query", "query_params", "seller_id", "sku", "date", "user_id", "content"}
    observed_keys = set(journal_payload) | set(journal_payload.get("metrics") or {})
    if observed_keys & forbidden_keys:
        raise AssertionError(
            f"sanitized RUM event contains forbidden fields: {sorted(observed_keys & forbidden_keys)}"
        )
    if "example.invalid" in json.dumps(journal_payload, ensure_ascii=False):
        raise AssertionError("sanitized RUM event leaked rejected payload content")


class _FakeHtmlHandler:
    def __init__(self) -> None:
        self.path = "/sheet-vitrina-v1/vitrina"
        self.wfile = BytesIO()
        self.response_status = 0
        self.response_headers: dict[str, str] = {}

    def send_response(self, status: int) -> None:
        self.response_status = int(status)

    def send_header(self, name: str, value: str) -> None:
        self.response_headers[str(name)] = str(value)

    def end_headers(self) -> None:
        return


if __name__ == "__main__":
    main()
