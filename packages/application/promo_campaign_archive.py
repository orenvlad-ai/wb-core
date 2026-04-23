"""Archive-first promo campaign storage and interval-based replay."""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Iterable

from openpyxl import load_workbook

from packages.application.promo_metric_truth import (
    PromoCandidateRow,
    PromoEligibilityEvaluation,
    evaluate_candidate_rows,
    find_workbook_data_sheet,
    iter_workbook_plan_rows,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.promo_live_source import (
    PromoLiveSourceIncomplete,
    PromoLiveSourceItem,
    PromoLiveSourceSuccess,
)
from packages.contracts.promo_xlsx_collector_block import PromoMetadata


PROMO_CAMPAIGN_ARCHIVE_DIRNAME = "promo_campaign_archive"
ARCHIVE_RECORD_FILENAME = "archive_record.json"
ARCHIVE_METADATA_FILENAME = "metadata.json"
ARCHIVE_WORKBOOK_FILENAME = "workbook.xlsx"
ARCHIVE_WORKBOOK_INSPECTION_FILENAME = "workbook_inspection.json"
PRICES_SOURCE_KEY = "prices_snapshot"
PRICES_ACCEPTED_CURRENT_ROLE = "accepted_current_snapshot"


@dataclass(frozen=True)
class PromoCampaignArchiveRecord:
    archive_key: str
    archive_dir: str
    metadata_fingerprint: str
    workbook_fingerprint: str | None
    workbook_present: bool
    workbook_path: str | None
    workbook_inspection_path: str | None
    collected_at: str
    downloaded_at: str | None
    metadata: PromoMetadata


@dataclass(frozen=True)
class PromoCampaignArchiveSyncSummary:
    scanned_promo_dirs: int = 0
    created_records: int = 0
    updated_records: int = 0
    unchanged_records: int = 0

    def to_note(self) -> str:
        return (
            f"archive_scanned={self.scanned_promo_dirs}; "
            f"archive_created={self.created_records}; "
            f"archive_updated={self.updated_records}; "
            f"archive_unchanged={self.unchanged_records}"
        )


@dataclass(frozen=True)
class DailyPriceTruthResolution:
    price_by_nm_id: dict[int, float]
    source_note: str


def promo_campaign_archive_root(runtime_dir: Path) -> Path:
    return runtime_dir / PROMO_CAMPAIGN_ARCHIVE_DIRNAME


def load_promo_campaign_archive(runtime_dir: Path) -> list[PromoCampaignArchiveRecord]:
    archive_root = promo_campaign_archive_root(runtime_dir)
    if not archive_root.exists():
        return []
    records: list[PromoCampaignArchiveRecord] = []
    for record_path in sorted(archive_root.glob(f"*/{ARCHIVE_RECORD_FILENAME}")):
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        metadata_payload = payload.get("metadata", {})
        records.append(
            PromoCampaignArchiveRecord(
                archive_key=str(payload.get("archive_key", "") or ""),
                archive_dir=str(payload.get("archive_dir", "") or record_path.parent),
                metadata_fingerprint=str(payload.get("metadata_fingerprint", "") or ""),
                workbook_fingerprint=_normalize_optional_text(payload.get("workbook_fingerprint")),
                workbook_present=bool(payload.get("workbook_present", False)),
                workbook_path=_normalize_optional_text(payload.get("workbook_path")),
                workbook_inspection_path=_normalize_optional_text(payload.get("workbook_inspection_path")),
                collected_at=str(payload.get("collected_at", "") or ""),
                downloaded_at=_normalize_optional_text(payload.get("downloaded_at")),
                metadata=PromoMetadata(**metadata_payload),
            )
        )
    return records


def resolve_reusable_campaign(
    *,
    archive_root: Path,
    metadata: PromoMetadata,
) -> PromoCampaignArchiveRecord | None:
    matches: list[PromoCampaignArchiveRecord] = []
    for record in load_promo_campaign_archive(archive_root.parent):
        workbook_path = Path(record.workbook_path) if record.workbook_path else None
        if not record.workbook_present or workbook_path is None or not workbook_path.exists():
            continue
        if _record_matches_live_metadata(record=record, metadata=metadata):
            matches.append(record)
    if not matches:
        return None
    matches.sort(
        key=lambda record: (
            record.downloaded_at or "",
            record.collected_at,
            record.archive_key,
        ),
        reverse=True,
    )
    return matches[0]


def sync_promo_campaign_archive(runtime_dir: Path) -> PromoCampaignArchiveSyncSummary:
    runs_root = runtime_dir / "promo_xlsx_collector_runs"
    archive_root = promo_campaign_archive_root(runtime_dir)
    archive_root.mkdir(parents=True, exist_ok=True)
    summary = PromoCampaignArchiveSyncSummary()
    if not runs_root.exists():
        return summary
    created = 0
    updated = 0
    unchanged = 0
    scanned = 0
    for metadata_path in sorted(runs_root.glob("*/promos/*/metadata.json")):
        scanned += 1
        changed = _sync_archive_record_from_metadata(
            metadata_path=metadata_path,
            archive_root=archive_root,
        )
        if changed == "created":
            created += 1
        elif changed == "updated":
            updated += 1
        else:
            unchanged += 1
    return PromoCampaignArchiveSyncSummary(
        scanned_promo_dirs=scanned,
        created_records=created,
        updated_records=updated,
        unchanged_records=unchanged,
    )


def materialize_promo_result_from_archive(
    *,
    runtime_dir: Path,
    snapshot_date: str,
    requested_nm_ids: Iterable[int],
    sync_summary: PromoCampaignArchiveSyncSummary | None = None,
    trace_run_dir: str | None = None,
    detail_prefix: str | None = None,
) -> PromoLiveSourceSuccess | PromoLiveSourceIncomplete:
    sync_state = sync_summary or sync_promo_campaign_archive(runtime_dir)
    archive_root = promo_campaign_archive_root(runtime_dir)
    requested = sorted({int(nm_id) for nm_id in requested_nm_ids})
    slot_date = date.fromisoformat(snapshot_date)
    records = load_promo_campaign_archive(runtime_dir)
    covering = [record for record in records if _record_covers_date(record, slot_date)]
    usable = [
        record
        for record in covering
        if record.workbook_present
        and record.workbook_path
        and Path(record.workbook_path).exists()
    ]
    missing_artifacts = [record for record in covering if record not in usable]

    common = dict(
        snapshot_date=snapshot_date,
        date_from=snapshot_date,
        date_to=snapshot_date,
        requested_count=len(requested),
        trace_run_dir=trace_run_dir or str(archive_root),
        current_promos=len(covering),
        current_promos_downloaded=len(usable),
        current_promos_blocked=len(missing_artifacts),
        future_promos=0,
        skipped_past_promos=0,
        ambiguous_promos=0,
        current_download_export_kinds=sorted(
            {
                str(record.metadata.export_kind)
                for record in usable
                if record.metadata.export_kind
            }
        ),
    )

    detail_parts = [
        "archive_mode=interval_replay",
        sync_state.to_note(),
        f"covering_campaigns={len(covering)}",
        f"usable_campaigns={len(usable)}",
    ]
    if detail_prefix:
        detail_parts.insert(0, detail_prefix)

    if missing_artifacts:
        missing_keys = ",".join(record.archive_key for record in missing_artifacts[:8])
        return PromoLiveSourceIncomplete(
            kind="incomplete",
            covered_count=0,
            items=[],
            missing_nm_ids=requested,
            detail="; ".join(detail_parts + [f"missing_campaign_artifacts={missing_keys}"]),
            **common,
        )

    candidate_rows_by_nm_id: dict[int, list[PromoCandidateRow]] = {
        nm_id: []
        for nm_id in requested
    }
    candidate_row_count = 0
    for record in usable:
        candidate_row_count += _merge_archive_workbook_rows(
            candidate_rows_by_nm_id=candidate_rows_by_nm_id,
            workbook_path=Path(str(record.workbook_path)),
            requested_nm_ids=requested,
            campaign_identity=_campaign_identity(record),
        )
    detail_parts.append(f"requested_candidate_rows={candidate_row_count}")

    requested_nm_ids_with_candidates = sorted(
        nm_id
        for nm_id, candidate_rows in candidate_rows_by_nm_id.items()
        if candidate_rows
    )
    if not requested_nm_ids_with_candidates:
        return PromoLiveSourceSuccess(
            kind="success",
            covered_count=len(requested),
            items=_build_zero_items(snapshot_date=snapshot_date, requested_nm_ids=requested),
            detail="; ".join(
                detail_parts
                + [
                    "daily_price_source=not_needed",
                    "no_covering_campaign_rows_for_requested_nm_ids=true",
                ]
            ),
            **common,
        )

    price_truth = _load_daily_price_truth(
        runtime_dir=runtime_dir,
        snapshot_date=snapshot_date,
        requested_nm_ids=requested_nm_ids_with_candidates,
    )
    detail_parts.append(price_truth.source_note)
    missing_price_truth_nm_ids = sorted(
        nm_id
        for nm_id in requested_nm_ids_with_candidates
        if nm_id not in price_truth.price_by_nm_id
    )

    materialized_items: list[PromoLiveSourceItem] = []
    truthful_zero_no_candidate_nm_ids: list[int] = []
    truthful_zero_ineligible_nm_ids: list[int] = []
    eligible_nm_ids: list[int] = []
    for nm_id in requested:
        candidate_rows = candidate_rows_by_nm_id[nm_id]
        if candidate_rows and nm_id in missing_price_truth_nm_ids:
            continue
        item, evaluation = _build_item_from_candidate_rows(
            snapshot_date=snapshot_date,
            nm_id=nm_id,
            candidate_rows=candidate_rows,
            price_seller_discounted=price_truth.price_by_nm_id.get(nm_id),
        )
        materialized_items.append(item)
        if not candidate_rows:
            truthful_zero_no_candidate_nm_ids.append(nm_id)
        elif evaluation.eligible_campaign_identities:
            eligible_nm_ids.append(nm_id)
        else:
            truthful_zero_ineligible_nm_ids.append(nm_id)

    detail_parts.extend(
        _promo_metric_coverage_detail_parts(
            eligible_nm_ids=eligible_nm_ids,
            truthful_zero_no_candidate_nm_ids=truthful_zero_no_candidate_nm_ids,
            truthful_zero_ineligible_nm_ids=truthful_zero_ineligible_nm_ids,
            missing_price_truth_nm_ids=missing_price_truth_nm_ids,
        )
    )
    archive_keys = ",".join(record.archive_key for record in usable[:8])
    if missing_price_truth_nm_ids:
        return PromoLiveSourceIncomplete(
            kind="incomplete",
            covered_count=len(requested) - len(missing_price_truth_nm_ids),
            items=materialized_items,
            missing_nm_ids=missing_price_truth_nm_ids,
            detail="; ".join(
                detail_parts
                + [
                    "daily_price_truth_required=true",
                    "missing_daily_price_truth_nm_ids="
                    + ",".join(str(nm_id) for nm_id in missing_price_truth_nm_ids),
                    f"archive_keys={archive_keys}",
                ]
            ),
            **common,
        )
    return PromoLiveSourceSuccess(
        kind="success",
        covered_count=len(requested),
        items=materialized_items,
        detail="; ".join(detail_parts + [f"archive_keys={archive_keys}"]),
        **common,
    )


def _sync_archive_record_from_metadata(
    *,
    metadata_path: Path,
    archive_root: Path,
) -> str:
    promo_dir = metadata_path.parent
    metadata = PromoMetadata(**json.loads(metadata_path.read_text(encoding="utf-8")))
    archive_key = _archive_key(metadata)
    archive_dir = archive_root / archive_key
    archive_dir.mkdir(parents=True, exist_ok=True)

    existing_record = _load_archive_record(archive_dir / ARCHIVE_RECORD_FILENAME)
    workbook_src = promo_dir / "workbook.xlsx"
    inspection_src = promo_dir / "workbook_inspection.json"
    workbook_present = workbook_src.exists()
    workbook_fingerprint = _sha256_path(workbook_src) if workbook_present else None

    archive_workbook_path = archive_dir / ARCHIVE_WORKBOOK_FILENAME
    archive_inspection_path = archive_dir / ARCHIVE_WORKBOOK_INSPECTION_FILENAME
    record_changed = existing_record is None

    if workbook_present:
        if (
            existing_record is None
            or existing_record.workbook_fingerprint != workbook_fingerprint
            or not archive_workbook_path.exists()
        ):
            shutil.copy2(workbook_src, archive_workbook_path)
            record_changed = True
        if inspection_src.exists():
            if not archive_inspection_path.exists() or archive_inspection_path.read_bytes() != inspection_src.read_bytes():
                shutil.copy2(inspection_src, archive_inspection_path)
                record_changed = True
    elif existing_record and existing_record.workbook_present and existing_record.workbook_path:
        archive_workbook_path = Path(existing_record.workbook_path)
        if existing_record.workbook_inspection_path:
            archive_inspection_path = Path(existing_record.workbook_inspection_path)
        workbook_fingerprint = existing_record.workbook_fingerprint
    else:
        archive_workbook_path = archive_dir / ARCHIVE_WORKBOOK_FILENAME
        archive_inspection_path = archive_dir / ARCHIVE_WORKBOOK_INSPECTION_FILENAME

    normalized_metadata = _normalize_metadata_for_archive(
        metadata=metadata,
        workbook_path=archive_workbook_path if archive_workbook_path.exists() else None,
    )
    metadata_fingerprint = _metadata_fingerprint(normalized_metadata)
    if existing_record is None or existing_record.metadata_fingerprint != metadata_fingerprint:
        record_changed = True

    archive_metadata_path = archive_dir / ARCHIVE_METADATA_FILENAME
    metadata_payload = asdict(normalized_metadata)
    existing_metadata_bytes = archive_metadata_path.read_bytes() if archive_metadata_path.exists() else None
    new_metadata_bytes = json.dumps(metadata_payload, ensure_ascii=False, indent=2).encode("utf-8")
    if existing_metadata_bytes != new_metadata_bytes:
        archive_metadata_path.write_bytes(new_metadata_bytes)
        record_changed = True

    record = PromoCampaignArchiveRecord(
        archive_key=archive_key,
        archive_dir=str(archive_dir),
        metadata_fingerprint=metadata_fingerprint,
        workbook_fingerprint=workbook_fingerprint,
        workbook_present=bool(archive_workbook_path.exists()),
        workbook_path=str(archive_workbook_path) if archive_workbook_path.exists() else None,
        workbook_inspection_path=(
            str(archive_inspection_path)
            if archive_inspection_path.exists()
            else None
        ),
        collected_at=normalized_metadata.collected_at,
        downloaded_at=(
            normalized_metadata.collected_at if workbook_src.exists() else (
                existing_record.downloaded_at if existing_record is not None else None
            )
        ),
        metadata=normalized_metadata,
    )
    record_payload = json.dumps(asdict(record), ensure_ascii=False, indent=2).encode("utf-8")
    record_path = archive_dir / ARCHIVE_RECORD_FILENAME
    existing_record_bytes = record_path.read_bytes() if record_path.exists() else None
    if existing_record_bytes != record_payload:
        record_path.write_bytes(record_payload)
        record_changed = True

    if existing_record is None:
        return "created"
    if record_changed:
        return "updated"
    return "unchanged"


def _load_archive_record(record_path: Path) -> PromoCampaignArchiveRecord | None:
    if not record_path.exists():
        return None
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    return PromoCampaignArchiveRecord(
        archive_key=str(payload.get("archive_key", "") or ""),
        archive_dir=str(payload.get("archive_dir", "") or record_path.parent),
        metadata_fingerprint=str(payload.get("metadata_fingerprint", "") or ""),
        workbook_fingerprint=_normalize_optional_text(payload.get("workbook_fingerprint")),
        workbook_present=bool(payload.get("workbook_present", False)),
        workbook_path=_normalize_optional_text(payload.get("workbook_path")),
        workbook_inspection_path=_normalize_optional_text(payload.get("workbook_inspection_path")),
        collected_at=str(payload.get("collected_at", "") or ""),
        downloaded_at=_normalize_optional_text(payload.get("downloaded_at")),
        metadata=PromoMetadata(**payload.get("metadata", {})),
    )


def _normalize_metadata_for_archive(
    *,
    metadata: PromoMetadata,
    workbook_path: Path | None,
) -> PromoMetadata:
    payload = asdict(metadata)
    if workbook_path is not None and workbook_path.exists():
        payload["saved_path"] = str(workbook_path)
        payload["saved_filename"] = workbook_path.name
    return PromoMetadata(**payload)


def _record_covers_date(record: PromoCampaignArchiveRecord, slot_date: date) -> bool:
    if not record.metadata.promo_start_at or not record.metadata.promo_end_at:
        return False
    start_date = date.fromisoformat(record.metadata.promo_start_at[:10])
    end_date = date.fromisoformat(record.metadata.promo_end_at[:10])
    return start_date <= slot_date <= end_date


def _record_matches_live_metadata(
    *,
    record: PromoCampaignArchiveRecord,
    metadata: PromoMetadata,
) -> bool:
    if metadata.promo_id is not None and record.metadata.promo_id != metadata.promo_id:
        return False
    comparable_fields = (
        "promo_title",
        "promo_period_text",
        "promo_start_at",
        "promo_end_at",
        "period_parse_confidence",
        "temporal_classification",
        "promo_status",
        "promo_status_text",
        "eligible_count",
        "participating_count",
        "excluded_count",
        "source_tab",
        "source_filter_code",
    )
    return all(
        getattr(record.metadata, field_name) == getattr(metadata, field_name)
        for field_name in comparable_fields
    )


def _merge_archive_workbook_rows(
    *,
    candidate_rows_by_nm_id: dict[int, list[PromoCandidateRow]],
    workbook_path: Path,
    requested_nm_ids: Iterable[int],
    campaign_identity: str,
) -> int:
    requested = {int(nm_id) for nm_id in requested_nm_ids}
    workbook = load_workbook(filename=str(workbook_path), read_only=False, data_only=True)
    row_count = 0
    try:
        sheet, header_row_index = find_workbook_data_sheet(workbook)
        for row in iter_workbook_plan_rows(
            sheet=sheet,
            header_row_index=header_row_index,
            requested_nm_ids=requested,
        ):
            candidate_rows_by_nm_id.setdefault(row.nm_id, []).append(
                PromoCandidateRow(
                    nm_id=row.nm_id,
                    campaign_identity=campaign_identity,
                    plan_price=row.plan_price,
                )
            )
            row_count += 1
    finally:
        workbook.close()
    return row_count


def _load_daily_price_truth(
    *,
    runtime_dir: Path,
    snapshot_date: str,
    requested_nm_ids: Iterable[int],
) -> DailyPriceTruthResolution:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    payload, captured_at = runtime.load_temporal_source_slot_snapshot(
        source_key=PRICES_SOURCE_KEY,
        snapshot_date=snapshot_date,
        snapshot_role=PRICES_ACCEPTED_CURRENT_ROLE,
    )
    if _is_exact_prices_payload(payload, snapshot_date):
        note = "daily_price_source=prices_snapshot.accepted_current_snapshot"
        if captured_at:
            note = f"{note}; daily_price_captured_at={captured_at}"
        return DailyPriceTruthResolution(
            price_by_nm_id=_extract_price_by_nm_id(
                payload=payload,
                requested_nm_ids=requested_nm_ids,
            ),
            source_note=note,
        )

    return DailyPriceTruthResolution(
        price_by_nm_id={},
        source_note="daily_price_source=missing",
    )


def _extract_price_by_nm_id(
    *,
    payload: object,
    requested_nm_ids: Iterable[int],
) -> dict[int, float]:
    requested = {int(nm_id) for nm_id in requested_nm_ids}
    price_by_nm_id: dict[int, float] = {}
    for item in list(getattr(payload, "items", []) or []):
        nm_id = getattr(item, "nm_id", None)
        price = getattr(item, "price_seller_discounted", None)
        if not isinstance(nm_id, int) or nm_id not in requested:
            continue
        if not isinstance(price, (int, float)):
            continue
        price_by_nm_id[nm_id] = float(price)
    return price_by_nm_id


def _is_exact_prices_payload(payload: object | None, snapshot_date: str) -> bool:
    if payload is None:
        return False
    if str(getattr(payload, "snapshot_date", "") or "") != snapshot_date:
        return False
    items = getattr(payload, "items", None)
    return isinstance(items, list)


def _campaign_identity(record: PromoCampaignArchiveRecord) -> str:
    if record.metadata.promo_id is not None and record.metadata.period_id is not None:
        return f"{record.metadata.promo_id}:{record.metadata.period_id}"
    if record.metadata.promo_id is not None:
        return str(record.metadata.promo_id)
    return record.archive_key


def _build_zero_items(
    *,
    snapshot_date: str,
    requested_nm_ids: Iterable[int],
) -> list[PromoLiveSourceItem]:
    return [
        PromoLiveSourceItem(
            snapshot_date=snapshot_date,
            nm_id=nm_id,
            promo_count_by_price=0.0,
            promo_entry_price_best=0.0,
            promo_participation=0.0,
        )
        for nm_id in sorted({int(nm_id) for nm_id in requested_nm_ids})
    ]


def _build_item_from_candidate_rows(
    *,
    snapshot_date: str,
    nm_id: int,
    candidate_rows: list[PromoCandidateRow],
    price_seller_discounted: float | None,
) -> tuple[PromoLiveSourceItem, PromoEligibilityEvaluation]:
    evaluation = evaluate_candidate_rows(
        candidate_rows=candidate_rows,
        price_seller_discounted=price_seller_discounted,
    )
    return (
        PromoLiveSourceItem(
            snapshot_date=snapshot_date,
            nm_id=nm_id,
            promo_count_by_price=round(evaluation.promo_count_by_price, 6),
            promo_entry_price_best=round(evaluation.promo_entry_price_best, 6),
            promo_participation=round(evaluation.promo_participation, 6),
        ),
        evaluation,
    )


def _promo_metric_coverage_detail_parts(
    *,
    eligible_nm_ids: list[int],
    truthful_zero_no_candidate_nm_ids: list[int],
    truthful_zero_ineligible_nm_ids: list[int],
    missing_price_truth_nm_ids: list[int],
) -> list[str]:
    return [
        f"promo_metric_eligible_nm_ids={_format_nm_id_sample(eligible_nm_ids)}",
        (
            "promo_metric_truthful_zero_no_candidate_nm_ids="
            f"{_format_nm_id_sample(truthful_zero_no_candidate_nm_ids)}"
        ),
        (
            "promo_metric_truthful_zero_ineligible_nm_ids="
            f"{_format_nm_id_sample(truthful_zero_ineligible_nm_ids)}"
        ),
        (
            "promo_metric_missing_price_truth_nm_ids="
            f"{_format_nm_id_sample(missing_price_truth_nm_ids)}"
        ),
    ]


def _format_nm_id_sample(nm_ids: list[int], *, limit: int = 20) -> str:
    unique = sorted({int(nm_id) for nm_id in nm_ids})
    if not unique:
        return ""
    sample = ",".join(str(nm_id) for nm_id in unique[:limit])
    if len(unique) > limit:
        sample = f"{sample},...(+{len(unique) - limit})"
    return sample


def _metadata_fingerprint(metadata: PromoMetadata) -> str:
    stable_payload = {
        "promo_id": metadata.promo_id,
        "period_id": metadata.period_id,
        "promo_title": metadata.promo_title,
        "promo_period_text": metadata.promo_period_text,
        "promo_start_at": metadata.promo_start_at,
        "promo_end_at": metadata.promo_end_at,
        "period_parse_confidence": metadata.period_parse_confidence,
        "temporal_classification": metadata.temporal_classification,
        "promo_status": metadata.promo_status,
        "promo_status_text": metadata.promo_status_text,
        "eligible_count": metadata.eligible_count,
        "participating_count": metadata.participating_count,
        "excluded_count": metadata.excluded_count,
        "source_tab": metadata.source_tab,
        "source_filter_code": metadata.source_filter_code,
        "export_kind": metadata.export_kind,
    }
    raw = json.dumps(stable_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _archive_key(metadata: PromoMetadata) -> str:
    promo_part = str(metadata.promo_id) if metadata.promo_id is not None else "pending"
    period_part = str(metadata.period_id) if metadata.period_id is not None else "pending"
    return f"{promo_part}__{period_part}__{_slugify(metadata.promo_title)}"


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^\w]+", "-", str(value or "").lower(), flags=re.UNICODE)
    normalized = normalized.strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized or "pending"


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
