"""Smoke-check WB SPP tester with fake upstreams and no live WB writes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    DEFAULT_SHEET_PRICES_SPP_TEST_HISTORY_PATH,
    DEFAULT_SHEET_PRICES_SPP_TEST_PLAN_PATH,
    DEFAULT_SHEET_PRICES_SPP_TEST_RESTORE_PATH,
    DEFAULT_SHEET_PRICES_SPP_TEST_SCHEDULE_PATH,
    DEFAULT_SHEET_PRICES_SPP_TEST_START_PATH,
    DEFAULT_SHEET_PRICES_SPP_TEST_STATUS_PATH,
    DEFAULT_WB_BUYER_SESSION_CHECK_PATH,
    DEFAULT_WB_BUYER_RECOVERY_LAUNCHER_PATH,
    DEFAULT_WB_BUYER_RECOVERY_START_PATH,
    DEFAULT_WB_BUYER_RECOVERY_STATUS_PATH,
    DEFAULT_WB_BUYER_RECOVERY_STOP_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.adapters.wb_prices_management import WbPricesApiError  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.wb_spp_tester import (  # noqa: E402
    SppTesterPublicCardSource,
    WbSppTesterBlock,
    WbSppTesterCadenceConfig,
    WbSppTesterError,
    WbSppTesterSafetyConfig,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


NOW = datetime(2026, 7, 7, 7, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def timestamp(self) -> str:
        return self.value.isoformat()


class FakeSppPricesSource:
    def __init__(
        self,
        *,
        rate_limit_upload_attempts: int = 0,
        quarantine: bool = False,
        quarantine_after_upload: bool = False,
    ) -> None:
        self.rate_limit_upload_attempts = rate_limit_upload_attempts
        self.quarantine = quarantine
        self.quarantine_after_upload = quarantine_after_upload
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
        if self.quarantine or (self.quarantine_after_upload and self.upload_attempts > 0):
            rows.append({"nmID": PRIMARY_NM, "newPrice": 100, "oldPrice": 1000, "newDiscount": 10, "oldDiscount": 10})
        return {"data": {"quarantineGoods": rows[offset : offset + limit]}, "error": False, "errorText": ""}


class FakePublicSource:
    def __init__(
        self,
        prices: FakeSppPricesSource,
        *,
        stale: bool = False,
        timeout_reads: set[int] | None = None,
        rate_limit_reads: set[int] | None = None,
        raise_on_read: bool = False,
    ) -> None:
        self.prices = prices
        self.stale = stale
        self.timeout_reads = set(timeout_reads or set())
        self.rate_limit_reads = set(rate_limit_reads or set())
        self.raise_on_read = raise_on_read
        self.reads = 0
        self.destination_contexts: list[dict[str, Any]] = []

    def fetch_public_buyer_price(
        self,
        nm_id: int,
        *,
        destination_context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self.reads += 1
        if self.raise_on_read:
            raise RuntimeError("fake anonymous buyer diagnostic failure")
        normalized_context = dict(destination_context or {})
        self.destination_contexts.append(normalized_context)
        if self.reads in self.rate_limit_reads:
            return {
                "status": "429",
                "public_buyer_price": None,
                "endpoint": "fake_public_card",
                "http_status": 429,
                "headers": {},
                "body_summary": "rate limit",
                "diagnostics": {"read": self.reads},
                "destination_context": normalized_context,
            }
        if self.reads in self.timeout_reads:
            return {
                "status": "timeout",
                "public_buyer_price": None,
                "endpoint": "fake_public_card",
                "http_status": None,
                "headers": {},
                "body_summary": "timeout",
                "diagnostics": {"read": self.reads},
                "destination_context": normalized_context,
            }
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
            "destination_context": normalized_context,
        }


class FakeBuyerSource:
    def __init__(
        self,
        prices: FakeSppPricesSource,
        *,
        session_status: str = "valid",
        stale: bool = False,
        raise_on_price: bool = False,
    ) -> None:
        self.prices = prices
        self.session_status = session_status
        self.stale = stale
        self.raise_on_price = raise_on_price
        self.reads = 0

    def check_session(self) -> Mapping[str, Any]:
        labels = {"valid": "Действует", "missing": "Не установлена", "expired": "Истекла", "wrong_account": "Другой аккаунт"}
        return {
            "status": self.session_status,
            "status_label": labels.get(self.session_status, "Ошибка проверки"),
            "valid": self.session_status == "valid",
            "reason": "buyer_session_valid" if self.session_status == "valid" else "buyer_session_invalid",
            "checked_at": NOW.isoformat(),
            "session_fingerprint": "a" * 64 if self.session_status == "valid" else "",
            "account_confirmed": self.session_status == "valid",
        }

    def fetch_authenticated_buyer_price(self, nm_id: int) -> Mapping[str, Any]:
        self.reads += 1
        if self.raise_on_price:
            raise RuntimeError("fake authenticated buyer diagnostic failure")
        if self.session_status != "valid":
            return {
                "status": f"session_{self.session_status}",
                "reason": "buyer_session_invalid",
                "authenticated_buyer_price": None,
            }
        discounted = self.prices.discounted_price
        anonymous_spp = 0.10 if discounted < 800 else 0.14
        anonymous_price = round(discounted * (1 - anonymous_spp), 2)
        authenticated_price = 617.58 if self.stale else round(anonymous_price * 0.98, 2)
        return {
            "status": "ok",
            "nm_id": int(nm_id),
            "authenticated_buyer_price": authenticated_price,
            "normal_price": authenticated_price,
            "wallet_price": round(authenticated_price * 0.97, 2),
            "card_price": None,
            "club_price": None,
            "payment_context": "account_default_with_wallet_option",
            "destination_context": {"dest": "-6441813", "currency": "rub"},
            "measured_at": NOW.isoformat(),
            "source_method": "authenticated_browser_network_json:sizes.0.price.product",
            "source_endpoint": "https://card.wb.ru/cards/v4/detail",
            "session_fingerprint": "a" * 64,
            "freshness": {"live_read": True, "stability": "single_read"},
            "diagnostics": {"network_primary": True},
        }


class SessionLossBuyerSource(FakeBuyerSource):
    def __init__(self, prices: FakeSppPricesSource) -> None:
        super().__init__(prices)
        self.price_reads = 0
        self.lost = False

    def check_session(self) -> Mapping[str, Any]:
        self.session_status = "expired" if self.lost else "valid"
        return super().check_session()

    def fetch_authenticated_buyer_price(self, nm_id: int) -> Mapping[str, Any]:
        self.price_reads += 1
        if self.price_reads >= 2:
            self.lost = True
            return {
                "status": "session_expired",
                "reason": "buyer_login_required",
                "authenticated_buyer_price": None,
            }
        return super().fetch_authenticated_buyer_price(nm_id)


class FakeBuyerRecoveryController:
    def __init__(self) -> None:
        self.running = False

    def read_status(self, *, launcher_download_path: str, run_id: str | None = None, with_probe: bool = True) -> Mapping[str, Any]:
        return self._payload(launcher_download_path)

    def start(self, *, replace: bool, launcher_download_path: str) -> Mapping[str, Any]:
        self.running = True
        return self._payload(launcher_download_path)

    def stop(self, *, launcher_download_path: str) -> Mapping[str, Any]:
        self.running = False
        return self._payload(launcher_download_path)

    def build_launcher_archive(self, *, public_status_url: str, public_operator_url: str) -> tuple[bytes, str]:
        return b"safe-launcher", "wb-buyer-session-test.zip"

    def _payload(self, launcher_download_path: str) -> Mapping[str, Any]:
        return {
            "contract_name": "wb_buyer_session_recovery_v1",
            "run_id": "buyer-recovery-test",
            "status": "awaiting_login" if self.running else "stopped",
            "status_label": "Нужно войти" if self.running else "Остановлена",
            "status_tone": "warning" if self.running else "neutral",
            "running": self.running,
            "run_is_final": not self.running,
            "launcher_ready": self.running,
            "can_download_launcher": self.running,
            "launcher_download_path": launcher_download_path if self.running else "",
            "session": {"status": "missing", "valid": False},
        }


def main() -> None:
    _run_backend_unit_smokes()
    _run_http_smoke()
    print("wb_spp_tester_smoke: OK")


def _run_backend_unit_smokes() -> None:
    class MustNotFetch:
        def fetch(self, _request: Any) -> Mapping[str, Any]:
            raise AssertionError("invalid destination must fail before anonymous network fetch")

    invalid_destination = SppTesterPublicCardSource(source=MustNotFetch()).fetch_public_buyer_price(
        PRIMARY_NM,
        destination_context={"dest": "-6441813&unsafe=1", "currency": "rub"},
    )
    if invalid_destination.get("status") != "context_invalid":
        raise AssertionError("invalid authenticated destination must not fall back to module 35 default")

    with TemporaryDirectory(prefix="wb-spp-test-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = _seed_runtime(runtime_dir)

        source = FakeSppPricesSource()
        public_source = FakePublicSource(source)
        block = _build_block(runtime, runtime_dir, source, public_source)
        baseline = block.build_baseline({"nmID": PRIMARY_NM})
        if not baseline["baseline"]["can_start"] or baseline["baseline"]["discountedPrice"] != 900:
            raise AssertionError(f"baseline capture mismatch: {baseline}")
        if public_source.destination_contexts[0] != {"dest": "-6441813", "currency": "rub"}:
            raise AssertionError("anonymous control must inherit the authenticated buyer destination")
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
        completed_status = block.status({})
        if (
            completed_status["active_job"] is not None
            or completed_status["job"]["job_id"] != job["job_id"]
            or (completed_status["job"].get("lifecycle_diagnostics") or {})
            .get("restore_proof", {})
            .get("proof_status")
            != "confirmed"
        ):
            raise AssertionError(f"restored terminal job must clear active pointer but remain the latest job: {completed_status}")
        if (runtime_dir / "sheet_vitrina_v1_prices" / "spp_tests" / "current_job.json").exists():
            raise AssertionError("normal restored completion must remove current_job.json")

        invalid_session_source = FakeSppPricesSource()
        invalid_buyer = FakeBuyerSource(invalid_session_source, session_status="missing")
        invalid_session_block = _build_block(
            runtime,
            runtime_dir / "buyer_missing",
            invalid_session_source,
            FakePublicSource(invalid_session_source),
            buyer_source=invalid_buyer,
        )
        try:
            invalid_session_block.start(_start_payload(700, 900, max_measurements=3), actor="smoke")
        except WbSppTesterError as exc:
            if exc.payload.get("reason") != "buyer_session_invalid" or exc.payload.get("action") != "Установить сессию":
                raise
        else:
            raise AssertionError("missing buyer session must block before all seller writes")
        if invalid_session_source.upload_payloads:
            raise AssertionError("missing buyer session must not fall back to anonymous or write a seller price")

        loss_prices = FakeSppPricesSource()
        loss_buyer = SessionLossBuyerSource(loss_prices)
        loss_block = _build_block(
            runtime,
            runtime_dir / "buyer_loss",
            loss_prices,
            FakePublicSource(loss_prices),
            buyer_source=loss_buyer,
        )
        loss_job = loss_block.start(_start_payload(700, 900, max_measurements=3), actor="smoke")["job"]
        if loss_job["measurements"][0]["status"] != "buyer_session_lost":
            raise AssertionError(f"session loss during measurement must stop further reads: {loss_job}")
        if not loss_job["restore"]["restored"] or loss_prices.good["discount"] != 10 or loss_prices.discounted_price != 900.0:
            raise AssertionError(f"seller tuple restore must not depend on buyer price availability: {loss_job['restore']}")
        if len(loss_prices.upload_payloads) != 2:
            raise AssertionError("session loss must stop after one measurement write and one exact restore write")

        schedule_clock = MutableClock(NOW)
        schedule_prices = FakeSppPricesSource()
        schedule_buyer = FakeBuyerSource(schedule_prices)
        schedule_block = _build_block(
            runtime,
            runtime_dir / "buyer_schedule_skip",
            schedule_prices,
            FakePublicSource(schedule_prices),
            clock=schedule_clock,
            buyer_source=schedule_buyer,
        )
        saved_schedule = schedule_block.save_schedule(
            {
                "enabled": True,
                "nmID": PRIMARY_NM,
                "range_min_discounted": 700,
                "range_max_discounted": 900,
                "precision_rub": 2,
                "max_measurements": 3,
                "local_time_hhmm": "12:01",
                "timezone": "Asia/Yekaterinburg",
                "future_live_price_changes_confirmed": True,
            },
            actor="smoke",
        )["schedule"]
        schedule_buyer.session_status = "expired"
        schedule_clock.value = datetime.fromisoformat(saved_schedule["next_run_at"]) + timedelta(minutes=1)
        tick = schedule_block.run_due_schedule_tick()
        if tick["status"] != "skipped" or tick["job"]["result_status"] != "buyer_session_invalid":
            raise AssertionError(f"scheduled invalid session must produce observable skip: {tick}")
        if schedule_prices.upload_payloads:
            raise AssertionError("scheduled invalid session must not write seller prices")

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

        repeated_rate_source = FakeSppPricesSource(rate_limit_upload_attempts=3)
        repeated_rate_block = _build_block(
            runtime,
            runtime_dir / "repeated_rate",
            repeated_rate_source,
            FakePublicSource(repeated_rate_source),
        )
        repeated_rate_job = repeated_rate_block.start(_start_payload(700, 900, max_measurements=3), actor="smoke")["job"]
        repeated_rate_audit = (
            runtime_dir / "repeated_rate" / "sheet_vitrina_v1_prices" / "spp_tests" / "audit.jsonl"
        ).read_text(encoding="utf-8")
        if (
            repeated_rate_source.upload_attempts != 3
            or repeated_rate_job["measurements"][0]["status"] != "rate_limited_stop"
            or not repeated_rate_job["restore"]["restored"]
            or "wb_prices_429_stop" not in repeated_rate_audit
        ):
            raise AssertionError(f"repeated 429 must stop probing and enter seller restore: {repeated_rate_job}")

        high_source = FakeSppPricesSource()
        high_block = _build_block(runtime, runtime_dir / "high", high_source, FakePublicSource(high_source))
        high_job = high_block.start(_start_payload(1400, 1600, max_measurements=3), actor="smoke")["job"]
        if not any(step.get("kind") == "bridge" for step in high_job["restore"]["steps"]):
            raise AssertionError(f"large downward restore must use bridge steps, got: {high_job['restore']['steps']}")
        if any(
            step.get("status") != "ok"
            or (step.get("quarantine") or {}).get("is_quarantined")
            or (step.get("readback") or {}).get("discountedPrice") != step.get("expected_discounted_price")
            for step in high_job["restore"]["steps"]
        ):
            raise AssertionError(f"every bridge/final seller step needs readback and quarantine proof: {high_job['restore']}")

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
        if (
            restored["status"] != "restored"
            or restored["job"]["status"] != "interrupted_restored"
            or restored["job"]["result_status"] != "inconclusive"
            or not restored["job"]["restore"]["restored"]
            or restored["active_job"] is not None
        ):
            raise AssertionError(f"emergency restore failed: {restored}")
        emergency_current = runtime_dir / "emergency" / "sheet_vitrina_v1_prices" / "spp_tests" / "current_job.json"
        if emergency_current.exists():
            raise AssertionError("emergency seller restore proof must clear current_job.json")

        diagnostic_source = FakeSppPricesSource()
        diagnostic_buyer = FakeBuyerSource(diagnostic_source)
        diagnostic_public = FakePublicSource(diagnostic_source)
        diagnostic_block = _build_block(
            runtime,
            runtime_dir / "restore_diagnostics",
            diagnostic_source,
            diagnostic_public,
            buyer_source=diagnostic_buyer,
        )
        diagnostic_job = dict(manual_job)
        diagnostic_job["job_id"] = "already_baseline_diagnostic_failure"
        diagnostic_job["baseline"] = diagnostic_block._capture_baseline(nm_id=PRIMARY_NM, strict=False)
        diagnostic_job["measurements"] = []
        diagnostic_buyer.raise_on_price = True
        diagnostic_public.raise_on_read = True
        diagnostic_block._save_job(diagnostic_job)
        diagnostic_block._write_current_job(diagnostic_job)
        diagnostic_restored = diagnostic_block.restore(
            {"job_id": diagnostic_job["job_id"], "confirm_restore": True},
            actor="smoke",
        )
        diagnostic_repeated = diagnostic_block.restore(
            {"job_id": diagnostic_job["job_id"], "confirm_restore": True},
            actor="smoke",
        )
        diagnostic_proof = diagnostic_repeated["job"]["restore"]["proof"]
        if (
            diagnostic_source.upload_payloads
            or diagnostic_restored["job"]["status"] != "interrupted_restored"
            or diagnostic_repeated["job"]["status"] != "interrupted_restored"
            or diagnostic_proof.get("proof_status") != "confirmed"
            or diagnostic_proof.get("authenticated_status") != "probe_error"
            or diagnostic_proof.get("anonymous_status") != "probe_error"
            or (diagnostic_repeated["job"].get("lifecycle_diagnostics") or {})
            .get("restore_proof", {})
            .get("proof_status")
            != "confirmed"
            or diagnostic_repeated["active_job"] is not None
        ):
            raise AssertionError(
                f"already-baseline idempotent restore must ignore buyer diagnostics and clear lock: {diagnostic_repeated}"
            )

        timeout_source = FakeSppPricesSource()
        timeout_public = FakePublicSource(timeout_source, timeout_reads={2, 3, 4})
        timeout_block = _build_block(runtime, runtime_dir / "timeout", timeout_source, timeout_public)
        timeout_job = timeout_block.start(_start_payload(700, 900, max_measurements=3), actor="smoke")["job"]
        if timeout_job["measurements"][0]["status"] != "public_unstable" or not timeout_job["restore"]["restored"]:
            raise AssertionError(f"public timeout must stop probing and restore: {timeout_job}")

        public_preflight_source = FakeSppPricesSource()
        public_preflight_block = _build_block(
            runtime,
            runtime_dir / "public_preflight_429",
            public_preflight_source,
            FakePublicSource(public_preflight_source, rate_limit_reads={1}),
        )
        try:
            public_preflight_block.start(_start_payload(700, 900, max_measurements=3), actor="smoke")
        except WbSppTesterError as exc:
            if exc.http_status != 422 or "public_spp_baseline_incomplete" not in set(exc.payload.get("blockers") or []):
                raise
        else:
            raise AssertionError("unsafe public baseline must block before a live write")
        if public_preflight_source.upload_payloads:
            raise AssertionError("unsafe public baseline performed an unexpected price write")

        public_429_source = FakeSppPricesSource()
        public_429 = FakePublicSource(public_429_source, rate_limit_reads={2})
        public_429_block = _build_block(runtime, runtime_dir / "public_429", public_429_source, public_429)
        public_429_job = public_429_block.start(_start_payload(700, 900, max_measurements=3), actor="smoke")["job"]
        if public_429_job["measurements"][0]["status"] != "public_429" or not public_429_job["restore"]["restored"]:
            raise AssertionError(f"public 429 must stop probing and restore: {public_429_job}")

        quarantine_source = FakeSppPricesSource(quarantine_after_upload=True)
        quarantine_block = _build_block(runtime, runtime_dir / "quarantine", quarantine_source, FakePublicSource(quarantine_source))
        quarantine_job = quarantine_block.start(_start_payload(700, 900, max_measurements=3), actor="smoke")["job"]
        if quarantine_job["status"] != "manual_restore_required" or quarantine_job["measurements"][0]["status"] != "quarantine_detected":
            raise AssertionError(f"quarantine must stop probing and require guarded remediation: {quarantine_job}")

        orphan_source = FakeSppPricesSource()
        orphan_block = _build_block(runtime, runtime_dir / "orphan", orphan_source, FakePublicSource(orphan_source))
        active_job = dict(manual_job)
        active_job["job_id"] = "active_job"
        active_job["status"] = "measuring"
        active_job["restore"] = {"required": True, "restored": False, "proof": None, "steps": []}
        orphan_block._save_job(active_job)
        orphan_block._write_current_job(active_job)
        orphan_status = orphan_block.status({})
        reconciled = orphan_status["job"]
        if (
            reconciled["status"] != "interrupted_restored"
            or not reconciled["restore"]["restored"]
            or (reconciled.get("lifecycle_diagnostics") or {}).get("restore_proof", {}).get("proof_status")
            != "confirmed"
            or orphan_status["active_job"] is not None
            or (runtime_dir / "orphan" / "sheet_vitrina_v1_prices" / "spp_tests" / "current_job.json").exists()
        ):
            raise AssertionError(f"orphan with fresh baseline proof must become terminal and clear active lock: {orphan_status}")

        unrestored_source = FakeSppPricesSource()
        unrestored_block = _build_block(runtime, runtime_dir / "unrestored", unrestored_source, FakePublicSource(unrestored_source))
        unrestored_source.upload_task([{"nmID": PRIMARY_NM, "price": 1500, "discount": 10}])
        unrestored_job = dict(active_job)
        unrestored_job["job_id"] = "unrestored_job"
        unrestored_block._save_job(unrestored_job)
        unrestored_block._write_current_job(unrestored_job)
        try:
            unrestored_block.start(_start_payload(700, 900), actor="smoke")
        except WbSppTesterError as exc:
            if exc.http_status != 409 or exc.payload.get("reason") != "active_or_unrestored_job":
                raise
        else:
            raise AssertionError("unrestored orphan must reject a new SPP job")
        if unrestored_block.status({})["job"]["status"] != "manual_restore_required":
            raise AssertionError("unrestored orphan must become manual_restore_required")
        unrestored_restore = unrestored_block.restore(
            {"job_id": "unrestored_job", "confirm_restore": True},
            actor="smoke",
        )
        if (
            unrestored_restore["status"] != "restored"
            or not unrestored_source.upload_payloads
            or unrestored_restore["active_job"] is not None
        ):
            raise AssertionError(f"not-baseline orphan restore must stage seller restore and clear lock: {unrestored_restore}")

        lock_source = FakeSppPricesSource()
        lock_block = _build_block(runtime, runtime_dir / "lock", lock_source, FakePublicSource(lock_source))
        held_lock = lock_block._acquire_execution_lock(owner="smoke_contention", blocking=False)
        try:
            try:
                lock_block.start(_start_payload(700, 900), actor="smoke")
            except WbSppTesterError as exc:
                if exc.http_status != 409 or exc.payload.get("reason") != "execution_lock_busy":
                    raise
            else:
                raise AssertionError("live execution lock must reject a concurrent manual start")
        finally:
            lock_block._release_execution_lock(held_lock, job_id="")

        legacy_job = dict(manual_job)
        legacy_job["job_id"] = "legacy_without_trigger"
        legacy_job.pop("trigger_source", None)
        legacy_job["lifecycle_diagnostics"] = {
            **dict(legacy_job.get("lifecycle_diagnostics") or {}),
            "internal_path": "/opt/wb-core-runtime/state/private.json",
            "authorization_header": "Bearer must-not-leak",
        }
        block._save_job(legacy_job)
        history_page = block.history({"limit": 1})
        if len(history_page["items"]) != 1 or not history_page["has_more"] or not history_page["next_cursor"]:
            raise AssertionError(f"bounded history pagination mismatch: {history_page}")
        history_next = block.history({"limit": 5, "cursor": history_page["next_cursor"]})
        if not history_next["items"]:
            raise AssertionError(f"history cursor did not return the next page: {history_next}")
        legacy_summary = next(
            (item for item in [*history_page["items"], *history_next["items"]] if item["job_id"] == "legacy_without_trigger"),
            None,
        )
        if legacy_summary is None or legacy_summary["trigger_source"] is not None:
            raise AssertionError(f"legacy trigger source must stay unknown: {history_next}")
        detail = block.history_detail("legacy_without_trigger")["job"]
        detail_lifecycle = detail.get("lifecycle_diagnostics") or {}
        if (
            "internal_path" in detail_lifecycle
            or "authorization_header" in detail_lifecycle
            or "trigger_source" in detail and detail["trigger_source"]
        ):
            raise AssertionError(f"history detail leaked unsafe or fabricated fields: {detail}")
        try:
            block.history_detail("../audit")
        except WbSppTesterError as exc:
            if exc.http_status != 400:
                raise
        else:
            raise AssertionError("history detail must reject path traversal")

    _run_schedule_smokes()


def _run_schedule_smokes() -> None:
    with TemporaryDirectory(prefix="wb-spp-schedule-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = _seed_runtime(runtime_dir)
        clock = MutableClock(datetime(2026, 7, 7, 7, 0, tzinfo=timezone.utc))
        source = FakeSppPricesSource()
        block = _build_block(runtime, runtime_dir, source, FakePublicSource(source), clock=clock)
        schedule_payload = {
            "enabled": True,
            "nmID": PRIMARY_NM,
            "range_min_discounted": 700,
            "range_max_discounted": 900,
            "precision_rub": 2,
            "max_measurements": 3,
            "local_time_hhmm": "12:05",
            "timezone": "Asia/Yekaterinburg",
            "future_live_price_changes_confirmed": True,
        }
        saved = block.save_schedule({"schedule": schedule_payload}, actor="smoke")["schedule"]
        if source.upload_payloads:
            raise AssertionError("saving an enabled schedule must not start a job immediately")
        if saved["next_run_at"] != "2026-07-07T12:05:00+05:00":
            raise AssertionError(f"next_run_at mismatch: {saved}")
        if block.run_due_schedule_tick()["status"] != "not_due":
            raise AssertionError("schedule must wait for its assigned time")

        clock.value = datetime(2026, 7, 7, 7, 5, tzinfo=timezone.utc)
        first_tick = block.run_due_schedule_tick()
        first_job = first_tick.get("job") or {}
        if (
            first_tick["status"] != "finished"
            or first_job.get("trigger_source") != "schedule"
            or first_job.get("status") != "complete"
            or not (first_job.get("restore") or {}).get("restored")
            or not (first_job.get("input") or {}).get("restore_baseline")
        ):
            raise AssertionError(f"scheduled job did not use mandatory shared restore path: {first_tick}")
        upload_count = len(source.upload_payloads)
        if block.run_due_schedule_tick()["status"] != "not_due" or len(source.upload_payloads) != upload_count:
            raise AssertionError("schedule must be at-most-once for the claimed business date")
        restarted = _build_block(runtime, runtime_dir, source, FakePublicSource(source), clock=clock)
        if restarted.run_due_schedule_tick()["status"] != "not_due" or len(source.upload_payloads) != upload_count:
            raise AssertionError("schedule claim must remain idempotent after restart")
        disabled = restarted.save_schedule(
            {"schedule": {**schedule_payload, "enabled": False, "future_live_price_changes_confirmed": False}},
            actor="smoke",
        )["schedule"]
        if disabled["enabled"] or disabled["next_run_at"]:
            raise AssertionError(f"disabled schedule state mismatch: {disabled}")

    with TemporaryDirectory(prefix="wb-spp-late-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = _seed_runtime(runtime_dir)
        clock = MutableClock(datetime(2026, 7, 7, 7, 0, tzinfo=timezone.utc))
        source = FakeSppPricesSource()
        block = _build_block(runtime, runtime_dir, source, FakePublicSource(source), clock=clock, schedule_late_window_minutes=15)
        block.save_schedule(
            {
                "enabled": True,
                "nmID": PRIMARY_NM,
                "range_min_discounted": 700,
                "range_max_discounted": 900,
                "precision_rub": 2,
                "max_measurements": 3,
                "local_time_hhmm": "12:01",
                "timezone": "Asia/Yekaterinburg",
                "future_live_price_changes_confirmed": True,
            },
            actor="smoke",
        )
        clock.value = datetime(2026, 7, 7, 7, 17, tzinfo=timezone.utc)
        late = block.run_due_schedule_tick()
        if late["status"] != "skipped_late" or (late.get("job") or {}).get("result_status") != "missed_late_window":
            raise AssertionError(f"late-run policy mismatch: {late}")
        if source.upload_payloads:
            raise AssertionError("late schedule skip must not mutate WB prices")

    with TemporaryDirectory(prefix="wb-spp-contention-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = _seed_runtime(runtime_dir)
        clock = MutableClock(datetime(2026, 7, 7, 7, 0, tzinfo=timezone.utc))
        source = FakeSppPricesSource()
        block = _build_block(runtime, runtime_dir, source, FakePublicSource(source), clock=clock)
        block.save_schedule(
            {
                "enabled": True,
                "nmID": PRIMARY_NM,
                "range_min_discounted": 700,
                "range_max_discounted": 900,
                "precision_rub": 2,
                "max_measurements": 3,
                "local_time_hhmm": "12:01",
                "timezone": "Asia/Yekaterinburg",
                "future_live_price_changes_confirmed": True,
            },
            actor="smoke",
        )
        held = block._acquire_execution_lock(owner="manual_job", blocking=False)
        clock.value = datetime(2026, 7, 7, 7, 1, tzinfo=timezone.utc)
        try:
            contention = block.run_due_schedule_tick()
        finally:
            block._release_execution_lock(held, job_id="")
        if contention["status"] != "skipped" or (contention.get("job") or {}).get("result_status") != "execution_lock_busy":
            raise AssertionError(f"manual/scheduled lock contention mismatch: {contention}")
        if source.upload_payloads:
            raise AssertionError("scheduled contention skip must not mutate WB prices")

    with TemporaryDirectory(prefix="wb-spp-safety-skip-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = _seed_runtime(runtime_dir)
        clock = MutableClock(datetime(2026, 7, 7, 7, 0, tzinfo=timezone.utc))
        source = FakeSppPricesSource(quarantine=True)
        block = _build_block(runtime, runtime_dir, source, FakePublicSource(source), clock=clock)
        block.save_schedule(
            {
                "enabled": True,
                "nmID": PRIMARY_NM,
                "range_min_discounted": 700,
                "range_max_discounted": 900,
                "precision_rub": 2,
                "max_measurements": 3,
                "local_time_hhmm": "12:01",
                "timezone": "Asia/Yekaterinburg",
                "future_live_price_changes_confirmed": True,
            },
            actor="smoke",
        )
        clock.value = datetime(2026, 7, 7, 7, 1, tzinfo=timezone.utc)
        skipped = block.run_due_schedule_tick()
        if skipped["status"] != "skipped" or (skipped.get("job") or {}).get("result_status") != "safety_blocker":
            raise AssertionError(f"scheduled safety skip mismatch: {skipped}")
        if source.upload_payloads:
            raise AssertionError("scheduled safety skip must not mutate WB prices")


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
            buyer_session_recovery_controller=FakeBuyerRecoveryController(),  # type: ignore[arg-type]
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
            status, buyer_session = _get_json(f"{base_url}{DEFAULT_WB_BUYER_SESSION_CHECK_PATH}")
            if status != 200 or buyer_session.get("status") != "valid" or not buyer_session.get("valid"):
                raise AssertionError(f"buyer session check route failed: {status} {buyer_session}")
            if any(marker in json.dumps(buyer_session).lower() for marker in ("cookie", "authorization", "phone", "otp", "storage_state")):
                raise AssertionError("buyer session route leaked sensitive fields")
            status, recovery_start = _post_json(f"{base_url}{DEFAULT_WB_BUYER_RECOVERY_START_PATH}", {"replace": True})
            if status != 200 or not recovery_start.get("launcher_ready"):
                raise AssertionError(f"buyer recovery start route failed: {status} {recovery_start}")
            status, recovery_status = _get_json(f"{base_url}{DEFAULT_WB_BUYER_RECOVERY_STATUS_PATH}?probe=false")
            if status != 200 or recovery_status.get("status") != "awaiting_login":
                raise AssertionError(f"buyer recovery status route failed: {status} {recovery_status}")
            with request.urlopen(f"{base_url}{DEFAULT_WB_BUYER_RECOVERY_LAUNCHER_PATH}", timeout=10) as response:
                if response.status != 200 or response.read() != b"safe-launcher":
                    raise AssertionError("buyer recovery launcher route failed")
            status, recovery_stop = _post_json(f"{base_url}{DEFAULT_WB_BUYER_RECOVERY_STOP_PATH}", {})
            if status != 200 or recovery_stop.get("running"):
                raise AssertionError(f"buyer recovery stop route failed: {status} {recovery_stop}")
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
            uploads_before_schedule_save = len(source.upload_payloads)
            status, schedule = _post_json(
                f"{base_url}{DEFAULT_SHEET_PRICES_SPP_TEST_SCHEDULE_PATH}",
                {
                    "schedule": {
                        "enabled": True,
                        "nmID": PRIMARY_NM,
                        "range_min_discounted": 700,
                        "range_max_discounted": 900,
                        "precision_rub": 2,
                        "max_measurements": 3,
                        "local_time_hhmm": "12:05",
                        "timezone": "Asia/Yekaterinburg",
                        "future_live_price_changes_confirmed": True,
                    }
                },
            )
            if status != 200 or schedule["contract_name"] != "sheet_vitrina_v1_prices_spp_test_schedule":
                raise AssertionError(f"schedule save route failed: {status} {schedule}")
            if len(source.upload_payloads) != uploads_before_schedule_save:
                raise AssertionError("schedule HTTP save must not start a price job immediately")
            status, schedule_read = _get_json(f"{base_url}{DEFAULT_SHEET_PRICES_SPP_TEST_SCHEDULE_PATH}")
            if status != 200 or not schedule_read["schedule"]["enabled"]:
                raise AssertionError(f"schedule read route failed: {status} {schedule_read}")
            status, history = _get_json(f"{base_url}{DEFAULT_SHEET_PRICES_SPP_TEST_HISTORY_PATH}?limit=1")
            if status != 200 or not history["items"] or history["items"][0]["trigger_source"] != "manual":
                raise AssertionError(f"history route failed: {status} {history}")
            history_job_id = history["items"][0]["job_id"]
            status, history_detail = _get_json(f"{base_url}{DEFAULT_SHEET_PRICES_SPP_TEST_HISTORY_PATH}/{history_job_id}")
            if status != 200 or history_detail["job"]["job_id"] != history_job_id:
                raise AssertionError(f"history detail route failed: {status} {history_detail}")
            status, invalid_detail = _get_json(f"{base_url}{DEFAULT_SHEET_PRICES_SPP_TEST_HISTORY_PATH}/..%2Faudit")
            if status != 400 or "invalid" not in str(invalid_detail.get("error") or ""):
                raise AssertionError(f"history path traversal must be rejected: {status} {invalid_detail}")
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
    clock: MutableClock | None = None,
    schedule_late_window_minutes: int = 15,
    buyer_source: Any | None = None,
) -> WbSppTesterBlock:
    clock = clock or MutableClock(NOW)
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
            schedule_late_window_minutes=schedule_late_window_minutes,
        )
        if zero_cadence
        else WbSppTesterCadenceConfig(run_async=False)
    )
    return WbSppTesterBlock(
        runtime=runtime,
        runtime_dir=runtime_dir,
        prices_source=prices_source,
        public_source=public_source,
        buyer_source=buyer_source or FakeBuyerSource(prices_source, stale=public_source.stale),
        now_factory=clock.now,
        timestamp_factory=clock.timestamp,
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
    try:
        with request.urlopen(url, timeout=10) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return int(exc.status), json.loads(exc.read().decode("utf-8"))


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
