"""Guarded dry-run/apply/idempotency smoke for Proxy V4 transit repair."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_proxy_v4_initialize import run_initialization  # noqa: E402
from apps.sheet_vitrina_v1_proxy_v4_initialize_smoke import _ready_plan  # noqa: E402
from apps.sheet_vitrina_v1_proxy_v4_smoke import (  # noqa: E402
    BUNDLE_FIXTURE,
    _ensure_finance_tables,
    _save_buyout_week,
    _save_finance_week,
)
from apps.sheet_vitrina_v1_proxy_v4_transit_repair import (  # noqa: E402
    CORRECTION_EFFECTIVE_DATE,
    CORRECTION_VERSION_KIND,
    _protected_operational_digest,
    run_transit_repair,
)
from packages.application.calculation_parameters import CalculationParametersBlock  # noqa: E402
from packages.application.calculation_parameters_v4 import (  # noqa: E402
    AUTOMATIC_RATE_FIELDS,
    PROXY_V4_FORMULA_VERSION,
    PROXY_V4_LEGACY_FORMULA_VERSION,
    ProxyV4ParametersBlock,
    _parameter_fingerprint,
    _parameters_from_values,
    build_latest_confirmed_week_window,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


NOW = datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)
DEPLOYED_SHA = "2" * 40


def main() -> None:
    with TemporaryDirectory(prefix="proxy-v4-transit-repair-smoke-") as temp_dir:
        root = Path(temp_dir)
        runtime_dir = root / "runtime"
        evidence_dir = root / "evidence"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        accepted = runtime.ingest_bundle(
            json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8")),
            activated_at="2026-08-01T00:00:00Z",
        )
        if accepted.status != "accepted":
            raise AssertionError(f"fixture ingest failed: {accepted}")
        CalculationParametersBlock(runtime=runtime).ensure_initial_version(
            created_at="2026-07-01T00:00:00Z"
        )
        enabled_nm_ids = [
            item.nm_id for item in runtime.load_current_state().config_v2 if item.enabled
        ]
        _ensure_finance_tables(runtime.db_path)
        for week_start, buyout, first_loaded_at in (
            ("2026-07-06", "0.70", "2026-07-13T07:00:00Z"),
            ("2026-07-13", "0.80", "2026-07-20T07:00:00Z"),
            ("2026-07-20", "0.90", "2026-07-27T07:00:00Z"),
            ("2026-07-27", "1.00", "2026-08-03T07:00:00Z"),
        ):
            _save_buyout_week(runtime, week_start, enabled_nm_ids, Decimal(buyout))
            _save_finance_week(runtime.db_path, week_start, first_loaded_at)

        current_state = runtime.load_current_state()
        for day in range(1, 10):
            as_of_date = f"2026-08-{day:02d}"
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at=f"{as_of_date}T12:00:00Z",
                plan=_ready_plan(as_of_date, enabled_nm_ids[:2]),
            )
        initialized = run_initialization(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir / "initialization",
            apply=False,
            now=datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc),
        )
        ProxyV4ParametersBlock(
            runtime=runtime,
            now_factory=lambda: datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc),
        )
        sha_file = root / "deployed.sha"
        sha_file.write_text(DEPLOYED_SHA + "\n", encoding="utf-8")
        init_applied = run_initialization(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir / "initialization",
            apply=True,
            manifest_path=Path(initialized["manifest_path"]),
            expected_manifest_sha256=str(initialized["manifest_sha256"]),
            expected_deployed_sha=DEPLOYED_SHA,
            deployed_sha_file=sha_file,
            approval_reference="initialization-test-gate",
            now=datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc),
        )
        if init_applied["status"] != "reconciled":
            raise AssertionError(f"fixture initialization failed: {init_applied}")

        _save_buyout_week(runtime, "2026-08-03", enabled_nm_ids, Decimal("0.95"))
        _save_finance_week(
            runtime.db_path,
            "2026-08-03",
            "2026-08-10T07:00:00Z",
        )
        _insert_legacy_aug_16_revision(runtime)
        for day in range(14, 18):
            as_of_date = f"2026-08-{day:02d}"
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at=f"{as_of_date}T12:00:00Z",
                plan=_ready_plan(as_of_date, enabled_nm_ids[:2]),
            )
        _ensure_protected_tables(runtime.db_path)
        protected_before = _protected_operational_digest(runtime.db_path)
        v3_before = _v3_digest(runtime.db_path)
        finance_before = _finance_metrics(runtime.db_path, "2026-08-03")

        dry_run = run_transit_repair(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir / "transit",
            apply=False,
            now=NOW,
        )
        if (
            dry_run["status"] != "ready"
            or dry_run["target_snapshot_count"] != 4
            or Decimal(dry_run["legacy_other_expense_rate"]) != Decimal("0.019")
            or Decimal(dry_run["corrected_other_expense_rate"]) != Decimal("0.014")
            or Decimal(dry_run["transit_residual_rate"]) != Decimal("0.005")
        ):
            raise AssertionError(f"transit repair dry-run drifted: {dry_run}")
        manifest = json.loads(Path(dry_run["manifest_path"]).read_text(encoding="utf-8"))
        correction_parameters = json.loads(
            manifest["desired"]["correction_version"]["parameters_json"]
        )
        if (
            correction_parameters["effective_date"] != CORRECTION_EFFECTIVE_DATE
            or correction_parameters["formula_version"] != PROXY_V4_FORMULA_VERSION
            or correction_parameters["other_expense_rate"] != "0.014"
        ):
            raise AssertionError(f"corrected immutable revision drifted: {correction_parameters}")
        if manifest["expected_effect"]["changed_v4_cell_count"] != 24:
            raise AssertionError(f"exact V4 cell scope drifted: {manifest['expected_effect']}")

        applied = run_transit_repair(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir / "transit",
            apply=True,
            manifest_path=Path(dry_run["manifest_path"]),
            expected_manifest_sha256=str(dry_run["manifest_sha256"]),
            expected_deployed_sha=DEPLOYED_SHA,
            deployed_sha_file=sha_file,
            approval_reference="owner-apply-gate-test",
            now=NOW,
        )
        if (
            applied["status"] != "reconciled"
            or not applied["database_written"]
            or not applied["non_target_preserved"]
            or applied["backup_integrity_check"] != "ok"
        ):
            raise AssertionError(f"transit repair apply failed: {applied}")
        if (
            _protected_operational_digest(runtime.db_path) != protected_before
            or _v3_digest(runtime.db_path) != v3_before
            or _finance_metrics(runtime.db_path, "2026-08-03") != finance_before
            or finance_before["transit_logistics"] != "8"
            or finance_before["capitalized_transit_logistics"] != "3"
        ):
            raise AssertionError("Finance/V3/canonical cost contours changed during V4 repair")
        _assert_repaired_snapshots(runtime, enabled_nm_ids[:2])

        block = ProxyV4ParametersBlock(runtime=runtime, now_factory=lambda: NOW)
        corrected = block.parameters_for_date("2026-08-16")
        if (
            corrected is None
            or corrected.formula_version != PROXY_V4_FORMULA_VERSION
            or corrected.version_kind != CORRECTION_VERSION_KIND
            or corrected.other_expense_rate != Decimal("0.014")
        ):
            raise AssertionError(f"corrected version readback failed: {corrected}")
        if block.parameters_for_date("2026-08-15").other_expense_rate != Decimal("0.014"):  # type: ignore[union-attr]
            raise AssertionError("pre-correction date did not retain its own immutable revision")
        rollover = block.materialize_latest_confirmed_window(business_date="2026-08-18")
        if rollover["status"] != "already_materialized" or rollover["created"]:
            raise AssertionError(f"ordinary refresh did not reuse corrected no-transit source: {rollover}")

        repeated = run_transit_repair(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir / "transit",
            apply=True,
            manifest_path=Path(dry_run["manifest_path"]),
            expected_manifest_sha256=str(dry_run["manifest_sha256"]),
            expected_deployed_sha=DEPLOYED_SHA,
            deployed_sha_file=sha_file,
            approval_reference="owner-apply-gate-test",
            now=NOW,
        )
        if repeated["status"] != "already_applied" or not repeated["idempotent_noop"]:
            raise AssertionError(f"transit repair repeat was not idempotent: {repeated}")

    print("proxy_v4_transit_repair_manifest_exact_scope: ok")
    print("proxy_v4_transit_repair_backup_cas_readback: ok")
    print("proxy_v4_transit_repair_v3_finance_cost_invariants: ok")
    print("proxy_v4_transit_repair_idempotent_refresh_semantics: ok")


def _insert_legacy_aug_16_revision(runtime: RegistryUploadDbBackedRuntime) -> None:
    window = build_latest_confirmed_week_window(
        runtime=runtime,
        today=date.fromisoformat(CORRECTION_EFFECTIVE_DATE),
    )
    if window["status"] != "ready":
        raise AssertionError(f"legacy source window is not ready: {window}")
    automatic = {
        field: Decimal(str(window["automatic_rates"][field]))
        for field in AUTOMATIC_RATE_FIELDS
    }
    excluded = window["aligned_finance"]["excluded_amounts"]
    revenue = Decimal(str(window["aligned_finance"]["net_revenue"]))
    automatic["other_expense_rate"] += (
        Decimal(str(excluded["transit_logistics"]))
        - Decimal(str(excluded["capitalized_transit_logistics"]))
    ) / revenue
    previous = ProxyV4ParametersBlock(runtime=runtime).parameters_for_date("2026-08-15")
    if previous is None:
        raise AssertionError("previous V4 revision is missing")
    revision = 3
    legacy = _parameters_from_values(
        effective_date=CORRECTION_EFFECTIVE_DATE,
        tax_rate=previous.tax_rate,
        automatic_rates=automatic,
        source_window_from=str(window["source_window_from"]),
        source_window_to=str(window["source_window_to"]),
        source_window_fingerprint="sha256:" + "a" * 64,
        source_week_ranges=tuple(tuple(item) for item in window["source_week_ranges"]),
        source_slot_from=str(window["source_slot_from"]),
        source_slot_to=str(window["source_slot_to"]),
        buyout_order_count_weight=Decimal(str(window["aligned_buyout"]["order_count_weight"])),
        finance_net_revenue_weight=revenue,
        version_id="proxy_v4_v3_20260816",
        revision=revision,
        version_kind="automatic_latest_week",
        created_at="2026-08-16T05:00:00Z",
        created_by="sheet_vitrina_v1_refresh",
        formula_version=PROXY_V4_LEGACY_FORMULA_VERSION,
    )
    public = legacy.public()
    fingerprint = _parameter_fingerprint(legacy)
    public["fingerprint"] = fingerprint
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_proxy_v4_parameter_versions(
                   version_id,block_key,revision,effective_date,source_window_from,
                   source_window_to,source_window_fingerprint,parameters_json,
                   fingerprint,version_kind,created_by,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                legacy.version_id,
                "proxy_profit_margin_v4",
                legacy.revision,
                legacy.effective_date,
                legacy.source_window_from,
                legacy.source_window_to,
                legacy.source_window_fingerprint,
                json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                fingerprint,
                legacy.version_kind,
                legacy.created_by,
                legacy.created_at,
            ),
        )


def _ensure_protected_tables(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_wb_daily_cost(
                as_of_date TEXT,nm_id TEXT,wac_rub TEXT
            );
            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_cost_daily_state(
                as_of_date TEXT,nm_id TEXT,wac_rub TEXT
            );
            CREATE TABLE IF NOT EXISTS wb_finance_weekly_sku_aggregates(
                seller_id TEXT,week_start TEXT,week_end TEXT,nm_id TEXT,metrics_json TEXT
            );
            """
        )


