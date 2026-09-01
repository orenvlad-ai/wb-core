#!/usr/bin/env python3
"""Production-shaped apply/readback/revoke rehearsal for WBC0027 break-glass."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wbc0027_breakglass_last_good import (  # noqa: E402
    ECONOMICS_KEYS,
    PRODUCTION_FAMILY_COUNTS,
    PRODUCTION_SOURCE_EMPTY_IDENTITIES,
    WAC_KEYS,
    _breakglass_only_authorizer,
    apply_manifest,
    build_manifest,
    readback_manifest,
    revoke_manifest,
    revoke_readback_manifest,
)
from packages.application.sheet_vitrina_v1_breakglass_last_good import (  # noqa: E402
    apply_breakglass_last_good_overlay,
    read_active_breakglass_last_good,
)
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow  # noqa: E402

CAPTURE_ID = "ivhc_5b3641a9ee83e335828455c4c612"
CAPTURE_DIGEST = "sha256:d2e6b12311a6cc94097c2b54fec7590d6f08f1a917636bf18cf79219e0612fc7"
BUNDLE = "registry_upload_bundle_v1__2026-06-08T00:00:00Z"
READY_AS_OF = "2026-08-29"
SNAPSHOT_ID = "2026-08-29__2026-08-30__sheet_vitrina_v1_temporal_live_v1__current"
COLUMN_DATE = "2026-08-30"
PUBLIC_DATES = ["2026-08-31", "2026-09-01"]
NM_IDS = [497413772, *range(600000001, 600000033)]


def main() -> None:
    with TemporaryDirectory(prefix="wbc0027-breakglass-last-good-") as raw:
        root = Path(raw)
        db_path = root / "operational.sqlite3"
        source_path = root / "sealed-economics.json"
        _seed_operational(db_path)
        raw_plan = _seed_sealed_economics(source_path)
        operation_id = "wbc0027-breakglass-production-shaped-op"
        manifest = build_manifest(
            db_path=db_path,
            operation_id=operation_id,
            source_capture_id=CAPTURE_ID,
            economics_source_path=source_path,
            expected_economics_source_sha256=_file_sha256(source_path),
            expected_raw_plan_sha256=_text_sha256(raw_plan),
            economics_patch_index=2,
            economics_bundle_version=BUNDLE,
            economics_ready_as_of=READY_AS_OF,
            economics_snapshot_id=SNAPSHOT_ID,
            economics_column_date=COLUMN_DATE,
            expected_capture_sha256=CAPTURE_DIGEST,
            expected_capture_sequence=361,
            expected_capture_captured_at="2026-08-31T11:09:59Z",
            public_date_columns=PUBLIC_DATES,
            expected_cell_count=303,
            created_at="2026-09-01T15:00:00Z",
        )
        assert manifest["scope"]["family_counts"] == PRODUCTION_FAMILY_COUNTS
        assert manifest["scope"]["eligible_presentation_count"] == 606
        assert set(manifest["source"]["source_empty_identities"]) == PRODUCTION_SOURCE_EMPTY_IDENTITIES
        assert manifest["scope"]["inventory_totals"] == {
            "Orenburg": 25920, "Moscow": 72898, "FBS": 98818,
            "WB": 44428, "combined": 143246,
        }
        manifest_path = root / "manifest.json"
        manifest_path.write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
        manifest_digest = _file_sha256(manifest_path)
        writer_lock_path = root / "writer.lock"
        writer_lock_path.touch()
        evidence_dir = root / "evidence"
        _assert_source_drift_blocks(
            source_path, db_path, manifest_path, manifest_digest, operation_id,
            evidence_dir, writer_lock_path,
        )
        _assert_authorizer_denies_source_write(db_path)
        receipt = apply_manifest(
            db_path=db_path, manifest_path=manifest_path,
            expected_manifest_sha256=manifest_digest, operation_id=operation_id,
            evidence_dir=evidence_dir, writer_lock_path=writer_lock_path,
        )
        assert receipt["cell_insert_count"] == 303
        assert receipt["production_mutation_submit_count"] == 1
        assert receipt["transaction_count"] == 1
        assert receipt["readback"]["status"] == "verified"
        assert all(
            receipt[f"{name}_write_count"] == 0
            for name in ("wb", "fbo", "warehouse", "history", "ready_snapshot", "source", "capital", "non_target")
        )
        _assert_overlay_matrix(db_path, manifest)
        assert readback_manifest(
            db_path=db_path, manifest_path=manifest_path,
            expected_manifest_sha256=manifest_digest, operation_id=operation_id,
        )["active"] is True
        revocation_id = "wbc0027-breakglass-production-shaped-revoke"
        revoked = revoke_manifest(
            db_path=db_path, manifest_path=manifest_path,
            expected_manifest_sha256=manifest_digest, operation_id=operation_id,
            revocation_id=revocation_id,
            reason="production-shaped guarded revoke rehearsal",
            evidence_dir=evidence_dir, writer_lock_path=writer_lock_path,
        )
        assert revoked["production_mutation_submit_count"] == 1
        assert revoked["transaction_count"] == 1
        assert revoked["readback"]["status"] == "verified_revoked"
        assert read_active_breakglass_last_good(db_path) is None
        second = revoke_readback_manifest(
            db_path=db_path, manifest_path=manifest_path,
            expected_manifest_sha256=manifest_digest, operation_id=operation_id,
            revocation_id=revocation_id,
        )
        assert second == revoked["readback"]
        _assert_revoked_overlay_is_noop(db_path, manifest)
        _assert_artifact_before_marker(db_path, evidence_dir, operation_id, revocation_id)
        _assert_no_blind_retry(
            db_path, manifest_path, manifest_digest, operation_id, revocation_id,
            evidence_dir, writer_lock_path,
        )
    print("wbc0027_breakglass_last_good_smoke: OK")


def _assert_overlay_matrix(db_path: Path, manifest: dict[str, object]) -> None:
    rows: list[WebVitrinaContractRow] = []
    ordinary_presentations = 0
    for cell_index, cell in enumerate(manifest["cells"]):
        values: dict[str, object] = {}
        for date_index, business_date in enumerate(PUBLIC_DATES):
            presentation_index = cell_index * len(PUBLIC_DATES) + date_index
            if presentation_index < 34:
                values[business_date] = f"ordinary-{presentation_index}"
                ordinary_presentations += 1
            else:
                values[business_date] = ""
        rows.append(_row(str(cell["row_id"]), values))
    for row_id in sorted(PRODUCTION_SOURCE_EMPTY_IDENTITIES):
        rows.append(_row(row_id, {item: "" for item in PUBLIC_DATES}))
    overlaid = apply_breakglass_last_good_overlay(rows, db_path=db_path, date_columns=PUBLIC_DATES)
    by_id = {item.row_id: item for item in overlaid}
    provisional = sum(
        1 for row in overlaid for business_date in PUBLIC_DATES
        if (row.presentation_by_date.get(business_date) or {}).get("quality_state")
        == "last_good_provisional"
    )
    preserved = sum(
        1 for row in overlaid for value in row.values_by_date.values()
        if isinstance(value, str) and value.startswith("ordinary-")
    )
    assert provisional == 572
    assert preserved == ordinary_presentations == 34
    assert all(
        by_id[row_id].values_by_date == {item: "" for item in PUBLIC_DATES}
        for row_id in PRODUCTION_SOURCE_EMPTY_IDENTITIES
    )


def _assert_source_drift_blocks(
    source_path: Path, db_path: Path, manifest_path: Path, manifest_digest: str,
    operation_id: str, evidence_dir: Path, writer_lock_path: Path,
) -> None:
    exact = source_path.read_bytes()
    source_path.write_bytes(exact + b" ")
    try:
        apply_manifest(
            db_path=db_path, manifest_path=manifest_path,
            expected_manifest_sha256=manifest_digest, operation_id=operation_id,
            evidence_dir=evidence_dir, writer_lock_path=writer_lock_path,
        )
    except Exception as exc:
        assert "sealed economics source changed" in str(exc)
    else:
        raise AssertionError("source drift must fail before submit")
    finally:
        source_path.write_bytes(exact)


def _assert_authorizer_denies_source_write(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.set_authorizer(_breakglass_only_authorizer)
    try:
        conn.execute("UPDATE registry_upload_current_state SET activated_at='forbidden'")
    except sqlite3.DatabaseError as exc:
        assert "authorized" in str(exc).lower()
    else:
        raise AssertionError("breakglass authorizer must deny source writes")
    finally:
        conn.rollback()
        conn.set_authorizer(None)
        conn.close()


def _assert_revoked_overlay_is_noop(db_path: Path, manifest: dict[str, object]) -> None:
    first = manifest["cells"][0]
    row = _row(str(first["row_id"]), {item: "" for item in PUBLIC_DATES})
    assert apply_breakglass_last_good_overlay(
        [row], db_path=db_path, date_columns=PUBLIC_DATES
    )[0].values_by_date == row.values_by_date


def _assert_artifact_before_marker(
    db_path: Path, evidence_dir: Path, operation_id: str, revocation_id: str
) -> None:
    before = json.loads((evidence_dir / f"{operation_id}.before.json").read_text())
    revoke_plan = json.loads((evidence_dir / f"{revocation_id}.plan.json").read_text())
    conn = sqlite3.connect(db_path)
    applied_at = conn.execute(
        "SELECT applied_at FROM sheet_vitrina_v1_breakglass_last_good_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0]
    revoked_at = conn.execute(
        "SELECT revoked_at FROM sheet_vitrina_v1_breakglass_last_good_revocations WHERE revocation_id=?",
        (revocation_id,),
    ).fetchone()[0]
    conn.close()
    assert str(before["captured_at"]) <= str(applied_at)
    assert str(revoke_plan["created_at"]) == str(revoked_at)


def _assert_no_blind_retry(
    db_path: Path, manifest_path: Path, manifest_digest: str,
    operation_id: str, revocation_id: str, evidence_dir: Path,
    writer_lock_path: Path,
) -> None:
    callbacks = (
        lambda: apply_manifest(
            db_path=db_path, manifest_path=manifest_path,
            expected_manifest_sha256=manifest_digest, operation_id=operation_id,
            evidence_dir=evidence_dir, writer_lock_path=writer_lock_path,
        ),
        lambda: revoke_manifest(
            db_path=db_path, manifest_path=manifest_path,
            expected_manifest_sha256=manifest_digest, operation_id=operation_id,
            revocation_id=revocation_id, reason="must not repeat",
            evidence_dir=evidence_dir, writer_lock_path=writer_lock_path,
        ),
    )
    for callback in callbacks:
        try:
            callback()
        except Exception as exc:
            assert "blind retry forbidden" in str(exc) or "not active" in str(exc)
        else:
            raise AssertionError("a second submit must fail closed")


def _seed_operational(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE registry_upload_current_state(slot INTEGER PRIMARY KEY,bundle_version TEXT,activated_at TEXT);
        CREATE TABLE registry_upload_config_v2(bundle_version TEXT,nm_id INTEGER,enabled INTEGER,display_name TEXT,group_name TEXT,display_order INTEGER);
        CREATE TABLE sheet_vitrina_v1_inventory_history_captures(
          capture_sequence INTEGER,capture_id TEXT,business_date TEXT,capture_kind TEXT,
          formula_version TEXT,bundle_version TEXT,ready_snapshot_id TEXT,ready_plan_version TEXT,
          generation_identity TEXT,facility_roster_revision TEXT,facility_roster_json TEXT,
          source_manifest_json TEXT,source_digest TEXT,captured_at TEXT
        );
        CREATE TABLE sheet_vitrina_v1_inventory_history_components(
          capture_id TEXT,scope_kind TEXT,scope_key TEXT,nm_id INTEGER,component_kind TEXT,
          component_id TEXT,component_label TEXT,state TEXT,quantity INTEGER,source_revision TEXT,
          source_digest TEXT,source_watermark TEXT,provenance_json TEXT,captured_at TEXT
        );
        CREATE TABLE sheet_vitrina_v1_inventory_history_finalizations(x TEXT);
        CREATE TABLE sheet_vitrina_v1_ready_snapshots(x TEXT);
        CREATE TABLE sheet_vitrina_v1_warehouse_functional_active(x TEXT);
        CREATE TABLE sheet_vitrina_v1_warehouse_wb_snapshots(x TEXT);
        CREATE TABLE sheet_vitrina_v1_ff_pool_balances(x TEXT);
        CREATE TABLE sheet_vitrina_v1_ff_pool_fbs_lifecycle_current(x TEXT);
        INSERT INTO registry_upload_current_state VALUES(1,'registry_upload_bundle_v1__2026-06-08T00:00:00Z','2026-09-01T00:00:00Z');
        """
    )
    for index, nm_id in enumerate(NM_IDS):
        conn.execute(
            "INSERT INTO registry_upload_config_v2 VALUES(?,?,?,?,?,?)",
            (BUNDLE, nm_id, 1, f"SKU {nm_id}", "G", index),
        )
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_inventory_history_captures VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (361, CAPTURE_ID, "2026-08-31", "accepted_refresh", "inventory_planning_v1",
         BUNDLE, "ready", "plan", "generation", "roster", "[]", "{}",
         CAPTURE_DIGEST, "2026-08-31T11:09:59Z"),
    )
    scopes = [("TOTAL", "TOTAL", None), *[("SKU", f"SKU:{item}", item) for item in NM_IDS]]
    for scope_kind, scope_key, nm_id in scopes:
        quantities = (
            (("WB", "WB", "WB", 44428),
             ("FBS_FACILITY", "orenburg", "FBS Оренбург", 25920),
             ("FBS_FACILITY", "moscow", "FBS Москва", 72898))
            if scope_key == "TOTAL" else
            (("WB", "WB", "WB", 1),
             ("FBS_FACILITY", "orenburg", "FBS Оренбург", 2),
             ("FBS_FACILITY", "moscow", "FBS Москва", 3))
        )
        for kind, component_id, label, quantity in quantities:
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_inventory_history_components VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (CAPTURE_ID, scope_kind, scope_key, nm_id, kind, component_id, label,
                 "exact", quantity, "revision", "sha256:" + "2" * 64,
                 "watermark", "{}", "2026-08-31T11:09:59Z"),
            )
    conn.commit()
    conn.close()


