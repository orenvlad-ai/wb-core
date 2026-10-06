"""RO current-source admission; only a proven canonical profile grants nine hours.

Discovery is bounded and is never authority. No connection/transaction is taken
over, no runtime is provisioned, and copies/fixtures keep their legacy limits.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from packages.application.storage_registry import StoreRegistry, MANIFEST_FILENAME, parse_manifest
from packages.application import business_data_schedule_profile as schedule

OFFICIAL_LEGACY_SECONDS = 30 * 60
BOOK_LEGACY_SECONDS = 3 * 3600
SELECTED_MAX_AGE_SECONDS = 9 * 3600
MAX_DISCOVERY_ANCESTORS = 8


class FbsSnapshotPolicyError(ValueError):
    pass


class _OpenedReadConnection(sqlite3.Connection):
    """Only open_current_snapshot_readonly creates the opened-file proof."""


def open_current_snapshot_readonly(path, *, timeout=5):
    """Bind a new owned RO connection at open/first-read, before it is handed out.

    Match WindowReadContext.start's before-open/after-pin inode boundary. No
    BEGIN here: callers retain their existing transaction start and ownership.
    """
    path = Path(path).resolve()
    try:
        before = path.stat()
    except OSError as exc:
        # Keep the existing missing/inaccessible RO-source exception boundary.
        raise sqlite3.OperationalError("fbs_readonly_source_unavailable") from exc
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=timeout,
                           factory=_OpenedReadConnection)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        after = path.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise FbsSnapshotPolicyError("fbs_policy_operational_file_changed_during_open")
        conn._fbs_opened_generation = ("operational_file", str(path), before.st_dev, before.st_ino)
        return conn
    except OSError as exc:
        conn.close()
        raise FbsSnapshotPolicyError("fbs_policy_operational_file_changed_during_open") from exc
    except BaseException:
        conn.close()
        raise


def _opened_generation(conn):
    if type(conn) is _OpenedReadConnection:
        return conn._fbs_opened_generation
    from packages.application.web_vitrina_window_read_context import _BorrowedConnection, active_window_read_context
    context = active_window_read_context()
    if type(conn) is _BorrowedConnection and context is not None and conn._physical is context._physical:
        if conn.ready_header_cache_generation != context.operational_generation:
            raise FbsSnapshotPolicyError("fbs_policy_borrowed_generation_mismatch")
        return context.operational_generation
    return None  # PRAGMA path/schema can never prove an old raw connection's inode.


@dataclass(frozen=True)
class CurrentSnapshotPolicy:
    selected: bool = False
    runtime_dir: str = ""
    manifest_sha256: str = ""
    profile_fingerprint: str = ""

    @property
    def official_max_age_seconds(self):
        return SELECTED_MAX_AGE_SECONDS if self.selected else OFFICIAL_LEGACY_SECONDS

    @property
    def book_max_age_seconds(self):
        return SELECTED_MAX_AGE_SECONDS if self.selected else BOOK_LEGACY_SECONDS


def _main_path(conn):
    name = next((str(row[2]) for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
    return Path(name).resolve() if name else None


def _identity(conn, manifest):
    # Same five-field boundary as StoreRegistry.connect; no schema initializer.
    if manifest.state == "monolith":
        return
    row = conn.execute("SELECT schema_revision,logical_store,generation_id,generation_epoch,source_fingerprint "
                       "FROM finance_operational_schema_meta WHERE singleton=1").fetchone()
    expected = (manifest.operational.schema_revision, "operational", manifest.operational.generation_id,
                manifest.operational.generation_epoch, manifest.source_fingerprint)
    if row is None or tuple(row) != expected:
        raise FbsSnapshotPolicyError("fbs_policy_operational_generation_mismatch")


def _load_manifest(registry):
    # Registry writes a private manifest. Reuse the bounded NOFOLLOW reader so
    # even discovery cannot read an arbitrary large file or follow a symlink.
    payload = schedule.read_private(registry.manifest_path, optional=True)
    return parse_manifest(payload) if payload is not None else None


def _bound_policy(conn, runtime):
    registry = StoreRegistry(runtime)
    manifest = _load_manifest(registry)
    if manifest is None:
        raise FbsSnapshotPolicyError("fbs_policy_authority_changed")
    actual = _main_path(conn)
    expected = registry.resolve("operational", manifest=manifest)
    if actual is None or actual != expected:
        raise FbsSnapshotPolicyError("fbs_policy_operational_authority_mismatch")
    before = expected.stat()
    opened = _opened_generation(conn)
    if opened is not None and opened != ("operational_file", str(expected), before.st_dev, before.st_ino):
        raise FbsSnapshotPolicyError("fbs_policy_opened_file_authority_changed")
    _identity(conn, manifest)
    selector = schedule.load_selector(runtime)
    if selector is not None and selector["profile"]["fbs_max_age_seconds"] != SELECTED_MAX_AGE_SECONDS:
        raise FbsSnapshotPolicyError("fbs_policy_approved_source_age_mismatch")
    # Pin both bounded authorities around the read; never cache a selected
    # policy across operations, rollback, or storage generation cutover.
    after_manifest = _load_manifest(registry)
    if (after_manifest is None or after_manifest.manifest_sha256 != manifest.manifest_sha256
            or schedule.load_selector(runtime) != selector):
        raise FbsSnapshotPolicyError("fbs_policy_authority_changed")
    after = expected.stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise FbsSnapshotPolicyError("fbs_policy_operational_file_changed")
    return CurrentSnapshotPolicy(bool(selector) and opened is not None, str(runtime), manifest.manifest_sha256,
                                 selector["profile_fingerprint"] if selector else "")


def resolve_current_snapshot_policy(*, connection=None, runtime_dir=None):
    """Resolve a current policy without writing or owning caller transactions.

    Explicit runtime callers and window context supply candidates. For conn-only
    readers inspect at most eight ancestors for persisted manifests; only an
    exact selected operational path and its schema identity establish authority.
    Unknown/noncanonical fixtures fall back to legacy. A selected canonical
    runtime with bad metadata fails closed instead of granting more freshness.
    """
    try:
        if runtime_dir is not None:
            runtime = Path(runtime_dir).resolve()
            registry = StoreRegistry(runtime)
            manifest = _load_manifest(registry)
            if manifest is None:
                return CurrentSnapshotPolicy()
            if connection is not None:
                return _bound_policy(connection, runtime)
            from packages.application.web_vitrina_window_read_context import borrowed_operational_connection
            borrowed = borrowed_operational_connection(registry.resolve("operational", manifest=manifest))
            if borrowed is not None:
                return _bound_policy(borrowed, runtime)
            with closing(open_current_snapshot_readonly(registry.resolve("operational", manifest=manifest))) as conn:
                return _bound_policy(conn, runtime)
        if connection is None:
            return CurrentSnapshotPolicy()
        actual = _main_path(connection)
        if actual is None:
            return CurrentSnapshotPolicy()
        candidates = set()
        from packages.application.web_vitrina_window_read_context import active_window_read_context
        context = active_window_read_context()
        if context is not None and context.runtime_dir is not None:
            candidates.add(context.runtime_dir)
        candidates.update(list(actual.parents)[:MAX_DISCOVERY_ANCESTORS])
        matched = []
        for runtime in sorted(candidates):
            path = runtime / MANIFEST_FILENAME
            if not path.is_file():
                continue
            try:
                registry = StoreRegistry(runtime)
                manifest = _load_manifest(registry)
                if manifest is not None and registry.resolve("operational", manifest=manifest) == actual:
                    matched.append(runtime)
            except (OSError, ValueError, RuntimeError, AttributeError, IndexError, KeyError, TypeError):
                # Unknown ancestors never establish authority or grant9h.
                continue
        if len(matched) > 1:
            raise FbsSnapshotPolicyError("fbs_policy_ambiguous_runtime")
        return _bound_policy(connection, matched[0]) if matched else CurrentSnapshotPolicy()
    except FbsSnapshotPolicyError:
        raise
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError, AttributeError, IndexError) as exc:
        raise FbsSnapshotPolicyError("fbs_current_snapshot_policy_unavailable") from exc
