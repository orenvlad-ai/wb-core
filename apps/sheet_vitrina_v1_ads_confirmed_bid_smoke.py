"""Confirmed exact Ads bids: private fixtures, fake WB, no live API calls."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from http.cookiejar import CookieJar
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error, parse, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.registry_upload_http_entrypoint_auth_smoke import _password_hash, _patched_env
from apps.sheet_vitrina_v1_ads_smoke import (
    EXTERNAL_NM, FakePromotionSource, NOW, _reserve_free_port, _seed_runtime,
)
from apps.sku_management_smoke import FakeAds, FakePrices, NM_ID
from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_ADS_BID_COMMIT_PATH, DEFAULT_SHEET_ADS_BID_PREVIEW_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH, DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH, DEFAULT_UPLOAD_PATH, build_registry_upload_http_server,
)
from packages.adapters.wb_promotion import WbPromotionApiError
from packages.application.business_data_write_barrier import acquire_barrier
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.change_registry_writer import InternalWriterRegistry, InternalWriterRegistryError
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_ads import AdsSafetyConfig, SheetVitrinaV1AdsBlock, SheetVitrinaV1AdsError
from packages.application.sku_management import SkuManagementBlock, SkuManagementError
from packages.application.wb_prices_management import WbPricesManagementBlock
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig


SAFETY = AdsSafetyConfig(True, 100_000, Decimal("20"), 10_000, 180)
CODES = {"absolute_max_bid", "max_percent_increase", "max_absolute_increase"}
TARGET = {"nm_id": EXTERNAL_NM, "advert_id": 1003, "placement": "search", "requested_bid_rub": "1500"}


class NoFrameSource(FakePromotionSource):
    def __init__(self):
        super().__init__()
        self.bid = 100_000
        self.minimum = 90_000
        self.status = 9
        self.payment = "cpm"
        self.bid_type = "manual"
        self.nm_id = EXTERNAL_NM
        self.placement = "search"
        self.ambiguous = False

    def fetch_adverts(self, advert_ids, **kwargs):
        return {"adverts": [{
            "id": 1003, "status": self.status, "bid_type": self.bid_type,
            "settings": {"name": "No Frame CPM", "payment_type": self.payment, "placements": {self.placement: True}},
            "nm_settings": [{"nm_id": self.nm_id, "bids_kopecks": {self.placement: self.bid}}],
        }]} if 1003 in advert_ids else {"adverts": []}

    def fetch_min_bids(self, **kwargs):
        self.min_bid_calls.append(dict(kwargs))
        return {"bids": []} if self.minimum is None else {
            "bids": [{"nm_id": self.nm_id, "bids": [{"type": "search", "value": self.minimum}]}],
        }

    def patch_bids(self, payload):
        self.patch_payloads.append(json.loads(json.dumps(payload)))
        if self.ambiguous:
            raise WbPromotionApiError("fixture connection lost after submit", "fixture://ads/bids")
        self.bid = int(payload["bids"][0]["nm_bids"][0]["bid_kopecks"])
        return {"result": "ok"}


def block_at(runtime, path, source, *, writer=None):
    return SheetVitrinaV1AdsBlock(runtime=runtime, runtime_dir=path, source=source,
        now_factory=lambda: NOW, safety_config=SAFETY, writer_registry=writer)


def assert_blocked(block, payload, status=None):
    before = len(block.source.patch_payloads)
    try:
        block.commit_bid_change(payload, actor="operator")
    except SheetVitrinaV1AdsError as exc:
        if status is not None:
            assert exc.http_status == status, (exc.http_status, str(exc))
    else:
        raise AssertionError("unsafe commit accepted")
    assert len(block.source.patch_payloads) == before


def backend_checks(root):
    runtime = _seed_runtime(root / "runtime")
    source = NoFrameSource()
    ChangeRegistryRepository(root / "runtime").initialize_schema()
    writer = InternalWriterRegistry(runtime_dir=root / "runtime", seller_id="fixture-seller", account_scope="seller-portal-primary")
    block = block_at(runtime, root / "runtime", source, writer=writer)
    preview = block.preview_bid_change(TARGET)
    facts = preview["preview"]
    assert {item["code"] for item in facts["safety_threshold_warnings"]} == CODES
    assert source.patch_payloads == []
    assert EXTERNAL_NM not in {item.nm_id for item in runtime.load_current_state().config_v2}
    for flag in (None, False, "true"):
        assert_blocked(block, {"preview_id": facts["preview_id"], "confirm": flag}, 400)
        assert not (block._preview_dir / f"{facts['preview_id']}.claim").exists()
    # Commit recalculates warnings against the current thresholds.
    block.safety = replace(SAFETY, max_absolute_increase_kopecks=20_000)
    result = block.commit_bid_change(preview["confirmation_payload"], actor="operator")
    assert {item["code"] for item in result["safety_threshold_warnings"]} == CODES
    assert next(item for item in result["safety_threshold_warnings"] if item["code"] == "max_absolute_increase")["threshold_minor"] == 20_000
    assert result["registry_readback_status"] == "confirmed", result
    stored = writer.read_by_receipt(result["registry_receipt_reference"])
    assert stored["operation"]["operation_id"] == result["registry_operation_id"]
    assert len(stored["items"]) == len(stored["latest_attempts"]) == 1
    assert stored["items"][0]["requested_value_integer"] == 150_000
    assert stored["latest_attempts"][0]["state"] == "confirmed"
    assert source.patch_payloads == [{"bids": [{"advert_id": 1003, "nm_bids": [{"nm_id": EXTERNAL_NM, "placement": "search", "bid_kopecks": 150_000}]}]}]
    assert result["audit_event"]["safety_threshold_warnings"] == result["safety_threshold_warnings"]
    assert_blocked(block, preview["confirmation_payload"], 409)

    mutations = {
        "changed_bid": lambda b, s, p: setattr(s, "bid", 101_000),
        "changed_status": lambda b, s, p: setattr(s, "status", 11),
        "unsupported_status": lambda b, s, p: setattr(s, "status", 7),
        "changed_payment": lambda b, s, p: setattr(s, "payment", "cpc"),
        "changed_bid_type": lambda b, s, p: setattr(s, "bid_type", "unified"),
        "changed_nm": lambda b, s, p: setattr(s, "nm_id", EXTERNAL_NM + 1),
        "changed_placement": lambda b, s, p: setattr(s, "placement", "recommendations"),
        "missing_min": lambda b, s, p: setattr(s, "minimum", None),
        "raised_min": lambda b, s, p: setattr(s, "minimum", 150_001),
        "expired": lambda b, s, p: b._save_preview({**p, "expires_at_epoch": 0}),
        "write_disabled": lambda b, s, p: setattr(b, "safety", replace(SAFETY, write_enabled=False)),
    }
    for name, mutate in mutations.items():
        source = NoFrameSource()
        candidate = block_at(runtime, root / name, source)
        p = candidate.preview_bid_change(TARGET)
        mutate(candidate, source, p["preview"])
        assert_blocked(candidate, p["confirmation_payload"])
        assert source.patch_payloads == [], name

    source = NoFrameSource()
    candidate = block_at(runtime, root / "ambiguous", source)
    p = candidate.preview_bid_change(TARGET)
    source.ambiguous = True
    try:
        candidate.commit_bid_change(p["confirmation_payload"], actor="operator")
    except WbPromotionApiError:
        pass
    else:
        raise AssertionError("fixture ambiguity not raised")
    assert_blocked(candidate, p["confirmation_payload"], 409)
    assert len(source.patch_payloads) == 1

    class BrokenRegistry:
        def prepare_bid(self, **kwargs):
            raise InternalWriterRegistryError("fixture unavailable")
    source = NoFrameSource()
    candidate = block_at(runtime, root / "registry-failure", source, writer=BrokenRegistry())
    p = candidate.preview_bid_change(TARGET)
    assert_blocked(candidate, p["confirmation_payload"], 503)

    # The SKU wrapper uses its existing confirm/actor gate and shares warnings.
    ads_source = FakeAds()
    ads_source.bid = 100_000
    ads_source.min_bid = 90_000
    ads = block_at(runtime, root / "sku", ads_source)
    sku = SkuManagementBlock(runtime=runtime, runtime_dir=root / "sku", ads_block=ads,
        prices_block=WbPricesManagementBlock(runtime=runtime, runtime_dir=root / "sku", source=FakePrices()),
        now_factory=lambda: NOW, timestamp_factory=lambda: "2026-06-28T06:00:00Z", sleep=lambda _: None, readback_attempts=1)
    p = sku.preview_bid({"nm_id": NM_ID, "advert_id": 77, "placement": "search", "requested_bid_rub": 1500}, actor="operator")["preview"]
    assert {item["code"] for item in p["safety_threshold_warnings"]} == CODES
    assert p["override_required_warnings"] == []
    for body, actor in (({"preview_id": p["preview_id"]}, "operator"), ({"preview_id": p["preview_id"], "confirm": True}, "other")):
        try:
            sku.commit_bid(body, actor=actor)
        except SkuManagementError:
            pass
        else:
            raise AssertionError("SKU confirmation/actor gate missing")
        assert ads_source.calls["patch"] == 0
    result = sku.commit_bid({"preview_id": p["preview_id"], "confirm": True}, actor="operator")
    assert result["confirmed_value"] == 1500 and result["readback_status"] == "matching"
    assert {item["code"] for item in result["safety_threshold_warnings"]} == CODES
    assert ads_source.calls["patch"] == 1


def post(opener, url, payload):
    req = request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        response = opener.open(req, timeout=5)
    except error.HTTPError as exc:
        response = exc
    with response:
        return response.code, json.loads(response.read())


def login(base, username):
    opener = request.build_opener(request.HTTPCookieProcessor(CookieJar()))
    body = parse.urlencode({"username": username, "password": "fixture-password", "next": "/sheet-vitrina-v1/vitrina?tab=sku-management"}).encode()
    with opener.open(request.Request(base + "/login", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=5) as response:
        assert response.status == 200
    return opener


def http_checks(root):
    runtime_dir = root / "runtime"
    runtime = _seed_runtime(runtime_dir)
    runtime.save_sheet_vitrina_user({"user_id": "limited", "username": "limited", "display_name": "Limited fixture", "password_hash": _password_hash("fixture-password"), "role": "operator", "allowed_sections": ["sku_management"], "manage_users": False, "is_active": True, "created_at": "2026-06-28T06:00:00Z", "updated_at": "2026-06-28T06:00:00Z"})
    source = NoFrameSource()
    ads = block_at(runtime, runtime_dir, source)
    app = RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir, runtime=runtime, ads_block=ads, now_factory=lambda: NOW)
    config = RegistryUploadHttpEntrypointConfig(host="127.0.0.1", port=_reserve_free_port(), upload_path=DEFAULT_UPLOAD_PATH, sheet_plan_path=DEFAULT_SHEET_PLAN_PATH, sheet_refresh_path="/v1/sheet-vitrina-v1/refresh", sheet_status_path=DEFAULT_SHEET_STATUS_PATH, sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH, runtime_dir=runtime_dir)
    with _patched_env({"WB_CORE_WEB_AUTH_REQUIRED": "1", "WB_CORE_WEB_AUTH_USERNAME": "owner", "WB_CORE_WEB_AUTH_PASSWORD_HASH": _password_hash("fixture-password"), "WB_CORE_WEB_AUTH_SESSION_SECRET": "fixture-session-secret"}):
        server = build_registry_upload_http_server(config, entrypoint=app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{config.port}"
            ready = ads.preview_bid_change(TARGET)["confirmation_payload"]
            url = base + DEFAULT_SHEET_ADS_BID_COMMIT_PATH
            assert post(request.build_opener(), url, ready)[0] == 401
            assert source.patch_payloads == []
            limited = login(base, "limited")
            assert post(limited, url, ready)[0] == 403
            assert source.patch_payloads == []
            owner = login(base, "owner")
            assert post(owner, url, {"preview_id": ready["preview_id"]})[0] == 400
            assert source.patch_payloads == []
            status, preview = post(owner, base + DEFAULT_SHEET_ADS_BID_PREVIEW_PATH, TARGET)
            assert status == 200
            status, result = post(owner, url, preview["confirmation_payload"])
            assert status == 200, result
            assert len(source.patch_payloads) == 1 and source.bid == 150_000
            assert post(owner, url, preview["confirmation_payload"])[0] == 409
            acquire_barrier(runtime_dir, window_id="confirmed-bids-fixture", window_kind="maintenance_pause", plan_fingerprint="sha256:" + "a" * 64, approval_reference="fixture-approval", actor="fixture", reason="fixture hold")
            assert post(owner, url, ready)[0] == 423
            assert len(source.patch_payloads) == 1
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main():
    with TemporaryDirectory(prefix="ads-confirmed-bids-") as tmp:
        root = Path(tmp)
        backend_checks(root / "backend")
        http_checks(root / "http")
    print("sheet_vitrina_v1_ads_confirmed_bid_smoke: OK")


if __name__ == "__main__":
    main()
