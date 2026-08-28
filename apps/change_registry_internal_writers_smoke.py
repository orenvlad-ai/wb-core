"""Deterministic acceptance smoke for all incumbent internal writer surfaces."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.change_registry_baseline_engine_smoke as baseline_fixture  # noqa: E402
from apps.change_registry_baseline_engine_smoke import _acquisition, _price  # noqa: E402
from packages.application.change_registry import (  # noqa: E402
    ATTEMPT_EVENTS_TABLE,
    FACT_LINKS_TABLE,
    FACTS_TABLE,
    ITEMS_TABLE,
    OPERATIONS_TABLE,
    ChangeRegistryRepository,
)
from packages.application.change_registry_baseline_engine import (  # noqa: E402
    ChangeRegistryBaselineEngine,
)
from packages.application.change_registry_writer import (  # noqa: E402
    InternalWriterRegistry,
    InternalWriterRegistryError,
    price_tuple_from_wb,
)
from packages.application.sku_inventory_balance import (  # noqa: E402
    DryRunInventoryBalanceApplyAdapter,
)
from packages.application.wb_prices_management import (  # noqa: E402
    WbPricesManagementBlock,
    WbPricesSafetyConfig,
)
from packages.application.wb_spp_tester import (  # noqa: E402
    WbSppTesterBlock,
    WbSppTesterCadenceConfig,
    WbSppTesterSafetyConfig,
)
from packages.application.sheet_vitrina_v1_ads import (  # noqa: E402
    AdsSafetyConfig,
    SheetVitrinaV1AdsBlock,
)
from apps.sheet_vitrina_v1_ads_smoke import FakePromotionSource  # noqa: E402
from packages.adapters.wb_prices_management import WbPricesApiError  # noqa: E402


SELLER = "seller-canonical"
ACCOUNT = "seller-portal-primary"


class Clock:
    def __init__(self, value: str) -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


def main() -> None:
    with TemporaryDirectory(prefix="change-registry-writers-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        repository = ChangeRegistryRepository(runtime_dir)
        repository.initialize_schema()
        db_path = runtime_dir / "registry_upload_runtime.sqlite3"
        clock = Clock("2026-08-29T10:00:01Z")
        writer = InternalWriterRegistry(
            runtime_dir=runtime_dir,
            seller_id=SELLER,
            account_scope=ACCOUNT,
            timestamp_factory=clock,
        )

        # Preview/read-only and Balance dry-run are outside the writer seam.
        assert _counts(db_path) == (0, 0, 0, 0, 0)
        dry_run = DryRunInventoryBalanceApplyAdapter().apply(
            {"target_key": "bid:1:2:search", "final_target_bid_rub": 0},
            actor="operator",
        )
        assert dry_run["wb_patch_called"] is False
        assert _counts(db_path) == (0, 0, 0, 0, 0)

        price_before = price_tuple_from_wb(
            price=100, discount=10, seller_price=90
        )
        price_after = price_tuple_from_wb(
            price=120, discount=10, seller_price=108
        )

        prices = writer.prepare_price(
            source_surface="prices_upload",
            actor="operator-a",
            native_operation_id="prices-preview-1",
            nm_id=101,
            before=price_before,
            requested=price_after,
            explicit_fields=("original_price_minor",),
            requested_at="2026-08-29T10:00:00Z",
            correlation_id="prices-preview-1",
            native_audit_reference="sheet_vitrina_v1_prices/upload_audit.jsonl#operation=prices-preview-1",
        )
        writer.submitted(
            prices,
            receipt_reference="wb-prices-upload:1",
            receipt_basis={"upload_id": 1},
        )
        writer.confirm_price(
            prices,
            confirmed=price_after,
            readback_basis={"upload_id": 1, "tuple": price_after},
            receipt_reference="wb-prices-upload:1",
        )

        sku_price = writer.prepare_price(
            source_surface="sku_management_price",
            actor="operator-b",
            native_operation_id="sku-price-preview-1",
            nm_id=102,
            before=price_before,
            requested=price_after,
            explicit_fields=("original_price_minor",),
            requested_at="2026-08-29T10:00:00Z",
        )
        writer.submitted(
            sku_price,
            receipt_reference="wb-prices-upload:2",
            receipt_basis={"upload_id": 2},
        )
        writer.ambiguous(
            sku_price,
            error_code="wb_readback_mismatch",
            error_message="not yet visible",
            receipt_reference="wb-prices-upload:2",
        )
        clock.value = "2026-08-29T10:00:02Z"
        writer.confirm_price(
            sku_price,
            confirmed=price_after,
            readback_basis={"upload_id": 2, "tuple": price_after},
            receipt_reference="wb-prices-upload:2",
        )

        for stage, before, after in (
            ("measurement:point-1", price_before, price_after),
            ("restore:final:1", price_after, price_before),
        ):
            spp = writer.prepare_price(
                source_surface="spp_tester",
                actor="operator-c",
                native_operation_id=f"job-1:{stage}",
                nm_id=103,
                before=before,
                requested=after,
                explicit_fields=("original_price_minor", "discount_bps"),
                requested_at="2026-08-29T10:00:00Z",
                correlation_id="job-1",
                apply_operation_id="job-1",
                stage=stage,
                native_audit_reference=f"sheet_vitrina_v1_prices/spp_tests/audit.jsonl#job=job-1&stage={stage}",
            )
            receipt = f"wb-spp:job-1:{stage}"
            writer.submitted(spp, receipt_reference=receipt, receipt_basis={"stage": stage})
            writer.confirm_price(
                spp,
                confirmed=after,
                readback_basis={"stage": stage, "tuple": after},
                receipt_reference=receipt,
            )

        ads = writer.prepare_bid(
            source_surface="ads_bid_change",
            actor="operator-d",
            native_operation_id="ads-preview-1",
            nm_id=104,
            advert_id=4001,
            placement="search",
            before_bid_minor=500,
            requested_bid_minor=0,
            requested_at="2026-08-29T10:00:00Z",
        )
        writer.submitted(
            ads,
            receipt_reference="wb-ads-bid:ads-preview-1",
            receipt_basis={"bid_minor": 0},
        )
        writer.confirm_bid(
            ads,
            confirmed_bid_minor=0,
            readback_basis={"bid_minor": 0},
            receipt_reference="wb-ads-bid:ads-preview-1",
        )

        sku_bid = writer.prepare_bid(
            source_surface="sku_management_bid",
            actor="operator-e",
            native_operation_id="sku-bid-preview-1",
            nm_id=105,
            advert_id=5001,
            placement="recommendations",
            before_bid_minor=700,
            requested_bid_minor=800,
            requested_at="2026-08-29T10:00:00Z",
        )
        writer.fail_before_submit(
            sku_bid,
            rejected=True,
            error_code="wb_http_400",
            error_message="WB rejected exact request",
        )

        _assert_surface_lifecycles(db_path)
        _assert_registry_failure_prevents_submit(runtime_dir, clock)
        _assert_concrete_writer_blocks(runtime_dir, writer, clock)
        _assert_checkpoint_writer_race_both_orderings(Path(tmp))

    print("change_registry_internal_writers_smoke: OK")


def _assert_surface_lifecycles(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        surfaces = {
            row[0]
            for row in conn.execute(
                f"SELECT source_surface FROM {OPERATIONS_TABLE}"
            ).fetchall()
        }
        assert surfaces == {
            "prices_upload",
            "sku_management_price",
            "spp_tester",
            "ads_bid_change",
            "sku_management_bid",
        }
        assert conn.execute(
            f"SELECT COUNT(*) FROM {OPERATIONS_TABLE} WHERE source_surface='spp_tester'"
        ).fetchone()[0] == 2
        assert conn.execute(
            f"SELECT COUNT(*) FROM {ITEMS_TABLE} WHERE target_kind='price'"
        ).fetchone()[0] == 12
        assert conn.execute(
            f"SELECT COUNT(*) FROM {ITEMS_TABLE} WHERE target_kind='bid'"
        ).fetchone()[0] == 2
        assert conn.execute(
            f"SELECT COUNT(*) FROM {FACTS_TABLE} WHERE parameter_field='bid_minor' AND after_value_integer=0"
        ).fetchone()[0] == 1
        states = {
            row[0]
            for row in conn.execute(
                f"SELECT state FROM {ATTEMPT_EVENTS_TABLE}"
            ).fetchall()
        }
        assert {"created", "submitted", "confirmed", "ambiguous", "resolved", "rejected"} <= states
        assert conn.execute(
            f"SELECT COUNT(*) FROM {FACTS_TABLE} fact JOIN {FACT_LINKS_TABLE} link ON link.fact_id=fact.fact_id WHERE link.link_kind='change_item'"
        ).fetchone()[0] == conn.execute(
            f"SELECT COUNT(*) FROM {FACTS_TABLE}"
        ).fetchone()[0]


def _assert_registry_failure_prevents_submit(runtime_dir: Path, clock: Clock) -> None:
    class BrokenRepository:
        def prepare_writer_operation(self, **_kwargs):
            raise RuntimeError("injected registry storage failure")

    broken = InternalWriterRegistry(
        runtime_dir=runtime_dir,
        seller_id=SELLER,
        account_scope=ACCOUNT,
        timestamp_factory=clock,
        repository=BrokenRepository(),  # type: ignore[arg-type]
    )
    wb_calls = 0
    try:
        broken.prepare_bid(
            source_surface="ads_bid_change",
            actor="operator",
            native_operation_id="fail-closed",
            nm_id=1,
            advert_id=2,
            placement="search",
            before_bid_minor=1,
            requested_bid_minor=2,
            requested_at="2026-08-29T10:00:00Z",
        )
        wb_calls += 1
    except InternalWriterRegistryError:
        pass
    else:
        raise AssertionError("registry preparation failure was not surfaced")
    assert wb_calls == 0

    db_path = runtime_dir / "registry_upload_runtime.sqlite3"
    counts_before = _counts(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"""CREATE TRIGGER writer_prepare_abort
                BEFORE INSERT ON {ITEMS_TABLE}
                BEGIN SELECT RAISE(ABORT,'injected writer prepare failure'); END"""
        )
        conn.commit()
    healthy = InternalWriterRegistry(
        runtime_dir=runtime_dir,
        seller_id=SELLER,
        account_scope=ACCOUNT,
        timestamp_factory=clock,
    )
    try:
        healthy.prepare_bid(
            source_surface="ads_bid_change",
            actor="operator",
            native_operation_id="atomic-fail-closed",
            nm_id=9,
            advert_id=10,
            placement="search",
            before_bid_minor=1,
            requested_bid_minor=2,
            requested_at="2026-08-29T10:00:00Z",
        )
    except InternalWriterRegistryError:
        pass
    else:
        raise AssertionError("atomic registry failure was not surfaced")
    finally:
        with sqlite3.connect(db_path) as conn:
            conn.execute("DROP TRIGGER writer_prepare_abort")
            conn.commit()
    assert _counts(db_path) == counts_before


def _assert_concrete_writer_blocks(
    runtime_dir: Path,
    writer: InternalWriterRegistry,
    clock: Clock,
) -> None:
    class Runtime:
        def list_nomenclature_items(self, **_kwargs):
            return []

    class PricesSource:
        def __init__(self) -> None:
            self.upload_calls = 0
            self.price = 100
            self.discount = 10

        def goods(self):
            discounted = self.price * (100 - self.discount) / 100
            return {
                "data": {
                    "listGoods": [
                        {
                            "nmID": 301,
                            "vendorCode": "registry-smoke",
                            "currencyIsoCode4217": "RUB",
                            "discount": self.discount,
                            "sizes": [
                                {
                                    "sizeID": 1,
                                    "techSizeName": "0",
                                    "price": self.price,
                                    "discountedPrice": discounted,
                                    "clubDiscountedPrice": discounted,
                                }
                            ],
                        }
                    ]
                },
                "error": False,
            }

        def fetch_goods_by_nm_ids(self, _nm_ids):
            return self.goods()

        def upload_task(self, goods):
            self.upload_calls += 1
            self.price = int(goods[0]["price"])
            if "discount" in goods[0]:
                self.discount = int(goods[0]["discount"])
            return {"data": {"id": 301, "alreadyExists": False}}

        def fetch_upload_status(self, _upload_id):
            return {"data": {"status": 3}}

    clock.value = "2026-08-29T13:00:01Z"
    prices_source = PricesSource()
    prices = WbPricesManagementBlock(
        runtime=Runtime(),
        runtime_dir=runtime_dir,
        source=prices_source,
        timestamp_factory=clock,
        safety_config=WbPricesSafetyConfig(write_enabled=True, preview_ttl_seconds=60),
        writer_registry=writer,
        registry_source_surface="prices_upload",
    )
    preview = prices.preview_changes(
        {"changes": [{"nmID": 301, "price": 120}]},
        current_payload=prices_source.goods(),
    )
    committed = prices.upload_task(preview["confirmation_payload"], actor="operator")
    assert prices_source.upload_calls == 1
    assert committed["registry_operation_id"]
    try:
        prices.upload_task(preview["confirmation_payload"], actor="operator")
    except Exception:
        pass
    else:
        raise AssertionError("same native price operation was submitted twice")
    assert prices_source.upload_calls == 1
    clock.value = "2026-08-29T13:00:02Z"
    assert prices.get_upload_task(301)["registry_readback_status"] == "confirmed"

    spp_source = PricesSource()
    spp = WbSppTesterBlock(
        runtime=Runtime(),
        runtime_dir=runtime_dir,
        prices_source=spp_source,
        buyer_source=object(),  # no buyer call in this direct writer seam check
        timestamp_factory=clock,
        safety_config=WbSppTesterSafetyConfig(
            spp_test_enabled=True, prices_write_enabled=True
        ),
        cadence_config=WbSppTesterCadenceConfig(run_async=False),
        writer_registry=writer,
    )
    job = {
        "job_id": "concrete-spp-job",
        "actor": "operator",
        "nmID": 301,
        "created_at": "2026-08-29T13:00:00Z",
    }
    upload = spp._upload_price_with_backoff(
        job,
        [{"nmID": 301, "price": 120, "discount": 10}],
        stage="measurement:1",
        before={"price": 100, "discount": 10, "discountedPrice": 90},
        requested={"price": 120, "discount": 10, "discountedPrice": 108},
    )
    assert spp_source.upload_calls == 1
    spp._confirm_registry_upload(
        job,
        upload,
        {"price": 120, "discount": 10, "discountedPrice": 108},
    )

    class RateLimitedPricesSource(PricesSource):
        def upload_task(self, _goods):
            self.upload_calls += 1
            raise WbPricesApiError(
                method="POST",
                url="https://discounts-prices-api.wildberries.ru/api/v2/upload/task",
                http_status=429,
            )

    rate_limited_source = RateLimitedPricesSource()
    spp_rate_limited = WbSppTesterBlock(
        runtime=Runtime(),
        runtime_dir=runtime_dir,
        prices_source=rate_limited_source,
        buyer_source=object(),
        timestamp_factory=clock,
        safety_config=WbSppTesterSafetyConfig(
            spp_test_enabled=True, prices_write_enabled=True
        ),
        cadence_config=WbSppTesterCadenceConfig(run_async=False),
        writer_registry=writer,
    )
    stopped = spp_rate_limited._upload_price_with_backoff(
        {**job, "job_id": "concrete-spp-429"},
        [{"nmID": 301, "price": 130}],
        stage="measurement:1",
        before={"price": 120, "discount": 10, "discountedPrice": 108},
        requested={"price": 130, "discount": 10, "discountedPrice": 117},
    )
    assert stopped["status"] == "rate_limited_stop"
    assert rate_limited_source.upload_calls == 1

    promotion_source = FakePromotionSource()
    ads = SheetVitrinaV1AdsBlock(
        runtime=Runtime(),
        runtime_dir=runtime_dir,
        source=promotion_source,
        now_factory=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
        timestamp_factory=clock,
        safety_config=AdsSafetyConfig(
            write_enabled=True,
            absolute_max_bid_kopecks=100_000,
            max_percent_increase=Decimal("100"),
            max_absolute_increase_kopecks=100_000,
            preview_ttl_seconds=60,
        ),
        writer_registry=writer,
        registry_source_surface="ads_bid_change",
    )
    bid_preview = ads.preview_bid_change(
        {
            "nm_id": 210183919,
            "advert_id": 1001,
            "placement": "search",
            "requested_bid_rub": 14,
        }
    )
    bid_commit = ads.commit_bid_change(
        {"preview_id": bid_preview["preview"]["preview_id"]},
        actor="operator",
    )
    assert len(promotion_source.patch_payloads) == 1
    assert bid_commit["registry_readback_status"] == "ambiguous"
    assert ads.reconcile_registry_bid(
        receipt_reference=bid_commit["registry_receipt_reference"],
        exact_readback={"current_bid_kopecks": 1400},
    ) == "confirmed"


def _assert_checkpoint_writer_race_both_orderings(root: Path) -> None:
    baseline_fixture.ACCOUNT = ACCOUNT
    for ordering in ("writer_first", "checkpoint_first"):
        runtime_dir = root / ordering
        repository = ChangeRegistryRepository(runtime_dir)
        repository.initialize_schema()
        engine = ChangeRegistryBaselineEngine(
            runtime_dir=runtime_dir,
            seller_id=SELLER,
            account_scope=ACCOUNT,
        )
        baseline = _acquisition(
            started_at="2026-08-29T10:00:00Z",
            completed_at="2026-08-29T10:01:00Z",
            status="complete",
            prices=[_price(201, original=10_000, discount=1_000, seller=9_000)],
            campaigns=[],
        )
        changed = _acquisition(
            started_at="2026-08-29T11:00:00Z",
            completed_at="2026-08-29T11:01:00Z",
            status="complete",
            prices=[_price(201, original=12_000, discount=1_000, seller=10_800)],
            campaigns=[],
        )
        engine.ingest(baseline)
        clock = Clock("2026-08-29T10:30:01Z")
        writer = InternalWriterRegistry(
            runtime_dir=runtime_dir,
            seller_id=SELLER,
            account_scope=ACCOUNT,
            timestamp_factory=clock,
        )

        prepared = writer.prepare_price(
            source_surface="prices_upload",
            actor="operator",
            native_operation_id=f"race-{ordering}",
            nm_id=201,
            before={
                "original_price_minor": 10_000,
                "discount_bps": 1_000,
                "seller_price_minor": 9_000,
            },
            requested={
                "original_price_minor": 12_000,
                "discount_bps": 1_000,
                "seller_price_minor": 10_800,
            },
            explicit_fields=("original_price_minor",),
            requested_at="2026-08-29T10:30:00Z",
        )
        writer.submitted(
            prepared,
            receipt_reference=f"race:{ordering}",
            receipt_basis={"ordering": ordering},
        )

        def confirm_writer() -> None:
            writer.confirm_price(
                prepared,
                confirmed={
                    "original_price_minor": 12_000,
                    "discount_bps": 1_000,
                    "seller_price_minor": 10_800,
                },
                readback_basis={"ordering": ordering},
                receipt_reference=f"race:{ordering}",
            )

        if ordering == "writer_first":
            clock.value = "2026-08-29T10:31:00Z"
            confirm_writer()
            receipt = engine.ingest(changed)
        else:
            receipt = engine.ingest(changed)
            clock.value = "2026-08-29T11:02:00Z"
            confirm_writer()
        assert len(receipt["fact_ids"]) == 2
        db_path = runtime_dir / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(f"SELECT COUNT(*) FROM {FACTS_TABLE}").fetchone()[0] == 2
            assert conn.execute(
                f"SELECT COUNT(*) FROM {FACT_LINKS_TABLE} WHERE link_kind='checkpoint'"
            ).fetchone()[0] == 2
            assert conn.execute(
                f"SELECT COUNT(*) FROM {FACT_LINKS_TABLE} WHERE link_kind='change_item'"
            ).fetchone()[0] == 2
        # Same values at a later, unrelated interval are not globally deduped.
        clock.value = "2026-08-29T12:01:00Z"
        unrelated = writer.prepare_price(
            source_surface="prices_upload",
            actor="operator",
            native_operation_id=f"unrelated-{ordering}",
            nm_id=201,
            before={
                "original_price_minor": 10_000,
                "discount_bps": 1_000,
                "seller_price_minor": 9_000,
            },
            requested={
                "original_price_minor": 12_000,
                "discount_bps": 1_000,
                "seller_price_minor": 10_800,
            },
            explicit_fields=("original_price_minor",),
            requested_at="2026-08-29T12:00:00Z",
        )
        writer.submitted(
            unrelated,
            receipt_reference=f"unrelated:{ordering}",
            receipt_basis={"ordering": ordering},
        )
        writer.confirm_price(
            unrelated,
            confirmed={
                "original_price_minor": 12_000,
                "discount_bps": 1_000,
                "seller_price_minor": 10_800,
            },
            readback_basis={"unrelated": ordering},
            receipt_reference=f"unrelated:{ordering}",
        )
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(f"SELECT COUNT(*) FROM {FACTS_TABLE}").fetchone()[0] == 4


def _counts(db_path: Path) -> tuple[int, int, int, int, int]:
    with sqlite3.connect(db_path) as conn:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                OPERATIONS_TABLE,
                ITEMS_TABLE,
                ATTEMPT_EVENTS_TABLE,
                FACTS_TABLE,
                FACT_LINKS_TABLE,
            )
        )  # type: ignore[return-value]


if __name__ == "__main__":
    main()