def _seed_sealed_economics(path: Path) -> str:
    rows: list[list[object]] = []
    sku_keys = sorted(
        item for item in WAC_KEYS | ECONOMICS_KEYS
        if not item.startswith("total_") and not item.endswith("_total")
    )
    total_keys = sorted(
        item for item in WAC_KEYS | ECONOMICS_KEYS
        if item.startswith("total_") or item.endswith("_total")
    )
    for nm_id in NM_IDS:
        for metric_key in sku_keys:
            row_id = f"SKU:{nm_id}|{metric_key}"
            value: object = "" if row_id in PRODUCTION_SOURCE_EMPTY_IDENTITIES else 123.45
            rows.append([metric_key, row_id, "", value])
    for metric_key in total_keys:
        rows.append([metric_key, f"TOTAL|{metric_key}", "", 456.78])
    plan = {
        "as_of_date": READY_AS_OF,
        "date_columns": [READY_AS_OF, COLUMN_DATE],
        "sheets": [{"sheet_name": "DATA_VITRINA", "rows": rows}],
    }
    raw_plan = _canonical_json(plan)
    payload = {"functional_economics": {"patches": [
        {"identity": [BUNDLE, "2026-08-25", "old-1"], "before_plan_json": "{}"},
        {"identity": [BUNDLE, "2026-08-26", "old-2"], "before_plan_json": "{}"},
        {"identity": [BUNDLE, READY_AS_OF, SNAPSHOT_ID], "before_plan_json": raw_plan},
    ]}}
    path.write_text(_canonical_json(payload) + "\n", encoding="utf-8")
    return raw_plan


def _row(row_id: str, values: dict[str, object]) -> WebVitrinaContractRow:
    scope_key, _, metric_key = row_id.partition("|")
    return WebVitrinaContractRow(
        row_id=row_id, row_order=1,
        scope_kind="TOTAL" if scope_key == "TOTAL" else "SKU",
        scope_key=scope_key, scope_label=scope_key,
        metric_key=metric_key, metric_label=metric_key,
        row_last_updated_at="2026-09-01T00:00:00Z", section="test",
        group=None, nm_id=None if scope_key == "TOTAL" else int(scope_key.split(":", 1)[1]),
        format="number", values_by_date=values, presentation_by_date={},
    )


def _file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    main()
