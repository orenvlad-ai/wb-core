"""Logical SQLite store registry and generation-manifest boundary.

The registry deliberately has an inert default: when no persisted manifest is
present both logical stores resolve to the existing monolith.  Merely deploying
this module therefore creates no files and changes no runtime source of truth.
Any split generation must be selected by an explicit, fsynced manifest switch.
"""

from __future__ import annotations

from collections import Counter, deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterator, Literal, Mapping

from packages.application.sqlite_contention import connect_sqlite


MANIFEST_CONTRACT = "wb_core_storage_generation_manifest_v1"
MANIFEST_FILENAME = "storage_generation_manifest.json"
MONOLITH_FILENAME = "registry_upload_runtime.sqlite3"
RAW_SCHEMA_REVISION = "finance_raw_v1"
OPERATIONAL_SCHEMA_REVISION = "operational_v1"
_ALLOWED_STATES = {"monolith", "shadow", "cutover", "rollback"}
StoreName = Literal["finance_raw", "operational"]
StoreMode = Literal["ro", "rw"]


class StorageRegistryError(ValueError):
    """Fail-closed storage registry or manifest validation error."""


@dataclass(frozen=True)
class StoreGeneration:
    logical_store: StoreName
    generation_id: str
    generation_epoch: str
    relative_path: str
    schema_revision: str
    watermark: str


@dataclass(frozen=True)
class GenerationManifest:
    contract_version: str
    state: str
    canonical_source: str
    generation_epoch: str
    raw: StoreGeneration
    operational: StoreGeneration
    rollback_generation_id: str
    created_at: str
    source_fingerprint: str
    manifest_sha256: str
    implicit: bool = False


@dataclass(frozen=True)
class SQLiteOpenObservation:
    opened_at: str
    logical_store: StoreName
    mode: StoreMode
    operation: str
    generation_id: str
    schema_revision: str
    path_identity: str


_OBSERVATIONS: deque[SQLiteOpenObservation] = deque(maxlen=2048)
_OBSERVATION_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical_json(value)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _store_payload(store: StoreGeneration) -> dict[str, str]:
    payload = asdict(store)
    payload.pop("logical_store", None)
    return payload


def manifest_payload(manifest: GenerationManifest, *, include_digest: bool = True) -> dict[str, Any]:
    payload = {
        "contract_version": manifest.contract_version,
        "state": manifest.state,
        "canonical_source": manifest.canonical_source,
        "generation_epoch": manifest.generation_epoch,
        "raw": _store_payload(manifest.raw),
        "operational": _store_payload(manifest.operational),
        "rollback_generation_id": manifest.rollback_generation_id,
        "created_at": manifest.created_at,
        "source_fingerprint": manifest.source_fingerprint,
    }
    if include_digest:
        payload["manifest_sha256"] = manifest.manifest_sha256
    return payload


def _implicit_manifest() -> GenerationManifest:
    epoch = "monolith"
    raw = StoreGeneration(
        logical_store="finance_raw",
        generation_id="monolith",
        generation_epoch=epoch,
        relative_path=MONOLITH_FILENAME,
        schema_revision="finance_raw_legacy_monolith_v1",
        watermark="",
    )
    operational = StoreGeneration(
        logical_store="operational",
        generation_id="monolith",
        generation_epoch=epoch,
        relative_path=MONOLITH_FILENAME,
        schema_revision="operational_legacy_monolith_v1",
        watermark="",
    )
    provisional = GenerationManifest(
        contract_version=MANIFEST_CONTRACT,
        state="monolith",
        canonical_source="monolith",
        generation_epoch=epoch,
        raw=raw,
        operational=operational,
        rollback_generation_id="monolith",
        created_at="",
        source_fingerprint="",
        manifest_sha256="",
        implicit=True,
    )
    digest = _sha256(manifest_payload(provisional, include_digest=False))
    return replace(provisional, manifest_sha256=digest)


def _parse_store(logical_store: StoreName, raw: Any) -> StoreGeneration:
    if not isinstance(raw, Mapping):
        raise StorageRegistryError(f"{logical_store} generation must be an object")
    return StoreGeneration(
        logical_store=logical_store,
        generation_id=str(raw.get("generation_id") or "").strip(),
        generation_epoch=str(raw.get("generation_epoch") or "").strip(),
        relative_path=str(raw.get("relative_path") or "").strip(),
        schema_revision=str(raw.get("schema_revision") or "").strip(),
        watermark=str(raw.get("watermark") or "").strip(),
    )


