"""One logical SKU/TOTAL review-rating metric in the runtime catalog."""
from datetime import date
from packages.contracts.registry_upload_bundle_v1 import MetricV2Item

SOURCE_KEY = "card_rating"
SKU_METRIC_KEY = "card_rating"
TOTAL_METRIC_KEY = "avg_card_rating"


def extend_metrics_with_card_rating(metrics):
    existing = list(metrics)
    keys = {item.metric_key for item in existing}
    return existing + [
        MetricV2Item(metric_key=key, enabled=True, scope=scope,
                     label_ru="Рейтинг карточки", calc_type="metric",
                     calc_ref=SKU_METRIC_KEY, show_in_data=True, format="rating",
                     display_order=520, section="Общие")
        for key, scope in ((SKU_METRIC_KEY, "SKU"), (TOTAL_METRIC_KEY, "TOTAL"))
        if key not in keys
    ]


def card_rating_presentation(*, rows, slots, live_sources):
    """Explain missing and preserved observations without inventing a rating."""
    statuses = {status.temporal_slot: status for status in live_sources.statuses
                if status.source_key == SOURCE_KEY}
    result = {}
    for slot in slots:
        status = statuses.get(slot.slot_key)
        if status is None:
            continue
        stale = "preserved_after_invalid_attempt" in status.note
        source = live_sources.slot_lookups[slot.slot_key]
        lookup = source.card_rating_lookup
        measured = source.card_rating_observed_at or max((getattr(item, "observed_at", "") for item in lookup.values()), default="")
        period = source.card_rating_request_period
        provenance = (f"Период запроса: {period['start']} — {period['end']}. " if period else
                      "Период запроса старого наблюдения неизвестен. ")
        if period and source.card_rating_request_period_policy == "last_7_completed_business_days_v1":
            provenance += "Последние 7 завершённых дней на момент наблюдения. "
        provenance += f"Наблюдение источника: {measured}. " if measured else "Время наблюдения источника неизвестно. "
        for row in rows:
            if row[1].split("|", 1)[-1] not in {SKU_METRIC_KEY, TOTAL_METRIC_KEY}:
                continue
            value = row[2 + list(slots).index(slot)]
            missing = value in (None, "")
            reason = ("Рейтинг по отзывам из отчёта WB «Оценки и отзывы», из 5. " + provenance +
                ("Последнее подтверждённое наблюдение; текущее обновление не удалось. " if stale else "") +
                ("Рейтинг для этой даты не подтверждён; отсутствие не равно нулю." if missing else
                 "TOTAL — арифметическое среднее SKU с доступным рейтингом." if row[1].startswith("TOTAL|") else
                 "Исходное значение рейтинга из отчёта WB."))
            requested = getattr(source, "card_rating_requested_count", None)
            covered = getattr(source, "card_rating_covered_count", None)
            if row[1].startswith("TOTAL|") and type(requested) is int and type(covered) is int and 0 <= covered <= requested:
                reason += f" В ответе WB доступен рейтинг {covered} из {requested} запрошенных SKU."
            result.setdefault(row[1], {})[slot.column_date] = {
                "source": SOURCE_KEY, "source_as_of_date": slot.column_date,
                "source_observed_at": measured, "source_request_period": period,
                "quality_state": "missing" if missing else "stale" if stale else "exact",
                "quality_reason": reason, "reason": reason,
            }
    return result


def include_card_rating_rows(rows, *, config, dates, metrics):
    """Expose built-ins on an older ready snapshot; unobserved dates stay blank."""
    from packages.contracts.web_vitrina_contract import WebVitrinaContractRow
    result = list(rows)
    existing = {row.row_id for row in result}
    additions = [("TOTAL", None, metrics[TOTAL_METRIC_KEY])]
    additions += [(f"SKU:{item.nm_id}", item, metrics[SKU_METRIC_KEY])
                  for item in config if item.enabled]
    for scope, item, metric in additions:
        row_id = scope + "|" + metric.metric_key
        if row_id in existing:
            continue
        result.append(WebVitrinaContractRow(
            row_id=row_id, row_order=len(result) + 1,
            scope_kind="SKU" if item else "TOTAL", scope_key=scope,
            scope_label=item.display_name if item else "Итого",
            metric_key=metric.metric_key, metric_label=metric.label_ru,
            row_last_updated_at="", section=metric.section,
            group=item.group if item else None, nm_id=item.nm_id if item else None,
            format=metric.format, values_by_date={day: "" for day in dates},
            presentation_by_date={day: {"source": SOURCE_KEY, "quality_state": "missing",
                "reason": "Рейтинг по отзывам из отчёта WB «Оценки и отзывы», из 5: для этой даты нет сохранённого наблюдения."}
                for day in dates},
        ))
    return result


def include_card_rating_catalog_presentation(catalog):
    """A missing catalog day still declares rendering for dated observations.

    History reuses one static catalog across dates. Keep its rendering contract
    independent of whether that one day's rating happened to be available.
    Dated cells and their precision/quality are never changed here.
    """
    if not any(row_id.split("|", 1)[-1] in {SKU_METRIC_KEY, TOTAL_METRIC_KEY}
               for row_id in catalog["rows"]):
        return catalog
    from dataclasses import asdict
    from packages.application.web_vitrina_view_model import _FORMATTER_LIBRARY
    from packages.application.web_vitrina_gravity_table_adapter import _renderer_id
    from packages.contracts.web_vitrina_gravity_table_adapter import WebVitrinaGravityTableRenderer
    presentation = catalog.setdefault("presentation", {})
    formatters = presentation.setdefault("formatters", [])
    if not any(item["formatter_id"] == "rating" for item in formatters):
        formatters.append(asdict(_FORMATTER_LIBRARY["rating"]))
    renderer_id = _renderer_id(cell_kind="number", formatter_id="rating")
    renderers = presentation.setdefault("renderers", [])
    if not any(item["renderer_id"] == renderer_id for item in renderers):
        renderers.append(asdict(WebVitrinaGravityTableRenderer(
            renderer_id=renderer_id, gravity_variant="text", formatter_id="rating",
            align="end", placeholder_text=_FORMATTER_LIBRARY["rating"].null_display,
        )))
    return catalog


def normalize_request_period(period):
    """Preserve explicit provenance, including deserialized legacy namespaces."""
    if period is None:
        return None
    start = period.get("start") if isinstance(period, dict) else getattr(period, "start", None)
    end = period.get("end") if isinstance(period, dict) else getattr(period, "end", None)
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("item-rating request_period requires start/end dates")
    if date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end or start > end:
        raise ValueError("item-rating request_period requires ordered ISO dates")
    return {"start": start, "end": end}
