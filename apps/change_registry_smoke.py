"""Deterministic safety smoke for the inert seller change registry."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.change_registry import (  # noqa: E402
    ANNOTATION_REVISIONS_TABLE,
    ATTEMPT_EVENTS_TABLE,
    CHECKPOINTS_TABLE,
    FACT_LINKS_TABLE,
    FACTS_TABLE,
    IDENTITY_INCIDENTS_TABLE,
    IMMUTABLE_TABLES,
    ITEMS_TABLE,
    MANUAL_PENDING_CURRENT_TABLE,
    MANUAL_PENDING_EVENTS_TABLE,
    MISSING,
    OBSERVATION_VALUES_TABLE,
    OBSERVER_LEASES_TABLE,
    CHECKPOINT_SOURCE_MANIFESTS_TABLE,
    OBSERVER_HEALTH_EVENTS_TABLE,
    OBSERVER_JOB_EVENTS_TABLE,
    OBSERVER_JOBS_TABLE,
    OPERATIONS_TABLE,
    ChangeRegistryConflict,
    ChangeRegistryError,
    ChangeRegistryRepository,
    canonical_digest,
    ensure_change_registry_schema,
    target_identity,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.storage_registry import StoreRegistry  # noqa: E402


NOW = "2026-08-26T10:00:00Z"
LATER = "2026-08-26T10:01:00Z"
LATEST = "2026-08-26T10:02:00Z"
SELLER = "seller-canonical"
ACCOUNT = "wb-seller-account"


def main() -> None:
    with TemporaryDirectory(prefix="change-registry-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        # Existing runtime schema initialization is the only automatic wiring.
        # It creates empty foundation tables and performs no action capture.
        assert runtime.list_ff_stock_operations(limit=1) == []
        repository = ChangeRegistryRepository(runtime_dir)
        repository.initialize_schema()
        _assert_schema_contract(runtime.db_path)
        _assert_repository_contract(repository, runtime.db_path)
        _assert_store_registry_and_backup(runtime_dir, runtime.db_path, Path(tmp))

    print("change_registry_smoke: OK")


def _assert_schema_contract(db_path: Path) -> None:
    expected = set(IMMUTABLE_TABLES) | {
        MANUAL_PENDING_CURRENT_TABLE,
        OBSERVER_LEASES_TABLE,
    }
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        actual = {
            str(row[0])
            for row in conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name LIKE 'change_registry_%'"""
            ).fetchall()
        }
        assert actual == expected
        assert all(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            for table in expected
        )
        schema_before = conn.execute(
            """SELECT type,name,tbl_name,sql FROM sqlite_master
               WHERE name LIKE 'change_registry_%'
               ORDER BY type,name"""
        ).fetchall()
        conn.commit()
        bytes_before = conn.serialize()
        schema_version_before = conn.execute("PRAGMA schema_version").fetchone()[0]
        ensure_change_registry_schema(conn)
        conn.commit()
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == schema_version_before
        assert conn.execute(
            """SELECT type,name,tbl_name,sql FROM sqlite_master
               WHERE name LIKE 'change_registry_%'
               ORDER BY type,name"""
        ).fetchall() == schema_before
        assert conn.serialize() == bytes_before

        for table in sorted(expected):
            columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
            assert columns
            assert all(str(column[2]).upper() != "REAL" for column in columns)
            lowered = {str(column[1]).casefold() for column in columns}
            assert not any(
                marker in column
                for column in lowered
                for marker in ("token", "secret", "cookie", "password", "raw_payload")
            )


