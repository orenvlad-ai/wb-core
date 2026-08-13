"""Focused Stage 3 checks for protected facility/pool read and mutation models."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from io import BytesIO
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook  # noqa: E402

from packages.application.ff_pool_documents import (  # noqa: E402
    DOCUMENTS_TABLE,
    FfPoolDocumentService,
)
from packages.application.ff_pool_documents_xlsx import CHINA_SHEET, XLSX_CONTENT_TYPE  # noqa: E402
from packages.application.ff_pool_foundation import (  # noqa: E402
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_CHANGES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
    evaluate_ff_pool_aggregate_parity,
    record_ff_pool_parity_diagnostic,
)
from packages.application.ff_pool_surfaces import FfPoolSurface, FfPoolSurfaceError  # noqa: E402
from packages.contracts.ff_pool_documents import DocumentIdentity  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self.current.isoformat(timespec="seconds").replace("+00:00", "Z")
        self.current += timedelta(seconds=1)
        return value


def main() -> None:
    _schema_absence_is_controlled()
    _guided_preview_is_default_off()
    with TemporaryDirectory(prefix="ff-pool-surfaces-") as directory:
        root = Path(directory)
        clock = Clock()
        service = FfPoolDocumentService(
            db_path=root / "state.sqlite3",
            runtime_dir=root,
            timestamp_factory=clock,
            resume=False,
        )
        surface = FfPoolSurface(
            db_path=service.db_path,
            runtime_dir=root,
            timestamp_factory=clock,
        )
        _default_off_reads_do_not_write(surface, service.db_path)
        facilities = _facility_management(surface, service.db_path, clock)
        request_id = _document_workflow(surface, facilities)
        _exact_reader_and_pagination(surface, service.db_path, clock, facilities)
        _deactivation_with_dependencies_is_blocked(surface, facilities)
        _read_models(surface, request_id, facilities)
    print("ff_pool_surfaces_smoke: OK")


def _guided_preview_is_default_off() -> None:
    with TemporaryDirectory(prefix="ff-guided-default-off-") as directory:
        root = Path(directory)
        clock = Clock()
        service = FfPoolDocumentService(
            db_path=root / "state.sqlite3", runtime_dir=root, timestamp_factory=clock, resume=False
        )
        with sqlite3.connect(service.db_path) as conn:
            conn.execute(
                f"INSERT INTO {FACILITIES_TABLE} VALUES(?,?,?,?,?,?,?)",
                ("fac_preview", "PREVIEW", "Preview FF", 1, "Europe/Moscow", clock(), clock()),
            )
            conn.execute(
                f"INSERT INTO {FACILITY_PROFILES_TABLE} VALUES(?,?,?, ?,?)",
                ("fac_preview", "Москва", "{}", clock(), clock()),
            )
            conn.commit()
        lines = [{
            "nm_id": 101, "barcode": "0000000000101", "sku": "SKU-101",
            "accepted_quantity": 2, "accepted_capital_rub": "200.00",
        }]
        workbook_bytes = service.generate_china_acceptance_template(
            shipment_lines=lines, source_revision="supplier-revision-v1", selected_facility_id="fac_preview"
        )
        workbook = load_workbook(BytesIO(workbook_bytes))
        workbook[CHINA_SHEET]["G6"], workbook[CHINA_SHEET]["H6"] = 1, 1
        output = BytesIO()
        workbook.save(output)
        preview = service.preview_china_acceptance_workbook(
            identity=DocumentIdentity(
                request_id="fixture:guided-preview:request",
                source_system="supplier_registry",
                source_type="china_acceptance_workbook",
                source_id="shipment-preview",
                source_revision="supplier-revision-v1",
                idempotency_epoch=1,
                actor="fixture",
                business_date="2026-08-12",
            ),
            source_bytes=output.getvalue(),
            source_filename="guided.xlsx",
            source_content_type=XLSX_CONTENT_TYPE,
            shipment_lines=lines,
            template_source_revision="supplier-revision-v1",
        )
        assert preview["state"] == "ready"
        surface = FfPoolSurface(db_path=service.db_path, runtime_dir=root, timestamp_factory=clock)
        status = surface.request_status(str(preview["request_id"]))
        assert not status["confirm_allowed"]
        assert status["guided_acceptance_activation"]["reason"] == "writer_epoch_off"
        try:
            surface.confirm_document(str(preview["request_id"]))
            raise AssertionError("default-off guided acceptance must fail closed")
        except FfPoolSurfaceError as exc:
            assert exc.code == "guided_acceptance_not_activated"
        with sqlite3.connect(service.db_path) as conn:
            assert conn.execute(f"SELECT COUNT(*) FROM {DOCUMENTS_TABLE}").fetchone()[0] == 0


def _schema_absence_is_controlled() -> None:
    with TemporaryDirectory(prefix="ff-pool-surface-absent-") as directory:
        db_path = Path(directory) / "empty.sqlite3"
        db_path.touch()
        surface = FfPoolSurface(db_path=db_path, runtime_dir=Path(directory))
        payload = surface.capabilities()
        assert payload["status"] == "schema_absent"
        assert payload["feature"]["reason"] == "schema_absent_default_off"
        assert payload["hidden_actions"] == ["facility_pool_opening"]


def _counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sheet_vitrina_v1_ff_%'"
            ).fetchall()
        ]
        return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}


def _default_off_reads_do_not_write(surface: FfPoolSurface, db_path: Path) -> None:
    before = _counts(db_path)
    first = surface.capabilities(aggregate_revision="aggregate-empty")
    page = surface.facilities_page(aggregate_revision="aggregate-empty")
    documents = surface.documents_page()
    after = _counts(db_path)
    assert before == after
    assert not first["feature"]["writer_effective"]
    assert not first["feature"]["reader_effective"]
    assert page["facilities"] == [] and documents["documents"] == []
    assert first["etag"] == surface.capabilities(aggregate_revision="aggregate-empty")["etag"]
    try:
        surface.create_facility(
            {"request_id": "fixture:facility:off", "name": "Blocked", "active": True},
            actor="fixture",
        )
    except FfPoolSurfaceError as exc:
        assert exc.code == "facility_pool_feature_off" and exc.http_status == 409
    else:
        raise AssertionError("feature-off facility mutation must fail closed")
    assert _counts(db_path) == before


def _facility_management(surface: FfPoolSurface, db_path: Path, clock: Clock) -> list[dict[str, object]]:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) "
            "VALUES(1,1,0,'stage3-fixture-writer',?,'{}')",
            (clock(),),
        )
        conn.commit()
    first_payload = {
        "request_id": "fixture:facility:moscow",
        "name": "Москва FF",
        "city": "Москва",
        "active": True,
        "display_timezone": "Europe/Moscow",
    }
    first = surface.create_facility(first_payload, actor="fixture-operator")
    repeated = surface.create_facility(first_payload, actor="fixture-operator")
    assert not first["idempotent"] and repeated["idempotent"]
    facility = dict(first["facility"])
    stable_id, stable_code = facility["facility_id"], facility["code"]
    updated = surface.update_facility(
        str(stable_id),
        {
            "request_id": "fixture:facility:moscow:update",
            "expected_updated_at": facility["updated_at"],
            "name": "Москва Север",
            "active": False,
            "display_timezone": "Asia/Yekaterinburg",
        },
        actor="fixture-operator",
    )
    assert updated["facility"]["facility_id"] == stable_id
    assert updated["facility"]["code"] == stable_code
    assert len(updated["audit"]) == 4
    second = surface.create_facility(
        {
            "request_id": "fixture:facility:orenburg",
            "name": "Москва Юг",
            "city": "Москва",
            "active": True,
            "display_timezone": "Asia/Yekaterinburg",
        },
        actor="fixture-operator",
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {FACILITY_CHANGES_TABLE}").fetchone()[0] == 5
        try:
            conn.execute(f"DELETE FROM {FACILITIES_TABLE} WHERE facility_id=?", (stable_id,))
        except sqlite3.IntegrityError as exc:
            assert "retained" in str(exc)
        else:
            raise AssertionError("facility physical delete must be blocked")
    return [dict(updated["facility"]), dict(second["facility"])]


def _deactivation_with_dependencies_is_blocked(
    surface: FfPoolSurface,
    facilities: list[dict[str, object]],
) -> None:
    facility_id = str(facilities[1]["facility_id"])
    detail = surface.facility_detail(facility_id)
    try:
        surface.update_facility(
            facility_id,
            {
                "request_id": "fixture:facility:deactivation-blocked",
                "expected_updated_at": detail["facility"]["updated_at"],
                "active": False,
            },
            actor="fixture-operator",
        )
    except FfPoolSurfaceError as exc:
        assert exc.code == "facility_deactivation_blocked"
        assert exc.details["nonzero_balance_count"] == 2
    else:
        raise AssertionError("facility with non-zero balances must stay active")


def _document_workflow(surface: FfPoolSurface, facilities: list[dict[str, object]]) -> str:
    request = surface.accept_document_preview(
        {
            "request_id": "fixture:transfer:root",
            "document_kind": "transfer_root",
            "business_date": "2026-08-12",
            "manifest": {
                "source": {"facility_id": facilities[1]["facility_id"], "pool": "FBS"},
                "destination": {"facility_id": facilities[1]["facility_id"], "pool": "FBO"},
            },
        },
        actor="fixture-operator",
    )
    assert request["state"] == "ready" and request["confirm_allowed"]
    canonical = str(request["request_id"])
    confirmed = surface.confirm_document(canonical)
    assert confirmed["state"] == "complete"
    repeat = surface.accept_document_preview(
        {
            "request_id": "fixture:transfer:root",
            "document_kind": "transfer_root",
            "business_date": "2026-08-12",
            "manifest": {
                "source": {"facility_id": facilities[1]["facility_id"], "pool": "FBS"},
                "destination": {"facility_id": facilities[1]["facility_id"], "pool": "FBO"},
            },
        },
        actor="fixture-operator",
    )
    assert repeat["request_id"] == canonical and repeat["state"] == "complete"
    return canonical


def _exact_reader_and_pagination(
    surface: FfPoolSurface,
    db_path: Path,
    clock: Clock,
    facilities: list[dict[str, object]],
) -> None:
    facility_id = str(facilities[1]["facility_id"])
    aggregate = [
        {"nm_id": 101, "quantity": 1, "capital_rub": "0.1"},
        {"nm_id": 102, "quantity": 2, "capital_rub": "0.2"},
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) "
            "VALUES(2,1,1,'stage3-fixture-reader',?,'{}')",
            (clock(),),
        )
        conn.executemany(
            f"INSERT INTO {BALANCES_TABLE}(facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,source_watermark,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (facility_id, "FBS", 101, 2, 1, "0.1", "0.1", "fixture", clock()),
                (facility_id, "FBS", 102, 2, 2, "0.2", "0.1", "fixture", clock()),
            ],
        )
        parity = evaluate_ff_pool_aggregate_parity(conn, aggregate)
        assert parity.status == "pass"
        record_ff_pool_parity_diagnostic(
            conn,
            diagnostic_id="stage3-fixture-pass",
            aggregate_revision="aggregate-v2",
            checked_at=clock(),
            result=parity,
        )
        conn.commit()
    page = surface.facilities_page(page=1, limit=1, aggregate_revision="aggregate-v2")
    assert page["feature"]["reader_effective"] and page["page"]["has_next"]
    second_page = surface.facilities_page(page=2, limit=1, aggregate_revision="aggregate-v2")
    assert len(second_page["facilities"]) == 1
    detail = surface.facility_detail(facility_id, aggregate_revision="aggregate-v2")
    fbs = next(item for item in detail["pools"] if item["pool"] == "FBS")
    assert fbs["quantity"] == 3 and fbs["capital_rub"] == "0.3" and fbs["wac_rub"] == "0.1"
    pool = surface.pool_detail(facility_id, "FBS", limit=1, aggregate_revision="aggregate-v2")
    assert len(pool["balances"]) == 1 and pool["page"]["has_next"]


def _read_models(surface: FfPoolSurface, request_id: str, facilities: list[dict[str, object]]) -> None:
    status = surface.request_status(request_id)
    assert status["state"] == "complete" and len(status["steps"]) == 6
    documents = surface.documents_page(page=1, limit=10)
    assert documents["page"]["total_count"] == 1
    assert all(item["document_kind"] != "facility_pool_opening" for item in documents["documents"])
    root_id = str(documents["documents"][0]["root_document_id"])
    detail = surface.document_detail(root_id)
    assert detail["root_document_id"] == root_id and len(detail["documents"]) == 1
    assert surface.document_lines(root_id)["lines"] == []
    assert surface.document_expenses(root_id)["expenses"] == []
    graph = surface.document_graph(root_id)
    assert len(graph["nodes"]) == 1
    filtered = surface.documents_page(facility_id=str(facilities[1]["facility_id"]))
    assert filtered["documents"] == []  # root has no movement lines; context is never inferred.
    assert documents["payload_bytes"] < 128 * 1024


if __name__ == "__main__":
    main()
