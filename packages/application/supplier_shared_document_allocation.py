"""Bounded, pure allocation for the approved 26GN724/725 shared sources.

This is deliberately not an upload API. The caller authenticates the original
bytes by SHA, supplies reviewed annex ownership, and persists each result using
the normal financial-document writer.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

OWNERS = ("26GN724", "26GN725")
OWNER_BASIS = {"26GN724": (Decimal(34000), Decimal("211625")), "26GN725": (Decimal(24750), Decimal("160500"))}
OWNER_IDS = {"26GN724": "sup_2822298fed2543a4b9c36fa0ccbc9bcc", "26GN725": "sup_345043dcc1d545e98ad3f6722bc6f2ee"}
OWNER_INVOICE_TOTALS = {"26GN724": Decimal("223525"), "26GN725": Decimal("169162.5")}
CUSTOMS = {"customs_fee_1010": ("customs_fee_1010_rub", Decimal("21344"), 0), "import_duty_2010": ("import_duty_2010_rub", Decimal("514431.57"), 1), "import_vat_5010": ("import_vat_5010_rub", Decimal("1244924.40"), 1)}
INVOICES = {"211": Decimal("113544"), "212": Decimal("5000"), "226": Decimal("755986")}
MARKER = "shared_document_allocation"


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("missing or invalid numeric source evidence") from None
    if not result.is_finite() or result < 0:
        raise ValueError("source evidence must be finite and nonnegative")
    return result


def _split(value, basis, *, cents=True):
    amount = _decimal(value)
    ratio = OWNER_BASIS[OWNERS[0]][basis] / sum((v[basis] for v in OWNER_BASIS.values()), Decimal(0))
    first = amount * ratio
    if cents:
        first = first.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    return {OWNERS[0]: first, OWNERS[1]: amount-first}


def allocate_shared_document(parsed, *, source_sha256, source_filename, owners):
    """Return independent parsed payloads; owners maps order IDs to reviewed bases.

    Each owner has quantity, product-only invoice_value_cny, invoice_total_cny,
    and DT additionally requires
    annex_quantities mapping source annex row numbers to allocated quantities.
    Shared SKU rows may occur in both orders; their quantities must conserve.
    Full original normalized metadata and expense lines remain in provenance;
    raw_parse is preserved byte-for-byte at the JSON value level.
    """
    if not re.fullmatch(r'[0-9a-fA-F]{64}', str(source_sha256)) or not source_filename:
        raise ValueError("original source SHA-256 and filename are required")
    if set(owners) != set(OWNERS):
        raise ValueError("only approved owners 26GN724/725 are supported")
    for owner, basis in OWNER_BASIS.items():
        if (_decimal(owners[owner].get('quantity')), _decimal(owners[owner].get('invoice_value_cny'))) != basis:
            raise ValueError("owner quantities/product invoice values differ from approved basis")
        if _decimal(owners[owner].get('invoice_total_cny')) != OWNER_INVOICE_TOTALS[owner]:
            raise ValueError('owner full invoice total differs from approved source')
    normalized = parsed.get('normalized_parse') or {}
    if MARKER in normalized:
        raise ValueError("source has already been allocated")
    doc_type = normalized.get('document_type')
    lines = parsed.get('expense_lines') or []
    is_dt = doc_type == 'customs_declaration'
    if is_dt:
        if normalized.get('declaration_number') != '10323020/071026/5052442':
            raise ValueError("unapproved declaration")
        if len(lines) != 3 or {line.get('category') for line in lines} != set(CUSTOMS):
            raise ValueError("DT must contain exactly the three approved expense categories")
        for line in lines:
            key, amount, _ = CUSTOMS[line['category']]
            if _decimal(line.get('amount_rub')) != amount or _decimal(line.get('amount')) != amount or _decimal(normalized.get(key)) != amount:
                raise ValueError("DT source amount differs from approved original")
        source_total = sum((v[1] for v in CUSTOMS.values()), Decimal(0))
        if _decimal(normalized.get('invoice_amount_cny')) != sum(OWNER_INVOICE_TOTALS.values(), Decimal(0)):
            raise ValueError("DT invoice value does not reconcile")
        annex = normalized.get('annex_items') or []
        row_ids = [str(row.get('annex_row_number') or '') for row in annex]
        selected = {owner: {str(k): _decimal(v) for k, v in owners[owner].get('annex_quantities', {}).items()} for owner in OWNERS}
        if not annex or '' in row_ids or len(set(row_ids)) != len(row_ids):
            raise ValueError("source annex rows require unique nonempty row numbers")
        if any(set(values) - set(row_ids) for values in selected.values()):
            raise ValueError("owner references unknown source annex row")
        for row in annex:
            if sum((selected[owner].get(str(row['annex_row_number']), Decimal(0)) for owner in OWNERS), Decimal(0)) != _decimal(row.get('quantity')):
                raise ValueError("allocated annex quantities do not conserve source row")
        for owner in OWNERS:
            if sum(selected[owner].values(), Decimal(0)) != OWNER_BASIS[owner][0]:
                raise ValueError("annex ownership does not reconcile to owner quantity")
    elif doc_type == 'logistics_invoice':
        number = str(normalized.get('document_number') or '')
        if number not in INVOICES or len(lines) != 1:
            raise ValueError("only approved logistics invoices 211/212/226 are supported")
        source_total = INVOICES[number]
        if _decimal(lines[0].get('amount_rub')) != source_total or _decimal(lines[0].get('amount')) != source_total:
            raise ValueError("logistics amount differs from approved original")
    else:
        raise ValueError("unsupported source document type")
    for field in ('total_amount', 'total_amount_rub'):
        if _decimal(normalized.get(field)) != source_total:
            raise ValueError("source total does not reconcile to expense lines")
    if any(line.get('currency') != 'RUB' for line in lines):
        raise ValueError("only original RUB expense lines are supported")
    results = {}
    for owner in OWNERS:
        result = deepcopy(parsed)
        meta = result['normalized_parse']
        allocated_lines = result['expense_lines']
        for line in allocated_lines:
            basis = CUSTOMS[line['category']][2] if is_dt else 0
            for field in ('amount', 'amount_rub', 'vat_amount_rub'):
                if line.get(field) is not None:
                    line[field] = float(_split(line[field], basis)[owner])
            line.pop('line_id', None)
            line.pop('financial_document_id', None)
            line.pop('supplier_order_id', None)
        total = sum((_decimal(line['amount_rub']) for line in allocated_lines), Decimal(0))
        meta['total_amount'] = meta['total_amount_rub'] = float(total)
        if meta.get('vat_amount_rub') is not None:
            meta['vat_amount_rub'] = float(_split(meta['vat_amount_rub'], 0)[owner])
        if is_dt:
            for category, (key, _, _) in CUSTOMS.items():
                meta[key] = next(line['amount_rub'] for line in allocated_lines if line['category'] == category)
            meta['total_customs_payments_rub'] = float(total)
            meta['allocation_metadata_quality'] = 'allocated_owner_view; weights_places_and_customs_value_estimated_from_original_parser; original_raw_evidence_retained'
            result['warnings'] = list(result.get('warnings') or []) + ['Shared source allocated to this order: goods/annex quantities follow reviewed order split; weights, places and customs value are estimates allocated from original parser metadata, not separately declared owner measurements.']
            meta['invoice_amount_cny'] = float(OWNER_INVOICE_TOTALS[owner])
            for field, basis in (('total_customs_value_rub', 1), ('customs_gross_weight_kg', 0), ('customs_net_weight_kg', 0), ('gross_weight_kg', 0), ('net_weight_kg', 0), ('total_places', 0)):
                if meta.get(field) is not None:
                    meta[field] = float(_split(meta[field], basis, cents=field == 'total_customs_value_rub')[owner])
            meta['annex_items'] = [row for row in meta['annex_items'] if selected[owner].get(str(row['annex_row_number']), Decimal(0)) > 0]
            for row in meta['annex_items']:
                row['source_declared_quantity'] = row['quantity']
                row['quantity'] = float(selected[owner][str(row['annex_row_number'])])
                row['allocation_evidence'] = 'reviewed_order_product_quantity_split_of_original_annex_row'
            positions = {str(row['parent_position_number']) for row in meta['annex_items']}
            meta['goods_items'] = [row for row in meta.get('goods_items', []) if str(row.get('position_number')) in positions]
            for row in meta['goods_items']:
                row['quantity'] = float(sum((_decimal(a['quantity']) for a in meta['annex_items'] if str(a['parent_position_number']) == str(row['position_number'])), Decimal(0)))
                row['source_declared_quantity'] = row.get('source_declared_quantity', next((original.get('quantity') for original in normalized.get('goods_items', []) if str(original.get('position_number')) == str(row['position_number'])), None))
                row['source_declared_unit'] = row.get('unit')
                row['unit'] = 'ШТ'
                row['quantity_evidence'] = 'allocated_piece_quantity_from_reviewed_order_annex_split_not_original_goods_measurement'
            meta['goods_item_count'] = meta['total_goods_count'] = len(meta['goods_items'])
            meta['annex_item_count'] = len(meta['annex_items'])
            meta['annex_quantity_total'] = float(OWNER_BASIS[owner][0])
            meta['annex_quantity_conserved'] = True
            meta['annex_parent_position_count'] = len(positions)
            meta['annex_parent_positions_complete'] = positions == {str(row['position_number']) for row in meta['goods_items']}
            if not meta['annex_parent_positions_complete']:
                raise ValueError('annex parent positions are incomplete')
        meta[MARKER] = {'version': 1, 'scope': '26GN724_26GN725_20261007', 'owner': owner, 'owner_supplier_order_id': OWNER_IDS[owner], 'source_sha256': source_sha256.lower(), 'source_filename': source_filename, 'owners': deepcopy(owners), 'source_normalized_parse': deepcopy(normalized), 'source_expense_lines': deepcopy(lines), 'allocation_rules': {'logistics': 'quantity', 'customs_fee_1010': 'quantity', 'import_duty_2010': 'invoice_value_cny', 'import_vat_5010': 'invoice_value_cny'}, 'rounding': '26GN724_HALF_UP_kopeks_26GN725_exact_residual', 'ancillary_metadata_allocation': 'estimated_weights_and_places_by_quantity_customs_value_by_invoice_value', 'ancillary_metadata_quality': 'estimated_from_original_parser_values_not_individual_declared_measurements'}
        results[owner] = result
    return results


def is_allocated_document(document):
    """Recognize and validate the persisted marker before suppressing reparse.

    Absence returns False. Any present malformed or mismatched marker raises;
    a corrupt allocation must never silently fall back to a full-source reparse.
    """
    meta = document.get('normalized_parse') or {}
    if MARKER not in meta:
        return False
    marker = meta[MARKER]
    if not isinstance(marker, dict) or marker.get('version') != 1 or marker.get('scope') != '26GN724_26GN725_20261007':
        raise ValueError('invalid shared document allocation marker')
    owner = marker.get('owner')
    if owner not in OWNERS or document.get('supplier_order_id') != OWNER_IDS[owner] or marker.get('owner_supplier_order_id') != OWNER_IDS[owner]:
        raise ValueError('shared allocation owner does not match persisted document')
    source_hash = marker.get('source_sha256')
    if not re.fullmatch(r'[0-9a-f]{64}', str(source_hash)) or document.get('file_sha256') != source_hash:
        raise ValueError('shared allocation source hash does not match original attachment')
    if not marker.get('source_filename') or not isinstance(marker.get('source_normalized_parse'), dict) or not marker.get('source_expense_lines'):
        raise ValueError('shared allocation original provenance is incomplete')
    if set(marker.get('owners') or {}) != set(OWNERS):
        raise ValueError('shared allocation owners are incomplete')
    for key, basis in OWNER_BASIS.items():
        evidence = marker['owners'][key]
        if (_decimal(evidence.get('quantity')), _decimal(evidence.get('invoice_value_cny'))) != basis:
            raise ValueError('shared allocation basis does not match approved evidence')
        if _decimal(evidence.get('invoice_total_cny')) != OWNER_INVOICE_TOTALS[key]:
            raise ValueError('shared allocation full invoice total does not match approved evidence')
    original = marker['source_normalized_parse']
    if original.get('document_type') != meta.get('document_type'):
        raise ValueError('shared allocation document type changed')
    if meta.get('document_type') == 'customs_declaration':
        if original.get('declaration_number') != '10323020/071026/5052442' or meta.get('declaration_number') != original.get('declaration_number'):
            raise ValueError('shared allocation declaration is outside approved scope')
    elif meta.get('document_type') == 'logistics_invoice':
        if str(original.get('document_number')) not in INVOICES or meta.get('document_number') != original.get('document_number'):
            raise ValueError('shared allocation invoice is outside approved scope')
    else:
        raise ValueError('shared allocation type is outside approved scope')
    return True