def _assert_repaired_snapshots(
    runtime: RegistryUploadDbBackedRuntime,
    nm_ids: list[int],
) -> None:
    for day in range(14, 18):
        as_of_date = f"2026-08-{day:02d}"
        snapshot = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of_date)
        rows = {
            str(row[1]): row
            for sheet in snapshot.sheets
            if sheet.sheet_name == "DATA_VITRINA"
            for row in sheet.rows
        }
        expected = {
            "TOTAL|total_proxy_profit_4_rub",
            "TOTAL|proxy_margin_4_pct_total",
            *{
                f"SKU:{nm_id}|{metric}"
                for nm_id in nm_ids
                for metric in ("proxy_profit_4_rub", "proxy_margin_4_pct")
            },
        }
        if not expected.issubset(rows):
            raise AssertionError(f"repaired snapshot misses V4 rows on {as_of_date}")
        if any(rows[row_id][2] in (None, "") for row_id in expected):
            raise AssertionError(f"repaired snapshot has blank eligible V4 cell on {as_of_date}")
        metadata = snapshot.metadata.get("proxy_v4_historical_initialization")
        if not metadata or metadata["eligibility_by_date"][as_of_date]["eligible_sku_count"] != 2:
            raise AssertionError(f"repaired snapshot eligibility metadata drifted on {as_of_date}")


def _finance_metrics(db_path: Path, week_start: str) -> dict[str, str]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT metrics_json FROM wb_finance_weekly_aggregates WHERE week_start=?",
            (week_start,),
        ).fetchone()
    return json.loads(str(row[0]))


def _v3_digest(db_path: Path) -> str:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT version_id,revision,effective_date,rates_json,fingerprint
               FROM sheet_vitrina_v1_calculation_parameter_versions ORDER BY revision"""
        ).fetchall()
    return json.dumps(rows, sort_keys=True)


if __name__ == "__main__":
    main()
