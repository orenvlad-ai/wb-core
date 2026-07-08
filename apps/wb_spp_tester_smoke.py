"""Smoke-check WB SPP tester with fake upstreams and no live WB writes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_prices_management_smoke import PRIMARY_NM, _seed_runtime  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_PRICES_SPP_TEST_BASELINE_PATH,
    DEFAULT_SHEET_PRICES_SPP_TEST_PLAN_PATH,
    DEFAULT_SHEET_PRICES_SPP_TEST_RESTORE_PATH,
    DEFAULT_SHEET_PRICES_SPP_TEST_START_PATH,
    DEFAULT_SHEET_PRICES_SPP_TEST_STATUS_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.adapters.wb_prices_management import WbPricesApiError  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.wb_spp_tester import (  # noqa: E402
    WbSppTesterBlock,
    WbSppTesterCadenceConfig,
    WbSppTesterError,
    WbSppTesterSafetyConfig,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


NOW = datetime(2026, 7, 7, 7, 0, tzinfo=timezone.utc)


class FakeSppPricesSource:
    def __init__(self, *, rate_limit_upload_attempts: int = 0, quarantine: bool = False) -> None:
        self.rate_limit_upload_attempts = rate_limit_upload_attempts
        self.quarantine = quarantine
        self.upload_attempts = 0
        self.upload_payloads: list[list[dict[str, Any]]] = []
        self.next_upload_id = 7000
        self.good = {
            "nmID": PRIMARY_NM,
            "vendorCode": "VC-PRIMARY",
            "currencyIsoCode4217": "RUB",
            "discount": 10,
            "clubDiscount": 0,
            "editableSizePrice": False,
            "wholesaleDiscountThreshold": [],
            "isBadTurnover": False,
            "sizes": [
                {
                    "sizeID": 11,
                    "techSizeName": "0",
                    "price": 1000,
                    "discountedPrice": 900,
                    "clubDiscountedPrice": 900,
                }
            ],
        }

    @property
    def discounted_price(self) -> float:
        return float(self.good["sizes"][0]["discountedPrice"])

    def fetch_goods(self, *, limit: int, offset: int, filter_nm_id: int | None = None) -> Mapping[str, Any]:
        if filter_nm_id is not None and int(filter_nm_id) != PRIMARY_NM:
            rows: list[Mapping[str, Any]] = []
        else:
            rows = [self.good]
        return {"data": {"listGoods": rows[offset : offset + limit]}, "error": False, "errorText": ""}

    def fetch_goods_by_nm_ids(self, nm_ids: Sequence[int]) -> Mapping[str, Any]:
        rows = [self.good] if PRIMARY_NM in {int(value) for value in nm_ids} else []
        return {"data": {"listGoods": rows}, "error": False, "errorText": ""}

    def upload_task(self, goods: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        self.upload_attempts += 1
        if self.upload_attempts <= self.rate_limit_upload_attempts:
            raise WbPricesApiError(
                method="POST",
                url="https://discounts-prices-api.wildberries.ru/api/v2/upload/task",
                http_status=429,
                headers={"Retry-After": "0"},
                body_summary="rate limit",
                retry_after_seconds=0,
            )
        copied = [dict(item) for item in goods]
        self.upload_payloads.append(copied)
        for item in copied:
            if int(item.get("nmID") or 0) != PRIMARY_NM:
                continue
            if item.get("discount") is not None:
                self.good["discount"] = int(item["discount"])
            if item.get("price") is not None:
                self.good["sizes"][0]["price"] = int(item["price"])
            price = float(self.good["sizes"][0]["price"])
            discount = float(self.good["discount"])
            discounted = round(price * (100 - discount) / 100, 2)
            self.good["sizes"][0]["discountedPrice"] = discounted
            self.good["sizes"][0]["clubDiscountedPrice"] = discounted
        self.next_upload_id += 1
        return {"data": {"id": self.next_upload_id, "alreadyExists": False}, "error": False, "errorText": ""}

    def fetch_upload_status(self, upload_id: int) -> Mapping[str, Any]:
        return {
            "data": {
                "uploadID": int(upload_id),
                "status": 3,
                "uploadDate": "2026-07-07T07:00:00+00:00",
                "activationDate": "2026-07-07T07:00:00+00:00",
                "overAllGoodsNumber": 1,
                "successGoodsNumber": 1,
            },
            "error": False,
            "errorText": "",
        }

    def fetch_upload_goods(self, *, upload_id: int, limit: int, offset: int) -> Mapping[str, Any]:
        return {"data": {"uploadID": int(upload_id), "historyGoods": []}, "error": False, "errorText": ""}

    def fetch_quarantine_goods(self, *, limit: int, offset: int) -> Mapping[str, Any]:
        rows = []
        if self.quarantine:
            rows.append({"nmID": PRIMARY_NM, "newPrice": 100, "oldPrice": 1000, "newDiscount": 10, "oldDiscount": 10})
        return {"data": {"quarantineGoods": rows[offset : offset + limit]}, "error": False, "errorText": ""}


class FakePublicSource:
    def __init__(self, prices: FakeSppPricesSource, *, stale: bool = False) -> None:
        self.prices = prices
        self.stale = stale
        self.reads = 0

    def fetch_public_buyer_price(self, nm_id: int) -> Mapping[str, Any]:
        self.reads += 1
        discounted = self.prices.discounted_price
        if self.stale:
            public_price = 630.0
        else:
            spp = 0.10 if discounted < 800 else 0.14
            public_price = round(discounted * (1 - spp), 2)
        return {
            "status": "ok",
            "public_buyer_price": public_price,
            "endpoint": "fake_public_card",
            "http_status": 200,
            "headers": {},
            "body_summary": "",
            "diagnostics": {"read": self.reads},
        }


def main() -> None:
    _run_backend_unit_smokes()
    _run_http_smoke()
    print("wb_spp_tester_smoke: OK")


def _run_backend_unit_smokes() -> None:
    with TemporaryDirectory(prefix="wb-spp-test-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = _seed_runtime(runtime_dir)

        source = FakeSppPricesSource()
        block = _build_block(runtime, runtime_dir, source, FakePublicSource(source))
        baseline = block.build_baseline({"nmID": PRIMARY_NM})
        if not baseline["baseline"]["can_start"] or baseline["baseline"]["discountedPrice"] != 900:
            raise AssertionError(f"baseline capture mismatch: {baseline}")
        plan = block.build_plan(
            {
                "nmID": PRIMARY_NM,
                "range_min_discounted": 700,
                "range_max_discounted": 900,
                "precision_rub": 2,
                "max_measurements": 5,
            }
        )
        if len(plan["plan"]["initial_points"]) != 3 or plan["plan"]["refinement_budget"] != 2:
            raise AssertionError(f"plan preview mismatch: {plan}")
        plan_30 = block.build_plan(
            {
                "nmID": PRIMARY_NM,
                "range_min_discounted": 700,
                "range_max_discounted": 900,
                "precision_rub": 2,
                "max_measurements": 30,
            }
        )
        plan_30_payload = plan_30["plan"]
        if (
            plan_30_payload["max_measurements"] != 30
            or plan_30_payload["refinement_budget"] != 27
            or plan_30_payload["request_budget"]["wb_uploads"] != 30
            or plan_30_payload["request_budget"]["public_reads"] != 90
        ):
            raise AssertionError(f"max_measurements=30 plan budget mismatch: {plan_30_payload}")
        duration_source = FakeSppPricesSource()
        duration_block = _build_block(
            runtime,
            runtime_dir / "duration",
            duration_source,
            FakePublicSource(duration_source),
            zero_cadence=False,
        )
        duration_plan = duration_block.build_plan(
            {
                "nmID": PRIMARY_NM,
                "range_min_discounted": 700,
                "range_max_discounted": 900,
                "precision_rub": 2,
                "max_measurements": 30,
            }
        )["plan"]
        if duration_plan["estimated_duration_seconds"] != 27600:
            raise AssertionError(f"max_measurements=30 duration mismatch: {duration_plan}")
        try:
            block.build_plan(
                {
                    "nmID": PRIMARY_NM,
                    "range_min_discounted": 700,
                    "range_max_discounted": 900,
                    "precision_rub": 2,
                    "max_measurements": 31,
                }
            )
        except WbSppTesterError as exc:
            if exc.http_status != 422 or "between 3 and 30" not in str(exc):
                raise
        else:
            raise AssertionError("max_measurements=31 must be rejected")
        started = block.start(_start_payload(700, 900, max_measurements=5), actor="smoke")
        job = started["job"]
        if job["result_status"] != "threshold_detected":
            raise AssertionError(f"threshold should be detected, got: {job}")
        if not job["restore"]["restored"]:
            raise AssertionError(f"baseline restore proof missing: {job['restore']}")

        stale_source = FakeSppPricesSource()
        stale_block = _build_block(runtime, runtime_dir / "stale", stale_source, FakePublicSource(stale_source, stale=True))
        stale_job = stale_block.start(_start_payload(700, 900, max_measurements=3), actor="smoke")["job"]
        if not any(row["status"] == "stale_public_price" for row in stale_job["measurements"]):
            raise AssertionError(f"stale public price point must be marked, got: {stale_job['measurements']}")

        rate_source = FakeSppPricesSource(rate_limit_upload_attempts=1)
        rate_block = _build_block(runtime, runtime_dir / "rate", rate_source, FakePublicSource(rate_source))
        rate_job = rate_block.start(_start_payload(700, 900, max_measurements=3), actor="smoke")["job"]
        audit_text = (runtime_dir / "rate" / "sheet_vitrina_v1_prices" / "spp_tests" / "audit.jsonl").read_text(encoding="utf-8")
        if "wb_prices_429_backoff" not in audit_text or not rate_job["restore"]["restored"]:
            raise AssertionError("429 backoff must be audited and restore must still run")

        high_source = FakeSppPricesSource()
        high_block = _build_block(runtime, runtime_dir / "high", high_source, FakePublicSource(high_source))
        high_job = high_block.start(_start_payload(1400, 1600, max_measurements=3), actor="smoke")["job"]
        if not any(step.get("kind") == "bridge" for step in high_job["restore"]["steps"]):
            raise AssertionError(f"large downward restore must use bridge steps, got: {high_job['restore']['steps']}")

        emergency_source = FakeSppPricesSource()
        emergency_block = _build_block(runtime, runtime_dir / "emergency", emergency_source, FakePublicSource(emergency_source))
        baseline_payload = emergency_block.build_baseline({"nmID": PRIMARY_NM})["baseline"]
        emergency_source.upload_task([{"nmID": PRIMARY_NM, "price": 1500, "discount": 10}])
        manual_job = {
            "job_id": "manual_restore_job",
            "created_at": "2026-07-07T07:00:00Z",
            "updated_at": "2026-07-07T07:00:00Z",
            "actor": "smoke",
            "status": "manual_restore_required",
            "result_status": "manual_restore_required",
            "nmID": PRIMARY_NM,
            "input": _start_payload(700, 900, max_measurements=3),
            "baseline": baseline_payload,
            "plan": {},
            "measurements": [{"actual_wb_discounted_price": emergency_source.discounted_price}],
            "thresholds": [],
            "timeline": [],
            "restore": {"required": True, "restored": False, "proof": None, "steps": []},
            "manual_restore_required": True,
            "error": "",
        }
        emergency_block._save_job(manual_job)
        emergency_block._write_current_job(manual_job)
        restored = emergency_block.restore({"job_id": "manual_restore_job", "confirm_restore": True}, actor="smoke")
        if restored["status"] != "restored" or not restored["job"]["restore"]["restored"]:
            raise AssertionError(f"emergency restore failed: {restored}")

        lock_source = FakeSppPricesSource()
        lock_block = _build_block(runtime, runtime_dir / "lock", lock_source, FakePublicSource(lock_source))
        active_job = dict(manual_job)
        active_job["job_id"] = "active_job"
        active_job["status"] = "measuring"
        lock_block._save_job(active_job)
        lock_block._write_current_job(active_job)
        try:
            lock_block.start(_start_payload(700, 900), actor="smoke")
        except WbSppTesterError as exc:
            if exc.http_status != 409:
                raise
        else:
            raise AssertionError("active SPP job must reject concurrent start")


def _run_http_smoke() -> None:
    with TemporaryDirectory(prefix="wb-spp-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = _seed_runtime(runtime_dir)
        source = FakeSppPricesSource()
        block = _build_block(runtime, runtime_dir, source, FakePublicSource(source))
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            now_factory=lambda: NOW,
            activated_at_factory=lambda: "2026-07-07T07:00:00Z",
            spp_tester_block=block,
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
            status, baseline = _get_json(f"{base_url}{DEFAULT_SHEET_PRICES_SPP_TEST_BASELINE_PATH}?nmID={PRIMARY_NM}")
            if status != 200 or baseline["contract_name"] != "sheet_vitrina_v1_prices_spp_test_baseline":
                raise AssertionError(f"baseline route failed: {status} {baseline}")
            status, plan = _post_json(
                f"{base_url}{DEFAULT_SHEET_PRICES_SPP_TEST_PLAN_PATH}",
                _start_payload(700, 900, max_measurements=30),
            )
            if status != 200 or plan["contract_name"] != "sheet_vitrina_v1_prices_spp_test_plan":
                raise AssertionError(f"plan route failed: {status} {plan}")
            if plan["plan"]["max_measurements"] != 30 or plan["plan"]["request_budget"]["wb_uploads"] != 30:
                raise AssertionError(f"plan route must accept max_measurements=30: {plan}")
            status, rejected = _post_json(
                f"{base_url}{DEFAULT_SHEET_PRICES_SPP_TEST_PLAN_PATH}",
                _start_payload(700, 900, max_measurements=31),
            )
            if status != 422 or "between 3 and 30" not in str(rejected.get("error") or ""):
                raise AssertionError(f"plan route must reject max_measurements=31: {status} {rejected}")
            status, started = _post_json(
                f"{base_url}{DEFAULT_SHEET_PRICES_SPP_TEST_START_PATH}",
                _start_payload(700, 900, max_measurements=3),
            )
            if status != 200 or started["contract_name"] != "sheet_vitrina_v1_prices_spp_test_start":
                raise AssertionError(f"start route failed: {status} {started}")
            status, status_payload = _get_json(f"{base_url}{DEFAULT_SHEET_PRICES_SPP_TEST_STATUS_PATH}")
            if status != 200 or status_payload["contract_name"] != "sheet_vitrina_v1_prices_spp_test_status":
                raise AssertionError(f"status route failed: {status} {status_payload}")
            status, restore = _post_json(
                f"{base_url}{DEFAULT_SHEET_PRICES_SPP_TEST_RESTORE_PATH}",
                {"job_id": started["job"]["job_id"], "confirm_restore": True},
            )
            if status != 200 or restore["contract_name"] != "sheet_vitrina_v1_prices_spp_test_restore":
                raise AssertionError(f"restore route failed: {status} {restore}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def _build_block(
    runtime: Any,
    runtime_dir: Path,
    prices_source: FakeSppPricesSource,
    public_source: FakePublicSource,
    *,
    zero_cadence: bool = True,
) -> WbSppTesterBlock:
    cadence_config = (
        WbSppTesterCadenceConfig(
            run_async=False,
            measurement_upload_cooldown_seconds=0,
            first_public_poll_delay_seconds=0,
            public_poll_gap_seconds=0,
            extended_public_poll_gap_seconds=0,
            upload_status_poll_seconds=0,
            upload_status_max_polls=2,
            readback_poll_seconds=0,
            readback_max_polls=2,
            rate_limit_min_cooldown_seconds=0,
            active_lock_ttl_seconds=60,
        )
        if zero_cadence
        else WbSppTesterCadenceConfig(run_async=False)
    )
    return WbSppTesterBlock(
        runtime=runtime,
        runtime_dir=runtime_dir,
        prices_source=prices_source,
        public_source=public_source,
        now_factory=lambda: NOW,
        timestamp_factory=lambda: "2026-07-07T07:00:00Z",
        sleep=lambda _seconds: None,
        safety_config=WbSppTesterSafetyConfig(spp_test_enabled=True, prices_write_enabled=True),
        cadence_config=cadence_config,
    )


def _start_payload(range_min: int, range_max: int, *, max_measurements: int = 3) -> dict[str, Any]:
    return {
        "nmID": PRIMARY_NM,
        "range_min_discounted": range_min,
        "range_max_discounted": range_max,
        "precision_rub": 2,
        "max_measurements": max_measurements,
        "mode": "safe_slow",
        "confirm_live_price_change": True,
        "restore_baseline": True,
    }


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str) -> tuple[int, Mapping[str, Any]]:
    with request.urlopen(url, timeout=10) as response:
        return int(response.status), json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: Mapping[str, Any]) -> tuple[int, Mapping[str, Any]]:
    req = request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=15) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return int(exc.status), json.loads(exc.read().decode("utf-8"))


if __name__ == "__main__":
    main()
