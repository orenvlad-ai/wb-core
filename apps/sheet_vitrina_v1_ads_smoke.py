"""Smoke-check SKU-first ads MVP without live WB write calls."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_ADS_BID_COMMIT_PATH,
    DEFAULT_SHEET_ADS_BID_PREVIEW_PATH,
    DEFAULT_SHEET_ADS_SKUS_PATH,
    DEFAULT_SHEET_ADS_SKU_PREFIX,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.sheet_vitrina_v1_ads import (  # noqa: E402
    AdsSafetyConfig,
    SheetVitrinaV1AdsBlock,
    SheetVitrinaV1AdsError,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


BUNDLE_FIXTURE = ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
NOW = datetime(2026, 6, 28, 6, 0, tzinfo=timezone.utc)
PRIMARY_NM = 210183919
SECONDARY_NM = 210184534
EXTERNAL_NM = 999999001


class FakePromotionSource:
    def __init__(self, *, unsupported_status: bool = False) -> None:
        self.unsupported_status = unsupported_status
        self.patch_payloads: list[dict[str, object]] = []
        self.min_bid_calls: list[dict[str, object]] = []

    def fetch_campaign_count(self) -> dict[str, object]:
        return {
            "adverts": [
                {
                    "status": 9,
                    "advert_list": [
                        {"advertId": 1001},
                        {"advertId": 1002},
                        {"advertId": 1003},
                    ],
                }
            ]
        }

    def fetch_adverts(self, advert_ids, *, statuses=None, payment_type="") -> dict[str, object]:
        status = 7 if self.unsupported_status else 9
        adverts = [
            {
                "id": 1001,
                "status": status,
                "name": "Manual CPM campaign",
                "bid_type": "manual",
                "settings": {
                    "name": "Manual CPM campaign",
                    "payment_type": "cpm",
                    "placements": {"search": True, "recommendations": True},
                },
                "nm_settings": [
                    {
                        "nm_id": PRIMARY_NM,
                        "bids_kopecks": {"search": 1500, "recommendations": 1700},
                    },
                    {
                        "nm_id": SECONDARY_NM,
                        "bids_kopecks": {"search": 1600},
                    },
                ],
            },
            {
                "id": 1002,
                "status": 11,
                "name": "Unified CPC campaign",
                "bid_type": "unified",
                "settings": {
                    "name": "Unified CPC campaign",
                    "payment_type": "cpc",
                    "placements": {"search": True},
                },
                "nm_settings": [
                    {
                        "nm_id": PRIMARY_NM,
                        "bids_kopecks": {"search": 2100},
                    }
                ],
            },
            {
                "id": 1003,
                "status": 9,
                "name": "External nm campaign",
                "bid_type": "manual",
                "settings": {
                    "name": "External nm campaign",
                    "payment_type": "cpm",
                    "placements": {"search": True},
                },
                "nm_settings": [
                    {
                        "nm_id": EXTERNAL_NM,
                        "bids_kopecks": {"search": 1300},
                    }
                ],
            },
        ]
        wanted = {int(value) for value in advert_ids}
        return {"adverts": [item for item in adverts if int(item["id"]) in wanted]}

    def fetch_min_bids(self, *, advert_id, nm_ids, payment_type, placement_types) -> dict[str, object]:
        self.min_bid_calls.append(
            {
                "advert_id": advert_id,
                "nm_ids": list(nm_ids),
                "payment_type": payment_type,
                "placement_types": list(placement_types),
            }
        )
        bid_map = {"search": 1000, "recommendation": 1200, "combined": 1100}
        return {
            "bids": [
                {
                    "nm_id": int(nm_ids[0]),
                    "bids": [
                        {"type": placement_type, "value": bid_map.get(str(placement_type), 1000)}
                        for placement_type in placement_types
                    ],
                }
            ]
        }

    def fetch_recommendations(self, *, advert_id, nm_id) -> dict[str, object]:
        return {"base": {"competitiveBid": {"bidKopecks": 1900}}}

    def fetch_fullstats(self, advert_ids, *, begin_date, end_date):
        return [
            {
                "advertId": 1001,
                "days": [
                    {
                        "date": begin_date,
                        "apps": [
                            {
                                "nms": [
                                    {"nmId": PRIMARY_NM, "views": 100, "clicks": 10, "orders": 2, "sum": 40.0},
                                    {"nmId": SECONDARY_NM, "views": 60, "clicks": 6, "orders": 1, "sum": 18.0},
                                ]
                            }
                        ],
                    }
                ],
            },
            {
                "advertId": 1002,
                "days": [
                    {
                        "date": begin_date,
                        "apps": [
                            {
                                "nms": [
                                    {"nmId": PRIMARY_NM, "views": 50, "clicks": 5, "orders": 1, "sum": 15.0},
                                ]
                            }
                        ],
                    }
                ],
            },
        ]

    def patch_bids(self, payload):
        copied = json.loads(json.dumps(payload))
        self.patch_payloads.append(copied)
        return {"result": "ok", "patched_count": 1}


def main() -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-ads-smoke-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = _seed_runtime(runtime_dir)
        source = FakePromotionSource()
        block = _build_ads_block(runtime, runtime_dir, source, write_enabled=True)

        sku_payload = block.build_sku_table({"date_from": "2026-06-22", "date_to": "2026-06-28"})
        _assert_sku_table(sku_payload)

        detail = block.build_sku_detail(PRIMARY_NM, {"date_from": "2026-06-22", "date_to": "2026-06-28"})
        _assert_sku_detail(detail)

        preview = block.preview_bid_change(
            {
                "nm_id": PRIMARY_NM,
                "advert_id": 1001,
                "placement": "recommendation",
                "requested_bid_rub": "18.00",
            }
        )
        preview_facts = preview["preview"]
        if preview_facts["placement"] != "recommendations" or preview_facts["new_bid_kopecks"] != 1800:
            raise AssertionError(f"preview must normalize placement and rub->kopecks, got {preview_facts}")
        if preview_facts["min_bid_kopecks"] != 1200:
            raise AssertionError(f"preview must include min bid, got {preview_facts}")

        commit = block.commit_bid_change({"preview_id": preview_facts["preview_id"]}, actor="smoke_actor")
        if commit.get("status") != "pending_refresh":
            raise AssertionError(f"commit should return pending refresh, got {commit}")
        expected_patch = {
            "bids": [
                {
                    "advert_id": 1001,
                    "nm_bids": [
                        {
                            "nm_id": PRIMARY_NM,
                            "bid_kopecks": 1800,
                            "placement": "recommendations",
                        }
                    ],
                }
            ]
        }
        if source.patch_payloads != [expected_patch]:
            raise AssertionError(f"PATCH request shape mismatch: {source.patch_payloads}")
        audit_path = runtime_dir / "sheet_vitrina_v1_ads" / "bid_audit.jsonl"
        audit_lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        if len(audit_lines) != 1 or json.loads(audit_lines[0]).get("actor") != "smoke_actor":
            raise AssertionError(f"audit event mismatch: {audit_lines}")

        _assert_negative_cases(runtime, runtime_dir)
        _run_http_smoke(runtime, runtime_dir)

    print("sheet_vitrina_v1_ads_smoke: OK")


def _assert_sku_table(payload: dict[str, object]) -> None:
    if payload.get("contract_name") != "sheet_vitrina_v1_ads_skus":
        raise AssertionError(f"sku contract mismatch: {payload}")
    rows = {int(row["nm_id"]): row for row in payload.get("rows", [])}
    primary = rows[PRIMARY_NM]
    if primary.get("campaign_count") != 2 or primary.get("placement_count") != 3:
        raise AssertionError(f"SKU-first reverse mapping mismatch: {primary}")
    if primary.get("our_sku") != "OUR-210183919":
        raise AssertionError(f"nomenclature enrichment mismatch: {primary}")
    if round(float(primary.get("spend_rub") or 0), 2) != 55.0:
        raise AssertionError(f"stats aggregate mismatch: {primary}")
    external = rows[EXTERNAL_NM]
    if external.get("status") != "missing_in_registry":
        raise AssertionError(f"external campaign nm must be surfaced, got {external}")


def _assert_sku_detail(payload: dict[str, object]) -> None:
    rows = payload.get("rows") or []
    keys = {(int(row["advert_id"]), str(row["placement"])) for row in rows}
    expected = {(1001, "search"), (1001, "recommendations"), (1002, "search")}
    if keys != expected:
        raise AssertionError(f"drawer must preserve campaign/placement rows, got {rows}")
    cpm_reco = next(row for row in rows if int(row["advert_id"]) == 1001 and row["placement"] == "recommendations")
    if cpm_reco.get("recommended_bid_kopecks") != 1900:
        raise AssertionError(f"CPM recommended bid missing: {cpm_reco}")
    cpc = next(row for row in rows if int(row["advert_id"]) == 1002)
    if cpc.get("recommended_bid_status") != "not_available":
        raise AssertionError(f"CPC recommendations must be not_available: {cpc}")
    if cpm_reco.get("stats_scope") != "campaign_sku_aggregate":
        raise AssertionError(f"stats scope must not claim placement stats: {cpm_reco}")


def _assert_negative_cases(runtime: RegistryUploadDbBackedRuntime, runtime_dir: Path) -> None:
    block = _build_ads_block(runtime, runtime_dir, FakePromotionSource(), write_enabled=False)
    preview = block.preview_bid_change(
        {
            "nm_id": PRIMARY_NM,
            "advert_id": 1001,
            "placement": "search",
            "requested_bid_rub": "16.00",
        }
    )
    try:
        block.commit_bid_change({"preview_id": preview["preview"]["preview_id"]}, actor="smoke")
    except SheetVitrinaV1AdsError as exc:
        if exc.http_status != 403:
            raise AssertionError(f"write disabled must be 403, got {exc.http_status}") from exc
    else:
        raise AssertionError("write disabled commit must be blocked")

    below_min_block = _build_ads_block(runtime, runtime_dir, FakePromotionSource(), write_enabled=True)
    try:
        below_min_block.preview_bid_change(
            {
                "nm_id": PRIMARY_NM,
                "advert_id": 1001,
                "placement": "search",
                "requested_bid_rub": "9.99",
            }
        )
    except SheetVitrinaV1AdsError as exc:
        if exc.http_status != 422 or "below" not in str(exc):
            raise AssertionError(f"below-min failure mismatch: {exc}") from exc
    else:
        raise AssertionError("below-min bid must be blocked")

    try:
        below_min_block.preview_bid_change(
            {
                "nm_id": SECONDARY_NM,
                "advert_id": 1002,
                "placement": "search",
                "requested_bid_rub": "22.00",
            }
        )
    except SheetVitrinaV1AdsError as exc:
        if exc.http_status != 422:
            raise AssertionError(f"mismatch failure status mismatch: {exc.http_status}") from exc
    else:
        raise AssertionError("mismatched nm_id/advert_id must be blocked")

    unsupported_block = _build_ads_block(runtime, runtime_dir, FakePromotionSource(unsupported_status=True), write_enabled=True)
    try:
        unsupported_block.preview_bid_change(
            {
                "nm_id": PRIMARY_NM,
                "advert_id": 1001,
                "placement": "search",
                "requested_bid_rub": "16.00",
            }
        )
    except SheetVitrinaV1AdsError as exc:
        if exc.http_status != 422 or "unsupported campaign status" not in str(exc):
            raise AssertionError(f"unsupported status failure mismatch: {exc}") from exc
    else:
        raise AssertionError("unsupported campaign status must be blocked")

    try:
        below_min_block.preview_bid_change(
            {
                "nm_id": [PRIMARY_NM, SECONDARY_NM],
                "advert_id": 1001,
                "placement": "search",
                "requested_bid_rub": "16.00",
            }
        )
    except SheetVitrinaV1AdsError:
        pass
    else:
        raise AssertionError("bulk/list nm_id payload must be rejected")


def _run_http_smoke(runtime: RegistryUploadDbBackedRuntime, runtime_dir: Path) -> None:
    source = FakePromotionSource()
    entrypoint = RegistryUploadHttpEntrypoint(
        runtime_dir=runtime_dir,
        runtime=runtime,
        now_factory=lambda: NOW,
        activated_at_factory=lambda: "2026-06-28T06:00:00Z",
        ads_block=_build_ads_block(runtime, runtime_dir, source, write_enabled=True),
    )
    config = RegistryUploadHttpEntrypointConfig(
        host="127.0.0.1",
        port=_reserve_free_port(),
        upload_path=DEFAULT_UPLOAD_PATH,
        sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
        sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
        sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
        sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
        runtime_dir=runtime_dir,
    )
    server = build_registry_upload_http_server(config, entrypoint=entrypoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{config.port}"
        ui_status, ui_html = _get_text(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}")
        if ui_status != 200:
            raise AssertionError(f"operator shell route must return 200, got {ui_status}")
        for expected in (
            'data-unified-tab-button="ads"',
            'data-unified-tab-panel="ads"',
            'data-ads-sku-body',
            'data-ads-drawer',
            'data-ads-modal',
            f'"ads_skus_path": "{DEFAULT_SHEET_ADS_SKUS_PATH}"',
            f'"ads_sku_path": "{DEFAULT_SHEET_ADS_SKU_PREFIX}"',
            f'"ads_bid_preview_path": "{DEFAULT_SHEET_ADS_BID_PREVIEW_PATH}"',
            f'"ads_bid_commit_path": "{DEFAULT_SHEET_ADS_BID_COMMIT_PATH}"',
        ):
            if expected not in ui_html:
                raise AssertionError(f"ads UI must contain {expected!r}")
        if 'method: "PATCH"' in _ads_script_slice(ui_html):
            raise AssertionError("frontend ads code must not perform direct PATCH")

        status, skus = _get_json(f"{base_url}{DEFAULT_SHEET_ADS_SKUS_PATH}")
        if status != 200 or skus.get("contract_name") != "sheet_vitrina_v1_ads_skus":
            raise AssertionError(f"ads skus route mismatch: {status} {skus}")
        status, detail = _get_json(f"{base_url}{DEFAULT_SHEET_ADS_SKU_PREFIX}/{PRIMARY_NM}")
        if status != 200 or detail.get("contract_name") != "sheet_vitrina_v1_ads_sku":
            raise AssertionError(f"ads sku route mismatch: {status} {detail}")
        status, preview = _post_json(
            f"{base_url}{DEFAULT_SHEET_ADS_BID_PREVIEW_PATH}",
            {
                "nm_id": PRIMARY_NM,
                "advert_id": 1001,
                "placement": "search",
                "requested_bid_rub": "16.00",
            },
        )
        if status != 200 or preview.get("contract_name") != "sheet_vitrina_v1_ads_bid_change_preview":
            raise AssertionError(f"preview route mismatch: {status} {preview}")
        status, commit = _post_json(
            f"{base_url}{DEFAULT_SHEET_ADS_BID_COMMIT_PATH}",
            {"preview_id": preview["preview"]["preview_id"]},
        )
        if status != 200 or commit.get("contract_name") != "sheet_vitrina_v1_ads_bid_change_commit":
            raise AssertionError(f"commit route mismatch: {status} {commit}")
        status, below_min = _post_json(
            f"{base_url}{DEFAULT_SHEET_ADS_BID_PREVIEW_PATH}",
            {
                "nm_id": PRIMARY_NM,
                "advert_id": 1001,
                "placement": "search",
                "requested_bid_rub": "9.99",
            },
        )
        if status != 422 or "below" not in str(below_min.get("error")):
            raise AssertionError(f"below-min route failure mismatch: {status} {below_min}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _seed_runtime(runtime_dir: Path) -> RegistryUploadDbBackedRuntime:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    result = runtime.ingest_bundle(bundle, activated_at="2026-06-28T06:00:00Z")
    if result.status != "accepted":
        raise AssertionError(f"bundle fixture must be accepted, got {result}")
    runtime.save_nomenclature_item(
        {
            "item_id": "ads-smoke-primary",
            "is_active": True,
            "our_sku": "OUR-210183919",
            "nm_id": PRIMARY_NM,
            "barcode": "4600000000001",
            "nomenclature_name": "Primary ads smoke SKU",
            "product_type": "phone-case",
            "match_key": "ads-smoke-primary",
            "created_at": "2026-06-28T06:00:00Z",
            "updated_at": "2026-06-28T06:00:00Z",
        }
    )
    return runtime


def _build_ads_block(
    runtime: RegistryUploadDbBackedRuntime,
    runtime_dir: Path,
    source: FakePromotionSource,
    *,
    write_enabled: bool,
) -> SheetVitrinaV1AdsBlock:
    return SheetVitrinaV1AdsBlock(
        runtime=runtime,
        runtime_dir=runtime_dir,
        source=source,
        now_factory=lambda: NOW,
        timestamp_factory=lambda: "2026-06-28T06:00:00Z",
        cache_ttl_seconds=120,
        safety_config=AdsSafetyConfig(
            write_enabled=write_enabled,
            absolute_max_bid_kopecks=100_000,
            max_percent_increase=1000,
            max_absolute_increase_kopecks=100_000,
            preview_ttl_seconds=180,
        ),
    )


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    status, text = _get_text(url)
    return status, json.loads(text)


def _get_text(url: str) -> tuple[int, str]:
    try:
        with urllib_request.urlopen(url, timeout=5) as response:
            return int(response.status), response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8")


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=5) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return int(exc.code), json.loads(exc.read().decode("utf-8"))


def _ads_script_slice(html: str) -> str:
    marker = "function ensureAdsLoaded()"
    index = html.find(marker)
    return html[index:] if index >= 0 else html


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