def _assert_repository_contract(
    repository: ChangeRegistryRepository, db_path: Path
) -> None:
    operation = _append_operation(repository, "operation-a", NOW, "native-a")
    assert operation["seller_id"] == SELLER
    assert repository.create_operation(
        operation_id="operation-a",
        seller_id=SELLER,
        account_scope=ACCOUNT,
        source_surface="sku_management",
        actor_principal="operator-a",
        actor_kind="human",
        requested_at=NOW,
        created_at=NOW,
        native_idempotency_key="native-a",
        correlation_id="corr-operation-a",
        provenance_digest=_digest("operation-a"),
    ) == operation
    _rejected(
        ChangeRegistryConflict,
        lambda: _append_operation(repository, "operation-other", NOW, "native-a"),
    )
    _rejected(
        ChangeRegistryError,
        lambda: _append_operation(repository, "operation-empty-seller", NOW, "native-b", seller_id=""),
    )

    price_target = target_identity("price", nm_id=1001)
    item = repository.append_change_item(
        change_item_id="item-price-a",
        operation_id="operation-a",
        target=price_target,
        parameter_field="original_price_minor",
        before_value=12_300,
        requested_value=12_500,
        created_at=NOW,
        recommendation_item_id="recommendation-1",
    )
    assert item["requested_value_integer"] == 12_500
    assert repository.append_change_item(
        change_item_id="item-price-a",
        operation_id="operation-a",
        target=price_target,
        parameter_field="original_price_minor",
        before_value=12_300,
        requested_value=12_500,
        created_at=NOW,
        recommendation_item_id="recommendation-1",
    ) == item
    _rejected(
        ChangeRegistryError,
        lambda: target_identity("price", nm_id=1001, advert_id=9),
    )
    _rejected(
        ChangeRegistryError,
        lambda: target_identity("bid", nm_id=1001, advert_id=9),
    )
    _rejected(
        ChangeRegistryError,
        lambda: repository.append_change_item(
            change_item_id="item-float",
            operation_id="operation-a",
            target=price_target,
            parameter_field="original_price_minor",
            before_value=12_300,
            requested_value=125.5,
            created_at=NOW,
        ),
    )
    _rejected(
        ChangeRegistryError,
        lambda: repository.append_change_item(
            change_item_id="item-discount",
            operation_id="operation-a",
            target=price_target,
            parameter_field="discount_bps",
            before_value=100,
            requested_value=10_001,
            created_at=NOW,
        ),
    )

    created = repository.append_attempt_event(
        attempt_event_id="attempt-event-1",
        attempt_id="attempt-1",
        change_item_id="item-price-a",
        sequence_no=1,
        state="created",
        occurred_at=NOW,
        native_event_key="attempt-native-1",
    )
    assert repository.append_attempt_event(
        attempt_event_id="attempt-event-1",
        attempt_id="attempt-1",
        change_item_id="item-price-a",
        sequence_no=1,
        state="created",
        occurred_at=NOW,
        native_event_key="attempt-native-1",
    ) == created
    repository.append_attempt_event(
        attempt_event_id="attempt-event-2",
        attempt_id="attempt-1",
        change_item_id="item-price-a",
        sequence_no=2,
        state="submitted",
        occurred_at=LATER,
        receipt_reference="wb-task-sanitized-1",
        receipt_digest=_digest("receipt-1"),
    )
    repository.append_attempt_event(
        attempt_event_id="attempt-event-3",
        attempt_id="attempt-1",
        change_item_id="item-price-a",
        sequence_no=3,
        state="ambiguous",
        occurred_at=LATEST,
        error_code="transport_ambiguous",
        error_message="connection closed before a conclusive response",
    )
    repository.append_attempt_event(
        attempt_event_id="attempt-event-4",
        attempt_id="attempt-1",
        change_item_id="item-price-a",
        sequence_no=4,
        state="resolved",
        resolution_state="confirmed",
        occurred_at="2026-08-26T10:03:00Z",
        readback_proof_kind="exact_tuple",
        readback_digest=_digest("readback-1"),
    )
    _rejected(
        ChangeRegistryError,
        lambda: repository.append_attempt_event(
            attempt_event_id="attempt-event-bad",
            attempt_id="attempt-bad",
            change_item_id="item-price-a",
            sequence_no=1,
            state="submitted",
            occurred_at=NOW,
        ),
    )
    _rejected(
        ChangeRegistryError,
        lambda: repository.append_attempt_event(
            attempt_event_id="attempt-event-secret",
            attempt_id="attempt-secret",
            change_item_id="item-price-a",
            sequence_no=1,
            state="created",
            occurred_at=NOW,
            error_message="Authorization: Bearer should-not-persist",
        ),
    )

    checkpoint = repository.append_checkpoint(
        checkpoint_id="checkpoint-1",
        seller_id=SELLER,
        account_scope=ACCOUNT,
        source_surface="wb_prices_readback",
        scan_kind="observer",
        started_at=NOW,
        completed_at=LATER,
        completeness_status="complete",
        expected_target_count=3,
        observed_target_count=3,
        completeness_digest=_digest("checkpoint-completeness"),
        evidence_digest=_digest("checkpoint-evidence"),
    )
    assert checkpoint["completeness_status"] == "complete"
    missing = repository.append_observation_value(
        observation_value_id="observation-missing",
        checkpoint_id="checkpoint-1",
        target=price_target,
        parameter_field="seller_price_minor",
        observation_status="missing",
        value=MISSING,
        observed_at=LATER,
        evidence_digest=_digest("missing-evidence"),
        health_code="field_absent",
    )
    zero = repository.append_observation_value(
        observation_value_id="observation-zero",
        checkpoint_id="checkpoint-1",
        target=target_identity("price", nm_id=1002),
        parameter_field="seller_price_minor",
        observation_status="exact_zero",
        value=0,
        observed_at=LATER,
        evidence_digest=_digest("zero-evidence"),
    )
    explicit_null = repository.append_observation_value(
        observation_value_id="observation-null",
        checkpoint_id="checkpoint-1",
        target=target_identity("price", nm_id=1003),
        parameter_field="seller_price_minor",
        observation_status="exact",
        value=None,
        observed_at=LATER,
        evidence_digest=_digest("null-evidence"),
    )
    assert missing["value_kind"] == "missing" and missing["value_integer"] is None
    assert zero["value_kind"] == "integer" and zero["value_integer"] == 0
    assert explicit_null["value_kind"] == "null"

    # Observer proof may arrive before a writer link; the link can be appended
    # later without duplicating the fact or deduplicating by value alone.
    fact = repository.append_fact(
        fact_id="fact-1",
        seller_id=SELLER,
        account_scope=ACCOUNT,
        target=price_target,
        parameter_field="original_price_minor",
        before_value=12_300,
        after_value=12_500,
        observed_from=LATER,
        observed_to=LATEST,
        proven_at=LATEST,
        proof_kind="wb_readback",
        evidence_digest=_digest("fact-evidence-1"),
    )
    assert repository.read_fact("fact-1")["links"] == []
    second_fact = repository.append_fact(
        fact_id="fact-2",
        seller_id=SELLER,
        account_scope=ACCOUNT,
        target=price_target,
        parameter_field="original_price_minor",
        before_value=12_300,
        after_value=12_500,
        observed_from=LATER,
        observed_to=LATEST,
        proven_at="2026-08-26T10:03:30Z",
        proof_kind="native_audit",
        evidence_digest=_digest("fact-evidence-2"),
    )
    assert second_fact["after_value_integer"] == fact["after_value_integer"]
    _rejected(
        ChangeRegistryConflict,
        lambda: repository.append_fact(
            fact_id="fact-duplicate-evidence",
            seller_id=SELLER,
            account_scope=ACCOUNT,
            target=price_target,
            parameter_field="original_price_minor",
            before_value=12_300,
            after_value=12_500,
            observed_from=LATER,
            observed_to=LATEST,
            proven_at="2026-08-26T10:04:00Z",
            proof_kind="wb_readback",
            evidence_digest=_digest("fact-evidence-1"),
        ),
    )
    repository.append_fact_link(
        fact_link_id="fact-link-item",
        fact_id="fact-1",
        link_kind="change_item",
        linked_id="item-price-a",
        linked_at="2026-08-26T10:04:00Z",
        evidence_digest=_digest("link-item"),
    )
    repository.append_fact_link(
        fact_link_id="fact-link-checkpoint",
        fact_id="fact-1",
        link_kind="checkpoint",
        linked_id="checkpoint-1",
        linked_at="2026-08-26T10:04:01Z",
        evidence_digest=_digest("link-checkpoint"),
    )
    repository.append_fact_link(
        fact_link_id="fact-link-native",
        fact_id="fact-1",
        link_kind="native_audit",
        linked_id="prices-jsonl:sha256:fixture",
        linked_at="2026-08-26T10:04:02Z",
        evidence_digest=_digest("link-native"),
    )
    repository.append_fact_link(
        fact_link_id="fact-link-recommendation",
        fact_id="fact-1",
        link_kind="recommendation_item",
        linked_id="recommendation-1",
        linked_at="2026-08-26T10:04:03Z",
        evidence_digest=_digest("link-recommendation"),
    )
    assert len(repository.read_fact("fact-1")["links"]) == 4

    # Campaign cardinality zero/many is incident evidence, never an action.
    assert repository.resolve_campaign_identity(
        incident_id="incident-zero",
        seller_id=SELLER,
        account_scope=ACCOUNT,
        advert_id=9001,
        candidate_nm_ids=[],
        source_surface="wb_promotion",
        observed_at=NOW,
        evidence_digest=_digest("incident-zero"),
    ) is None
    assert repository.resolve_campaign_identity(
        incident_id="incident-many",
        seller_id=SELLER,
        account_scope=ACCOUNT,
        advert_id=9002,
        candidate_nm_ids=[1002, 1001, 1002],
        source_surface="wb_promotion",
        observed_at=LATER,
        evidence_digest=_digest("incident-many"),
    ) is None
    exact_campaign = repository.resolve_campaign_identity(
        incident_id="unused-exact-one",
        seller_id=SELLER,
        account_scope=ACCOUNT,
        advert_id=9003,
        candidate_nm_ids=[1001],
        source_surface="wb_promotion",
        observed_at=LATEST,
        evidence_digest=_digest("exact-one"),
    )
    assert exact_campaign == target_identity("campaign", nm_id=1001, advert_id=9003)
    campaign_item = repository.append_change_item(
        change_item_id="item-campaign-a",
        operation_id="operation-a",
        target=exact_campaign,
        parameter_field="campaign_state",
        before_value=MISSING,
        requested_value=" ACTIVE ",
        created_at=LATEST,
    )
    assert campaign_item["requested_value_text"] == "active"

    annotation_1 = repository.append_annotation_revision(
        annotation_revision_id="annotation-1",
        subject_kind="operation",
        subject_id="operation-a",
        actor_principal="operator-a",
        reason="initial reason",
        comment="initial comment",
        created_at=NOW,
    )
    assert repository.append_annotation_revision(
        annotation_revision_id="annotation-1",
        subject_kind="operation",
        subject_id="operation-a",
        actor_principal="operator-a",
        reason="initial reason",
        comment="initial comment",
        created_at=NOW,
    ) == annotation_1
    repository.append_annotation_revision(
        annotation_revision_id="annotation-2",
        subject_kind="operation",
        subject_id="operation-a",
        parent_revision_id="annotation-1",
        actor_principal="operator-b",
        reason="clarified reason",
        comment="clarified comment",
        created_at=LATER,
    )

    pending = repository.append_manual_pending_event(
        pending_event_id="pending-event-1",
        pending_id="pending-1",
        change_item_id="item-price-a",
        sequence_no=1,
        state="pending",
        occurred_at=NOW,
        evidence_digest=_digest("pending-1"),
        expected_pointer_revision=0,
    )
    assert repository.append_manual_pending_event(
        pending_event_id="pending-event-1",
        pending_id="pending-1",
        change_item_id="item-price-a",
        sequence_no=1,
        state="pending",
        occurred_at=NOW,
        evidence_digest=_digest("pending-1"),
        expected_pointer_revision=0,
    ) == pending
    repository.append_manual_pending_event(
        pending_event_id="pending-event-2",
        pending_id="pending-1",
        change_item_id="item-price-a",
        sequence_no=2,
        state="matched",
        related_fact_id="fact-1",
        occurred_at=LATEST,
        evidence_digest=_digest("pending-match"),
        expected_pointer_revision=1,
    )

    for index in range(2, 5):
        _append_operation(
            repository,
            f"operation-{index}",
            f"2026-08-26T10:0{index}:00Z",
            f"native-{index}",
        )
    first_page = repository.list_operations(
        seller_id=SELLER, account_scope=ACCOUNT, limit=2
    )
    assert [row["operation_id"] for row in first_page["items"]] == [
        "operation-a",
        "operation-2",
    ]
    second_page = repository.list_operations(
        seller_id=SELLER,
        account_scope=ACCOUNT,
        limit=2,
        cursor=first_page["next_cursor"],
    )
    assert [row["operation_id"] for row in second_page["items"]] == [
        "operation-3",
        "operation-4",
    ]
    assert second_page["next_cursor"] == ""

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        real_rejected = False
        try:
            conn.execute(
                f"""INSERT INTO {ITEMS_TABLE}(
                    change_item_id,operation_id,seller_id,account_scope,target_kind,
                    nm_id,advert_id,placement,parameter_field,before_value_kind,
                    requested_value_kind,requested_value_integer,mapping_version,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,'missing','integer',?,?,?)""",
                (
                    "direct-real",
                    "operation-a",
                    SELLER,
                    ACCOUNT,
                    "price",
                    1001,
                    0,
                    "",
                    "original_price_minor",
                    125.5,
                    "wb_change_registry_mapping_v1",
                    NOW,
                ),
            )
        except sqlite3.IntegrityError:
            real_rejected = True
        assert real_rejected

        for table in IMMUTABLE_TABLES:
            row = conn.execute(f"SELECT rowid FROM {table} LIMIT 1").fetchone()
            if table in {
                CHECKPOINT_SOURCE_MANIFESTS_TABLE,
                OBSERVER_JOBS_TABLE,
                OBSERVER_JOB_EVENTS_TABLE,
                OBSERVER_HEALTH_EVENTS_TABLE,
            }:
                assert row is None
                continue
            assert row is not None, f"missing fixture row for {table}"
            rowid = int(row[0])
            _sqlite_rejected(
                lambda table=table, rowid=rowid: conn.execute(
                    f"UPDATE {table} SET rowid=rowid WHERE rowid=?", (rowid,)
                )
            )
            _sqlite_rejected(
                lambda table=table, rowid=rowid: conn.execute(
                    f"DELETE FROM {table} WHERE rowid=?", (rowid,)
                )
            )
        pointer = conn.execute(
            f"SELECT * FROM {MANUAL_PENDING_CURRENT_TABLE}"
        ).fetchone()
        assert pointer is not None and pointer["active"] == 0 and pointer["revision"] == 2
        _sqlite_rejected(
            lambda: conn.execute(f"DELETE FROM {MANUAL_PENDING_CURRENT_TABLE}")
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _assert_store_registry_and_backup(
    runtime_dir: Path, db_path: Path, temp_root: Path
) -> None:
    registry = StoreRegistry(runtime_dir)
    assert registry.resolve("operational") == db_path.resolve()
    with registry.session(
        "operational", mode="ro", operation="change_registry_smoke_readback"
    ) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        source_counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in IMMUTABLE_TABLES
        }
    backup_path = temp_root / "change-registry-backup.sqlite3"
    with sqlite3.connect(db_path) as source, sqlite3.connect(backup_path) as backup:
        source.backup(backup)
    with sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as backup:
        backup.execute("PRAGMA query_only=ON")
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {
            table: backup.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in IMMUTABLE_TABLES
        } == source_counts


def _append_operation(
    repository: ChangeRegistryRepository,
    operation_id: str,
    timestamp: str,
    native_key: str,
    *,
    seller_id: str = SELLER,
) -> dict:
    return repository.create_operation(
        operation_id=operation_id,
        seller_id=seller_id,
        account_scope=ACCOUNT,
        source_surface="sku_management",
        actor_principal="operator-a",
        actor_kind="human",
        requested_at=timestamp,
        created_at=timestamp,
        native_idempotency_key=native_key,
        correlation_id=f"corr-{operation_id}",
        provenance_digest=_digest(operation_id),
    )


def _digest(value: str) -> str:
    return canonical_digest({"fixture": value})


def _rejected(error: type[Exception], call: Callable[[], object]) -> None:
    try:
        call()
    except error:
        return
    raise AssertionError(f"expected {error.__name__}")


def _sqlite_rejected(call: Callable[[], object]) -> None:
    try:
        call()
    except sqlite3.IntegrityError:
        return
    raise AssertionError("expected SQLite append-only rejection")


if __name__ == "__main__":
    main()
