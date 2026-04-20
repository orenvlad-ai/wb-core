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

HEADER_NM_ID = "Артикул WB"
HEADER_PLAN_PRICE = "Плановая цена для акции"
HEADER_CURRENT_PRICE = "Текущая розничная цена"


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

    if not covering:
        return PromoLiveSourceIncomplete(
            kind="incomplete",
            covered_count=0,
            items=[],
            missing_nm_ids=requested,
            detail="; ".join(detail_parts + [f"no_covering_campaign_artifacts_for_date={snapshot_date}"]),
            **common,
        )

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

    accumulator = {
        nm_id: {"promo_count_by_price": 0.0, "promo_entry_price_best": 0.0}
        for nm_id in requested
    }
    for record in usable:
        _merge_archive_workbook_rows(
            accumulator=accumulator,
            workbook_path=Path(str(record.workbook_path)),
            requested_nm_ids=requested,
        )

    items = [
        PromoLiveSourceItem(
            snapshot_date=snapshot_date,
            nm_id=nm_id,
            promo_count_by_price=round(values["promo_count_by_price"], 6),
            promo_entry_price_best=round(values["promo_entry_price_best"], 6),
            promo_participation=1.0 if values["promo_count_by_price"] > 0 else 0.0,
        )
        for nm_id, values in sorted(accumulator.items())
    ]
    archive_keys = ",".join(record.archive_key for record in usable[:8])
    return PromoLiveSourceSuccess(
        kind="success",
        covered_count=len(requested),
        items=items,
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
    accumulator: dict[int, dict[str, float]],
    workbook_path: Path,
    requested_nm_ids: Iterable[int],
) -> None:
    requested = set(requested_nm_ids)
    workbook = load_workbook(filename=str(workbook_path), read_only=False, data_only=True)
    try:
        sheet, header_row_index = _find_workbook_data_sheet(workbook)
        header = list(next(sheet.iter_rows(min_row=header_row_index, max_row=header_row_index, values_only=True)))
        header_index = {str(name).strip(): idx for idx, name in enumerate(header) if name not in (None, "")}
        for required in (HEADER_NM_ID, HEADER_PLAN_PRICE, HEADER_CURRENT_PRICE):
            if required not in header_index:
                raise ValueError(f"promo archive workbook missing required header: {required}")
        for row in sheet.iter_rows(min_row=header_row_index + 1, values_only=True):
            nm_id = _parse_int(row[header_index[HEADER_NM_ID]])
            if nm_id is None or nm_id not in requested:
                continue
            plan_price = _parse_float(row[header_index[HEADER_PLAN_PRICE]])
            current_price = _parse_float(row[header_index[HEADER_CURRENT_PRICE]])
            if plan_price is None:
                continue
            accumulator[nm_id]["promo_entry_price_best"] = max(
                accumulator[nm_id]["promo_entry_price_best"],
                plan_price,
            )
            if current_price is None:
                continue
            if current_price < plan_price:
                accumulator[nm_id]["promo_count_by_price"] += 1.0
    finally:
        workbook.close()


def _find_workbook_data_sheet(workbook) -> tuple[object, int]:
    for sheet_name in workbook.sheetnames:
        sheet = workbook[sheet_name]
        row_index = _find_workbook_header_row_index(sheet)
        if row_index is not None:
            return sheet, row_index
    first_sheet = workbook[workbook.sheetnames[0]]
    row_index = _find_workbook_header_row_index(first_sheet)
    return first_sheet, row_index or 1


def _find_workbook_header_row_index(sheet) -> int | None:
    for row_index, row in enumerate(sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 10), values_only=True), start=1):
        header = [value for value in row if value not in (None, "")]
        normalized = {str(value).strip() for value in header}
        if {HEADER_NM_ID, HEADER_PLAN_PRICE, HEADER_CURRENT_PRICE}.issubset(normalized):
            return row_index
    return None


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


def _parse_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\xa0", "").replace("%", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(value: object) -> int | None:
    numeric = _parse_float(value)
    if numeric is None:
        return None
    return int(numeric)


def _normalize_optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
