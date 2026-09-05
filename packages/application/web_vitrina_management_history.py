"""Explicit, missing-only management estimates; never a warehouse/Finance input.

The receipt lives in the ready plan. Ordinary publication carries these dated
cells forward, and the final read overlay keeps their estimated provenance.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from typing import Any, Iterable

from packages.application.calculation_parameters import calculate_proxy_3, aggregate_proxy_3
from packages.application.calculation_parameters_v4 import calculate_proxy_4, aggregate_proxy_4, calculate_proxy_v4_margin_per_unit

SOURCE = "web_vitrina_management_history_v1"
FACT_SOURCE = 'web_vitrina_closed_day_facts_v1'
FACT_GROUPS = frozenset({'sales_funnel_history','seller_funnel_snapshot','web_source_snapshot','ads_compact'})
COST = "our_wb_unit_cost_rub"
TARGET_METRICS = frozenset({COST, "total_" + COST, "proxy_profit_3_rub",
    "total_proxy_profit_3_rub", "proxy_margin_3_pct", "proxy_margin_3_pct_total",
    "proxy_profit_4_rub", "total_proxy_profit_4_rub", "proxy_margin_4_pct",
    "proxy_margin_4_pct_total", "proxy_margin_per_unit_rub", "proxy_margin_per_unit_rub_total"})


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def data_sheet(plan: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in plan["sheets"] if s["sheet_name"] == "DATA_VITRINA")


def has_complete_recovery(metadata: dict[str, Any], day: str) -> bool:
    return any(cell.get('source') == FACT_SOURCE and cell.get('source_as_of_date') == day
        and set(cell.get('complete_source_groups', [])) == FACT_GROUPS
        for dates in metadata.get('server_cell_presentation', {}).values()
        for key, cell in dates.items() if key == day)


def project_complete_day(plan: dict[str, Any], *, sources: dict[str, Any], conn: Any,
                         bundle_version: str, operation_id: str) -> dict[str, Any]:
    """Bounded replacement of the previously unpublished intraday 01 September.

    All formula evaluation remains with the canonical registry evaluator. No
    stock, capital, Finance or cost values are part of this factual exception.
    """
    from types import SimpleNamespace
    import re
    from packages.application.registry_upload_db_backed_runtime import _load_config_items, _load_metric_items, _load_formula_items
    from packages.application.sheet_vitrina_v1_live_plan import _MetricEvaluator, TemporalLiveSources
    day='2026-09-01'
    if day not in plan.get('date_columns', []): return {'plan':plan,'changes':[]}
    if set(sources) != FACT_GROUPS: raise ValueError('complete-day-source-group-mismatch')
    for key, source in sources.items():
        payload=source['payload']
        if source.get('business_date') != day or source.get('complete') is not True or payload.get('kind') != 'success':
            raise ValueError('closed-day-source-not-complete:'+key)
        if source.get('fetched_at','')[:10] <= day or source.get('payload_sha256') != digest(payload):
            raise ValueError('closed-day-source-evidence-invalid:'+key)
        dates={payload.get('date',day),payload.get('snapshot_date',day),payload.get('date_from',day),payload.get('date_to',day)}
        if dates != {day}: raise ValueError('closed-day-source-date-mismatch:'+key)
    config=[x for x in _load_config_items(conn,bundle_version) if x.enabled]
    scope={x.nm_id for x in config}
    for source in sources.values():
        if not scope.issubset({int(x['nm_id']) for x in source['payload']['items']}):
            raise ValueError('closed-day-source-roster-incomplete')
    metrics={x.metric_key:x for x in _load_metric_items(conn,bundle_version)}
    formulas={x.formula_id:x for x in _load_formula_items(conn,bundle_version)}
    history={}
    for x in sources['sales_funnel_history']['payload']['items']:
        if x.get('date')!=day: raise ValueError('history-item-date-mismatch')
        history.setdefault(int(x['nm_id']),{})[x['metric']]=x['value']
    direct=set().union(*(set(x) for x in history.values()))
    direct.update({'view_count','open_card_count','ctr','views_current','ctr_current','orders_current','position_avg',
        'ads_views','ads_clicks','ads_atbs','ads_orders','ads_sum','ads_sum_price','ads_cpc','ads_ctr','ads_cr'})
    affected=set(direct)
    # Registry-driven scalar ratios/aggregates only, not economics or inventory.
    while True:
        added=set()
        for key,m in metrics.items():
            if any(word in key for word in ('proxy','cost','capital','stock','fin_','own_','onec_')): continue
            ref=formulas[m.calc_ref].expression if m.calc_type=='formula' and m.calc_ref in formulas else m.calc_ref
            if set(re.findall(r'[A-Za-z_][A-Za-z_0-9]*',ref)) & affected: added.add(key)
        if added.issubset(affected): break
        affected.update(added)
    lookups=SimpleNamespace(history_lookup=history,column_date=day)
    for group,attr in [('seller_funnel_snapshot','seller_funnel_lookup'),('web_source_snapshot','web_lookup'),('ads_compact','ads_compact_lookup')]:
        setattr(lookups,attr,{int(x['nm_id']):x for x in sources[group]['payload']['items']})
    evaluator=_MetricEvaluator(enabled_config=config,metrics_by_key=metrics,formulas_by_id=formulas,
        live_sources=TemporalLiveSources([],[],{day:lookups},{}))
    working=deepcopy(plan);sheet=data_sheet(working);index=sheet['header'].index(day)
    for row in sheet['rows']:
        scope_key,metric=row[1].split('|',1)
        if metric in affected: continue
        value=None if row[index] in ('',None) else float(row[index])
        if scope_key.startswith('SKU:'): evaluator.sku_cache[(day,int(scope_key[4:]),metric)]=value
        elif scope_key=='TOTAL': evaluator.total_cache[(day,metric)]=value
    cells=working.setdefault('metadata',{}).setdefault('server_cell_presentation',{})
    changes=[]
    for row in sheet['rows']:
        scope_key,metric=row[1].split('|',1)
        if metric not in affected: continue
        if scope_key.startswith('SKU:'): value=evaluator.resolve_sku(metric,int(scope_key[4:]),day)
        elif scope_key=='TOTAL': value=evaluator.resolve_total(metric,day)
        else: continue
        if value is None: continue
        provenance={'source':FACT_SOURCE,'state':'available','quality':'exact_closed_day_fact',
            'source_as_of_date':day,'complete_source_groups':sorted(FACT_GROUPS),
            'source_manifest':{k:{f:v for f,v in s.items() if f!='payload'} for k,s in sources.items()},
            'operation_id':operation_id,'management_value':str(value),
            'reason':'Полные фактические данные за указанный закрытый день.'}
        changes.append({'date':day,'row_id':row[1],'before':row[index],'after':value,'provenance':provenance,
                        'action':'unpublished_intraday_to_complete_closed_day'})
        row[index]=value;cells.setdefault(row[1],{})[day]=provenance
    return {'plan':working,'changes':changes}


def project(plan: dict[str, Any], *, dates: list[str], source: dict[str, Any],
            parameters: dict[str, tuple[Any, Any]], operation_id: str,
            estimate_missing_costs: bool = True) -> dict[str, Any]:
    """Calculate the exact missing-cell closure using dated saved operands."""
    after = deepcopy(plan)
    sheet = data_sheet(after)
    rows = {r[1]: r for r in sheet["rows"]}
    scopes = sorted(k.split("|")[0] for k in rows if k.endswith("|" + COST))
    expected = sorted(k.split("|")[0] for k in source["costs"] if k.startswith("SKU:"))
    if (estimate_missing_costs and scopes != expected) or not scopes:
        raise ValueError("management-history-roster-mismatch")
    presentation = after.setdefault("metadata", {}).setdefault("server_cell_presentation", {})
    changes, remaining = [], []
    source_digest = digest(source)
    for day in dates:
        if day not in sheet["header"]:
            continue
        index = sheet["header"].index(day)
        p3, p4 = parameters[day]

        def value(key: str) -> Any:
            row = rows.get(key)
            return None if row is None or len(row) <= index or row[index] in ("", None) else row[index]

        def fill(key: str, number: Any, evidence: dict[str, Any]) -> None:
            if key not in rows or value(key) is not None:
                return
            if number is None:
                remaining.append({"date": day, "row_id": key, "reason": "missing_operand_or_zero_denominator"})
                return
            old = rows[key][index] if index < len(rows[key]) else ""
            while len(rows[key]) <= index:
                rows[key].append("")
            rows[key][index] = float(number)
            reason = ("Управленческая оценка: собственная себестоимость SKU из сохранённого снимка "
                + source["column_date"] + "; остальные входы и параметры относятся к дате показателя.")
            cell = {"state": "unconfirmed", "tone": "warning", "source": SOURCE,
                "quality_state": "management_estimate", "quality_label": "Управленческая оценка",
                "reason": reason, "quality_reason": reason, "management_value": str(number),
                "source_as_of_date": source["column_date"], "source_snapshot_id": source["snapshot_id"],
                "source_plan_version": source["plan_version"], "source_bundle_version": source["bundle_version"],
                "source_digest": source_digest, "operation_id": operation_id,
                "target_date": day, "evidence": evidence}
            presentation.setdefault(key, {})[day] = cell
            changes.append({"date": day, "row_id": key, "before": old, "after": float(number), "provenance": cell})

        for scope in [*scopes, "TOTAL"]:
            key = scope + "|" + ("total_" if scope == "TOTAL" else "") + COST
            if estimate_missing_costs and day <= source["column_date"]:
                original = source["costs"][key]
                fill(key, Decimal(original["management_value"]), {"source_cost": original})
        results3, results4, inputs_by_scope = [], [], {}
        for scope in scopes:
            operands = {name: value(scope + "|" + metric) for name, metric in
                (("order_sum", "orderSum"), ("order_count", "orderCount"),
                 ("ads_sum", "ads_sum"), ("canonical_wb_wac", COST))}
            # Use the preserved exact estimate instead of its floating display representation.
            cost_cell = presentation.get(scope + "|" + COST, {}).get(day, {})
            if cost_cell.get("source") in (SOURCE, "official_fbs_management_inventory_v1"):
                operands["canonical_wb_wac"] = cost_cell["management_value"]
            inputs_by_scope[scope] = operands
            r3 = calculate_proxy_3(**operands, parameters=p3)
            r4 = calculate_proxy_4(**operands, parameters=p4, business_date=day)
            # Preserve already published profits in mixed exact/estimated totals.
            for result, metric, output in ((r3, "proxy_profit_3_rub", "proxy_profit_3"),
                                           (r4, "proxy_profit_4_rub", "proxy_profit_4")):
                saved = value(scope + "|" + metric)
                if saved is not None:
                    result[output] = Decimal(str(saved))
                    revenue = result["expected_buyout_revenue"]
                    result[output.replace("profit", "margin")] = result[output] / revenue if revenue else None
                    if output == "proxy_profit_4":
                        result["proxy_margin_per_unit"] = calculate_proxy_v4_margin_per_unit(
                            proxy_profit_4=result[output], expected_buyout_qty=result["expected_buyout_qty"],
                        )
            results3.append(r3)
            results4.append(r4)
            evidence = {"operands": operands, "operand_date": day,
                        "proxy3_version": p3.version_id, "proxy4_version": p4.version_id if p4 else None}
            for metric, number in (("proxy_profit_3_rub", r3["proxy_profit_3"]),
                ("proxy_margin_3_pct", r3["proxy_margin_3"]),
                ("proxy_profit_4_rub", r4["proxy_profit_4"]),
                ("proxy_margin_4_pct", r4["proxy_margin_4"]),
                ("proxy_margin_per_unit_rub", r4["proxy_margin_per_unit"])):
                fill(scope + "|" + metric, number, evidence)
        total3 = aggregate_proxy_3(results3)
        # Match the public evaluator: a positive-revenue SKU with unknown profit
        # blocks TOTAL; an actual zero-activity SKU may be ineligible.
        blocked4 = any(r["proxy_profit_4"] is None and
            (inputs_by_scope[s]["order_sum"] is None or Decimal(str(inputs_by_scope[s]["order_sum"])) > 0)
            for s, r in zip(scopes, results4))
        total4 = aggregate_proxy_4(results4) if not blocked4 else {}
        evidence = {"operand_date": day, "eligible_scope": scopes,
                    "input_digest": digest(inputs_by_scope), "proxy3_version": p3.version_id,
                    "proxy4_version": p4.version_id if p4 else None}
        for metric, number in (("total_proxy_profit_3_rub", total3["proxy_profit_3"]),
            ("proxy_margin_3_pct_total", total3["proxy_margin_3"]),
            ("total_proxy_profit_4_rub", total4.get("proxy_profit_4")),
            ("proxy_margin_4_pct_total", total4.get("proxy_margin_4")),
            ("proxy_margin_per_unit_rub_total", total4.get("proxy_margin_per_unit"))):
            fill("TOTAL|" + metric, number, evidence)
    # Decimal operands must be serializable without losing precision.
    after = json.loads(json.dumps(after, ensure_ascii=False, default=str))
    changes = json.loads(json.dumps(changes, ensure_ascii=False, default=str))
    return {"plan": after, "changes": changes, "remaining": remaining}


def load_presentations(conn: Any, *, bundle_version: str, dates: list[str]) -> dict[str, Any]:
    """Only explicitly applied estimates can survive ordinary publication."""
    result: dict[str, Any] = {}
    if not dates:
        return result
    placeholders = ','.join('?' for _ in dates)
    query = ("SELECT cells.key, dated.key, dated.value FROM sheet_vitrina_v1_ready_snapshots snapshot, "
        "json_each(snapshot.plan_json,'$.metadata.server_cell_presentation') cells, json_each(cells.value) dated "
        "WHERE snapshot.bundle_version=? AND dated.key IN (" + placeholders + ") "
        "AND json_extract(dated.value,'$.source')=? "
        "ORDER BY snapshot.refreshed_at,snapshot.as_of_date,snapshot.snapshot_id")
    for key, day, raw in conn.execute(query, (bundle_version, *dates, SOURCE)):
        result.setdefault(key, {})[day] = json.loads(raw)
    return result


def non_target_digest(plan: dict[str, Any], changes: list[dict[str, Any]]) -> str:
    """Fingerprint every byte-semantic field except the reviewed cell paths."""
    masked = deepcopy(plan)
    sheet = data_sheet(masked)
    rows = {r[1]: r for r in sheet['rows']}
    cells = masked.get('metadata', {}).get('server_cell_presentation', {})
    for change in changes:
        key, day = change['row_id'], change['date']
        rows[key][sheet['header'].index(day)] = '__reviewed_target__'
        if key in cells:
            cells[key].pop(day, None)
            if not cells[key]: cells.pop(key)
    metadata = masked.get('metadata', {})
    if not metadata.get('server_cell_presentation'): metadata.pop('server_cell_presentation', None)
    if not metadata: masked.pop('metadata', None)
    return digest(masked)


def preserve_applied_estimates(plan: dict[str, Any], *, original: dict[str, Any]) -> None:
    """Economics replay owns warehouse facts, not an accepted management overlay."""
    target = data_sheet(plan); before = data_sheet(original)
    rows = {r[1]:r for r in target['rows']}; old_rows = {r[1]:r for r in before['rows']}
    for key, by_date in original.get('metadata', {}).get('server_cell_presentation', {}).items():
        for day, cell in by_date.items():
            owned = cell.get('source') == SOURCE or (cell.get('source') == 'official_fbs_management_inventory_v1'
                and key.split('|')[-1] in {COST, 'total_' + COST})
            if not owned or key not in rows or key not in old_rows or day not in target['header'] or day not in before['header']:
                continue
            rows[key][target['header'].index(day)] = old_rows[key][before['header'].index(day)]
            plan.setdefault('metadata', {}).setdefault('server_cell_presentation', {}).setdefault(key, {})[day] = deepcopy(cell)


def capture_current_cost_source(db_path: Any, *, scopes: list[str], bundle_version: str, now: Any) -> dict[str, Any]:
    from packages.application.web_vitrina_official_fbs import build_current_official_fbs_estimate
    estimate = build_current_official_fbs_estimate(db_path, nm_ids=[int(s.split(':')[1]) for s in scopes], now=now)
    if not estimate.get('available'): raise ValueError('current-management-source-unavailable')
    costs = {}
    for scope in [*scopes, 'TOTAL']:
        item = estimate['total'] if scope == 'TOTAL' else estimate['skus'][int(scope.split(':')[1])]
        if item.get('cost') is None: raise ValueError('current-management-cost-incomplete')
        costs[scope + '|' + ('total_' if scope == 'TOTAL' else '') + COST] = {
            'source':'official_fbs_management_inventory_v1','source_as_of_date':estimate['date'],
            'source_generation_id':estimate['generation_id'],'source_digest':estimate['generation_digest'],
            'functional_version_id':estimate['functional_version_id'],'captured_at':estimate['captured_at'],
            'management_value':str(item['cost']),'state':'unconfirmed','tone':'warning','quality_state':'management_estimate',
            'quality_label':'Управленческая оценка','reason':'Текущая управленческая себестоимость по согласованным сохранённым складским входам.'}
    return {'column_date':estimate['date'],'snapshot_id':'current-management:' + estimate['generation_id'] + ':' + estimate['functional_version_id'],
            'plan_version':'official_fbs_management_inventory_v1','bundle_version':bundle_version,'costs':costs,
            'source_kind':'current_dated_operands'}


def carry_forward(plan: Any, *, presentation: dict[str, Any], business_date: str = "") -> Any:
    if not presentation:
        return plan
    metadata = deepcopy(dict(plan.metadata or {}))
    cells = metadata.setdefault("server_cell_presentation", {})
    sheets = []
    for sheet in plan.sheets:
        if sheet.sheet_name != "DATA_VITRINA":
            sheets.append(sheet)
            continue
        rows = deepcopy(sheet.rows)
        for row in rows:
            key = row[1]
            for day, cell in presentation.get(key, {}).items():
                if day in sheet.header and cell.get("source") == SOURCE and (not business_date or day < business_date):
                    index = sheet.header.index(day)
                    while len(row) <= index:
                        row.append("")
                    row[index] = float(cell["management_value"]) if cell.get("management_value") not in ('',None) else ''
                    cells.setdefault(key, {})[day] = deepcopy(cell)
        sheets.append(replace(sheet, rows=rows))
    return replace(plan, metadata=metadata, sheets=sheets)


def restore_rows(rows: Iterable[Any], *, presentation: dict[str, Any], business_date: str = "") -> list[Any]:
    result = []
    for row in rows:
        values, cells = dict(row.values_by_date), dict(row.presentation_by_date)
        for day, cell in presentation.get(row.row_id, {}).items():
            if day in values and cell.get("source") == SOURCE and (not business_date or day < business_date):
                values[day] = float(cell["management_value"]) if cell.get("management_value") not in ('',None) else ''
                cells[day] = dict(cell)
        result.append(replace(row, values_by_date=values, presentation_by_date=cells))
    return result


def dated_parameters(conn: Any, day: str) -> tuple[Any, Any] | None:
    from packages.application.calculation_parameters import PROXY_BLOCK_KEY, _parameters_from_row as parse3
    from packages.application.calculation_parameters_v4 import PROXY_V4_BLOCK_KEY, _parameters_from_row as parse4
    results = []
    for table, block, parse in (("sheet_vitrina_v1_calculation_parameter_versions", PROXY_BLOCK_KEY, parse3),
        ("sheet_vitrina_v1_proxy_v4_parameter_versions", PROXY_V4_BLOCK_KEY, parse4)):
        if not conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone():
            return None
        record = conn.execute('SELECT * FROM ' + table + ' WHERE block_key=? AND effective_date<=? ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1', (block, day)).fetchone()
        if record is None: return None
        results.append(parse(record))
    return tuple(results)


def recalculate_current(plan: dict[str, Any], *, business_date: str, parameters: tuple[Any, Any] | None) -> dict[str, Any]:
    """Current owned/missing proxy follows the same live cost and dated inputs."""
    if business_date not in plan.get('date_columns', []):
        return plan
    cells = plan.get('metadata', {}).get('server_cell_presentation', {})
    costs = {key: by_date[business_date] for key, by_date in cells.items()
        if key.split('|')[-1] in (COST, 'total_' + COST) and business_date in by_date
        and by_date[business_date].get('source') == 'official_fbs_management_inventory_v1'
        and by_date[business_date].get('source_as_of_date') == business_date
        and by_date[business_date].get('management_value') not in ('', None)}
    cost_rows = [r[1] for r in data_sheet(plan)['rows'] if r[1].split('|')[-1] in (COST, 'total_' + COST)]
    working = deepcopy(plan)
    working.setdefault('metadata', {}).setdefault('server_cell_presentation', {})
    sheet = data_sheet(working); index = sheet['header'].index(business_date)
    def keep_costs(revised):
        originals = {r[1]: r for r in data_sheet(plan)['rows'] if r[1] in cost_rows}
        for row in data_sheet(revised)['rows']:
            if row[1] in originals:
                row[index] = originals[row[1]][index]
                original = cells.get(row[1], {}).get(business_date)
                target = revised['metadata']['server_cell_presentation'].setdefault(row[1], {})
                if original is None: target.pop(business_date, None)
                else: target[business_date] = deepcopy(original)
        return revised
    for row in sheet['rows']:
        if row[1] in cost_rows and row[1] not in costs:
            row[index] = ''
            working['metadata']['server_cell_presentation'].setdefault(row[1], {})[business_date] = {}
        if row[1].split('|')[-1] in TARGET_METRICS - {COST, 'total_' + COST}:
            cell = cells.get(row[1], {}).get(business_date, {})
            if cell.get('source') == SOURCE or row[index] in ('',None):
                row[index] = ''
                working['metadata']['server_cell_presentation'].setdefault(row[1], {})[business_date] = {
                    'source':SOURCE, 'state':'unavailable', 'management_value':'',
                    'reason':'Недостаточно датированных входов для текущей управленческой оценки.'}
    if parameters is None or not cost_rows:
        return keep_costs(working)
    source = {'column_date':business_date, 'snapshot_id':plan.get('snapshot_id', 'current-read'),
        'plan_version':plan.get('plan_version','current-read'), 'bundle_version':plan.get('bundle_version','current-read'), 'costs':costs}
    result = project(working, dates=[business_date], source=source, parameters={business_date:parameters},
        operation_id='current-management-proxy:' + business_date, estimate_missing_costs=False)
    return keep_costs(result['plan'])


def recalculate_current_envelope(plan: Any, *, business_date: str, parameters: tuple[Any, Any] | None) -> Any:
    from dataclasses import asdict
    revised = recalculate_current(asdict(plan), business_date=business_date, parameters=parameters)
    revised_data = data_sheet(revised)
    return replace(plan, metadata=revised.get('metadata', {}), sheets=[
        replace(s, rows=revised_data['rows']) if s.sheet_name == 'DATA_VITRINA' else s for s in plan.sheets])


def recalculate_current_rows(rows: Iterable[Any], *, business_date: str, parameters: tuple[Any, Any] | None,
                             original_presentation: dict[str, Any], snapshot_id: str) -> list[Any]:
    rows = list(rows)
    if not rows or business_date not in rows[0].values_by_date:
        return rows
    presentation = {r.row_id:{business_date:dict(r.presentation_by_date.get(business_date, {}))} for r in rows}
    # Read-time quality overlays may replace an owned proxy marker. Ownership
    # comes from the saved plan; live costs come from the just-applied FBS view.
    for row in rows:
        original = original_presentation.get(row.row_id, {}).get(business_date, {})
        if original.get('source') == SOURCE:
            presentation[row.row_id][business_date] = original
    pseudo = {'date_columns':[business_date], 'snapshot_id':snapshot_id, 'metadata':{'server_cell_presentation':presentation},
              'sheets':[{'sheet_name':'DATA_VITRINA','header':['label','key',business_date],
                         'rows':[['',r.row_id,r.values_by_date.get(business_date,'')] for r in rows]}]}
    revised = recalculate_current(pseudo, business_date=business_date, parameters=parameters)
    return restore_rows(rows, presentation=revised['metadata']['server_cell_presentation'])
