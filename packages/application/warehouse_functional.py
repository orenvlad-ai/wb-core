"""Canonical six-warehouse state, functional cutover and bounded WB replay.

The immutable ``warehouse_opening_v1`` tables remain audit evidence.  This
module owns the only active warehouse read model.  A version is calculated
from a coherent source capture and published atomically; failed attempts never
replace the last good version.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Mapping

from packages.business_time import business_date_from_timestamp
from packages.application.calculation_parameters import CalculationParametersBlock
from packages.application.canonical_cost_engine import CanonicalCostEngine
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.stocks_block import StocksBlock
from packages.application.warehouse_archival_estimate import (
    QUALITY as BUSINESS_APPROVED_ARCHIVAL_ESTIMATE_QUALITY,
    ensure_archival_estimate_schema,
    overlay_opening_cost_rows,
)
from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
from packages.application.warehouse_stocks import (
    INACTIVE_SUPPLIER_STATUSES,
    WB_FINAL_ACCEPTED_STATUS_ID,
    WB_POST_SHIPMENT_GATE_STATUS_IDS,
    WarehouseOpeningSnapshotError,
    WarehouseStocksBlock,
    _is_doprinato,
    _normalized_wb_record,
    _validated_wb_goods,
)


FUNCTIONAL_CUTOVER_ID = "warehouse_functional_cutover_v1"
CONTRACT_NAME = "sheet_vitrina_v1_warehouse_functional"
CONTRACT_VERSION = "v2"
ZERO = Decimal("0")
ONE = Decimal("1")

STAGE_PRODUCTION = "production"
STAGE_CHINA_TO_FF = "china_to_ff"
STAGE_FF = "ff"
STAGE_FF_TO_WB = "ff_to_wb"
STAGE_WB = "wb"
STAGE_DISCREPANCY = "wb_acceptance_discrepancy"
STAGES = (
    STAGE_PRODUCTION,
    STAGE_CHINA_TO_FF,
    STAGE_FF,
    STAGE_FF_TO_WB,
    STAGE_WB,
    STAGE_DISCREPANCY,
)

STAGE_NAMES = {
    STAGE_PRODUCTION: "На производстве",
    STAGE_CHINA_TO_FF: "Китай → FF",
    STAGE_FF: "Склад FF",
    STAGE_FF_TO_WB: "FF → WB",
    STAGE_WB: "Склад WB",
    STAGE_DISCREPANCY: "Расхождения приёмки WB",
}

BANK_FEE_CATEGORIES = {
    "bank_transfer_fee",
    "currency_control_fee",
    "currency_control_vat",
    "other_bank_fee",
}
LOGISTICS_DOCUMENT_TYPE = "logistics_invoice"
CUSTOMS_DOCUMENT_TYPE = "customs_declaration"
CUSTOMS_BY_QUANTITY = {"customs_fee_1010"}
CUSTOMS_BY_VALUE = {"import_duty_2010", "import_vat_5010"}
SUPPLIER_COST_AFFECTING_DOCUMENT_TYPES = (
    "supplier_cny_payment",
    "bank_transfer_application",
    "bank_fee",
    "bank_fee_statement",
    LOGISTICS_DOCUMENT_TYPE,
    CUSTOMS_DOCUMENT_TYPE,
)

WAREHOUSE_QUALITY_PRESENTATIONS: Mapping[str, tuple[str, str]] = {
    "provisional": (
        "Предварительная себестоимость",
        "Расчёт использует подтверждённые факты, но часть будущих расходов ещё не закрыта.",
    ),
    "confirmed_payments_provisional_expenses": (
        "Платежи подтверждены, часть расходов предварительная",
        "Фактические платежи и банковские комиссии учтены; не все расходы этапа подтверждены.",
    ),
    "certified": (
        "Все расходы учтены / Подтверждено документами",
        "Платежи и расходы этапа подтверждены первичными документами.",
    ),
    "source_changed_provisional": (
        "Предварительная себестоимость — источники изменились",
        "После сертификации изменился учитываемый документ; зелёный статус снят до успешного targeted replay.",
    ),
    "primary_documents": (
        "Подтверждено первичными документами",
        "Количество и капитал рассчитаны по связанным платежам, invoice и документам расходов.",
    ),
    "mixed": (
        "Смешанные источники",
        "В строке объединены партии с разным уровнем документального подтверждения.",
    ),
    "moving_weighted_average": (
        "Скользящая средневзвешенная",
        "Стоимость рассчитана последовательным replay канонического append-only FF ledger.",
    ),
    "periodic_snapshot_wac": (
        "Средневзвешенная по историческому снимку",
        "Стоимость относится к точной бизнес-дате и учитывает подтверждённые приходы к этой дате.",
    ),
    "periodic_snapshot_wac_provisional": (
        "Текущая средневзвешенная WB",
        "Остаток взят из официального snapshot WB; себестоимость сохраняет статус исходных слоёв.",
    ),
    "periodic_snapshot_wac_closed": (
        "Закрытая средневзвешенная WB",
        "Количество и стоимость зафиксированы для закрытой канонической бизнес-даты.",
    ),
    "zero_quantity_without_cost_basis": (
        "Нулевой остаток без базы себестоимости",
        "На эту дату SKU присутствует в точном снимке с нулевым количеством; стоимость не подменяется нулём.",
    ),
    "same_purchase_price": (
        "По подтверждённой одинаковой закупочной цене",
        "SKU сопоставлен с подтверждённой строкой той же закупочной цены в базовой приёмке.",
    ),
    "interpolation": (
        "Интерполяция базовой стоимости",
        "Оценка зафиксирована при cutover между подтверждёнными ценовыми точками.",
    ),
    "extrapolation": (
        "Экстраполяция базовой стоимости",
        "Оценка зафиксирована при cutover по ближайшей подтверждённой ценовой точке.",
    ),
    "fallback_average": (
        "Оценка по зафиксированной средней",
        "Применена зафиксированная при cutover оценка с явным provenance.",
    ),
    "business_approved_archival_estimate": (
        "Утверждённая архивная оценка",
        "Владелец утвердил ретроспективную себестоимость для точного архивного manifest; новые фактические поступления заменят оценочную базу обычной WAC.",
    ),
    "direct_24_06": (
        "Зафиксировано по приёмке 24.06",
        "SKU-стоимость взята из подтверждённой базовой приёмки FF и заморожена для replay.",
    ),
    "direct_confirmed_downstream": (
        "Подтверждённые расходы FF → WB",
        "Расходы поставки от FF до WB связаны напрямую с этим SKU.",
    ),
    "confirmed_weighted_downstream_unit_cost": (
        "Средневзвешенные подтверждённые расходы FF → WB",
        "Несколько подтверждённых поставок объединены пропорционально их количеству.",
    ),
    "supply_specific_downstream_cost": (
        "Стоимость конкретной поставки FF → WB",
        "Входящая WAC и расходы этапа привязаны к конкретной поставке WB.",
    ),
    "proportional_wac_outbound": (
        "Списание по текущей средневзвешенной",
        "Капитал выбытия рассчитан пропорционально количеству по WAC на момент операции.",
    ),
    "current_wac_adjustment": (
        "Корректировка текущей средневзвешенной",
        "Версионированная корректировка меняет только производную стоимость, сохраняя первичный аудит.",
    ),
    "pooled_final_acceptance_discrepancy": (
        "Расхождение финальной приёмки",
        "Количество и капитал изолированы в отдельном пуле до доказанной доприёмки или корректировки.",
    ),
    "empty": (
        "Остаток отсутствует",
        "На выбранном срезе количество и товарный капитал равны нулю.",
    ),
}


class WarehouseFunctionalError(WarehouseOpeningSnapshotError):
    """Fail-closed functional warehouse invariant error."""


def _current_snapshot_effective_date(*, captured_at: str, snapshot_date: Any) -> str:
    """Bind a current-state version only to its canonical capture business date."""

    candidate = str(snapshot_date or "")[:10]
    capture_business_date = business_date_from_timestamp(captured_at)
    if candidate != capture_business_date:
        raise WarehouseFunctionalError(
            "current warehouse source capture cannot be published with a stale WB snapshot date: "
            f"captured_business_date={capture_business_date}, snapshot_date={candidate or 'missing'}"
        )
    return candidate


def enqueue_warehouse_targeted_recalculation(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    stable_source_id: str,
    source_revision: str,
    effective_date: str,
    affected_nm_ids: Iterable[int],
    requested_at: str | None = None,
) -> dict[str, Any]:
    """Coalesce one source revision for the next bounded atomic publication."""

    stable_id = str(stable_source_id or "").strip()
    revision = str(source_revision or "").strip()
    business_date = str(effective_date or "")[:10]
    nm_ids = sorted({int(item) for item in affected_nm_ids if int(item) > 0})
    if not stable_id or not revision or len(business_date) != 10:
        raise ValueError("stable source id, revision and effective date are required")
    now = requested_at or _now()
    queue_id = _stable_id(
        "whrq",
        {"stable_source_id": stable_id, "source_revision": revision},
    )
    runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
    with _connect(runtime.db_path) as conn:
        ensure_warehouse_functional_schema(conn)
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_targeted_recalc_queue(
                   queue_id,stable_source_id,source_revision,effective_date,affected_nm_ids_json,
                   status,requested_at,started_at,finished_at,error
               ) VALUES(?,?,?,?,?,'queued',?,NULL,NULL,NULL)
               ON CONFLICT(stable_source_id,source_revision) DO UPDATE SET
                   affected_nm_ids_json=excluded.affected_nm_ids_json,
                   effective_date=MIN(effective_date,excluded.effective_date),
                   requested_at=excluded.requested_at,
                   status=CASE WHEN status='complete' THEN status ELSE 'queued' END,
                   error=NULL""",
            (queue_id, stable_id, revision, business_date, _json(nm_ids), now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE queue_id=?",
            (queue_id,),
        ).fetchone()
    return dict(row) if row else {"queue_id": queue_id, "status": "queued"}


def load_supplier_flow_cost_state(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    shipment_id: str,
) -> dict[str, Any]:
    """Return shipment-specific active production/China stage costs."""

    with _connect(runtime.db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "sheet_vitrina_v1_warehouse_functional_active" not in tables:
            return {}
        active = conn.execute(
            "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
        ).fetchone()
        if active is None:
            return {}
        stored_rows = conn.execute(
            """SELECT warehouse_key,quantity,capital_rub,certified,quality,provenance_json
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND warehouse_key IN (?,?)""",
            (active["version_id"], STAGE_PRODUCTION, STAGE_CHINA_TO_FF),
        ).fetchall()
        shipment_row = (
            conn.execute(
                """
                SELECT actual_shipment_date,actual_ff_acceptance_date,order_status
                FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?
                """,
                (str(shipment_id or ""),),
            ).fetchone()
            if "sheet_vitrina_v1_supplier_shipments" in tables
            else None
        )
        document_ids = (
            [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT document_id
                    FROM sheet_vitrina_v1_supplier_financial_documents
                    WHERE supplier_order_id=?
                    """,
                    (str(shipment_id or ""),),
                ).fetchall()
            ]
            if "sheet_vitrina_v1_supplier_financial_documents" in tables
            else []
        )
        cny_document_ids = (
            [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT document_id
                    FROM sheet_vitrina_v1_cny_documents
                    WHERE source_order_id=?
                    """,
                    (str(shipment_id or ""),),
                ).fetchall()
            ]
            if "sheet_vitrina_v1_cny_documents" in tables
            else []
        )
        stable_ids = [
            f"supplier_shipment:{shipment_id}",
            *[
                f"supplier_financial_document:{document_id}"
                for document_id in document_ids
            ],
            *[
                f"cny_document:{document_id}"
                for document_id in cny_document_ids
            ],
        ]
        queue_rows = []
        if (
            stable_ids
            and "sheet_vitrina_v1_warehouse_targeted_recalc_queue" in tables
        ):
            placeholders = ",".join("?" for _ in stable_ids)
            queue_rows = conn.execute(
                f"""
                SELECT status,error,requested_at,started_at
                FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue
                WHERE stable_source_id IN ({placeholders})
                  AND status IN ('queued','running','error')
                ORDER BY requested_at DESC
                """,
                stable_ids,
            ).fetchall()
    selected_rows: list[dict[str, Any]] = []
    for stored_row in stored_rows:
        provenance = _loads(stored_row["provenance_json"], {})
        selected_sources = [
            dict(source)
            for source in provenance.get("source_records") or []
            if str(source.get("shipment_id") or "") == str(shipment_id or "")
        ]
        if not selected_sources:
            continue
        selected_quality_codes = sorted(
            {
                str(source.get("quality") or "").strip()
                for source in selected_sources
                if str(source.get("quality") or "").strip()
            }
        )
        selected_quality = (
            selected_quality_codes[0]
            if len(selected_quality_codes) == 1
            else (
                "mixed:" + ",".join(selected_quality_codes)
                if selected_quality_codes
                else str(stored_row["quality"] or "")
            )
        )
        selected_rows.append(
            {
                "warehouse_key": stored_row["warehouse_key"],
                "quantity": stored_row["quantity"],
                "capital_rub": stored_row["capital_rub"],
                # Aggregate balance flags describe every party of this SKU.
                # The supplier registry asks for one shipment, whose frozen
                # certification/quality is carried by its own flow records.
                "certified": all(
                    bool(source.get("expenses_complete_certification"))
                    for source in selected_sources
                ),
                "quality": selected_quality,
                # Revalidate only the shipment requested by the supplier
                # registry.  A balance can combine several shipments of one
                # SKU, but another shipment must not decide this card's tone.
                "provenance": {**provenance, "source_records": selected_sources},
            }
        )
    rows = _revalidate_balance_certifications(
        runtime=runtime,
        balances=selected_rows,
        active_version_id=str(active["version_id"]),
    )
    totals: defaultdict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "quantity": ZERO,
            "capital": ZERO,
            "certified": True,
            "quality": set(),
            "revalidation_failed": False,
        }
    )
    for row in rows:
        for source in dict(row.get("provenance") or {}).get("source_records") or []:
            stage = str(row["warehouse_key"])
            totals[stage]["quantity"] += _decimal(source.get("flow_quantity"))
            totals[stage]["capital"] += _decimal(source.get("flow_capital_rub"))
            totals[stage]["certified"] = totals[stage]["certified"] and bool(row["certified"])
            totals[stage]["quality"].add(str(row["quality"] or ""))
            if row.get("certification_revalidation_failed"):
                totals[stage]["quality"].add("source_changed_provisional")
                totals[stage]["revalidation_failed"] = True
    result: dict[str, Any] = {}
    for stage, item in totals.items():
        qty = _decimal(item["quantity"])
        capital = _decimal(item["capital"])
        result[stage] = {
            "quantity": _text(qty),
            "capital_rub": _text(capital),
            "average_unit_cost_rub": _text(capital / qty) if qty > ZERO else None,
            "certified": bool(item["certified"]),
            "quality": sorted(item["quality"]),
            "status": (
                "stale"
                if bool(item["revalidation_failed"])
                else "certified"
                if bool(item["certified"])
                else "provisional"
            ),
            "blocker": (
                "Active functional version не совпадает с текущими source/calculation fingerprints."
                if bool(item["revalidation_failed"])
                else ""
            ),
        }
    shipment_state = dict(shipment_row) if shipment_row is not None else {}
    actual_shipment_date = str(
        shipment_state.get("actual_shipment_date") or ""
    ).strip()
    actual_ff_acceptance_date = str(
        shipment_state.get("actual_ff_acceptance_date") or ""
    ).strip()
    stage_applicable = (
        {
            STAGE_PRODUCTION: not actual_shipment_date,
            STAGE_CHINA_TO_FF: bool(actual_shipment_date)
            and not actual_ff_acceptance_date,
        }
        if shipment_row is not None
        else {STAGE_PRODUCTION: True, STAGE_CHINA_TO_FF: True}
    )
    queue_state = queue_rows[0] if queue_rows else None
    queue_status = str(queue_state["status"]) if queue_state is not None else ""
    for stage in (STAGE_PRODUCTION, STAGE_CHINA_TO_FF):
        if not stage_applicable[stage]:
            result[stage] = {
                "status": "not_applicable",
                "average_unit_cost_rub": None,
                "certified": False,
                "reason": "Не применяется: поставка уже покинула эту стадию.",
            }
            continue
        if queue_status == "error":
            result[stage] = {
                "status": "error",
                "average_unit_cost_rub": None,
                "certified": False,
                "blocker": str(
                    queue_state["error"]
                    or "Targeted replay завершился ошибкой."
                ),
            }
        elif queue_status in {"running", "queued"}:
            result[stage] = {
                "status": queue_status,
                "average_unit_cost_rub": None,
                "certified": False,
                "reason": "Ожидает пересчёта current active functional version.",
            }
        elif stage not in result:
            result[stage] = {
                "status": "unavailable",
                "average_unit_cost_rub": None,
                "certified": False,
                "blocker": "В current active functional version нет себестоимости этой стадии.",
            }
    return result


def load_supplier_line_cost_breakdown(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    shipment_id: str,
) -> dict[str, Any]:
    """Expose the exact canonical supplier allocation used by warehouse replay.

    The read is deliberately calculated from primary sources, then compared
    with the fingerprints stored by the active immutable warehouse version.
    A closed shipment is green only while both fingerprints still match.
    """

    selected_id = str(shipment_id or "").strip()
    if not selected_id:
        return {}
    with _connect(runtime.db_path) as conn:
        ensure_warehouse_functional_schema(conn)
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        required = {
            "sheet_vitrina_v1_supplier_shipments",
            "sheet_vitrina_v1_supplier_shipment_lines",
            "sheet_vitrina_v1_cny_ledger_operations",
            "sheet_vitrina_v1_supplier_financial_documents",
            "sheet_vitrina_v1_supplier_financial_expense_lines",
        }
        if not required.issubset(tables):
            return {}
        # Keep every primary-source row and the active certification pointer
        # in one SQLite read snapshot.  Without an explicit transaction a
        # concurrent document commit could make the explanation combine
        # revisions that the canonical warehouse calculation never observed
        # together.
        conn.execute("BEGIN")
        sources = {
            "shipments": [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
                (selected_id,),
            ).fetchall()],
            "shipment_lines": [dict(row) for row in conn.execute(
                """SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines
                   WHERE shipment_id=? ORDER BY sort_order,line_id""",
                (selected_id,),
            ).fetchall()],
            "cny_operations": [dict(row) for row in conn.execute(
                """SELECT * FROM sheet_vitrina_v1_cny_ledger_operations
                   WHERE source_order_id=? ORDER BY sequence_key,operation_id""",
                (selected_id,),
            ).fetchall()],
            "financial_documents": [dict(row) for row in conn.execute(
                """SELECT * FROM sheet_vitrina_v1_supplier_financial_documents
                   WHERE supplier_order_id=? ORDER BY document_date,document_id""",
                (selected_id,),
            ).fetchall()],
            "financial_expense_lines": [dict(row) for row in conn.execute(
                """SELECT * FROM sheet_vitrina_v1_supplier_financial_expense_lines
                   WHERE supplier_order_id=? ORDER BY financial_document_id,sort_order,line_id""",
                (selected_id,),
            ).fetchall()],
            "cny_documents": (
                [dict(row) for row in conn.execute(
                    """SELECT * FROM sheet_vitrina_v1_cny_documents
                       WHERE source_order_id=? ORDER BY operation_date,operation_datetime,document_id""",
                    (selected_id,),
                ).fetchall()]
                if "sheet_vitrina_v1_cny_documents" in tables
                else []
            ),
        }
        active = conn.execute(
            "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
        ).fetchone()
        active_version_id = str(active["version_id"]) if active is not None else ""
        active_fingerprints: tuple[str, str] | None = None
        if active is not None:
            certified_row = _effective_supplier_cost_state(
                conn,
                version_id=str(active["version_id"]),
                shipment_id=selected_id,
            )
            if certified_row is not None:
                active_fingerprints = (
                    str(certified_row["source_fingerprint"]),
                    str(certified_row["calculation_fingerprint"]),
                )
    allocation = _supplier_cost_allocations(sources).get(selected_id)
    if allocation is None:
        return {}
    return _supplier_allocation_with_certification(
        allocation,
        active_version_id=active_version_id,
        active_fingerprints=active_fingerprints,
    )