def parse_manifest(payload: Any) -> GenerationManifest:
    if not isinstance(payload, Mapping):
        raise StorageRegistryError("generation manifest must be a JSON object")
    raw = _parse_store("finance_raw", payload.get("raw"))
    operational = _parse_store("operational", payload.get("operational"))
    manifest = GenerationManifest(
        contract_version=str(payload.get("contract_version") or "").strip(),
        state=str(payload.get("state") or "").strip(),
        canonical_source=str(payload.get("canonical_source") or "").strip(),
        generation_epoch=str(payload.get("generation_epoch") or "").strip(),
        raw=raw,
        operational=operational,
        rollback_generation_id=str(payload.get("rollback_generation_id") or "").strip(),
        created_at=str(payload.get("created_at") or "").strip(),
        source_fingerprint=str(payload.get("source_fingerprint") or "").strip(),
        manifest_sha256=str(payload.get("manifest_sha256") or "").strip(),
        implicit=False,
    )
    validate_manifest(manifest)
    expected = _sha256(manifest_payload(manifest, include_digest=False))
    if manifest.manifest_sha256 != expected:
        raise StorageRegistryError(
            f"generation manifest digest mismatch: expected {expected}, "
            f"got {manifest.manifest_sha256 or '<missing>'}"
        )
    return manifest


def validate_manifest(manifest: GenerationManifest) -> None:
    if manifest.contract_version != MANIFEST_CONTRACT:
        raise StorageRegistryError(
            f"unsupported generation manifest contract: {manifest.contract_version or '<missing>'}"
        )
    if manifest.state not in _ALLOWED_STATES:
        raise StorageRegistryError(f"unsupported generation state: {manifest.state or '<missing>'}")
    if manifest.canonical_source not in {"monolith", "split"}:
        raise StorageRegistryError(
            f"unsupported canonical source: {manifest.canonical_source or '<missing>'}"
        )
    if not manifest.generation_epoch:
        raise StorageRegistryError("generation_epoch is required")
    for store in (manifest.raw, manifest.operational):
        if not store.generation_id or not store.generation_epoch:
            raise StorageRegistryError(f"{store.logical_store} generation identity is required")
        if store.generation_epoch != manifest.generation_epoch:
            raise StorageRegistryError(
                f"mixed generation epoch: manifest={manifest.generation_epoch}, "
                f"{store.logical_store}={store.generation_epoch}"
            )
        if not store.schema_revision:
            raise StorageRegistryError(f"{store.logical_store} schema_revision is required")
        relative = Path(store.relative_path)
        if (
            not store.relative_path
            or relative.is_absolute()
            or ".." in relative.parts
            or relative == Path(".")
        ):
            raise StorageRegistryError(
                f"{store.logical_store} path must be a bounded relative path"
            )
    same_path = manifest.raw.relative_path == manifest.operational.relative_path
    if manifest.state == "monolith":
        if manifest.canonical_source != "monolith":
            raise StorageRegistryError("monolith state must keep canonical_source=monolith")
        if not same_path or manifest.raw.generation_id != manifest.operational.generation_id:
            raise StorageRegistryError("monolith state must pin one exact file generation")
    else:
        if same_path:
            raise StorageRegistryError("split generation cannot resolve both stores to one file")
        if manifest.raw.generation_id == manifest.operational.generation_id:
            raise StorageRegistryError("split stores require distinct generation ids")
    if manifest.state in {"shadow", "rollback"} and manifest.canonical_source != "monolith":
        raise StorageRegistryError(f"{manifest.state} state must keep the monolith canonical")
    if manifest.state == "cutover" and manifest.canonical_source != "split":
        raise StorageRegistryError("cutover state must select canonical_source=split")
    if not manifest.rollback_generation_id:
        raise StorageRegistryError("rollback_generation_id is required")


