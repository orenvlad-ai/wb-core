"""Application smoke for the manual WB SPP price checker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import threading
import time
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_spp_tester import (  # noqa: E402
    WbSppTesterBlock,
    WbSppTesterCadenceConfig,
    WbSppTesterError,
    WbSppTesterSafetyConfig,
)
from packages.contracts.wb_price_quarantine import evaluate_wb_price_quarantine_transition  # noqa: E402

PRIMARY_NM = 210183919
BASELINE = {"price": 1000, "discount": 10, "discountedPrice": 900.0}


class FakeRuntime:
    def list_nomenclature_items(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        assert active_only is True
        return [{
            "nm_id": PRIMARY_NM,
            "our_sku": "SKU-PRIMARY",
            "nomenclature_name": "Тестовый товар",
        }]


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self.value

    def timestamp(self) -> str:
        with self._lock:
            self.value += timedelta(seconds=1)
            return self.value.isoformat()


class FakePricesSource:
    def __init__(
        self,
        *,
        editable_size_price: bool = False,
        quarantine: bool = False,
        refuse_restore: bool = False,
        drift_on_goods_read: int | None = None,
    ) -> None:
        self.editable_size_price = editable_size_price
        self.quarantine = quarantine
        self.refuse_restore = refuse_restore
        self.drift_on_goods_read = drift_on_goods_read
        self.goods_reads = 0
        self.good = {
            "nmID": PRIMARY_NM,
            "vendorCode": "VC-PRIMARY",
            "currencyIsoCode4217": "RUB",
            "discount": BASELINE["discount"],
            "clubDiscount": 0,
            "editableSizePrice": editable_size_price,
            "wholesaleDiscountThreshold": [],
            "isBadTurnover": False,
            "sizes": [{
                "sizeID": 11,
                "techSizeName": "0",
                "price": BASELINE["price"],
                "discountedPrice": BASELINE["discountedPrice"],
                "clubDiscountedPrice": BASELINE["discountedPrice"],
            }],
        }
        self.upload_payloads: list[list[dict[str, Any]]] = []
        self.next_upload_id = 7000

    @property
    def discounted_price(self) -> float:
        return float(self.good["sizes"][0]["discountedPrice"])

    def fetch_goods(self, *, limit: int, offset: int, filter_nm_id: int | None = None) -> Mapping[str, Any]:
        rows = [] if filter_nm_id not in {None, PRIMARY_NM} else [self.good]
        return {"data": {"listGoods": rows[offset : offset + limit]}, "error": False, "errorText": ""}

    def fetch_goods_by_nm_ids(self, nm_ids: Sequence[int]) -> Mapping[str, Any]:
        self.goods_reads += 1
        if self.drift_on_goods_read == self.goods_reads:
            self.good["sizes"][0]["price"] = 1200
            self.good["sizes"][0]["discountedPrice"] = 1080.0
            self.good["sizes"][0]["clubDiscountedPrice"] = 1080.0
        rows = [self.good] if PRIMARY_NM in {int(value) for value in nm_ids} else []
        return {"data": {"listGoods": rows}, "error": False, "errorText": ""}

    def upload_task(self, goods: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        copied = [dict(item) for item in goods]
        self.upload_payloads.append(copied)
        for item in copied:
            is_restore = item.get("discount") is not None and int(item.get("price") or 0) == BASELINE["price"]
            if self.refuse_restore and is_restore:
                continue
            if item.get("discount") is not None:
                self.good["discount"] = int(item["discount"])
            if item.get("price") is not None:
                self.good["sizes"][0]["price"] = int(item["price"])
            discounted = round(
                float(self.good["sizes"][0]["price"]) * (100 - float(self.good["discount"])) / 100,
                2,
            )
            self.good["sizes"][0]["discountedPrice"] = discounted
            self.good["sizes"][0]["clubDiscountedPrice"] = discounted
        self.next_upload_id += 1
        return {
            "data": {"id": self.next_upload_id, "alreadyExists": False},
            "headers": {"Authorization": "must-not-leak"},
            "access_token": "must-not-leak",
            "error": False,
        }

    def fetch_upload_status(self, upload_id: int) -> Mapping[str, Any]:
        return {"data": {"uploadID": int(upload_id), "status": 3}, "error": False, "errorText": ""}

    def fetch_upload_goods(self, *, upload_id: int, limit: int, offset: int) -> Mapping[str, Any]:
        return {"data": {"uploadID": int(upload_id), "historyGoods": []}, "error": False, "errorText": ""}

    def fetch_quarantine_goods(self, *, limit: int, offset: int) -> Mapping[str, Any]:
        rows = [{"nmID": PRIMARY_NM, "newPrice": 100, "oldPrice": 1000}] if self.quarantine else []
        return {"data": {"quarantineGoods": rows[offset : offset + limit]}, "error": False, "errorText": ""}


class FakeBuyerSource:
    def __init__(
        self,
        prices: FakePricesSource,
        *,
        logged_out: bool = False,
        fail_capability_calls: set[int] | None = None,
    ) -> None:
        self.prices = prices
        self.logged_out = logged_out
        self.fail_capability_calls = set(fail_capability_calls or set())
        self.capability_calls = 0
        self.price_reads = 0

    def check_spp_capability(self) -> Mapping[str, Any]:
        self.capability_calls += 1
        blocked = self.logged_out or self.capability_calls in self.fail_capability_calls
        return {
            "status": "logged_out" if blocked else "valid",
            "status_label": "Разлогинен" if blocked else "Готов",
            "valid": not blocked,
            "capability": "authenticated_buyer_price",
            "capability_status": "blocked_by_auth" if blocked else "available",
            "capability_valid": not blocked,
            "capability_checked_at": "2026-08-08T08:00:00+00:00",
            "reason": "buyer_login_required" if blocked else "authenticated_price_available",
        }

    def fetch_authenticated_buyer_price(self, nm_id: int) -> Mapping[str, Any]:
        self.price_reads += 1
        if self.logged_out:
            return {"status": "session_logged_out", "authenticated_buyer_price": None}
        buyer_price = round(self.prices.discounted_price * 0.90, 2)
        return {
            "status": "ok",
            "nm_id": int(nm_id),
            "authenticated_buyer_price": buyer_price,
            "payment_context": "account_default",
            "destination_context": {"dest": "-6441813", "currency": "rub", "locale": "ru"},
            "session_fingerprint": "a" * 64,
            "authenticated_session_proof": True,
            "persistent_profile": True,
        }


class BlockingCooldown:
    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()

    def __call__(self, seconds: float) -> None:
        if float(seconds) >= 1 and not self.release.is_set():
            self.reached.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError("progressive smoke cooldown was not released")


def _payload(prices: Sequence[Any]) -> dict[str, Any]:
    return {
        "nmID": PRIMARY_NM,
        "price_count": len(prices),
        "prices": list(prices),
        "confirm_live_price_change": True,
        "restore_baseline": True,
    }


def build_block(
    runtime_dir: Path,
    prices: FakePricesSource | None = None,
    buyer: FakeBuyerSource | None = None,
    *,
    run_async: bool = False,
    sleep: Any = None,
    cooldown_seconds: int = 0,
) -> tuple[WbSppTesterBlock, FakePricesSource, FakeBuyerSource]:
    prices = prices or FakePricesSource()
    buyer = buyer or FakeBuyerSource(prices)
    clock = MutableClock()
    block = WbSppTesterBlock(
        runtime=FakeRuntime(),
        runtime_dir=runtime_dir,
        prices_source=prices,
        buyer_source=buyer,
        now_factory=clock.now,
        timestamp_factory=clock.timestamp,
        sleep=sleep or (lambda _seconds: None),
        safety_config=WbSppTesterSafetyConfig(spp_test_enabled=True, prices_write_enabled=True),
        cadence_config=WbSppTesterCadenceConfig(
            run_async=run_async,
            measurement_upload_cooldown_seconds=cooldown_seconds,
            first_buyer_poll_delay_seconds=0,
            buyer_poll_gap_seconds=0,
            upload_status_poll_seconds=0,
            upload_status_max_polls=2,
            readback_poll_seconds=0,
            readback_max_polls=2,
            rate_limit_min_cooldown_seconds=0,
            active_lock_ttl_seconds=60,
        ),
    )
    return block, prices, buyer


def assert_restored(job: Mapping[str, Any], source: FakePricesSource) -> None:
    proof = dict((job.get("restore") or {}))
    if not (
        proof.get("restored") is True
        and proof.get("proof_status") == "confirmed"
        and proof.get("price_matches") is True
        and proof.get("discount_matches") is True
        and proof.get("discountedPrice_matches") is True
        and proof.get("quarantine_absent") is True
    ):
        raise AssertionError(f"restore proof is incomplete: {proof}")
    actual = {
        "price": source.good["sizes"][0]["price"],
        "discount": source.good["discount"],
        "discountedPrice": source.good["sizes"][0]["discountedPrice"],
    }
    if actual != BASELINE:
        raise AssertionError(f"seller tuple was not restored: {actual}")


def _expect_validation_error(block: WbSppTesterBlock, prices: Sequence[Any]) -> None:
    try:
        block.start(_payload(prices), actor="smoke")
    except WbSppTesterError as exc:
        if exc.http_status not in {400, 422}:
            raise AssertionError(f"unexpected validation status: {exc.http_status}") from exc
    else:
        raise AssertionError(f"invalid prices must be rejected: {prices}")


def _run_validation_and_exact_order() -> None:
    with TemporaryDirectory(prefix="spp-manual-validation-") as raw:
        block, source, buyer = build_block(Path(raw))
        for invalid in ([], [""], [0], [-1], ["abc"], ["1.234"], [1, 2, 3, 4, 5, 6, 7]):
            _expect_validation_error(block, invalid)
        if source.upload_payloads or buyer.capability_calls:
            raise AssertionError("input validation must finish before bot preflight or seller writes")

        one = block.start(_payload([810]), actor="smoke")["job"]
        if one["target_prices"] != [810.0] or len(one["measurements"]) != 1:
            raise AssertionError(f"one-price contract mismatch: {one}")
        assert_restored(one, source)

        six_prices = [810, 805, 800, 795, 790, 785]
        six = block.start(_payload(six_prices), actor="smoke")["job"]
        if six["target_prices"] != [float(value) for value in six_prices] or len(six["measurements"]) != 6:
            raise AssertionError(f"six-price exact order mismatch: {six}")
        assert_restored(six, source)

        repeated = [810, 805, 810]
        exact = block.start(_payload(repeated), actor="smoke")["job"]
        if [row["target_price"] for row in exact["measurements"]] != [float(value) for value in repeated]:
            raise AssertionError("manual prices must remain ordered, including duplicates")
        measurement_writes = [
            row[0] for row in source.upload_payloads if row and row[0].get("discount") is None
        ]
        if len(measurement_writes) != 10:
            raise AssertionError("only the explicitly requested 1 + 6 + 3 measurement writes are allowed")
        if buyer.capability_calls != 10:
            raise AssertionError(
                "each Start preflight must cover price one, with fresh preflights before every later seller write"
            )


def _run_logged_out_zero_writes() -> None:
    with TemporaryDirectory(prefix="spp-manual-logged-out-") as raw:
        source = FakePricesSource()
        buyer = FakeBuyerSource(source, logged_out=True)
        block, _, _ = build_block(Path(raw), source, buyer)
        try:
            block.start(_payload([810]), actor="smoke")
        except WbSppTesterError as exc:
            if exc.payload.get("reason") != "buyer_logged_out":
                raise AssertionError(f"wrong logged-out error: {exc.payload}") from exc
            log_events = exc.payload.get("log_events") or []
            if not log_events or log_events[-1].get("stage") != "Проверка бота":
                raise AssertionError(f"logged-out Start must return its sanitized technical log: {exc.payload}")
        else:
            raise AssertionError("logged-out buyer must block Start")
        if source.upload_payloads:
            raise AssertionError("logged-out Start must perform zero seller writes")


def _run_progressive_result() -> None:
    with TemporaryDirectory(prefix="spp-manual-progress-") as raw:
        gate = BlockingCooldown()
        block, source, _buyer = build_block(
            Path(raw),
            run_async=True,
            sleep=gate,
            cooldown_seconds=1,
        )
        started = block.start(_payload([810, 800]), actor="smoke")
        job_id = str(started["job"]["job_id"])
        if not gate.reached.wait(timeout=5):
            raise AssertionError("runner did not reach the between-price checkpoint")
        progressive = block.status({"job_id": job_id})["job"]
        if progressive["status"] != "cooldown" or len(progressive["measurements"]) != 1:
            raise AssertionError(f"first result must be visible before the second price: {progressive}")
        gate.release.set()
        deadline = time.monotonic() + 5
        final: Mapping[str, Any] = {}
        while time.monotonic() < deadline:
            final = block.status({"job_id": job_id})["job"] or {}
            if final.get("status") == "complete":
                break
            time.sleep(0.01)
        if final.get("status") != "complete" or len(final.get("measurements") or []) != 2:
            raise AssertionError(f"async manual check did not finish: {final}")
        if abs(float(final["measurements"][0]["spp"]) - 0.1) > 0.0001:
            raise AssertionError(f"SPP must use actual seller readback and authenticated buyer price: {final}")
        assert_restored(final, source)


def _run_mid_run_loss_and_restore() -> None:
    with TemporaryDirectory(prefix="spp-manual-loss-") as raw:
        source = FakePricesSource()
        buyer = FakeBuyerSource(source, fail_capability_calls={2})
        block, _, _ = build_block(Path(raw), source, buyer)
        job = block.start(_payload([810, 800, 790]), actor="smoke")["job"]
        statuses = [row["status"] for row in job["measurements"]]
        if statuses != ["ok", "buyer_session_invalid"]:
            raise AssertionError(f"mid-run capability loss must stop remaining prices: {job}")
        measurement_writes = [row for row in source.upload_payloads if row[0].get("discount") is None]
        if len(measurement_writes) != 1:
            raise AssertionError("session loss before price two must allow only the first measurement write")
        if job["status"] != "failed" or job["result_status"] != "inconclusive":
            raise AssertionError(f"restored mid-run failure must stay a failed result: {job}")
        assert_restored(job, source)


def _run_manual_restore_required_only_when_unproved() -> None:
    with TemporaryDirectory(prefix="spp-manual-restore-") as raw:
        source = FakePricesSource(refuse_restore=True)
        block, _, _ = build_block(Path(raw), source)
        job = block.start(_payload([810]), actor="smoke")["job"]
        proof = dict(job.get("restore") or {})
        if (
            job["status"] != "manual_restore_required"
            or job["manual_restore_required"] is not True
            or proof.get("proof_status") != "not_confirmed"
            or proof.get("restored") is not False
        ):
            raise AssertionError(f"manual restore may be requested only with a failed fresh proof: {job}")


def _run_history_and_log_contract() -> None:
    with TemporaryDirectory(prefix="spp-manual-history-") as raw:
        block, source, _buyer = build_block(Path(raw))
        first = block.start(_payload([810]), actor="smoke")["job"]
        second = block.start(_payload([805, 800, 795, 790, 785, 780]), actor="smoke")["job"]
        history = block.history({"limit": 10})
        if [item["job_id"] for item in history["items"][:2]] != [second["job_id"], first["job_id"]]:
            raise AssertionError(f"history must be newest-first: {history}")
        serialized_history = json.dumps(history, ensure_ascii=False)
        for forbidden in ("evidence", "timeline", "plan", "threshold", "headers", "access_token"):
            if forbidden in serialized_history:
                raise AssertionError(f"compact history leaked {forbidden}")
        status = block.status({"job_id": second["job_id"]})
        events = status["log_events"]
        if len(events) != 10:
            raise AssertionError(f"technical log must contain exactly the latest ten useful events: {events}")
        if any(set(event) != {"time", "stage", "message"} for event in events):
            raise AssertionError(f"technical log shape must remain compact: {events}")
        serialized_log = json.dumps(events, ensure_ascii=False)
        if "must-not-leak" in serialized_log or "Authorization" in serialized_log or "access_token" in serialized_log:
            raise AssertionError("technical log leaked sensitive upstream data")
        assert_restored(second, source)


def _run_baseline_safety_guards() -> None:
    for source, expected in (
        (FakePricesSource(editable_size_price=True), "editableSizePrice=true"),
        (FakePricesSource(quarantine=True), "price_quarantine_present"),
    ):
        with TemporaryDirectory(prefix="spp-manual-safety-") as raw:
            block, _, _ = build_block(Path(raw), source)
            try:
                block.start(_payload([810]), actor="smoke")
            except WbSppTesterError as exc:
                if expected not in set(exc.payload.get("blockers") or []):
                    raise AssertionError(f"missing baseline safety blocker {expected}: {exc.payload}") from exc
            else:
                raise AssertionError(f"unsafe baseline must block Start: {expected}")
            if source.upload_payloads:
                raise AssertionError("unsafe baseline must perform zero seller writes")


def _run_quarantine_sequence_guards() -> None:
    if not evaluate_wb_price_quarantine_transition(900, 600).risky:
        raise AssertionError("exact 1.5x boundary must be quarantined inclusively")
    if evaluate_wb_price_quarantine_transition(900, 600.01).risky:
        raise AssertionError("a transition below the conservative boundary must remain available")
    for targets, expected_from, expected_to in (
        ([599.4], "baseline", "price_1"),
        ([810, 540], "price_1", "price_2"),
    ):
        with TemporaryDirectory(prefix="spp-quarantine-plan-") as raw:
            block, source, _buyer = build_block(Path(raw))
            try:
                block.start(_payload(targets), actor="smoke")
            except WbSppTesterError as exc:
                risks = exc.payload.get("risky_transitions") or []
                if (
                    exc.http_status != 422
                    or exc.payload.get("reason") != "price_quarantine_risk"
                    or not risks
                    or risks[0].get("from") != expected_from
                    or risks[0].get("to") != expected_to
                    or (risks[0].get("quarantine_transition") or {}).get("risky") is not True
                ):
                    raise AssertionError(f"wrong controlled quarantine error: {exc.payload}") from exc
            else:
                raise AssertionError(f"risky sequence must be rejected before a job starts: {targets}")
            if source.upload_payloads:
                raise AssertionError("controlled 422 quarantine rejection must perform zero upload_task calls")

    with TemporaryDirectory(prefix="spp-quarantine-rounded-safe-") as raw:
        block, source, _buyer = build_block(Path(raw))
        job = block.start(_payload([600]), actor="smoke")["job"]
        if job["status"] != "complete" or job["measurements"][0]["seller_discounted_price"] != 600.3:
            raise AssertionError(f"integer seller-price rounding must keep the safe target available: {job}")
        assert_restored(job, source)


def _run_fresh_prewrite_drift_guard() -> None:
    with TemporaryDirectory(prefix="spp-prewrite-drift-") as raw:
        source = FakePricesSource(drift_on_goods_read=2)
        block, _, _buyer = build_block(Path(raw), source)
        job = block.start(_payload([810]), actor="smoke")["job"]
        if [row["status"] for row in job["measurements"]] != ["seller_state_drift"]:
            raise AssertionError(f"fresh seller tuple drift must block the measurement write: {job}")
        measurement_uploads = [
            upload for upload in source.upload_payloads
            if upload and upload[0].get("discount") is None
        ]
        if measurement_uploads:
            raise AssertionError("fresh per-write drift guard must fail before a measurement upload_task")
        if job["status"] != "failed" or job["result_status"] != "inconclusive":
            raise AssertionError(f"drift-blocked job must fail closed after restore: {job}")
        assert_restored(job, source)


def _run_restore_bridge_threshold_guard() -> None:
    with TemporaryDirectory(prefix="spp-restore-bridge-threshold-") as raw:
        block, source, _buyer = build_block(Path(raw))
        public_job = block.start(_payload([1800]), actor="smoke")["job"]
        stored_job = block._load_job(str(public_job["job_id"])) or {}  # noqa: SLF001
        steps = (stored_job.get("restore") or {}).get("steps") or []
        bridge_steps = [step for step in steps if step.get("kind") == "bridge"]
        if not bridge_steps:
            raise AssertionError(f"large downward restore must use bounded bridge steps: {steps}")
        if any((step.get("quarantine_transition") or {}).get("risky") for step in steps):
            raise AssertionError(f"every restore bridge must stay below the 1.5x threshold: {steps}")
        if any((step.get("prewrite_guard") or {}).get("quarantine_transition", {}).get("risky") for step in steps):
            raise AssertionError(f"fresh restore prewrite guards must remain conservative: {steps}")
        assert_restored(public_job, source)


def _run_removed_contract_guard() -> None:
    adapter = (ROOT / "packages" / "adapters" / "registry_upload_http_entrypoint.py").read_text(encoding="utf-8")
    application = (ROOT / "packages" / "application" / "registry_upload_http_entrypoint.py").read_text(encoding="utf-8")
    combined = adapter + application
    for removed in (
        "SPP_TEST_BASELINE_PATH",
        "SPP_TEST_PLAN_PATH",
        "SPP_TEST_SCHEDULE_PATH",
        "handle_sheet_prices_spp_test_plan_request",
        "handle_sheet_prices_spp_test_schedule",
    ):
        if removed in combined:
            raise AssertionError(f"removed SPP contract is still wired: {removed}")


def main() -> None:
    _run_validation_and_exact_order()
    _run_logged_out_zero_writes()
    _run_progressive_result()
    _run_mid_run_loss_and_restore()
    _run_manual_restore_required_only_when_unproved()
    _run_history_and_log_contract()
    _run_baseline_safety_guards()
    _run_quarantine_sequence_guards()
    _run_fresh_prewrite_drift_guard()
    _run_restore_bridge_threshold_guard()
    _run_removed_contract_guard()
    print("wb_spp_tester_smoke: OK")


if __name__ == "__main__":
    main()