def load_supplier_cost_summary_fields(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    shipment_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Batch compact registry summaries in one coherent read snapshot.

    Full per-line/document proof stays on the shipment detail route.  The
    unpaginated collection receives only the aggregate fields it renders, so
    it never opens one connection or embeds one proof graph per row.
    """

    selected_ids = sorted({str(value or "").strip() for value in shipment_ids if str(value or "").strip()})
    if not selected_ids:
        return {}
    selected = set(selected_ids)
    with _connect(runtime.db_path) as conn:
        ensure_warehouse_functional_schema(conn)
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        required = {
            "sheet_vitrina_v1_supplier_shipments",
            "sheet_vitrina_v1_supplier_shipment_lines",
            "sheet_vitrina_v1_cny_ledger_operations",
            "sheet_vitrina_v1_supplier_financial_documents",
            "sheet_vitrina_v1_supplier_financial_expense_lines",
        }
        if not required.issubset(tables):
            return {}
        conn.execute("BEGIN")
        sources = {
            "shipments": [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_supplier_shipments ORDER BY shipment_id"
                ).fetchall()
                if str(row["shipment_id"] or "") in selected
            ],
            "shipment_lines": [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines
                       ORDER BY shipment_id,sort_order,line_id"""
                ).fetchall()
                if str(row["shipment_id"] or "") in selected
            ],
            "cny_operations": [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM sheet_vitrina_v1_cny_ledger_operations
                       ORDER BY sequence_key,operation_id"""
                ).fetchall()
                if str(row["source_order_id"] or "") in selected
            ],
            "financial_documents": [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM sheet_vitrina_v1_supplier_financial_documents
                       ORDER BY supplier_order_id,document_date,document_id"""
                ).fetchall()
                if str(row["supplier_order_id"] or "") in selected
            ],
            "financial_expense_lines": [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM sheet_vitrina_v1_supplier_financial_expense_lines
                       ORDER BY supplier_order_id,financial_document_id,sort_order,line_id"""
                ).fetchall()
                if str(row["supplier_order_id"] or "") in selected
            ],
            "cny_documents": (
                [
                    dict(row)
                    for row in conn.execute(
                        """SELECT * FROM sheet_vitrina_v1_cny_documents
                           ORDER BY source_order_id,operation_date,operation_datetime,document_id"""
                    ).fetchall()
                    if str(row["source_order_id"] or "") in selected
                ]
                if "sheet_vitrina_v1_cny_documents" in tables
                else []
            ),
        }
        active = conn.execute(
            "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
        ).fetchone()
        active_version_id = str(active["version_id"]) if active is not None else ""
        active_fingerprints_by_shipment: dict[str, tuple[str, str]] = {}
        if active is not None:
            active_fingerprints_by_shipment = {
                shipment_id: (
                    str(row["source_fingerprint"]),
                    str(row["calculation_fingerprint"]),
                )
                for shipment_id, row in _effective_supplier_cost_states(
                    conn,
                    version_id=active_version_id,
                    shipment_ids=selected_ids,
                ).items()
            }
    allocations = _supplier_cost_allocations(sources)
    shipments_by_id = {
        str(item.get("shipment_id") or ""): item for item in sources["shipments"]
    }
    lines_by_id: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    documents_by_id: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    expense_lines_by_id: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in sources["shipment_lines"]:
        lines_by_id[str(item.get("shipment_id") or "")].append(item)
    for item in sources["financial_documents"]:
        documents_by_id[str(item.get("supplier_order_id") or "")].append(item)
    for item in sources["financial_expense_lines"]:
        expense_lines_by_id[str(item.get("supplier_order_id") or "")].append(item)

    # Keep legacy approximate columns available without returning to per-row
    # runtime queries.  Exact fields below always come from the canonical proof
    # and deliberately overwrite any legacy exact-looking values.
    from packages.application.supplier_financial_documents import build_financial_summary
    from packages.application.supplier_expense_allocation import (
        project_supplier_order_expense_allocation,
    )

    result: dict[str, dict[str, Any]] = {}
    for shipment_id in selected_ids:
        header = shipments_by_id.get(shipment_id, {})
        financial_summary = build_financial_summary(
            documents_by_id.get(shipment_id, []),
            expense_lines_by_id.get(shipment_id, []),
            shipment={"header": header, "lines": lines_by_id.get(shipment_id, [])},
        )
        per_unit = (
            dict(financial_summary.get("per_unit") or {})
            if isinstance(financial_summary.get("per_unit"), Mapping)
            else {}
        )
        allocation = allocations.get(shipment_id)
        canonical_allocation = (
            _supplier_allocation_with_certification(
                allocation,
                active_version_id=active_version_id,
                active_fingerprints=active_fingerprints_by_shipment.get(shipment_id),
            )
            if allocation is not None
            else {}
        )
        canonical_summary = supplier_cost_summary_fields(canonical_allocation)
        result[shipment_id] = {
            "approx_invoice_cost_rub": per_unit.get("approx_invoice_cost_rub"),
            "approx_landed_cost_per_unit_rub": per_unit.get(
                "approx_landed_cost_per_unit_rub"
            ),
            **canonical_summary,
            "expense_allocation": project_supplier_order_expense_allocation(
                canonical_allocation
            ),
        }
    return result


def _supplier_allocation_with_certification(
    allocation: Mapping[str, Any],
    *,
    active_version_id: str,
    active_fingerprints: tuple[str, str] | None,
) -> dict[str, Any]:
    result = json.loads(json.dumps(dict(allocation), ensure_ascii=False, default=str))
    matches_active = bool(
        active_fingerprints
        and active_fingerprints
        == (
            str(result.get("source_fingerprint") or ""),
            str(result.get("calculation_fingerprint") or ""),
        )
    )
    expenses_complete = bool(result.get("expenses_complete"))
    certified = expenses_complete and matches_active
    result["certification"] = {
        "certified": certified,
        "source_fingerprint_matches": matches_active,
        "source_fingerprint": result.get("source_fingerprint"),
        "calculation_fingerprint": result.get("calculation_fingerprint"),
        "certified_source_fingerprint": active_fingerprints[0] if active_fingerprints else None,
        "certified_calculation_fingerprint": active_fingerprints[1] if active_fingerprints else None,
        "active_version_id": active_version_id or None,
        "status_code": "certified" if certified else "provisional",
        "status_label_ru": (
            "Все расходы учтены"
            if certified
            else "Предварительная себестоимость — не все расходы учтены"
        ),
        "reason_ru": (
            "Актуальные source/calculation fingerprints совпадают с сертифицированной версией."
            if certified
            else (
                "Поставка отмечена закрытой, но актуальный расчёт ещё не совпал с сертифицированной версией."
                if expenses_complete
                else "Не все расходы поставки подтверждены."
            )
        ),
    }
    return result


def _effective_supplier_cost_state(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    shipment_id: str,
) -> sqlite3.Row | None:
    """Read one version's base certification plus append-only recovery overlay.

    Functional versions remain immutable.  A recovery replay therefore never
    inserts into ``warehouse_supplier_cost_states`` retroactively; it appends a
    version-scoped correction with an audited ``supersedes`` identity instead.
    A rollback is an append-only tombstone, so the previous state becomes
    effective again without deleting audit evidence.
    """

    return _effective_supplier_cost_states(
        conn,
        version_id=version_id,
        shipment_ids=[shipment_id],
    ).get(shipment_id)


def _effective_supplier_cost_states(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    shipment_ids: Iterable[str],
) -> dict[str, sqlite3.Row]:
    """Batch effective certification reads in at most two SQLite statements."""

    selected = sorted(
        {
            str(shipment_id or "").strip()
            for shipment_id in shipment_ids
            if str(shipment_id or "").strip()
        }
    )
    if not selected:
        return {}
    placeholders = ",".join("?" for _ in selected)
    result: dict[str, sqlite3.Row] = {}
    corrections = conn.execute(
        f"""SELECT correction.shipment_id,correction.source_fingerprint,
                   correction.calculation_fingerprint,correction.expenses_complete,
                   correction.calculation_available,correction.state_fingerprint,
                   correction.replay_id,replay.sequence_no
            FROM sheet_vitrina_v1_warehouse_supplier_cost_state_corrections correction
            JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replays replay
              ON replay.replay_id=correction.replay_id
            LEFT JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks rollback
              ON rollback.replay_id=replay.replay_id
            WHERE correction.version_id=?
              AND correction.shipment_id IN ({placeholders})
              AND rollback.replay_id IS NULL
              AND correction.expenses_complete=1
              AND correction.calculation_available=1
            ORDER BY correction.shipment_id,replay.sequence_no DESC""",
        (version_id, *selected),
    ).fetchall()
    for row in corrections:
        result.setdefault(str(row["shipment_id"]), row)
    remaining = [shipment_id for shipment_id in selected if shipment_id not in result]
    if not remaining:
        return result
    base_placeholders = ",".join("?" for _ in remaining)
    for row in conn.execute(
        f"""SELECT shipment_id,source_fingerprint,calculation_fingerprint,
                   expenses_complete,calculation_available,NULL AS state_fingerprint,
                   NULL AS replay_id,NULL AS sequence_no
            FROM sheet_vitrina_v1_warehouse_supplier_cost_states
            WHERE version_id=? AND shipment_id IN ({base_placeholders})
              AND expenses_complete=1 AND calculation_available=1""",
        (version_id, *remaining),
    ).fetchall():
        result[str(row["shipment_id"])] = row
    return result


def _revalidate_balance_certifications(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    balances: Iterable[Mapping[str, Any]],
    active_version_id: str,
) -> list[dict[str, Any]]:
    """Fail closed when mutable supplier evidence no longer matches the active version.

    Balance rows intentionally retain the certification bit frozen in their
    immutable functional version.  Presentation cannot rely on that bit alone:
    a source mutation clears current shipment certification before targeted
    replay publishes a replacement version.  Reuse the canonical supplier
    allocation/fingerprint projection and require it to refer to the exact
    version whose balances are being rendered.
    """

    rows = [dict(item) for item in balances]
    shipment_ids_by_index = [
        _shipment_ids_from_provenance(item.get("provenance")) for item in rows
    ]
    shipment_ids = sorted(
        {
            shipment_id
            for item_ids in shipment_ids_by_index
            for shipment_id in item_ids
        }
    )
    certification_by_shipment: dict[str, bool] = {}
    for shipment_id in shipment_ids:
        proof = load_supplier_line_cost_breakdown(
            runtime=runtime,
            shipment_id=shipment_id,
        )
        certification = dict(proof.get("certification") or {})
        certification_by_shipment[shipment_id] = bool(
            certification.get("certified")
            and active_version_id
            and str(certification.get("active_version_id") or "")
            == active_version_id
        )

    result: list[dict[str, Any]] = []
    for item, item_shipment_ids in zip(rows, shipment_ids_by_index, strict=True):
        persisted_certified = bool(item.get("certified"))
        current_sources_certified = bool(
            not item_shipment_ids
            or all(
                certification_by_shipment.get(shipment_id, False)
                for shipment_id in item_shipment_ids
            )
        )
        item["persisted_certified"] = persisted_certified
        item["certified"] = persisted_certified and current_sources_certified
        item["certification_revalidated"] = bool(item["certified"])
        item["certification_source_shipments"] = item_shipment_ids
        item["certification_revalidation_failed"] = bool(
            persisted_certified and not current_sources_certified
        )
        result.append(item)
    return result


def _shipment_ids_from_provenance(value: Any) -> list[str]:
    """Collect supplier identities carried through nested warehouse provenance."""

    result: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            shipment_id = str(item.get("shipment_id") or "").strip()
            if shipment_id:
                result.add(shipment_id)
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)
    return sorted(result)


@dataclass(frozen=True)
class CostSeed:
    nm_id: int
    ff_unit_cost: Decimal
    wb_unit_cost: Decimal
    quality: str
    provenance: Mapping[str, Any]


@dataclass(frozen=True)
class WarehouseLine:
    warehouse_key: str
    nm_id: int
    quantity: Decimal
    capital: Decimal
    cost_covered_quantity: Decimal
    quality: str
    provenance: Mapping[str, Any]
    certified: bool = False
    wb_quantity: Decimal = ZERO
    wb_in_way_to_client: Decimal = ZERO
    wb_in_way_from_client: Decimal = ZERO

    @property
    def wac(self) -> Decimal | None:
        return self.capital / self.quantity if self.quantity > ZERO else None


def moving_weighted_average(
    *, quantity: Any, capital: Any, inbound_quantity: Any, inbound_capital: Any
) -> tuple[Decimal, Decimal, Decimal | None]:
    current_qty = _decimal(quantity)
    current_capital = _decimal(capital)
    inbound_qty = _decimal(inbound_quantity)
    inbound_cap = _decimal(inbound_capital)
    if min(current_qty, current_capital, inbound_qty, inbound_cap) < ZERO:
        raise ValueError("WAC inputs cannot be negative")
    result_qty = current_qty + inbound_qty
    result_capital = current_capital + inbound_cap
    return (
        result_qty,
        result_capital,
        result_capital / result_qty if result_qty > ZERO else None,
    )


def roll_periodic_wac(
    *, quantity: Any, capital: Any, quantity_delta: Any, capital_delta: Any
) -> tuple[Decimal, Decimal, Decimal | None]:
    """Roll a periodic cost pool, permitting a bounded source correction.

    Accepted-quantity corrections are signed deltas.  They may reverse a
    previously posted inbound layer, but they may never make the cost pool
    negative.  Ordinary warehouse movements continue to use the stricter
    positive-only ``moving_weighted_average`` helper.
    """

    current_qty = _decimal(quantity)
    current_capital = _decimal(capital)
    delta_qty = _decimal(quantity_delta)
    delta_capital = _decimal(capital_delta)
    if current_qty < ZERO or current_capital < ZERO:
        raise ValueError("periodic WAC opening inputs cannot be negative")
    result_qty = current_qty + delta_qty
    result_capital = current_capital + delta_capital
    if result_qty < ZERO or result_capital < ZERO:
        raise WarehouseFunctionalError("accepted source correction would make the WB cost pool negative")
    return (
        result_qty,
        result_capital,
        result_capital / result_qty if result_qty > ZERO else None,
    )


def accepted_quantity_delta(*, packed: Any, accepted: Any, previously_posted: Any) -> Decimal:
    current = min(max(_decimal(packed), ZERO), max(_decimal(accepted), ZERO))
    posted = _decimal(previously_posted)
    if posted < ZERO:
        raise ValueError("previously posted accepted quantity cannot be negative")
    return current - posted


def accepted_capital_delta(
    *, packed: Any, accepted: Any, unit_cost: Any, previously_posted_capital: Any
) -> Decimal:
    """Return the exact full-layer delta for late cost evidence or quantity correction."""

    current = min(max(_decimal(packed), ZERO), max(_decimal(accepted), ZERO))
    cost = _decimal(unit_cost)
    posted_capital = _decimal(previously_posted_capital)
    if min(cost, posted_capital) < ZERO:
        raise ValueError("accepted cost state cannot be negative")
    return current * cost - posted_capital


def allocate_capital(
    lines: Iterable[Mapping[str, Any]], *, total_capital: Any, method: str
) -> dict[int, Decimal]:
    """Allocate without intermediate rounding and conserve the exact total."""

    capital = _decimal(total_capital)
    if capital < ZERO:
        raise ValueError("capital cannot be negative")
    normalized: list[tuple[int, Decimal]] = []
    for raw in lines:
        nm_id = int(raw.get("nm_id") or raw.get("internal_nm_id") or 0)
        quantity = _decimal(raw.get("quantity") or raw.get("qty"))
        invoice_value = _decimal(raw.get("invoice_value") or raw.get("amount"))
        weight = quantity if method == "quantity" else invoice_value
        if nm_id <= 0 or quantity <= ZERO or weight <= ZERO:
            raise ValueError("allocation lines require positive nm_id, quantity and weight")
        normalized.append((nm_id, weight))
    denominator = sum((weight for _, weight in normalized), ZERO)
    if not normalized or denominator <= ZERO:
        raise ValueError("allocation denominator must be positive")
    result: defaultdict[int, Decimal] = defaultdict(Decimal)
    remainder = capital
    for index, (nm_id, weight) in enumerate(normalized):
        allocated = remainder if index == len(normalized) - 1 else capital * weight / denominator
        remainder -= allocated
        result[nm_id] += allocated
    return dict(result)


def _allocate_supplier_component(
    lines: list[Mapping[str, Any]],
    *,
    total_capital: Decimal,
    method: str,
) -> dict[str, Decimal]:
    """Allocate one document component to invoice lines with exact conservation."""

    if total_capital < ZERO:
        raise ValueError("supplier component capital cannot be negative")
    weighted: list[tuple[str, Decimal]] = []
    for row in lines:
        line_id = str(row.get("line_id") or "").strip()
        weight = (
            _decimal(row.get("quantity"))
            if method == "quantity"
            else _decimal(row.get("invoice_value_cny"))
        )
        if not line_id or weight <= ZERO:
            raise ValueError("supplier allocation requires a stable line id and positive weight")
        weighted.append((line_id, weight))
    denominator = sum((weight for _, weight in weighted), ZERO)
    if not weighted or denominator <= ZERO:
        raise ValueError("supplier allocation denominator must be positive")
    result: dict[str, Decimal] = {}
    remainder = total_capital
    for index, (line_id, weight) in enumerate(weighted):
        value = remainder if index == len(weighted) - 1 else total_capital * weight / denominator
        remainder -= value
        result[line_id] = value
    if not _decimal_conserves(sum(result.values(), ZERO), total_capital):
        raise WarehouseFunctionalError("supplier document allocation does not conserve capital")
    return result


def _decimal_conserves(left: Any, right: Any) -> bool:
    """Compare at the documented sub-kopeck Decimal audit precision."""

    return abs(_decimal(left) - _decimal(right)) <= Decimal("0.000000000000000001")


def _dedupe_supplier_control_reasons(
    values: Iterable[Mapping[str, Any]],
    *,
    limit: int = 8,
) -> list[dict[str, str]]:
    """Keep canonical document diagnostics stable, compact and non-duplicated."""

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        code = str(value.get("code") or "incomplete_cost_allocation").strip()
        reason_ru = str(value.get("reason_ru") or value.get("reason") or code).strip()
        key = (code, reason_ru)
        if not reason_ru or key in seen:
            continue
        seen.add(key)
        result.append({"code": code, "reason_ru": reason_ru})
        if len(result) >= limit:
            break
    return result


def _supplier_cost_allocations(sources: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the canonical per-line supplier cost proof used by warehouse replay and UI."""

    shipments = {str(row["shipment_id"]): dict(row) for row in sources.get("shipments") or []}
    product_lines: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    invalid_product_lines: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for raw in sources.get("shipment_lines") or []:
        row = dict(raw)
        if str(row.get("line_type") or "") != "product":
            continue
        shipment_id = str(row.get("shipment_id") or "")
        line_id = str(row.get("line_id") or "").strip()
        nm_id = int(row.get("internal_nm_id") or 0)
        quantity = _decimal(row.get("qty"))
        invoice_value = _line_value(row)
        reasons: list[str] = []
        if not line_id:
            reasons.append("нет устойчивого ID строки")
        if nm_id <= 0:
            reasons.append("нет однозначного сопоставления с nmID")
        if quantity <= ZERO:
            reasons.append("количество не положительное")
        if invoice_value <= ZERO:
            reasons.append("стоимость строки invoice не положительная")
        if reasons:
            invalid_product_lines[shipment_id].append(
                {
                    "line_id": line_id or "без ID",
                    "reason_ru": ", ".join(reasons),
                }
            )
            continue
        product_lines[shipment_id].append(
            {
                **row,
                "line_id": line_id,
                "nm_id": nm_id,
                "quantity": quantity,
                "unit_price_cny": _decimal(row.get("unit_price")),
                "invoice_value_cny": invoice_value,
            }
        )
    cny_documents = {
        str(row.get("document_id") or ""): dict(row)
        for row in sources.get("cny_documents") or []
    }
    operations: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    cost_operation_candidates: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in sources.get("cny_operations") or []:
        source_document = cny_documents.get(str(raw.get("source_document_id") or ""))
        if str(raw.get("operation_type") or "") in {"supplier_payment_out", "transfer_fee"}:
            cost_operation_candidates[str(raw.get("source_order_id") or "")].append(dict(raw))
        if _counted_cny_operation(raw, document=source_document):
            operations[str(raw.get("source_order_id") or "")].append(dict(raw))
    financial_documents = {
        str(row.get("document_id") or ""): dict(row)
        for row in sources.get("financial_documents") or []
    }
    expenses: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in sources.get("financial_expense_lines") or []:
        expenses[str(raw.get("supplier_order_id") or "")].append(dict(raw))
    for rows in product_lines.values():
        rows.sort(
            key=lambda item: (
                int(item.get("sort_order") or 0),
                str(item.get("line_id") or ""),
            )
        )
    for rows in operations.values():
        rows.sort(
            key=lambda item: (
                str(item.get("sequence_key") or ""),
                str(item.get("operation_id") or ""),
            )
        )
    for rows in cost_operation_candidates.values():
        rows.sort(
            key=lambda item: (
                str(item.get("sequence_key") or ""),
                str(item.get("operation_id") or ""),
            )
        )
    for rows in expenses.values():
        rows.sort(
            key=lambda item: (
                str(item.get("financial_document_id") or ""),
                int(item.get("sort_order") or 0),
                str(item.get("line_id") or ""),
            )
        )

    result: dict[str, dict[str, Any]] = {}
    for shipment_id, shipment in shipments.items():
        if str(shipment.get("order_status") or "").lower() in INACTIVE_SUPPLIER_STATUSES:
            continue
        rows = product_lines.get(shipment_id, [])
        payment_rows = [
            row for row in operations.get(shipment_id, [])
            if str(row.get("operation_type") or "") == "supplier_payment_out"
        ]
        fee_rows = [
            row for row in operations.get(shipment_id, [])
            if str(row.get("operation_type") or "") == "transfer_fee"
        ]
        stage = (
            STAGE_CHINA_TO_FF
            if str(shipment.get("actual_shipment_date") or "")[:10]
            else STAGE_PRODUCTION
        )
        blockers: list[dict[str, str]] = []
        if str(shipment.get("match_status") or "").strip() == "checksum_error":
            blockers.append(
                {
                    "code": "invoice_checksum_mismatch",
                    "reason_ru": (
                        "Итог строк invoice не совпадает с заявленным итогом документа. "
                        "Себестоимость не публикуется до исправления контрольной суммы."
                    ),
                }
            )
        for invalid_line in invalid_product_lines.get(shipment_id, []):
            blockers.append(
                {
                    "code": "invalid_invoice_product_line",
                    "reason_ru": (
                        f"Товарная строка {invalid_line['line_id']} не включена в расчёт: "
                        f"{invalid_line['reason_ru']}. Себестоимость поставки не публикуется."
                    ),
                }
            )
        if not rows:
            blockers.append(
                {
                    "code": "invoice_product_lines_unavailable",
                    "reason_ru": "Нет однозначно сопоставленных товарных строк invoice.",
                }
            )
        if not payment_rows:
            blockers.append(
                {
                    "code": "confirmed_supplier_payment_unavailable",
                    "reason_ru": "Нет подтверждённого платежа поставщику, связанного с invoice.",
                }
            )
        else:
            for payment in payment_rows:
                if abs(_decimal(payment.get("rub_value_delta"))) > ZERO:
                    continue
                blockers.append(
                    {
                        "code": "supplier_payment_rub_valuation_unavailable",
                        "reason_ru": (
                            "Подтверждённый платёж "
                            f"{str(payment.get('operation_id') or 'без ID')} не имеет положительной "
                            "фактической RUB-стоимости использованных юаней. Нулевая себестоимость "
                            "не предполагается."
                        ),
                    }
                )
        for fee in fee_rows:
            if abs(_decimal(fee.get("rub_value_delta"))) > ZERO:
                continue
            blockers.append(
                {
                    "code": "bank_fee_rub_valuation_unavailable",
                    "reason_ru": (
                        "Банковская комиссия "
                        f"{str(fee.get('operation_id') or 'без ID')} не имеет положительной "
                        "RUB-стоимости и не может быть молча исключена из себестоимости."
                    ),
                }
            )
        line_components: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        component_controls: list[dict[str, Any]] = []
        seen_source_components: set[str] = set()
        document_evidence: dict[tuple[str, str], dict[str, Any]] = {}

        def ensure_cost_document(
            *,
            document_id: str,
            document_type: str,
            source_document_id: str = "",
        ) -> dict[str, Any]:
            normalized_id = str(document_id or "").strip()
            normalized_type = str(document_type or "").strip()
            key = (normalized_id, normalized_type)
            evidence = document_evidence.setdefault(
                key,
                {
                    "document_id": normalized_id,
                    "document_type": normalized_type,
                    "source_document_ids": set(),
                    "components": [],
                    "incomplete_reasons": [],
                },
            )
            for value in (normalized_id, str(source_document_id or "").strip()):
                if value:
                    evidence["source_document_ids"].add(value)
            return evidence

        def add_document_reason(
            evidence: dict[str, Any],
            *,
            code: str,
            reason_ru: str,
        ) -> None:
            evidence["incomplete_reasons"].append(
                {"code": code, "reason_ru": reason_ru}
            )

        def add_document_component_candidate(
            evidence: dict[str, Any],
            *,
            source_component_id: str,
            amount_rub: Decimal | None,
            incomplete_reasons: Iterable[Mapping[str, str]] = (),
        ) -> None:
            evidence["components"].append(
                {
                    "source_component_id": source_component_id,
                    "amount_rub": amount_rub,
                    "incomplete_reasons": [dict(item) for item in incomplete_reasons],
                }
            )

        def cny_cost_document_evidence(
            source_document: Mapping[str, Any],
            operation: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            source_document_id = str(
                source_document.get("document_id")
                or (operation or {}).get("source_document_id")
                or ""
            ).strip()
            linked_financial_id = str(
                source_document.get("linked_financial_document_id") or ""
            ).strip()
            linked_financial = financial_documents.get(linked_financial_id, {})
            operation_type = str((operation or {}).get("operation_type") or "")
            inferred_type = (
                "supplier_cny_payment"
                if operation_type == "supplier_payment_out"
                else "bank_fee"
            )
            if linked_financial_id and linked_financial:
                return ensure_cost_document(
                    document_id=linked_financial_id,
                    document_type=str(linked_financial.get("document_type") or inferred_type),
                    source_document_id=source_document_id,
                )
            return ensure_cost_document(
                document_id=source_document_id,
                document_type=str(source_document.get("document_type") or inferred_type),
                source_document_id=source_document_id,
            )

        def add_component(
            *,
            source_component_id: str,
            component_key: str,
            label_ru: str,
            amount_rub: Decimal,
            method: str,
            document: Mapping[str, Any],
            source_amount_cny: Decimal | None = None,
            effective_rate: Decimal | None = None,
        ) -> None:
            if amount_rub <= ZERO or not rows:
                return
            if source_component_id in seen_source_components:
                raise WarehouseFunctionalError(
                    f"supplier component counted twice: {source_component_id}"
                )
            seen_source_components.add(source_component_id)
            allocated = _allocate_supplier_component(
                rows,
                total_capital=amount_rub,
                method=method,
            )
            document_payload = {
                "document_id": str(document.get("document_id") or document.get("source_document_id") or ""),
                "number": str(document.get("document_number") or document.get("number") or ""),
                "date": str(document.get("document_date") or document.get("operation_date") or "")[:10],
                "status": str(document.get("parse_status") or document.get("status") or ""),
                "type": str(document.get("document_type") or document.get("operation_type") or ""),
            }
            for row in rows:
                line_id = str(row["line_id"])
                line_components[line_id].append(
                    {
                        "source_component_id": source_component_id,
                        "component_key": component_key,
                        "label_ru": label_ru,
                        "amount_rub": _text(allocated[line_id]),
                        "source_amount_rub": _text(amount_rub),
                        "source_amount_cny": (
                            _text(source_amount_cny) if source_amount_cny is not None else None
                        ),
                        "effective_rate_rub_per_cny": (
                            _text(effective_rate) if effective_rate is not None else None
                        ),
                        "allocation_method": method,
                        "allocation_method_ru": (
                            "Пропорционально количеству"
                            if method == "quantity"
                            else "Пропорционально стоимости строк invoice"
                        ),
                        "document": document_payload,
                    }
                )
            component_controls.append(
                {
                    "source_component_id": source_component_id,
                    "component_key": component_key,
                    "source_amount_rub": _text(amount_rub),
                    "allocated_amount_rub": _text(sum(allocated.values(), ZERO)),
                    "conserved": _decimal_conserves(sum(allocated.values(), ZERO), amount_rub),
                }
            )

        for document in financial_documents.values():
            if str(document.get("supplier_order_id") or "") != shipment_id:
                continue
            document_type = str(document.get("document_type") or "")
            if document_type not in SUPPLIER_COST_AFFECTING_DOCUMENT_TYPES:
                continue
            evidence = ensure_cost_document(
                document_id=str(document.get("document_id") or ""),
                document_type=document_type,
                source_document_id=str(document.get("document_id") or ""),
            )
            if str(document.get("parse_status") or "") not in {"parsed", "confirmed"}:
                add_document_reason(
                    evidence,
                    code="financial_document_status_not_eligible",
                    reason_ru="Документ не имеет допустимого подтверждённого parse status.",
                )
        for document in cny_documents.values():
            if str(document.get("source_order_id") or "") != shipment_id:
                continue
            if str(document.get("document_type") or "") not in {
                "supplier_cny_payment",
                "bank_fee",
            }:
                continue
            evidence = cny_cost_document_evidence(document)
            if str(document.get("status") or "").strip().lower() != "posted":
                add_document_reason(
                    evidence,
                    code="cny_document_status_not_posted",
                    reason_ru="CNY-документ ещё не имеет допустимого canonical status.",
                )

        for operation in cost_operation_candidates.get(shipment_id, []):
            source_document = cny_documents.get(
                str(operation.get("source_document_id") or ""),
                {},
            )
            evidence = cny_cost_document_evidence(source_document, operation)
            candidate_reasons: list[dict[str, str]] = []
            raw_amount_rub = _optional_decimal(operation.get("rub_value_delta"))
            amount_rub = abs(raw_amount_rub) if raw_amount_rub is not None else None
            if not _counted_cny_operation(operation, document=source_document):
                candidate_reasons.append(
                    {
                        "code": "cny_operation_status_not_eligible",
                        "reason_ru": "Операция CNY ledger не имеет допустимого canonical status.",
                    }
                )
            if amount_rub is None or amount_rub <= ZERO:
                candidate_reasons.append(
                    {
                        "code": "cny_operation_rub_value_unavailable",
                        "reason_ru": "Операция CNY ledger не имеет положительной фактической RUB-стоимости.",
                    }
                )
            add_document_component_candidate(
                evidence,
                source_component_id="cny_operation:" + str(operation.get("operation_id") or ""),
                amount_rub=amount_rub,
                incomplete_reasons=candidate_reasons,
            )

        for expense in expenses.get(shipment_id, []):
            document = financial_documents.get(
                str(expense.get("financial_document_id") or ""),
                {},
            )
            document_type = str(document.get("document_type") or "")
            category = str(expense.get("category") or "")
            # CNY statement rows are provenance for the canonical CNY-ledger
            # fee operations already considered above.  Only independent,
            # positive RUB rows are additional eligible components; zero and
            # informational rows must not manufacture a false partial status.
            bank_fee_is_canonical_rub_line = bool(
                document_type == "bank_fee_statement"
                and category in BANK_FEE_CATEGORIES
                and str(expense.get("currency") or "").upper() == "RUB"
                and (_optional_decimal(expense.get("amount_rub")) or ZERO) > ZERO
            )
            is_cost_candidate = bool(
                (
                    bank_fee_is_canonical_rub_line
                )
                or document_type == LOGISTICS_DOCUMENT_TYPE
                or (
                    document_type == CUSTOMS_DOCUMENT_TYPE
                    and category in CUSTOMS_BY_QUANTITY | CUSTOMS_BY_VALUE
                )
            )
            if not is_cost_candidate:
                continue
            evidence = ensure_cost_document(
                document_id=str(document.get("document_id") or expense.get("financial_document_id") or ""),
                document_type=document_type,
                source_document_id=str(expense.get("financial_document_id") or ""),
            )
            candidate_reasons = []
            if not _validated_financial_expense(document=document, expense=expense):
                candidate_reasons.append(
                    {
                        "code": "financial_component_status_not_eligible",
                        "reason_ru": "Строка расхода или документ ещё не подтверждены для canonical allocation.",
                    }
                )
            if document_type in {LOGISTICS_DOCUMENT_TYPE, CUSTOMS_DOCUMENT_TYPE} and stage != STAGE_CHINA_TO_FF:
                candidate_reasons.append(
                    {
                        "code": "supplier_stage_not_eligible",
                        "reason_ru": "Расход станет распределяемым после подтверждения отгрузки из Китая.",
                    }
                )
            amount_rub = _optional_decimal(expense.get("amount_rub"))
            if amount_rub is None or amount_rub <= ZERO:
                candidate_reasons.append(
                    {
                        "code": "financial_component_amount_not_positive",
                        "reason_ru": "Строка расхода не имеет положительной суммы в RUB.",
                    }
                )
            add_document_component_candidate(
                evidence,
                source_component_id="expense_line:" + str(expense.get("line_id") or ""),
                amount_rub=amount_rub,
                incomplete_reasons=candidate_reasons,
            )

        for operation in payment_rows:
            rub = abs(_decimal(operation.get("rub_value_delta")))
            cny = abs(_decimal(operation.get("cny_delta")))
            document = cny_documents.get(str(operation.get("source_document_id") or ""), operation)
            add_component(
                source_component_id="cny_operation:" + str(operation.get("operation_id") or ""),
                component_key="supplier_payment",
                label_ru="Фактический платёж поставщику",
                amount_rub=rub,
                method="invoice_value",
                document={**operation, **document},
                source_amount_cny=cny,
                effective_rate=(rub / cny if cny > ZERO else None),
            )
        for operation in fee_rows:
            rub = abs(_decimal(operation.get("rub_value_delta")))
            cny = abs(_decimal(operation.get("cny_delta")))
            document = cny_documents.get(str(operation.get("source_document_id") or ""), operation)
            add_component(
                source_component_id="cny_operation:" + str(operation.get("operation_id") or ""),
                component_key="bank_fee",
                label_ru="Комиссия банка",
                amount_rub=rub,
                method="invoice_value",
                document={**operation, **document},
                source_amount_cny=cny,
                effective_rate=(rub / cny if cny > ZERO else None),
            )
        for expense in expenses.get(shipment_id, []):
            document = financial_documents.get(str(expense.get("financial_document_id") or ""), {})
            if not _validated_financial_expense(document=document, expense=expense):
                continue
            doc_type = str(document.get("document_type") or "")
            category = str(expense.get("category") or "")
            currency = str(expense.get("currency") or "").upper()
            amount = _decimal(expense.get("amount_rub"))
            component_key = ""
            label_ru = ""
            method = "invoice_value"
            if doc_type == "bank_fee_statement" and category in BANK_FEE_CATEGORIES and currency == "RUB":
                component_key, label_ru = "bank_fee", "Комиссия банка"
            elif stage == STAGE_CHINA_TO_FF and doc_type == LOGISTICS_DOCUMENT_TYPE:
                component_key, label_ru, method = "logistics", "Логистика Китай → FF", "quantity"
            elif stage == STAGE_CHINA_TO_FF and doc_type == CUSTOMS_DOCUMENT_TYPE:
                if category in CUSTOMS_BY_QUANTITY:
                    component_key, label_ru, method = "customs_fee_1010", "Таможенный сбор 1010", "quantity"
                elif category in CUSTOMS_BY_VALUE:
                    component_key = category
                    label_ru = "Пошлина 2010" if category == "import_duty_2010" else "Импортный НДС 5010"
            if not component_key or amount <= ZERO:
                continue
            add_component(
                source_component_id="expense_line:" + str(expense.get("line_id") or ""),
                component_key=component_key,
                label_ru=label_ru,
                amount_rub=amount,
                method=method,
                document=document,
            )

        allocated_by_source_component = {
            str(item.get("source_component_id") or ""): item
            for item in component_controls
        }
        document_controls: list[dict[str, Any]] = []
        for evidence in sorted(
            document_evidence.values(),
            key=lambda item: (
                str(item.get("document_type") or ""),
                str(item.get("document_id") or ""),
            ),
        ):
            candidates = list(evidence.get("components") or [])
            allocated = [
                allocated_by_source_component[str(item.get("source_component_id") or "")]
                for item in candidates
                if str(item.get("source_component_id") or "") in allocated_by_source_component
            ]
            reasons = [dict(item) for item in evidence.get("incomplete_reasons") or []]
            for candidate in candidates:
                reasons.extend(
                    dict(item) for item in candidate.get("incomplete_reasons") or []
                )
            if not candidates:
                reasons.append(
                    {
                        "code": "cost_components_not_recognized",
                        "reason_ru": "Документ не дал распознанных canonical cost-компонентов.",
                    }
                )
            if len(allocated) < len(candidates):
                reasons.append(
                    {
                        "code": "cost_components_not_fully_allocated",
                        "reason_ru": "Не все eligible cost-компоненты документа вошли в canonical allocation.",
                    }
                )
            if allocated and not all(bool(item.get("conserved")) for item in allocated):
                reasons.append(
                    {
                        "code": "document_allocation_not_conserved",
                        "reason_ru": "Контроль сохранения суммы документа не пройден.",
                    }
                )
            if allocated and blockers:
                reasons.append(
                    {
                        "code": "canonical_proof_has_blockers",
                        "reason_ru": "Canonical proof заказа имеет блокеры и не публикует распределённую себестоимость.",
                    }
                )
            reasons = _dedupe_supplier_control_reasons(reasons)
            candidate_amounts = [item.get("amount_rub") for item in candidates]
            eligible_amount = (
                sum((amount for amount in candidate_amounts if amount is not None), ZERO)
                if candidates and all(amount is not None for amount in candidate_amounts)
                else None
            )
            allocated_amount = sum(
                (_decimal(item.get("allocated_amount_rub")) for item in allocated),
                ZERO,
            )
            conserved = bool(
                candidates
                and len(allocated) == len(candidates)
                and all(bool(item.get("conserved")) for item in allocated)
                and not reasons
            )
            document_controls.append(
                {
                    "document_id": str(evidence.get("document_id") or ""),
                    "document_type": str(evidence.get("document_type") or ""),
                    "source_document_ids": sorted(evidence.get("source_document_ids") or []),
                    "cost_affecting": True,
                    "eligible_component_count": len(candidates),
                    "allocated_component_count": len(allocated),
                    "eligible_amount_rub": (
                        _text(eligible_amount) if eligible_amount is not None else None
                    ),
                    "allocated_amount_rub": _text(allocated_amount),
                    "conserved": conserved,
                    "incomplete_reasons": reasons,
                }
            )

        public_lines: list[dict[str, Any]] = []
        total_quantity = ZERO
        total_capital = ZERO
        for row in rows:
            line_id = str(row["line_id"])
            components = line_components.get(line_id, [])
            capital = sum((_decimal(item["amount_rub"]) for item in components), ZERO)
            quantity = _decimal(row["quantity"])
            total_quantity += quantity
            total_capital += capital
            public_lines.append(
                {
                    "line_id": line_id,
                    "nm_id": int(row["nm_id"]),
                    "sku": str(row.get("internal_sku") or ""),
                    "nomenclature_name": str(row.get("internal_name") or ""),
                    "barcode": str(row.get("barcode") or ""),
                    "quantity": _text(quantity),
                    "unit_price_cny": _text(row["unit_price_cny"]),
                    "invoice_value_cny": _text(row["invoice_value_cny"]),
                    "components": components,
                    "capital_rub": _text(capital) if not blockers else None,
                    "unit_cost_rub": _text(capital / quantity) if quantity > ZERO and not blockers else None,
                    "arithmetic": (
                        f"{_text(capital)} ₽ / {_text(quantity)} шт. = {_text(capital / quantity)} ₽/шт."
                        if quantity > ZERO and not blockers
                        else None
                    ),
                }
            )
        calculation_payload = {
            "shipment_id": shipment_id,
            "stage": stage,
            "invoice_currency": str(shipment.get("currency") or "").strip().upper(),
            "lines": public_lines,
            "component_controls": component_controls,
        }
        source_components: dict[str, dict[str, Any]] = {}
        for line in public_lines:
            for component in line.get("components") or []:
                source_components.setdefault(
                    str(component.get("source_component_id") or ""),
                    {
                        key: component.get(key)
                        for key in (
                            "source_component_id",
                            "component_key",
                            "source_amount_rub",
                            "source_amount_cny",
                            "effective_rate_rub_per_cny",
                            "allocation_method",
                            "document",
                        )
                    },
                )
        source_payload = {
            "shipment": {
                key: shipment.get(key)
                for key in (
                    "shipment_id",
                    "invoice_no",
                    "invoice_date",
                    "currency",
                    "actual_shipment_date",
                    "actual_ff_acceptance_date",
                    "order_status",
                    "expenses_complete",
                    "declared_invoice_total",
                    "invoice_amount_total",
                    "match_status",
                )
            },
            "lines": [
                {
                    key: row.get(key)
                    for key in (
                        "line_id",
                        "nm_id",
                        "quantity",
                        "unit_price_cny",
                        "invoice_value_cny",
                    )
                }
                for row in rows
            ],
            "recognized_components": [
                source_components[key] for key in sorted(source_components)
            ],
            "stage": stage,
        }
        source_fingerprint = "sha256:" + _hash(source_payload)
        calculation_fingerprint = "sha256:" + _hash(calculation_payload)
        result[shipment_id] = {
            "shipment_id": shipment_id,
            "invoice_no": str(shipment.get("invoice_no") or ""),
            "invoice_date": str(shipment.get("invoice_date") or "")[:10],
            "invoice_currency": str(shipment.get("currency") or "").strip().upper(),
            "first_payment_date": min(
                (
                    str(row.get("operation_date") or "")[:10]
                    for row in payment_rows
                    if str(row.get("operation_date") or "")
                ),
                default="",
            ),
            "actual_shipment_date": str(shipment.get("actual_shipment_date") or "")[:10],
            "actual_ff_acceptance_date": str(shipment.get("actual_ff_acceptance_date") or "")[:10],
            "stage": stage,
            "expenses_complete": bool(shipment.get("expenses_complete")),
            "source_fingerprint": source_fingerprint,
            "calculation_fingerprint": calculation_fingerprint,
            "quantity": _text(total_quantity),
            "capital_rub": _text(total_capital) if not blockers else None,
            "average_unit_cost_rub": (
                _text(total_capital / total_quantity)
                if total_quantity > ZERO and not blockers
                else None
            ),
            "lines": public_lines,
            "blockers": blockers,
            "component_controls": component_controls,
            "cost_affecting_document_types": list(
                SUPPLIER_COST_AFFECTING_DOCUMENT_TYPES
            ),
            "document_controls": document_controls,
            "controls": {
                "document_allocation_conserved": all(item["conserved"] for item in component_controls),
                "document_counted_once": len(seen_source_components) == len(component_controls),
                "line_components_equal_capital": all(
                    _decimal_conserves(
                        sum((_decimal(item["amount_rub"]) for item in line["components"]), ZERO),
                        line.get("capital_rub"),
                    )
                    for line in public_lines
                    if line.get("capital_rub") is not None
                ),
                "shipment_lines_equal_capital": (
                    _decimal_conserves(
                        sum((_decimal(line.get("capital_rub")) for line in public_lines), ZERO),
                        total_capital,
                    )
                ),
            },
        }
    return result


def supplier_cost_summary_fields(breakdown: Mapping[str, Any]) -> dict[str, Any]:
    """Project every exact-cost UI surface from one canonical proof."""

    canonical = dict(breakdown or {})
    blockers = list(canonical.get("blockers") or [])
    if not canonical or blockers:
        return {
            "exact_bank_fees_rub": None,
            "exact_currency_payment_cost_rub": None,
            "exact_landed_cost_total_rub": None,
            "exact_landed_cost_per_unit_rub": None,
            "exact_cost_status": "unavailable",
            "exact_cost_blockers": [
                str(item.get("reason_ru") or item.get("code") or "Недостающие данные")
                for item in blockers
            ] or ["Каноническая расшифровка себестоимости недоступна."],
            "exact_cost_warnings": [],
        }
    controls = list(canonical.get("component_controls") or [])
    certification = dict(canonical.get("certification") or {})
    return {
        "exact_bank_fees_rub": float(sum(
            (
                _decimal(item.get("source_amount_rub"))
                for item in controls
                if str(item.get("component_key") or "") == "bank_fee"
            ),
            ZERO,
        )),
        "exact_currency_payment_cost_rub": float(sum(
            (
                _decimal(item.get("source_amount_rub"))
                for item in controls
                if str(item.get("component_key") or "") == "supplier_payment"
            ),
            ZERO,
        )),
        "exact_landed_cost_total_rub": (
            float(_decimal(canonical.get("capital_rub")))
            if canonical.get("capital_rub") is not None
            else None
        ),
        "exact_landed_cost_per_unit_rub": (
            float(_decimal(canonical.get("average_unit_cost_rub")))
            if canonical.get("average_unit_cost_rub") is not None
            else None
        ),
        "exact_cost_status": (
            "certified" if certification.get("certified") else "provisional"
        ),
        "exact_cost_blockers": [],
        "exact_cost_warnings": [],
    }


def _supplier_cost_version_states(sources: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Freeze every active shipment's cost fingerprints in one functional version.

    This version-scoped state remains available after goods leave the supplier
    transit warehouses and their proof becomes nested inside the append-only FF
    ledger.  It therefore avoids inferring certification from the current
    warehouse location.
    """

    return [
        {
            "shipment_id": shipment_id,
            "source_fingerprint": str(allocation.get("source_fingerprint") or ""),
            "calculation_fingerprint": str(
                allocation.get("calculation_fingerprint") or ""
            ),
            "expenses_complete": bool(allocation.get("expenses_complete")),
            "calculation_available": not bool(allocation.get("blockers")),
        }
        for shipment_id, allocation in sorted(_supplier_cost_allocations(sources).items())
    ]


def reconcile_discrepancies(
    *,
    discrepancies: Iterable[Mapping[str, Any]],
    doprinato: Iterable[Mapping[str, Any]],
    audit: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pool positive discrepancies by SKU and quarantine unmatched doprinato."""

    pools: dict[int, dict[str, Any]] = {}
    for raw in discrepancies:
        nm_id = int(raw.get("nm_id") or 0)
        quantity = _decimal(raw.get("quantity"))
        capital = _decimal(raw.get("capital"))
        if nm_id <= 0 or quantity < ZERO or capital < ZERO:
            raise ValueError("invalid discrepancy receipt")
        if quantity == ZERO:
            continue
        pool = pools.setdefault(
            nm_id,
            {"nm_id": nm_id, "quantity": ZERO, "capital": ZERO, "receipts": [], "matches": []},
        )
        pool["quantity"] += quantity
        pool["capital"] += capital
        pool["receipts"].append(dict(raw))

    unmatched: list[dict[str, Any]] = []
    ordered = sorted(
        (dict(item) for item in doprinato),
        key=lambda item: (str(item.get("business_date") or ""), str(item.get("source_id") or "")),
    )
    for raw in ordered:
        nm_id = int(raw.get("nm_id") or 0)
        quantity = _decimal(raw.get("quantity"))
        if nm_id <= 0 or quantity < ZERO:
            raise ValueError("invalid doprinato")
        pool = pools.get(nm_id)
        available = _decimal((pool or {}).get("quantity"))
        matched = min(quantity, available)
        unmatched_qty = quantity - matched
        if matched > ZERO and pool is not None:
            wac = pool["capital"] / pool["quantity"]
            pool["quantity"] -= matched
            pool["capital"] -= matched * wac
            pool["matches"].append({**raw, "matched_quantity": _text(matched), "wac": _text(wac)})
        else:
            wac = None
        if unmatched_qty > ZERO:
            unmatched.append(
                {
                    **raw,
                    "quantity": _text(unmatched_qty),
                    "matched_quantity": _text(matched),
                    "reason": str(raw.get("reason") or "no_positive_discrepancy_for_sku"),
                }
            )
        if audit is not None:
            audit.append(
                {
                    **raw,
                    "matched_quantity": _text(matched),
                    "unmatched_quantity": _text(unmatched_qty),
                    "matched_wac_rub": _text(wac) if wac is not None else None,
                    "matched_capital_rub": _text(matched * wac) if wac is not None else "0",
                }
            )
    balances = [
        {
            **pool,
            "quantity": _text(pool["quantity"]),
            "capital": _text(pool["capital"]),
            "wac": _text(pool["capital"] / pool["quantity"]) if pool["quantity"] > ZERO else None,
        }
        for _, pool in sorted(pools.items())
        if pool["quantity"] > ZERO
    ]
    return balances, unmatched


def validate_cutover_ff_debit_coverage(capture: Mapping[str, Any]) -> dict[str, Any]:
    """Prove that every gated WB supply is already excluded from current FF.

    A supply is covered by an explicit append-only debit, by the immutable
    checkpoint membership, or by a business timestamp at/before the checkpoint
    boundary whose imported FF balance already absorbed it.
    """

    explicit_supply_ids = {
        str(row.get("source_object_id") or "")
        for row in capture.get("ff_operations") or []
        if str(row.get("source_type") or "")
        in {"wb_supply", "wb_supply_targeted_reconciliation"}
        and str(row.get("source_object_id") or "")
    }
    checkpoint_rows = list(capture.get("ff_auto_writeoff_checkpoint") or [])
    checkpoint = dict(checkpoint_rows[-1]) if checkpoint_rows else {}
    baseline_supply_ids = {
        str(item)
        for item in _loads(checkpoint.get("baseline_supply_ids_json"), [])
        if str(item or "")
    }
    checkpoint_date = str(checkpoint.get("created_at") or "")[:10]
    covered = 0
    checked = 0
    coverage_sources: defaultdict[str, int] = defaultdict(int)
    blockers: list[str] = []
    for raw in capture.get("wb_supplies") or []:
        record = _normalized_wb_record(raw)
        if _is_doprinato(record) or int(record.get("status_id") or 0) not in (
            WB_POST_SHIPMENT_GATE_STATUS_IDS | {WB_FINAL_ACCEPTED_STATUS_ID}
        ):
            continue
        if not any(_decimal(item.get("quantity")) > ZERO for item in _validated_wb_goods(record)):
            continue
        checked += 1
        supply_id = str(record.get("supply_id") or raw.get("supply_id") or "")
        wb_supply_id = str(record.get("wb_supply_id") or raw.get("wb_supply_id") or "")
        identities = {item for item in (supply_id, wb_supply_id) if item}
        if identities & explicit_supply_ids:
            coverage_sources["explicit_append_only_ff_debit"] += 1
            covered += 1
            continue
        if identities & baseline_supply_ids:
            coverage_sources["checkpoint_baseline_membership"] += 1
            covered += 1
            continue
        business_date = _supply_business_date(record, raw)
        if checkpoint_date and business_date and business_date <= checkpoint_date:
            coverage_sources["checkpoint_business_boundary"] += 1
            covered += 1
            continue
        blockers.append(supply_id or wb_supply_id or "missing_supply_id")
    if blockers:
        raise WarehouseFunctionalError(
            "WB supplies passed shipment gate without FF debit/checkpoint coverage: "
            + ",".join(sorted(blockers))
        )
    return {
        "checked_supply_count": checked,
        "covered_supply_count": covered,
        "coverage_sources": dict(sorted(coverage_sources.items())),
        "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
        "checkpoint_created_at": str(checkpoint.get("created_at") or ""),
        "uncovered_supply_count": 0,
    }


def build_frozen_opening_cost_map(
    *,
    target_nm_ids: Iterable[int],
    primary_rows: Iterable[Mapping[str, Any]],
    purchase_price_by_nm: Mapping[int, Any],
    downstream_rows: Iterable[Mapping[str, Any]],
    primary_identity: Mapping[str, Any],
) -> dict[int, CostSeed]:
    """Build the frozen 24.06 map with explicit quality for every target SKU."""

    direct: dict[int, tuple[Decimal, Decimal, Decimal]] = {}
    bands: defaultdict[Decimal, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    overall_qty = ZERO
    overall_capital = ZERO
    for raw in primary_rows:
        nm_id = int(raw.get("nm_id") or 0)
        quantity = _decimal(raw.get("qty") or raw.get("quantity"))
        price = _decimal(raw.get("invoice_unit_price_cny") or raw.get("purchase_price_cny"))
        cost = _decimal(raw.get("sku_ff_unit_cost_rub") or raw.get("ff_unit_cost_rub"))
        if nm_id <= 0 or min(quantity, price, cost) <= ZERO:
            continue
        old_qty, old_capital, _ = direct.get(nm_id, (ZERO, ZERO, ZERO))
        direct[nm_id] = (old_qty + quantity, old_capital + quantity * cost, price)
        bands[price].append((quantity, cost))
        overall_qty += quantity
        overall_capital += quantity * cost
    if overall_qty <= ZERO or not bands:
        raise WarehouseFunctionalError("frozen opening primary shipment has no positive cost bands")
    band_cost = {
        price: sum((qty * cost for qty, cost in rows), ZERO) / sum((qty for qty, _ in rows), ZERO)
        for price, rows in bands.items()
    }
    sorted_bands = sorted(band_cost)
    overall_cost = overall_capital / overall_qty

    downstream_rows = [dict(row) for row in downstream_rows]
    downstream_components = _supply_downstream_component_index(downstream_rows)
    downstream: defaultdict[int, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    total_downstream_qty = ZERO
    total_downstream_capital = ZERO
    for raw in downstream_rows:
        nm_id = int(raw.get("nm_id") or 0)
        quantity = _decimal(raw.get("quantity") or raw.get("accepted_qty"))
        component = downstream_components.get((str(raw.get("wb_supply_id") or ""), nm_id))
        if nm_id <= 0 or quantity <= ZERO or component is None:
            continue
        downstream_unit_cost = component["pre_acceptance_addon"] + component["acceptance_addon"]
        downstream[nm_id].append((quantity, downstream_unit_cost))
        total_downstream_qty += quantity
        total_downstream_capital += quantity * downstream_unit_cost
    if total_downstream_qty <= ZERO:
        raise WarehouseFunctionalError("confirmed downstream FF to WB cost evidence is missing")
    weighted_downstream_unit_cost = total_downstream_capital / total_downstream_qty

    result: dict[int, CostSeed] = {}
    for nm_id in sorted({int(item) for item in target_nm_ids if int(item) > 0}):
        purchase_price = _optional_decimal(purchase_price_by_nm.get(nm_id))
        provenance: dict[str, Any] = {"primary": dict(primary_identity)}
        if nm_id in direct:
            qty, capital, _price = direct[nm_id]
            ff_cost = capital / qty
            quality = "direct_24_06"
        elif purchase_price is not None and purchase_price in band_cost:
            ff_cost = band_cost[purchase_price]
            quality = "same_purchase_price"
            provenance["purchase_price_cny"] = _text(purchase_price)
        elif purchase_price is not None and len(sorted_bands) >= 2:
            lower = max((price for price in sorted_bands if price <= purchase_price), default=None)
            upper = min((price for price in sorted_bands if price >= purchase_price), default=None)
            if lower is not None and upper is not None and lower != upper:
                ff_cost = _linear(purchase_price, lower, band_cost[lower], upper, band_cost[upper])
                quality = "interpolation"
                points = (lower, upper)
            elif lower is None:
                nearest = sorted_bands[0]
                points = (nearest,)
                ff_cost = purchase_price * band_cost[nearest] / nearest
                quality = "extrapolation"
            else:
                nearest = sorted_bands[-1]
                points = (nearest,)
                ff_cost = purchase_price * band_cost[nearest] / nearest
                quality = "extrapolation"
            provenance["purchase_price_cny"] = _text(purchase_price)
            provenance["price_band_points"] = [_text(item) for item in points]
        elif purchase_price is not None and len(sorted_bands) == 1:
            only = sorted_bands[0]
            ff_cost = purchase_price * band_cost[only] / only
            quality = "extrapolation"
            provenance["single_band_ratio"] = _text(band_cost[only] / only)
        else:
            ff_cost = overall_cost
            quality = "fallback_average"
            provenance["missing_purchase_price"] = True
        if ff_cost <= ZERO:
            raise WarehouseFunctionalError(f"non-positive frozen FF cost for nmId {nm_id}")
        direct_downstream = downstream.get(nm_id, [])
        if direct_downstream:
            downstream_unit_cost = sum((qty * cost for qty, cost in direct_downstream), ZERO) / sum(
                (qty for qty, _ in direct_downstream), ZERO
            )
            wb_cost = ff_cost + downstream_unit_cost
            downstream_quality = "direct_confirmed_downstream"
        else:
            downstream_unit_cost = weighted_downstream_unit_cost
            wb_cost = ff_cost + downstream_unit_cost
            downstream_quality = "confirmed_weighted_downstream_unit_cost"
        if wb_cost <= ZERO:
            raise WarehouseFunctionalError(f"non-positive frozen WB cost for nmId {nm_id}")
        result[nm_id] = CostSeed(
            nm_id=nm_id,
            ff_unit_cost=ff_cost,
            wb_unit_cost=wb_cost,
            quality=quality,
            provenance={
                **provenance,
                "quality": quality,
                "downstream_quality": downstream_quality,
                "downstream_unit_cost_rub": _text(downstream_unit_cost),
                "frozen": True,
            },
        )
    return result


def build_historical_wb_cost_projection(
    *,
    opening_cost_map: Iterable[Mapping[str, Any]],
    daily_quantity_rows: Iterable[Mapping[str, Any]],
    downstream_rows: Iterable[Mapping[str, Any]],
    cutover_date: str,
) -> list[dict[str, Any]]:
    """Build 01.07..cutover daily WAC without inventing historical stock.

    Quantity is reused only from persisted daily snapshot evidence.  Cost is
    replaced by the frozen opening map and then rolled with confirmed accepted
    supply layers on their effective dates.
    """

    seeds = {
        int(item["nm_id"]): {
            "ff_wac": _decimal(item["ff_unit_cost_rub"]),
            "wac": _decimal(item["wb_unit_cost_rub"]),
            "quality": str(item["quality"]),
            "provenance": dict(item.get("provenance") or {}),
        }
        for item in opening_cost_map
        if int(item.get("nm_id") or 0) > 0
    }
    quantities: defaultdict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in daily_quantity_rows:
        day = str(row.get("as_of_date") or "")[:10]
        nm_id = int(row.get("nm_id") or 0)
        quantity = _decimal(row.get("physical_quantity") if row.get("physical_quantity") is not None else row.get("stock_qty"))
        if "2026-07-01" <= day < cutover_date and nm_id > 0 and quantity >= ZERO:
            quantities[day][nm_id] = {
                "quantity": quantity,
                "provenance": dict(row.get("quantity_provenance") or {}),
            }
    downstream_rows = [dict(row) for row in downstream_rows]
    downstream_components = _supply_downstream_component_index(downstream_rows)
    inbounds: defaultdict[str, defaultdict[int, list[tuple[Decimal, Decimal, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in downstream_rows:
        day = str(row.get("accepted_date") or row.get("supply_date") or "")[:10]
        nm_id = int(row.get("nm_id") or 0)
        quantity = _decimal(row.get("quantity") or row.get("accepted_qty"))
        seed = seeds.get(nm_id)
        component = downstream_components.get((str(row.get("wb_supply_id") or ""), nm_id))
        cost = (
            _decimal(seed.get("ff_wac"))
            + component["pre_acceptance_addon"]
            + component["acceptance_addon"]
            if seed is not None and component is not None
            else ZERO
        )
        if "2026-07-01" <= day < cutover_date and nm_id > 0 and quantity > ZERO and cost > ZERO:
            inbounds[day][nm_id].append((quantity, cost, str(row.get("wb_supply_id") or "")))
    last_qty: defaultdict[int, Decimal] = defaultdict(Decimal)
    last_wac = {nm_id: dict(seed) for nm_id, seed in seeds.items()}
    result: list[dict[str, Any]] = []
    for day in sorted(quantities):
        for nm_id, quantity_row in sorted(quantities[day].items()):
            quantity = _decimal(quantity_row["quantity"])
            seed = last_wac.get(nm_id)
            if seed is None or _decimal(seed.get("wac")) <= ZERO:
                if quantity > ZERO:
                    raise WarehouseFunctionalError(f"historical WB quantity has no frozen cost for {day}:{nm_id}")
                # A later republished snapshot may legitimately declare a SKU
                # that did not exist at cutover and had zero stock on the
                # historical day. Preserve the exact-column identity and zero
                # capital, but mark the absent cost basis explicitly so Proxy
                # consumers do not mistake zero for a proven unit cost.
                provenance = {
                    "source": "persisted_historical_daily_quantity",
                    "quantity_evidence": dict(quantity_row.get("provenance") or {}),
                    "frozen_opening": {},
                    "previous_snapshot_quantity": _text(last_qty[nm_id]),
                    "inbound_quantity": "0",
                    "inbound_supply_ids": [],
                    "last_valid_wac_retained": False,
                    "zero_quantity_without_cost_basis": True,
                }
                last_qty[nm_id] = ZERO
                item = {
                    "as_of_date": day,
                    "nm_id": nm_id,
                    "quantity": "0",
                    "wac_rub": "0",
                    "capital_rub": "0",
                    "quality": "zero_quantity_without_cost_basis",
                    "provenance": provenance,
                }
                item["fingerprint"] = "sha256:" + _hash(item)
                result.append(item)
                continue
            previous_wac = _decimal(seed["wac"])
            previous_qty = last_qty[nm_id]
            inbound_rows = inbounds[day].get(nm_id, [])
            inbound_qty = sum((item[0] for item in inbound_rows), ZERO)
            inbound_capital = sum((item[0] * item[1] for item in inbound_rows), ZERO)
            if inbound_qty > ZERO:
                basis_qty = previous_qty if previous_qty > ZERO else max(quantity - inbound_qty, ZERO)
                basis_capital = basis_qty * previous_wac
                _, _, rolled = moving_weighted_average(
                    quantity=basis_qty,
                    capital=basis_capital,
                    inbound_quantity=inbound_qty,
                    inbound_capital=inbound_capital,
                )
                wac = rolled or previous_wac
                quality = "periodic_snapshot_wac"
            else:
                wac = previous_wac
                quality = str(seed["quality"])
            provenance = {
                "source": "persisted_historical_daily_quantity",
                "quantity_evidence": dict(quantity_row.get("provenance") or {}),
                "frozen_opening": seed.get("provenance") or {},
                "previous_snapshot_quantity": _text(previous_qty),
                "inbound_quantity": _text(inbound_qty),
                "inbound_supply_ids": [item[2] for item in inbound_rows],
                "last_valid_wac_retained": quantity == ZERO,
            }
            last_qty[nm_id] = quantity
            last_wac[nm_id] = {"wac": wac, "quality": quality, "provenance": provenance}
            item = {
                "as_of_date": day,
                "nm_id": nm_id,
                "quantity": _text(quantity),
                "wac_rub": _text(wac),
                "capital_rub": _text(quantity * wac),
                "quality": quality,
                "provenance": provenance,
            }
            item["fingerprint"] = "sha256:" + _hash(item)
            result.append(item)
    return result


def _historical_projection_business_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return only the arithmetic identity used to prove a correction is non-rewriting."""

    return sorted(
        (
            {
                "as_of_date": str(item.get("as_of_date") or "")[:10],
                "nm_id": int(item.get("nm_id") or 0),
                "quantity": _text(_decimal(item.get("quantity"))),
                "wac_rub": _text(_decimal(item.get("wac_rub"))),
                "capital_rub": _text(_decimal(item.get("capital_rub"))),
            }
            for item in rows
        ),
        key=lambda item: (item["as_of_date"], item["nm_id"]),
    )


def _historical_snapshot_manifest(
    cells_by_date: Mapping[str, Mapping[int, Mapping[str, Any]]],
    *,
    business_dates: Iterable[str],
) -> list[dict[str, Any]]:
    """Pin only the selected exact columns consumed by the correction."""

    result: list[dict[str, Any]] = []
    for day in sorted({str(value or "")[:10] for value in business_dates}):
        cells = cells_by_date.get(day) or {}
        if not cells:
            continue
        provenance = dict(next(iter(cells.values())).get("provenance") or {})
        result.append(
            {
                "business_date": day,
                "bundle_version": str(provenance.get("bundle_version") or ""),
                "snapshot_as_of_date": str(
                    provenance.get("snapshot_as_of_date") or ""
                ),
                "activated_at": str(provenance.get("snapshot_activated_at") or ""),
                "refreshed_at": str(provenance.get("snapshot_refreshed_at") or ""),
                "sku_count": len(cells),
                "exact_stock_total_sha256": str(
                    provenance.get("snapshot_exact_stock_total_sha256") or ""
                ),
            }
        )
    return result


def _historical_snapshot_manifest_digest(
    manifest: Iterable[Mapping[str, Any]],
) -> str:
    normalized = [
        [
            str(item.get("business_date") or ""),
            str(item.get("bundle_version") or ""),
            str(item.get("snapshot_as_of_date") or ""),
            str(item.get("activated_at") or ""),
            str(item.get("refreshed_at") or ""),
            int(item.get("sku_count") or 0),
            str(item.get("exact_stock_total_sha256") or ""),
        ]
        for item in manifest
    ]
    return "sha256:" + _hash(normalized)


def _historical_correction_manifest_from_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Reconstruct the exact-column manifest bound into correction row provenance."""

    grouped: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in rows:
        grouped[str(item.get("as_of_date") or "")[:10]].append(item)
    result: list[dict[str, Any]] = []
    for day, day_rows in sorted(grouped.items()):
        identities = {
            (
                str(evidence.get("bundle_version") or ""),
                str(evidence.get("snapshot_as_of_date") or ""),
                str(evidence.get("snapshot_activated_at") or ""),
                str(evidence.get("snapshot_refreshed_at") or ""),
                str(evidence.get("snapshot_exact_stock_total_sha256") or ""),
            )
            for item in day_rows
            for evidence in [
                dict(item.get("provenance") or {}).get("quantity_evidence") or {}
            ]
        }
        if len(identities) != 1:
            raise WarehouseFunctionalError(
                f"historical correction rows have mixed exact-column provenance: {day}"
            )
        bundle, as_of_date, activated_at, refreshed_at, evidence_digest = next(
            iter(identities)
        )
        if not bundle or not evidence_digest:
            raise WarehouseFunctionalError(
                f"historical correction rows have incomplete exact-column provenance: {day}"
            )
        result.append(
            {
                "business_date": day,
                "bundle_version": bundle,
                "snapshot_as_of_date": as_of_date,
                "activated_at": activated_at,
                "refreshed_at": refreshed_at,
                "sku_count": len(day_rows),
                "exact_stock_total_sha256": evidence_digest,
            }
        )
    return result


def _latest_exact_stock_total_cells_by_date(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, dict[int, dict[str, Any]]]:
    """Return one coherent newest ready-snapshot column for each business date."""

    candidates_by_date: defaultdict[
        str, list[tuple[dict[int, dict[str, Any]], dict[str, Any]]]
    ] = defaultdict(list)
    ordered = sorted(
        (dict(item) for item in rows),
        key=lambda item: (
            str(item.get("activated_at") or ""),
            str(item.get("refreshed_at") or ""),
            str(item.get("as_of_date") or ""),
            str(item.get("bundle_version") or ""),
        ),
    )
    for raw_snapshot in ordered:
        plan_json = str(raw_snapshot.get("plan_json") or "")
        plan = _loads(plan_json, {})
        if not isinstance(plan, Mapping):
            continue
        dates = [str(value or "") for value in plan.get("date_columns") or []]
        data_sheet = next(
            (
                item
                for item in plan.get("sheets") or []
                if isinstance(item, Mapping)
                and str(item.get("sheet_name") or "") == "DATA_VITRINA"
            ),
            None,
        )
        if not dates or not isinstance(data_sheet, Mapping):
            continue
        declared_nm_ids: set[int] = set()
        stock_rows: dict[int, list[Any]] = {}
        duplicate_nm_ids: set[int] = set()
        for row in data_sheet.get("rows") or []:
            if not isinstance(row, list) or len(row) < 2:
                continue
            row_id = str(row[1] or "")
            if row_id.startswith("SKU:") and "|" in row_id:
                try:
                    declared_nm_id = int(row_id[len("SKU:") :].split("|", 1)[0])
                except ValueError:
                    declared_nm_id = 0
                if declared_nm_id > 0:
                    declared_nm_ids.add(declared_nm_id)
            if not row_id.startswith("SKU:") or not row_id.endswith("|stock_total"):
                continue
            try:
                nm_id = int(row_id[len("SKU:") : -len("|stock_total")])
            except ValueError:
                continue
            if nm_id <= 0:
                continue
            if nm_id in stock_rows:
                duplicate_nm_ids.add(nm_id)
            stock_rows[nm_id] = row
        for index, day in enumerate(dates):
            if day < "2026-07-01":
                continue
            cells: dict[int, dict[str, Any]] = {}
            base_provenance = {
                "source": "persisted_ready_snapshot_exact_column",
                "metric_key": "stock_total",
                "column_date": day,
                "snapshot_as_of_date": str(raw_snapshot.get("as_of_date") or ""),
                "snapshot_activated_at": str(raw_snapshot.get("activated_at") or ""),
                "snapshot_refreshed_at": str(raw_snapshot.get("refreshed_at") or ""),
                "bundle_version": str(raw_snapshot.get("bundle_version") or ""),
            }
            for nm_id in sorted(declared_nm_ids):
                row = stock_rows.get(nm_id)
                quantity = (
                    None
                    if row is None
                    or nm_id in duplicate_nm_ids
                    or len(row) <= 2 + index
                    else _optional_decimal(row[2 + index])
                )
                cells[nm_id] = {
                    "quantity": quantity,
                    "provenance": {},
                }
            base_provenance["snapshot_exact_stock_total_sha256"] = "sha256:" + _hash(
                [
                    [
                        nm_id,
                        None
                        if cell.get("quantity") is None
                        else _text(_decimal(cell.get("quantity"))),
                    ]
                    for nm_id, cell in sorted(cells.items())
                ]
            )
            for cell in cells.values():
                cell["provenance"] = dict(base_provenance)
            candidates_by_date[day].append((cells, base_provenance))

    selected: dict[str, dict[int, dict[str, Any]]] = {}
    for day, candidates in candidates_by_date.items():
        # A later publication cannot silently shrink a historical day's SKU
        # universe.  The complete identity set is the union declared by every
        # persisted candidate carrying that exact business date.
        expected_nm_ids = set().union(*(set(cells) for cells, _ in candidates))
        chosen: dict[int, dict[str, Any]] | None = None
        diagnostic: dict[int, dict[str, Any]] = {}
        for cells, base_provenance in candidates:
            expanded = {
                nm_id: (
                    cells[nm_id]
                    if nm_id in cells
                    else {
                        "quantity": None,
                        "provenance": {
                            **base_provenance,
                            "missing_declared_sku_scope": True,
                        },
                    }
                )
                for nm_id in sorted(expected_nm_ids)
            }
            coherent = bool(expanded) and set(cells) == expected_nm_ids and all(
                cell.get("quantity") is not None
                and _decimal(cell.get("quantity")) >= ZERO
                for cell in expanded.values()
            )
            if coherent:
                # Candidates are oldest to newest. Only a later column with the
                # same complete historical SKU universe may replace an older
                # coherent source.
                chosen = expanded
            elif chosen is None:
                # Retain an expanded invalid candidate only for exact fail-closed
                # date/SKU diagnostics when no coherent source exists at all.
                diagnostic = expanded
        selected[day] = chosen if chosen is not None else diagnostic
    return selected


def _missing_pre_cutover_historical_dates(
    frozen_rows: Iterable[Mapping[str, Any]],
    *,
    cutover_date: str,
) -> list[str]:
    """Return whole business dates absent from the immutable pre-cutover calendar."""

    normalized_cutover_date = str(cutover_date or "")[:10]
    if not normalized_cutover_date:
        return []
    last_pre_cutover = (
        date.fromisoformat(normalized_cutover_date) - timedelta(days=1)
    ).isoformat()
    expected_dates = set(_date_range("2026-07-01", last_pre_cutover))
    frozen_dates = {
        str(item.get("as_of_date") or "")[:10]
        for item in frozen_rows
        if str(item.get("as_of_date") or "")[:10]
    }
    return sorted(expected_dates - frozen_dates)


def _build_versioned_historical_correction(
    *,
    cutover: Mapping[str, Any],
    opening_cost_map: Iterable[Mapping[str, Any]],
    frozen_rows: Iterable[Mapping[str, Any]],
    correction_quantity_rows: Iterable[Mapping[str, Any]],
    downstream_rows: Iterable[Mapping[str, Any]],
    ready_snapshot_rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build an append-only correction for absent dates without rewriting frozen rows."""

    cutover_date = _business_date_value(cutover.get("cutover_at"))
    if not cutover_date:
        raise WarehouseFunctionalError("historical correction has no cutover date")
    frozen = _canonical_daily_projection_rows(frozen_rows)
    missing_dates = _missing_pre_cutover_historical_dates(
        frozen,
        cutover_date=cutover_date,
    )
    frozen_dates = {str(item["as_of_date"]) for item in frozen}
    snapshot_rows = [dict(item) for item in ready_snapshot_rows]
    snapshot_manifest: list[dict[str, Any]] = []
    snapshot_manifest_digest = _historical_snapshot_manifest_digest(snapshot_manifest)
    base = {
        "required": bool(missing_dates),
        "correction_id": None,
        "missing_dates": missing_dates,
        "row_count": 0,
        "row_fingerprints": [],
        "ready_snapshot_manifest": snapshot_manifest,
        "ready_snapshot_manifest_digest": snapshot_manifest_digest,
        "supersedes_cutover_id": str(cutover.get("cutover_id") or FUNCTIONAL_CUTOVER_ID),
        "supersedes_plan_fingerprint": str(cutover.get("plan_fingerprint") or ""),
        "method": "append_missing_dates_from_exact_persisted_snapshot_columns_v1",
    }
    if not missing_dates:
        return base, []
    if not snapshot_rows:
        raise WarehouseFunctionalError(
            "missing pre-cutover dates have no persisted exact-column snapshot evidence"
        )
    exact_cells_by_date = _latest_exact_stock_total_cells_by_date(snapshot_rows)
    incomplete_cells = sorted(
        (day, nm_id)
        for day in missing_dates
        for nm_id, cell in exact_cells_by_date.get(day, {}).items()
        if cell.get("quantity") is None or _decimal(cell.get("quantity")) < ZERO
    )
    absent_exact_dates = sorted(
        day for day in missing_dates if not exact_cells_by_date.get(day)
    )
    if absent_exact_dates or incomplete_cells:
        detail = [f"date:{day}" for day in absent_exact_dates]
        detail.extend(f"{day}:{nm_id}" for day, nm_id in incomplete_cells)
        raise WarehouseFunctionalError(
            "historical correction has incomplete exact stock_total evidence: "
            + ",".join(detail)
        )
    snapshot_manifest = _historical_snapshot_manifest(
        exact_cells_by_date,
        business_dates=missing_dates,
    )
    snapshot_manifest_digest = _historical_snapshot_manifest_digest(snapshot_manifest)
    base["ready_snapshot_manifest"] = snapshot_manifest
    base["ready_snapshot_manifest_digest"] = snapshot_manifest_digest
    correction_quantities = [dict(item) for item in correction_quantity_rows]
    supplied_missing_quantities = {
        (str(item.get("as_of_date") or "")[:10], int(item.get("nm_id") or 0)): _text(
            _decimal(item.get("physical_quantity"))
        )
        for item in correction_quantities
        if str(item.get("as_of_date") or "")[:10] in missing_dates
        and int(item.get("nm_id") or 0) > 0
    }
    exact_missing_quantities = {
        (day, nm_id): _text(_decimal(cell.get("quantity")))
        for day in missing_dates
        for nm_id, cell in exact_cells_by_date[day].items()
    }
    if supplied_missing_quantities != exact_missing_quantities:
        raise WarehouseFunctionalError(
            "historical correction quantity view differs from pinned exact stock_total columns"
        )
    replay_quantities = [
        {
            "as_of_date": str(item["as_of_date"]),
            "nm_id": int(item["nm_id"]),
            "physical_quantity": str(item["quantity"]),
            "quantity_provenance": {
                "source": "immutable_frozen_projection_overlap",
                "fingerprint": str(item.get("fingerprint") or ""),
            },
        }
        for item in frozen
    ]
    replay_quantities.extend(
        {
            "as_of_date": day,
            "nm_id": nm_id,
            "physical_quantity": _text(_decimal(cell.get("quantity"))),
            "quantity_provenance": dict(cell.get("provenance") or {}),
        }
        for day in missing_dates
        for nm_id, cell in sorted(exact_cells_by_date[day].items())
    )
    candidate = build_historical_wb_cost_projection(
        opening_cost_map=opening_cost_map,
        daily_quantity_rows=replay_quantities,
        downstream_rows=downstream_rows,
        cutover_date=cutover_date,
    )
    candidate_on_frozen_dates = [
        item for item in candidate if str(item.get("as_of_date") or "")[:10] in frozen_dates
    ]
    if _historical_projection_business_rows(candidate_on_frozen_dates) != (
        _historical_projection_business_rows(frozen)
    ):
        raise WarehouseFunctionalError(
            "historical correction evidence differs from existing frozen business values"
        )
    candidate_missing = [
        item for item in candidate if str(item.get("as_of_date") or "")[:10] in missing_dates
    ]
    candidate_nm_ids_by_date: defaultdict[str, set[int]] = defaultdict(set)
    for item in candidate_missing:
        candidate_nm_ids_by_date[str(item.get("as_of_date") or "")[:10]].add(
            int(item.get("nm_id") or 0)
        )
    identity_mismatches = []
    for day in missing_dates:
        expected_nm_ids = set(exact_cells_by_date[day])
        actual_nm_ids = candidate_nm_ids_by_date.get(day, set())
        if actual_nm_ids != expected_nm_ids:
            identity_mismatches.append(
                {
                    "date": day,
                    "missing_nm_ids": sorted(expected_nm_ids - actual_nm_ids),
                    "unexpected_nm_ids": sorted(actual_nm_ids - expected_nm_ids),
                }
            )
    if identity_mismatches:
        raise WarehouseFunctionalError(
            "historical correction exact stock_total SKU coverage mismatch: "
            + _json(identity_mismatches)
        )
    covered_dates = {str(item.get("as_of_date") or "")[:10] for item in candidate_missing}
    if covered_dates != set(missing_dates):
        absent = sorted(set(missing_dates) - covered_dates)
        raise WarehouseFunctionalError(
            "historical correction has no exact evidence for missing business dates: "
            + ",".join(absent)
        )
    correction_id = "whcorr_" + _hash(
        {
            "cutover_id": base["supersedes_cutover_id"],
            "supersedes": base["supersedes_plan_fingerprint"],
            "missing_dates": missing_dates,
            "snapshot_manifest_digest": snapshot_manifest_digest,
            "business_rows": _historical_projection_business_rows(candidate_missing),
        }
    )[:24]
    corrected_rows: list[dict[str, Any]] = []
    for item in candidate_missing:
        original_provenance = dict(item.get("provenance") or {})
        corrected_rows.append(
            _daily_wb_cost_row(
                day=str(item["as_of_date"]),
                nm_id=int(item["nm_id"]),
                quantity=_decimal(item["quantity"]),
                wac=_decimal(item["wac_rub"]),
                quality=str(item["quality"]),
                provenance={
                    **original_provenance,
                    "versioned_historical_correction": {
                        "correction_id": correction_id,
                        "method": base["method"],
                        "supersedes_cutover_id": base["supersedes_cutover_id"],
                        "supersedes_plan_fingerprint": base[
                            "supersedes_plan_fingerprint"
                        ],
                        "ready_snapshot_manifest_digest": snapshot_manifest_digest,
                        "original_projection_fingerprint": str(item.get("fingerprint") or ""),
                    },
                },
            )
        )
    result = {
        **base,
        "correction_id": correction_id,
        "row_count": len(corrected_rows),
        "row_fingerprints": [str(item["fingerprint"]) for item in corrected_rows],
    }
    return result, corrected_rows


def _validate_historical_correction_plan(
    correction: Mapping[str, Any],
    *,
    rows: Iterable[Mapping[str, Any]],
    cutover: Mapping[str, Any],
) -> None:
    normalized_rows = _canonical_daily_projection_rows(rows)
    fingerprints = [str(item["fingerprint"]) for item in normalized_rows]
    dates = sorted({str(item["as_of_date"]) for item in normalized_rows})
    if not str(correction.get("correction_id") or "").startswith("whcorr_"):
        raise WarehouseFunctionalError("historical correction id is invalid")
    if int(correction.get("row_count") or 0) != len(normalized_rows):
        raise WarehouseFunctionalError("historical correction row count mismatch")
    if list(correction.get("row_fingerprints") or []) != fingerprints:
        raise WarehouseFunctionalError("historical correction row fingerprint mismatch")
    if list(correction.get("missing_dates") or []) != dates:
        raise WarehouseFunctionalError("historical correction date identity mismatch")
    manifest = list(correction.get("ready_snapshot_manifest") or [])
    if not manifest or _historical_snapshot_manifest_digest(manifest) != str(
        correction.get("ready_snapshot_manifest_digest") or ""
    ):
        raise WarehouseFunctionalError("historical correction source manifest mismatch")
    if manifest != _historical_correction_manifest_from_rows(normalized_rows):
        raise WarehouseFunctionalError(
            "historical correction source manifest differs from row provenance"
        )
    if str(correction.get("supersedes_cutover_id") or "") != str(
        cutover.get("cutover_id") or ""
    ) or str(correction.get("supersedes_plan_fingerprint") or "") != str(
        cutover.get("plan_fingerprint") or ""
    ):
        raise WarehouseFunctionalError("historical correction supersedes identity mismatch")
    for item in normalized_rows:
        provenance = dict(item.get("provenance") or {}).get(
            "versioned_historical_correction"
        )
        if not isinstance(provenance, Mapping) or str(
            provenance.get("correction_id") or ""
        ) != str(correction["correction_id"]):
            raise WarehouseFunctionalError("historical correction provenance mismatch")
        if str(provenance.get("supersedes_cutover_id") or "") != str(
            correction.get("supersedes_cutover_id") or ""
        ) or str(provenance.get("supersedes_plan_fingerprint") or "") != str(
            correction.get("supersedes_plan_fingerprint") or ""
        ):
            raise WarehouseFunctionalError("historical correction row supersedes mismatch")
        if str(provenance.get("ready_snapshot_manifest_digest") or "") != str(
            correction.get("ready_snapshot_manifest_digest") or ""
        ):
            raise WarehouseFunctionalError(
                "historical correction row source manifest digest mismatch"
            )


def _validate_historical_correction_matches_derived(
    *,
    planned_correction: Mapping[str, Any],
    planned_rows: Iterable[Mapping[str, Any]],
    expected_correction: Mapping[str, Any],
    expected_rows: Iterable[Mapping[str, Any]],
) -> None:
    """Require a reviewed correction to equal the current deterministic derivation."""

    if _clone(planned_correction) != _clone(expected_correction):
        raise WarehouseFunctionalError(
            "historical correction plan differs from current persisted evidence"
        )
    if _canonical_daily_projection_rows(planned_rows) != (
        _canonical_daily_projection_rows(expected_rows)
    ):
        raise WarehouseFunctionalError(
            "historical correction rows differ from current persisted evidence"
        )


class WarehouseFunctionalBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        stocks_block: StocksBlock | None = None,
        timestamp_factory: Callable[[], str] | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime = runtime
        self.runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.timestamp_factory = timestamp_factory or _now
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.opening = WarehouseStocksBlock(
            runtime=runtime,
            stocks_block=stocks_block,
            timestamp_factory=self.timestamp_factory,
            now_factory=self.now_factory,
        )
        self.canonical_cost = CanonicalCostEngine(
            runtime=runtime,
            timestamp_factory=self.timestamp_factory,
        )
        self.calculation_parameters = CalculationParametersBlock(runtime=runtime)
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            conn.commit()

    def build_cutover_plan(self) -> dict[str, Any]:
        existing = self.readback()
        if existing.get("status") == "ready":
            return {
                "status": "already_applied",
                "idempotent": True,
                "cutover": existing["cutover"],
                "plan_fingerprint": existing["cutover"]["plan_fingerprint"],
            }
        return self._build_plan(kind="functional_cutover")

    def build_sync_plan(self, *, use_external_api: bool = True) -> dict[str, Any]:
        if not use_external_api:
            raise WarehouseFunctionalError("bounded WB sync requires a fresh official snapshot")
        if self.readback().get("status") != "ready":
            raise WarehouseFunctionalError("functional cutover must be applied before hourly sync")
        return self._build_plan(kind="hourly_wb_sync")

    def build_emergency_rebuild_plan(self) -> dict[str, Any]:
        """Rebuild from persisted sources only; never call an external API."""

        if self.readback().get("status") != "ready":
            raise WarehouseFunctionalError("functional cutover must be applied before emergency rebuild")
        return self._build_plan(kind="emergency_rebuild", wb_payload=self._last_good_wb_payload())

    def _build_plan(
        self,
        *,
        kind: str,
        wb_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if wb_payload is None:
            nomenclature = self.opening._opening_nomenclature_request()  # noqa: SLF001
            wb_payload = self.opening._fetch_wb_stock_snapshot(nomenclature)  # noqa: SLF001
        capture = self._capture_sources(
            captured_at=None,
            wb_payload=wb_payload,
            include_historical_correction=kind == "emergency_rebuild",
        )
        captured_at = str(capture["captured_at"])
        ff_debit_coverage = (
            validate_cutover_ff_debit_coverage(capture) if kind == "functional_cutover" else None
        )
        base_active_version_id = self._active_version_id()
        previous = self._active_lines()
        cutover = self._cutover_row()
        lines, unmatched, events, opening_cost_map, movement_documents = self._calculate_lines(
            capture=capture,
            previous=previous,
            cutover=cutover,
            cutover_mode=kind == "functional_cutover",
        )
        supplier_cost_states = _supplier_cost_version_states(capture)
        effective_date = _current_snapshot_effective_date(
            captured_at=captured_at,
            snapshot_date=capture["wb_snapshot"]["snapshot_date"],
        )
        projection_cutoff = (
            effective_date
            if kind == "functional_cutover"
            else (
                business_date_from_timestamp(str((cutover or {}).get("cutover_at")))
                if str((cutover or {}).get("cutover_at") or "")
                else effective_date
            )
        )
        if cutover is None:
            pre_cutover_wb_cost_projection = build_historical_wb_cost_projection(
                opening_cost_map=opening_cost_map,
                daily_quantity_rows=capture["historical_wb_daily_quantities"],
                downstream_rows=capture["downstream_cost_rows"],
                cutover_date=projection_cutoff,
            )
        else:
            # The cutover materializes the exact pre-cutover replay.  Later
            # ready snapshots are mutable publication artifacts, so an hourly
            # sync must reuse these frozen rows instead of reconstructing old
            # business dates from a newly published snapshot.
            pre_cutover_wb_cost_projection = list(
                capture.get("frozen_pre_cutover_wb_cost_projection") or []
            )
        historical_correction = {
            "required": False,
            "correction_id": None,
            "missing_dates": [],
            "row_count": 0,
            "row_fingerprints": [],
            "ready_snapshot_manifest": [],
            "ready_snapshot_manifest_digest": None,
            "supersedes_cutover_id": None,
            "supersedes_plan_fingerprint": None,
        }
        if cutover is not None and kind == "emergency_rebuild":
            historical_correction, correction_rows = _build_versioned_historical_correction(
                cutover=cutover,
                opening_cost_map=opening_cost_map,
                frozen_rows=pre_cutover_wb_cost_projection,
                correction_quantity_rows=capture.get(
                    "historical_correction_wb_daily_quantities"
                )
                or [],
                downstream_rows=capture["downstream_cost_rows"],
                ready_snapshot_rows=capture.get("historical_correction_ready_snapshots")
                or [],
            )
            pre_cutover_wb_cost_projection.extend(correction_rows)
            pre_cutover_wb_cost_projection = _canonical_daily_projection_rows(
                pre_cutover_wb_cost_projection
            )
        post_cutover_wb_cost_projection = self._build_post_cutover_daily_cost_projection(
            captured_at=captured_at,
            candidate_lines=lines,
            candidate_snapshot=capture["wb_snapshot"],
            new_events=events,
            opening_cost_map=opening_cost_map,
            cutover_mode=kind == "functional_cutover",
        )
        historical_wb_cost_projection = (
            pre_cutover_wb_cost_projection + post_cutover_wb_cost_projection
        )
        historical_calendar = _validate_historical_projection_calendar(
            historical_wb_cost_projection,
            effective_date=effective_date,
        )
        lines = _replace_current_wb_costs(
            lines,
            daily_projection=post_cutover_wb_cost_projection,
            current_date=effective_date,
        )
        summaries = _summaries(lines)
        positive = [line for line in lines if line.quantity > ZERO]
        gaps = [line for line in positive if line.wac is None or line.wac <= ZERO or line.capital <= ZERO]
        negatives = [line for line in lines if min(line.quantity, line.capital) < ZERO]
        if gaps:
            raise WarehouseFunctionalError(
                "positive warehouse balances have no positive cost coverage: "
                + ",".join(f"{line.warehouse_key}:{line.nm_id}" for line in gaps)
            )
        if negatives:
            raise WarehouseFunctionalError("negative warehouse quantity or capital is forbidden")
        plan = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "dry_run_ready",
            "kind": kind,
            "cutover_id": FUNCTIONAL_CUTOVER_ID,
            "captured_at": captured_at,
            "effective_date": effective_date,
            "base_active_version_id": base_active_version_id,
            "local_source_digest": capture["local_source_digest"],
            "wb_supply_source_digest": capture["wb_supply_source_digest"],
            "source_watermarks": capture["watermarks"],
            "absorbed_supply_revisions": capture["supply_revisions"] if kind == "functional_cutover" else {},
            "wb_snapshot": capture["wb_snapshot"],
            "opening_cost_map": opening_cost_map if kind == "functional_cutover" else [],
            "historical_correction": historical_correction,
            "historical_wb_cost_projection": historical_wb_cost_projection,
            "lines": [_line_payload(line) for line in lines],
            "summaries": summaries,
            "unmatched_doprinato": unmatched,
            "new_events": events,
            "movement_documents": movement_documents,
            "supplier_cost_states": supplier_cost_states,
            "ff_reservations": _ff_reservation_snapshot_rows(capture),
            "diff": _balance_diff(previous, lines),
            "invariants": {
                "warehouse_count": len(STAGES),
                "negative_balance_count": len(negatives),
                "positive_cost_gap_count": len(gaps),
                "historical_wb_cost_gap_count": sum(
                    1
                    for item in historical_wb_cost_projection
                    if _decimal(item["quantity"]) > ZERO and _decimal(item["wac_rub"]) <= ZERO
                ),
                "historical_wb_calendar": historical_calendar,
                "wb_quantity_source": "official_snapshot_only",
                "discrepancy_opening_zero": (
                    summaries[STAGE_DISCREPANCY]["quantity"] == "0"
                    if kind == "functional_cutover" else None
                ),
                "ff_debit_coverage": ff_debit_coverage,
            },
        }
        plan["calculation_digest"] = _calculation_digest(plan)
        plan["plan_fingerprint"] = _fingerprint(plan)
        return plan

    def apply_plan(
        self,
        plan: Mapping[str, Any],
        *,
        confirm_fingerprint: str,
        backup_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Publish at the common serialized boundary used by every caller."""

        with warehouse_functional_write_lock(self.runtime.runtime_dir):
            return self._apply_plan_locked(
                plan,
                confirm_fingerprint=confirm_fingerprint,
                backup_dir=backup_dir,
            )

    def _apply_plan_locked(
        self,
        plan: Mapping[str, Any],
        *,
        confirm_fingerprint: str,
        backup_dir: Path | None = None,
    ) -> dict[str, Any]:
        normalized = _clone(plan)
        fingerprint = str(normalized.get("plan_fingerprint") or "")
        if not fingerprint or fingerprint != str(confirm_fingerprint or ""):
            raise WarehouseFunctionalError("exact reviewed plan fingerprint is required")
        if fingerprint != _fingerprint({key: value for key, value in normalized.items() if key != "plan_fingerprint"}):
            raise WarehouseFunctionalError("functional plan fingerprint mismatch")
        kind = str(normalized.get("kind") or "")
        existing = self._cutover_row()
        if kind == "functional_cutover" and existing is not None:
            if existing["plan_fingerprint"] != fingerprint:
                raise WarehouseFunctionalError("functional cutover already exists with another fingerprint")
            return {**self.readback(), "idempotent": True}
        if self._version_exists(fingerprint):
            return {**self.readback(), "idempotent": True}
        planned_effective_date = _current_snapshot_effective_date(
            captured_at=str(normalized.get("captured_at") or ""),
            snapshot_date=(normalized.get("wb_snapshot") or {}).get("snapshot_date"),
        )
        if planned_effective_date != str(normalized.get("effective_date") or "")[:10]:
            raise WarehouseFunctionalError(
                "functional plan effective date differs from its coherent source capture"
            )
        apply_business_date = business_date_from_timestamp(self.timestamp_factory())
        if apply_business_date != planned_effective_date:
            raise WarehouseFunctionalError(
                "functional plan crossed the canonical business-date boundary before apply: "
                f"planned={planned_effective_date}, apply={apply_business_date}"
            )
        if kind != "functional_cutover" and self._active_version_id() != str(
            normalized.get("base_active_version_id") or ""
        ):
            raise WarehouseFunctionalError(
                "active functional warehouse version drifted after bounded calculation"
            )
        recovery_end_date = str(normalized.get("effective_date") or "")[:10]
        include_historical_correction = kind == "emergency_rebuild"
        current_digest = self._local_source_digest(
            recovery_end_date=recovery_end_date,
            include_historical_correction=include_historical_correction,
        )
        if current_digest != str(normalized.get("local_source_digest") or ""):
            raise WarehouseFunctionalError("local sources drifted after dry-run")
        if kind != "functional_cutover" and self._wb_supply_source_digest() != str(
            normalized.get("wb_supply_source_digest") or ""
        ):
            raise WarehouseFunctionalError("WB supply sources drifted after bounded capture")
        if kind == "emergency_rebuild":
            with _connect(self.runtime.db_path) as correction_conn:
                ensure_warehouse_functional_schema(correction_conn)
                self._validate_emergency_correction_against_current(
                    normalized,
                    connection=correction_conn,
                    recovery_end_date=recovery_end_date,
                )
        backup = None
        if kind in {"functional_cutover", "emergency_rebuild"}:
            if backup_dir is None or not Path(backup_dir).is_absolute():
                raise WarehouseFunctionalError(
                    f"absolute backup_dir is required for {kind.replace('_', ' ')}"
                )
            Path(backup_dir).mkdir(parents=True, exist_ok=True)
            timestamp = self.timestamp_factory().replace(":", "").replace("-", "")
            if kind == "functional_cutover":
                prefix = FUNCTIONAL_CUTOVER_ID
                destination = Path(backup_dir) / f"{prefix}-{timestamp}.sqlite3"
            else:
                prefix = f"warehouse-functional-emergency-{fingerprint.removeprefix('sha256:')[:16]}"
                destination = Path(backup_dir) / f"{prefix}.sqlite3"
            if kind == "emergency_rebuild" and destination.exists():
                destination = Path(backup_dir) / f"{prefix}-{timestamp}.sqlite3"
            backup = self.runtime.backup_database(destination)
            destination.chmod(0o600)
            if str(backup.get("integrity_check") or "").lower() != "ok":
                _discard_uncommitted_backup(backup)
                raise WarehouseFunctionalError(f"pre-{kind} backup integrity_check is not ok")
        now = self.timestamp_factory()
        publication_effective_at = now if kind == "functional_cutover" else str(normalized["captured_at"])
        version_id = "whfv_" + fingerprint.removeprefix("sha256:")[:24]
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                duplicate = conn.execute(
                    "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_versions WHERE plan_fingerprint=?",
                    (fingerprint,),
                ).fetchone()
                if duplicate is not None:
                    conn.rollback()
                    _discard_uncommitted_backup(backup)
                    return {**self.readback(), "idempotent": True}
                if kind != "functional_cutover" and self._active_version_id(connection=conn) != str(
                    normalized.get("base_active_version_id") or ""
                ):
                    raise WarehouseFunctionalError(
                        "active functional warehouse version drifted while acquiring apply lock"
                    )
                locked_apply_business_date = business_date_from_timestamp(
                    self.timestamp_factory()
                )
                if locked_apply_business_date != planned_effective_date:
                    raise WarehouseFunctionalError(
                        "functional plan crossed the canonical business-date boundary "
                        "while acquiring apply lock"
                    )
                if self._local_source_digest(
                    connection=conn,
                    recovery_end_date=recovery_end_date,
                    include_historical_correction=include_historical_correction,
                ) != current_digest:
                    raise WarehouseFunctionalError("local sources drifted while acquiring apply lock")
                if kind != "functional_cutover" and self._wb_supply_source_digest(connection=conn) != str(
                    normalized.get("wb_supply_source_digest") or ""
                ):
                    raise WarehouseFunctionalError("WB supply sources drifted while acquiring apply lock")
                if kind == "emergency_rebuild":
                    self._validate_emergency_correction_against_current(
                        normalized,
                        connection=conn,
                        recovery_end_date=recovery_end_date,
                    )
                if kind == "functional_cutover":
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers(
                               cutover_id,cutover_at,status,plan_fingerprint,source_watermarks_json,
                               absorbed_supply_revisions_json,backup_json,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            FUNCTIONAL_CUTOVER_ID,
                            publication_effective_at,
                            "posted",
                            fingerprint,
                            _json(normalized.get("source_watermarks") or {}),
                            _json(normalized.get("absorbed_supply_revisions") or {}),
                            _json(backup or {}),
                            now,
                            now,
                        ),
                    )
                    self.calculation_parameters.ensure_initial_version(
                        connection=conn,
                        created_at=now,
                    )
                    for item in normalized.get("opening_cost_map") or []:
                        conn.execute(
                            """INSERT INTO sheet_vitrina_v1_warehouse_opening_cost_map(
                                   cutover_id,nm_id,ff_unit_cost_rub,wb_unit_cost_rub,quality,
                                   provenance_json,fingerprint,created_at
                               ) VALUES(?,?,?,?,?,?,?,?)""",
                            (
                                FUNCTIONAL_CUTOVER_ID,
                                int(item["nm_id"]),
                                str(item["ff_unit_cost_rub"]),
                                str(item["wb_unit_cost_rub"]),
                                str(item["quality"]),
                                _json(item["provenance"]),
                                str(item["fingerprint"]),
                                now,
                            ),
                        )
                cutover_date = ""
                if kind != "functional_cutover":
                    cutover_date_row = conn.execute(
                        "SELECT cutover_at FROM sheet_vitrina_v1_warehouse_functional_cutovers WHERE cutover_id=?",
                        (FUNCTIONAL_CUTOVER_ID,),
                    ).fetchone()
                    if cutover_date_row is None:
                        raise WarehouseFunctionalError("functional daily replay has no cutover row")
                    cutover_date = _business_date_value(cutover_date_row["cutover_at"])
                    planned_frozen = _canonical_daily_projection_rows(
                        item
                        for item in normalized.get("historical_wb_cost_projection") or []
                        if str(item.get("as_of_date") or "")[:10] < cutover_date
                    )
                    persisted_frozen = _frozen_pre_cutover_wb_cost_projection(
                        conn,
                        cutover_date=cutover_date,
                    )
                    correction = dict(normalized.get("historical_correction") or {})
                    correction_fingerprints = {
                        str(value) for value in correction.get("row_fingerprints") or []
                    }
                    planned_correction = [
                        item
                        for item in planned_frozen
                        if str(item.get("fingerprint") or "") in correction_fingerprints
                    ]
                    planned_existing = [
                        item
                        for item in planned_frozen
                        if str(item.get("fingerprint") or "") not in correction_fingerprints
                    ]
                    if planned_existing != persisted_frozen:
                        raise WarehouseFunctionalError(
                            "pre-cutover WB daily cost projection differs from the frozen cutover history"
                        )
                    if bool(correction.get("required")):
                        if kind != "emergency_rebuild":
                            raise WarehouseFunctionalError(
                                "historical correction is allowed only in an emergency rebuild"
                            )
                        _validate_historical_correction_plan(
                            correction,
                            rows=planned_correction,
                            cutover=existing or {},
                        )
                        for item in planned_correction:
                            conn.execute(
                                """INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost(
                                       cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,
                                       quality,provenance_json,fingerprint,created_at
                                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                (
                                    FUNCTIONAL_CUTOVER_ID,
                                    str(item["as_of_date"]),
                                    int(item["nm_id"]),
                                    str(item["quantity"]),
                                    str(item["wac_rub"]),
                                    str(item["capital_rub"]),
                                    str(item["quality"]),
                                    _json(item.get("provenance") or {}),
                                    str(item["fingerprint"]),
                                    now,
                                ),
                            )
                        conn.execute(
                            """INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost_corrections(
                                   correction_id,cutover_id,version_id,supersedes_plan_fingerprint,
                                   correction_plan_fingerprint,missing_dates_json,row_fingerprints_json,
                                   ready_snapshot_manifest_json,ready_snapshot_manifest_digest,
                                   provenance_json,backup_json,created_at
                               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                str(correction["correction_id"]),
                                FUNCTIONAL_CUTOVER_ID,
                                version_id,
                                str(correction["supersedes_plan_fingerprint"]),
                                fingerprint,
                                _json(correction["missing_dates"]),
                                _json(correction["row_fingerprints"]),
                                _json(correction["ready_snapshot_manifest"]),
                                str(correction["ready_snapshot_manifest_digest"]),
                                _json(correction),
                                _json(backup or {}),
                                now,
                            ),
                        )
                    elif planned_correction:
                        raise WarehouseFunctionalError(
                            "unexpected historical correction rows in emergency plan"
                        )
                    conn.execute(
                        """DELETE FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                           WHERE cutover_id=? AND as_of_date>=?""",
                        (FUNCTIONAL_CUTOVER_ID, cutover_date),
                    )
                for item in normalized.get("historical_wb_cost_projection") or []:
                    if cutover_date and str(item.get("as_of_date") or "")[:10] < cutover_date:
                        continue
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost(
                               cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,
                               quality,provenance_json,fingerprint,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(cutover_id,as_of_date,nm_id) DO UPDATE SET
                               quantity=excluded.quantity,wac_rub=excluded.wac_rub,
                               capital_rub=excluded.capital_rub,quality=excluded.quality,
                               provenance_json=excluded.provenance_json,
                               fingerprint=excluded.fingerprint,created_at=excluded.created_at""",
                        (
                            FUNCTIONAL_CUTOVER_ID,
                            str(item["as_of_date"]),
                            int(item["nm_id"]),
                            str(item["quantity"]),
                            str(item["wac_rub"]),
                            str(item["capital_rub"]),
                            str(item["quality"]),
                            _json(item.get("provenance") or {}),
                            str(item["fingerprint"]),
                            now,
                        ),
                    )
                self._upsert_supplier_flows(conn, normalized.get("lines") or [], created_at=now)
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                           version_id,cutover_id,version_kind,effective_at,status,plan_fingerprint,
                           local_source_digest,source_watermarks_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        version_id,
                        FUNCTIONAL_CUTOVER_ID,
                        kind,
                        publication_effective_at,
                        "good",
                        fingerprint,
                        current_digest,
                        _json(normalized.get("source_watermarks") or {}),
                        now,
                    ),
                )
                self._insert_snapshot(conn, version_id=version_id, payload=normalized["wb_snapshot"])
                for item in normalized.get("supplier_cost_states") or []:
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_supplier_cost_states(
                               version_id,shipment_id,source_fingerprint,calculation_fingerprint,
                               expenses_complete,calculation_available,created_at
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            version_id,
                            str(item["shipment_id"]),
                            str(item["source_fingerprint"]),
                            str(item["calculation_fingerprint"]),
                            int(bool(item["expenses_complete"])),
                            int(bool(item["calculation_available"])),
                            now,
                        ),
                    )
                for item in normalized["lines"]:
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                               version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                               cost_covered_quantity,quality,certified,wb_quantity,
                               wb_in_way_to_client,wb_in_way_from_client,provenance_json
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            version_id,
                            item["warehouse_key"],
                            int(item["nm_id"]),
                            item["quantity"],
                            item["wac_rub"],
                            item["capital_rub"],
                            item["cost_covered_quantity"],
                            item["quality"],
                            int(bool(item["certified"])),
                            item["wb_quantity"],
                            item["wb_in_way_to_client"],
                            item["wb_in_way_from_client"],
                            _json(item["provenance"]),
                        ),
                    )
                for item in normalized.get("ff_reservations") or []:
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_functional_ff_reservations(
                               version_id,supply_id,nm_id,quantity
                           ) VALUES(?,?,?,?)""",
                        (
                            version_id,
                            str(item["supply_id"]),
                            int(item["nm_id"]),
                            str(item["quantity"]),
                        ),
                    )
                for item in normalized.get("unmatched_doprinato") or []:
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_unmatched_doprinato(
                               unmatched_id,version_id,source_id,business_date,nm_id,quantity,
                               matched_quantity,reason,provenance_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            _stable_id(
                                "unmatched",
                                {"version_id": version_id, "evidence": item},
                            ),
                            version_id,
                            str(item.get("source_id") or ""),
                            str(item.get("business_date") or ""),
                            int(item["nm_id"]),
                            str(item["quantity"]),
                            str(item.get("matched_quantity") or "0"),
                            str(item.get("reason") or ""),
                            _json(item),
                            now,
                        ),
                    )
                for event in normalized.get("new_events") or []:
                    conn.execute(
                        """INSERT OR IGNORE INTO sheet_vitrina_v1_warehouse_functional_events(
                               event_id,version_id,event_type,source_id,source_fingerprint,
                               business_date,nm_id,quantity,capital_rub,provenance_json,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            str(event["event_id"]),
                            version_id,
                            event["event_type"],
                            event["source_id"],
                            event["source_fingerprint"],
                            event["business_date"],
                            int(event["nm_id"]),
                            event["quantity"],
                            event["capital_rub"],
                            _json(event.get("provenance") or {}),
                            now,
                        ),
                    )
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_functional_active(slot,version_id,updated_at)
                       VALUES(1,?,?) ON CONFLICT(slot) DO UPDATE SET version_id=excluded.version_id,
                       updated_at=excluded.updated_at""",
                    (version_id, now),
                )
                if kind == "emergency_rebuild":
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status(
                               slot,last_attempt_at,last_success_at,last_error,active_version_id,updated_at
                           ) VALUES(1,NULL,NULL,NULL,?,?) ON CONFLICT(slot) DO UPDATE SET
                               active_version_id=excluded.active_version_id,updated_at=excluded.updated_at""",
                        (version_id, now),
                    )
                else:
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status(
                               slot,last_attempt_at,last_success_at,last_error,active_version_id,updated_at
                           ) VALUES(1,?,?,NULL,?,?) ON CONFLICT(slot) DO UPDATE SET
                               last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,
                               last_error=NULL,active_version_id=excluded.active_version_id,updated_at=excluded.updated_at""",
                        (now, now, version_id, now),
                    )
                self._insert_documents(conn, version_id=version_id, plan=normalized, created_at=now)
                _verify_version(conn, version_id=version_id, expected=normalized)
                conn.execute(
                    """UPDATE sheet_vitrina_v1_warehouse_targeted_recalc_queue
                       SET status='complete',finished_at=?,error=NULL
                       WHERE status IN ('queued','running')""",
                    (now,),
                )
                if business_date_from_timestamp(self.timestamp_factory()) != planned_effective_date:
                    raise WarehouseFunctionalError(
                        "functional plan crossed the canonical business-date boundary before commit"
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                _discard_uncommitted_backup(backup)
                raise
        return {**self.readback(), "idempotent": False, "backup": backup}

    def rollback_functional_cutover(
        self,
        *,
        confirm_fingerprint: str,
        backup_dir: Path,
    ) -> dict[str, Any]:
        with warehouse_functional_write_lock(self.runtime.runtime_dir):
            return self._rollback_functional_cutover_locked(
                confirm_fingerprint=confirm_fingerprint,
                backup_dir=backup_dir,
            )

    def _rollback_functional_cutover_locked(
        self,
        *,
        confirm_fingerprint: str,
        backup_dir: Path,
    ) -> dict[str, Any]:
        """Remove only derived functional state after exact confirmation.

        Primary supplier, CNY, FF and WB records are never touched.  A coherent
        pre-rollback backup is retained for recovery and audit.
        """

        cutover = self._cutover_row()
        if cutover is None:
            return {"status": "not_initialized", "idempotent": True}
        if str(cutover["plan_fingerprint"]) != str(confirm_fingerprint or ""):
            raise WarehouseFunctionalError("functional rollback fingerprint mismatch")
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            archival_active = conn.execute(
                """SELECT version_id
                   FROM sheet_vitrina_v1_warehouse_archival_estimate_active
                   WHERE slot=1"""
            ).fetchone()
        if archival_active is not None:
            raise WarehouseFunctionalError(
                "active archival estimate must be rolled back before functional cutover rollback"
            )
        destination_dir = Path(backup_dir)
        if not destination_dir.is_absolute():
            raise WarehouseFunctionalError("absolute backup_dir is required for rollback")
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / (
            f"{FUNCTIONAL_CUTOVER_ID}-rollback-{self.timestamp_factory().replace(':', '').replace('-', '')}.sqlite3"
        )
        backup = self.runtime.backup_database(destination)
        destination.chmod(0o600)
        if str(backup.get("integrity_check") or "").lower() != "ok":
            raise WarehouseFunctionalError("pre-rollback backup integrity_check is not ok")
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                version_ids = [
                    str(row[0])
                    for row in conn.execute(
                        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_versions WHERE cutover_id=?",
                        (FUNCTIONAL_CUTOVER_ID,),
                    ).fetchall()
                ]
                for version_id in version_ids:
                    conn.execute(
                        """DELETE FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks
                           WHERE replay_id IN(
                               SELECT replay_id
                               FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replays
                               WHERE version_id=?
                           )""",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_supplier_cost_state_corrections WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replays WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_functional_document_lines WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_functional_ff_reservations WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_wb_snapshots WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_unmatched_doprinato WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_functional_events WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_functional_documents WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_supplier_cost_states WHERE version_id=?",
                        (version_id,),
                    )
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1")
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_wb_sync_status WHERE slot=1")
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue")
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_versions WHERE cutover_id=?", (FUNCTIONAL_CUTOVER_ID,))
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_wb_daily_cost_corrections WHERE cutover_id=?", (FUNCTIONAL_CUTOVER_ID,))
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE cutover_id=?", (FUNCTIONAL_CUTOVER_ID,))
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_opening_cost_map WHERE cutover_id=?", (FUNCTIONAL_CUTOVER_ID,))
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_supplier_flows")
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_cutovers WHERE cutover_id=?", (FUNCTIONAL_CUTOVER_ID,))
                conn.execute(
                    """DELETE FROM sheet_vitrina_v1_calculation_parameter_versions
                       WHERE version_id=? AND source='functional_cutover_initial_version'
                         AND NOT EXISTS(
                           SELECT 1 FROM sheet_vitrina_v1_calculation_parameter_versions
                           WHERE block_key='proxy_profit_margin' AND version_id<>?
                         )""",
                    ("calculation_parameters_proxy_v1_20260701", "calculation_parameters_proxy_v1_20260701"),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "status": "rolled_back",
            "idempotent": False,
            "cutover_id": FUNCTIONAL_CUTOVER_ID,
            "plan_fingerprint": confirm_fingerprint,
            "backup": backup,
            "primary_sources_changed": False,
        }

    def record_failed_sync(self, error: Exception) -> None:
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status(
                       slot,last_attempt_at,last_success_at,last_error,active_version_id,updated_at
                   ) VALUES(1,?,NULL,?,NULL,?) ON CONFLICT(slot) DO UPDATE SET
                       last_attempt_at=excluded.last_attempt_at,last_error=excluded.last_error,
                       updated_at=excluded.updated_at""",
                (now, str(error)[:2000], now),
            )
            conn.commit()

    def overview(self) -> dict[str, Any]:
        readback = self.readback()
        if readback.get("status") != "ready":
            return readback
        lines = _revalidate_balance_certifications(
            runtime=self.runtime,
            balances=readback["balances"],
            active_version_id=str((readback.get("active_version") or {}).get("version_id") or ""),
        )
        summaries = _summaries([_line_from_payload(item) for item in lines])
        ff_reservations = dict(readback.get("ff_reservations") or {})
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready",
            "cutover": readback["cutover"],
            "active_version": readback["active_version"],
            "sync": readback["sync"],
            "warehouses": [
                {
                    "warehouse_key": key,
                    "warehouse_name": STAGE_NAMES[key],
                    **summaries[key],
                    "updated_at": readback["active_version"]["effective_at"],
                    "status": _summary_status(lines, key, readback["sync"]),
                    "ff_reservations": ff_reservations if key == STAGE_FF else None,
                }
                for key in STAGES
            ],
            "total": _total_summary(summaries),
        }

    def warehouse_detail(self, warehouse_key: str) -> dict[str, Any]:
        if warehouse_key not in STAGES:
            raise WarehouseFunctionalError(f"unknown warehouse: {warehouse_key}")
        readback = self.readback()
        if readback.get("status") != "ready":
            return readback
        balances = _revalidate_balance_certifications(
            runtime=self.runtime,
            balances=[
                item for item in readback["balances"] if item["warehouse_key"] == warehouse_key
            ],
            active_version_id=str((readback.get("active_version") or {}).get("version_id") or ""),
        )
        summary = _summaries([_line_from_payload(item) for item in balances])[warehouse_key]
        names = self._nomenclature_names()
        documents = self._warehouse_documents(warehouse_key)
        ff_reservations = (
            dict(readback.get("ff_reservations") or {})
            if warehouse_key == STAGE_FF
            else {"by_nm": {}, "rows": []}
        )
        ff_reservations_by_nm = dict(ff_reservations.get("by_nm") or {})
        public_balances = []
        balance_nm_ids: set[int] = set()
        for item in balances:
            nm_id = int(item["nm_id"])
            balance_nm_ids.add(nm_id)
            identity = names.get(nm_id, {})
            quality_presentation = _warehouse_quality_presentation(item.get("quality"))
            cost_status_presentation = _warehouse_balance_status_presentation(
                (
                    "source_changed_provisional"
                    if item.get("certification_revalidation_failed")
                    else item.get("quality")
                ),
                certified=bool(item.get("certified")),
            )
            warning_parts = [cost_status_presentation["label_ru"]]
            if quality_presentation["label_ru"] != cost_status_presentation["label_ru"]:
                warning_parts.append(quality_presentation["label_ru"])
            identity_warning = str(identity.get("warning") or "")
            if identity_warning:
                warning_parts.append(identity_warning)
            public_balances.append(
                {
                    **item,
                    "line_id": f"{item['version_id']}:{warehouse_key}:{nm_id}",
                    "sku": identity.get("sku") or str(nm_id),
                    "nomenclature_name": identity.get("name") or "",
                    "barcode": identity.get("barcode") or "",
                    "identity_source": identity.get("source") or "nm_id",
                    "average_unit_cost_rub": item.get("wac_rub"),
                    "physical_quantity": item.get("quantity"),
                    "reserved_quantity": _text(
                        _decimal((ff_reservations_by_nm.get(str(nm_id)) or {}).get("quantity"))
                    ) if warehouse_key == STAGE_FF else "0",
                    "available_quantity": _text(
                        max(
                            _decimal(item.get("quantity"))
                            - _decimal((ff_reservations_by_nm.get(str(nm_id)) or {}).get("quantity")),
                            ZERO,
                        )
                    ) if warehouse_key == STAGE_FF else item.get("quantity"),
                    "unsecured_reservation_quantity": _text(
                        max(
                            _decimal((ff_reservations_by_nm.get(str(nm_id)) or {}).get("quantity"))
                            - _decimal(item.get("quantity")),
                            ZERO,
                        )
                    ) if warehouse_key == STAGE_FF else "0",
                    "reservation_supply_ids": list(
                        (ff_reservations_by_nm.get(str(nm_id)) or {}).get("supply_ids") or []
                    ),
                    "quality_presentation": quality_presentation,
                    "cost_status_presentation": cost_status_presentation,
                    "identity_warning": identity_warning,
                    "quality_tone": (
                        "warning" if identity_warning else cost_status_presentation["tone"]
                    ),
                    "human_evidence": _warehouse_human_evidence(
                        item.get("provenance"),
                        quantity=item.get("quantity"),
                        capital_rub=item.get("capital_rub"),
                        quality=item.get("quality"),
                    ),
                    "warning": " · ".join(warning_parts),
                }
            )
        if warehouse_key == STAGE_FF:
            for nm_key, reservation in sorted(
                ff_reservations_by_nm.items(),
                key=lambda item: int(item[0]),
            ):
                nm_id = int(nm_key)
                if nm_id in balance_nm_ids:
                    continue
                identity = names.get(nm_id, {})
                quantity = _text(_decimal(reservation.get("quantity")))
                public_balances.append(
                    {
                        "line_id": f"reservation:{nm_id}",
                        "version_id": str(readback["active_version"]["version_id"]),
                        "warehouse_key": STAGE_FF,
                        "nm_id": nm_id,
                        "sku": identity.get("sku") or str(nm_id),
                        "nomenclature_name": identity.get("name") or "",
                        "barcode": identity.get("barcode") or "",
                        "quantity": "0",
                        "physical_quantity": "0",
                        "reserved_quantity": quantity,
                        "available_quantity": "0",
                        "unsecured_reservation_quantity": quantity,
                        "capital_rub": "0",
                        "wac_rub": None,
                        "average_unit_cost_rub": None,
                        "certified": False,
                        "quality": "reservation_waiting_for_receipt",
                        "quality_tone": "warning",
                        "quality_presentation": {
                            "code": "reservation_waiting_for_receipt",
                            "label_ru": "Ожидает поступления",
                            "description_ru": "Товар зарезервирован для WB-поставки, но физически ещё не списан со склада FF.",
                            "tone": "warning",
                        },
                        "cost_status_presentation": {
                            "code": "reservation_waiting_for_receipt",
                            "label_ru": "Ожидает поступления",
                            "description_ru": "Резерв не входит в физический остаток и товарный капитал.",
                            "tone": "warning",
                        },
                        "reservation_supply_ids": list(reservation.get("supply_ids") or []),
                        "human_evidence": [],
                        "warning": "Ожидает поступления",
                    }
                )
        public_documents = []
        for item in documents:
            document_lines = []
            parent_quality = str((item.get("provenance") or {}).get("quality") or "")
            for line in item.get("lines") or []:
                nm_id = int(line["nm_id"])
                identity = names.get(nm_id, {})
                line_quality = str((line.get("provenance") or {}).get("quality") or parent_quality)
                document_lines.append(
                    {
                        **line,
                        "sku": identity.get("sku") or str(nm_id),
                        "nomenclature_name": identity.get("name") or "",
                        "barcode": identity.get("barcode") or "",
                        "average_unit_cost_rub": line.get("wac_rub"),
                        "quality_presentation": _warehouse_quality_presentation(line_quality),
                        "human_evidence": _warehouse_human_evidence(
                            line.get("provenance"),
                            quantity=line.get("quantity"),
                            capital_rub=line.get("capital_rub"),
                            quality=line_quality,
                            fallback_date=item.get("occurred_at"),
                        ),
                    }
                )
            document_type = str(item.get("document_type") or "")
            labels = {
                "functional_cutover": "Функциональный cutover",
                "warehouse_sync": "Почасовая версия склада",
                "wb_final_acceptance_discrepancy": "Расхождение финальной приёмки",
                "wb_doprinato": "Доприёмка WB",
                "wb_unmatched_doprinato_audit": "Неразнесённая доприёмка",
                "wb_pre_cutover_unmatched_audit": "Доприёмка до границы учёта",
            }
            directions = {
                "wb_final_acceptance_discrepancy": (STAGE_FF_TO_WB, STAGE_DISCREPANCY),
                "wb_doprinato": (STAGE_DISCREPANCY, STAGE_WB),
                "wb_unmatched_doprinato_audit": ("transitional_audit", "non_stock"),
                "wb_pre_cutover_unmatched_audit": ("pre_cutover_audit", "non_stock"),
            }
            warehouse_from, warehouse_to = directions.get(document_type, ("source", warehouse_key))
            quantity = _decimal(item.get("quantity"))
            capital = _decimal(item.get("capital_rub"))
            public_documents.append(
                {
                    **item,
                    "document_number": str(item.get("document_id") or ""),
                    "document_type_label": labels.get(document_type, document_type),
                    "warehouse_name": STAGE_NAMES[warehouse_key],
                    "warehouse_from_key": warehouse_from,
                    "warehouse_to_key": warehouse_to,
                    "source_basis": str(item.get("source_id") or ""),
                    "sku_count": len(document_lines),
                    "total_quantity": _text(quantity),
                    "total_cost_rub": _text(capital / quantity) if quantity != ZERO else None,
                    "total_capital_rub": _text(capital),
                    "status_label": "Аудит · не склад" if document_type in {"wb_unmatched_doprinato_audit", "wb_pre_cutover_unmatched_audit"} else "Проведено",
                    "human_evidence": _warehouse_human_evidence(
                        item.get("provenance"),
                        quantity=item.get("quantity"),
                        capital_rub=item.get("capital_rub"),
                        quality=(item.get("provenance") or {}).get("quality"),
                        fallback_date=item.get("occurred_at"),
                    ),
                    "lines": document_lines,
                }
            )
        status = _summary_status(balances, warehouse_key, readback["sync"])
        status_presentation = _warehouse_status_presentation(
            status=status,
            sync=readback["sync"],
        )
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready",
            "cutover": readback["cutover"],
            "active_version": readback["active_version"],
            "sync": readback["sync"],
            "sync_presentation": status_presentation,
            "warehouse": {
                "warehouse_key": warehouse_key,
                "warehouse_name": STAGE_NAMES[warehouse_key],
                **summary,
                "sku_count": summary["sku_count"],
                "total_quantity": summary["quantity"],
                "total_capital_rub": summary["capital_rub"],
                "average_unit_cost_rub": summary["wac_rub"],
                "updated_at": readback["active_version"]["effective_at"],
                "source_basis": "canonical functional warehouse projection",
                "status": status,
                "status_label": status_presentation["label_ru"],
                "status_description": status_presentation["description_ru"],
                "wb_contour": {
                    "quantity": summary["wb_quantity"],
                    "in_way_to_client": summary["wb_in_way_to_client"],
                    "in_way_from_client": summary["wb_in_way_from_client"],
                    "total": summary["quantity"],
                    "formula_ru": (
                        "Всего в контуре WB = На складах WB + В пути к покупателям "
                        "+ В пути возврата на WB."
                    ),
                } if warehouse_key == STAGE_WB else None,
                "ff_reservations": ff_reservations if warehouse_key == STAGE_FF else None,
            },
            "balances": public_balances,
            "documents": public_documents,
            "unmatched_doprinato": readback["unmatched_doprinato"] if warehouse_key == STAGE_DISCREPANCY else [],
            "document_type_catalog": [
                {"key": "wb_final_acceptance_discrepancy", "label": "Расхождение финальной приёмки", "enabled": True},
                {"key": "wb_doprinato", "label": "Доприёмка WB", "enabled": True},
                {"key": "wb_unmatched_doprinato_audit", "label": "Неразнесённая доприёмка", "enabled": True},
                {"key": "wb_pre_cutover_unmatched_audit", "label": "Доприёмка до границы учёта", "enabled": True},
                {"key": "wb_discrepancy_writeoff", "label": "Списание расхождения", "enabled": False},
            ] if warehouse_key == STAGE_DISCREPANCY else [],
            "legacy_ff_route": "/v1/sheet-vitrina-v1/supply/ff-stocks" if warehouse_key == STAGE_FF else None,
        }

    def _nomenclature_names(self) -> dict[int, dict[str, str]]:
        result: dict[int, dict[str, str]] = {}
        try:
            state = self.runtime.load_current_state()
        except Exception:
            state = None
        if state is not None:
            for item in state.config_v2:
                nm_id = int(item.nm_id)
                result[nm_id] = {
                    "sku": str(getattr(item, "sku", "") or getattr(item, "display_name", "") or nm_id),
                    "name": str(getattr(item, "display_name", "") or ""),
                    "barcode": str(getattr(item, "barcode", "") or ""),
                    "source": "config_v2",
                    "warning": "",
                }
        active_by_nm: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
        try:
            for item in self.runtime.list_nomenclature_items(active_only=True):
                nm_id = int(item.get("nm_id") or 0)
                if nm_id > 0:
                    active_by_nm[nm_id].append(dict(item))
        except Exception:
            active_by_nm = defaultdict(list)
        for nm_id, candidates in active_by_nm.items():
            identities = {
                (
                    str(item.get("our_sku") or item.get("vendor_code") or "").strip(),
                    str(item.get("nomenclature_name") or item.get("wb_title") or "").strip(),
                    str(item.get("barcode") or "").strip(),
                )
                for item in candidates
            }
            if len(identities) != 1:
                current = result.setdefault(
                    nm_id,
                    {"sku": str(nm_id), "name": "", "barcode": "", "source": "nm_id"},
                )
                current["warning"] = "Неоднозначная активная номенклатура — требуется проверка"
                continue
            sku, name, barcode = next(iter(identities))
            current = result.get(nm_id, {})
            result[nm_id] = {
                "sku": sku or str(current.get("sku") or nm_id),
                "name": name or str(current.get("name") or ""),
                "barcode": barcode or str(current.get("barcode") or ""),
                "source": "active_nomenclature_exact_nm_id",
                "warning": "",
            }
        return result

    def _warehouse_documents(self, warehouse_key: str) -> list[dict[str, Any]]:
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            rows = conn.execute(
                """SELECT document.*
                   FROM sheet_vitrina_v1_warehouse_functional_documents document
                   JOIN sheet_vitrina_v1_warehouse_functional_versions version
                     ON version.version_id=document.version_id
                   WHERE version.cutover_id=? AND document.warehouse_key=?
                   ORDER BY document.occurred_at DESC,document.created_at DESC,document.document_id
                   LIMIT 200""",
                (FUNCTIONAL_CUTOVER_ID, warehouse_key),
            ).fetchall()
            result = []
            for row in rows:
                item = _document_public(row)
                line_rows = conn.execute(
                    """SELECT * FROM sheet_vitrina_v1_warehouse_functional_document_lines
                       WHERE document_id=? ORDER BY nm_id,line_id""",
                    (row["document_id"],),
                ).fetchall()
                item["lines"] = [
                    {**dict(line), "provenance": _loads(line["provenance_json"], {})}
                    for line in line_rows
                ]
                result.append(item)
        return result

    def readback(self) -> dict[str, Any]:
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            cutover_row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_cutovers WHERE cutover_id=?",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            active = conn.execute(
                """SELECT version.* FROM sheet_vitrina_v1_warehouse_functional_active active
                   JOIN sheet_vitrina_v1_warehouse_functional_versions version
                     ON version.version_id=active.version_id WHERE active.slot=1"""
            ).fetchone()
            if cutover_row is None or active is None:
                return {
                    "contract_name": CONTRACT_NAME,
                    "contract_version": CONTRACT_VERSION,
                    "status": "not_initialized",
                    "cutover": None,
                    "balances": [],
                    "documents": [],
                }
            balances = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? ORDER BY warehouse_key,nm_id",
                (active["version_id"],),
            ).fetchall()]
            ff_reservation_rows = [
                dict(row)
                for row in conn.execute(
                    """SELECT supply_id,nm_id,quantity
                       FROM sheet_vitrina_v1_warehouse_functional_ff_reservations
                       WHERE version_id=? ORDER BY supply_id,nm_id""",
                    (active["version_id"],),
                ).fetchall()
            ]
            documents = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_documents WHERE version_id=? ORDER BY occurred_at,document_id",
                (active["version_id"],),
            ).fetchall()]
            unmatched = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_unmatched_doprinato WHERE version_id=? ORDER BY business_date,unmatched_id",
                (active["version_id"],),
            ).fetchall()]
            historical_cost = conn.execute(
                """SELECT MIN(as_of_date) date_from,MAX(as_of_date) date_to,
                          COUNT(DISTINCT as_of_date) day_count,COUNT(*) row_count,
                          SUM(CASE WHEN CAST(quantity AS NUMERIC)>0 AND CAST(wac_rub AS NUMERIC)<=0 THEN 1 ELSE 0 END) gap_count
                   FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE cutover_id=?""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            historical_dates = [
                str(row["as_of_date"])
                for row in conn.execute(
                    """SELECT DISTINCT as_of_date
                       FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                       WHERE cutover_id=? ORDER BY as_of_date""",
                    (FUNCTIONAL_CUTOVER_ID,),
                ).fetchall()
            ]
            historical_quantity_rows = [
                dict(row)
                for row in conn.execute(
                    """SELECT as_of_date,nm_id,quantity
                       FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                       WHERE cutover_id=? ORDER BY as_of_date,nm_id""",
                    (FUNCTIONAL_CUTOVER_ID,),
                ).fetchall()
            ]
            historical_corrections = [
                {
                    **dict(row),
                    "missing_dates": _loads(row["missing_dates_json"], []),
                    "row_fingerprints": _loads(row["row_fingerprints_json"], []),
                    "ready_snapshot_manifest": _loads(
                        row["ready_snapshot_manifest_json"], []
                    ),
                    "provenance": _loads(row["provenance_json"], {}),
                    "backup": _loads(row["backup_json"], {}),
                }
                for row in conn.execute(
                    """SELECT *
                       FROM sheet_vitrina_v1_warehouse_wb_daily_cost_corrections
                       WHERE cutover_id=? ORDER BY created_at,correction_id""",
                    (FUNCTIONAL_CUTOVER_ID,),
                ).fetchall()
            ]
            cutover_version = conn.execute(
                """SELECT version_id,effective_at FROM sheet_vitrina_v1_warehouse_functional_versions
                   WHERE cutover_id=? AND version_kind='functional_cutover'
                   ORDER BY created_at LIMIT 1""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            cutover_discrepancy_rows = (
                conn.execute(
                    """SELECT quantity,capital_rub FROM sheet_vitrina_v1_warehouse_functional_balances
                       WHERE version_id=? AND warehouse_key=?""",
                    (cutover_version["version_id"], STAGE_DISCREPANCY),
                ).fetchall()
                if cutover_version is not None
                else []
            )
            active_snapshot = conn.execute(
                """SELECT *
                   FROM sheet_vitrina_v1_warehouse_wb_snapshots
                   WHERE version_id=? ORDER BY created_at DESC LIMIT 1""",
                (active["version_id"],),
            ).fetchone()
            recent_versions = [
                dict(row)
                for row in conn.execute(
                    """SELECT version.version_id,version.version_kind,version.effective_at,
                              version.created_at,version.status,version.plan_fingerprint,
                              snapshot.snapshot_date,snapshot.snapshot_id
                       FROM sheet_vitrina_v1_warehouse_functional_versions version
                       LEFT JOIN sheet_vitrina_v1_warehouse_wb_snapshots snapshot
                         ON snapshot.version_id=version.version_id
                       WHERE version.cutover_id=?
                       ORDER BY version.created_at DESC,version.version_id DESC LIMIT 24""",
                    (FUNCTIONAL_CUTOVER_ID,),
                ).fetchall()
            ]
            sync = conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_wb_sync_status WHERE slot=1").fetchone()
        public_balances = [_balance_public(item) for item in balances]
        expected_historical_dates = _date_range(
            "2026-07-01",
            (
                str(active_snapshot["snapshot_date"])
                if active_snapshot is not None
                else business_date_from_timestamp(str(active["effective_at"]))
            ),
        )
        missing_historical_dates = sorted(
            set(expected_historical_dates) - set(historical_dates)
        )
        historical_public = dict(historical_cost) if historical_cost else {}
        value_gap_count = int(historical_public.get("gap_count") or 0)
        contour_quantities_by_date: dict[str, dict[str, str]] = {}
        for row in historical_quantity_rows:
            day = str(row.get("as_of_date") or "")[:10]
            if not day:
                continue
            day_values = contour_quantities_by_date.setdefault(day, {})
            day_values[f"SKU:{int(row.get('nm_id') or 0)}"] = _text(
                _decimal(row.get("quantity"))
            )
        for day_values in contour_quantities_by_date.values():
            day_values["TOTAL"] = _text(
                sum((_decimal(value) for value in day_values.values()), ZERO)
            )
        historical_public.update(
            {
                "expected_day_count": len(expected_historical_dates),
                "missing_day_count": len(missing_historical_dates),
                "missing_dates": missing_historical_dates,
                "value_gap_count": value_gap_count,
                "gap_count": value_gap_count + len(missing_historical_dates),
                "contour_quantities_by_date": contour_quantities_by_date,
            }
        )
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready",
            "cutover": _cutover_public(cutover_row),
            "active_version": _version_public(active),
            "sync": dict(sync) if sync else {},
            "recent_versions": recent_versions,
            "wb_snapshot": (
                _wb_snapshot_integrity(dict(active_snapshot))
                if active_snapshot is not None
                else {}
            ),
            "balances": public_balances,
            "documents": [_document_public(item) for item in documents],
            "unmatched_doprinato": [_unmatched_public(item) for item in unmatched],
            "historical_wb_cost_projection": historical_public,
            "historical_wb_cost_corrections": historical_corrections,
            "ff_reservations": _ff_reservation_public_state_from_snapshot(
                reservations=ff_reservation_rows,
                balances=[
                    item for item in public_balances if item["warehouse_key"] == STAGE_FF
                ],
            ),
            "cutover_opening_discrepancy": {
                "quantity": _text(sum((_decimal(row["quantity"]) for row in cutover_discrepancy_rows), ZERO)),
                "capital_rub": _text(sum((_decimal(row["capital_rub"]) for row in cutover_discrepancy_rows), ZERO)),
                "version_id": str(cutover_version["version_id"]) if cutover_version is not None else "",
                "effective_at": str(cutover_version["effective_at"]) if cutover_version is not None else "",
            },
            "reconciliation": {
                "warehouse_count": len(STAGES),
                "negative_balance_count": sum(
                    1 for item in public_balances if _decimal(item["quantity"]) < ZERO
                ),
                "positive_cost_gap_count": sum(
                    1
                    for item in public_balances
                    if _decimal(item["quantity"]) > ZERO
                    and (_decimal(item["capital_rub"]) <= ZERO or _optional_decimal(item["wac_rub"]) is None)
                ),
            },
        }

    def _capture_sources(
        self,
        *,
        captured_at: str | None,
        wb_payload: Mapping[str, Any],
        include_historical_correction: bool = False,
    ) -> dict[str, Any]:
        capture_started_at = captured_at or self.timestamp_factory()
        snapshot_business_date = str(wb_payload.get("snapshot_date") or "").strip()
        if not snapshot_business_date:
            snapshot_business_date = business_date_from_timestamp(capture_started_at)
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            conn.execute("BEGIN")
            sources = _source_rows(
                conn,
                recovery_end_date=snapshot_business_date,
                include_historical_correction=include_historical_correction,
            )
            conn.commit()
        # The version timestamp describes the completed coherent local capture,
        # not the instant before the (potentially slow) WB fetch or DB read.  A
        # midnight boundary therefore fails closed against snapshot_date.
        captured_at = captured_at or self.timestamp_factory()
        sources = _functional_local_source_view(sources)
        local_digest = "sha256:" + _hash(_guarded_local_sources(sources))
        wb_data = dict(wb_payload.get("data") or {})
        raw_rows = list(wb_data.get("raw_rows") or wb_data.get("rows") or [])
        wb_items = list(wb_payload.get("canonical_items") or [])
        wb_snapshot = {
            "snapshot_id": "wbsnap_" + _hash(
                {
                    "fetched_at": wb_data.get("fetched_at"),
                    "requested_nm_ids": wb_payload.get("requested_nm_ids"),
                    "rows_digest": wb_data.get("raw_rows_digest"),
                }
            )[:24],
            "fetched_at": str(wb_data.get("fetched_at") or captured_at),
            "snapshot_date": snapshot_business_date,
            "requested_nm_ids": list(wb_payload.get("requested_nm_ids") or []),
            "pagination_complete": bool(wb_data.get("pagination_complete")),
            "page_count": int(wb_data.get("page_count") or 0),
            "page_offsets": list(wb_data.get("page_offsets") or []),
            "raw_row_count": len(raw_rows),
            "raw_rows_digest": str(wb_data.get("raw_rows_digest") or ("sha256:" + _hash(raw_rows))),
            "raw_rows": raw_rows,
            "items": wb_items,
        }
        if not wb_snapshot["pagination_complete"]:
            raise WarehouseFunctionalError("official WB snapshot pagination is incomplete")
        supply_revisions = _supply_revisions(sources["wb_supplies"])
        return {
            **sources,
            "captured_at": captured_at,
            "local_source_digest": local_digest,
            "wb_supply_source_digest": "sha256:" + _hash(supply_revisions),
            "supply_revisions": supply_revisions,
            "wb_snapshot": wb_snapshot,
            "watermarks": {
                "captured_at": captured_at,
                "local_source_digest": local_digest,
                "supplier_shipments": _watermark(sources["shipments"], "updated_at"),
                "cny_ledger": _watermark(sources["cny_operations"], "updated_at"),
                "financial_documents": _watermark(sources["financial_documents"], "updated_at"),
                "nomenclature_purchase_prices": _watermark(
                    sources["nomenclature_purchase_prices"], "updated_at"
                ),
                "fulfillment_service_uploads": _watermark(
                    sources["fulfillment_service_uploads"], "updated_at"
                ),
                "ff_ledger": _watermark(sources["ff_operations"], "created_at"),
                "ff_auto_writeoff_checkpoint": _watermark(
                    sources["ff_auto_writeoff_checkpoint"], "created_at"
                ),
                "wb_supplies": _watermark(sources["wb_supplies"], "last_list_synced_at", "synced_at"),
                "wb_snapshot": {
                    "snapshot_id": wb_snapshot["snapshot_id"],
                    "fetched_at": wb_snapshot["fetched_at"],
                    "requested_count": len(wb_snapshot["requested_nm_ids"]),
                    "raw_row_count": wb_snapshot["raw_row_count"],
                    "digest": wb_snapshot["raw_rows_digest"],
                    "pagination_complete": True,
                },
            },
        }

    def _calculate_lines(
        self,
        *,
        capture: Mapping[str, Any],
        previous: Mapping[tuple[str, int], WarehouseLine],
        cutover: Mapping[str, Any] | None,
        cutover_mode: bool,
    ) -> tuple[
        list[WarehouseLine],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        captured_at = str(capture["captured_at"])
        wb_items = {int(item["nm_id"]): dict(item) for item in capture["wb_snapshot"]["items"]}
        target_nm_ids = set(wb_items)
        target_nm_ids.update(int(row["nm_id"]) for row in capture["primary_cost_rows"] if int(row["nm_id"] or 0) > 0)
        target_nm_ids.update(int(row["nm_id"]) for row in capture["ff_lines"] if int(row["nm_id"] or 0) > 0)
        target_nm_ids.update(
            int(row["nm_id"])
            for row in capture["historical_wb_daily_quantities"]
            if int(row.get("nm_id") or 0) > 0
        )
        for raw_supply in capture["wb_supplies"]:
            record = _normalized_wb_record(raw_supply)
            for good in _validated_wb_goods(record):
                target_nm_ids.add(int(good["nm_id"]))
        purchase_price = _nomenclature_purchase_prices(capture["nomenclature_purchase_prices"])
        if cutover_mode:
            cost_map = build_frozen_opening_cost_map(
                target_nm_ids=target_nm_ids,
                primary_rows=capture["primary_cost_rows"],
                purchase_price_by_nm=purchase_price,
                downstream_rows=capture["downstream_cost_rows"],
                primary_identity=capture["primary_identity"],
            )
        else:
            cost_map = self._load_opening_cost_map()

        buckets: defaultdict[tuple[str, int], dict[str, Any]] = defaultdict(
            lambda: {"quantity": ZERO, "capital": ZERO, "covered": ZERO, "quality": [], "provenance": []}
        )
        supplier_flow_costs: dict[tuple[str, int], tuple[Decimal, Decimal, str, dict[str, Any]]] = {}
        for shipment_id, allocation in _supplier_cost_allocations(capture).items():
            if allocation.get("blockers"):
                if str(allocation.get("first_payment_date") or ""):
                    blocker_codes = ",".join(
                        sorted(
                            str(item.get("code") or "unknown")
                            for item in allocation.get("blockers") or []
                        )
                    )
                    raise WarehouseFunctionalError(
                        "activated supplier shipment "
                        f"{shipment_id} has unavailable canonical cost proof: {blocker_codes}"
                    )
                continue
            stage = str(allocation["stage"])
            lines_by_nm: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
            for line in allocation.get("lines") or []:
                lines_by_nm[int(line["nm_id"])].append(dict(line))
            flow_id = _supplier_flow_id(shipment_id)
            for nm_id, line_rows in lines_by_nm.items():
                quantity = sum((_decimal(row["quantity"]) for row in line_rows), ZERO)
                capital = sum((_decimal(row["capital_rub"]) for row in line_rows), ZERO)
                if quantity <= ZERO or capital <= ZERO:
                    raise WarehouseFunctionalError(f"activated supplier flow {flow_id} has incomplete capital")
                quality = (
                    "certified"
                    if bool(allocation.get("expenses_complete"))
                    else "confirmed_payments_provisional_expenses"
                )
                payment_components = [
                    component
                    for row in line_rows
                    for component in row.get("components") or []
                    if component.get("component_key") == "supplier_payment"
                ]
                bank_fee_components = [
                    component
                    for row in line_rows
                    for component in row.get("components") or []
                    if component.get("component_key") == "bank_fee"
                ]
                china_components = [
                    component
                    for row in line_rows
                    for component in row.get("components") or []
                    if component.get("component_key") not in {"supplier_payment", "bank_fee"}
                ]
                flow_provenance = {
                    "supplier_flow_id": flow_id,
                    "shipment_id": shipment_id,
                    "invoice_no": str(allocation.get("invoice_no") or ""),
                    "invoice_date": str(allocation.get("invoice_date") or "")[:10],
                    "business_date": (
                        str(allocation.get("actual_shipment_date") or "")[:10]
                        if stage == STAGE_CHINA_TO_FF
                        else str(allocation.get("first_payment_date") or allocation.get("invoice_date") or "")[:10]
                    ),
                    "actual_shipment_date": str(allocation.get("actual_shipment_date") or "")[:10],
                    "flow_quantity": _text(quantity),
                    "flow_capital_rub": _text(capital),
                    "quality": quality,
                    "expenses_complete_certification": bool(allocation.get("expenses_complete")),
                    "source_fingerprint": str(allocation["source_fingerprint"]),
                    "calculation_fingerprint": str(allocation["calculation_fingerprint"]),
                    "certified_source_fingerprint": (
                        str(allocation["source_fingerprint"])
                        if bool(allocation.get("expenses_complete"))
                        else None
                    ),
                    "certified_calculation_fingerprint": (
                        str(allocation["calculation_fingerprint"])
                        if bool(allocation.get("expenses_complete"))
                        else None
                    ),
                    "payment_operation_ids": sorted(
                        {str(item["source_component_id"]).split(":", 1)[-1] for item in payment_components}
                    ),
                    "bank_fee_source_ids": sorted(
                        {str(item["source_component_id"]) for item in bank_fee_components}
                    ),
                    "china_expense_sources": sorted(
                        {str(item["source_component_id"]) for item in china_components}
                    ),
                    "allocation": "supplier/payment/bank fee by invoice value; logistics/1010 by quantity; 2010/5010 by invoice value",
                    "line_cost_breakdown": line_rows,
                    "conservation_controls": allocation.get("controls") or {},
                }
                supplier_flow_costs[(shipment_id, nm_id)] = (quantity, capital, quality, flow_provenance)
                if str(allocation.get("actual_ff_acceptance_date") or "")[:10]:
                    continue
                _add_bucket(
                    buckets,
                    stage=stage,
                    nm_id=nm_id,
                    quantity=quantity,
                    capital=capital,
                    covered=quantity,
                    quality=quality,
                    provenance=flow_provenance,
                )

        ff_qty: defaultdict[int, Decimal] = defaultdict(Decimal)
        for row in capture["ff_lines"]:
            ff_qty[int(row["nm_id"])] += _decimal(row.get("quantity_delta"))
        ff_outbound_wac_by_supply_nm: dict[tuple[str, int], Decimal] = {}
        if cutover_mode:
            for nm_id, quantity in ff_qty.items():
                if quantity < ZERO:
                    raise WarehouseFunctionalError(f"canonical FF ledger is negative for nmId {nm_id}")
                if quantity == ZERO:
                    continue
                seed = cost_map[nm_id]
                _add_bucket(
                    buckets,
                    stage=STAGE_FF,
                    nm_id=nm_id,
                    quantity=quantity,
                    capital=quantity * seed.ff_unit_cost,
                    covered=quantity,
                    quality=seed.quality,
                    provenance={"source": "canonical_append_only_ff_ledger_cutover_opening", **dict(seed.provenance)},
                )
        else:
            ff_pools: dict[int, dict[str, Any]] = {
                nm_id: {
                    "quantity": line.quantity,
                    "capital": line.capital,
                    "operations": [],
                    "opening_version_id": line.provenance.get("version_id") or "",
                }
                for nm_id, line in self._cutover_stage_lines(STAGE_FF).items()
            }
            ff_lines_by_operation: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for row in capture["ff_lines"]:
                ff_lines_by_operation[str(row.get("operation_id") or "")].append(row)
            boundary = str((cutover or {}).get("cutover_at") or "")
            for operation in capture["ff_operations"]:
                if str(operation.get("created_at") or "") <= boundary:
                    continue
                operation_id = str(operation.get("operation_id") or "")
                source_type = str(operation.get("source_type") or "")
                source_object_id = str(operation.get("source_object_id") or "")
                for raw_line in ff_lines_by_operation.get(operation_id, []):
                    nm_id = int(raw_line.get("nm_id") or 0)
                    delta = _decimal(raw_line.get("quantity_delta"))
                    if nm_id <= 0 or delta == ZERO:
                        continue
                    pool = ff_pools.setdefault(
                        nm_id,
                        {"quantity": ZERO, "capital": ZERO, "operations": [], "opening_version_id": ""},
                    )
                    current_qty = _decimal(pool["quantity"])
                    current_capital = _decimal(pool["capital"])
                    current_wac = current_capital / current_qty if current_qty > ZERO else None
                    if delta > ZERO:
                        if source_type == "supplier_shipment":
                            flow = supplier_flow_costs.get((source_object_id, nm_id))
                            if flow is None:
                                raise WarehouseFunctionalError(
                                    f"FF supplier receipt {operation_id}:{nm_id} has no exact supplier-flow capital"
                                )
                            flow_qty, flow_capital, _quality, flow_provenance = flow
                            inbound_wac = flow_capital / flow_qty
                            inbound_provenance = flow_provenance
                        else:
                            if current_wac is None:
                                seed = cost_map.get(nm_id)
                                if seed is None:
                                    raise WarehouseFunctionalError(
                                        f"positive FF adjustment {operation_id}:{nm_id} has no prior or source cost"
                                    )
                                current_wac = seed.ff_unit_cost
                            inbound_wac = current_wac
                            inbound_provenance = {
                                "quality": "current_wac_adjustment",
                                "reason": "non_supplier_positive_FF_ledger_operation",
                            }
                        pool["quantity"] = current_qty + delta
                        pool["capital"] = current_capital + delta * inbound_wac
                    else:
                        if current_wac is None:
                            raise WarehouseFunctionalError(
                                f"FF outbound {operation_id}:{nm_id} has no positive cost pool"
                            )
                        outbound = abs(delta)
                        if outbound > current_qty:
                            raise WarehouseFunctionalError(
                                f"canonical FF replay would be negative for nmId {nm_id} at {operation_id}"
                            )
                        pool["quantity"] = current_qty - outbound
                        pool["capital"] = current_capital - outbound * current_wac
                        inbound_wac = current_wac
                        inbound_provenance = {"quality": "proportional_wac_outbound"}
                        if source_type in {"wb_supply", "wb_supply_targeted_reconciliation"} and source_object_id:
                            ff_outbound_wac_by_supply_nm[(source_object_id, nm_id)] = current_wac
                    pool["operations"].append(
                        {
                            "operation_id": operation_id,
                            "created_at": operation.get("created_at"),
                            "source_type": source_type,
                            "source_object_id": source_object_id,
                            "quantity_delta": _text(delta),
                            "unit_cost_rub": _text(inbound_wac),
                            "source": inbound_provenance,
                        }
                    )
            for nm_id, expected_quantity in ff_qty.items():
                actual_quantity = _decimal((ff_pools.get(nm_id) or {}).get("quantity"))
                if expected_quantity < ZERO or actual_quantity != expected_quantity:
                    raise WarehouseFunctionalError(
                        f"canonical FF replay mismatch for nmId {nm_id}: {actual_quantity} != {expected_quantity}"
                    )
            for nm_id, pool in ff_pools.items():
                quantity = _decimal(pool["quantity"])
                capital = _decimal(pool["capital"])
                if quantity == ZERO:
                    continue
                if capital <= ZERO:
                    raise WarehouseFunctionalError(f"FF replay has no capital for nmId {nm_id}")
                _add_bucket(
                    buckets,
                    stage=STAGE_FF,
                    nm_id=nm_id,
                    quantity=quantity,
                    capital=capital,
                    covered=quantity,
                    quality="moving_weighted_average",
                    provenance={
                        "source": "canonical_append_only_ff_ledger_replay",
                        "cutover_opening": True,
                        "cutover_date": _business_date_value((cutover or {}).get("cutover_at")),
                        "opening_version_id": str(pool.get("opening_version_id") or ""),
                        "operations": pool["operations"],
                    },
                )

        downstream_components = _supply_downstream_component_index(capture["downstream_cost_rows"])
        active_reservations = _active_ff_reservation_index(
            capture.get("ff_reservation_operations") or [],
            capture.get("ff_reservation_lines") or [],
        )
        cutover_revisions = dict((cutover or {}).get("absorbed_supply_revisions") or {})
        discrepancy_receipts: list[dict[str, Any]] = []
        doprinato_rows: list[dict[str, Any]] = []
        transitional_unmatched: list[dict[str, Any]] = []
        new_events: list[dict[str, Any]] = []
        accepted_event_totals = self._accepted_event_totals()
        for raw in capture["wb_supplies"]:
            record = _normalized_wb_record(raw)
            supply_id = str(record.get("supply_id") or "")
            revision = _supply_revision(raw)
            absorbed = cutover_revisions.get(supply_id) == revision
            status_id = int(record.get("status_id") or 0)
            is_doprinato = _is_doprinato(record)
            for good in _validated_wb_goods(record):
                nm_id = int(good["nm_id"])
                packed = _decimal(good.get("quantity"))
                accepted = _decimal(good.get("accepted_quantity"))
                wb_supply_id = str(record.get("wb_supply_id") or "")
                provenance = {
                    "supply_id": supply_id,
                    "wb_supply_id": str(record.get("wb_supply_id") or ""),
                    "source_revision": revision,
                    "status_id": status_id,
                    "packed_quantity": _text(packed),
                    "accepted_quantity": _text(accepted),
                }
                business_date = _supply_business_date(record, raw)
                before_boundary = bool(
                    cutover
                    and business_date
                    and business_date < business_date_from_timestamp(str(cutover["cutover_at"]))
                )
                needs_supply_cost = bool(
                    not is_doprinato
                    and (
                        status_id in WB_POST_SHIPMENT_GATE_STATUS_IDS
                        or (
                            status_id == WB_FINAL_ACCEPTED_STATUS_ID
                            and not cutover_mode
                            and not absorbed
                            and not before_boundary
                        )
                    )
                )
                reservation_quantity = active_reservations.get((supply_id, nm_id), ZERO)
                if reservation_quantity == ZERO and wb_supply_id:
                    reservation_quantity = active_reservations.get((wb_supply_id, nm_id), ZERO)
                outbound_ff_wac = ff_outbound_wac_by_supply_nm.get((supply_id, nm_id))
                if outbound_ff_wac is None:
                    outbound_ff_wac = ff_outbound_wac_by_supply_nm.get((wb_supply_id, nm_id))
                reservation_only = bool(
                    needs_supply_cost
                    and outbound_ff_wac is None
                    and reservation_quantity >= packed
                    and packed > ZERO
                )
                if reservation_only:
                    provenance.update(
                        {
                            "reservation_only": True,
                            "reservation_quantity": _text(reservation_quantity),
                            "reservation_status": "waiting_for_goods_or_validated_costs",
                            "physical_movement": False,
                        }
                    )
                    needs_supply_cost = False
                accepted_cost = ZERO
                pre_acceptance_cost = ZERO
                if needs_supply_cost:
                    component = downstream_components.get((wb_supply_id, nm_id))
                    if component is None:
                        component = downstream_components.get((supply_id, nm_id))
                    if component is None:
                        raise WarehouseFunctionalError(
                            f"WB supply {supply_id}:{nm_id} has no validated downstream cost state"
                        )
                    if outbound_ff_wac is None and (cutover_mode or absorbed):
                        seed = cost_map.get(nm_id)
                        outbound_ff_wac = seed.ff_unit_cost if seed is not None else None
                    if outbound_ff_wac is None or outbound_ff_wac <= ZERO:
                        raise WarehouseFunctionalError(
                            f"WB supply {supply_id}:{nm_id} has no FF WAC at ledger debit"
                        )
                    pre_acceptance_cost, accepted_cost = compose_supply_costs(
                        outbound_ff_wac=outbound_ff_wac,
                        pre_acceptance_addon=component["pre_acceptance_addon"],
                        acceptance_addon=component["acceptance_addon"],
                    )
                    provenance.update(
                        {
                            "ff_wac_at_ledger_debit_rub": _text(outbound_ff_wac),
                            "downstream_pre_acceptance_addon_rub": _text(component["pre_acceptance_addon"]),
                            "wb_paid_acceptance_addon_rub": _text(component["acceptance_addon"]),
                            "downstream_cost_layer_fingerprint": component["inputs_hash"],
                        }
                    )
                source_fingerprint = _hash(
                    {
                        "revision": revision,
                        "nm_id": nm_id,
                        "good": good,
                        "accepted_unit_cost_rub": _text(accepted_cost),
                        "pre_acceptance_unit_cost_rub": _text(pre_acceptance_cost),
                    }
                )
                source_id = f"{supply_id}:{nm_id}"
                if not cutover_mode and not absorbed and before_boundary:
                    audit_quantity = (
                        accepted if is_doprinato and accepted > ZERO
                        else packed if is_doprinato
                        else max(packed - accepted, ZERO)
                    )
                    if audit_quantity > ZERO:
                        transitional_unmatched.append(
                            {
                                "source_id": source_id,
                                "source_fingerprint": source_fingerprint,
                                "business_date": business_date,
                                "nm_id": nm_id,
                                "quantity": _text(audit_quantity),
                                "matched_quantity": "0",
                                "reason": "pre_cutover_business_state_discovered_late",
                                "provenance": {
                                    **provenance,
                                    "non_stock_audit": True,
                                    "source_kind": "doprinato" if is_doprinato else "final_acceptance_discrepancy",
                                },
                            }
                        )
                    continue
                if reservation_only:
                    continue
                if status_id in WB_POST_SHIPMENT_GATE_STATUS_IDS and not is_doprinato:
                    open_qty = max(packed - accepted, ZERO)
                    if open_qty > ZERO:
                        _add_bucket(
                            buckets,
                            stage=STAGE_FF_TO_WB,
                            nm_id=nm_id,
                            quantity=open_qty,
                            capital=open_qty * pre_acceptance_cost,
                            covered=open_qty,
                            quality="supply_specific_downstream_cost",
                            provenance={
                                **provenance,
                                "formula": "max(packed-accepted,0)",
                                "business_date": business_date,
                                "pre_acceptance_unit_cost_rub": _text(pre_acceptance_cost),
                                "flow_quantity": _text(open_qty),
                                "flow_capital_rub": _text(open_qty * pre_acceptance_cost),
                            },
                        )
                    continue
                if cutover_mode or absorbed:
                    continue
                if is_doprinato:
                    quantity = accepted if accepted > ZERO else packed
                    doprinato_rows.append(
                        {
                            "source_id": source_id,
                            "source_fingerprint": source_fingerprint,
                            "business_date": business_date,
                            "nm_id": nm_id,
                            "quantity": _text(quantity),
                            "reason": "pre_cutover_business_state_discovered_late" if before_boundary else "",
                            "provenance": provenance,
                        }
                    )
                elif status_id == WB_FINAL_ACCEPTED_STATUS_ID:
                    quantity = max(packed - accepted, ZERO)
                    if quantity > ZERO:
                        discrepancy_receipts.append(
                            {
                                "source_id": source_id,
                                "source_fingerprint": source_fingerprint,
                                "business_date": business_date,
                                "nm_id": nm_id,
                                "quantity": _text(quantity),
                                "capital": _text(quantity * pre_acceptance_cost),
                                "wac": _text(pre_acceptance_cost),
                                "provenance": {**provenance, "paid_acceptance_excluded": True},
                            }
                        )
                    previous_event = accepted_event_totals.get(
                        (source_id, nm_id),
                        {"quantity": ZERO, "capital": ZERO},
                    )
                    previously_posted = _decimal(previous_event["quantity"])
                    previously_posted_capital = _decimal(previous_event["capital"])
                    accepted_delta = accepted_quantity_delta(
                        packed=packed,
                        accepted=accepted,
                        previously_posted=previously_posted,
                    )
                    cumulative_accepted = previously_posted + accepted_delta
                    accepted_capital_delta_value = accepted_capital_delta(
                        packed=packed,
                        accepted=accepted,
                        unit_cost=accepted_cost,
                        previously_posted_capital=previously_posted_capital,
                    )
                    target_accepted_capital = previously_posted_capital + accepted_capital_delta_value
                    quantity_capital_delta = accepted_delta * accepted_cost
                    cost_correction_delta = accepted_capital_delta_value - quantity_capital_delta
                    previous_wb_quantity = (
                        previous[(STAGE_WB, nm_id)].quantity
                        if (STAGE_WB, nm_id) in previous
                        else ZERO
                    )
                    retained_ratio = (
                        min(previous_wb_quantity / previously_posted, Decimal("1"))
                        if previously_posted > ZERO
                        else ZERO
                    )
                    current_pool_capital_delta = (
                        quantity_capital_delta + cost_correction_delta * retained_ratio
                    )
                    event_key = ("wb_final_acceptance", source_fingerprint, nm_id, _text(accepted_delta))
                    if not before_boundary and (accepted_delta != ZERO or accepted_capital_delta_value != ZERO):
                        event_id = "whfe_" + _hash(event_key)[:24]
                        new_events.append(
                            {
                                "event_id": event_id,
                                "event_type": "wb_final_acceptance",
                                "source_id": source_id,
                                "source_fingerprint": source_fingerprint,
                                "business_date": business_date,
                                "nm_id": nm_id,
                                "quantity": _text(accepted_delta),
                                "capital_rub": _text(accepted_capital_delta_value),
                                "provenance": {
                                    **provenance,
                                    "cumulative_accepted_quantity": _text(cumulative_accepted),
                                    "previously_posted_accepted_quantity": _text(previously_posted),
                                    "accepted_quantity_delta": _text(accepted_delta),
                                    "previously_posted_accepted_capital_rub": _text(previously_posted_capital),
                                    "target_accepted_capital_rub": _text(target_accepted_capital),
                                    "accepted_capital_delta_rub": _text(accepted_capital_delta_value),
                                    "current_pool_retained_ratio": _text(retained_ratio),
                                    "current_pool_capital_delta_rub": _text(current_pool_capital_delta),
                                    "source_correction": accepted_delta < ZERO,
                                    "cost_source_correction": accepted_capital_delta_value != accepted_delta * accepted_cost,
                                },
                            }
                        )

        doprinato_audit: list[dict[str, Any]] = []
        discrepancy_balances, unmatched = reconcile_discrepancies(
            discrepancies=discrepancy_receipts,
            doprinato=doprinato_rows,
            audit=doprinato_audit,
        )
        unmatched.extend(transitional_unmatched)
        if cutover_mode and discrepancy_balances:
            raise WarehouseFunctionalError("functional cutover discrepancy opening must be zero")
        for item in discrepancy_balances:
            _add_bucket(
                buckets,
                stage=STAGE_DISCREPANCY,
                nm_id=int(item["nm_id"]),
                quantity=_decimal(item["quantity"]),
                capital=_decimal(item["capital"]),
                covered=_decimal(item["quantity"]),
                quality="pooled_final_acceptance_discrepancy",
                provenance={
                    "receipts": item["receipts"],
                    "doprinato_matches": item["matches"],
                    "paid_acceptance_excluded": True,
                },
            )

        inbound_by_nm: defaultdict[int, tuple[Decimal, Decimal]] = defaultdict(lambda: (ZERO, ZERO))
        for event in new_events:
            qty, capital = inbound_by_nm[int(event["nm_id"])]
            inbound_by_nm[int(event["nm_id"])] = (
                qty + _decimal(event["quantity"]),
                capital
                + _decimal(
                    (event.get("provenance") or {}).get("current_pool_capital_delta_rub")
                    if isinstance(event.get("provenance"), Mapping)
                    else event["capital_rub"]
                ),
            )
        for nm_id, item in wb_items.items():
            physical = _decimal(item.get("quantity"))
            to_client = _decimal(item.get("in_way_to_client"))
            from_client = _decimal(item.get("in_way_from_client"))
            contour = physical + to_client + from_client
            if contour == ZERO:
                continue
            if cutover_mode:
                wac = cost_map[nm_id].wb_unit_cost
                quality = cost_map[nm_id].quality
                provenance = dict(cost_map[nm_id].provenance)
            else:
                previous_line = previous.get((STAGE_WB, nm_id))
                previous_qty = previous_line.quantity if previous_line else ZERO
                previous_capital = previous_line.capital if previous_line else ZERO
                inbound_qty, inbound_capital = inbound_by_nm[nm_id]
                _, _, rolled = roll_periodic_wac(
                    quantity=previous_qty,
                    capital=previous_capital,
                    quantity_delta=inbound_qty,
                    capital_delta=inbound_capital,
                )
                if rolled is None:
                    seed = cost_map.get(nm_id)
                    if seed is None:
                        raise WarehouseFunctionalError(
                            f"official WB contour {nm_id} has neither prior nor inbound cost"
                        )
                    wac = seed.wb_unit_cost
                else:
                    wac = rolled
                quality = "periodic_snapshot_wac"
                provenance = {
                    "previous_quantity": _text(previous_qty),
                    "previous_capital": _text(previous_capital),
                    "new_accepted_quantity": _text(inbound_qty),
                    "new_accepted_capital": _text(inbound_capital),
                    "last_valid_wac_fallback": rolled is None,
                }
            _add_bucket(
                buckets,
                stage=STAGE_WB,
                nm_id=nm_id,
                quantity=contour,
                capital=contour * wac,
                covered=contour,
                quality=quality,
                provenance={
                    "source": "official_wb_snapshot",
                    "snapshot_id": capture["wb_snapshot"]["snapshot_id"],
                    "snapshot_date": capture["wb_snapshot"]["snapshot_date"],
                    "fetched_at": capture["wb_snapshot"]["fetched_at"],
                    **provenance,
                },
                wb_quantity=physical,
                wb_to_client=to_client,
                wb_from_client=from_client,
            )

        lines = [_bucket_line(key, value) for key, value in sorted(buckets.items()) if value["quantity"] > ZERO]
        opening_payload = [
            {
                "nm_id": seed.nm_id,
                "ff_unit_cost_rub": _text(seed.ff_unit_cost),
                "wb_unit_cost_rub": _text(seed.wb_unit_cost),
                "quality": seed.quality,
                "provenance": dict(seed.provenance),
                "fingerprint": "sha256:" + _hash(
                    {
                        "nm_id": seed.nm_id,
                        "ff": _text(seed.ff_unit_cost),
                        "wb": _text(seed.wb_unit_cost),
                        "quality": seed.quality,
                        "provenance": seed.provenance,
                    }
                ),
            }
            for seed in cost_map.values()
        ]
        movement_documents = [
            {
                "document_type": "wb_final_acceptance_discrepancy",
                "warehouse_key": STAGE_DISCREPANCY,
                "occurred_at": str(item.get("business_date") or captured_at),
                "source_id": str(item.get("source_id") or ""),
                "source_fingerprint": str(item.get("source_fingerprint") or ""),
                "quantity": str(item["quantity"]),
                "capital_rub": str(item["capital"]),
                "provenance": dict(item.get("provenance") or {}),
                "lines": [
                    {
                        "nm_id": int(item["nm_id"]),
                        "quantity": str(item["quantity"]),
                        "wac_rub": str(item["wac"]),
                        "capital_rub": str(item["capital"]),
                        "provenance": dict(item.get("provenance") or {}),
                    }
                ],
            }
            for item in discrepancy_receipts
        ]
        for item in doprinato_audit:
            matched = _decimal(item.get("matched_quantity"))
            unmatched_quantity = _decimal(item.get("unmatched_quantity"))
            if matched > ZERO:
                matched_capital = _decimal(item.get("matched_capital_rub"))
                movement_documents.append(
                    {
                        "document_type": "wb_doprinato",
                        "warehouse_key": STAGE_DISCREPANCY,
                        "occurred_at": str(item.get("business_date") or captured_at),
                        "source_id": str(item.get("source_id") or ""),
                        "source_fingerprint": str(item.get("source_fingerprint") or ""),
                        "quantity": _text(-matched),
                        "capital_rub": _text(-matched_capital),
                        "provenance": {**dict(item.get("provenance") or {}), "pooled_by_sku": True},
                        "lines": [
                            {
                                "nm_id": int(item["nm_id"]),
                                "quantity": _text(-matched),
                                "wac_rub": str(item.get("matched_wac_rub") or ""),
                                "capital_rub": _text(-matched_capital),
                                "provenance": {**dict(item.get("provenance") or {}), "pooled_by_sku": True},
                            }
                        ],
                    }
                )
            if unmatched_quantity > ZERO:
                movement_documents.append(
                    {
                        "document_type": "wb_unmatched_doprinato_audit",
                        "warehouse_key": STAGE_DISCREPANCY,
                        "occurred_at": str(item.get("business_date") or captured_at),
                        "source_id": str(item.get("source_id") or ""),
                        "source_fingerprint": str(item.get("source_fingerprint") or ""),
                        "quantity": _text(unmatched_quantity),
                        "capital_rub": "0",
                        "provenance": {
                            **dict(item.get("provenance") or {}),
                            "non_stock_audit": True,
                            "reason": str(item.get("reason") or "no_positive_discrepancy_for_sku"),
                        },
                        "lines": [
                            {
                                "nm_id": int(item["nm_id"]),
                                "quantity": _text(unmatched_quantity),
                                "wac_rub": None,
                                "capital_rub": "0",
                                "provenance": {"non_stock_audit": True},
                            }
                        ],
                    }
                )
        for item in transitional_unmatched:
            movement_documents.append(
                {
                    "document_type": "wb_pre_cutover_unmatched_audit",
                    "warehouse_key": STAGE_DISCREPANCY,
                    "occurred_at": str(item.get("business_date") or captured_at),
                    "source_id": str(item.get("source_id") or ""),
                    "source_fingerprint": str(item.get("source_fingerprint") or ""),
                    "quantity": str(item["quantity"]),
                    "capital_rub": "0",
                    "provenance": dict(item.get("provenance") or {}),
                    "lines": [
                        {
                            "nm_id": int(item["nm_id"]),
                            "quantity": str(item["quantity"]),
                            "wac_rub": None,
                            "capital_rub": "0",
                            "provenance": dict(item.get("provenance") or {}),
                        }
                    ],
                }
            )
        return lines, unmatched, new_events, opening_payload, movement_documents

    def _load_opening_cost_map(self) -> dict[int, CostSeed]:
        with _connect(self.runtime.db_path) as conn:
            raw_rows = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_opening_cost_map WHERE cutover_id=? ORDER BY nm_id",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchall()
            rows = overlay_opening_cost_rows(
                conn,
                (
                    {
                        **dict(row),
                        "provenance": _loads(row["provenance_json"], {}),
                    }
                    for row in raw_rows
                ),
            )
        return {
            int(row["nm_id"]): CostSeed(
                nm_id=int(row["nm_id"]),
                ff_unit_cost=_decimal(row["ff_unit_cost_rub"]),
                wb_unit_cost=_decimal(row["wb_unit_cost_rub"]),
                quality=str(row["quality"]),
                provenance=dict(row.get("provenance") or {}),
            )
            for row in rows
        }

    def _build_post_cutover_daily_cost_projection(
        self,
        *,
        captured_at: str,
        candidate_lines: Iterable[WarehouseLine],
        candidate_snapshot: Mapping[str, Any],
        new_events: Iterable[Mapping[str, Any]],
        opening_cost_map: Iterable[Mapping[str, Any]],
        cutover_mode: bool,
    ) -> list[dict[str, Any]]:
        """Replay versioned post-cutover WB WAC through the current snapshot day.

        Snapshot quantities are periodic physical evidence.  Accepted supply
        events contribute signed quantity/capital layers on their effective
        business date, so a late expense or accepted-quantity correction
        deterministically rewrites only the derived daily cost history.
        """

        current_date = str(candidate_snapshot.get("snapshot_date") or "")[:10]
        if len(current_date) != 10:
            current_date = business_date_from_timestamp(captured_at)
        opening_cost_rows = [dict(item) for item in opening_cost_map]
        seed_wac = {
            int(item["nm_id"]): _decimal(item["wb_unit_cost_rub"])
            for item in opening_cost_rows
            if int(item.get("nm_id") or 0) > 0
        }
        seed_meta = {
            int(item["nm_id"]): {
                "quality": str(item.get("quality") or ""),
                "provenance": dict(item.get("provenance") or {}),
            }
            for item in opening_cost_rows
            if int(item.get("nm_id") or 0) > 0
        }
        estimate_seed_wac = {
            int(item["nm_id"]): _decimal(item["wb_unit_cost_rub"])
            for item in opening_cost_rows
            if int(item.get("nm_id") or 0) > 0
            and str(item.get("quality") or "")
            == BUSINESS_APPROVED_ARCHIVAL_ESTIMATE_QUALITY
        }
        candidate_quantities = _wb_snapshot_quantities(candidate_snapshot.get("items") or [])
        candidate_wac = {
            item.nm_id: item.wac
            for item in candidate_lines
            if item.warehouse_key == STAGE_WB and item.wac is not None
        }
        if cutover_mode:
            rows = []
            for nm_id in sorted(set(seed_wac) | set(candidate_quantities) | set(candidate_wac)):
                quantity = candidate_quantities.get(nm_id, ZERO)
                wac = candidate_wac.get(nm_id) or seed_wac.get(nm_id)
                if wac is None or wac <= ZERO:
                    if quantity > ZERO:
                        raise WarehouseFunctionalError(
                            f"cutover WB snapshot has no daily WAC for nmId {nm_id}"
                        )
                    continue
                rows.append(
                    _daily_wb_cost_row(
                        day=current_date,
                        nm_id=nm_id,
                        quantity=quantity,
                        wac=wac,
                        quality="periodic_snapshot_wac_provisional",
                        provenance={
                            "source": "functional_cutover_official_wb_snapshot",
                            "snapshot_id": str(candidate_snapshot.get("snapshot_id") or ""),
                            "opening_cost": seed_meta.get(nm_id, {}),
                            "last_valid_wac_retained": quantity == ZERO,
                        },
                    )
                )
            return rows

        with _connect(self.runtime.db_path) as conn:
            cutover = conn.execute(
                "SELECT cutover_at FROM sheet_vitrina_v1_warehouse_functional_cutovers WHERE cutover_id=?",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            cutover_version = conn.execute(
                """SELECT version_id,effective_at,created_at
                   FROM sheet_vitrina_v1_warehouse_functional_versions
                   WHERE cutover_id=? AND version_kind='functional_cutover' AND status='good'
                   ORDER BY created_at LIMIT 1""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            version_snapshots = conn.execute(
                """SELECT version.version_id,version.effective_at,version.created_at,snapshot.items_json,
                          snapshot.snapshot_id,snapshot.snapshot_date
                   FROM sheet_vitrina_v1_warehouse_functional_versions version
                   JOIN sheet_vitrina_v1_warehouse_wb_snapshots snapshot
                     ON snapshot.version_id=version.version_id
                   WHERE version.cutover_id=? AND version.status='good'
                   ORDER BY version.effective_at,version.created_at,version.version_id""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchall()
            opening_rows = (
                conn.execute(
                    """SELECT nm_id,quantity,wac_rub FROM sheet_vitrina_v1_warehouse_functional_balances
                       WHERE version_id=? AND warehouse_key=? ORDER BY nm_id""",
                    (cutover_version["version_id"], STAGE_WB),
                ).fetchall()
                if cutover_version is not None
                else []
            )
            persisted_events = conn.execute(
                """SELECT event_id,business_date,nm_id,quantity,capital_rub,source_id,
                          source_fingerprint,provenance_json
                   FROM sheet_vitrina_v1_warehouse_functional_events
                   WHERE event_type='wb_final_acceptance'
                   ORDER BY business_date,created_at,event_id"""
            ).fetchall()
        if cutover is None or cutover_version is None:
            raise WarehouseFunctionalError("functional daily WAC replay has no cutover baseline")
        cutover_date = business_date_from_timestamp(str(cutover["cutover_at"]))
        if current_date < cutover_date:
            raise WarehouseFunctionalError("functional daily WAC replay date precedes cutover")

        opening_quantity = {int(row["nm_id"]): _decimal(row["quantity"]) for row in opening_rows}
        opening_wac = {
            int(row["nm_id"]): _decimal(row["wac_rub"])
            for row in opening_rows
            if row["wac_rub"] not in (None, "")
        }
        if not seed_wac:
            loaded_seeds = self._load_opening_cost_map()
            seed_wac = {nm_id: seed.wb_unit_cost for nm_id, seed in loaded_seeds.items()}
            seed_meta = {
                nm_id: {"quality": seed.quality, "provenance": dict(seed.provenance)}
                for nm_id, seed in loaded_seeds.items()
            }

        snapshots_by_day: dict[str, dict[str, Any]] = {}
        for row in version_snapshots:
            day = str(row["snapshot_date"] or "")[:10]
            if len(day) != 10:
                day = business_date_from_timestamp(str(row["effective_at"]))
            if not cutover_date <= day <= current_date:
                continue
            snapshots_by_day[day] = {
                "quantities": _wb_snapshot_quantities(_loads(row["items_json"], [])),
                "snapshot_id": str(row["snapshot_id"]),
                "version_id": str(row["version_id"]),
            }
        snapshots_by_day[current_date] = {
            "quantities": candidate_quantities,
            "snapshot_id": str(candidate_snapshot.get("snapshot_id") or ""),
            "version_id": "candidate",
        }

        events_by_day: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        seen_event_ids: set[str] = set()
        for row in persisted_events:
            item = dict(row)
            item["provenance"] = _loads(item.pop("provenance_json"), {})
            event_id = str(item.get("event_id") or "")
            seen_event_ids.add(event_id)
            day = str(item.get("business_date") or "")[:10]
            if not cutover_date <= day <= current_date:
                raise WarehouseFunctionalError(
                    f"functional acceptance event {event_id} has invalid replay date {day!r}"
                )
            events_by_day[day].append(item)
        for raw in new_events:
            item = dict(raw)
            event_id = str(item.get("event_id") or "")
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            day = str(item.get("business_date") or "")[:10]
            if not cutover_date <= day <= current_date:
                raise WarehouseFunctionalError(
                    f"new functional acceptance event {event_id} has invalid replay date {day!r}"
                )
            events_by_day[day].append(item)

        target_nm_ids = set(seed_wac) | set(opening_quantity) | set(candidate_quantities)
        for snapshot in snapshots_by_day.values():
            target_nm_ids.update(snapshot["quantities"])
        for rows in events_by_day.values():
            target_nm_ids.update(int(item.get("nm_id") or 0) for item in rows)
        target_nm_ids.discard(0)

        previous_quantity = {
            nm_id: opening_quantity.get(nm_id, ZERO) for nm_id in target_nm_ids
        }
        previous_wac = {
            nm_id: estimate_seed_wac.get(nm_id)
            or opening_wac.get(nm_id)
            or seed_wac.get(nm_id)
            for nm_id in target_nm_ids
        }
        previous_quality = {
            nm_id: (
                BUSINESS_APPROVED_ARCHIVAL_ESTIMATE_QUALITY
                if nm_id in estimate_seed_wac
                else ""
            )
            for nm_id in target_nm_ids
        }
        result: list[dict[str, Any]] = []
        for day in _date_range(cutover_date, current_date):
            snapshot = snapshots_by_day.get(day)
            event_groups: defaultdict[int, list[Mapping[str, Any]]] = defaultdict(list)
            for event in events_by_day.get(day, []):
                event_groups[int(event.get("nm_id") or 0)].append(event)
            for nm_id in sorted(target_nm_ids):
                prior_qty = previous_quantity.get(nm_id, ZERO)
                prior_wac = previous_wac.get(nm_id)
                event_rows = event_groups.get(nm_id, [])
                quantity_delta = sum((_decimal(item.get("quantity")) for item in event_rows), ZERO)
                capital_delta = sum((_decimal(item.get("capital_rub")) for item in event_rows), ZERO)
                if quantity_delta != ZERO or capital_delta != ZERO:
                    if prior_wac is None:
                        if quantity_delta <= ZERO or capital_delta <= ZERO:
                            raise WarehouseFunctionalError(
                                f"daily WB replay has no opening WAC for correction {day}:{nm_id}"
                            )
                        rolled_qty = quantity_delta
                        rolled_capital = capital_delta
                    else:
                        rolled_qty = prior_qty + quantity_delta
                        rolled_capital = prior_qty * prior_wac + capital_delta
                    if rolled_qty < ZERO or rolled_capital < ZERO:
                        raise WarehouseFunctionalError(
                            f"daily WB replay correction makes pool negative for {day}:{nm_id}"
                        )
                    if rolled_qty > ZERO:
                        if rolled_capital <= ZERO:
                            raise WarehouseFunctionalError(
                                f"daily WB replay loses positive capital for {day}:{nm_id}"
                            )
                        prior_wac = rolled_capital / rolled_qty
                    previous_quality[nm_id] = "periodic_snapshot_wac_closed"
                quantity = (
                    snapshot["quantities"].get(nm_id, ZERO)
                    if snapshot is not None
                    else prior_qty
                )
                if prior_wac is None or prior_wac <= ZERO:
                    if quantity > ZERO:
                        raise WarehouseFunctionalError(
                            f"daily WB snapshot has no WAC for {day}:{nm_id}"
                        )
                    continue
                quality = (
                    BUSINESS_APPROVED_ARCHIVAL_ESTIMATE_QUALITY
                    if previous_quality.get(nm_id)
                    == BUSINESS_APPROVED_ARCHIVAL_ESTIMATE_QUALITY
                    else "periodic_snapshot_wac_provisional"
                    if day == current_date
                    else "periodic_snapshot_wac_closed"
                )
                result.append(
                    _daily_wb_cost_row(
                        day=day,
                        nm_id=nm_id,
                        quantity=quantity,
                        wac=prior_wac,
                        quality=quality,
                        provenance={
                            "source": "versioned_functional_wb_daily_replay",
                            "snapshot_id": str((snapshot or {}).get("snapshot_id") or "carried_last_good"),
                            "snapshot_version_id": str((snapshot or {}).get("version_id") or "carried_last_good"),
                            "previous_snapshot_quantity": _text(prior_qty),
                            "accepted_quantity_delta": _text(quantity_delta),
                            "accepted_capital_delta_rub": _text(capital_delta),
                            "accepted_event_ids": [str(item.get("event_id") or "") for item in event_rows],
                            "opening_cost": seed_meta.get(nm_id, {}),
                            "last_valid_wac_retained": quantity == ZERO,
                            "snapshot_carried_forward": snapshot is None,
                        },
                    )
                )
                previous_quantity[nm_id] = quantity
                previous_wac[nm_id] = prior_wac
                previous_quality[nm_id] = quality
        return result

    def _active_version_id(self, *, connection: sqlite3.Connection | None = None) -> str:
        if connection is not None:
            row = connection.execute(
                "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
            ).fetchone()
            return str(row["version_id"]) if row is not None else ""
        with _connect(self.runtime.db_path) as conn:
            return self._active_version_id(connection=conn)

    def _version_exists(self, plan_fingerprint: str) -> bool:
        with _connect(self.runtime.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sheet_vitrina_v1_warehouse_functional_versions WHERE plan_fingerprint=?",
                (plan_fingerprint,),
            ).fetchone()
        return row is not None

    def _active_lines(self) -> dict[tuple[str, int], WarehouseLine]:
        readback = self.readback()
        return {
            (str(item["warehouse_key"]), int(item["nm_id"])): _line_from_payload(item)
            for item in readback.get("balances") or []
        }

    def _cutover_stage_lines(self, stage: str) -> dict[int, WarehouseLine]:
        with _connect(self.runtime.db_path) as conn:
            version = conn.execute(
                """SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_versions
                   WHERE cutover_id=? AND version_kind='functional_cutover'
                   ORDER BY created_at LIMIT 1""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            rows = (
                conn.execute(
                    """SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances
                       WHERE version_id=? AND warehouse_key=? ORDER BY nm_id""",
                    (version["version_id"], stage),
                ).fetchall()
                if version is not None
                else []
            )
        result: dict[int, WarehouseLine] = {}
        for row in rows:
            payload = _balance_public(dict(row))
            payload["provenance"] = {
                **dict(payload.get("provenance") or {}),
                "version_id": str(row["version_id"]),
            }
            result[int(row["nm_id"])] = _line_from_payload(payload)
        return result

    def _processed_event_fingerprints(self) -> set[tuple[str, str, int]]:
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                "SELECT event_type,source_fingerprint,nm_id FROM sheet_vitrina_v1_warehouse_functional_events"
            ).fetchall()
        return {(str(row[0]), str(row[1]), int(row[2])) for row in rows}

    def _accepted_event_totals(self) -> dict[tuple[str, int], dict[str, Decimal]]:
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                """SELECT source_id,nm_id,quantity,capital_rub
                   FROM sheet_vitrina_v1_warehouse_functional_events
                   WHERE event_type='wb_final_acceptance'
                   ORDER BY created_at,event_id"""
            ).fetchall()
        totals: defaultdict[tuple[str, int], dict[str, Decimal]] = defaultdict(
            lambda: {"quantity": ZERO, "capital": ZERO}
        )
        for row in rows:
            item = totals[(str(row["source_id"]), int(row["nm_id"]))]
            item["quantity"] += _decimal(row["quantity"])
            item["capital"] += _decimal(row["capital_rub"])
        return {key: dict(value) for key, value in totals.items()}

    def _cutover_row(self) -> dict[str, Any] | None:
        with _connect(self.runtime.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_cutovers WHERE cutover_id=?",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
        return _cutover_public(row) if row else None

    def _local_source_digest(
        self,
        *,
        connection: sqlite3.Connection | None = None,
        recovery_end_date: str | None = None,
        include_historical_correction: bool = False,
    ) -> str:
        if connection is not None:
            sources = _functional_local_source_view(
                _source_rows(
                    connection,
                    recovery_end_date=recovery_end_date,
                    include_historical_correction=include_historical_correction,
                )
            )
            return "sha256:" + _hash(_guarded_local_sources(sources))
        with _connect(self.runtime.db_path) as conn:
            sources = _functional_local_source_view(
                _source_rows(
                    conn,
                    recovery_end_date=recovery_end_date,
                    include_historical_correction=include_historical_correction,
                )
            )
            return "sha256:" + _hash(_guarded_local_sources(sources))

    def _validate_emergency_correction_against_current(
        self,
        plan: Mapping[str, Any],
        *,
        connection: sqlite3.Connection,
        recovery_end_date: str,
    ) -> None:
        """Re-derive the only admissible frozen correction from current evidence."""

        sources = _functional_local_source_view(
            _source_rows(
                connection,
                recovery_end_date=recovery_end_date,
                include_historical_correction=True,
            )
        )
        cutover_row = connection.execute(
            "SELECT * FROM sheet_vitrina_v1_warehouse_functional_cutovers WHERE cutover_id=?",
            (FUNCTIONAL_CUTOVER_ID,),
        ).fetchone()
        if cutover_row is None:
            raise WarehouseFunctionalError("historical correction has no persisted cutover")
        expected_correction, expected_rows = _build_versioned_historical_correction(
            cutover=_cutover_public(cutover_row),
            opening_cost_map=_frozen_opening_cost_map_rows(connection),
            frozen_rows=sources.get("frozen_pre_cutover_wb_cost_projection") or [],
            correction_quantity_rows=sources.get(
                "historical_correction_wb_daily_quantities"
            )
            or [],
            downstream_rows=sources.get("downstream_cost_rows") or [],
            ready_snapshot_rows=sources.get(
                "historical_correction_ready_snapshots"
            )
            or [],
        )
        planned_correction = dict(plan.get("historical_correction") or {})
        correction_fingerprints = {
            str(value)
            for value in planned_correction.get("row_fingerprints") or []
        }
        planned_rows = [
            item
            for item in plan.get("historical_wb_cost_projection") or []
            if str(item.get("fingerprint") or "") in correction_fingerprints
        ]
        _validate_historical_correction_matches_derived(
            planned_correction=planned_correction,
            planned_rows=planned_rows,
            expected_correction=expected_correction,
            expected_rows=expected_rows,
        )

    def _wb_supply_source_digest(self, *, connection: sqlite3.Connection | None = None) -> str:
        if connection is not None:
            rows = connection.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_supplies ORDER BY supply_id"
            ).fetchall()
            return "sha256:" + _hash(_supply_revisions(dict(row) for row in rows))
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_supplies ORDER BY supply_id"
            ).fetchall()
            return "sha256:" + _hash(_supply_revisions(dict(row) for row in rows))

    def _last_good_wb_payload(self) -> dict[str, Any]:
        with _connect(self.runtime.db_path) as conn:
            row = conn.execute(
                """SELECT snapshot.* FROM sheet_vitrina_v1_warehouse_wb_snapshots snapshot
                   JOIN sheet_vitrina_v1_warehouse_functional_versions version
                     ON version.version_id=snapshot.version_id
                   WHERE version.status='good'
                   ORDER BY version.effective_at DESC,snapshot.created_at DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            raise WarehouseFunctionalError("no last-good WB snapshot is available")
        return {
            "snapshot_date": str(row["snapshot_date"]),
            "requested_nm_ids": _loads(row["requested_nm_ids_json"], []),
            "canonical_items": _loads(row["items_json"], []),
            "data": {
                "fetched_at": str(row["fetched_at"]),
                "pagination_complete": bool(row["pagination_complete"]),
                "page_count": int(row["page_count"]),
                "page_offsets": _loads(row["page_offsets_json"], []),
                "raw_rows_digest": str(row["raw_rows_digest"]),
                "rows": _loads(row["raw_rows_json"], []),
            },
        }

    def _upsert_supplier_flows(
        self,
        conn: sqlite3.Connection,
        lines: Iterable[Mapping[str, Any]],
        *,
        created_at: str,
    ) -> None:
        flows: dict[str, dict[str, str]] = {}
        for line in lines:
            if str(line.get("warehouse_key") or "") not in {STAGE_PRODUCTION, STAGE_CHINA_TO_FF}:
                continue
            provenance = dict(line.get("provenance") or {})
            for source in provenance.get("source_records") or []:
                flow_id = str(source.get("supplier_flow_id") or "")
                shipment_id = str(source.get("shipment_id") or "")
                if not flow_id or not shipment_id:
                    continue
                flows[flow_id] = {
                    "shipment_id": shipment_id,
                    "invoice_no": str(source.get("invoice_no") or ""),
                    "source_fingerprint": "sha256:" + _hash(source),
                }
        for flow_id, item in sorted(flows.items()):
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_supplier_flows(
                       supplier_flow_id,shipment_id,invoice_no,created_at,source_fingerprint
                   ) VALUES(?,?,?,?,?) ON CONFLICT(supplier_flow_id) DO UPDATE SET
                       invoice_no=excluded.invoice_no,source_fingerprint=excluded.source_fingerprint""",
                (
                    flow_id,
                    item["shipment_id"],
                    item["invoice_no"],
                    created_at,
                    item["source_fingerprint"],
                ),
            )

    def _insert_snapshot(self, conn: sqlite3.Connection, *, version_id: str, payload: Mapping[str, Any]) -> None:
        stored_snapshot_id = _stable_id(
            "wbsnapv",
            {"source_snapshot_id": payload["snapshot_id"], "version_id": version_id},
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(
                   snapshot_id,version_id,fetched_at,snapshot_date,requested_nm_ids_json,
                   pagination_complete,page_count,page_offsets_json,raw_row_count,raw_rows_digest,
                   raw_rows_json,items_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                stored_snapshot_id,
                version_id,
                payload["fetched_at"],
                payload["snapshot_date"],
                _json(payload["requested_nm_ids"]),
                int(bool(payload["pagination_complete"])),
                int(payload["page_count"]),
                _json(payload["page_offsets"]),
                int(payload["raw_row_count"]),
                payload["raw_rows_digest"],
                _json(payload["raw_rows"]),
                _json(payload["items"]),
                self.timestamp_factory(),
            ),
        )

    def _insert_documents(
        self, conn: sqlite3.Connection, *, version_id: str, plan: Mapping[str, Any], created_at: str
    ) -> None:
        document_type = "functional_cutover" if plan["kind"] == "functional_cutover" else "warehouse_sync"
        for stage in STAGES:
            summary = plan["summaries"][stage]
            document_id = _stable_id(
                "whdoc",
                {"version_id": version_id, "warehouse_key": stage, "type": document_type},
            )
            source_fingerprint = "sha256:" + _hash(
                [item for item in plan["lines"] if item["warehouse_key"] == stage]
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_documents(
                       document_id,version_id,warehouse_key,document_type,occurred_at,source_id,
                       source_fingerprint,quantity,capital_rub,provenance_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    version_id,
                    stage,
                    document_type,
                    created_at if document_type == "functional_cutover" else plan["captured_at"],
                    FUNCTIONAL_CUTOVER_ID if document_type == "functional_cutover" else plan["wb_snapshot"]["snapshot_id"],
                    source_fingerprint,
                    summary["quantity"],
                    summary["capital_rub"],
                    _json({"source_watermarks": plan["source_watermarks"], "quality": summary["quality"]}),
                    created_at,
                ),
            )
            for item in plan["lines"]:
                if item["warehouse_key"] != stage:
                    continue
                self._insert_document_line(
                    conn,
                    document_id=document_id,
                    version_id=version_id,
                    item=item,
                    created_at=created_at,
                )
        for item in plan.get("movement_documents") or []:
            document_id = _stable_id(
                "whdoc",
                {
                    "warehouse_key": item["warehouse_key"],
                    "type": item["document_type"],
                    "source_id": item["source_id"],
                    "source_fingerprint": item["source_fingerprint"],
                },
            )
            inserted = conn.execute(
                """INSERT OR IGNORE INTO sheet_vitrina_v1_warehouse_functional_documents(
                       document_id,version_id,warehouse_key,document_type,occurred_at,source_id,
                       source_fingerprint,quantity,capital_rub,provenance_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    version_id,
                    item["warehouse_key"],
                    item["document_type"],
                    item["occurred_at"],
                    item["source_id"],
                    item["source_fingerprint"],
                    item["quantity"],
                    item["capital_rub"],
                    _json(item.get("provenance") or {}),
                    created_at,
                ),
            ).rowcount
            if not inserted:
                continue
            for line in item.get("lines") or []:
                self._insert_document_line(
                    conn,
                    document_id=document_id,
                    version_id=version_id,
                    item=line,
                    created_at=created_at,
                )

    @staticmethod
    def _insert_document_line(
        conn: sqlite3.Connection,
        *,
        document_id: str,
        version_id: str,
        item: Mapping[str, Any],
        created_at: str,
    ) -> None:
        nm_id = int(item["nm_id"])
        line_id = _stable_id("whdocline", {"document_id": document_id, "nm_id": nm_id})
        provenance = dict(item.get("provenance") or {})
        if str(item.get("quality") or "") and not str(provenance.get("quality") or ""):
            provenance["quality"] = str(item["quality"])
        conn.execute(
            """INSERT OR IGNORE INTO sheet_vitrina_v1_warehouse_functional_document_lines(
                   line_id,document_id,version_id,nm_id,quantity,wac_rub,capital_rub,
                   provenance_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                line_id,
                document_id,
                version_id,
                nm_id,
                str(item["quantity"]),
                item.get("wac_rub"),
                str(item["capital_rub"]),
                _json(provenance),
                created_at,
            ),
        )


def ensure_warehouse_functional_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_cutovers(
            cutover_id TEXT PRIMARY KEY,cutover_at TEXT NOT NULL,status TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL UNIQUE,source_watermarks_json TEXT NOT NULL,
            absorbed_supply_revisions_json TEXT NOT NULL,backup_json TEXT NOT NULL,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_opening_cost_map(
            cutover_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_warehouse_functional_cutovers(cutover_id),
            nm_id INTEGER NOT NULL,ff_unit_cost_rub TEXT NOT NULL,wb_unit_cost_rub TEXT NOT NULL,
            quality TEXT NOT NULL,provenance_json TEXT NOT NULL,fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,PRIMARY KEY(cutover_id,nm_id)
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_wb_daily_cost(
            cutover_id TEXT NOT NULL,as_of_date TEXT NOT NULL,nm_id INTEGER NOT NULL,
            quantity TEXT NOT NULL,wac_rub TEXT NOT NULL,capital_rub TEXT NOT NULL,
            quality TEXT NOT NULL,provenance_json TEXT NOT NULL,fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,PRIMARY KEY(cutover_id,as_of_date,nm_id)
        );
        CREATE INDEX IF NOT EXISTS warehouse_wb_daily_cost_by_date
        ON sheet_vitrina_v1_warehouse_wb_daily_cost(as_of_date,nm_id);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_wb_daily_cost_corrections(
            correction_id TEXT PRIMARY KEY,cutover_id TEXT NOT NULL,version_id TEXT NOT NULL,
            supersedes_plan_fingerprint TEXT NOT NULL,correction_plan_fingerprint TEXT NOT NULL UNIQUE,
            missing_dates_json TEXT NOT NULL,row_fingerprints_json TEXT NOT NULL,
            ready_snapshot_manifest_json TEXT NOT NULL,ready_snapshot_manifest_digest TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            backup_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_versions(
            version_id TEXT PRIMARY KEY,cutover_id TEXT NOT NULL,version_kind TEXT NOT NULL,
            effective_at TEXT NOT NULL,status TEXT NOT NULL,plan_fingerprint TEXT NOT NULL UNIQUE,
            local_source_digest TEXT NOT NULL,source_watermarks_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_active(
            slot INTEGER PRIMARY KEY CHECK(slot=1),version_id TEXT NOT NULL,updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_balances(
            version_id TEXT NOT NULL,warehouse_key TEXT NOT NULL,nm_id INTEGER NOT NULL,
            quantity TEXT NOT NULL,wac_rub TEXT,capital_rub TEXT NOT NULL,cost_covered_quantity TEXT NOT NULL,
            quality TEXT NOT NULL,certified INTEGER NOT NULL,wb_quantity TEXT NOT NULL,
            wb_in_way_to_client TEXT NOT NULL,wb_in_way_from_client TEXT NOT NULL,
            provenance_json TEXT NOT NULL,PRIMARY KEY(version_id,warehouse_key,nm_id)
        );
        CREATE INDEX IF NOT EXISTS warehouse_functional_balance_stage
        ON sheet_vitrina_v1_warehouse_functional_balances(version_id,warehouse_key,nm_id);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_ff_reservations(
            version_id TEXT NOT NULL,supply_id TEXT NOT NULL,nm_id INTEGER NOT NULL,
            quantity TEXT NOT NULL,PRIMARY KEY(version_id,supply_id,nm_id)
        );
        CREATE INDEX IF NOT EXISTS warehouse_functional_ff_reservation_stage
        ON sheet_vitrina_v1_warehouse_functional_ff_reservations(version_id,nm_id,supply_id);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_supplier_cost_states(
            version_id TEXT NOT NULL,shipment_id TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,calculation_fingerprint TEXT NOT NULL,
            expenses_complete INTEGER NOT NULL,calculation_available INTEGER NOT NULL,
            created_at TEXT NOT NULL,PRIMARY KEY(version_id,shipment_id)
        );
        CREATE INDEX IF NOT EXISTS warehouse_supplier_cost_state_shipment
        ON sheet_vitrina_v1_warehouse_supplier_cost_states(shipment_id,version_id);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_supplier_cost_state_replays(
            replay_id TEXT PRIMARY KEY,version_id TEXT NOT NULL,sequence_no INTEGER NOT NULL,
            supersedes_version_plan_fingerprint TEXT NOT NULL,
            replay_plan_fingerprint TEXT NOT NULL UNIQUE,source_manifest_digest TEXT NOT NULL,
            target_shipment_ids_json TEXT NOT NULL,state_fingerprints_json TEXT NOT NULL,
            provenance_json TEXT NOT NULL,backup_json TEXT NOT NULL,created_at TEXT NOT NULL,
            UNIQUE(version_id,sequence_no)
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_supplier_cost_state_corrections(
            correction_id TEXT PRIMARY KEY,replay_id TEXT NOT NULL,version_id TEXT NOT NULL,
            shipment_id TEXT NOT NULL,source_fingerprint TEXT NOT NULL,
            calculation_fingerprint TEXT NOT NULL,expenses_complete INTEGER NOT NULL,
            calculation_available INTEGER NOT NULL,supersedes_state_fingerprint TEXT NOT NULL,
            state_fingerprint TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS warehouse_supplier_cost_state_correction_lookup
        ON sheet_vitrina_v1_warehouse_supplier_cost_state_corrections(
            version_id,shipment_id,replay_id
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks(
            rollback_id TEXT PRIMARY KEY,replay_id TEXT NOT NULL UNIQUE,
            replay_plan_fingerprint TEXT NOT NULL,rollback_fingerprint TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,primary_source_digest TEXT NOT NULL,
            backup_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_wb_snapshots(
            snapshot_id TEXT PRIMARY KEY,version_id TEXT NOT NULL,fetched_at TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,requested_nm_ids_json TEXT NOT NULL,pagination_complete INTEGER NOT NULL,
            page_count INTEGER NOT NULL,page_offsets_json TEXT NOT NULL,raw_row_count INTEGER NOT NULL,
            raw_rows_digest TEXT NOT NULL,raw_rows_json TEXT NOT NULL,items_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_unmatched_doprinato(
            unmatched_id TEXT PRIMARY KEY,version_id TEXT NOT NULL,source_id TEXT NOT NULL,
            business_date TEXT,nm_id INTEGER NOT NULL,quantity TEXT NOT NULL,matched_quantity TEXT NOT NULL,
            reason TEXT NOT NULL,provenance_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_events(
            event_id TEXT PRIMARY KEY,version_id TEXT NOT NULL,event_type TEXT NOT NULL,source_id TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,business_date TEXT,nm_id INTEGER NOT NULL,quantity TEXT NOT NULL,
            capital_rub TEXT NOT NULL,provenance_json TEXT NOT NULL,created_at TEXT NOT NULL,
            UNIQUE(event_type,source_fingerprint,nm_id)
        );
        CREATE INDEX IF NOT EXISTS warehouse_functional_event_cost_lookup
        ON sheet_vitrina_v1_warehouse_functional_events(event_type,nm_id,business_date);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_documents(
            document_id TEXT PRIMARY KEY,version_id TEXT NOT NULL,warehouse_key TEXT NOT NULL,
            document_type TEXT NOT NULL,occurred_at TEXT NOT NULL,source_id TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,quantity TEXT NOT NULL,capital_rub TEXT NOT NULL,
            provenance_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_document_lines(
            line_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,version_id TEXT NOT NULL,
            nm_id INTEGER NOT NULL,quantity TEXT NOT NULL,wac_rub TEXT,capital_rub TEXT NOT NULL,
            provenance_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS warehouse_functional_document_lines_document
        ON sheet_vitrina_v1_warehouse_functional_document_lines(document_id,nm_id);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_supplier_flows(
            supplier_flow_id TEXT PRIMARY KEY,shipment_id TEXT NOT NULL UNIQUE,invoice_no TEXT,
            created_at TEXT NOT NULL,source_fingerprint TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_wb_sync_status(
            slot INTEGER PRIMARY KEY CHECK(slot=1),last_attempt_at TEXT,last_success_at TEXT,
            last_error TEXT,active_version_id TEXT,updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_targeted_recalc_queue(
            queue_id TEXT PRIMARY KEY,stable_source_id TEXT NOT NULL,source_revision TEXT NOT NULL,
            effective_date TEXT NOT NULL,affected_nm_ids_json TEXT NOT NULL,status TEXT NOT NULL,
            requested_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,error TEXT,
            UNIQUE(stable_source_id,source_revision)
        );
        """
    )
    ensure_archival_estimate_schema(conn)


def _source_rows(
    conn: sqlite3.Connection,
    *,
    recovery_end_date: str | None = None,
    include_historical_correction: bool = False,
) -> dict[str, Any]:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {
        "sheet_vitrina_v1_supplier_shipments",
        "sheet_vitrina_v1_supplier_shipment_lines",
        "sheet_vitrina_v1_cny_ledger_operations",
        "sheet_vitrina_v1_supplier_financial_documents",
        "sheet_vitrina_v1_supplier_financial_expense_lines",
        "sheet_vitrina_v1_ff_stock_operations",
        "sheet_vitrina_v1_ff_stock_operation_lines",
        "sheet_vitrina_v1_ff_stock_reservation_operations",
        "sheet_vitrina_v1_ff_stock_reservation_lines",
        "sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint",
        "sheet_vitrina_v1_wb_supplies",
        "sheet_vitrina_v1_wb_supply_cost_layers",
        "sheet_vitrina_v1_fulfillment_service_uploads",
        "sheet_vitrina_v1_fulfillment_service_lines",
        "sheet_vitrina_v1_canonical_cost_baseline_versions",
        "sheet_vitrina_v1_canonical_cost_baseline_lines",
        "sheet_vitrina_v1_canonical_cost_daily_state",
        "sheet_vitrina_v1_supplier_ff_cost_layer_lines",
        "sheet_vitrina_v1_nomenclature_items",
        "sheet_vitrina_v1_warehouse_archival_estimate_versions",
        "sheet_vitrina_v1_warehouse_archival_estimate_rows",
        "sheet_vitrina_v1_warehouse_archival_estimate_active",
    }
    missing = sorted(required - tables)
    if missing:
        raise WarehouseFunctionalError("required source tables are missing: " + ",".join(missing))
    baseline = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_canonical_cost_baseline_versions WHERE is_current=1"
    ).fetchone()
    if baseline is None:
        raise WarehouseFunctionalError("frozen canonical baseline is not materialized")
    report = _loads(baseline["report_json"], {})
    primary_id = str(baseline["primary_shipment_id"])
    cutover_row = conn.execute(
        "SELECT cutover_at FROM sheet_vitrina_v1_warehouse_functional_cutovers WHERE cutover_id=?",
        (FUNCTIONAL_CUTOVER_ID,),
    ).fetchone()
    recovery_boundary = (
        _business_date_value(cutover_row["cutover_at"])
        if cutover_row is not None
        else str(recovery_end_date or "")[:10]
    )
    if not recovery_boundary:
        latest_snapshot = conn.execute(
            "SELECT MAX(as_of_date) AS as_of_date FROM sheet_vitrina_v1_ready_snapshots"
        ).fetchone()
        recovery_boundary = str(
            latest_snapshot["as_of_date"]
            if latest_snapshot is not None and latest_snapshot["as_of_date"]
            else date.today().isoformat()
        )[:10]
    queries = {
        "shipments": "SELECT * FROM sheet_vitrina_v1_supplier_shipments ORDER BY shipment_id",
        "shipment_lines": "SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines ORDER BY shipment_id,sort_order,line_id",
        "cny_operations": "SELECT * FROM sheet_vitrina_v1_cny_ledger_operations ORDER BY sequence_key,operation_id",
        "financial_documents": "SELECT * FROM sheet_vitrina_v1_supplier_financial_documents ORDER BY document_date,document_id",
        "financial_expense_lines": "SELECT * FROM sheet_vitrina_v1_supplier_financial_expense_lines ORDER BY supplier_order_id,financial_document_id,sort_order,line_id",
        "ff_operations": "SELECT * FROM sheet_vitrina_v1_ff_stock_operations ORDER BY created_at,operation_id",
        "ff_lines": "SELECT * FROM sheet_vitrina_v1_ff_stock_operation_lines ORDER BY operation_id,line_no",
        "ff_reservation_operations": "SELECT * FROM sheet_vitrina_v1_ff_stock_reservation_operations ORDER BY created_at,operation_id",
        "ff_reservation_lines": "SELECT * FROM sheet_vitrina_v1_ff_stock_reservation_lines ORDER BY operation_id,line_no",
        "ff_auto_writeoff_checkpoint": "SELECT * FROM sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint ORDER BY slot",
        "wb_supplies": "SELECT * FROM sheet_vitrina_v1_wb_supplies ORDER BY supply_id",
        "fulfillment_service_uploads": "SELECT * FROM sheet_vitrina_v1_fulfillment_service_uploads ORDER BY upload_id",
        "fulfillment_service_lines": "SELECT * FROM sheet_vitrina_v1_fulfillment_service_lines ORDER BY upload_id,row_index,id",
        "nomenclature_purchase_prices": "SELECT item_id,nm_id,purchase_price_yuan,updated_at FROM sheet_vitrina_v1_nomenclature_items WHERE is_active=1 AND nm_id IS NOT NULL ORDER BY nm_id,item_id",
        "downstream_cost_rows": "SELECT wb_supply_id,nm_id,accepted_qty quantity,accepted_date,supply_date,sku_ff_unit_cost_rub ff_unit_cost_rub,transit_cost_status,transit_per_unit_rub,ff_services_per_unit_rub,ff_storage_per_unit_rub,pre_acceptance_unit_cost_rub,wb_acceptance_amount_total,wb_acceptance_per_accepted_unit_rub,our_wb_unit_cost_rub wb_unit_cost_rub,source_status,component_status_json,inputs_hash FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE is_current=1 ORDER BY wb_supply_id,nm_id",
        "historical_wb_daily_quantities": "SELECT as_of_date,nm_id,physical_quantity FROM sheet_vitrina_v1_canonical_cost_daily_state WHERE stage='WB' AND as_of_date>='2026-07-01' ORDER BY as_of_date,nm_id",
        "archival_estimate_active": "SELECT version.version_id,version.effective_date,version.unit_cost_rub,version.quality,version.owner_approval_reference,version.manifest_digest,version.production_dry_run_plan_sha256,version.source_digest,version.plan_fingerprint,row.nm_id,row.unit_cost_rub row_unit_cost_rub,row.quality row_quality,row.lineage_json,row.row_fingerprint FROM sheet_vitrina_v1_warehouse_archival_estimate_active active JOIN sheet_vitrina_v1_warehouse_archival_estimate_versions version ON version.version_id=active.version_id JOIN sheet_vitrina_v1_warehouse_archival_estimate_rows row ON row.version_id=version.version_id WHERE active.slot=1 ORDER BY row.nm_id",
    }
    if "sheet_vitrina_v1_cny_documents" in tables:
        queries["cny_documents"] = (
            "SELECT * FROM sheet_vitrina_v1_cny_documents "
            "ORDER BY operation_date,operation_datetime,document_id"
        )
    result = {
        key: [dict(row) for row in conn.execute(sql).fetchall()]
        for key, sql in queries.items()
    }
    result["ff_operations"] = sorted(
        result["ff_operations"],
        key=_ff_operation_replay_sort_key,
    )
    result.setdefault("cny_documents", [])
    ready_snapshots, frozen_projection = _historical_recovery_source_rows(
        conn,
        cutover_at=(str(cutover_row["cutover_at"]) if cutover_row is not None else ""),
        recovery_boundary=recovery_boundary,
    )
    result["ready_snapshots"] = ready_snapshots
    result["frozen_pre_cutover_wb_cost_projection"] = frozen_projection
    correction_missing_dates = (
        _missing_pre_cutover_historical_dates(
            frozen_projection,
            cutover_date=_business_date_value(cutover_row["cutover_at"]),
        )
        if cutover_row is not None
        else []
    )
    result["historical_correction_ready_snapshots"] = (
        _ready_snapshot_historical_correction_rows(
            conn,
            missing_dates=correction_missing_dates,
        )
        if include_historical_correction
        and cutover_row is not None
        and correction_missing_dates
        else []
    )
    result["historical_correction_missing_dates"] = correction_missing_dates
    result["primary_cost_rows"] = [dict(row) for row in conn.execute(
        """SELECT line.nm_id,line.qty,line.invoice_unit_price_cny,line.sku_ff_unit_cost_rub,
                  line.layer_line_id,line.source_status
           FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines line
           WHERE line.supplier_shipment_id=? AND line.nm_id IS NOT NULL ORDER BY line.nm_id""",
        (primary_id,),
    ).fetchall()]
    result["primary_identity"] = {
        "shipment_id": primary_id,
        "accepted_ff_date": str(baseline["primary_accepted_ff_date"]),
        "baseline_fingerprint": str(baseline["fingerprint"]),
        "ff_cost_layer_id": str((report.get("primary_shipment") or {}).get("ff_cost_layer_id") or ""),
    }
    return result


def _ff_operation_replay_sort_key(operation: Mapping[str, Any]) -> tuple[str, int, str]:
    """Keep same-second supplier receipts ahead of dependent FF outbounds."""
    is_supplier_receipt = (
        str(operation.get("source_type") or "") == "supplier_shipment"
        and str(operation.get("operation_type") or "") == "auto_receipt"
    )
    return (
        str(operation.get("created_at") or ""),
        0 if is_supplier_receipt else 1,
        str(operation.get("operation_id") or ""),
    )


def _ready_snapshot_recovery_rows(
    conn: sqlite3.Connection,
    *,
    recovery_boundary: str,
) -> list[dict[str, Any]]:
    """Load only snapshots that can contribute an exact pre-cutover column.

    The outer snapshot date is capped at the immutable cutover boundary, so
    daily post-cutover snapshots cannot make the hourly source scan grow.
    """

    return [
        dict(row)
        for row in conn.execute(
            """SELECT snapshot.bundle_version,snapshot.as_of_date,snapshot.activated_at,
                      snapshot.refreshed_at,snapshot.plan_json
               FROM sheet_vitrina_v1_ready_snapshots snapshot
               WHERE snapshot.as_of_date <= ?
                 AND json_valid(snapshot.plan_json)
                 AND EXISTS (
                       SELECT 1
                       FROM json_each(snapshot.plan_json, '$.date_columns') day
                       WHERE CAST(day.value AS TEXT) >= '2026-07-01'
                         AND CAST(day.value AS TEXT) < ?
                 )
               ORDER BY snapshot.activated_at,snapshot.refreshed_at,
                        snapshot.as_of_date,snapshot.bundle_version""",
            (recovery_boundary, recovery_boundary),
        ).fetchall()
    ]


def _ready_snapshot_historical_correction_rows(
    conn: sqlite3.Connection,
    *,
    missing_dates: Iterable[str],
) -> list[dict[str, Any]]:
    """Load every persisted bundle carrying an exact missing-date column.

    Unlike the ordinary pre-cutover source scan, the outer snapshot date is not
    a business-date boundary here: a later publication may legitimately retain
    the only persisted exact column for a date omitted by cutover.
    """

    dates = sorted({str(value or "")[:10] for value in missing_dates if value})
    if not dates:
        return []
    placeholders = ",".join("?" for _ in dates)
    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT snapshot.bundle_version,snapshot.as_of_date,snapshot.activated_at,
                       snapshot.refreshed_at,snapshot.plan_json
                FROM sheet_vitrina_v1_ready_snapshots snapshot
                WHERE json_valid(snapshot.plan_json)
                  AND EXISTS (
                        SELECT 1
                        FROM json_each(snapshot.plan_json, '$.date_columns') day
                        WHERE CAST(day.value AS TEXT) IN ({placeholders})
                  )
                ORDER BY snapshot.activated_at,snapshot.refreshed_at,
                         snapshot.as_of_date,snapshot.bundle_version""",
            dates,
        ).fetchall()
    ]


def _historical_recovery_source_rows(
    conn: sqlite3.Connection,
    *,
    cutover_at: str,
    recovery_boundary: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select mutable snapshot evidence only before the immutable cutover."""

    if not str(cutover_at or "").strip():
        return (
            _ready_snapshot_recovery_rows(
                conn,
                recovery_boundary=recovery_boundary,
            ),
            [],
        )
    # Ready snapshots can legitimately be republished for old outer dates.
    # After cutover they are no longer an admissible historical input: the
    # versioned daily rows written by cutover are the immutable replay boundary.
    return (
        [],
        _frozen_pre_cutover_wb_cost_projection(
            conn,
            cutover_date=_business_date_value(cutover_at),
        ),
    )


def _canonical_daily_projection_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize derived daily rows for immutable-history comparisons."""

    result = [
        {
            "as_of_date": str(item.get("as_of_date") or "")[:10],
            "nm_id": int(item.get("nm_id") or 0),
            "quantity": str(item.get("quantity") or "0"),
            "wac_rub": str(item.get("wac_rub") or "0"),
            "capital_rub": str(item.get("capital_rub") or "0"),
            "quality": str(item.get("quality") or ""),
            "provenance": dict(item.get("provenance") or {}),
            "fingerprint": str(item.get("fingerprint") or ""),
        }
        for item in rows
    ]
    return sorted(result, key=lambda item: (item["as_of_date"], item["nm_id"]))


def _frozen_pre_cutover_wb_cost_projection(
    conn: sqlite3.Connection,
    *,
    cutover_date: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT as_of_date,nm_id,quantity,wac_rub,capital_rub,quality,
                  provenance_json,fingerprint
           FROM sheet_vitrina_v1_warehouse_wb_daily_cost
           WHERE cutover_id=? AND as_of_date<?
           ORDER BY as_of_date,nm_id""",
        (FUNCTIONAL_CUTOVER_ID, str(cutover_date)[:10]),
    ).fetchall()
    return _canonical_daily_projection_rows(
        {
            **dict(row),
            "provenance": _loads(row["provenance_json"], {}),
        }
        for row in rows
    )


def _frozen_opening_cost_map_rows(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    rows = [
        {
            **dict(row),
            "provenance": _loads(row["provenance_json"], {}),
        }
        for row in conn.execute(
            """SELECT nm_id,ff_unit_cost_rub,wb_unit_cost_rub,quality,
                      provenance_json,fingerprint
               FROM sheet_vitrina_v1_warehouse_opening_cost_map
               WHERE cutover_id=? ORDER BY nm_id""",
            (FUNCTIONAL_CUTOVER_ID,),
        ).fetchall()
    ]
    return overlay_opening_cost_rows(conn, rows)


def _merge_historical_wb_quantity_evidence(
    *,
    canonical_rows: Iterable[Mapping[str, Any]],
    ready_snapshot_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Fill exact historical WB quantity dates from persisted ready columns.

    Canonical daily rows remain first priority.  A ready snapshot is used only
    for an exact ``stock_total`` column that was already materialized for the
    requested business date; no current value or preceding date is carried
    backward or forward.
    """

    merged: dict[tuple[str, int], dict[str, Any]] = {}
    ordered_snapshots = sorted(
        (dict(item) for item in ready_snapshot_rows),
        key=lambda item: (
            str(item.get("activated_at") or ""),
            str(item.get("refreshed_at") or ""),
            str(item.get("as_of_date") or ""),
            str(item.get("bundle_version") or ""),
        ),
    )
    for raw_snapshot in ordered_snapshots:
        plan_json = str(raw_snapshot.get("plan_json") or "")
        plan = _loads(plan_json, {})
        if not isinstance(plan, Mapping):
            continue
        dates = [str(value or "") for value in plan.get("date_columns") or []]
        data_sheet = next(
            (
                item
                for item in plan.get("sheets") or []
                if isinstance(item, Mapping)
                and str(item.get("sheet_name") or "") == "DATA_VITRINA"
            ),
            None,
        )
        if not dates or not isinstance(data_sheet, Mapping):
            continue
        snapshot_fingerprint = "sha256:" + hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        for row in data_sheet.get("rows") or []:
            if not isinstance(row, list) or len(row) < 2:
                continue
            row_id = str(row[1] or "")
            if not row_id.startswith("SKU:") or not row_id.endswith("|stock_total"):
                continue
            try:
                nm_id = int(row_id[len("SKU:") : -len("|stock_total")])
            except ValueError:
                continue
            if nm_id <= 0:
                continue
            for index, day in enumerate(dates):
                if day < "2026-07-01" or len(row) <= 2 + index:
                    continue
                quantity = _optional_decimal(row[2 + index])
                if quantity is None or quantity < ZERO:
                    continue
                merged[(day, nm_id)] = {
                    "as_of_date": day,
                    "nm_id": nm_id,
                    "physical_quantity": _text(quantity),
                    "quantity_provenance": {
                        "source": "persisted_ready_snapshot_exact_column",
                        "metric_key": "stock_total",
                        "column_date": day,
                        "snapshot_as_of_date": str(raw_snapshot.get("as_of_date") or ""),
                        "bundle_version": str(raw_snapshot.get("bundle_version") or ""),
                        "snapshot_plan_fingerprint": snapshot_fingerprint,
                    },
                }
    for row in canonical_rows:
        day = str(row.get("as_of_date") or "")[:10]
        nm_id = int(row.get("nm_id") or 0)
        if day < "2026-07-01" or nm_id <= 0:
            continue
        merged[(day, nm_id)] = {
            **dict(row),
            "as_of_date": day,
            "nm_id": nm_id,
            "quantity_provenance": {
                "source": "canonical_cost_daily_state",
                "stage": "WB",
                "as_of_date": day,
            },
        }
    return [merged[key] for key in sorted(merged)]


def _coherent_historical_wb_quantity_evidence(
    ready_snapshot_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Materialize each date only from its selected single coherent column."""

    result: list[dict[str, Any]] = []
    for day, cells in sorted(
        _latest_exact_stock_total_cells_by_date(ready_snapshot_rows).items()
    ):
        if not cells or any(
            cell.get("quantity") is None or _decimal(cell.get("quantity")) < ZERO
            for cell in cells.values()
        ):
            continue
        for nm_id, cell in sorted(cells.items()):
            quantity = _decimal(cell["quantity"])
            result.append(
                {
                    "as_of_date": day,
                    "nm_id": nm_id,
                    "physical_quantity": _text(quantity),
                    "quantity_provenance": dict(cell.get("provenance") or {}),
                }
            )
    return result


def _functional_local_source_view(sources: Mapping[str, Any]) -> dict[str, Any]:
    """Return the deterministic source representation used by plan and apply.

    Exact period columns are a derived view of persisted ready snapshots.  Both
    the dry-run capture and the optimistic apply gate must hash that same view;
    hashing raw canonical rows at apply would reject an unchanged reviewed plan.
    """

    normalized = dict(sources)
    normalized["historical_wb_daily_quantities"] = _merge_historical_wb_quantity_evidence(
        canonical_rows=normalized.get("historical_wb_daily_quantities") or [],
        ready_snapshot_rows=normalized.get("ready_snapshots") or [],
    )
    correction_quantities = (
        # A post-cutover correction is deliberately narrower than the normal
        # canonical replay. Every date comes from one selected coherent exact
        # column; neither canonical rows nor per-SKU snapshot stitching enter.
        _coherent_historical_wb_quantity_evidence(
            normalized.get("historical_correction_ready_snapshots") or []
        )
    )
    missing_dates_value = normalized.get("historical_correction_missing_dates")
    if missing_dates_value is not None:
        missing_dates = {str(value or "")[:10] for value in missing_dates_value or []}
        correction_quantities = [
            item
            for item in correction_quantities
            if str(item.get("as_of_date") or "")[:10] in missing_dates
        ]
    normalized["historical_correction_wb_daily_quantities"] = correction_quantities
    normalized.setdefault("ready_snapshots", [])
    normalized.setdefault("historical_correction_ready_snapshots", [])
    normalized.setdefault("historical_correction_missing_dates", [])
    return normalized


def _guarded_local_sources(sources: Mapping[str, Any]) -> dict[str, Any]:
    """Apply guard for production sources that cutover is not allowed to mutate.

    Fresh WB supply evidence is captured into the reviewed plan from a disposable
    coherent database copy.  Its own digest stays in the plan, while all other
    supplier/CNY/financial/FF/cost evidence is optimistically rechecked against
    production immediately before atomic apply.
    """

    return {
        key: value
        for key, value in sources.items()
        if key
        not in {
            "wb_supplies",
            "downstream_cost_rows",
            # Raw ready-snapshot JSON contains unrelated publication rows and
            # metadata. The derived exact-column view below is the only
            # correction evidence consumed by arithmetic and therefore the
            # only snapshot state admitted to the optimistic drift digest.
            "historical_correction_ready_snapshots",
        }
    }


def _ff_reservation_snapshot_rows(capture: Mapping[str, Any]) -> list[dict[str, Any]]:
    active = _active_ff_reservation_index(
        capture.get("ff_reservation_operations") or [],
        capture.get("ff_reservation_lines") or [],
    )
    return [
        {"supply_id": supply_id, "nm_id": nm_id, "quantity": _text(quantity)}
        for (supply_id, nm_id), quantity in sorted(active.items())
    ]


def _ff_reservation_public_state_from_snapshot(
    *,
    reservations: Iterable[Mapping[str, Any]],
    balances: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    reservation_rows = [dict(item) for item in reservations]
    balances_by_nm = {
        int(item.get("nm_id") or 0): _decimal(item.get("quantity"))
        for item in balances
    }
    by_nm: dict[str, dict[str, Any]] = {}
    for item in reservation_rows:
        nm_id = int(item.get("nm_id") or 0)
        key = str(nm_id)
        current = by_nm.setdefault(key, {"quantity": ZERO, "supply_ids": []})
        current["quantity"] = _decimal(current["quantity"]) + _decimal(item.get("quantity"))
        supply_id = str(item.get("supply_id") or "")
        if supply_id and supply_id not in current["supply_ids"]:
            current["supply_ids"].append(supply_id)
    reserved = sum((_decimal(item["quantity"]) for item in by_nm.values()), ZERO)
    physical = sum(balances_by_nm.values(), ZERO)
    available = sum(
        (max(quantity - _decimal((by_nm.get(str(nm_id)) or {}).get("quantity")), ZERO)
         for nm_id, quantity in balances_by_nm.items()),
        ZERO,
    )
    unsecured = sum(
        (
            max(
                _decimal(item["quantity"])
                - max(balances_by_nm.get(int(nm_id), ZERO), ZERO),
                ZERO,
            )
            for nm_id, item in by_nm.items()
        ),
        ZERO,
    )
    public_by_nm = {
        nm_id: {
            "quantity": _text(_decimal(item["quantity"])),
            "supply_ids": sorted(item["supply_ids"]),
        }
        for nm_id, item in by_nm.items()
    }
    return {
        "physical_quantity": _text(physical),
        "reserved_quantity": _text(reserved),
        "available_quantity": _text(available),
        "unsecured_reservation_quantity": _text(unsecured),
        "reservation_supply_count": len(
            {str(item.get("supply_id") or "") for item in reservation_rows}
        ),
        "reservation_sku_count": len(public_by_nm),
        "by_nm": public_by_nm,
        "rows": reservation_rows,
        "formula_ru": "Доступно = max(физический остаток − резерв, 0); необеспеченный резерв не создаёт отрицательный остаток или капитал.",
    }


def _active_ff_reservation_index(
    operations: Iterable[Mapping[str, Any]],
    lines: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, int], Decimal]:
    supply_by_operation = {
        str(item.get("operation_id") or ""): str(item.get("supply_id") or "")
        for item in operations
    }
    result: dict[tuple[str, int], Decimal] = {}
    for line in lines:
        supply_id = supply_by_operation.get(str(line.get("operation_id") or ""), "")
        nm_id = int(line.get("nm_id") or 0)
        if not supply_id or nm_id <= 0:
            continue
        key = (supply_id, nm_id)
        result[key] = result.get(key, ZERO) + _decimal(line.get("quantity_delta"))
    return {key: quantity for key, quantity in result.items() if quantity > ZERO}


def _supply_downstream_component_index(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    """Return validated non-FF supply components using Decimal arithmetic."""

    result: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        supply_id = str(row.get("wb_supply_id") or "")
        nm_id = int(row.get("nm_id") or 0)
        if not supply_id or nm_id <= 0:
            continue
        transit_status = str(row.get("transit_cost_status") or "")
        if transit_status not in {"transit_confirmed", "direct_zero_confirmed"}:
            continue
        transit = _decimal(row.get("transit_per_unit_rub"))
        services = _decimal(row.get("ff_services_per_unit_rub"))
        storage = _decimal(row.get("ff_storage_per_unit_rub"))
        acceptance = _decimal(row.get("wb_acceptance_per_accepted_unit_rub"))
        if min(transit, services, storage, acceptance) < ZERO:
            raise WarehouseFunctionalError(
                f"WB supply {supply_id}:{nm_id} has a negative downstream component"
            )
        result[(supply_id, nm_id)] = {
            "pre_acceptance_addon": transit + services + storage,
            "acceptance_addon": acceptance,
            "inputs_hash": str(row.get("inputs_hash") or ""),
        }
    return result


def compose_supply_costs(
    *,
    outbound_ff_wac: Any,
    pre_acceptance_addon: Any,
    acceptance_addon: Any,
) -> tuple[Decimal, Decimal]:
    """Compose supply cost without carrying a legacy FF-cost baseline forward."""

    ff_wac = _decimal(outbound_ff_wac)
    pre_addon = _decimal(pre_acceptance_addon)
    acceptance = _decimal(acceptance_addon)
    if ff_wac <= ZERO or min(pre_addon, acceptance) < ZERO:
        raise WarehouseFunctionalError("invalid FF WAC or downstream supply cost component")
    pre_acceptance = ff_wac + pre_addon
    return pre_acceptance, pre_acceptance + acceptance


def _nomenclature_purchase_prices(
    rows: Iterable[Mapping[str, Any]],
) -> dict[int, Decimal]:
    """Resolve price bands from the active nomenclature at cutover time."""

    candidates: defaultdict[int, set[Decimal]] = defaultdict(set)
    for row in rows:
        nm_id = int(row.get("nm_id") or 0)
        price = _optional_decimal(row.get("purchase_price_yuan"))
        if nm_id > 0 and price is not None and price > ZERO:
            candidates[nm_id].add(price)
    conflicts = {nm_id: values for nm_id, values in candidates.items() if len(values) > 1}
    if conflicts:
        raise WarehouseFunctionalError(
            "active nomenclature has conflicting CNY purchase prices for nmIds: "
            + ",".join(str(nm_id) for nm_id in sorted(conflicts))
        )
    return {nm_id: next(iter(values)) for nm_id, values in candidates.items()}


def _add_bucket(
    buckets: defaultdict[tuple[str, int], dict[str, Any]],
    *,
    stage: str,
    nm_id: int,
    quantity: Decimal,
    capital: Decimal,
    covered: Decimal,
    quality: str,
    provenance: Mapping[str, Any],
    wb_quantity: Decimal = ZERO,
    wb_to_client: Decimal = ZERO,
    wb_from_client: Decimal = ZERO,
) -> None:
    if stage not in STAGES or nm_id <= 0 or min(quantity, capital, covered) < ZERO:
        raise WarehouseFunctionalError("invalid warehouse bucket contribution")
    target = buckets[(stage, nm_id)]
    target["quantity"] += quantity
    target["capital"] += capital
    target["covered"] += min(covered, quantity)
    target["quality"].append(quality)
    target["provenance"].append(dict(provenance))
    target["wb_quantity"] = target.get("wb_quantity", ZERO) + wb_quantity
    target["wb_to_client"] = target.get("wb_to_client", ZERO) + wb_to_client
    target["wb_from_client"] = target.get("wb_from_client", ZERO) + wb_from_client


def _bucket_line(key: tuple[str, int], value: Mapping[str, Any]) -> WarehouseLine:
    stage, nm_id = key
    quality = sorted(set(value["quality"]))
    return WarehouseLine(
        warehouse_key=stage,
        nm_id=nm_id,
        quantity=_decimal(value["quantity"]),
        capital=_decimal(value["capital"]),
        cost_covered_quantity=min(_decimal(value["covered"]), _decimal(value["quantity"])),
        quality=quality[0] if len(quality) == 1 else "mixed:" + ",".join(quality),
        certified=all(item in {"direct_24_06", "primary_documents", "certified"} for item in quality),
        provenance={"source_records": list(value["provenance"])},
        wb_quantity=_decimal(value.get("wb_quantity")),
        wb_in_way_to_client=_decimal(value.get("wb_to_client")),
        wb_in_way_from_client=_decimal(value.get("wb_from_client")),
    )


def _line_payload(line: WarehouseLine) -> dict[str, Any]:
    return {
        "warehouse_key": line.warehouse_key,
        "nm_id": line.nm_id,
        "quantity": _text(line.quantity),
        "wac_rub": _text(line.wac) if line.wac is not None else None,
        "capital_rub": _text(line.capital),
        "cost_covered_quantity": _text(line.cost_covered_quantity),
        "coverage_share": _text(line.cost_covered_quantity / line.quantity) if line.quantity > ZERO else None,
        "quality": line.quality,
        "certified": line.certified,
        "wb_quantity": _text(line.wb_quantity),
        "wb_in_way_to_client": _text(line.wb_in_way_to_client),
        "wb_in_way_from_client": _text(line.wb_in_way_from_client),
        "provenance": dict(line.provenance),
    }


def _line_from_payload(item: Mapping[str, Any]) -> WarehouseLine:
    return WarehouseLine(
        warehouse_key=str(item["warehouse_key"]),
        nm_id=int(item["nm_id"]),
        quantity=_decimal(item["quantity"]),
        capital=_decimal(item["capital_rub"]),
        cost_covered_quantity=_decimal(item["cost_covered_quantity"]),
        quality=str(item["quality"]),
        certified=bool(item.get("certified")),
        provenance=dict(item.get("provenance") or {}),
        wb_quantity=_decimal(item.get("wb_quantity")),
        wb_in_way_to_client=_decimal(item.get("wb_in_way_to_client")),
        wb_in_way_from_client=_decimal(item.get("wb_in_way_from_client")),
    )


def _summaries(lines: Iterable[WarehouseLine]) -> dict[str, dict[str, Any]]:
    grouped: defaultdict[str, list[WarehouseLine]] = defaultdict(list)
    for line in lines:
        grouped[line.warehouse_key].append(line)
    result = {}
    for stage in STAGES:
        rows = grouped[stage]
        quantity = sum((item.quantity for item in rows), ZERO)
        capital = sum((item.capital for item in rows), ZERO)
        covered = sum((item.cost_covered_quantity for item in rows), ZERO)
        result[stage] = {
            "quantity": _text(quantity),
            "wac_rub": _text(capital / quantity) if quantity > ZERO else None,
            "capital_rub": _text(capital),
            "cost_covered_quantity": _text(covered),
            "coverage_share": _text(covered / quantity) if quantity > ZERO else None,
            "sku_count": len(rows),
            "quality": sorted(set(item.quality for item in rows)),
            "certified": bool(rows) and all(item.certified for item in rows),
            "wb_quantity": _text(sum((item.wb_quantity for item in rows), ZERO)),
            "wb_in_way_to_client": _text(sum((item.wb_in_way_to_client for item in rows), ZERO)),
            "wb_in_way_from_client": _text(sum((item.wb_in_way_from_client for item in rows), ZERO)),
        }
    return result


def _validate_historical_projection_calendar(
    projection: Iterable[Mapping[str, Any]],
    *,
    effective_date: str,
) -> dict[str, Any]:
    """Fail closed before activation when any required business day is absent."""

    expected_dates = _date_range("2026-07-01", str(effective_date or "")[:10])
    projected_dates = {
        str(item.get("as_of_date") or "")[:10]
        for item in projection
        if str(item.get("as_of_date") or "").strip()
    }
    missing_dates = sorted(set(expected_dates) - projected_dates)
    if missing_dates:
        raise WarehouseFunctionalError(
            "historical WB cost projection has missing business dates: "
            + ",".join(missing_dates)
        )
    return {
        "date_from": expected_dates[0] if expected_dates else None,
        "date_to": expected_dates[-1] if expected_dates else None,
        "expected_day_count": len(expected_dates),
        "projected_day_count": len(projected_dates & set(expected_dates)),
        "missing_day_count": 0,
        "missing_dates": [],
    }


def _wb_snapshot_quantities(items: Iterable[Mapping[str, Any]]) -> dict[int, Decimal]:
    result: dict[int, Decimal] = {}
    for item in items:
        nm_id = int(item.get("nm_id") or 0)
        if nm_id <= 0:
            continue
        quantity = _decimal(item.get("wb_contour_quantity"))
        if quantity < ZERO:
            raise WarehouseFunctionalError(f"negative official WB contour quantity for nmId {nm_id}")
        result[nm_id] = quantity
    return result


def _wb_snapshot_integrity(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    raw_rows = [dict(item) for item in _loads(snapshot.get("raw_rows_json"), [])]
    items = [dict(item) for item in _loads(snapshot.get("items_json"), [])]
    raw_by_nm: defaultdict[int, dict[str, Decimal]] = defaultdict(
        lambda: {"physical": ZERO, "to_client": ZERO, "from_client": ZERO}
    )
    exact_rows: set[str] = set()
    source_keys: set[tuple[int, int, int, str, str]] = set()
    exact_duplicate_count = 0
    source_key_duplicate_count = 0
    for row in raw_rows:
        nm_id = int(row.get("nmId") or row.get("nm_id") or 0)
        if nm_id <= 0:
            continue
        raw_by_nm[nm_id]["physical"] += _decimal(
            row.get("stockCount")
            if row.get("stockCount") is not None
            else row.get("quantity")
        )
        raw_by_nm[nm_id]["to_client"] += _decimal(
            row.get("inWayToClient") if row.get("inWayToClient") is not None else row.get("in_way_to_client")
        )
        raw_by_nm[nm_id]["from_client"] += _decimal(
            row.get("inWayFromClient") if row.get("inWayFromClient") is not None else row.get("in_way_from_client")
        )
        exact_key = _json(row)
        exact_duplicate_count += int(exact_key in exact_rows)
        exact_rows.add(exact_key)
        warehouse_id = int(row.get("warehouseId") or row.get("warehouse_id") or 0)
        warehouse_name = str(row.get("warehouseName") or row.get("warehouse_name") or "").strip()
        region_name = str(row.get("regionName") or row.get("region_name") or "").strip()
        source_key = (
            nm_id,
            int(row.get("chrtId") or row.get("chrt_id") or 0),
            warehouse_id,
            warehouse_name.casefold() if warehouse_id == 0 else "",
            region_name.casefold() if warehouse_id == 0 else "",
        )
        source_key_duplicate_count += int(source_key in source_keys)
        source_keys.add(source_key)
    canonical_by_nm = {
        int(item.get("nm_id") or 0): {
            "physical": _decimal(item.get("quantity")),
            "to_client": _decimal(item.get("in_way_to_client")),
            "from_client": _decimal(item.get("in_way_from_client")),
        }
        for item in items
        if int(item.get("nm_id") or 0) > 0
    }
    zero_components = {"physical": ZERO, "to_client": ZERO, "from_client": ZERO}
    normalized_raw_by_nm = {
        nm_id: dict(raw_by_nm.get(nm_id, zero_components))
        for nm_id in canonical_by_nm
    }
    mapping_matches = (
        set(raw_by_nm).issubset(canonical_by_nm)
        and normalized_raw_by_nm == canonical_by_nm
    )
    physical = sum((item["physical"] for item in canonical_by_nm.values()), ZERO)
    to_client = sum((item["to_client"] for item in canonical_by_nm.values()), ZERO)
    from_client = sum((item["from_client"] for item in canonical_by_nm.values()), ZERO)
    contour = physical + to_client + from_client
    return {
        "snapshot_id": str(snapshot.get("snapshot_id") or ""),
        "version_id": str(snapshot.get("version_id") or ""),
        "snapshot_date": str(snapshot.get("snapshot_date") or ""),
        "fetched_at": str(snapshot.get("fetched_at") or ""),
        "pagination_complete": bool(snapshot.get("pagination_complete")),
        "page_count": int(snapshot.get("page_count") or 0),
        "page_offsets": _loads(snapshot.get("page_offsets_json"), []),
        "raw_row_count": len(raw_rows),
        "raw_rows_digest": str(snapshot.get("raw_rows_digest") or ""),
        "sku_count": len(canonical_by_nm),
        "physical_quantity": _text(physical),
        "in_way_to_client": _text(to_client),
        "in_way_from_client": _text(from_client),
        "wb_contour_quantity": _text(contour),
        "arithmetic": (
            f"{_text(physical)} + {_text(to_client)} + {_text(from_client)} = {_text(contour)}"
        ),
        "exact_duplicate_count": exact_duplicate_count,
        "source_key_duplicate_count": source_key_duplicate_count,
        "raw_to_canonical_mapping_matches": mapping_matches,
    }


def _daily_wb_cost_row(
    *,
    day: str,
    nm_id: int,
    quantity: Decimal,
    wac: Decimal,
    quality: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    item = {
        "as_of_date": day,
        "nm_id": nm_id,
        "quantity": _text(quantity),
        "wac_rub": _text(wac),
        "capital_rub": _text(quantity * wac),
        "quality": quality,
        "provenance": dict(provenance),
    }
    item["fingerprint"] = "sha256:" + _hash(item)
    return item


def _date_range(start: str, end: str) -> list[str]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
    ]


def _replace_current_wb_costs(
    lines: Iterable[WarehouseLine],
    *,
    daily_projection: Iterable[Mapping[str, Any]],
    current_date: str,
) -> list[WarehouseLine]:
    current = {
        int(item["nm_id"]): dict(item)
        for item in daily_projection
        if str(item.get("as_of_date") or "") == current_date
    }
    result: list[WarehouseLine] = []
    for line in lines:
        if line.warehouse_key != STAGE_WB:
            result.append(line)
            continue
        daily = current.get(line.nm_id)
        if daily is None:
            raise WarehouseFunctionalError(
                f"current functional WB balance has no daily WAC replay for nmId {line.nm_id}"
            )
        wac = _decimal(daily["wac_rub"])
        result.append(
            WarehouseLine(
                warehouse_key=line.warehouse_key,
                nm_id=line.nm_id,
                quantity=line.quantity,
                capital=line.quantity * wac,
                cost_covered_quantity=line.cost_covered_quantity,
                quality=str(daily["quality"]),
                provenance={
                    **dict(line.provenance),
                    "daily_wac_replay": dict(daily.get("provenance") or {}),
                    "daily_wac_fingerprint": str(daily.get("fingerprint") or ""),
                },
                certified=line.certified,
                wb_quantity=line.wb_quantity,
                wb_in_way_to_client=line.wb_in_way_to_client,
                wb_in_way_from_client=line.wb_in_way_from_client,
            )
        )
    return result


def _total_summary(summaries: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    quantity = sum((_decimal(item["quantity"]) for item in summaries.values()), ZERO)
    capital = sum((_decimal(item["capital_rub"]) for item in summaries.values()), ZERO)
    return {
        "quantity": _text(quantity),
        "capital_rub": _text(capital),
        "wac_rub": _text(capital / quantity) if quantity > ZERO else None,
    }


def _balance_diff(
    previous: Mapping[tuple[str, int], WarehouseLine],
    current: Iterable[WarehouseLine],
) -> dict[str, Any]:
    current_lookup = {(item.warehouse_key, item.nm_id): item for item in current}
    changed: list[dict[str, Any]] = []
    for warehouse_key, nm_id in sorted(set(previous) | set(current_lookup)):
        before = previous.get((warehouse_key, nm_id))
        after = current_lookup.get((warehouse_key, nm_id))
        before_qty = before.quantity if before else ZERO
        before_capital = before.capital if before else ZERO
        after_qty = after.quantity if after else ZERO
        after_capital = after.capital if after else ZERO
        if before_qty == after_qty and before_capital == after_capital:
            continue
        changed.append(
            {
                "warehouse_key": warehouse_key,
                "nm_id": nm_id,
                "quantity_before": _text(before_qty),
                "quantity_after": _text(after_qty),
                "quantity_delta": _text(after_qty - before_qty),
                "capital_before": _text(before_capital),
                "capital_after": _text(after_capital),
                "capital_delta": _text(after_capital - before_capital),
            }
        )
    return {
        "changed_line_count": len(changed),
        "lines": changed,
    }


def _summary_status(rows: Iterable[Mapping[str, Any]], stage: str, sync: Mapping[str, Any]) -> str:
    selected = [row for row in rows if str(row.get("warehouse_key") or "") == stage]
    if sync.get("last_error"):
        return "stale_error"
    if not selected or all(_decimal(row.get("quantity")) == ZERO for row in selected):
        return "empty"
    if selected and all(bool(row.get("certified")) for row in selected):
        return "certified"
    return "provisional"


def _warehouse_quality_presentation(value: Any) -> dict[str, str]:
    quality = str(value or "provisional").strip()
    if quality.startswith("mixed:"):
        child_codes = [item for item in quality.split(":", 1)[1].split(",") if item]
        child_labels = [
            _warehouse_quality_presentation(item)["label_ru"]
            for item in child_codes
        ]
        return {
            "code": quality,
            "label_ru": "Смешанные источники",
            "description_ru": (
                "Объединены партии с разным уровнем подтверждения: "
                + "; ".join(child_labels)
                if child_labels
                else "Объединены партии с разным уровнем подтверждения."
            ),
        }
    label, description = WAREHOUSE_QUALITY_PRESENTATIONS.get(
        quality,
        (
            "Расчётная себестоимость",
            "Статус сохранён в техническом аудите; пользовательское значение рассчитано каноническим контуром.",
        ),
    )
    return {
        "code": quality,
        "label_ru": label,
        "description_ru": description,
    }


def _warehouse_balance_status_presentation(
    value: Any,
    *,
    certified: bool,
) -> dict[str, str]:
    """Central row-level cost status; quality remains supporting provenance."""

    quality = _warehouse_quality_presentation(value)
    if certified:
        return {
            "code": "certified",
            "tone": "success",
            "label_ru": "Все расходы учтены / Подтверждено документами",
            "description_ru": (
                "Сертифицированный source fingerprint совпадает с текущим расчётом. "
                + quality["description_ru"]
            ),
        }
    return {
        **quality,
        "tone": "warning",
    }


def _warehouse_status_presentation(
    *,
    status: str,
    sync: Mapping[str, Any],
) -> dict[str, str]:
    if status == "stale_error":
        attempt = str(sync.get("last_attempt_at") or "")
        success = str(sync.get("last_success_at") or "")
        reason = _warehouse_sync_error_reason(sync.get("last_error"))
        description = f"Попытка: {attempt or 'время не записано'}. Причина: {reason}."
        if success:
            description += f" Показана последняя успешная версия от {success}."
        return {
            "code": status,
            "tone": "error",
            "label_ru": "Ошибка последней синхронизации",
            "description_ru": description,
        }
    if status == "certified":
        return {
            "code": status,
            "tone": "success",
            "label_ru": "Все расходы учтены / Подтверждено документами",
            "description_ru": (
                "Все строки склада подтверждены применимыми документами."
                + _warehouse_sync_success_suffix(sync)
            ),
        }
    if status == "empty":
        return {
            "code": status,
            "tone": "neutral",
            "label_ru": "Остаток отсутствует",
            "description_ru": (
                "Количество и товарный капитал на выбранном срезе равны нулю."
                + _warehouse_sync_success_suffix(sync)
            ),
        }
    return {
        "code": status,
        "tone": "warning",
        "label_ru": "Предварительный расчёт",
        "description_ru": (
            "Часть расходов ещё не закрыта; учтены только подтверждённые на текущий момент факты."
            + _warehouse_sync_success_suffix(sync)
        ),
    }


def _warehouse_sync_success_suffix(sync: Mapping[str, Any]) -> str:
    success = str(sync.get("last_success_at") or "")
    return f" Последняя успешная синхронизация: {success}." if success else ""


def _warehouse_sync_error_reason(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "warehouse_unmatched_doprinato.unmatched_id" in text:
        return "коллизия идентификатора строки аудита доприёмки"
    if not text:
        return "причина не записана"
    if any(token in text for token in ("401", "403", "unauthorized", "forbidden", "authentication", "authorization")):
        return "источник отклонил авторизацию; требуется проверка служебной сессии"
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "WB временно ограничил частоту запросов; сохранена последняя успешная версия"
    if "timeout" in text or "timed out" in text:
        return "источник не ответил за допустимое время; сохранена последняя успешная версия"
    if any(token in text for token in ("pagination", "coverage is incomplete", "missing_nm_ids")):
        return "WB вернул неполный снимок; публикация отклонена проверкой полноты"
    if any(token in text for token in ("drifted", "fingerprint mismatch", "source digest")):
        return "источники изменились после dry-run; требуется новый согласованный план"
    if any(token in text for token in ("negative", "cost gap", "invariant")):
        return "расчёт остановлен проверкой целостности складских данных"
    if any(token in text for token in ("sqlite", "database is locked", "operationalerror", "integrity_check")):
        return "временная ошибка хранилища; сохранена последняя успешная версия"
    if any(token in text for token in ("status 5", "http 5", "bad gateway", "service unavailable")):
        return "внешний сервис временно недоступен; сохранена последняя успешная версия"
    return "синхронизация остановлена; техническая причина доступна в журнале аудита"


def _warehouse_human_evidence(
    provenance: Any,
    *,
    quantity: Any,
    capital_rub: Any,
    quality: Any,
    fallback_date: Any = None,
) -> dict[str, Any]:
    raw = dict(provenance or {}) if isinstance(provenance, Mapping) else {}
    records = _warehouse_evidence_records(
        raw,
        aggregate_quantity=quantity,
        aggregate_capital=capital_rub,
    )
    quality_presentation = _warehouse_quality_presentation(quality)
    items = []
    for record in records:
        invoice = str(record.get("invoice_no") or "")
        operation = str(record.get("operation_id") or "")
        supply = str(
            record.get("wb_supply_id")
            or record.get("shipment_id")
            or record.get("supplier_flow_id")
            or record.get("source_id")
            or ""
        )
        document = (
            str(record.get("document_label") or "")
            or invoice
            or supply
            or operation
            or str(record.get("snapshot_id") or "")
            or "Каноническая проекция"
        )
        business_date = next(
            (
                str(record.get(key) or "")[:10]
                for key in (
                    "business_date",
                    "invoice_date",
                    "accepted_date",
                    "supply_date",
                    "actual_ff_acceptance_date",
                    "snapshot_date",
                    "fetched_at",
                    "created_at",
                    "cutover_date",
                )
                if str(record.get(key) or "")
            ),
            "",
        )
        if not business_date:
            business_date = str(fallback_date or "")[:10]
        record_quality = str(record.get("quality") or "")
        if not record_quality and "expenses_complete_certification" in record:
            record_quality = (
                "certified"
                if bool(record.get("expenses_complete_certification"))
                else "confirmed_payments_provisional_expenses"
            )
        record_quality_presentation = _warehouse_quality_presentation(record_quality or quality)
        allocation = str(record.get("allocation") or "")
        if not allocation:
            if record.get("source") == "canonical_append_only_ff_ledger_replay":
                allocation = "Скользящая средневзвешенная по append-only FF ledger"
            elif record.get("daily_wac_replay") or raw.get("daily_wac_replay"):
                allocation = "Периодическая средневзвешенная по точной бизнес-дате"
            else:
                allocation = "Капитал партии / положительное количество"
        quantity_contribution, capital_contribution = _warehouse_evidence_contribution(
            record,
            aggregate_quantity=quantity,
            aggregate_capital=capital_rub,
            record_count=len(records),
        )
        items.append(
            {
                "document": document,
                "date": business_date or "—",
                "invoice_or_supply": invoice or supply or operation or "—",
                "quantity_source": (
                    "Полное количество строки invoice после первого подтверждённого платежа"
                    if invoice
                    else "Изменение количества в append-only FF ledger"
                    if operation
                    else "Открытый остаток конкретной поставки FF → WB"
                    if supply and record.get("packed_quantity") is not None
                    else str(record.get("source") or "Канонический складской источник")
                ),
                "cost_source": _warehouse_cost_source_label(record),
                "confirmation_status": record_quality_presentation["label_ru"],
                "allocation_method": allocation,
                "quantity_contribution": str(quantity_contribution),
                "capital_contribution_rub": str(capital_contribution),
            }
        )
    return {
        "status": quality_presentation,
        "items": items,
    }


def _warehouse_evidence_records(
    raw: Mapping[str, Any],
    *,
    aggregate_quantity: Any,
    aggregate_capital: Any,
) -> list[dict[str, Any]]:
    source_records = raw.get("source_records")
    records = [dict(item) for item in source_records or [] if isinstance(item, Mapping)]
    if records:
        expanded: list[dict[str, Any]] = []
        for record in records:
            if not record.get("operations"):
                expanded.append(record)
                continue
            record_quantity = record.get("flow_quantity")
            record_capital = record.get("flow_capital_rub")
            if len(records) == 1:
                record_quantity = (
                    aggregate_quantity if record_quantity is None else record_quantity
                )
                record_capital = aggregate_capital if record_capital is None else record_capital
            if record_quantity is None or record_capital is None:
                expanded.append(record)
                continue
            expanded.extend(
                _expand_ff_ledger_evidence_record(
                    record,
                    aggregate_quantity=record_quantity,
                    aggregate_capital=record_capital,
                )
            )
        return expanded
    operations = [dict(item) for item in raw.get("operations") or [] if isinstance(item, Mapping)]
    if not operations:
        return [dict(raw)]

    return _expand_ff_ledger_evidence_record(
        raw,
        aggregate_quantity=aggregate_quantity,
        aggregate_capital=aggregate_capital,
    )


def _expand_ff_ledger_evidence_record(
    raw: Mapping[str, Any],
    *,
    aggregate_quantity: Any,
    aggregate_capital: Any,
) -> list[dict[str, Any]]:
    operations = [dict(item) for item in raw.get("operations") or [] if isinstance(item, Mapping)]

    common = {
        key: value
        for key, value in raw.items()
        if key not in {"operations", "source_records"}
    }
    operation_records: list[dict[str, Any]] = []
    operation_quantity = ZERO
    operation_capital = ZERO
    complete_capital = True
    for operation in operations:
        delta = _optional_decimal(operation.get("quantity_delta"))
        unit_cost = _optional_decimal(operation.get("unit_cost_rub"))
        capital_delta = delta * unit_cost if delta is not None and unit_cost is not None else None
        if delta is not None:
            operation_quantity += delta
        if capital_delta is None:
            complete_capital = False
        else:
            operation_capital += capital_delta
        operation_id = str(operation.get("operation_id") or "")
        operation_records.append(
            {
                **common,
                **operation,
                "document_label": f"Операция FF {operation_id}" if operation_id else "Операция FF ledger",
                "business_date": str(operation.get("created_at") or "")[:10],
                "flow_quantity": _text(delta) if delta is not None else None,
                "flow_capital_rub": _text(capital_delta) if capital_delta is not None else None,
                "quality": "moving_weighted_average",
            }
        )

    opening_quantity = _decimal(aggregate_quantity) - operation_quantity
    opening_capital = (
        _decimal(aggregate_capital) - operation_capital
        if complete_capital
        else None
    )
    opening = {
        **common,
        "document_label": "Остаток FF на cutover",
        "business_date": str(raw.get("cutover_date") or "")[:10],
        "flow_quantity": _text(opening_quantity),
        "flow_capital_rub": _text(opening_capital) if opening_capital is not None else None,
        "quality": "moving_weighted_average",
    }
    return [opening, *operation_records]


def _warehouse_evidence_contribution(
    record: Mapping[str, Any],
    *,
    aggregate_quantity: Any,
    aggregate_capital: Any,
    record_count: int,
) -> tuple[str, str]:
    quantity = _optional_decimal(record.get("flow_quantity"))
    capital = _optional_decimal(record.get("flow_capital_rub"))
    if quantity is None and record.get("packed_quantity") is not None:
        packed = _decimal(record.get("packed_quantity"))
        accepted = _decimal(record.get("accepted_quantity"))
        quantity = max(packed - accepted, ZERO)
    if capital is None and quantity is not None:
        unit_cost = _optional_decimal(record.get("pre_acceptance_unit_cost_rub"))
        if unit_cost is None:
            ff_wac = _optional_decimal(record.get("ff_wac_at_ledger_debit_rub"))
            addon = _optional_decimal(record.get("downstream_pre_acceptance_addon_rub"))
            if ff_wac is not None and addon is not None:
                unit_cost = ff_wac + addon
        if unit_cost is not None:
            capital = quantity * unit_cost
    if record_count == 1:
        quantity = quantity if quantity is not None else _optional_decimal(aggregate_quantity)
        capital = capital if capital is not None else _optional_decimal(aggregate_capital)
    return (
        _text(quantity) if quantity is not None else "—",
        _text(capital) if capital is not None else "—",
    )


def _warehouse_cost_source_label(record: Mapping[str, Any]) -> str:
    if record.get("payment_operation_ids") or record.get("cny_fee_operation_ids"):
        parts = ["Фактические CNY-платежи в RUB"]
        if (
            record.get("bank_fee_source_ids")
            or record.get("cny_fee_operation_ids")
            or _decimal(record.get("direct_rub_bank_fees")) > ZERO
        ):
            parts.append("связанные банковские комиссии")
        if record.get("china_expense_sources"):
            parts.append("документы расходов этапа Китай → FF")
        return ", ".join(parts)
    if record.get("ff_wac_at_ledger_debit_rub") is not None:
        return "WAC FF на момент списания и расходы этапа FF → WB"
    if record.get("operation_id"):
        return "Себестоимость на момент операции append-only FF ledger"
    source = str(record.get("source") or "")
    if source == "canonical_append_only_ff_ledger_replay":
        return "SKU-стоимость базовой приёмки и replay канонического FF ledger"
    if "snapshot" in source or record.get("snapshot_id"):
        return "Официальный snapshot WB и каноническая предыдущая WAC"
    return source or "Каноническая складская стоимость"


def _balance_public(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **dict(item),
        "certified": bool(item.get("certified")),
        "provenance": _loads(item.get("provenance_json"), {}),
    }


def _cutover_public(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    return {
        "cutover_id": str(row["cutover_id"]),
        "cutover_at": str(row["cutover_at"]),
        "status": str(row["status"]),
        "plan_fingerprint": str(row["plan_fingerprint"]),
        "source_watermarks": _loads(row["source_watermarks_json"], {}),
        "absorbed_supply_revisions": _loads(row["absorbed_supply_revisions_json"], {}),
        "backup": _loads(row["backup_json"], {}),
    }


def _version_public(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    return {
        "version_id": str(row["version_id"]),
        "version_kind": str(row["version_kind"]),
        "effective_at": str(row["effective_at"]),
        "status": str(row["status"]),
        "plan_fingerprint": str(row["plan_fingerprint"]),
        "local_source_digest": str(row["local_source_digest"]),
        "source_watermarks": _loads(row["source_watermarks_json"], {}),
    }


def _document_public(item: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(item)
    return {**value, "provenance": _loads(value.get("provenance_json"), {})}


def _unmatched_public(item: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(item)
    provenance = _loads(value.get("provenance_json"), {})
    quality = (
        str(provenance.get("quality") or "")
        if isinstance(provenance, Mapping)
        else ""
    ) or "pooled_final_acceptance_discrepancy"
    return {
        **value,
        "provenance": provenance,
        "human_evidence": _warehouse_human_evidence(
            provenance,
            quantity=value.get("quantity"),
            capital_rub="0",
            quality=quality,
            fallback_date=value.get("business_date"),
        ),
    }


def _verify_version(conn: sqlite3.Connection, *, version_id: str, expected: Mapping[str, Any]) -> None:
    stored = conn.execute(
        "SELECT warehouse_key,nm_id,quantity,wac_rub,capital_rub,cost_covered_quantity FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? ORDER BY warehouse_key,nm_id",
        (version_id,),
    ).fetchall()
    actual = [list(row) for row in stored]
    wanted = [
        [item["warehouse_key"], int(item["nm_id"]), item["quantity"], item["wac_rub"], item["capital_rub"], item["cost_covered_quantity"]]
        for item in sorted(expected["lines"], key=lambda item: (item["warehouse_key"], int(item["nm_id"])))
    ]
    if actual != wanted:
        raise WarehouseFunctionalError("functional apply readback mismatch")
    stored_reservations = [
        list(row)
        for row in conn.execute(
            """SELECT supply_id,nm_id,quantity
               FROM sheet_vitrina_v1_warehouse_functional_ff_reservations
               WHERE version_id=? ORDER BY supply_id,nm_id""",
            (version_id,),
        ).fetchall()
    ]
    wanted_reservations = [
        [str(item["supply_id"]), int(item["nm_id"]), str(item["quantity"])]
        for item in sorted(
            expected.get("ff_reservations") or [],
            key=lambda item: (str(item["supply_id"]), int(item["nm_id"])),
        )
    ]
    if stored_reservations != wanted_reservations:
        raise WarehouseFunctionalError("functional FF reservation readback mismatch")
    if len(expected["summaries"]) != 6:
        raise WarehouseFunctionalError("functional apply did not publish six warehouses")


def _supply_revision(row: Mapping[str, Any]) -> str:
    return "sha256:" + _hash(
        {
            "supply_id": row.get("supply_id"),
            "status_id": row.get("status_id"),
            "normalized": _stable_supply_normalized(row.get("normalized_row_json")),
            "goods_hash": row.get("raw_goods_hash"),
            "goods": row.get("raw_goods_json"),
            "updated": row.get("updated_date"),
        }
    )


def _stable_supply_normalized(value: Any) -> Any:
    normalized = _loads(value, value)
    if not isinstance(normalized, Mapping):
        return normalized
    business_state = dict(normalized)
    for key in ("synced_at", "last_list_synced_at", "last_enriched_at"):
        business_state.pop(key, None)
    return business_state


def _supply_revisions(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    return {
        str(row.get("supply_id") or ""): _supply_revision(row)
        for row in rows
        if str(row.get("supply_id") or "")
    }


def _supply_business_date(record: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    for key in (
        "actual_acceptance_date",
        "acceptance_date",
        "fact_date",
        "closed_at",
        "updated_date",
    ):
        value = str(record.get(key) or row.get(key) or "")
        if len(value) >= 10:
            return value[:10]
    return ""


def _validated_financial_expense(
    *,
    document: Mapping[str, Any],
    expense: Mapping[str, Any],
) -> bool:
    """Only reviewed parser output can enter canonical warehouse capital."""

    return (
        str(document.get("parse_status") or "") in {"parsed", "confirmed"}
        and str(expense.get("status") or "parsed") in {"parsed", "confirmed"}
    )


def _counted_cny_operation(
    operation: Mapping[str, Any],
    *,
    document: Mapping[str, Any] | None = None,
) -> bool:
    """Mirror ledger replay while excluding genuinely reviewable documents."""

    operation_status = str(operation.get("status") or "").strip().lower()
    document_status = str(
        operation.get("document_status")
        or (document or {}).get("status")
        or ""
    ).strip().lower()
    error_reason = str(operation.get("error_reason") or "").strip().lower()
    if document_status not in {"", "posted"}:
        return False
    if operation_status == "posted":
        return True
    return (
        operation_status == "needs_review"
        and document_status == "posted"
        and error_reason == "date_only_deterministic_sequence"
    )


def _line_value(row: Mapping[str, Any]) -> Decimal:
    raw_amount = row.get("amount")
    if raw_amount is not None and str(raw_amount).strip():
        # A parser-supplied amount is primary invoice evidence.  In particular,
        # an explicit zero/negative amount must fail closed upstream rather than
        # being silently reconstructed from quantity and unit price.
        return _decimal(raw_amount)
    return _decimal(row.get("qty")) * _decimal(row.get("unit_price"))


def _supplier_flow_id(shipment_id: str) -> str:
    return "supplier_flow_" + hashlib.sha256(str(shipment_id).encode("utf-8")).hexdigest()[:20]


def _linear(x: Decimal, x1: Decimal, y1: Decimal, x2: Decimal, y2: Decimal) -> Decimal:
    if x1 == x2:
        return y1
    return y1 + (x - x1) * (y2 - y1) / (x2 - x1)


def _watermark(rows: Iterable[Mapping[str, Any]], key: str, fallback: str = "") -> dict[str, Any]:
    values = [str(row.get(key) or row.get(fallback) or "") for row in rows]
    return {
        "row_count": len(values),
        "max": max(values, default=""),
        "digest": "sha256:" + _hash(list(rows)),
    }


def _stable_id(prefix: str, payload: Any) -> str:
    return f"{prefix}_" + _hash(payload)[:24]


def _fingerprint(payload: Mapping[str, Any]) -> str:
    normalized = {key: value for key, value in payload.items() if key != "plan_fingerprint"}
    return "sha256:" + _hash(normalized)


def _calculation_digest(plan: Mapping[str, Any]) -> str:
    """Hash derived business state without volatile source-capture identities.

    A fresh official WB fetch intentionally has a new ``snapshot_id`` and
    ``fetched_at`` even when every quantity and calculated rouble value is
    unchanged.  Those capture facts remain protected by the external source
    guards and the exact plan fingerprint, but must not make the independent
    semantic recheck report a false calculation drift.
    """

    projection = [
        {
            key: item.get(key)
            for key in (
                "as_of_date",
                "nm_id",
                "quantity",
                "wac_rub",
                "capital_rub",
                "quality",
            )
        }
        for item in plan.get("historical_wb_cost_projection") or []
    ]
    lines = [
        {
            key: item.get(key)
            for key in (
                "warehouse_key",
                "nm_id",
                "quantity",
                "wac_rub",
                "capital_rub",
                "cost_covered_quantity",
                "coverage_share",
                "quality",
                "certified",
                "wb_quantity",
                "wb_in_way_to_client",
                "wb_in_way_from_client",
            )
        }
        for item in plan.get("lines") or []
    ]
    unmatched = [
        {
            key: item.get(key)
            for key in (
                "source_id",
                "source_fingerprint",
                "business_date",
                "nm_id",
                "quantity",
                "matched_quantity",
                "reason",
            )
        }
        for item in plan.get("unmatched_doprinato") or []
    ]
    events = [
        {
            key: item.get(key)
            for key in (
                "event_type",
                "source_id",
                "source_fingerprint",
                "business_date",
                "nm_id",
                "quantity",
                "capital_rub",
            )
        }
        for item in plan.get("new_events") or []
    ]
    movement_documents = [
        {
            key: item.get(key)
            for key in (
                "document_type",
                "warehouse_key",
                "occurred_at",
                "source_id",
                "source_fingerprint",
                "quantity",
                "capital_rub",
            )
        }
        | {
            "lines": [
                {
                    key: line.get(key)
                    for key in ("nm_id", "quantity", "wac_rub", "capital_rub")
                }
                for line in item.get("lines") or []
            ]
        }
        for item in plan.get("movement_documents") or []
    ]
    payload = {
        # The opening map is itself frozen primary evidence; retain its exact
        # provenance and quality in the semantic guard.
        "opening_cost_map": plan.get("opening_cost_map") or [],
        "historical_wb_cost_projection": projection,
        "lines": lines,
        "summaries": plan.get("summaries") or {},
        "unmatched_doprinato": unmatched,
        "new_events": events,
        "movement_documents": movement_documents,
        "supplier_cost_states": plan.get("supplier_cost_states") or [],
        "ff_reservations": plan.get("ff_reservations") or [],
        "invariants": plan.get("invariants") or {},
    }
    return "sha256:" + _hash(payload)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _clone(value: Any) -> Any:
    return json.loads(_json(value))


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value in (None, ""):
        return ZERO
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise WarehouseFunctionalError(f"invalid decimal: {value!r}") from exc
    if not result.is_finite():
        raise WarehouseFunctionalError(f"non-finite decimal: {value!r}")
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return _decimal(value)


def _text(value: Decimal | None) -> str:
    if value is None:
        return ""
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(ONE))
    return format(normalized, "f")


def _discard_uncommitted_backup(backup: Mapping[str, Any] | None) -> None:
    """Remove only the coherent backup created by an apply that never committed."""

    path_value = str((backup or {}).get("path") or "")
    if not path_value:
        return
    path = Path(path_value)
    if not path.is_absolute():
        return
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        candidate.unlink(missing_ok=True)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _business_date_value(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if len(normalized) == 10:
        return normalized
    return business_date_from_timestamp(normalized)
