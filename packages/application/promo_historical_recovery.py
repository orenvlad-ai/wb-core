"""Read-only exact-day promo reconstruction shared by routine display and publication.

No runtime/source constructors, browser calls, archive sync or database writes.
A complete historical composite is never evidence of end-of-day freshness.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any
from zoneinfo import ZoneInfo

from packages.application.promo_campaign_archive import (
    DailyPriceTruthResolution, PromoCampaignArchiveRecord, PromoCampaignArchiveSyncSummary,
    _complete_identity_discovery, materialize_promo_result_from_archive,
    promo_campaign_has_normalized_rows,
)
from packages.contracts.promo_live_source import PromoLiveSourceIncomplete
from packages.contracts.promo_xlsx_collector_block import PromoMetadata

BUSINESS_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
MAX_RUNS = 24
MAX_ATTEMPTS = 4
MAX_ARCHIVE_RECORDS = 256
MAX_QUERIES = 24
MAX_FILES = 1024
MAX_BYTES = 64 * 1024 * 1024
MAX_SECONDS = 10.0


class PromoHistoricalRecoveryError(ValueError):
    pass


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class RecoveryBudget:
    deadline: float = field(default_factory=lambda: time.monotonic() + MAX_SECONDS)
    files: int = 0
    bytes: int = 0
    queries: int = 0

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise PromoHistoricalRecoveryError("promo-reconstruction-budget-exceeded")

    def read(self, path: Path) -> bytes:
        self.check()
        size = path.stat().st_size
        if self.files >= MAX_FILES or self.bytes + size > MAX_BYTES:
            raise PromoHistoricalRecoveryError("promo-reconstruction-budget-exceeded")
        self.files += 1
        self.bytes += size
        data = path.read_bytes()
        self.check()
        return data

    def query(self, conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list:
        self.check()
        if self.queries >= MAX_QUERIES:
            raise PromoHistoricalRecoveryError("promo-reconstruction-budget-exceeded")
        self.queries += 1
        conn.set_progress_handler(lambda: int(time.monotonic() >= self.deadline), 1000)
        try:
            rows = conn.execute(sql, args).fetchmany(10001)
        finally:
            conn.set_progress_handler(None, 0)
        self.check()
        if len(rows) > 10000:
            raise PromoHistoricalRecoveryError("promo-reconstruction-query-result-too-large")
        return rows


def bounded_archive_records(runtime: Path, budget: RecoveryBudget) -> list[PromoCampaignArchiveRecord]:
    paths = []
    for path in (runtime / "promo_campaign_archive").glob("*/archive_record.json"):
        if len(paths) >= MAX_ARCHIVE_RECORDS:
            raise PromoHistoricalRecoveryError("promo-reconstruction-archive-budget-exceeded")
        paths.append(path)
    records = []
    for path in sorted(paths):
        payload = json.loads(budget.read(path))
        records.append(PromoCampaignArchiveRecord(
            archive_key=str(payload.get("archive_key") or ""),
            archive_dir=str(payload.get("archive_dir") or path.parent),
            metadata_fingerprint=str(payload.get("metadata_fingerprint") or ""),
            workbook_fingerprint=payload.get("workbook_fingerprint"),
            workbook_present=bool(payload.get("workbook_present")),
            workbook_path=payload.get("workbook_path"),
            workbook_inspection_path=payload.get("workbook_inspection_path"),
            collected_at=str(payload.get("collected_at") or ""),
            downloaded_at=payload.get("downloaded_at"), metadata=PromoMetadata(**payload.get("metadata", {})),
        ))
    return records


def exact_day_runs(runtime: Path, day: str, budget: RecoveryBudget) -> list[Path]:
    date.fromisoformat(day)
    paths = []
    for path in (runtime / "promo_xlsx_collector_runs").glob(f"{day}__*/run_summary.json"):
        budget.check()
        if len(paths) >= MAX_RUNS:
            # No arbitrary truncation can certify absence of contradictory runs.
            raise PromoHistoricalRecoveryError("promo-reconstruction-run-budget-exceeded")
        paths.append(path)
    return sorted(paths, reverse=True)


def read_reconstruction_run(runtime: Path, day: str, run_name: str, *, budget: RecoveryBudget | None = None) -> tuple[Path, dict[str, Any], datetime]:
    budget = budget or RecoveryBudget()
    if not run_name.startswith(day + "__") or "/" in run_name or ".." in run_name:
        raise PromoHistoricalRecoveryError(f"promo-reconstruction-run-invalid:{day}")
    path = runtime / "promo_xlsx_collector_runs" / run_name / "run_summary.json"
    if not path.is_file():
        raise PromoHistoricalRecoveryError(f"promo-reconstruction-run-missing:{day}")
    summary = json.loads(budget.read(path))
    if Path(str(summary.get("run_dir") or "")).resolve() != path.parent.resolve():
        raise PromoHistoricalRecoveryError(f"promo-reconstruction-run-identity-drift:{day}")
    observed = datetime.fromisoformat(str(summary.get("started_at") or ""))
    if observed.tzinfo is None or observed.astimezone(BUSINESS_TIMEZONE).date().isoformat() != day:
        raise PromoHistoricalRecoveryError(f"promo-reconstruction-run-date-invalid:{day}")
    if _complete_identity_discovery(runtime, day, capture_run_summary=path) is None:
        raise PromoHistoricalRecoveryError(f"promo-reconstruction-identity-incomplete:{day}")
    budget.check()
    return path, summary, observed


def reconstruction_artifact_proof(runtime: Path, day: str, run_path: Path, summary: dict[str, Any], observed: datetime, *, budget: RecoveryBudget | None = None) -> str:
    """Pin original run metadata and pre-observation archive workbooks."""
    budget = budget or RecoveryBudget()
    run_items = {item.get("promo_id"): item for item in summary.get("promos") or []
                 if isinstance(item, dict) and type(item.get("promo_id")) is int}
    proof: list[Any] = []
    for record in bounded_archive_records(runtime, budget):
        metadata = record.metadata
        if not (metadata.promo_start_at and metadata.promo_end_at and metadata.promo_start_at[:10] <= day <= metadata.promo_end_at[:10]):
            continue
        normalized = False
        if not record.workbook_path or not Path(record.workbook_path).is_file():
            for name in ("campaign_rows.jsonl", "campaign_rows_manifest.json"):
                path = Path(record.archive_dir) / name
                if path.is_file():
                    budget.read(path)
            normalized = promo_campaign_has_normalized_rows(record)
        if not record.workbook_present and not normalized:
            # A metadata-only announcement is handled by exact-day discovery;
            # a real artifact loss remains fatal in ordinary artifact validation.
            continue
        item = run_items.get(metadata.promo_id)
        if item is None or item.get("status") not in {"reused_archive", "downloaded"}:
            raise PromoHistoricalRecoveryError(f"promo-reconstruction-workbook-not-in-run:{day}:{metadata.promo_id}")
        workbook = Path(record.workbook_path) if record.workbook_path else Path(record.archive_dir) / "workbook.xlsx"
        raw_path = Path(str(item.get("metadata_path") or ""))
        if (not (workbook.is_file() or normalized) or Path(str(item.get("saved_path") or "")).resolve() != workbook.resolve()
                or raw_path.parent.parent.parent.resolve() != run_path.parent.resolve()
                or not raw_path.is_file()):
            raise PromoHistoricalRecoveryError(f"promo-reconstruction-artifact-path-invalid:{day}:{metadata.promo_id}")
        reuse_path = raw_path.parent / "archive_reuse.json"
        if item.get("status") == "reused_archive":
            if not reuse_path.is_file():
                raise PromoHistoricalRecoveryError(f"promo-reconstruction-reuse-proof-missing:{day}:{metadata.promo_id}")
            reuse_bytes = budget.read(reuse_path)
            reuse = json.loads(reuse_bytes)
            downloaded_at = datetime.fromisoformat(str(reuse.get("downloaded_at") or ""))
            if (reuse.get("archive_key") != record.archive_key
                    or Path(str(reuse.get("reused_workbook_path") or "")).resolve() != workbook.resolve()
                    or downloaded_at.tzinfo is None or downloaded_at.astimezone(timezone.utc) > observed.astimezone(timezone.utc)):
                raise PromoHistoricalRecoveryError(f"promo-reconstruction-reuse-proof-invalid:{day}:{metadata.promo_id}")
        else:
            reuse_bytes = b""
        raw_bytes = budget.read(raw_path)
        raw = json.loads(raw_bytes)
        if any(raw.get(field) != getattr(metadata, field) for field in ("promo_id", "promo_start_at", "promo_end_at", "promo_status")):
            raise PromoHistoricalRecoveryError(f"promo-reconstruction-artifact-identity-drift:{day}:{metadata.promo_id}")
        if workbook.is_file() and datetime.fromtimestamp(workbook.stat().st_mtime, tz=timezone.utc) > observed.astimezone(timezone.utc):
            raise PromoHistoricalRecoveryError(f"promo-reconstruction-workbook-too-new:{day}:{metadata.promo_id}")
        if normalized:
            if item.get("status") != "reused_archive" or not record.workbook_fingerprint:
                raise PromoHistoricalRecoveryError(f"promo-reconstruction-normalized-proof-missing:{day}:{metadata.promo_id}")
            normalized_bytes = [budget.read(Path(record.archive_dir) / name)
                                for name in ("campaign_rows.jsonl", "campaign_rows_manifest.json")]
            material_sha = _digest([record.workbook_fingerprint, *[hashlib.sha256(data).hexdigest() for data in normalized_bytes]])
        else:
            material_sha = hashlib.sha256(budget.read(workbook)).hexdigest()
        proof.append((metadata.promo_id, hashlib.sha256(raw_bytes).hexdigest(),
                      hashlib.sha256(reuse_bytes).hexdigest(), material_sha))
    if not proof:
        raise PromoHistoricalRecoveryError(f"promo-reconstruction-no-usable-artifacts:{day}")
    return _digest(sorted(proof))


def qualified_reconstruction(runtime: Path, day: str, spec: dict[str, str], conn: sqlite3.Connection,
                             price_ids: set[int], *, budget: RecoveryBudget | None = None) -> tuple[DailyPriceTruthResolution, dict]:
    budget = budget or RecoveryBudget()
    run_path, summary, identity_observed = read_reconstruction_run(runtime, day, spec["identity_run"], budget=budget)
    checkpoints = budget.query(conn, "SELECT started_at,completed_at,completeness_status FROM change_registry_checkpoints WHERE checkpoint_id=?", (spec["price_checkpoint_id"],))
    manifests = budget.query(conn, "SELECT completeness_status,expected_count,observed_count,evidence_digest FROM change_registry_checkpoint_source_manifests WHERE checkpoint_id=? AND source_name='prices'", (spec["price_checkpoint_id"],))
    if len(checkpoints) != 1 or len(manifests) != 1:
        raise PromoHistoricalRecoveryError(f"promo-reconstruction-price-checkpoint-incomplete:{day}")
    checkpoint, manifest = checkpoints[0], manifests[0]
    if checkpoint[2] != "complete" or manifest[0] != "complete" or manifest[1] != manifest[2] or not manifest[3]:
        raise PromoHistoricalRecoveryError(f"promo-reconstruction-price-checkpoint-incomplete:{day}")
    price_observed = datetime.fromisoformat(str(checkpoint[1]).replace("Z", "+00:00"))
    if price_observed.tzinfo is None or price_observed.astimezone(BUSINESS_TIMEZONE).date().isoformat() != day or price_observed >= identity_observed.astimezone(timezone.utc):
        raise PromoHistoricalRecoveryError(f"promo-reconstruction-observation-order-invalid:{day}")
    observed_rows = budget.query(conn, "SELECT nm_id,observation_status,value_kind,value_integer,observed_at,evidence_digest FROM change_registry_observation_values WHERE checkpoint_id=? AND target_kind='price' AND parameter_field='seller_price_minor' ORDER BY nm_id", (spec["price_checkpoint_id"],))
    prices = {}
    for nm_id, status, kind, minor, observed_at, evidence in observed_rows:
        observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        if (type(nm_id) is not int or nm_id <= 0 or nm_id in prices or status not in {"exact", "exact_zero"}
                or kind != "integer" or type(minor) is not int or minor < 0 or not evidence
                or observed.tzinfo is None or observed.astimezone(BUSINESS_TIMEZONE).date().isoformat() != day
                or observed > price_observed):
            raise PromoHistoricalRecoveryError(f"promo-reconstruction-price-observation-invalid:{day}")
        prices[nm_id] = minor / 100.0
    if set(prices) != price_ids or len(prices) != manifest[1] or not prices:
        raise PromoHistoricalRecoveryError(f"promo-reconstruction-price-sku-scope-mismatch:{day}")
    artifact_sha = reconstruction_artifact_proof(runtime, day, run_path, summary, identity_observed, budget=budget)
    paths = exact_day_runs(runtime, day, budget)
    later = [path for path in paths if path.parent.name > run_path.parent.name]
    latest = None
    if later:
        data = budget.read(later[0]); item = json.loads(data)
        latest = {"run_summary": str(later[0]), "status": item.get("status"), "started_at": item.get("started_at"),
                  "blocked_before_card_count": item.get("blocked_before_card_count"),
                  "unresolved_identity_count": sum(1 for entry in item.get("promos") or [] if isinstance(entry, dict) and entry.get("promo_id") is None),
                  "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}
    rows_sha = _digest([tuple(row) for row in observed_rows])
    proof = {"identity_run": str(run_path), "identity_observed_at": summary["started_at"],
             "identity_run_sha256": "sha256:" + hashlib.sha256(budget.read(run_path)).hexdigest(),
             "price_checkpoint_id": spec["price_checkpoint_id"], "price_observed_at": checkpoint[1],
             "price_rows_sha256": rows_sha, "price_values_sha256": _digest([[row[0], row[1], row[3]] for row in observed_rows]),
             "price_manifest_evidence_digest": manifest[3], "artifact_sha256": artifact_sha,
             "later_run_count_not_used": len(later), "latest_later_attempt": latest,
             "freshness": "historical_composite_observation_only"}
    truth = DailyPriceTruthResolution(price_by_nm_id=prices,
        source_note=f"daily_price_source=change_registry_checkpoint; checkpoint_id={spec['price_checkpoint_id']}; price_observed_at={checkpoint[1]}; identities_observed_at={summary['started_at']}; historical_composite_reconstruction=true",
        captured_at=str(checkpoint[1]), fingerprint=rows_sha)
    budget.check()
    return truth, proof


def find_reconstruction(runtime: Path, day: str, conn: sqlite3.Connection, price_ids: set[int],
                        *, budget: RecoveryBudget | None = None) -> tuple[dict, DailyPriceTruthResolution, dict] | None:
    budget = budget or RecoveryBudget()
    start = datetime.combine(date.fromisoformat(day), datetime_time(), BUSINESS_TIMEZONE).astimezone(timezone.utc).isoformat()
    attempts = 0
    for path in exact_day_runs(runtime, day, budget):
        try:
            _, _, observed = read_reconstruction_run(runtime, day, path.parent.name, budget=budget)
        except (PromoHistoricalRecoveryError, OSError, ValueError, TypeError):
            budget.check()
            continue
        rows = budget.query(conn, "SELECT c.checkpoint_id FROM change_registry_checkpoints c JOIN change_registry_checkpoint_source_manifests m ON m.checkpoint_id=c.checkpoint_id AND m.source_name='prices' WHERE c.completeness_status='complete' AND m.completeness_status='complete' AND julianday(c.completed_at)>=julianday(?) AND julianday(c.completed_at)<julianday(?) ORDER BY julianday(c.completed_at) DESC,c.checkpoint_id DESC LIMIT 4", (start, observed.astimezone(timezone.utc).isoformat()))
        for row in rows:
            if attempts >= MAX_ATTEMPTS:
                return None
            attempts += 1
            spec = {"identity_run": path.parent.name, "price_checkpoint_id": row[0]}
            try:
                truth, proof = qualified_reconstruction(runtime, day, spec, conn, price_ids, budget=budget)
                return spec, truth, proof
            except (PromoHistoricalRecoveryError, OSError, ValueError, TypeError):
                budget.check()
    return None


def recover_promo_display(*, runtime_dir: Path, db_path: Path, snapshot_date: str,
                          requested_nm_ids: list[int]) -> PromoLiveSourceIncomplete | None:
    """One exact-date, read-only attempt. Numeric display stays unaccepted/incomplete."""
    budget = RecoveryBudget()
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            # Match the complete source roster, then serve only the caller subset.
            rows = budget.query(conn, "SELECT payload_json FROM temporal_source_slot_snapshots WHERE source_key='prices_snapshot' AND snapshot_date=? AND snapshot_role='accepted_current_snapshot'", (snapshot_date,))
            if len(rows) != 1:
                return None
            payload = json.loads(rows[0][0])
            items = payload.get("items")
            if payload.get("kind") != "success" or payload.get("snapshot_date") != snapshot_date or not isinstance(items, list):
                return None
            ids = [item.get("nm_id") for item in items if isinstance(item, dict)]
            if len(ids) != len(items) or any(type(nm_id) is not int or nm_id <= 0 for nm_id in ids) or len(set(ids)) != len(ids):
                return None
            if not set(requested_nm_ids).issubset(ids):
                return None
            selected = find_reconstruction(runtime_dir, snapshot_date, conn, set(ids), budget=budget)
            if selected is None:
                return None
            spec, truth, proof = selected
            # No archive synchronization and no accepted slot writes.
            result = materialize_promo_result_from_archive(runtime_dir=runtime_dir, snapshot_date=snapshot_date,
                requested_nm_ids=requested_nm_ids, sync_summary=PromoCampaignArchiveSyncSummary(), diagnostics={},
                price_truth=truth, identity_run_summary=runtime_dir / "promo_xlsx_collector_runs" / spec["identity_run"] / "run_summary.json")
            budget.check()
            # A collector may replace artifacts concurrently. Never display
            # operands that changed while the ordinary materializer read them.
            run_path, summary, observed = read_reconstruction_run(runtime_dir, snapshot_date, spec["identity_run"], budget=budget)
            if ("sha256:" + hashlib.sha256(budget.read(run_path)).hexdigest() != proof["identity_run_sha256"]
                    or reconstruction_artifact_proof(runtime_dir, snapshot_date, run_path, summary, observed, budget=budget)
                    != proof["artifact_sha256"]):
                return None
            if result.kind != "success" or result.covered_count != len(set(requested_nm_ids)) or len(result.items) != len(set(requested_nm_ids)):
                return None
            values = asdict(result)
            values.update(kind="incomplete", items=result.items, missing_nm_ids=[],
                observation_quality="historical_composite_observation_only",
                detail=result.detail + "; resolution_rule=promo_historical_composite_display_only; freshness=historical_composite_observation_only; snapshot_acceptance=disabled; closed_day_freshness_unproven=true")
            values["diagnostics"] = {**result.diagnostics, "historical_reconstruction": proof,
                "display_only": True, "closed_day_freshness_unproven": True,
                "recovery_limits": {"attempts": MAX_ATTEMPTS, "exact_day_runs": MAX_RUNS, "files": MAX_FILES,
                                    "bytes": MAX_BYTES, "queries": MAX_QUERIES, "seconds": MAX_SECONDS}}
            return PromoLiveSourceIncomplete(**values)
    except (PromoHistoricalRecoveryError, sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        return None


def composite_cell_presentation(*, rows: list, day: str, proof: dict) -> dict:
    """Warning accompanies every numeric promo cell, including materialized totals."""
    observed = str(proof.get("identity_observed_at") or "")
    price_observed = str(proof.get("price_observed_at") or "")
    reason = (f"Составное наблюдение: цены {price_observed}, акции {observed}. "
              "Полнота на конец дня не подтверждена.")
    result = {}
    for row in rows:
        if len(row) < 2:
            continue
        key = str(row[1])
        metric = key.rsplit("|", 1)[-1]
        if metric not in {"promo_participation", "promo_count_by_price", "promo_entry_price_best",
                          "total_promo_participation", "total_promo_count_by_price", "avg_promo_entry_price_best"}:
            continue
        result[key] = {day: {"state": "unconfirmed", "tone": "warning", "quality_state": "preliminary",
                            "reason": reason, "quality_reason": reason,
                            "source": "WB · составное наблюдение", "source_as_of_date": day,
                            "source_observed_at": observed,
                            "observation_quality": "historical_composite_observation_only"}}
    return result
