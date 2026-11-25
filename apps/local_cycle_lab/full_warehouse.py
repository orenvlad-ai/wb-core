"""Synthetic historical inputs and independent native accounting readback.

Only the historical incoming cost layers are imported as frozen inputs. The
baseline, functional cutover, facility opening, FBS book and expense are built
by their actual domain writers. Never call this on an accepted-expense archive:
the facility cutover advances its immutable feature epoch.
"""
from contextlib import closing, contextmanager, ExitStack
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sqlite3
import fcntl
import subprocess

NM = 101
QUANTITY = 7
WB_QUANTITY = 12
UNIT_COST = Decimal("111.18")
EXPENSE = Decimal("123.45")
DERIVED_TOLERANCE = Decimal("1e-40")
PREFIX = "sheet_vitrina_v1_"


def _readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _insert(conn, table, row):
    # Names below are owned by this versioned input fixture, never user input.
    conn.execute(f"INSERT INTO {PREFIX}{table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                 tuple(row.values()))


def expected():
    with localcontext() as context:
        context.prec = 50
        initial = UNIT_COST * QUANTITY
        after = initial + EXPENSE
        wb = UNIT_COST * WB_QUANTITY
        return dict(nm_id=NM, fbs_quantity=QUANTITY, wb_quantity=WB_QUANTITY,
                    initial_fbs_capital_rub=str(initial), initial_unit_cost_rub=str(UNIT_COST),
                    expense_rub=str(EXPENSE), fbs_capital_rub=str(after),
                    fbs_wac_rub=str(after / QUANTITY), wb_capital_rub=str(wb),
                    shared_quantity=QUANTITY + WB_QUANTITY,
                    shared_capital_rub=str(after + wb),
                    shared_wac_rub=str((after + wb) / (QUANTITY + WB_QUANTITY)))


def seed_facility(runtime, stamp):
    """Source-only directory/configuration for a NEW fixture, before cutover.

    Creating an active facility through the operator UI after catalogue setup
    already posts dense zero inventory, making the legacy migration ineligible.
    This imported initial directory has no balances or business documents.
    """
    from packages.application.ff_pool_documents import FfPoolDocumentService
    FfPoolDocumentService(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir, resume=False,
                          timestamp_factory=lambda: stamp)
    with sqlite3.connect(runtime.db_path) as conn:
        for name in ("ff_facilities", "ff_pool_documents", "ff_pool_balances", "ff_pool_feature_epochs"):
            if conn.execute(f"SELECT COUNT(*) FROM {PREFIX}{name}").fetchone()[0]:
                raise ValueError("source-only initial directory requires new empty domain: " + name)
        _insert(conn, "ff_pool_feature_epochs", dict(epoch=1, writer_enabled=1, reader_enabled=0,
            source_revision="synthetic-bootstrap-config-v1", created_at=stamp, metadata_json='{"synthetic_input":true}'))
        _insert(conn, "ff_facilities", dict(facility_id="lab-synthetic-ff", code="LAB-FF", name="Тестовый FF",
            active=1, display_timezone="Asia/Yekaterinburg", created_at=stamp, updated_at=stamp))
        _insert(conn, "ff_facility_profiles", dict(facility_id="lab-synthetic-ff", city="Москва", created_at=stamp, updated_at=stamp))


@contextmanager
def _bootstrap_barrier(entry, fingerprint):
    """Observe guest conditions and own actual native admission/barrier locks.

    This does not claim a production maintenance pause. No guest service or
    timer setting is changed: exact before/after service readback proves that.
    """
    from .full_history import isolation_evidence
    from apps.web_vitrina_finished_snapshot_build import HEAVY_UNITS
    from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, warehouse_functional_write_lock
    from packages.application.business_data_write_barrier import acquire_barrier, barrier_status, abort_barrier_acquire
    isolation = isolation_evidence()
    if isolation["host_mounts"]:
        raise ValueError("bootstrap requires a guest without host mounts")
    def units():
        result = {}
        for unit in sorted(set(HEAVY_UNITS) | {name.removesuffix('.service') + '.timer' for name in HEAVY_UNITS}):
            completed = subprocess.run(["systemctl", "show", unit,
                "--property=LoadState,ActiveState,MainPID,UnitFileState"], text=True,
                capture_output=True, timeout=5)
            values = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
            missing_timer = unit.endswith(".timer") and values.get("LoadState") == "not-found"
            if (completed.returncode and not missing_timer) or (unit in HEAVY_UNITS and values.get("LoadState") != "loaded"):
                raise ValueError("guest unit cannot be inspected: " + unit + completed.stderr)
            if (values.get("ActiveState") != "inactive"
                    or (unit in HEAVY_UNITS and values.get("MainPID") != "0")
                    or ("MainPID" in values and values["MainPID"] != "0")):
                raise ValueError("guest writer/timer is not inactive: " + unit + repr(values))
            result[unit] = values
        return result
    before = units()
    listeners = []
    for name in ("tcp", "tcp6"):
        path = Path("/proc/net") / name
        if path.exists():
            listeners.extend(line for line in path.read_text().splitlines()[1:] if line.split()[3] == "0A")
    if listeners:
        raise ValueError("fresh bootstrap namespace already has a listening server")
    runtime = entry.runtime.runtime_dir
    evidence = dict(scope="disposable_local_bootstrap_only", isolation=isolation, units_before=before,
                    http_listeners=listeners, service_or_timer_mutations=0)
    with ExitStack() as stack:
        stack.enter_context(warehouse_functional_job_lock(runtime, blocking=False))
        handle = stack.enter_context((runtime / ".ff-pool-document-posting.lock").open("a+b"))
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with warehouse_functional_write_lock(runtime, blocking=False) as probe:
            evidence["warehouse_writer_probe"] = dict(probe)
        window = "lab-bootstrap-" + fingerprint.removeprefix("sha256:")[:20]
        evidence["http_barrier"] = acquire_barrier(runtime, window_id=window, window_kind="final_cutover",
            plan_fingerprint=fingerprint, approval_reference="local-lab-synthetic-bootstrap", actor="local-lab",
            reason="Isolated initial migration; no HTTP listener and no service/timer writers")
        actual = barrier_status(runtime)
        if not actual["active"] or actual["window_id"] != window:
            raise ValueError("native bootstrap HTTP barrier readback mismatch")
        external = dict(maintenance_quiet=True, http_write_barrier_active=actual["active"],
            warehouse_timer_held=True, warehouse_lock_held=False, supplier_acceptance_writer_held=True,
            fbs_collector_continues=bool(entry.wb_fbs_orders.enabled), local_observations=evidence)
        try:
            yield external, evidence
        finally:
            after = units()
            if after != before:
                raise ValueError("guest service/timer state changed during bootstrap; barrier retained")
            evidence["units_after"] = after
            evidence["http_barrier_release"] = abort_barrier_acquire(runtime, window_id=window,
                plan_fingerprint=fingerprint, actor="local-lab", reason="Local initial migration ended; no automatic setting was changed",
                restore_readback=dict(status="restored", exact_prior_state_restored=True,
                    units_before=before, units_after=after, service_or_timer_mutations=0))
            assert not barrier_status(runtime)["active"]


def _historical_inputs(runtime):
    """Import explicit synthetic legacy sources, before any derived cutover.

    The native engine deliberately discovers a June receipt of >=100k units
    and average 111.181389 +/- .01. This historical price reference is not the
    current physical balance: the incoming FF opening document is seven units.
    No expense posting, book, current projection or completed queue is seeded.
    """
    stamp = "2026-06-24T08:00:00Z"
    with sqlite3.connect(runtime.db_path) as conn:
        # A frozen incoming source snapshot is not a newly edited supplier
        # command: importing it must not create a preparation intent that would
        # recalculate its historical cost from deliberately absent payment data.
        _insert(conn, "supplier_shipments", dict(
            shipment_id="lab-historical-june", created_at=stamp, updated_at=stamp,
            shipment_date="2026-06-01", actual_shipment_date="2026-06-10",
            actual_ff_acceptance_date="2026-06-23", order_status="accepted_ff", expenses_complete=1,
            invoice_no="SYNTHETIC-HISTORICAL-PRICE", invoice_date="2026-06-01", currency="CNY",
            product_qty_total=100000, product_amount_total=1000000, extras_amount_total=0,
            invoice_amount_total=1000000, match_status="all_matched", warnings_json="[]", errors_json="[]"))
        _insert(conn, "supplier_shipment_lines", dict(
            line_id="lab-historical-line", shipment_id="lab-historical-june", line_type="product", sort_order=1,
            internal_sku="LAB-101", internal_nm_id=NM, internal_name="Synthetic historical price reference",
            qty=100000, unit_price=10, amount=1000000, currency="CNY", match_status="matched",
            manual_override=0, raw_json='{"synthetic_historical_input":true}'))
        _insert(conn, "supplier_ff_cost_layers", dict(
            layer_id="lab-frozen-ff-cost-v1", supplier_shipment_id="lab-historical-june",
            status="confirmed", accepted_ff_date="2026-06-23", calculated_at=stamp,
            effective_cny_rate=10, invoice_amount_total_cny=1000000, invoice_extras_total_cny=0,
            product_qty_total=100000, common_expense_pool_rub=1118000,
            common_expense_per_unit_rub=11.18, weighted_avg_ff_unit_cost_rub=111.18,
            reconciliation_status="ok", reconciliation_delta_rub=0,
            inputs_hash="synthetic-frozen-historical-ff-cost-v1", version=1, is_current=1,
            source_status_json='{"synthetic_historical_input":true}', component_status_json="{}"))
        _insert(conn, "supplier_ff_cost_layer_lines", dict(
            layer_line_id="lab-frozen-ff-cost-line-v1", layer_id="lab-frozen-ff-cost-v1",
            supplier_shipment_id="lab-historical-june", supplier_line_id="lab-historical-line",
            nm_id=NM, sku="LAB-101", display_name="Synthetic historical price reference", qty=100000,
            invoice_unit_price_cny=10, sku_purchase_cost_rub=100,
            allocated_common_expenses_per_unit_rub=11.18, sku_ff_unit_cost_rub=111.18,
            line_total_cost_rub=11118000, allocation_method="qty_based_common_pool", source_status="confirmed"))
        _insert(conn, "wb_supply_cost_layers", dict(
            wb_supply_cost_layer_id="lab-frozen-wb-cost-v1", wb_supply_id="lab-historical-direct",
            cache_key="lab-historical-direct", nm_id=NM, accepted_qty=12, qty_denominator=12,
            supply_date="2026-06-24", accepted_date="2026-06-24", supplier_ff_cost_layer_id="lab-frozen-ff-cost-v1",
            supplier_ff_cost_layer_line_id="lab-frozen-ff-cost-line-v1", sku_ff_unit_cost_rub=111.18,
            transit_cost_status="direct_zero_confirmed", transit_amount_total=0, transit_per_unit_rub=0,
            ff_services_amount_total=0, ff_services_per_unit_rub=0, ff_storage_amount_total=0,
            ff_storage_per_unit_rub=0, pre_acceptance_unit_cost_rub=111.18,
            wb_acceptance_amount_total=0, wb_acceptance_per_accepted_unit_rub=0,
            our_wb_unit_cost_rub=111.18, source_status="confirmed", component_status_json='{"synthetic_historical_input":true}',
            missing_reason="", calculated_at=stamp, inputs_hash="synthetic-frozen-historical-wb-cost-v1",
            version=1, is_current=1))
    runtime.create_ff_stock_operation(operation_id="lab-incoming-opening-v1", operation_type="opening_balance",
        source_type="manual", source_key="lab:incoming-opening-v1", source_object_id="lab-incoming-opening-v1",
        source_object_label="Synthetic incoming balance, 7 units", created_at="2026-07-01T08:00:00Z",
        business_effective_date="2026-07-01", created_by="local-lab", diagnostics={"synthetic_input": True},
        lines=[dict(nm_id=NM, sku="LAB-101", quantity_delta=QUANTITY)])
    # Own the historical supplier receipt using its real native identity. A
    # header without this receipt would legitimately create 100k new units at
    # the first reconciliation. The native canonical replay collapses its first
    # receipt to July 1, so its consumption belongs to that SAME opening day,
    # before the independent seven-unit incoming opening.
    from packages.application.ff_stock_ledger import FfStockLedgerBlock
    FfStockLedgerBlock(runtime=runtime, timestamp_factory=lambda: "2026-06-23T08:00:00Z").record_supplier_acceptance(
        runtime.load_supplier_shipment("lab-historical-june"))
    runtime.create_ff_stock_operation(operation_id="lab-historical-consumption-v1", operation_type="manual_writeoff",
        source_type="manual", source_key="lab:historical-consumption-v1", source_object_id="lab-historical-june",
        source_object_label="Synthetic historical reference lot consumed before incoming opening",
        created_at="2026-07-01T07:00:00Z", business_effective_date="2026-07-01", created_by="local-lab",
        diagnostics={"synthetic_historical_input": True}, lines=[dict(nm_id=NM, sku="LAB-101", quantity_delta=-100000)])
    # The production-shaped facility migration requires an explicit pending
    # receipt exclusion. This unpaid, unaccepted unit contributes no opening.
    with sqlite3.connect(runtime.db_path) as conn:
        _insert(conn, "supplier_shipments", dict(shipment_id="lab-excluded-pending", created_at="2026-11-24T07:00:00Z",
            updated_at="2026-11-24T07:00:00Z", shipment_date="2026-11-24", actual_shipment_date="2026-11-24",
            order_status="in_transit", invoice_no="SYNTHETIC-PENDING-EXCLUDED", invoice_date="2026-11-24",
            currency="CNY", product_qty_total=1, product_amount_total=10, extras_amount_total=0,
            invoice_amount_total=10, match_status="all_matched", warnings_json="[]", errors_json="[]"))
        _insert(conn, "supplier_shipment_lines", dict(line_id="lab-excluded-line", shipment_id="lab-excluded-pending",
            line_type="product", sort_order=1, internal_sku="LAB-101", internal_nm_id=NM,
            internal_name="Synthetic excluded pending unit", qty=1, unit_price=10,
            amount=10, currency="CNY", match_status="matched", manual_override=0, raw_json='{"synthetic_input":true}'))


def prepare(entry):
    """Prepare an empty isolated copy before the operator enters the expense."""
    runtime = entry.runtime
    if not (runtime.runtime_dir / "wbc-local-scenario.json").is_file():
        raise ValueError("local-lab marker required")
    with closing(_readonly(runtime.db_path)) as conn:
        empty_directory = conn.execute(f"SELECT COUNT(*) FROM {PREFIX}ff_facilities").fetchone()[0] == 0
    if empty_directory:
        seed_facility(runtime, entry.activated_at_factory())
    with closing(_readonly(runtime.db_path)) as conn:
        for name in ("ff_pool_overhead_confirmations", "warehouse_functional_cutovers", "ff_pool_cutover_manifests", "ff_pool_balances"):
            if conn.execute(f"SELECT COUNT(*) FROM {PREFIX}{name}").fetchone()[0]:
                raise ValueError("bootstrap requires empty copied lab runtime: " + name)
        facilities = [dict(row) for row in conn.execute(f"SELECT * FROM {PREFIX}ff_facilities WHERE active=1")]
        if len(facilities) != 1:
            raise ValueError("one synthetic active facility required")
    facility = facilities[0]
    _historical_inputs(runtime)
    from packages.application.fulfillment_services import _ensure_schema as ensure_fulfillment_schema
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_fulfillment_schema(conn)
    from packages.application.canonical_cost_engine import CanonicalCostEngine
    engine = CanonicalCostEngine(runtime=runtime, timestamp_factory=entry.activated_at_factory)
    baseline = engine.build_baseline_plan()
    engine.materialize_baseline_plan(baseline)
    # Historical incoming state is a declared fixture input. Functional cost
    # replay consumes only its physical_quantity; costs are recalculated below.
    with sqlite3.connect(runtime.db_path) as conn:
        day = date(2026, 7, 1)
        while day < entry.now_factory().date():
            _insert(conn, "canonical_cost_daily_state", dict(
                as_of_date=day.isoformat(), nm_id=NM, stage="WB", physical_quantity=str(WB_QUANTITY),
                paid_equivalent_quantity=str(WB_QUANTITY), recognized_capital_rub=str(UNIT_COST * WB_QUANTITY),
                paid_capital_rub=str(UNIT_COST * WB_QUANTITY), cost_covered_quantity=str(WB_QUANTITY),
                confirmed_quantity=str(WB_QUANTITY), recognized_unit_cost_rub=str(UNIT_COST),
                paid_unit_cost_rub=str(UNIT_COST), source_quality="synthetic_frozen_historical_input",
                underaccepted_quantity="0", underaccepted_recognized_capital_rub="0", underaccepted_paid_capital_rub="0",
                diagnostics_json='{"synthetic_historical_input":true}', calculated_at="2026-11-24T07:00:00Z",
                fingerprint="synthetic-historical-wb-12-" + day.isoformat()))
            day += timedelta(days=1)
    functional = entry.warehouse_functional_block
    cutover = functional.build_cutover_plan()
    functional.apply_plan(cutover, confirm_fingerprint=cutover["plan_fingerprint"],
                          backup_dir=runtime.runtime_dir / "lab-bootstrap-backups")
    registry = entry.wb_fbs_warehouse_registry
    first_registry = registry.collect()
    binding = registry.preview_binding(dict(request_id="lab-bootstrap-binding", seller_warehouse_id=7001,
                                            facility_id=facility["facility_id"]), actor="local-lab")
    registry.confirm_binding("lab-bootstrap-binding", preview_fingerprint=binding["preview_fingerprint"], actor="local-lab")
    stock = registry.collect()
    orders = entry.wb_fbs_orders.collect()
    from packages.application.ff_pool_cutover_production import FfPoolCutoverProductionMutation
    env = runtime.runtime_dir / "lab-bootstrap.env"
    env.write_text("WB_FBS_COLLECTOR_ENABLED=true\n")
    # The hash describes this synthetic fixture, not a claimed deployed release.
    fixture_sha = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()
    # The cutover's native compound boundary normalizes to whole seconds. Its
    # source.as_of must have the same precision on capture and revalidation.
    pool_stamp = lambda: datetime.fromisoformat(entry.activated_at_factory().replace("Z", "+00:00")).isoformat(timespec="seconds").replace("+00:00", "Z")
    pool = FfPoolCutoverProductionMutation(runtime_dir=runtime.runtime_dir, env_file=env,
        deployed_sha=fixture_sha, timestamp_factory=pool_stamp)
    gate = pool.build_gate_plan(excluded_shipment_ids=["lab-excluded-pending"], opening_facility_id=facility["facility_id"])
    if not gate["apply_allowed"]:
        raise ValueError("synthetic facility cutover blocked: " + json.dumps(gate["blockers"], ensure_ascii=False))
    with _bootstrap_barrier(entry, gate["fingerprint"]) as (external, admission):
        result = pool.apply(gate, fingerprint=gate["fingerprint"], approval_reference="local-lab-synthetic-bootstrap",
            actor="local-lab", backup_dir=runtime.runtime_dir / "lab-bootstrap-backups", external_barrier_evidence=external)
    sync = functional.build_sync_plan()
    functional.apply_plan(sync, confirm_fingerprint=sync["plan_fingerprint"])
    from packages.application import fbs_accounting_runtime as accounting
    book, version = accounting.prepare(runtime.runtime_dir, now=entry.now_factory(), opening=True)
    initial = book["state"]["periods"][entry.now_factory().date().isoformat()]["rows"][f"{facility['facility_id']}:{NM}"]
    assert Decimal(initial["quantity"]) == QUANTITY, initial
    assert Decimal(initial["capital_rub"]) == UNIT_COST * QUANTITY, initial
    assert Decimal(initial["wac_rub"]) == UNIT_COST, initial
    accounting.save(runtime.runtime_dir, book, expected=version, operation_id="lab-bootstrap-book-v1")
    wanted = expected()
    expected_cells = {f"SKU:101|{metric}": value
        for metric, value in (("own_capital_FF_qty", str(QUANTITY)),
                             ("own_capital_FF_unit_cost_rub", wanted["fbs_wac_rub"]),
                             ("own_capital_FF_capital_rub", wanted["fbs_capital_rub"]))}
    initial_cells = {f"SKU:101|{metric}": value
        for metric, value in (("own_capital_FF_qty", str(QUANTITY)),
                             ("own_capital_FF_unit_cost_rub", str(UNIT_COST)),
                             ("own_capital_FF_capital_rub", str(UNIT_COST * QUANTITY)))}
    total_metrics = {"own_capital_FF_qty": "total_own_capital_FF_qty",
                     "own_capital_FF_unit_cost_rub": "avg_own_capital_FF_unit_cost_rub",
                     "own_capital_FF_capital_rub": "total_own_capital_FF_capital_rub"}
    for cells in (expected_cells, initial_cells):
        cells.update({"TOTAL|" + total: cells["SKU:101|" + sku] for sku, total in total_metrics.items()})
    expected_cells["SKU:101|our_wb_unit_cost_rub"] = wanted["shared_wac_rub"]
    initial_cells["SKU:101|our_wb_unit_cost_rub"] = str(UNIT_COST)
    report = dict(contract="local_full_warehouse_bootstrap_v1", facility=facility, facility_id=facility["facility_id"],
        expected_cells=expected_cells, initial_cells=initial_cells, initial_accounting_row=initial,
        opening="FBS 7 units at 111.18 RUB (778.26 RUB); WB 12 units at 111.18 RUB (1334.16 RUB).",
        expected=expected(), canonical_baseline=baseline["fingerprint"], functional_cutover=cutover["plan_fingerprint"],
        facility_cutover=result, bootstrap_admission=admission, first_registry=first_registry, stock=stock, orders=orders,
        historical_inputs="Explicit synthetic frozen historical cost layers; not production data.")
    (runtime.runtime_dir / "lab-full-warehouse-bootstrap.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def verify(entry, operation_id):
    """Read native persisted postings, accounting and Ready; no writes."""
    from packages.application import fbs_accounting_runtime as accounting
    from packages.application.operator_ff_overhead import read_acceptance
    runtime = entry.runtime
    wanted = expected()
    acceptance = read_acceptance(runtime.db_path, operation_id)
    assert acceptance and acceptance["request_id"] == operation_id, acceptance
    assert Decimal(acceptance["summary"]["amount_rub"]) == EXPENSE, acceptance
    assert acceptance["summary"]["scope"] == "FBS", acceptance
    book, _version = accounting.load(runtime.runtime_dir)
    day = entry.now_factory().date().isoformat()
    with closing(_readonly(runtime.db_path)) as conn:
        documents = [dict(row) for row in conn.execute(f"SELECT * FROM {PREFIX}ff_pool_documents WHERE request_id=?", (operation_id,))]
        assert len(documents) == 1, documents
        document = documents[0]
        assert document["document_kind"] == "pool_overhead" and document["business_date"] == day, document
        domain = json.loads(document["posted_manifest_json"])["domain"]
        assert domain["scope"] == "FBS" and Decimal(domain["amount_rub"]) == EXPENSE, domain
        lines = [dict(row) for row in conn.execute(f"SELECT * FROM {PREFIX}ff_pool_movement_lines WHERE operation_id=?", (document["operation_id"],))]
        assert len(lines) == 1, lines
        assert lines[0]["nm_id"] == NM and lines[0]["pool"] == "FBS", lines
        assert Decimal(str(lines[0]["quantity_delta"])) == 0, lines
        assert Decimal(str(lines[0]["capital_delta_rub"])) == EXPENSE, lines
        expenses = [dict(row) for row in conn.execute(f"SELECT * FROM {PREFIX}ff_pool_document_expense_lines WHERE document_id=?", (document["document_id"],))]
        assert len(expenses) == 1 and Decimal(expenses[0]["amount_rub"]) == EXPENSE, expenses
        pools = [dict(row) for row in conn.execute(f"SELECT * FROM {PREFIX}ff_pool_balances WHERE pool='FBS' AND nm_id=?", (NM,))]
        assert len(pools) == 1 and pools[0]["quantity"] == QUANTITY, pools
        assert Decimal(pools[0]["capital_rub"]) == Decimal(wanted["fbs_capital_rub"]), pools
        confirmation_count = conn.execute(f"SELECT COUNT(*) FROM {PREFIX}ff_pool_overhead_confirmations WHERE request_id=?", (operation_id,)).fetchone()[0]
        assert confirmation_count == 1
    rows = book["state"]["periods"][day]["rows"]
    row = next(value for value in rows.values() if int(value["nm_id"]) == NM)
    assert Decimal(row["quantity"]) == QUANTITY, row
    assert Decimal(row["expense_capital_rub"]) == EXPENSE, row
    assert Decimal(row["cost_mass_quantity"]) == QUANTITY, row
    assert len(row["documents"]) == 1, row
    allocation = row["documents"][0]
    assert allocation["document_id"] == document["document_id"] and allocation["kind"] == "expense", allocation
    assert Decimal(str(allocation["quantity"])) == 0 and Decimal(allocation["capital_rub"]) == EXPENSE, allocation
    # Native snapshot accounting multiplies a repeating 50-digit WAC by qty.
    # Its derived capital may differ by ~1e-47 RUB; documentary money and the
    # native ledger above stay exact. Require both a much-smaller-than-kopek
    # numeric tolerance and exact kopek-rounded equality for derived values.
    def derived(actual, expected_value):
        with localcontext() as context:
            context.prec = 80
            actual, expected_value = Decimal(actual), Decimal(expected_value)
            assert actual.is_finite() and abs(actual - expected_value) <= DERIVED_TOLERANCE, (actual, expected_value)
            assert actual.quantize(Decimal(".01"), rounding=ROUND_HALF_UP) == expected_value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    derived(row["capital_rub"], wanted["fbs_capital_rub"])
    derived(row["wac_rub"], wanted["fbs_wac_rub"])
    shared = book["shared_days"][day]["rows"][str(NM)]
    assert Decimal(shared["quantity"]) == wanted["shared_quantity"], shared
    derived(shared["capital_rub"], wanted["shared_capital_rub"])
    derived(shared["unit_cost_rub"], wanted["shared_wac_rub"])
    wb = next(component for component in shared["components"] if component["component"] == "WB")
    assert Decimal(wb["quantity"]) == WB_QUANTITY and Decimal(wb["capital_rub"]) == Decimal(wanted["wb_capital_rub"]), wb
    assert acceptance["state"] == "completed", acceptance
    receipt = accounting.current_publication_receipt(runtime, now=entry.now_factory())
    assert receipt, "native current Ready publication proof missing"
    pick = lambda value, keys: {key: value[key] for key in keys}
    return dict(expected=wanted, request_id=operation_id, acceptance_state=acceptance["state"],
        confirmation_count=confirmation_count, document_count=len(documents), posting_count=len(lines),
        document=pick(document, ("document_id", "operation_id", "document_kind", "request_id", "business_date", "posted_manifest_sha256")),
        movements=[pick(line, ("operation_id", "line_no", "facility_id", "pool", "nm_id", "quantity_delta", "capital_delta_rub")) for line in lines],
        pools=[pick(pool, ("facility_id", "pool", "nm_id", "quantity", "capital_rub", "wac_rub")) for pool in pools],
        accounting_row=pick(row, ("facility_id", "nm_id", "quantity", "capital_rub", "wac_rub", "expense_capital_rub", "documents")),
        shared_row=pick(shared, ("nm_id", "quantity", "capital_rub", "unit_cost_rub", "status")),
        ready_publication_verified=True)
