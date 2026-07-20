"""Deterministic customs-declaration item matching and XLSX export."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re
from typing import Any, Iterable, Mapping

from packages.application.supplier_customs_dt_matching_policy import (
    DT_ANNEX_MATCHING_POLICY_VERSION,
    canonical_dt_series,
    normalized_model_key_set,
    resolve_dt_annex_series_model,
)


ZERO = Decimal("0")
MATCHED_STATUSES = {"matched", "matched_by_barcode", "matched_by_compatibility"}
WORKBOOK_HEADERS = (
    "№ позиции ДТ",
    "№ строки приложения",
    "Наименование из ДТ",
    "Артикул из ДТ",
    "Модель из ДТ",
    "Определённая серия",
    "Количество",
    "Ед. изм.",
    "nmID",
    "Наша номенклатура",
    "Штрихкод",
    "Штрихкод из ДТ",
    "Статус сопоставления",
    "Основание сопоставления",
    "Код ТН ВЭД",
)


def build_customs_breakdown_xlsx(
    *,
    customs_document: Mapping[str, Any],
    shipment: Mapping[str, Any],
    shipment_lines: Iterable[Mapping[str, Any]],
    nomenclature_items: Iterable[Mapping[str, Any]],
    packing_documents: Iterable[Mapping[str, Any]] = (),
) -> tuple[bytes, str, dict[str, Any]]:
    """Return a verified workbook, safe filename and bounded mapping receipt."""

    normalized = _mapping(customs_document.get("normalized_parse"))
    goods_items = [dict(item) for item in normalized.get("goods_items") or [] if isinstance(item, Mapping)]
    annex_items = [dict(item) for item in normalized.get("annex_items") or [] if isinstance(item, Mapping)]
    if annex_items:
        matching = match_customs_annex_items(
            annex_items=annex_items,
            goods_items=goods_items,
            shipment_lines=shipment_lines,
            nomenclature_items=nomenclature_items,
            expected_quantity_total=normalized.get("annex_quantity_total"),
            parser_quantity_conserved=normalized.get("annex_quantity_conserved"),
        )
    else:
        matching = _upgrade_legacy_matching(match_customs_goods_items(
            goods_items=goods_items,
            shipment_lines=shipment_lines,
            nomenclature_items=nomenclature_items,
            packing_documents=packing_documents,
        ))
    declaration_number = str(
        normalized.get("declaration_number")
        or normalized.get("document_number")
        or customs_document.get("document_number")
        or customs_document.get("document_id")
        or "document"
    ).strip()
    declaration_date = str(
        normalized.get("declaration_date")
        or normalized.get("document_date")
        or customs_document.get("document_date")
        or ""
    ).strip()
    header = _mapping(shipment.get("header")) if isinstance(shipment.get("header"), Mapping) else dict(shipment)
    metadata = {
        "declaration_number": declaration_number,
        "declaration_date": declaration_date,
        "supplier_order_id": str(header.get("shipment_id") or customs_document.get("supplier_order_id") or ""),
        "invoice_number": str(header.get("invoice_no") or ""),
        "invoice_date": str(header.get("invoice_date") or ""),
        "source_filename": str(
            customs_document.get("file_original_name")
            or customs_document.get("original_filename")
            or customs_document.get("source_filename")
            or ""
        ),
    }
    workbook_bytes = _render_workbook(metadata=metadata, matching=matching)
    validation = validate_customs_breakdown_workbook(
        workbook_bytes,
        expected_row_count=len(matching["rows"]),
        expected_quantity_total=matching.get("output_quantity_total"),
    )
    if not validation["valid"]:
        raise ValueError("generated customs breakdown workbook is invalid: " + "; ".join(validation["errors"]))
    receipt = {
        **{key: value for key, value in matching.items() if key != "rows"},
        "workbook_valid": True,
        "workbook_row_count": validation["row_count"],
        "workbook_quantity_total": validation["quantity_total"],
        "declaration_number": declaration_number,
        "declaration_date": declaration_date,
        "requires_review": bool(matching["requires_review"]),
        "review_message": "Расшифровка ДТ требует проверки" if matching["requires_review"] else "",
    }
    return workbook_bytes, customs_breakdown_filename(declaration_number), receipt


def match_customs_annex_items(
    *,
    annex_items: Iterable[Mapping[str, Any]],
    goods_items: Iterable[Mapping[str, Any]],
    shipment_lines: Iterable[Mapping[str, Any]],
    nomenclature_items: Iterable[Mapping[str, Any]],
    expected_quantity_total: Any = None,
    parser_quantity_conserved: Any = None,
) -> dict[str, Any]:
    """Match DT annex rows only inside the current authoritative supplier order."""

    annex_items = [dict(item) for item in annex_items if isinstance(item, Mapping)]
    goods_items = [dict(item) for item in goods_items if isinstance(item, Mapping)]
    product_lines = [
        dict(line)
        for line in shipment_lines
        if str(line.get("line_type") or "") == "product"
        and int(line.get("internal_nm_id") or 0) > 0
        and str(line.get("match_status") or "") in MATCHED_STATUSES
    ]
    product_lines.sort(key=lambda item: (int(item.get("sort_order") or 0), str(item.get("line_id") or "")))
    nomenclature_by_nm: dict[int, dict[str, Any]] = {}
    for raw in nomenclature_items:
        item = dict(raw)
        if not bool(item.get("is_active", True)):
            continue
        nm_id = int(item.get("nm_id") or item.get("internal_nm_id") or 0)
        if nm_id > 0:
            nomenclature_by_nm[nm_id] = item

    candidates = [_dt_order_candidate(line, nomenclature_by_nm) for line in product_lines]
    candidates = [candidate for candidate in candidates if candidate["nm_id"] > 0]
    by_barcode: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    by_series_models: defaultdict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        for barcode in candidate["barcodes"]:
            by_barcode[barcode].append(candidate)
        for model_keys in candidate["model_key_sets"]:
            if candidate["series"] and model_keys:
                by_series_models[(candidate["series"], model_keys)].append(candidate)

    rows: list[dict[str, Any]] = []
    status_counts: defaultdict[str, int] = defaultdict(int)
    dt_quantity_total = ZERO
    output_quantity_total = ZERO
    quantity_complete = True
    for item in annex_items:
        quantity = _decimal_or_none(item.get("quantity"))
        if quantity is None:
            quantity_complete = False
        else:
            dt_quantity_total += quantity
            output_quantity_total += quantity
        source_barcode = _digits(item.get("barcode") or _mapping(item.get("identifiers")).get("barcode"))
        policy = resolve_dt_annex_series_model(item)
        row_candidates: list[dict[str, Any]] = []
        basis = ""
        if source_barcode:
            row_candidates = _unique_dt_owners(by_barcode.get(source_barcode, []))
            if row_candidates:
                basis = "точный barcode из ДТ внутри текущего заказа"
        if source_barcode and len(row_candidates) > 1:
            row = _annex_mapping_row(
                item=item,
                policy=policy,
                quantity=quantity,
                source_barcode=source_barcode,
                status="ambiguous",
                status_ru="Неоднозначно",
                basis="barcode из ДТ принадлежит нескольким nmID текущего заказа",
            )
        else:
            if not row_candidates and policy.get("status") == "confirmed":
                key = (
                    str(policy.get("series") or ""),
                    tuple(str(value) for value in policy.get("model_keys") or []),
                )
                row_candidates = _unique_dt_owners(by_series_models.get(key, []))
                if row_candidates:
                    basis = (
                        f"{DT_ANNEX_MATCHING_POLICY_VERSION}: точная серия и полный набор iPhone model keys "
                        "внутри текущего заказа"
                    )
            if len(row_candidates) == 1:
                row = _annex_mapping_row(
                    item=item,
                    policy=policy,
                    quantity=quantity,
                    source_barcode=source_barcode,
                    candidate=row_candidates[0],
                    status="matched",
                    status_ru="Сопоставлено",
                    basis=basis,
                )
            elif len(row_candidates) > 1:
                row = _annex_mapping_row(
                    item=item,
                    policy=policy,
                    quantity=quantity,
                    source_barcode=source_barcode,
                    status="ambiguous",
                    status_ru="Неоднозначно",
                    basis="точная серия и набор моделей принадлежат нескольким nmID текущего заказа",
                )
            elif policy.get("status") == "ambiguous":
                row = _annex_mapping_row(
                    item=item,
                    policy=policy,
                    quantity=quantity,
                    source_barcode=source_barcode,
                    status="ambiguous",
                    status_ru="Неоднозначно",
                    basis="противоречивые Артикул/Модель или признаки серии в строке ДТ",
                )
            else:
                row = _annex_mapping_row(
                    item=item,
                    policy=policy,
                    quantity=quantity,
                    source_barcode=source_barcode,
                    status="unmatched",
                    status_ru="Не сопоставлено",
                    basis="нет единственного точного barcode либо exact series/model-key владельца в текущем заказе",
                )
        rows.append(row)
        status_counts[str(row["status"])] += 1

    expected_total = _decimal_or_none(expected_quantity_total)
    quantity_conserved = bool(
        quantity_complete
        and dt_quantity_total == output_quantity_total
        and (expected_total is None or expected_total == dt_quantity_total)
        and parser_quantity_conserved is not False
    )
    requires_review = bool(
        not annex_items
        or status_counts.get("ambiguous")
        or status_counts.get("unmatched")
        or not quantity_conserved
    )
    return {
        "rows": rows,
        "position_count": len(goods_items),
        "annex_item_count": len(annex_items),
        "output_row_count": len(rows),
        "status_counts": dict(sorted(status_counts.items())),
        "matched_count": status_counts.get("matched", 0),
        "ambiguous_count": status_counts.get("ambiguous", 0),
        "unmatched_count": status_counts.get("unmatched", 0),
        "dt_quantity_total": _decimal_text(dt_quantity_total) if quantity_complete else None,
        "output_quantity_total": _decimal_text(output_quantity_total) if quantity_complete else None,
        "quantity_conserved": quantity_conserved,
        "matching_policy_version": DT_ANNEX_MATCHING_POLICY_VERSION,
        "reconciliation_status": "ok" if not requires_review else "requires_review",
        "requires_review": requires_review,
    }


def match_customs_goods_items(
    *,
    goods_items: Iterable[Mapping[str, Any]],
    shipment_lines: Iterable[Mapping[str, Any]],
    nomenclature_items: Iterable[Mapping[str, Any]],
    packing_documents: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    goods_items = [dict(item) for item in goods_items if isinstance(item, Mapping)]
    product_lines = [
        dict(line)
        for line in shipment_lines
        if str(line.get("line_type") or "") == "product"
        and int(line.get("internal_nm_id") or 0) > 0
        and str(line.get("match_status") or "") in MATCHED_STATUSES
    ]
    product_lines.sort(key=lambda item: (int(item.get("sort_order") or 0), str(item.get("line_id") or "")))
    line_by_barcode: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    line_by_name: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in product_lines:
        barcode = _digits(line.get("barcode"))
        if barcode:
            line_by_barcode[barcode].append(line)
        for source_name in _line_source_names(line):
            line_by_name[source_name].append(line)

    nomenclature_by_barcode: defaultdict[str, set[int]] = defaultdict(set)
    nomenclature_name_by_nm: dict[int, str] = {}
    for item in nomenclature_items:
        if not bool(item.get("is_active", True)):
            continue
        nm_id = int(item.get("nm_id") or 0)
        if nm_id <= 0:
            continue
        nomenclature_name_by_nm[nm_id] = str(item.get("nomenclature_name") or "")
        barcode_values = [item.get("barcode"), *(item.get("barcodes") or [])]
        for value in barcode_values:
            barcode = _digits(value)
            if barcode:
                nomenclature_by_barcode[barcode].add(nm_id)

    _attach_packing_exact_names(line_by_name, product_lines, packing_documents)

    rows: list[dict[str, Any]] = []
    status_counts: defaultdict[str, int] = defaultdict(int)
    dt_quantity_total = ZERO
    output_quantity_total = ZERO
    quantity_complete = True
    for item in goods_items:
        position = str(item.get("position_number") or "")
        source_name = str(item.get("source_name") or "").strip()
        quantity = _decimal_or_none(item.get("quantity"))
        unit = str(item.get("unit") or "").strip()
        barcode = _digits(item.get("barcode") or _mapping(item.get("identifiers")).get("barcode"))
        if quantity is None:
            quantity_complete = False
        else:
            dt_quantity_total += quantity

        candidates = _unique_lines(line_by_barcode.get(barcode, [])) if barcode else []
        barcode_basis = ""
        if not candidates and barcode:
            owner_nm_ids = nomenclature_by_barcode.get(barcode, set())
            candidates = _unique_lines(
                line for line in product_lines if int(line.get("internal_nm_id") or 0) in owner_nm_ids
            )
            if candidates:
                barcode_basis = "точный barcode через server-owned номенклатуру"
        elif candidates:
            barcode_basis = "точный barcode строки заказа"

        if barcode and len(candidates) == 1:
            rows.append(
                _mapping_row(
                    item=item,
                    line=candidates[0],
                    quantity=quantity,
                    status="matched",
                    status_ru="Сопоставлено",
                    basis=barcode_basis,
                    barcode=barcode,
                    nomenclature_name_by_nm=nomenclature_name_by_nm,
                )
            )
            status_counts["matched"] += 1
            if quantity is not None:
                output_quantity_total += quantity
            continue
        if barcode and len(candidates) > 1:
            rows.append(_unmatched_row(item, quantity, "ambiguous", "Неоднозначно", "barcode принадлежит нескольким строкам заказа", barcode))
            status_counts["ambiguous"] += 1
            if quantity is not None:
                output_quantity_total += quantity
            continue

        source_model = str(_mapping(item.get("identifiers")).get("source_model") or "").strip()
        exact_source_keys = [
            key
            for key in (_name_key(source_name), _name_key(source_model))
            if key
        ]
        candidate_sets = [
            _unique_lines(line_by_name.get(key, []))
            for key in dict.fromkeys(exact_source_keys)
            if line_by_name.get(key)
        ]
        name_candidates = _unique_lines(
            line for candidates_for_key in candidate_sets for line in candidates_for_key
        )
        if len(name_candidates) == 1:
            rows.append(
                _mapping_row(
                    item=item,
                    line=name_candidates[0],
                    quantity=quantity,
                    status="matched",
                    status_ru="Сопоставлено",
                    basis="точное source model/name связанного invoice или packing list",
                    barcode=barcode,
                    nomenclature_name_by_nm=nomenclature_name_by_nm,
                )
            )
            status_counts["matched"] += 1
            if quantity is not None:
                output_quantity_total += quantity
            continue
        if len(name_candidates) > 1:
            group_total = sum((_decimal(line.get("qty")) for line in name_candidates), ZERO)
            candidate_keys_consistent = bool(
                candidate_sets
                and all(
                    {
                        str(line.get("line_id") or "")
                        for line in candidates_for_key
                    }
                    == {
                        str(line.get("line_id") or "")
                        for line in name_candidates
                    }
                    for candidates_for_key in candidate_sets
                )
            )
            if (
                candidate_keys_consistent
                and quantity is not None
                and quantity >= ZERO
                and group_total == quantity
            ):
                for line in name_candidates:
                    allocated_quantity = _decimal(line.get("qty"))
                    rows.append(
                        _mapping_row(
                            item=item,
                            line=line,
                            quantity=allocated_quantity,
                            status="reconciled_group",
                            status_ru="Сопоставлено группой",
                            basis=(
                                "точная reconciliation-группа: source name совпадает, "
                                f"сумма количеств строк заказа {group_total} = количеству позиции ДТ {quantity}"
                            ),
                            barcode=barcode,
                            nomenclature_name_by_nm=nomenclature_name_by_nm,
                            source_quantity=quantity,
                        )
                    )
                    output_quantity_total += allocated_quantity
                status_counts["reconciled_group"] += 1
                continue
            rows.append(
                _unmatched_row(
                    item,
                    quantity,
                    "ambiguous",
                    "Неоднозначно",
                    (
                        "точные source model/name дали противоречивые строки заказа"
                        if not candidate_keys_consistent
                        else "точное source model/name совпало с несколькими SKU, но контроль количества группы не сошёлся"
                    ),
                    barcode,
                )
            )
            status_counts["ambiguous"] += 1
            if quantity is not None:
                output_quantity_total += quantity
            continue

        rows.append(
            _unmatched_row(
                item,
                quantity,
                "unmatched",
                "Не сопоставлено",
                "нет точного barcode, source model/name или сходящейся reconciliation-группы",
                barcode,
            )
        )
        status_counts["unmatched"] += 1
        if quantity is not None:
            output_quantity_total += quantity

    quantity_conserved = quantity_complete and dt_quantity_total == output_quantity_total
    requires_review = bool(
        not goods_items
        or status_counts.get("ambiguous")
        or status_counts.get("unmatched")
        or not quantity_conserved
    )
    return {
        "rows": rows,
        "position_count": len(goods_items),
        "output_row_count": len(rows),
        "status_counts": dict(sorted(status_counts.items())),
        "dt_quantity_total": _decimal_text(dt_quantity_total) if quantity_complete else None,
        "output_quantity_total": _decimal_text(output_quantity_total) if quantity_complete else None,
        "quantity_conserved": quantity_conserved,
        "reconciliation_status": "ok" if not requires_review else "requires_review",
        "requires_review": requires_review,
    }


def _dt_order_candidate(
    line: Mapping[str, Any],
    nomenclature_by_nm: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any]:
    nm_id = int(line.get("internal_nm_id") or 0)
    nomenclature = _mapping(nomenclature_by_nm.get(nm_id))
    group_key = str(
        line.get("group_key")
        or line.get("product_type")
        or nomenclature.get("group_key")
        or nomenclature.get("product_type")
        or ""
    ).strip()
    model_key_sets: list[tuple[str, ...]] = []

    def add_model_key_set(values: Iterable[Any]) -> None:
        keys = tuple(sorted({str(value).strip() for value in values if str(value or "").strip()}))
        if keys and keys not in model_key_sets:
            model_key_sets.append(keys)

    for raw_keys in (line.get("compatible_model_keys"), nomenclature.get("compatible_model_keys")):
        if isinstance(raw_keys, list):
            add_model_key_set(raw_keys)
    for source in (
        line.get("model_raw"),
        _mapping(line.get("raw")).get("model_raw"),
        nomenclature.get("compatible_models_text"),
        nomenclature.get("match_key"),
        nomenclature.get("nomenclature_name"),
    ):
        add_model_key_set(normalized_model_key_set(source))
    additional_barcodes = nomenclature.get("barcodes")
    if not isinstance(additional_barcodes, list):
        additional_barcodes = []
    barcodes = {
        barcode
        for value in (
            line.get("barcode"),
            nomenclature.get("barcode"),
            *additional_barcodes,
        )
        if (barcode := _digits(value))
    }
    canonical_barcode = _digits(line.get("barcode")) or _digits(nomenclature.get("barcode"))
    if not canonical_barcode and barcodes:
        canonical_barcode = sorted(barcodes)[0]
    return {
        "nm_id": nm_id,
        "line_id": str(line.get("line_id") or ""),
        "sort_order": int(line.get("sort_order") or 0),
        "nomenclature_name": str(
            line.get("internal_name")
            or nomenclature.get("nomenclature_name")
            or nomenclature.get("internal_name")
            or ""
        ),
        "canonical_barcode": canonical_barcode,
        "barcodes": tuple(sorted(barcodes)),
        "group_key": group_key,
        "series": canonical_dt_series(group_key),
        "model_keys": model_key_sets[0] if model_key_sets else (),
        "model_key_sets": tuple(model_key_sets),
    }


def _unique_dt_owners(candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    owners: dict[int, dict[str, Any]] = {}
    for raw in candidates:
        candidate = dict(raw)
        nm_id = int(candidate.get("nm_id") or 0)
        if nm_id <= 0:
            continue
        current = owners.get(nm_id)
        candidate_key = (int(candidate.get("sort_order") or 0), str(candidate.get("line_id") or ""))
        current_key = (
            int((current or {}).get("sort_order") or 0),
            str((current or {}).get("line_id") or ""),
        )
        if current is None or candidate_key < current_key:
            owners[nm_id] = candidate
    return [owners[nm_id] for nm_id in sorted(owners)]


def _annex_mapping_row(
    *,
    item: Mapping[str, Any],
    policy: Mapping[str, Any],
    quantity: Decimal | None,
    source_barcode: str,
    status: str,
    status_ru: str,
    basis: str,
    candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = dict(candidate or {})
    identifiers = _mapping(item.get("identifiers"))
    return {
        "position_number": str(item.get("parent_position_number") or item.get("position_number") or ""),
        "annex_row_number": str(item.get("annex_row_number") or ""),
        "source_name": str(item.get("source_name") or ""),
        "source_article": str(item.get("article") or identifiers.get("article") or ""),
        "source_model": str(item.get("source_model") or identifiers.get("source_model") or ""),
        "determined_series": _dt_series_label(policy.get("series")),
        "canonical_group": str(candidate.get("group_key") or ""),
        "quantity": _decimal_text(quantity) if quantity is not None else None,
        "unit": str(item.get("unit") or ""),
        "nm_id": int(candidate.get("nm_id") or 0) or None,
        "nomenclature_name": str(candidate.get("nomenclature_name") or ""),
        "barcode": str(candidate.get("canonical_barcode") or ""),
        "source_barcode": source_barcode,
        "status": status,
        "status_ru": status_ru,
        "basis": basis,
        "customs_code": str(identifiers.get("customs_code") or item.get("customs_code") or ""),
    }


def _upgrade_legacy_matching(matching: Mapping[str, Any]) -> dict[str, Any]:
    upgraded = dict(matching)
    upgraded["rows"] = [
        {
            **dict(row),
            "annex_row_number": "",
            "source_article": "",
            "determined_series": "",
            "source_barcode": str(row.get("barcode") or ""),
        }
        for row in matching.get("rows") or []
        if isinstance(row, Mapping)
    ]
    upgraded["annex_item_count"] = 0
    upgraded["matched_count"] = sum(
        int(value)
        for key, value in dict(upgraded.get("status_counts") or {}).items()
        if key in {"matched", "reconciled_group"}
    )
    upgraded["ambiguous_count"] = int(dict(upgraded.get("status_counts") or {}).get("ambiguous") or 0)
    upgraded["unmatched_count"] = int(dict(upgraded.get("status_counts") or {}).get("unmatched") or 0)
    upgraded["matching_policy_version"] = "supplier_customs_legacy_exact_v1"
    return upgraded


def _dt_series_label(value: Any) -> str:
    return {
        "clean": "Clean",
        "anti_spy": "Anti-spy",
        "matte": "Matte",
    }.get(str(value or "").strip(), "")


def validate_customs_breakdown_workbook(
    workbook_bytes: bytes,
    *,
    expected_row_count: int,
    expected_quantity_total: Any,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        from openpyxl import load_workbook  # type: ignore

        workbook = load_workbook(BytesIO(workbook_bytes), read_only=True, data_only=True)
        worksheet = workbook["Расшифровка ДТ"]
    except Exception as exc:
        return {"valid": False, "errors": [f"openpyxl readback failed: {exc}"], "row_count": 0, "quantity_total": None}
    header_row = None
    for row_index in range(1, min(worksheet.max_row, 40) + 1):
        values = tuple(worksheet.cell(row=row_index, column=index).value for index in range(1, len(WORKBOOK_HEADERS) + 1))
        if values == WORKBOOK_HEADERS:
            header_row = row_index
            break
    if header_row is None:
        errors.append("workbook header row is missing")
        return {"valid": False, "errors": errors, "row_count": 0, "quantity_total": None}
    if expected_row_count <= 0:
        errors.append("workbook has no customs goods rows")
    row_count = 0
    quantity_total = ZERO
    for row_index in range(header_row + 1, worksheet.max_row + 1):
        position = worksheet.cell(row=row_index, column=1).value
        if position in (None, ""):
            continue
        row_count += 1
        quantity = worksheet.cell(row=row_index, column=7).value
        if quantity in (None, ""):
            errors.append(f"row {row_index} quantity is missing")
        elif not isinstance(quantity, int | float):
            errors.append(f"row {row_index} quantity is not numeric")
        else:
            quantity_total += Decimal(str(quantity))
        unit = worksheet.cell(row=row_index, column=8).value
        if unit in (None, ""):
            errors.append(f"row {row_index} quantity unit is missing")
        for column, label in ((11, "canonical barcode"), (12, "source barcode")):
            barcode_cell = worksheet.cell(row=row_index, column=column)
            if barcode_cell.value not in (None, "") and barcode_cell.data_type != "s":
                errors.append(f"row {row_index} {label} is not stored as text")
    if row_count != expected_row_count:
        errors.append(f"row count {row_count} != expected {expected_row_count}")
    expected_total = _decimal_or_none(expected_quantity_total)
    if expected_total is not None and quantity_total != expected_total:
        errors.append(f"quantity total {quantity_total} != expected {expected_total}")
    return {
        "valid": not errors,
        "errors": errors,
        "row_count": row_count,
        "quantity_total": _decimal_text(quantity_total),
    }


def customs_breakdown_filename(declaration_number: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9А-Яа-яЁё._-]+", "_", str(declaration_number or "document")).strip("._-")
    return f"DT_{safe or 'document'}_rasshifrovka.xlsx"


def _render_workbook(*, metadata: Mapping[str, Any], matching: Mapping[str, Any]) -> bytes:
    from openpyxl import Workbook  # type: ignore
    from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Расшифровка ДТ"
    worksheet.append(["Расшифровка ДТ"])
    worksheet["A1"].font = Font(bold=True, size=14)
    metadata_rows = (
        ("Номер ДТ", metadata.get("declaration_number")),
        ("Дата ДТ", metadata.get("declaration_date")),
        ("Заказ", metadata.get("supplier_order_id")),
        ("Invoice", metadata.get("invoice_number")),
        ("Дата invoice", metadata.get("invoice_date")),
        ("Исходный файл", metadata.get("source_filename")),
        ("Контроль количества", matching.get("reconciliation_status")),
        ("Итого количество по ДТ", matching.get("dt_quantity_total")),
        ("Итого количество в расшифровке", matching.get("output_quantity_total")),
        ("Version matching policy", matching.get("matching_policy_version")),
    )
    for label, value in metadata_rows:
        worksheet.append([label, value if value not in (None, "") else "—"])
    worksheet.append([])
    worksheet.append(list(WORKBOOK_HEADERS))
    header_row = worksheet.max_row
    for cell in worksheet[header_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4C1D95")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for row in matching.get("rows") or []:
        quantity = _excel_number(row.get("quantity"))
        worksheet.append(
            [
                str(row.get("position_number") or ""),
                str(row.get("annex_row_number") or ""),
                str(row.get("source_name") or ""),
                str(row.get("source_article") or ""),
                str(row.get("source_model") or ""),
                str(row.get("determined_series") or ""),
                quantity,
                str(row.get("unit") or ""),
                int(row["nm_id"]) if row.get("nm_id") not in (None, "") else None,
                str(row.get("nomenclature_name") or ""),
                str(row.get("barcode") or ""),
                str(row.get("source_barcode") or ""),
                str(row.get("status_ru") or ""),
                str(row.get("basis") or ""),
                str(row.get("customs_code") or ""),
            ]
        )
        worksheet.cell(row=worksheet.max_row, column=11).number_format = "@"
        worksheet.cell(row=worksheet.max_row, column=12).number_format = "@"
    worksheet.freeze_panes = f"A{header_row + 1}"
    worksheet.auto_filter.ref = f"A{header_row}:O{worksheet.max_row}"
    widths = (16, 18, 42, 30, 30, 22, 15, 12, 15, 34, 24, 24, 24, 70, 18)
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[chr(64 + index)].width = width
    for row in worksheet.iter_rows(min_row=header_row + 1):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    control = workbook.create_sheet("Контроль")
    control.append(["Показатель", "Значение"])
    control.append(["Статус reconciliation", matching.get("reconciliation_status")])
    control.append(["Позиций ДТ", matching.get("position_count")])
    control.append(["Строк приложения", matching.get("annex_item_count")])
    control.append(["Строк в расшифровке", matching.get("output_row_count")])
    control.append(["Сопоставлено", matching.get("matched_count")])
    control.append(["Неоднозначно", matching.get("ambiguous_count")])
    control.append(["Не сопоставлено", matching.get("unmatched_count")])
    control.append(["Итого количество ДТ", _excel_number(matching.get("dt_quantity_total"))])
    control.append(["Итого количество расшифровки", _excel_number(matching.get("output_quantity_total"))])
    control.append(["Количество сохранено", "Да" if matching.get("quantity_conserved") else "Нет"])
    control.append(["Version matching policy", matching.get("matching_policy_version")])
    for cell in control[1]:
        cell.font = Font(bold=True)
    control.column_dimensions["A"].width = 34
    control.column_dimensions["B"].width = 24
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _mapping_row(
    *,
    item: Mapping[str, Any],
    line: Mapping[str, Any],
    quantity: Decimal | None,
    status: str,
    status_ru: str,
    basis: str,
    barcode: str,
    nomenclature_name_by_nm: Mapping[int, str],
    source_quantity: Decimal | None = None,
) -> dict[str, Any]:
    nm_id = int(line.get("internal_nm_id") or 0)
    identifiers = _mapping(item.get("identifiers"))
    return {
        "position_number": str(item.get("position_number") or ""),
        "source_name": str(item.get("source_name") or ""),
        "quantity": _decimal_text(quantity) if quantity is not None else None,
        "source_quantity": _decimal_text(source_quantity if source_quantity is not None else quantity) if (source_quantity is not None or quantity is not None) else None,
        "unit": str(item.get("unit") or ""),
        "barcode": barcode,
        "source_model": str(identifiers.get("source_model") or ""),
        "customs_code": str(identifiers.get("customs_code") or ""),
        "nm_id": nm_id,
        "nomenclature_name": str(line.get("internal_name") or nomenclature_name_by_nm.get(nm_id) or ""),
        "status": status,
        "status_ru": status_ru,
        "basis": basis,
    }


def _unmatched_row(
    item: Mapping[str, Any],
    quantity: Decimal | None,
    status: str,
    status_ru: str,
    basis: str,
    barcode: str,
) -> dict[str, Any]:
    identifiers = _mapping(item.get("identifiers"))
    return {
        "position_number": str(item.get("position_number") or ""),
        "source_name": str(item.get("source_name") or ""),
        "quantity": _decimal_text(quantity) if quantity is not None else None,
        "source_quantity": _decimal_text(quantity) if quantity is not None else None,
        "unit": str(item.get("unit") or ""),
        "barcode": barcode,
        "source_model": str(identifiers.get("source_model") or ""),
        "customs_code": str(identifiers.get("customs_code") or ""),
        "nm_id": None,
        "nomenclature_name": "",
        "status": status,
        "status_ru": status_ru,
        "basis": basis,
    }


def _line_source_names(line: Mapping[str, Any]) -> set[str]:
    raw = _mapping(line.get("raw"))
    values = (
        line.get("model_raw"),
        raw.get("model_raw"),
        raw.get("name_spec"),
        raw.get("source_name"),
        raw.get("description"),
    )
    return {key for key in (_name_key(value) for value in values) if key}


def _attach_packing_exact_names(
    line_by_name: defaultdict[str, list[dict[str, Any]]],
    product_lines: list[dict[str, Any]],
    packing_documents: Iterable[Mapping[str, Any]],
) -> None:
    for document in packing_documents:
        normalized = _mapping(document.get("normalized_parse"))
        for item in normalized.get("line_items") or []:
            if not isinstance(item, Mapping):
                continue
            packing_names = {_name_key(item.get("model")), _name_key(item.get("description"))} - {""}
            for packing_name in packing_names:
                candidates = _unique_lines(
                    line for line in product_lines if packing_name in _line_source_names(line)
                )
                if candidates:
                    line_by_name[packing_name].extend(candidates)


def _unique_lines(lines: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in lines:
        line = dict(raw)
        key = str(line.get("line_id") or f"nm:{line.get('internal_nm_id')}:{line.get('sort_order')}")
        if key not in seen:
            seen.add(key)
            result.append(line)
    return result


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _name_key(value: Any) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", " ", str(value or "").casefold()).strip()


def _digits(value: Any) -> str:
    text = str(value or "").strip().replace(" ", "")
    return text if text.isascii() and text.isdigit() else ""


def _decimal(value: Any) -> Decimal:
    parsed = _decimal_or_none(value)
    return parsed if parsed is not None else ZERO


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _excel_number(value: Any) -> int | float | None:
    parsed = _decimal_or_none(value)
    if parsed is None:
        return None
    return int(parsed) if parsed == parsed.to_integral_value() else float(parsed)
