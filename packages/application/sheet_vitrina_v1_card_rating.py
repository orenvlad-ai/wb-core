"""One logical SKU/TOTAL review-rating metric in the runtime catalog."""
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
        lookup = live_sources.slot_lookups[slot.slot_key].card_rating_lookup
        measured = max((getattr(item, "observed_at", "") for item in lookup.values()), default="")
        for row in rows:
            if row[1].split("|", 1)[-1] not in {SKU_METRIC_KEY, TOTAL_METRIC_KEY}:
                continue
            value = row[2 + list(slots).index(slot)]
            missing = value in (None, "")
            reason = ("Рейтинг по отзывам WB из 5. " +
                ("Последнее подтверждённое наблюдение; текущее обновление не удалось. " if stale else "") +
                ("Рейтинг для этой даты не подтверждён; отсутствие не равно нулю." if missing else
                 "TOTAL — арифметическое среднее SKU с доступным рейтингом."))
            result.setdefault(row[1], {})[slot.column_date] = {
                "source": SOURCE_KEY, "source_as_of_date": slot.column_date,
                "source_observed_at": measured,
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
                "reason": "Рейтинг по отзывам WB из 5: для этой даты нет сохранённого наблюдения."}
                for day in dates},
        ))
    return result