class StoreRegistry:
    """Resolve and open exact logical store generations."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        manifest_path: Path | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.manifest_path = (
            Path(manifest_path).expanduser().resolve()
            if manifest_path is not None
            else self.runtime_dir / MANIFEST_FILENAME
        )
        if self.manifest_path.parent != self.runtime_dir:
            raise StorageRegistryError("generation manifest must live in the runtime directory")

    def load(self, *, require_files: bool = False) -> GenerationManifest:
        if not self.manifest_path.exists():
            manifest = _implicit_manifest()
        else:
            try:
                payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StorageRegistryError(f"cannot read generation manifest: {type(exc).__name__}") from exc
            manifest = parse_manifest(payload)
        if require_files:
            for name in ("finance_raw", "operational"):
                path = self.resolve(name, manifest=manifest)
                if not path.is_file():
                    raise StorageRegistryError(f"{name} generation file is missing")
        return manifest

    def generation(
        self,
        logical_store: StoreName,
        *,
        manifest: GenerationManifest | None = None,
    ) -> StoreGeneration:
        selected = manifest or self.load()
        return selected.raw if logical_store == "finance_raw" else selected.operational

    def resolve(
        self,
        logical_store: StoreName,
        *,
        manifest: GenerationManifest | None = None,
    ) -> Path:
        selected = self.generation(logical_store, manifest=manifest)
        path = (self.runtime_dir / selected.relative_path).resolve()
        try:
            path.relative_to(self.runtime_dir)
        except ValueError as exc:
            raise StorageRegistryError(f"{logical_store} path escapes the runtime directory") from exc
        return path

    def connect(
        self,
        logical_store: StoreName,
        *,
        mode: StoreMode,
        operation: str,
        manifest: GenerationManifest | None = None,
        timeout_ms: int | None = None,
        priority: str | None = None,
        isolation_level: str | None = "",
        require_schema_revision: str | None = None,
    ) -> sqlite3.Connection:
        operation_name = str(operation or "").strip()
        if not operation_name:
            raise StorageRegistryError("store operation name is required")
        selected_manifest = manifest or self.load()
        validate_manifest(selected_manifest)
        generation = self.generation(
            logical_store,
            manifest=selected_manifest,
        )
        if (
            require_schema_revision
            and generation.schema_revision != require_schema_revision
        ):
            raise StorageRegistryError(
                f"{logical_store} schema mismatch: expected {require_schema_revision}, "
                f"got {generation.schema_revision}"
            )
        path = self.resolve(logical_store, manifest=selected_manifest)
        if mode == "ro":
            if not path.is_file():
                raise StorageRegistryError(f"{logical_store} generation file is missing")
            uri = f"file:{path}?mode=ro"
            conn = connect_sqlite(
                uri,
                timeout_ms=timeout_ms,
                priority=priority,
                isolation_level=isolation_level,
                uri=True,
            )
            conn.execute("PRAGMA query_only=ON")
            if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
                conn.close()
                raise StorageRegistryError(f"{logical_store} query_only could not be enabled")
        elif mode == "rw":
            if not path.parent.is_dir():
                raise StorageRegistryError(f"{logical_store} generation directory is missing")
            conn = connect_sqlite(
                f"file:{path}?mode=rwc",
                timeout_ms=timeout_ms,
                priority=priority,
                isolation_level=isolation_level,
                uri=True,
            )
        else:
            raise StorageRegistryError(f"unsupported store mode: {mode}")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if (
            not selected_manifest.implicit
            and selected_manifest.state != "monolith"
        ):
            meta_table = (
                "finance_raw_schema_meta"
                if logical_store == "finance_raw"
                else "finance_operational_schema_meta"
            )
            try:
                identity = conn.execute(
                    f"""SELECT schema_revision,logical_store,generation_id,
                               generation_epoch,source_fingerprint
                        FROM {meta_table} WHERE singleton=1"""
                ).fetchone()
            except sqlite3.DatabaseError as exc:
                conn.close()
                raise StorageRegistryError(
                    f"{logical_store} schema identity is unavailable"
                ) from exc
            if identity is None:
                conn.close()
                raise StorageRegistryError(
                    f"{logical_store} schema identity row is missing"
                )
            actual = {
                "schema_revision": str(identity["schema_revision"]),
                "logical_store": str(identity["logical_store"]),
                "generation_id": str(identity["generation_id"]),
                "generation_epoch": str(identity["generation_epoch"]),
                "source_fingerprint": str(identity["source_fingerprint"]),
            }
            expected = {
                "schema_revision": generation.schema_revision,
                "logical_store": logical_store,
                "generation_id": generation.generation_id,
                "generation_epoch": generation.generation_epoch,
                "source_fingerprint": selected_manifest.source_fingerprint,
            }
            if actual != expected:
                conn.close()
                raise StorageRegistryError(
                    f"{logical_store} file identity does not match the selected generation"
                )
        observation = SQLiteOpenObservation(
            opened_at=_utc_now(),
            logical_store=logical_store,
            mode=mode,
            operation=operation_name[:160],
            generation_id=generation.generation_id,
            schema_revision=generation.schema_revision,
            path_identity=_sha256(
                {
                    "generation_id": generation.generation_id,
                    "relative_path": generation.relative_path,
                }
            ),
        )
        with _OBSERVATION_LOCK:
            _OBSERVATIONS.append(observation)
        return conn

    @contextmanager
    def session(
        self,
        logical_store: StoreName,
        *,
        mode: StoreMode,
        operation: str,
        manifest: GenerationManifest | None = None,
        timeout_ms: int | None = None,
        priority: str | None = None,
        isolation_level: str | None = "",
        require_schema_revision: str | None = None,
    ) -> Iterator[sqlite3.Connection]:
        conn = self.connect(
            logical_store,
            mode=mode,
            operation=operation,
            manifest=manifest,
            timeout_ms=timeout_ms,
            priority=priority,
            isolation_level=isolation_level,
            require_schema_revision=require_schema_revision,
        )
        try:
            yield conn
        finally:
            conn.close()

    def attach_readonly(
        self,
        conn: sqlite3.Connection,
        logical_store: StoreName,
        *,
        schema_name: str,
        operation: str,
        manifest: GenerationManifest | None = None,
    ) -> GenerationManifest:
        """Attach one registry-selected store read-only with identity readback."""

        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema_name) is None:
            raise StorageRegistryError("attached schema name is invalid")
        selected_manifest = manifest or self.load(require_files=True)
        validate_manifest(selected_manifest)
        generation = (
            selected_manifest.raw
            if logical_store == "finance_raw"
            else selected_manifest.operational
        )
        path = self.resolve(logical_store, manifest=selected_manifest)
        database_rows = conn.execute("PRAGMA database_list").fetchall()
        databases = {str(row[1]) for row in database_rows}
        if schema_name in databases:
            raise StorageRegistryError(
                f"attached schema already exists: {schema_name}"
            )
        main_row = next(
            (row for row in database_rows if str(row[1]) == "main"),
            None,
        )
        expected_main_store: StoreName = (
            "operational"
            if logical_store == "finance_raw"
            else "finance_raw"
        )
        expected_main_path = self.resolve(
            expected_main_store,
            manifest=selected_manifest,
        )
        actual_main_path = Path(
            str(main_row[2] if main_row is not None else "")
        ).resolve()
        if actual_main_path != expected_main_path:
            raise StorageRegistryError(
                "primary connection does not match the pinned generation"
            )
        conn.execute(
            f"ATTACH DATABASE ? AS {schema_name}",
            (f"file:{path}?mode=ro",),
        )
        if (
            not selected_manifest.implicit
            and selected_manifest.state != "monolith"
        ):
            meta_table = (
                "finance_raw_schema_meta"
                if logical_store == "finance_raw"
                else "finance_operational_schema_meta"
            )
            identity = conn.execute(
                f"""SELECT schema_revision,logical_store,generation_id,
                           generation_epoch,source_fingerprint
                    FROM {schema_name}.{meta_table} WHERE singleton=1"""
            ).fetchone()
            expected = (
                generation.schema_revision,
                logical_store,
                generation.generation_id,
                generation.generation_epoch,
                selected_manifest.source_fingerprint,
            )
            if identity is None or tuple(identity) != expected:
                conn.execute(f"DETACH DATABASE {schema_name}")
                raise StorageRegistryError(
                    f"{logical_store} attached file identity mismatch"
                )
        observation = SQLiteOpenObservation(
            opened_at=_utc_now(),
            logical_store=logical_store,
            mode="ro",
            operation=str(operation or "registry_attach")[:160],
            generation_id=generation.generation_id,
            schema_revision=generation.schema_revision,
            path_identity=_sha256(
                {
                    "generation_id": generation.generation_id,
                    "relative_path": generation.relative_path,
                }
            ),
        )
        with _OBSERVATION_LOCK:
            _OBSERVATIONS.append(observation)
        return selected_manifest

    def status(self) -> dict[str, Any]:
        manifest = self.load()
        raw_path = self.resolve("finance_raw", manifest=manifest)
        operational_path = self.resolve("operational", manifest=manifest)
        with _OBSERVATION_LOCK:
            observations = list(_OBSERVATIONS)
        counts = Counter(
            (item.logical_store, item.mode, item.operation) for item in observations
        )
        return {
            "contract_version": MANIFEST_CONTRACT,
            "state": manifest.state,
            "canonical_source": manifest.canonical_source,
            "implicit_manifest": manifest.implicit,
            "generation_epoch": manifest.generation_epoch,
            "manifest_sha256": manifest.manifest_sha256,
            "raw": {
                **_store_payload(manifest.raw),
                "exists": raw_path.is_file(),
                "size_bytes": raw_path.stat().st_size if raw_path.is_file() else 0,
            },
            "operational": {
                **_store_payload(manifest.operational),
                "exists": operational_path.is_file(),
                "size_bytes": operational_path.stat().st_size
                if operational_path.is_file()
                else 0,
            },
            "rollback_generation_id": manifest.rollback_generation_id,
            "open_observation_count": len(observations),
            "open_observations": [
                {
                    "logical_store": store,
                    "mode": mode,
                    "operation": operation,
                    "count": count,
                }
                for (store, mode, operation), count in sorted(counts.items())
            ],
        }


def build_manifest(
    *,
    state: str,
    canonical_source: str,
    generation_epoch: str,
    raw_generation_id: str,
    raw_relative_path: str,
    raw_watermark: str,
    operational_generation_id: str,
    operational_relative_path: str,
    operational_watermark: str,
    rollback_generation_id: str,
    source_fingerprint: str,
    created_at: str | None = None,
) -> GenerationManifest:
    manifest = GenerationManifest(
        contract_version=MANIFEST_CONTRACT,
        state=state,
        canonical_source=canonical_source,
        generation_epoch=generation_epoch,
        raw=StoreGeneration(
            logical_store="finance_raw",
            generation_id=raw_generation_id,
            generation_epoch=generation_epoch,
            relative_path=raw_relative_path,
            schema_revision=RAW_SCHEMA_REVISION,
            watermark=raw_watermark,
        ),
        operational=StoreGeneration(
            logical_store="operational",
            generation_id=operational_generation_id,
            generation_epoch=generation_epoch,
            relative_path=operational_relative_path,
            schema_revision=OPERATIONAL_SCHEMA_REVISION,
            watermark=operational_watermark,
        ),
        rollback_generation_id=rollback_generation_id,
        created_at=created_at or _utc_now(),
        source_fingerprint=source_fingerprint,
        manifest_sha256="",
        implicit=False,
    )
    validate_manifest(manifest)
    digest = _sha256(manifest_payload(manifest, include_digest=False))
    return replace(manifest, manifest_sha256=digest)


def atomic_write_manifest(path: Path, manifest: GenerationManifest) -> None:
    """Durably replace one exact generation manifest.

    This function is intentionally never called from ordinary runtime startup.
    It belongs only to an explicitly authorized apply/cutover or rollback.
    """

    validate_manifest(manifest)
    expected = _sha256(manifest_payload(manifest, include_digest=False))
    if manifest.manifest_sha256 != expected:
        raise StorageRegistryError("refusing to write a manifest with an invalid digest")
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, 0o600)
    try:
        payload = (_canonical_json(manifest_payload(manifest)) + "\n").encode("utf-8")
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(fd)
        fd = -1
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary.exists():
            temporary.unlink()


def explain_query_plan(
    conn: sqlite3.Connection,
    sql: str,
    parameters: tuple[Any, ...] = (),
) -> list[str]:
    if not str(sql or "").lstrip().upper().startswith("SELECT"):
        raise StorageRegistryError("query-plan instrumentation accepts SELECT only")
    return [
        str(row[3])
        for row in conn.execute("EXPLAIN QUERY PLAN " + sql, parameters).fetchall()
    ]
