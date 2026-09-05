"""Prepared FBS lifecycle quality for the read-only Web Vitrina path.

The FBS collector can be deliberately owner-paused during an incident.  While
it is paused, an interactive request must never rescan the operational status
backlog.  A separate query-only builder publishes one compact quality snapshot;
the page reads that snapshot as last-good evidence.  Missing or invalid cache
evidence fails closed to unavailable FBS cells, never to zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterable, Mapping

from packages.application.ff_pool_fbs_applicability import current_business_date
from packages.application.ff_pool_fbs_lifecycle import (
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    IDENTITY_PENDING_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    _lifecycle_quality_cursor,
    fbs_lifecycle_quality_coverage,
)
from packages.application.storage_registry import StoreRegistry


CACHE_SCHEMA = "web_vitrina_fbs_lifecycle_last_good_v1"
CACHE_FILENAME = ".web-vitrina-fbs-lifecycle-last-good.json"
OWNER_POLICY_FILENAME = ".auto-updates-policy.json"
OWNER_POLICY_SCHEMA = "auto_updates_owner_policy_v2"
QUALITY_CONTRACT = "fbs_lifecycle_quality_coverage_v1"
LAST_GOOD_SOURCE_MODE = "last_good_owner_paused"
UNAVAILABLE_SOURCE_MODE = "unavailable_owner_paused"


class FbsLifecycleQualityCacheAdmissionError(RuntimeError):
    """A candidate was not exact enough to replace the last-good cache."""


@dataclass(frozen=True)
class FbsLifecycleQualityFallback:
    source_mode: str
    last_good_at: str
    source_as_of_date: str
    coverage: Mapping[str, Any]
    reason_ru: str

    @property
    def has_last_good(self) -> bool:
        return self.source_mode == LAST_GOOD_SOURCE_MODE

    def resolve(
        self,
        as_of_date: str,
        requested_nm_ids: Iterable[int] | None = None,
    ) -> dict[str, Any]:
        """Project one prepared all-SKU snapshot to an exact page request."""

        target_date = str(as_of_date or "")[:10]
        requested = {
            int(value) for value in (requested_nm_ids or []) if int(value) > 0
        }
        groups: list[dict[str, Any]] = []
        for raw in self.coverage.get("groups") or []:
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            earliest = str(item.get("earliest_business_date") or "")[:10]
            if target_date and earliest and earliest > target_date:
                continue
            nm_id = item.get("nm_id")
            if requested and nm_id is not None and int(nm_id) not in requested:
                continue
            groups.append(item)
        material = {
            "contract": QUALITY_CONTRACT,
            "as_of_date": target_date,
            "status": "partial" if groups else str(self.coverage.get("status") or "exact"),
            "groups": groups,
        }
        return {
            **material,
            "digest": _fingerprint(material),
            "source_mode": self.source_mode,
            "last_good_at": self.last_good_at,
            "source_as_of_date": self.source_as_of_date,
            "reason_ru": self.reason_ru,
        }


def load_owner_paused_fallback(
    runtime_dir: Path,
) -> FbsLifecycleQualityFallback | None:
    """Return a bounded fallback only when FBS is explicitly paused or ambiguous."""

    root = Path(runtime_dir).resolve()
    policy_path = root / OWNER_POLICY_FILENAME
    if not policy_path.is_file():
        return None
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _unavailable_fallback("Политика обновления FBS повреждена или недоступна.")
    if not isinstance(policy, Mapping) or policy.get("schema_version") != OWNER_POLICY_SCHEMA:
        return _unavailable_fallback("Политика обновления FBS имеет неизвестную версию.")
    processes = policy.get("processes")
    if not isinstance(processes, Mapping):
        return _unavailable_fallback("Политика обновления FBS не содержит состояния процессов.")
    process = processes.get("fbs_shadow")
    if process is None:
        return _unavailable_fallback(
            "Политика обновления FBS не содержит однозначного состояния процесса."
        )
    if not isinstance(process, Mapping) or not isinstance(process.get("desired"), bool):
        return _unavailable_fallback("Состояние фонового обновления FBS неоднозначно.")
    if process.get("desired") is not False:
        return None
    revision = policy.get("revision")
    if not isinstance(revision, int) or revision < 1:
        return _unavailable_fallback("Ревизия политики обновления FBS неоднозначна.")
    cache = _load_cache(
        root / CACHE_FILENAME,
        expected_policy_revision=revision,
        expected_policy_digest=_fingerprint(policy),
    )
    if cache is None:
        return _unavailable_fallback(
            "Фоновое обновление FBS приостановлено; подтверждённые last-good данные ещё не подготовлены."
        )
    if not _cache_matches_current_source(root, cache):
        return _unavailable_fallback(
            "Фоновое обновление FBS приостановлено; источник изменился после подготовки last-good данных."
        )
    generated_at = str(cache.get("generated_at") or "")
    return FbsLifecycleQualityFallback(
        source_mode=LAST_GOOD_SOURCE_MODE,
        last_good_at=generated_at,
        source_as_of_date=str(cache.get("source_as_of_date") or ""),
        coverage=dict(cache["coverage"]),
        reason_ru=(
            "Фоновое обновление FBS приостановлено; показаны последние подтверждённые "
            f"данные за {cache.get('source_as_of_date')}, проверенные {generated_at}."
        ),
    )


def build_and_publish_cache(
    runtime_dir: Path,
    *,
    db_path: Path | None = None,
    generated_at: str | None = None,
    source_as_of_date: str | None = None,
) -> dict[str, Any]:
    """Build one query-only snapshot, then atomically publish its compact JSON."""

    root = Path(runtime_dir).resolve()
    source_path = (
        Path(db_path).resolve()
        if db_path is not None
        else StoreRegistry(root).resolve("operational")
    )
    if not source_path.is_file():
        raise RuntimeError("operational store is missing")
    policy_before = _explicit_owner_pause(root)
    source_date = current_business_date(source_as_of_date)
    uri = f"file:{source_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        source_state = _source_state(conn, tables=tables)
        _admit_source_state(source_state)
        coverage = fbs_lifecycle_quality_coverage(
            conn,
            as_of_date=source_date,
            requested_nm_ids=None,
        )
        conn.rollback()
    _admit_exact_candidate(
        coverage=coverage,
        source_state=source_state,
        source_as_of_date=source_date,
    )
    policy_after = _explicit_owner_pause(root)
    if policy_after != policy_before:
        raise FbsLifecycleQualityCacheAdmissionError(
            "FBS owner policy changed while the cache candidate was built"
        )
    timestamp = generated_at or _utc_now()
    if not _valid_utc_timestamp(timestamp):
        raise FbsLifecycleQualityCacheAdmissionError(
            "FBS lifecycle quality candidate timestamp is invalid"
        )
    payload: dict[str, Any] = {
        "schema": CACHE_SCHEMA,
        "generated_at": timestamp,
        "source_as_of_date": source_date,
        "source_db_path": str(source_path),
        "source_state": source_state,
        "owner_policy": policy_after,
        "admission": {
            "status": "exact",
            "coverage_digest_verified": True,
            "unmaterialized_status_count": 0,
            "unresolved_pending_count": 0,
            "source_state_digest": _fingerprint(source_state),
        },
        "coverage": coverage,
    }
    payload["cache_digest"] = _fingerprint(payload)
    root.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(root / CACHE_FILENAME, payload)
    readback = _load_cache(
        root / CACHE_FILENAME,
        expected_policy_revision=int(policy_after["revision"]),
        expected_policy_digest=str(policy_after["policy_digest"]),
    )
    if readback is None or readback.get("cache_digest") != payload["cache_digest"]:
        raise RuntimeError("FBS lifecycle last-good cache readback failed")
    return {
        "status": "published",
        "path": str(root / CACHE_FILENAME),
        "generated_at": timestamp,
        "source_as_of_date": source_date,
        "coverage_status": str(coverage.get("status") or ""),
        "group_count": len(list(coverage.get("groups") or [])),
        "source_state": source_state,
        "cache_digest": str(payload["cache_digest"]),
    }


def _explicit_owner_pause(runtime_dir: Path) -> dict[str, Any]:
    policy_path = runtime_dir / OWNER_POLICY_FILENAME
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise FbsLifecycleQualityCacheAdmissionError(
            "exact FBS owner policy is unavailable"
        ) from exc
    process = (
        ((policy.get("processes") or {}).get("fbs_shadow") or {})
        if isinstance(policy, Mapping)
        else {}
    )
    revision = policy.get("revision") if isinstance(policy, Mapping) else None
    if (
        not isinstance(policy, Mapping)
        or policy.get("schema_version") != OWNER_POLICY_SCHEMA
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(process, Mapping)
        or process.get("desired") is not False
    ):
        raise FbsLifecycleQualityCacheAdmissionError(
            "FBS must have an exact explicit desired=false owner policy"
        )
    return {
        "schema_version": OWNER_POLICY_SCHEMA,
        "revision": revision,
        "fbs_shadow_desired": False,
        "policy_digest": _fingerprint(policy),
    }


def _admit_exact_candidate(
    *,
    coverage: Mapping[str, Any],
    source_state: Mapping[str, Any],
    source_as_of_date: str,
) -> None:
    material = {
        "contract": coverage.get("contract"),
        "as_of_date": coverage.get("as_of_date"),
        "status": coverage.get("status"),
        "groups": coverage.get("groups"),
    }
    if (
        material["contract"] != QUALITY_CONTRACT
        or material["as_of_date"] != source_as_of_date
        or coverage.get("digest") != _fingerprint(material)
    ):
        raise FbsLifecycleQualityCacheAdmissionError(
            "FBS lifecycle quality candidate digest is invalid"
        )
    if material["status"] != "exact" or material["groups"] != []:
        raise FbsLifecycleQualityCacheAdmissionError(
            "FBS lifecycle quality candidate is partial or incomplete"
        )
    _admit_source_state(source_state)


def _admit_source_state(source_state: Mapping[str, Any]) -> None:
    numeric_fields = (
        "lifecycle_cursor",
        "max_status_observation_sequence",
        "unmaterialized_status_count",
        "unresolved_pending_count",
    )
    if (
        any(
            type(source_state.get(field)) is not int
            or int(source_state[field]) < 0
            for field in numeric_fields
        )
        or not isinstance(source_state.get("cutover_id"), str)
        or int(source_state["unmaterialized_status_count"]) != 0
        or int(source_state["unresolved_pending_count"]) != 0
        or int(source_state["lifecycle_cursor"])
        < int(source_state["max_status_observation_sequence"])
    ):
        raise FbsLifecycleQualityCacheAdmissionError(
            "FBS lifecycle source is not fully materialized"
        )


def _source_state(
    conn: sqlite3.Connection,
    *,
    tables: set[str],
) -> dict[str, Any]:
    max_status_sequence = (
        int(
            conn.execute(
                f"SELECT COALESCE(MAX(observation_sequence),0) FROM {STATUS_OBSERVATIONS_TABLE}"
            ).fetchone()[0]
        )
        if STATUS_OBSERVATIONS_TABLE in tables
        else 0
    )
    unresolved_pending_count = 0
    if {IDENTITY_PENDING_TABLE, IDENTITY_PENDING_RESOLUTIONS_TABLE}.issubset(tables):
        unresolved_pending_count = int(
            conn.execute(
                f"""SELECT COUNT(*) FROM {IDENTITY_PENDING_TABLE} AS pending
                    LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} AS resolution
                      ON resolution.pending_id=pending.pending_id
                    WHERE resolution.pending_id IS NULL"""
            ).fetchone()[0]
        )
    cutover_id = ""
    manifest_table = "sheet_vitrina_v1_ff_pool_cutover_manifests"
    if manifest_table in tables:
        row = conn.execute(
            f"SELECT cutover_id FROM {manifest_table} ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1"
        ).fetchone()
        cutover_id = str(row[0]) if row is not None else ""
    cursor = (
        _lifecycle_quality_cursor(conn, cutover_id=cutover_id, tables=tables)
        if cutover_id
        else 0
    )
    unmaterialized_status_count = (
        int(
            conn.execute(
                f"SELECT COUNT(*) FROM {STATUS_OBSERVATIONS_TABLE} WHERE observation_sequence>?",
                (cursor,),
            ).fetchone()[0]
        )
        if STATUS_OBSERVATIONS_TABLE in tables
        else 0
    )
    return {
        "cutover_id": cutover_id,
        "lifecycle_cursor": cursor,
        "max_status_observation_sequence": max_status_sequence,
        "unmaterialized_status_count": unmaterialized_status_count,
        "unresolved_pending_count": unresolved_pending_count,
    }


def _load_cache(
    path: Path,
    *,
    expected_policy_revision: int | None = None,
    expected_policy_digest: str | None = None,
) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != CACHE_SCHEMA:
        return None
    supplied_digest = str(payload.get("cache_digest") or "")
    material = dict(payload)
    material.pop("cache_digest", None)
    if supplied_digest != _fingerprint(material):
        return None
    coverage = payload.get("coverage")
    source_state = payload.get("source_state")
    admission = payload.get("admission")
    owner_policy = payload.get("owner_policy")
    source_as_of_date = str(payload.get("source_as_of_date") or "")
    if (
        not isinstance(coverage, Mapping)
        or coverage.get("contract") != QUALITY_CONTRACT
        or not isinstance(coverage.get("groups"), list)
        or not isinstance(source_state, Mapping)
        or not isinstance(admission, Mapping)
        or not isinstance(owner_policy, Mapping)
        or not _valid_iso_date(source_as_of_date)
        or not _valid_utc_timestamp(str(payload.get("generated_at") or ""))
    ):
        return None
    coverage_material = {
        "contract": coverage.get("contract"),
        "as_of_date": coverage.get("as_of_date"),
        "status": coverage.get("status"),
        "groups": coverage.get("groups"),
    }
    if (
        coverage.get("digest") != _fingerprint(coverage_material)
        or coverage.get("as_of_date") != source_as_of_date
        or coverage.get("status") != "exact"
        or coverage.get("groups") != []
        or admission.get("status") != "exact"
        or admission.get("coverage_digest_verified") is not True
        or admission.get("unmaterialized_status_count") != 0
        or admission.get("unresolved_pending_count") != 0
        or admission.get("source_state_digest") != _fingerprint(source_state)
        or owner_policy.get("schema_version") != OWNER_POLICY_SCHEMA
        or owner_policy.get("fbs_shadow_desired") is not False
        or not isinstance(owner_policy.get("revision"), int)
        or int(owner_policy.get("revision") or 0) < 1
        or not str(owner_policy.get("policy_digest") or "").startswith("sha256:")
        or (
            expected_policy_digest is not None
            and str(owner_policy.get("policy_digest") or "")
            != expected_policy_digest
        )
        or (
            expected_policy_revision is not None
            and int(owner_policy.get("revision") or 0) != expected_policy_revision
        )
    ):
        return None
    try:
        _admit_source_state(source_state)
    except (FbsLifecycleQualityCacheAdmissionError, TypeError, ValueError):
        return None
    return payload


def _cache_matches_current_source(
    runtime_dir: Path,
    cache: Mapping[str, Any],
) -> bool:
    """Reject a last-good label when its exact source snapshot has moved."""

    try:
        current_db_path = StoreRegistry(runtime_dir).resolve("operational").resolve()
        if str(cache.get("source_db_path") or "") != str(current_db_path):
            return False
        uri = f"file:{current_db_path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.2) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=200")
            conn.execute("BEGIN")
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            current_source_state = _source_state(conn, tables=tables)
            conn.rollback()
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        return False
    return _fingerprint(current_source_state) == _fingerprint(cache.get("source_state"))


def _valid_iso_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def _valid_utc_timestamp(value: str) -> bool:
    if not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _unavailable_fallback(reason_ru: str) -> FbsLifecycleQualityFallback:
    coverage = {
        "contract": QUALITY_CONTRACT,
        "as_of_date": "",
        "status": "partial",
        "groups": [
            {
                "facility_id": "",
                "nm_id": None,
                "earliest_business_date": "",
                "reason_codes": ["lifecycle_quality_last_good_unavailable"],
                "status_sequence_count": 0,
                "status_sequence_digest": _fingerprint([]),
            }
        ],
    }
    coverage["digest"] = _fingerprint(coverage)
    return FbsLifecycleQualityFallback(
        source_mode=UNAVAILABLE_SOURCE_MODE,
        last_good_at="",
        source_as_of_date="",
        coverage=coverage,
        reason_ru=reason_ru,
    )


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
