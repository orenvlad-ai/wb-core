#!/usr/bin/env python3
"""Playwright smoke for the shared warehouse UI and legacy FF transition."""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import copy
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.warehouse_stocks_smoke import _block, _seed_runtime  # noqa: E402
from apps.warehouse_stocks_production_ui_flow import run_warehouse_ui_flow  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.warehouse_functional import (  # noqa: E402
    FUNCTIONAL_CUTOVER_ID,
    STAGES,
    STAGE_WB,
    WarehouseFunctionalBlock,
    WarehouseLine,
    _fingerprint,
    _line_payload,
    _summaries,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    with TemporaryDirectory(prefix="warehouse-browser-smoke-") as temp_dir:
        root = Path(temp_dir)
        runtime = _seed_runtime(root / "runtime")
        block = _block(runtime)
        plan = block.build_opening_plan()
        block.apply_opening_plan(
            plan,
            confirm_fingerprint=plan["plan_fingerprint"],
            backup_dir=root / "backups",
        )
        functional = _apply_functional_fixture(
            runtime=runtime,
            opening_plan=plan,
            backup_dir=root / "functional-backups",
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime.runtime_dir,
        )
        with _patched_env({"WB_CORE_WEB_AUTH_REQUIRED": "0"}):
            server = build_registry_upload_http_server(
                config,
                entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir, runtime=runtime),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = run_warehouse_ui_flow(
                    base_url=f"http://127.0.0.1:{config.port}",
                    auth_cookie=None,
                    expected_readback=functional.readback(),
                    evidence_dir=root / "ui-evidence",
                    strict_business_acceptance=False,
                    allowed_server_error_paths=(
                        "/v1/sheet-vitrina-v1/supply/wb-supplies/overlay-options",
                    ),
                    allowed_console_error_messages=(
                        "Failed to load resource: the server responded with a status of 422 (Unprocessable Content)",
                        "Failed to load resource: the server responded with a status of 500 (Internal Server Error)",
                    ),
                )
                _assert(result.get("status") == "ok", "browser flow status")
                legacy_ff = result.get("legacy_ff_reconciliation") or {}
                ff_evidence = next(
                    item for item in result.get("warehouses") or [] if item.get("warehouse_key") == "ff"
                )
                _assert(result.get("legacy_ff_transition") is True, "legacy FF transition status")
                _assert(legacy_ff.get("loaded_before_screenshot") is True, "legacy FF loaded evidence")
                _assert(legacy_ff.get("document_id") == ff_evidence.get("document_id"), "legacy FF document")
                _assert(legacy_ff.get("sku_count") == ff_evidence.get("sku_count"), "legacy FF SKU count")
                _assert(
                    legacy_ff.get("total_quantity") == ff_evidence.get("total_quantity"),
                    "legacy FF total quantity",
                )
                _assert(
                    legacy_ff.get("balance_rows") == ff_evidence.get("balance_rows"),
                    "legacy FF balance rows",
                )
                _assert(legacy_ff.get("economics_loaded") is True, "legacy FF functional economics")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    print("warehouse stocks browser smoke: ok")


def _apply_functional_fixture(
    *,
    runtime,
    opening_plan: dict,
    backup_dir: Path,
) -> WarehouseFunctionalBlock:
    block = WarehouseFunctionalBlock(runtime=runtime, timestamp_factory=lambda: "2026-07-18T08:05:00Z")
    block._local_source_digest = lambda **_: "sha256:local-browser-fixture"  # type: ignore[method-assign]
    block._wb_supply_source_digest = lambda **_: "sha256:supply-browser-fixture"  # type: ignore[method-assign]
    lines: list[WarehouseLine] = []
    for document in opening_plan["documents"]:
        stage = str(document["warehouse_key"])
        for raw in document.get("lines") or []:
            quantity = Decimal(str(raw["quantity"]))
            if quantity <= 0:
                continue
            nm_id = int(raw["nm_id"])
            wac = Decimal(100 + nm_id)
            lines.append(
                WarehouseLine(
                    warehouse_key=stage,
                    nm_id=nm_id,
                    quantity=quantity,
                    capital=quantity * wac,
                    cost_covered_quantity=quantity,
                    quality="direct_24_06",
                    provenance={"fixture": True, "source_records": list((raw.get("provenance") or {}).get("source_records") or [])},
                    certified=True,
                    wb_quantity=quantity if stage == STAGE_WB else Decimal("0"),
                )
            )
    summaries = _summaries(lines)
    wb_lines = [item for item in lines if item.warehouse_key == STAGE_WB]
    opening_cost_map = [
        {
            "nm_id": nm_id,
            "ff_unit_cost_rub": str(Decimal(90 + nm_id)),
            "wb_unit_cost_rub": str(Decimal(100 + nm_id)),
            "quality": "direct_24_06",
            "provenance": {"fixture": True},
            "fingerprint": f"sha256:browser-{nm_id}",
        }
        for nm_id in sorted({item.nm_id for item in lines})
    ]
    snapshot = {
        "snapshot_id": "wbsnap_browser_fixture",
        "fetched_at": "2026-07-18T08:00:00Z",
        "snapshot_date": "2026-07-18",
        "requested_nm_ids": sorted({item.nm_id for item in lines}),
        "pagination_complete": True,
        "page_count": 1,
        "page_offsets": [0],
        "raw_row_count": len(wb_lines),
        "raw_rows_digest": "sha256:browser-rows",
        "raw_rows": [{"nmId": item.nm_id, "quantity": str(item.quantity)} for item in wb_lines],
        "items": [
            {
                "nm_id": item.nm_id,
                "quantity": str(item.quantity),
                "in_way_to_client": "0",
                "in_way_from_client": "0",
                "wb_contour_quantity": str(item.quantity),
            }
            for item in wb_lines
        ],
    }
    daily = [
        {
            "as_of_date": "2026-07-18",
            "nm_id": item.nm_id,
            "quantity": str(item.quantity),
            "wac_rub": str(item.wac),
            "capital_rub": str(item.capital),
            "quality": "periodic_snapshot_wac_provisional",
            "provenance": {"fixture": True},
            "fingerprint": f"sha256:browser-daily-{item.nm_id}",
        }
        for item in wb_lines
    ]
    functional_plan = {
        "contract_name": "sheet_vitrina_v1_warehouse_functional",
        "contract_version": "v2",
        "status": "dry_run_ready",
        "kind": "functional_cutover",
        "cutover_id": FUNCTIONAL_CUTOVER_ID,
        "captured_at": "2026-07-18T08:00:00Z",
        "effective_date": "2026-07-18",
        "base_active_version_id": "",
        "local_source_digest": "sha256:local-browser-fixture",
        "wb_supply_source_digest": "sha256:supply-browser-fixture",
        "source_watermarks": {"fixture": True},
        "absorbed_supply_revisions": {},
        "wb_snapshot": snapshot,
        "opening_cost_map": opening_cost_map,
        "historical_wb_cost_projection": daily,
        "lines": [_line_payload(item) for item in lines],
        "summaries": summaries,
        "unmatched_doprinato": [],
        "new_events": [],
        "movement_documents": [],
        "diff": {"changed_line_count": len(lines), "lines": []},
        "invariants": {
            "warehouse_count": len(STAGES),
            "negative_balance_count": 0,
            "positive_cost_gap_count": 0,
            "historical_wb_cost_gap_count": 0,
            "wb_quantity_source": "official_snapshot_only",
            "discrepancy_opening_zero": True,
            "ff_debit_coverage": {"uncovered_supply_count": 0},
        },
    }
    functional_plan["plan_fingerprint"] = _fingerprint(functional_plan)
    block.apply_plan(
        copy.deepcopy(functional_plan),
        confirm_fingerprint=functional_plan["plan_fingerprint"],
        backup_dir=backup_dir,
    )
    return block


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _patched_env(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _assert(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    main()
