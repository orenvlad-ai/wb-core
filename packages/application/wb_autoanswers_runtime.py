"""Durable server runtime for WB feedback synchronization and autoanswers.

This module intentionally contains no WB or OpenAI network calls.  It owns the
SQLite source of truth, idempotency, leases, hashes, policy settings and budget
reservations.  Network adapters and workers are separate modules.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from packages.contracts.wb_autoanswers import (
    AUTO_SAFE_ROUTES,
    BACKFILL_FROM_DATE,
    EVALUATION_SIGNATURE,
    MODE_AUTO_ALL,
    MODE_AUTO_SAFE,
    MODE_DRAFT_ONLY,
    MODE_MANUAL,
    NODE_BOUNDARY_VERSION,
    PROMPT_BUNDLE_VERSION,
    STATE_APPROVED,
    STATE_GENERATED,
    STATE_NEEDS_REVIEW,
    STATE_PROCESSING,
    STATE_PUBLISH_PENDING_READBACK,
    STATE_PUBLISHED,
    STATE_PUBLISHING,
    STATE_QUEUED,
    STATE_RETRYABLE_ERROR,
    STATE_SKIPPED,
    STATE_SYNCED,
    STATE_TERMINAL_ERROR,
    AutoanswersSettings,
    assert_transition,
    processing_key,
    publication_key,
    validate_mode,
)


SCHEMA_VERSION = 2
DEFAULT_DAILY_CAP_USD = Decimal("5.00")
DEFAULT_MONTHLY_CAP_USD = Decimal("50.00")
DEFAULT_WARNING_RATIO = Decimal("0.70")
# Conservative upper reservation covers the frozen pipeline's bounded normal
# path plus two rewrite/validator cycles.  Settlement releases the difference.
DEFAULT_JOB_RESERVATION_USD = Decimal("1.00")
DEFAULT_POLICY_VERSION = "owner-policy-2026-07-20-v1"
DEFAULT_LEASE_SECONDS = 300
BACKLOG_PREVIEW_TTL_SECONDS = 900
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
COMPRESSED_SCHEMA_BACKUP_CONTRACT = "wb_autoanswers_compressed_schema_backup_v2"


class AutoanswersRuntimeError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zstd_decompressed_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    process = subprocess.Popen(
        ["zstd", "--decompress", "--stdout", "--quiet", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
        digest.update(chunk)
    _stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise AutoanswersRuntimeError(
            "compressed schema backup decompression failed: "
            + stderr.decode("utf-8", errors="replace").strip(),
            code="schema_backup_failed",
        )
    return digest.hexdigest()


def _verified_compressed_schema_backup_status(
    runtime_dir: Path,
    *,
    verify_bytes: bool,
) -> dict[str, Any]:
    backup_dir = runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
    manifests = sorted(backup_dir.glob("*.sqlite3.zst.manifest.json")) if backup_dir.is_dir() else []
    if not manifests:
        return {"count": 0, "latest_filename": None, "integrity_check": None, "sha256": None}
    manifest_path = manifests[-1]
    try:
        metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
        filename = str(metadata.get("compressed_filename") or "")
        if (
            metadata.get("contract") != COMPRESSED_SCHEMA_BACKUP_CONTRACT
            or int(metadata.get("schema_version") or 0) != SCHEMA_VERSION
            or Path(filename).name != filename
            or metadata.get("sqlite_integrity_check") != "ok"
        ):
            raise ValueError("compressed schema backup manifest contract mismatch")
        archive = backup_dir / filename
        if not archive.is_file() or archive.stat().st_size != int(metadata.get("compressed_size") or -1):
            raise ValueError("compressed schema backup archive size mismatch")
        if verify_bytes:
            archive_sha = _sha256_path(archive)
            if archive_sha != str(metadata.get("compressed_sha256") or ""):
                raise ValueError("compressed schema backup archive hash mismatch")
            subprocess.run(
                ["zstd", "--test", "--quiet", str(archive)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=7200,
                check=True,
            )
            if _zstd_decompressed_sha256(archive) != str(metadata.get("snapshot_sha256") or ""):
                raise ValueError("compressed schema backup restore hash mismatch")
    except Exception as exc:
        if isinstance(exc, AutoanswersRuntimeError):
            raise
        raise AutoanswersRuntimeError(
            "compressed pre-schema backup verification failed",
            code="schema_backup_failed",
        ) from exc
    return {
        "count": len(manifests),
        "latest_filename": filename,
        "manifest_filename": manifest_path.name,
        "size_bytes": int(metadata["compressed_size"]),
        "integrity_check": "ok",
        "sha256": f"sha256:{metadata['compressed_sha256']}",
        "snapshot_sha256": f"sha256:{metadata['snapshot_sha256']}",
        "format": "zstd",
    }


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized_reply(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def final_reply_hash(value: Any) -> str:
    return sha256_text(normalized_reply(value))


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\u0000", " ").split()).strip()


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def stable_media_url(value: Any) -> str:
    """Remove query/fragment churn while preserving the media object identity."""

    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if not parsed.scheme or not parsed.netloc:
        return text.split("?", 1)[0].split("#", 1)[0]
    host = parsed.hostname.lower() if parsed.hostname else parsed.netloc.lower()
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path, "", ""))


def _tags(raw: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("bables", "bubbles", "badges", "tags", "reviewTags", "review_tags", "chips"):
        value = raw.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    label = _clean_text(item.get("name") or item.get("label") or item.get("text"))
                else:
                    label = _clean_text(item)
                if label:
                    values.append(label)
    return sorted(set(values), key=lambda item: item.casefold())


def media_projection(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    media: list[dict[str, Any]] = []
    photos = raw.get("photoLinks") or raw.get("photos") or []
    if isinstance(photos, list):
        for ordinal, item in enumerate(photos):
            record = _mapping(item)
            full = str(record.get("fullSize") or record.get("full_size") or record.get("url") or "").strip()
            mini = str(record.get("miniSize") or record.get("mini_size") or record.get("preview") or "").strip()
            media.append(
                {
                    "kind": "photo",
                    "ordinal": ordinal,
                    "stable_full": stable_media_url(full),
                    "stable_preview": stable_media_url(mini),
                    "source_full": full,
                    "source_preview": mini,
                }
            )
    video_value = raw.get("video") or raw.get("videos")
    videos = video_value if isinstance(video_value, list) else [video_value] if video_value else []
    for ordinal, item in enumerate(videos):
        record = _mapping(item)
        link = str(record.get("link") or record.get("url") or "").strip()
        preview = str(record.get("previewImage") or record.get("preview") or "").strip()
        media.append(
            {
                "kind": "video",
                "ordinal": ordinal,
                "stable_full": stable_media_url(link),
                "stable_preview": stable_media_url(preview),
                "source_full": link,
                "source_preview": preview,
                "duration_seconds": _safe_int(record.get("durationSec") or record.get("duration_seconds")),
            }
        )
    return media


def content_projection(raw: Mapping[str, Any]) -> dict[str, Any]:
    product = _mapping(raw.get("productDetails") or raw.get("product"))
    media = media_projection(raw)
    stable_media = [
        {
            "kind": item["kind"],
            "ordinal": item["ordinal"],
            "stable_full": item["stable_full"],
            "stable_preview": item["stable_preview"],
            **(
                {"duration_seconds": item.get("duration_seconds")}
                if item["kind"] == "video"
                else {}
            ),
        }
        for item in media
    ]
    rating = _safe_int(raw.get("productValuation") if raw.get("productValuation") is not None else raw.get("rating"))
    return {
        "text": _clean_text(raw.get("text")),
        "pros": _clean_text(raw.get("pros")),
        "cons": _clean_text(raw.get("cons")),
        "rating": rating,
        "product": {
            "nm_id": _safe_int(product.get("nmId") if product else raw.get("nmId")),
            "supplier_article": _clean_text(product.get("supplierArticle") or raw.get("supplierArticle")),
            "product_name": _clean_text(product.get("productName") or raw.get("productName")),
            "brand_name": _clean_text(product.get("brandName") or raw.get("brandName")),
            "size": _clean_text(product.get("size")),
        },
        "tags": _tags(raw),
        "media": stable_media,
    }


def observation_projection(raw: Mapping[str, Any]) -> dict[str, Any]:
    answer = raw.get("answer")
    answer_record = _mapping(answer)
    answer_text = _clean_text(answer_record.get("text") if answer_record else answer)
    return {
        "answer": {
            "text": answer_text,
            "state": _clean_text(answer_record.get("state")),
            "editable": answer_record.get("editable") if answer_record else None,
        },
        "state": _clean_text(raw.get("state")),
        "was_viewed": raw.get("wasViewed"),
        "order_status": _clean_text(raw.get("orderStatus")),
        "matching_size": _clean_text(raw.get("matchingSize")),
        "return_allowed": raw.get("isAbleReturnProductOrders"),
        "return_date": _clean_text(raw.get("returnProductOrdersDate")),
        "parent_feedback_id": _clean_text(raw.get("parentFeedbackId")),
        "child_feedback_id": _clean_text(raw.get("childFeedbackId")),
    }


def content_version_hash(raw: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(content_projection(raw)))


def wb_observation_hash(raw: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(observation_projection(raw)))


def _answer_text(raw: Mapping[str, Any]) -> str:
    return str(observation_projection(raw)["answer"]["text"])


def _feedback_id(raw: Mapping[str, Any]) -> str:
    value = _clean_text(raw.get("id") or raw.get("feedbackId") or raw.get("feedback_id"))
    if not value:
        raise AutoanswersRuntimeError("WB feedback has no id", code="feedback_id_missing")
    return value


def _force_off_from_env(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return str(source.get("WB_AUTOANSWERS_FORCE_OFF", "")).strip().lower() in TRUE_VALUES


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    except Exception as exc:
        raise ValueError(f"invalid money value: {value}") from exc


class AutoanswersRepository:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        now_factory: Any = utc_now,
        env: Mapping[str, str] | None = None,
        schema_lock_held: bool = False,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.db_path = self.runtime_dir / "registry_upload_runtime.sqlite3"
        self.now_factory = now_factory
        self.env = env
        self.ensure_schema(schema_lock_held=schema_lock_held)

    def _now(self) -> datetime:
        value = self.now_factory()
        if not isinstance(value, datetime):
            raise TypeError("now_factory must return datetime")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _connect(self) -> sqlite3.Connection:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_schema(self, *, schema_lock_held: bool = False) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if schema_lock_held:
            self._ensure_schema_locked()
            return
        lock_path = self.runtime_dir / ".wb_autoanswers_schema.lock"
        with lock_path.open("a+b") as lock_handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                self._ensure_schema_locked()
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _ensure_schema_locked(self) -> None:
        if not self._schema_version_is_applied():
            self._backup_database_before_first_schema()
        conn = self._connect()
        try:
            # sqlite3.executescript otherwise commits an already-open
            # transaction. Start the migration inside the script so
            # all additive DDL plus marker/settings rows are atomic.
            conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA_SQL)
            self._migrate_schema_v2(conn)
            conn.execute(
                "INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswers_schema_migrations(version, applied_at) VALUES(?, ?)",
                (SCHEMA_VERSION, iso_utc(self._now())),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswers_settings(
                    singleton, master_enabled, mode, enable_epoch, enabled_at,
                    daily_cap_usd, monthly_cap_usd, warning_ratio,
                    max_reservation_per_review_usd, policy_version, updated_at
                ) VALUES(1, 0, ?, 0, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    MODE_DRAFT_ONLY,
                    str(DEFAULT_DAILY_CAP_USD),
                    str(DEFAULT_MONTHLY_CAP_USD),
                    str(DEFAULT_WARNING_RATIO),
                    str(DEFAULT_JOB_RESERVATION_USD),
                    DEFAULT_POLICY_VERSION,
                    iso_utc(self._now()),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _schema_version_is_applied(self) -> bool:
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return False
        uri = f"file:{self.db_path.resolve()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=10) as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    ("sheet_vitrina_v1_wb_autoanswers_schema_migrations",),
                ).fetchone()
                if table is None:
                    return False
                row = conn.execute(
                    "SELECT 1 FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations WHERE version=?",
                    (SCHEMA_VERSION,),
                ).fetchone()
                return row is not None
        except sqlite3.DatabaseError as exc:
            raise AutoanswersRuntimeError(
                "runtime database is unreadable before autoanswers schema migration",
                code="schema_preflight_failed",
            ) from exc

    def _backup_database_before_first_schema(self) -> Path | None:
        """Create and verify a coherent backup before the first additive schema."""

        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return None
        compressed = _verified_compressed_schema_backup_status(
            self.runtime_dir,
            verify_bytes=True,
        )
        if int(compressed.get("count") or 0) > 0:
            return (
                self.runtime_dir
                / "backups"
                / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
                / str(compressed["latest_filename"])
            )
        backup_dir = self.runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(backup_dir, 0o700)
        stamp = self._now().strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / (
            f"registry_upload_runtime__pre_autoanswers_v{SCHEMA_VERSION}__{stamp}__{uuid4().hex[:8]}.sqlite3"
        )
        source_uri = f"file:{self.db_path.resolve()}?mode=ro"
        try:
            with sqlite3.connect(source_uri, uri=True, timeout=60) as source:
                with sqlite3.connect(backup_path, timeout=60) as target:
                    source.backup(target)
            os.chmod(backup_path, 0o600)
            with sqlite3.connect(f"file:{backup_path.resolve()}?mode=ro", uri=True, timeout=60) as verify:
                integrity = str(verify.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise sqlite3.DatabaseError(f"backup integrity_check={integrity}")
        except Exception as exc:
            backup_path.unlink(missing_ok=True)
            raise AutoanswersRuntimeError(
                "verified backup failed; autoanswers schema was not applied",
                code="schema_backup_failed",
            ) from exc
        return backup_path

    @staticmethod
    def _migrate_schema_v2(conn: sqlite3.Connection) -> None:
        """Widen the persisted mode enum and add manual-review evidence columns."""

        settings_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_wb_autoanswers_settings'"
        ).fetchone()
        settings_sql = str(settings_sql_row["sql"] or "") if settings_sql_row else ""
        if "'manual'" not in settings_sql:
            conn.execute(
                "ALTER TABLE sheet_vitrina_v1_wb_autoanswers_settings RENAME TO sheet_vitrina_v1_wb_autoanswers_settings_v1"
            )
            conn.execute(
                """
                CREATE TABLE sheet_vitrina_v1_wb_autoanswers_settings(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    master_enabled INTEGER NOT NULL DEFAULT 0 CHECK(master_enabled IN (0,1)),
                    mode TEXT NOT NULL DEFAULT 'draft_only' CHECK(mode IN ('manual','draft_only','auto_safe','auto_all')),
                    enable_epoch INTEGER NOT NULL DEFAULT 0,
                    enabled_at TEXT,
                    daily_cap_usd TEXT NOT NULL,
                    monthly_cap_usd TEXT NOT NULL,
                    warning_ratio TEXT NOT NULL,
                    max_reservation_per_review_usd TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_autoanswers_settings
                SELECT singleton, master_enabled, mode, enable_epoch, enabled_at,
                       daily_cap_usd, monthly_cap_usd, warning_ratio,
                       max_reservation_per_review_usd, policy_version, updated_at
                FROM sheet_vitrina_v1_wb_autoanswers_settings_v1
                """
            )
            conn.execute("DROP TABLE sheet_vitrina_v1_wb_autoanswers_settings_v1")

        def add_column(table: str, column: str, declaration: str) -> None:
            columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

        job_table = "sheet_vitrina_v1_wb_autoanswer_jobs"
        for name, declaration in (
            ("manual_reply", "TEXT"),
            ("manual_reply_sha256", "TEXT"),
            ("manual_guard_passed", "INTEGER"),
            ("manual_guard_errors_json", "TEXT"),
            ("manual_reviewed_by", "TEXT"),
            ("manual_reviewed_at", "TEXT"),
            ("manual_edit_revision", "INTEGER NOT NULL DEFAULT 0"),
        ):
            add_column(job_table, name, declaration)

        publication_table = "sheet_vitrina_v1_wb_publication_jobs"
        for name, declaration in (
            ("request_source", "TEXT NOT NULL DEFAULT 'automatic'"),
            ("requested_by", "TEXT"),
            ("mode_at_enqueue", "TEXT"),
            ("manual_edit_revision", "INTEGER"),
        ):
            add_column(publication_table, name, declaration)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sv1_pub_jobs_one_create_per_version
            ON sheet_vitrina_v1_wb_publication_jobs(feedback_id, content_version)
            """
        )

    def settings(self) -> AutoanswersSettings:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton = 1").fetchone()
        if row is None:
            raise AutoanswersRuntimeError("autoanswers settings missing", code="settings_missing")
        force_off = _force_off_from_env(self.env)
        enabled = bool(row["master_enabled"])
        return AutoanswersSettings(
            master_enabled=enabled,
            force_off=force_off,
            effective_enabled=enabled and not force_off,
            mode=str(row["mode"]),
            enable_epoch=int(row["enable_epoch"]),
            enabled_at=str(row["enabled_at"]) if row["enabled_at"] else None,
            daily_cap_usd=float(row["daily_cap_usd"]),
            monthly_cap_usd=float(row["monthly_cap_usd"]),
            warning_ratio=float(row["warning_ratio"]),
            max_reservation_per_review_usd=float(row["max_reservation_per_review_usd"]),
            policy_version=str(row["policy_version"]),
            updated_at=str(row["updated_at"]),
        )

    def update_settings(
        self,
        *,
        master_enabled: bool | None = None,
        mode: str | None = None,
        daily_cap_usd: Any | None = None,
        monthly_cap_usd: Any | None = None,
        warning_ratio: Any | None = None,
        actor_id: str,
    ) -> AutoanswersSettings:
        actor = _clean_text(actor_id)
        if not actor:
            raise ValueError("actor_id is required")
        now = self._now()
        with self.transaction() as conn:
            current = conn.execute("SELECT * FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton = 1").fetchone()
            if current is None:
                raise AutoanswersRuntimeError("autoanswers settings missing", code="settings_missing")
            next_master = bool(current["master_enabled"]) if master_enabled is None else bool(master_enabled)
            if next_master and not bool(current["master_enabled"]) and _force_off_from_env(self.env):
                raise AutoanswersRuntimeError(
                    "autoanswers cannot be enabled while emergency force-off is active",
                    code="emergency_force_off",
                )
            next_mode = str(current["mode"]) if mode is None else validate_mode(mode)
            daily = _money(current["daily_cap_usd"] if daily_cap_usd is None else daily_cap_usd)
            monthly = _money(current["monthly_cap_usd"] if monthly_cap_usd is None else monthly_cap_usd)
            ratio = Decimal(str(current["warning_ratio"] if warning_ratio is None else warning_ratio))
            if daily <= 0 or monthly <= 0 or monthly < daily:
                raise ValueError("budget caps must be positive and monthly >= daily")
            if ratio <= 0 or ratio >= 1:
                raise ValueError("warning_ratio must be between 0 and 1")
            epoch = int(current["enable_epoch"])
            enabled_at = current["enabled_at"]
            if next_master and not bool(current["master_enabled"]):
                epoch += 1
                enabled_at = iso_utc(now)
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_settings
                SET master_enabled=?, mode=?, enable_epoch=?, enabled_at=?,
                    daily_cap_usd=?, monthly_cap_usd=?, warning_ratio=?, updated_at=?
                WHERE singleton=1
                """,
                (int(next_master), next_mode, epoch, enabled_at, str(daily), str(monthly), str(ratio), iso_utc(now)),
            )
            self._audit(
                conn,
                aggregate_type="settings",
                aggregate_id="singleton",
                event_type="settings_updated",
                actor_type="user",
                actor_id=actor,
                details={
                    "master_enabled": next_master,
                    "mode": next_mode,
                    "enable_epoch": epoch,
                    "daily_cap_usd": str(daily),
                    "monthly_cap_usd": str(monthly),
                    "warning_ratio": str(ratio),
                },
                at=now,
            )
        return self.settings()

    def assert_effective_on(self, *, operation: str) -> AutoanswersSettings:
        settings = self.settings()
        if not settings.effective_enabled:
            reason = "emergency_force_off" if settings.force_off else "master_switch_off"
            raise AutoanswersRuntimeError(
                f"autoanswers is OFF; {operation} is blocked",
                code=reason,
            )
        return settings

    def _audit(
        self,
        conn: sqlite3.Connection,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        actor_type: str,
        actor_id: str,
        details: Mapping[str, Any],
        at: datetime,
        previous_state: str | None = None,
        next_state: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_wb_autoanswers_audit_events(
                event_id, aggregate_type, aggregate_id, event_type,
                previous_state, next_state, actor_type, actor_id,
                bundle_version, evaluation_signature, details_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                uuid4().hex,
                aggregate_type,
                aggregate_id,
                event_type,
                previous_state,
                next_state,
                actor_type,
                actor_id,
                PROMPT_BUNDLE_VERSION,
                EVALUATION_SIGNATURE,
                canonical_json(dict(details)),
                iso_utc(at),
            ),
        )

    def upsert_feedback(
        self,
        raw: Mapping[str, Any],
        *,
        source_stream: str,
        run_kind: str,
        sync_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Upsert one WB observation while versioning semantic content only.

        ``run_kind`` is either ``backfill`` or ``steady``.  Backfill rows are
        deliberately never auto-enqueued.  A review first observed while the
        master switch is off also stays outside future automatic backlogs.
        """

        if run_kind not in {"backfill", "steady", "reconciliation", "detail_readback"}:
            raise ValueError(f"unsupported sync run_kind: {run_kind}")
        feedback_id = _feedback_id(raw)
        now = self._now()
        content = content_projection(raw)
        observation = observation_projection(raw)
        content_hash = sha256_text(canonical_json(content))
        observation_hash = sha256_text(canonical_json(observation))
        media = media_projection(raw)
        product = content["product"]
        created_at_wb = _clean_text(raw.get("createdDate") or raw.get("created_at")) or None
        updated_at_wb = _clean_text(raw.get("updatedDate") or raw.get("updated_at")) or None
        answer_text = str(observation["answer"]["text"])
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
                (feedback_id,),
            ).fetchone()
            settings = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
            ).fetchone()
            if settings is None:
                raise AutoanswersRuntimeError("autoanswers settings missing", code="settings_missing")
            is_new = current is None
            content_changed = is_new or str(current["content_version_hash"]) != content_hash
            observation_changed = is_new or str(current["wb_observation_hash"]) != observation_hash
            content_version = 1 if is_new else int(current["content_version"]) + int(content_changed)
            effective_on = bool(settings["master_enabled"]) and not _force_off_from_env(self.env)
            eligible_epoch: int | None = None
            automatic_mode = str(settings["mode"]) != MODE_MANUAL
            if is_new and run_kind == "steady" and effective_on and automatic_mode and not answer_text:
                eligible_epoch = int(settings["enable_epoch"])
            elif current is not None and current["auto_eligible_epoch"] is not None:
                eligible_epoch = int(current["auto_eligible_epoch"])

            first_seen_at = iso_utc(now) if is_new else str(current["first_seen_at"])
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_feedbacks(
                    feedback_id, created_at_wb, updated_at_wb, content_version,
                    content_version_hash, wb_observation_hash, content_json,
                    observation_json, raw_json, answer_text, rating, nm_id,
                    supplier_article, product_name, brand_name, has_photo,
                    has_video, source_stream, first_seen_at, last_seen_at,
                    sync_status, auto_eligible_epoch, last_sync_run_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(feedback_id) DO UPDATE SET
                    created_at_wb=excluded.created_at_wb,
                    updated_at_wb=excluded.updated_at_wb,
                    content_version=excluded.content_version,
                    content_version_hash=excluded.content_version_hash,
                    wb_observation_hash=excluded.wb_observation_hash,
                    content_json=excluded.content_json,
                    observation_json=excluded.observation_json,
                    raw_json=excluded.raw_json,
                    answer_text=excluded.answer_text,
                    rating=excluded.rating,
                    nm_id=excluded.nm_id,
                    supplier_article=excluded.supplier_article,
                    product_name=excluded.product_name,
                    brand_name=excluded.brand_name,
                    has_photo=excluded.has_photo,
                    has_video=excluded.has_video,
                    source_stream=excluded.source_stream,
                    last_seen_at=excluded.last_seen_at,
                    sync_status=excluded.sync_status,
                    auto_eligible_epoch=COALESCE(sheet_vitrina_v1_wb_feedbacks.auto_eligible_epoch, excluded.auto_eligible_epoch),
                    last_sync_run_id=excluded.last_sync_run_id
                """,
                (
                    feedback_id,
                    created_at_wb,
                    updated_at_wb,
                    content_version,
                    content_hash,
                    observation_hash,
                    canonical_json(content),
                    canonical_json(observation),
                    canonical_json(dict(raw)),
                    answer_text,
                    content["rating"],
                    product["nm_id"],
                    product["supplier_article"],
                    product["product_name"],
                    product["brand_name"],
                    int(any(item["kind"] == "photo" for item in media)),
                    int(any(item["kind"] == "video" for item in media)),
                    _clean_text(source_stream),
                    first_seen_at,
                    iso_utc(now),
                    STATE_SYNCED,
                    eligible_epoch,
                    sync_run_id,
                ),
            )
            if content_changed:
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_wb_feedback_versions(
                        feedback_id, content_version, content_version_hash,
                        content_json, source_raw_json, created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        feedback_id,
                        content_version,
                        content_hash,
                        canonical_json(content),
                        canonical_json(dict(raw)),
                        iso_utc(now),
                    ),
                )
            for item in media:
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_wb_feedback_media(
                        feedback_id, content_version, kind, ordinal,
                        stable_full_url, stable_preview_url, source_full_url,
                        source_preview_url, duration_seconds, fetch_status, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,'pending',?)
                    ON CONFLICT(feedback_id, content_version, kind, ordinal) DO UPDATE SET
                        source_full_url=excluded.source_full_url,
                        source_preview_url=excluded.source_preview_url,
                        duration_seconds=excluded.duration_seconds,
                        updated_at=excluded.updated_at
                    """,
                    (
                        feedback_id,
                        content_version,
                        item["kind"],
                        item["ordinal"],
                        item["stable_full"],
                        item["stable_preview"],
                        item["source_full"],
                        item["source_preview"],
                        item.get("duration_seconds"),
                        iso_utc(now),
                    ),
                )
            if is_new:
                self._audit(
                    conn,
                    aggregate_type="feedback",
                    aggregate_id=feedback_id,
                    event_type="feedback_discovered",
                    actor_type="sync",
                    actor_id=source_stream,
                    details={"run_kind": run_kind, "content_version": content_version},
                    at=now,
                    previous_state="discovered",
                    next_state=STATE_SYNCED,
                )
            elif content_changed or observation_changed:
                self._audit(
                    conn,
                    aggregate_type="feedback",
                    aggregate_id=feedback_id,
                    event_type="feedback_upserted",
                    actor_type="sync",
                    actor_id=source_stream,
                    details={
                        "content_changed": content_changed,
                        "observation_changed": observation_changed,
                        "content_version": content_version,
                    },
                    at=now,
                )
        return {
            "feedback_id": feedback_id,
            "is_new": is_new,
            "content_changed": content_changed,
            "observation_changed": observation_changed,
            "content_version": content_version,
            "content_version_hash": content_hash,
            "wb_observation_hash": observation_hash,
            "has_external_answer": bool(answer_text),
            "auto_eligible_epoch": eligible_epoch,
            "auto_enqueue": bool(
                run_kind == "steady"
                and content_changed
                and eligible_epoch is not None
                and effective_on
                and automatic_mode
                and eligible_epoch == int(settings["enable_epoch"])
                and not answer_text
            ),
        }

    def sync_cursor(self, stream_key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_sync_state WHERE stream_key=?",
                (_clean_text(stream_key),),
            ).fetchone()
        if row is None:
            return None
        return {
            "stream_key": row["stream_key"],
            "cursor": json.loads(str(row["cursor_json"])),
            "watermark_at": row["watermark_at"],
            "last_success_at": row["last_success_at"],
            "updated_at": row["updated_at"],
        }

    def local_unanswered_count(self) -> int:
        with closing(self._connect()) as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_feedbacks WHERE COALESCE(answer_text,'')=''"
                ).fetchone()[0]
            )

    def latest_feedback_id(self, *, sync_run_id: str | None = None) -> str | None:
        clauses: list[str] = []
        params: list[Any] = []
        if sync_run_id:
            clauses.append("last_sync_run_id=?")
            params.append(_clean_text(sync_run_id))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"""
                SELECT feedback_id FROM sheet_vitrina_v1_wb_feedbacks
                {where}
                ORDER BY COALESCE(created_at_wb, first_seen_at) DESC, feedback_id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return str(row["feedback_id"]) if row is not None else None

    def operational_status(self) -> dict[str, Any]:
        """Return content-free production evidence for sync and safety gates."""

        with closing(self._connect()) as conn:
            feedback = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       MIN(substr(created_at_wb,1,10)) AS min_created_date,
                       MAX(substr(created_at_wb,1,10)) AS max_created_date,
                       SUM(CASE WHEN COALESCE(answer_text,'')='' THEN 1 ELSE 0 END) AS unanswered,
                       SUM(CASE WHEN has_photo=1 THEN 1 ELSE 0 END) AS with_photo,
                       SUM(CASE WHEN has_video=1 THEN 1 ELSE 0 END) AS with_video
                FROM sheet_vitrina_v1_wb_feedbacks
                """
            ).fetchone()
            ai_rows = conn.execute(
                "SELECT state, COUNT(*) AS count FROM sheet_vitrina_v1_wb_autoanswer_jobs GROUP BY state"
            ).fetchall()
            publication_rows = conn.execute(
                "SELECT state, COUNT(*) AS count FROM sheet_vitrina_v1_wb_publication_jobs GROUP BY state"
            ).fetchall()
            cursor_rows = conn.execute(
                "SELECT stream_key, cursor_json, watermark_at, last_success_at, updated_at FROM sheet_vitrina_v1_wb_sync_state ORDER BY stream_key"
            ).fetchall()
            schema_rows = conn.execute(
                "SELECT version, applied_at FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations ORDER BY version"
            ).fetchall()
        backup_dir = self.runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
        backups = sorted(backup_dir.glob("*.sqlite3")) if backup_dir.is_dir() else []
        compressed = _verified_compressed_schema_backup_status(
            self.runtime_dir,
            verify_bytes=False,
        )
        settings = self.settings()
        return {
            "settings": {
                "master_enabled": settings.master_enabled,
                "force_off": settings.force_off,
                "effective_enabled": settings.effective_enabled,
                "mode": settings.mode,
                "enable_epoch": settings.enable_epoch,
            },
            "feedbacks": {
                "total": int(feedback["total"] or 0),
                "min_created_date": feedback["min_created_date"],
                "max_created_date": feedback["max_created_date"],
                "unanswered": int(feedback["unanswered"] or 0),
                "with_photo": int(feedback["with_photo"] or 0),
                "with_video": int(feedback["with_video"] or 0),
            },
            "ai_jobs": {str(row["state"]): int(row["count"]) for row in ai_rows},
            "publication_jobs": {str(row["state"]): int(row["count"]) for row in publication_rows},
            "sync_cursors": [
                {
                    "stream_key": str(row["stream_key"]),
                    "cursor": json.loads(str(row["cursor_json"])),
                    "watermark_at": row["watermark_at"],
                    "last_success_at": row["last_success_at"],
                    "updated_at": row["updated_at"],
                }
                for row in cursor_rows
            ],
            "schema_migrations": [dict(row) for row in schema_rows],
            "schema_backup": {
                "count": len(backups) + int(compressed.get("count") or 0),
                "latest_filename": (
                    backups[-1].name if backups else compressed.get("latest_filename")
                ),
            },
        }

    def verified_schema_backup_status(self) -> dict[str, Any]:
        backup_dir = self.runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
        backups = sorted(backup_dir.glob("*.sqlite3")) if backup_dir.is_dir() else []
        if not backups:
            return _verified_compressed_schema_backup_status(
                self.runtime_dir,
                verify_bytes=True,
            )
        latest = backups[-1]
        with sqlite3.connect(f"file:{latest.resolve()}?mode=ro", uri=True, timeout=60) as conn:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        digest = hashlib.sha256()
        with latest.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "count": len(backups),
            "latest_filename": latest.name,
            "size_bytes": latest.stat().st_size,
            "integrity_check": integrity,
            "sha256": f"sha256:{digest.hexdigest()}",
        }

    def save_sync_cursor(
        self,
        stream_key: str,
        *,
        cursor: Mapping[str, Any],
        watermark_at: str | None = None,
        successful: bool = False,
    ) -> None:
        now = self._now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_sync_state(
                    stream_key, cursor_json, watermark_at, last_success_at, updated_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(stream_key) DO UPDATE SET
                    cursor_json=excluded.cursor_json,
                    watermark_at=COALESCE(excluded.watermark_at, sheet_vitrina_v1_wb_sync_state.watermark_at),
                    last_success_at=COALESCE(excluded.last_success_at, sheet_vitrina_v1_wb_sync_state.last_success_at),
                    updated_at=excluded.updated_at
                """,
                (
                    _clean_text(stream_key),
                    canonical_json(dict(cursor)),
                    watermark_at,
                    iso_utc(now) if successful else None,
                    iso_utc(now),
                ),
            )

    def start_sync_run(self, *, run_kind: str, source_stream: str, cursor: Mapping[str, Any]) -> str:
        run_id = uuid4().hex
        now = self._now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_sync_runs(
                    sync_run_id, run_kind, source_stream, state, cursor_json,
                    started_at
                ) VALUES(?,?,?,'running',?,?)
                """,
                (run_id, _clean_text(run_kind), _clean_text(source_stream), canonical_json(dict(cursor)), iso_utc(now)),
            )
        return run_id

    def finish_sync_run(
        self,
        sync_run_id: str,
        *,
        state: str,
        discovered_count: int,
        upserted_count: int,
        cursor: Mapping[str, Any],
        error_code: str | None = None,
    ) -> None:
        if state not in {"succeeded", "retryable_error", "terminal_error"}:
            raise ValueError("invalid sync terminal state")
        now = self._now()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_sync_runs
                SET state=?, discovered_count=?, upserted_count=?, cursor_json=?,
                    error_code=?, finished_at=?
                WHERE sync_run_id=?
                """,
                (
                    state,
                    int(discovered_count),
                    int(upserted_count),
                    canonical_json(dict(cursor)),
                    _clean_text(error_code) or None,
                    iso_utc(now),
                    sync_run_id,
                ),
            )

    def enqueue_sync_command(self, *, request_key: str, actor_id: str) -> dict[str, Any]:
        key = _clean_text(request_key)
        if not key:
            raise ValueError("request_key is required")
        now = self._now()
        command_id = sha256_text(f"sync_now|{key}")
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswers_commands(
                    command_id, command_type, request_key, state, actor_id,
                    available_at, attempts, created_at, updated_at
                ) VALUES(?,'sync_now',?,'queued',?,?,0,?,?)
                """,
                (command_id, key, _clean_text(actor_id), iso_utc(now), iso_utc(now), iso_utc(now)),
            )
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            return dict(row)

    def claim_sync_command(self, *, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any] | None:
        now = self._now()
        lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_wb_autoanswers_commands
                WHERE (state IN ('queued','retryable_error') AND available_at<=?)
                   OR (state='processing' AND lease_until<=?)
                ORDER BY created_at, command_id LIMIT 1
                """,
                (iso_utc(now), iso_utc(now)),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_commands
                SET state='processing', lease_owner=?, lease_until=?, attempts=attempts+1, updated_at=?
                WHERE command_id=?
                """,
                (_clean_text(worker_id), iso_utc(lease_until), iso_utc(now), row["command_id"]),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_commands WHERE command_id=?",
                    (row["command_id"],),
                ).fetchone()
            )

    def finish_sync_command(
        self,
        command_id: str,
        *,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        now = self._now()
        state = "retryable_error" if retry_after_seconds is not None else "terminal_error" if error_code else "succeeded"
        available_at = now + timedelta(seconds=max(1, int(retry_after_seconds or 0)))
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_commands
                SET state=?, result_json=?, error_code=?, available_at=?,
                    lease_owner=NULL, lease_until=NULL, updated_at=? WHERE command_id=?
                """,
                (
                    state,
                    canonical_json(dict(result or {})),
                    _clean_text(error_code) or None,
                    iso_utc(available_at),
                    iso_utc(now),
                    _clean_text(command_id),
                ),
            )

    def enqueue_processing(
        self,
        feedback_id: str,
        *,
        content_version: int | None = None,
        trigger_source: str,
        actor_id: str,
        allow_history: bool = False,
    ) -> dict[str, Any]:
        settings = self.assert_effective_on(operation="AI enqueue")
        now = self._now()
        with self.transaction() as conn:
            feedback = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
                (_clean_text(feedback_id),),
            ).fetchone()
            if feedback is None:
                raise AutoanswersRuntimeError("feedback not found", code="feedback_not_found")
            version = int(content_version or feedback["content_version"])
            if version != int(feedback["content_version"]):
                raise AutoanswersRuntimeError("stale feedback version", code="stale_content_version")
            if feedback["answer_text"]:
                raise AutoanswersRuntimeError("WB already has an answer", code="external_answer_present")
            if not allow_history and feedback["auto_eligible_epoch"] != settings.enable_epoch:
                raise AutoanswersRuntimeError(
                    "feedback is outside the current automatic enable epoch",
                    code="historical_backlog_requires_preview",
                )
            key = processing_key(str(feedback["feedback_id"]), version)
            existing = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (key,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_wb_autoanswer_jobs(
                        processing_key, feedback_id, content_version,
                        content_version_hash, state, trigger_source,
                        bundle_version, evaluation_signature, policy_version,
                        enable_epoch, available_at, attempts, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)
                    """,
                    (
                        key,
                        feedback["feedback_id"],
                        version,
                        feedback["content_version_hash"],
                        STATE_QUEUED,
                        _clean_text(trigger_source),
                        PROMPT_BUNDLE_VERSION,
                        EVALUATION_SIGNATURE,
                        settings.policy_version,
                        settings.enable_epoch,
                        iso_utc(now),
                        iso_utc(now),
                        iso_utc(now),
                    ),
                )
                self._audit(
                    conn,
                    aggregate_type="processing_job",
                    aggregate_id=key,
                    event_type="processing_enqueued",
                    actor_type="user" if allow_history else "sync",
                    actor_id=_clean_text(actor_id),
                    details={"trigger_source": trigger_source, "allow_history": allow_history},
                    at=now,
                    previous_state=STATE_SYNCED,
                    next_state=STATE_QUEUED,
                )
                existing = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                    (key,),
                ).fetchone()
            return dict(existing)

    def enqueue_manual_processing(
        self,
        feedback_id: str,
        *,
        content_version: int,
        actor_id: str,
    ) -> dict[str, Any]:
        """Idempotently queue one exact current review version after an explicit click."""

        settings = self.assert_effective_on(operation="manual AI enqueue")
        if settings.mode != MODE_MANUAL:
            raise AutoanswersRuntimeError(
                "manual generation is available only in manual mode",
                code="manual_mode_required",
            )
        return self.enqueue_processing(
            feedback_id,
            content_version=content_version,
            trigger_source="manual_generate",
            actor_id=actor_id,
            allow_history=True,
        )

    @staticmethod
    def _period_bounds(now: datetime) -> tuple[str, str]:
        day = now.astimezone(timezone.utc).date().isoformat()
        month = day[:7]
        return day, month

    def budget_status(self) -> dict[str, Any]:
        settings = self.settings()
        now = self._now()
        day, month = self._period_bounds(now)
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN substr(created_at,1,10)=? THEN actual_cost_usd ELSE 0 END),0) AS daily_actual,
                    COALESCE(SUM(CASE WHEN substr(created_at,1,7)=? THEN actual_cost_usd ELSE 0 END),0) AS monthly_actual,
                    COALESCE(SUM(CASE WHEN status='reserved' AND substr(created_at,1,10)=? THEN reserved_usd ELSE 0 END),0) AS daily_reserved,
                    COALESCE(SUM(CASE WHEN status='reserved' AND substr(created_at,1,7)=? THEN reserved_usd ELSE 0 END),0) AS monthly_reserved
                FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
                """,
                (day, month, day, month),
            ).fetchone()
        daily = _money(row["daily_actual"]) + _money(row["daily_reserved"])
        monthly = _money(row["monthly_actual"]) + _money(row["monthly_reserved"])
        daily_cap = _money(settings.daily_cap_usd)
        monthly_cap = _money(settings.monthly_cap_usd)
        ratio = Decimal(str(settings.warning_ratio))
        return {
            "daily_used_and_reserved_usd": float(daily),
            "monthly_used_and_reserved_usd": float(monthly),
            "daily_cap_usd": float(daily_cap),
            "monthly_cap_usd": float(monthly_cap),
            "warning_ratio": float(ratio),
            "warning": daily >= daily_cap * ratio or monthly >= monthly_cap * ratio,
            "hard_cap_reached": daily >= daily_cap or monthly >= monthly_cap,
        }

    def _reserve_budget(self, conn: sqlite3.Connection, *, key: str, settings: AutoanswersSettings, at: datetime) -> None:
        existing = conn.execute(
            "SELECT status FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations WHERE processing_key=?",
            (key,),
        ).fetchone()
        if existing is not None:
            return
        day, month = self._period_bounds(at)
        totals = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN substr(created_at,1,10)=? THEN actual_cost_usd + CASE WHEN status='reserved' THEN reserved_usd ELSE 0 END ELSE 0 END),0) AS daily_total,
                COALESCE(SUM(CASE WHEN substr(created_at,1,7)=? THEN actual_cost_usd + CASE WHEN status='reserved' THEN reserved_usd ELSE 0 END ELSE 0 END),0) AS monthly_total
            FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
            """,
            (day, month),
        ).fetchone()
        reservation = _money(settings.max_reservation_per_review_usd)
        if _money(totals["daily_total"]) + reservation > _money(settings.daily_cap_usd):
            raise AutoanswersRuntimeError("daily OpenAI budget hard cap", code="daily_budget_cap")
        if _money(totals["monthly_total"]) + reservation > _money(settings.monthly_cap_usd):
            raise AutoanswersRuntimeError("monthly OpenAI budget hard cap", code="monthly_budget_cap")
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_wb_autoanswers_budget_reservations(
                processing_key, reserved_usd, actual_cost_usd, status, created_at, updated_at
            ) VALUES(?,?,0,'reserved',?,?)
            """,
            (key, str(reservation), iso_utc(at), iso_utc(at)),
        )

    def claim_processing_job(self, *, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any] | None:
        settings = self.assert_effective_on(operation="AI processing")
        now = self._now()
        lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT j.* FROM sheet_vitrina_v1_wb_autoanswer_jobs j
                WHERE (
                    (j.state=? AND j.available_at<=?) OR
                    (j.state=? AND j.lease_until IS NOT NULL AND j.lease_until<=?) OR
                    (j.state=? AND j.retry_stage='processing' AND j.available_at<=?)
                )
                  AND (? <> ? OR j.trigger_source='manual_generate')
                ORDER BY j.created_at, j.processing_key
                LIMIT 1
                """,
                (
                    STATE_QUEUED,
                    iso_utc(now),
                    STATE_PROCESSING,
                    iso_utc(now),
                    STATE_RETRYABLE_ERROR,
                    iso_utc(now),
                    settings.mode,
                    MODE_MANUAL,
                ),
            ).fetchone()
            if row is None:
                return None
            if int(row["enable_epoch"]) != settings.enable_epoch:
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                    SET state=?, last_error_code='enable_epoch_stale', updated_at=?
                    WHERE processing_key=?
                    """,
                    (STATE_NEEDS_REVIEW, iso_utc(now), row["processing_key"]),
                )
                return None
            feedback = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
                (row["feedback_id"],),
            ).fetchone()
            if feedback is None or int(feedback["content_version"]) != int(row["content_version"]):
                conn.execute(
                    "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state=?, last_error_code='stale_content_version', updated_at=? WHERE processing_key=?",
                    (STATE_NEEDS_REVIEW, iso_utc(now), row["processing_key"]),
                )
                return None
            if feedback["answer_text"]:
                conn.execute(
                    "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state=?, last_error_code='external_answer_present', updated_at=? WHERE processing_key=?",
                    (STATE_NEEDS_REVIEW, iso_utc(now), row["processing_key"]),
                )
                return None
            self._reserve_budget(conn, key=str(row["processing_key"]), settings=settings, at=now)
            previous = str(row["state"])
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state=?, lease_owner=?, lease_until=?, attempts=attempts+1,
                    started_at=COALESCE(started_at,?), updated_at=?, retry_stage=NULL
                WHERE processing_key=?
                """,
                (
                    STATE_PROCESSING,
                    _clean_text(worker_id),
                    iso_utc(lease_until),
                    iso_utc(now),
                    iso_utc(now),
                    row["processing_key"],
                ),
            )
            self._audit(
                conn,
                aggregate_type="processing_job",
                aggregate_id=str(row["processing_key"]),
                event_type="processing_claimed",
                actor_type="worker",
                actor_id=_clean_text(worker_id),
                details={"lease_until": iso_utc(lease_until)},
                at=now,
                previous_state=previous,
                next_state=STATE_PROCESSING,
            )
            claimed = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (row["processing_key"],),
            ).fetchone()
            return dict(claimed)

    def record_processing_retry(
        self,
        processing_key_value: str,
        *,
        error_code: str,
        retry_after_seconds: int,
        worker_id: str,
    ) -> None:
        now = self._now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (processing_key_value,),
            ).fetchone()
            if row is None:
                raise AutoanswersRuntimeError("processing job not found", code="job_not_found")
            assert_transition(str(row["state"]), STATE_RETRYABLE_ERROR)
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state=?, retry_stage='processing', available_at=?, lease_owner=NULL,
                    lease_until=NULL, last_error_code=?, updated_at=?
                WHERE processing_key=?
                """,
                (
                    STATE_RETRYABLE_ERROR,
                    iso_utc(now + timedelta(seconds=max(1, int(retry_after_seconds)))),
                    _clean_text(error_code),
                    iso_utc(now),
                    processing_key_value,
                ),
            )
            self._audit(
                conn,
                aggregate_type="processing_job",
                aggregate_id=processing_key_value,
                event_type="processing_retry_scheduled",
                actor_type="worker",
                actor_id=_clean_text(worker_id),
                details={"error_code": error_code, "retry_after_seconds": retry_after_seconds},
                at=now,
                previous_state=str(row["state"]),
                next_state=STATE_RETRYABLE_ERROR,
            )

    def settle_budget(self, processing_key_value: str, *, actual_cost_usd: Any) -> None:
        actual = _money(actual_cost_usd)
        if actual < 0:
            raise ValueError("actual cost cannot be negative")
        now = self._now()
        with self.transaction() as conn:
            reservation = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations WHERE processing_key=?",
                (processing_key_value,),
            ).fetchone()
            if reservation is None:
                raise AutoanswersRuntimeError("budget reservation missing", code="reservation_missing")
            if reservation["status"] == "settled" and _money(reservation["actual_cost_usd"]) != actual:
                raise AutoanswersRuntimeError("settled cost is immutable", code="cost_conflict")
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations
                SET actual_cost_usd=?, reserved_usd=0, status='settled', updated_at=?
                WHERE processing_key=?
                """,
                (str(actual), iso_utc(now), processing_key_value),
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET actual_cost_usd=?, updated_at=? WHERE processing_key=?",
                (str(actual), iso_utc(now), processing_key_value),
            )

    def complete_skip(self, processing_key_value: str, *, reason: str, worker_id: str) -> dict[str, Any]:
        now = self._now()
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (processing_key_value,),
            ).fetchone()
            if job is None:
                raise AutoanswersRuntimeError("processing job not found", code="job_not_found")
            assert_transition(str(job["state"]), STATE_SKIPPED)
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state=?, last_error_code=?, lease_owner=NULL, lease_until=NULL,
                    completed_at=?, updated_at=? WHERE processing_key=?
                """,
                (STATE_SKIPPED, _clean_text(reason), iso_utc(now), iso_utc(now), processing_key_value),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations
                SET reserved_usd=0, actual_cost_usd=0, status='settled', updated_at=?
                WHERE processing_key=?
                """,
                (iso_utc(now), processing_key_value),
            )
            self._audit(
                conn,
                aggregate_type="processing_job",
                aggregate_id=processing_key_value,
                event_type="prefilter_skipped",
                actor_type="worker",
                actor_id=_clean_text(worker_id),
                details={"reason": reason},
                at=now,
                previous_state=str(job["state"]),
                next_state=STATE_SKIPPED,
            )
            return dict(
                conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                    (processing_key_value,),
                ).fetchone()
            )

    def append_node_audit(self, processing_key_value: str, events: Sequence[Mapping[str, Any]]) -> None:
        now = self._now()
        with self.transaction() as conn:
            for index, event in enumerate(events):
                self._audit(
                    conn,
                    aggregate_type="processing_job",
                    aggregate_id=processing_key_value,
                    event_type="frozen_node_event",
                    actor_type="node_pipeline",
                    actor_id=PROMPT_BUNDLE_VERSION,
                    details={"index": index, "event": dict(event)},
                    at=now,
                )

    def complete_generation(
        self,
        processing_key_value: str,
        *,
        result: Mapping[str, Any],
        worker_id: str,
    ) -> dict[str, Any]:
        settings = self.settings()
        reply = str(result.get("final_reply") or "").strip()
        route = _clean_text(result.get("final_route"))
        if not reply or not route:
            raise AutoanswersRuntimeError("AI result misses final_reply/final_route", code="invalid_ai_result")
        hard_gates_passed = bool(result.get("hard_gates_passed"))
        fallback_used = bool(result.get("fallback_used"))
        media_uncertain = bool(result.get("media_uncertain"))
        node_contract_valid = bool(result.get("node_contract_valid"))
        now = self._now()
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (processing_key_value,),
            ).fetchone()
            if job is None:
                raise AutoanswersRuntimeError("processing job not found", code="job_not_found")
            if str(job["state"]) != STATE_PROCESSING:
                raise AutoanswersRuntimeError("processing job is not claimed", code="job_not_processing")
            feedback = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
                (job["feedback_id"],),
            ).fetchone()
            stale = feedback is None or int(feedback["content_version"]) != int(job["content_version"])
            external_answer = bool(feedback and feedback["answer_text"])
            result_json = canonical_json(dict(result))
            reply_sha = final_reply_hash(reply)
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state=?, final_route=?, case_code=?, final_reply=?, final_reply_sha256=?,
                    result_json=?, hard_gates_passed=?, fallback_used=?,
                    media_uncertain=?, node_contract_valid=?, lease_owner=NULL,
                    lease_until=NULL, completed_at=?, updated_at=?
                WHERE processing_key=?
                """,
                (
                    STATE_GENERATED,
                    route,
                    _clean_text(result.get("case_code")) or None,
                    reply,
                    reply_sha,
                    result_json,
                    int(hard_gates_passed),
                    int(fallback_used),
                    int(media_uncertain),
                    int(node_contract_valid),
                    iso_utc(now),
                    iso_utc(now),
                    processing_key_value,
                ),
            )
            self._audit(
                conn,
                aggregate_type="processing_job",
                aggregate_id=processing_key_value,
                event_type="generation_completed",
                actor_type="worker",
                actor_id=_clean_text(worker_id),
                details={"route": route, "reply_sha256": reply_sha},
                at=now,
                previous_state=STATE_PROCESSING,
                next_state=STATE_GENERATED,
            )
            review_reasons: list[str] = []
            if not settings.effective_enabled:
                review_reasons.append(
                    "emergency_force_off" if settings.force_off else "master_switched_off_during_processing"
                )
            if stale:
                review_reasons.append("stale_content_version")
            if external_answer:
                review_reasons.append("external_answer_present")
            if not node_contract_valid:
                review_reasons.append("node_contract_invalid")
            if not hard_gates_passed:
                review_reasons.append("hard_gate_failed")
            if fallback_used:
                review_reasons.append("fallback_used")
            if media_uncertain:
                review_reasons.append("media_uncertain")
            if route == "seller_chat":
                review_reasons.append("seller_chat_review_only")

            next_state = STATE_GENERATED
            if review_reasons:
                next_state = STATE_NEEDS_REVIEW
            elif settings.mode in {MODE_MANUAL, MODE_DRAFT_ONLY}:
                next_state = STATE_GENERATED
            elif settings.mode == MODE_AUTO_SAFE:
                next_state = STATE_APPROVED if route in AUTO_SAFE_ROUTES else STATE_NEEDS_REVIEW
                if next_state == STATE_NEEDS_REVIEW:
                    review_reasons.append("route_not_in_auto_safe_allowlist")
            elif settings.mode == MODE_AUTO_ALL:
                next_state = STATE_APPROVED
            if next_state != STATE_GENERATED:
                assert_transition(STATE_GENERATED, next_state)
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                    SET state=?, review_reasons_json=?, updated_at=?
                    WHERE processing_key=?
                    """,
                    (next_state, canonical_json(review_reasons), iso_utc(now), processing_key_value),
                )
                self._audit(
                    conn,
                    aggregate_type="processing_job",
                    aggregate_id=processing_key_value,
                    event_type="publication_policy_decided",
                    actor_type="policy",
                    actor_id=settings.policy_version,
                    details={"mode": settings.mode, "review_reasons": review_reasons},
                    at=now,
                    previous_state=STATE_GENERATED,
                    next_state=next_state,
                )
            if next_state == STATE_APPROVED:
                self._create_publication_job(
                    conn,
                    job=job,
                    reply=reply,
                    reply_sha=reply_sha,
                    request_source="automatic",
                    requested_by=None,
                    mode_at_enqueue=settings.mode,
                    manual_edit_revision=None,
                    at=now,
                )
            stored = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (processing_key_value,),
            ).fetchone()
            return dict(stored)

    def record_processing_terminal(
        self,
        processing_key_value: str,
        *,
        error_code: str,
        worker_id: str,
    ) -> None:
        now = self._now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (processing_key_value,),
            ).fetchone()
            if row is None:
                raise AutoanswersRuntimeError("processing job not found", code="job_not_found")
            assert_transition(str(row["state"]), STATE_TERMINAL_ERROR)
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state=?, lease_owner=NULL, lease_until=NULL, last_error_code=?,
                    completed_at=?, updated_at=? WHERE processing_key=?
                """,
                (STATE_TERMINAL_ERROR, _clean_text(error_code), iso_utc(now), iso_utc(now), processing_key_value),
            )
            reservation = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations WHERE processing_key=?",
                (processing_key_value,),
            ).fetchone()
            if reservation and reservation["status"] == "reserved":
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations
                    SET actual_cost_usd=reserved_usd, reserved_usd=0, status='settled', updated_at=?
                    WHERE processing_key=?
                    """,
                    (iso_utc(now), processing_key_value),
                )
            self._audit(
                conn,
                aggregate_type="processing_job",
                aggregate_id=processing_key_value,
                event_type="processing_terminal_error",
                actor_type="worker",
                actor_id=_clean_text(worker_id),
                details={"error_code": error_code},
                at=now,
                previous_state=str(row["state"]),
                next_state=STATE_TERMINAL_ERROR,
            )

    def _create_publication_job(
        self,
        conn: sqlite3.Connection,
        *,
        job: Mapping[str, Any],
        reply: str,
        reply_sha: str,
        request_source: str,
        requested_by: str | None,
        mode_at_enqueue: str,
        manual_edit_revision: int | None,
        at: datetime,
    ) -> str:
        pub_key = publication_key(str(job["feedback_id"]), int(job["content_version"]), reply_sha)
        conn.execute(
            """
            INSERT OR IGNORE INTO sheet_vitrina_v1_wb_publication_jobs(
                publication_key, processing_key, feedback_id, content_version,
                content_version_hash, exact_reply, normalized_reply_sha256,
                state, available_at, attempts, request_source, requested_by,
                mode_at_enqueue, manual_edit_revision, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)
            """,
            (
                pub_key,
                job["processing_key"],
                job["feedback_id"],
                job["content_version"],
                job["content_version_hash"],
                reply,
                reply_sha,
                STATE_APPROVED,
                iso_utc(at),
                _clean_text(request_source),
                _clean_text(requested_by) or None,
                mode_at_enqueue,
                manual_edit_revision,
                iso_utc(at),
                iso_utc(at),
            ),
        )
        stored = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_wb_publication_jobs WHERE feedback_id=? AND content_version=?",
            (job["feedback_id"], job["content_version"]),
        ).fetchone()
        if stored is None:
            raise AutoanswersRuntimeError("publication job was not persisted", code="publication_persist_failed")
        if str(stored["publication_key"]) != pub_key:
            raise AutoanswersRuntimeError(
                "a publication already exists for this feedback version",
                code="publication_already_exists",
            )
        return pub_key

    def manual_guard_context(self, processing_key_value: str) -> dict[str, Any]:
        settings = self.assert_effective_on(operation="manual reply validation")
        if settings.mode != MODE_MANUAL:
            raise AutoanswersRuntimeError("manual mode is required", code="manual_mode_required")
        with closing(self._connect()) as conn:
            job = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (_clean_text(processing_key_value),),
            ).fetchone()
            if job is None:
                raise AutoanswersRuntimeError("processing job not found", code="job_not_found")
            feedback = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
                (job["feedback_id"],),
            ).fetchone()
        if str(job["state"]) not in {STATE_GENERATED, STATE_NEEDS_REVIEW}:
            raise AutoanswersRuntimeError("job is not ready for review", code="invalid_approval_state")
        if feedback is None or int(feedback["content_version"]) != int(job["content_version"]):
            raise AutoanswersRuntimeError("stale feedback version", code="stale_content_version")
        if str(feedback["content_version_hash"]) != str(job["content_version_hash"]):
            raise AutoanswersRuntimeError("stale feedback hash", code="stale_content_hash")
        if feedback["answer_text"]:
            raise AutoanswersRuntimeError("WB already has an answer", code="external_answer_present")
        if not bool(job["node_contract_valid"]) or not bool(job["hard_gates_passed"]):
            raise AutoanswersRuntimeError("hard gates did not pass", code="hard_gate_failed")
        if bool(job["fallback_used"]) or bool(job["media_uncertain"]):
            raise AutoanswersRuntimeError("uncertain result cannot be published", code="uncertain_result")
        result = json.loads(str(job["result_json"] or "{}"))
        pipeline = result.get("pipeline_result") if isinstance(result.get("pipeline_result"), Mapping) else {}
        return {
            "processing_key": str(job["processing_key"]),
            "feedback_id": str(job["feedback_id"]),
            "content_version": int(job["content_version"]),
            "route": str(job["final_route"] or ""),
            "case_code": str(job["case_code"]) if job["case_code"] else None,
            "primary_issue": str(pipeline.get("primary_issue")) if pipeline.get("primary_issue") else None,
        }

    def save_manual_reply_review(
        self,
        processing_key_value: str,
        *,
        reply: str,
        guard_passed: bool,
        guard_errors: Sequence[str],
        actor_id: str,
    ) -> dict[str, Any]:
        context = self.manual_guard_context(processing_key_value)
        exact_reply = str(reply or "").strip()
        if not exact_reply:
            raise ValueError("reply is required")
        now = self._now()
        digest = final_reply_hash(exact_reply)
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (processing_key_value,),
            ).fetchone()
            if job is None:
                raise AutoanswersRuntimeError("processing job not found", code="job_not_found")
            if conn.execute(
                "SELECT 1 FROM sheet_vitrina_v1_wb_publication_jobs WHERE feedback_id=? AND content_version=?",
                (job["feedback_id"], job["content_version"]),
            ).fetchone():
                raise AutoanswersRuntimeError("publication is already queued", code="publication_already_exists")
            previous = str(job["state"])
            next_state = previous
            if not guard_passed and previous == STATE_GENERATED:
                next_state = STATE_NEEDS_REVIEW
            revision = int(job["manual_edit_revision"] or 0) + 1
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state=?, manual_reply=?, manual_reply_sha256=?, manual_guard_passed=?,
                    manual_guard_errors_json=?, manual_reviewed_by=?, manual_reviewed_at=?,
                    manual_edit_revision=?, updated_at=?
                WHERE processing_key=?
                """,
                (
                    next_state,
                    exact_reply,
                    digest,
                    int(bool(guard_passed)),
                    canonical_json([_clean_text(item) for item in guard_errors]),
                    _clean_text(actor_id),
                    iso_utc(now),
                    revision,
                    iso_utc(now),
                    processing_key_value,
                ),
            )
            self._audit(
                conn,
                aggregate_type="processing_job",
                aggregate_id=processing_key_value,
                event_type="manual_reply_guarded",
                actor_type="user",
                actor_id=_clean_text(actor_id),
                details={
                    "reply_sha256": digest,
                    "guard_passed": bool(guard_passed),
                    "guard_errors": list(guard_errors),
                    "manual_edit_revision": revision,
                    "route": context["route"],
                },
                at=now,
                previous_state=previous,
                next_state=next_state,
            )
            stored = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (processing_key_value,),
            ).fetchone()
        return dict(stored)

    def approve_for_publication(
        self,
        processing_key_value: str,
        *,
        actor_id: str,
        confirmed: bool = False,
        expected_reply_sha256: str | None = None,
    ) -> dict[str, Any]:
        settings = self.assert_effective_on(operation="manual publication approval")
        if not confirmed:
            raise AutoanswersRuntimeError("explicit publication confirmation is required", code="confirmation_required")
        now = self._now()
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (processing_key_value,),
            ).fetchone()
            if job is None:
                raise AutoanswersRuntimeError("processing job not found", code="job_not_found")
            if str(job["state"]) not in {STATE_GENERATED, STATE_NEEDS_REVIEW}:
                raise AutoanswersRuntimeError("job cannot be approved", code="invalid_approval_state")
            feedback = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
                (job["feedback_id"],),
            ).fetchone()
            if feedback is None or int(feedback["content_version"]) != int(job["content_version"]):
                raise AutoanswersRuntimeError("stale feedback version", code="stale_content_version")
            if feedback["answer_text"]:
                raise AutoanswersRuntimeError("WB already has an answer", code="external_answer_present")
            if not bool(job["node_contract_valid"]) or not bool(job["hard_gates_passed"]):
                raise AutoanswersRuntimeError("hard gates did not pass", code="hard_gate_failed")
            if bool(job["fallback_used"]) or bool(job["media_uncertain"]):
                raise AutoanswersRuntimeError("uncertain result cannot be published", code="uncertain_result")
            is_manual_mode = settings.mode == MODE_MANUAL
            reply = str((job["manual_reply"] if is_manual_mode else job["final_reply"]) or "")
            reply_sha = str((job["manual_reply_sha256"] if is_manual_mode else job["final_reply_sha256"]) or "")
            if is_manual_mode:
                if not bool(job["manual_guard_passed"]) or not job["manual_reviewed_by"]:
                    raise AutoanswersRuntimeError("manual reply has not passed final guards", code="manual_guard_required")
                if not expected_reply_sha256 or str(expected_reply_sha256) != reply_sha:
                    raise AutoanswersRuntimeError("reviewed reply hash changed", code="manual_reply_hash_mismatch")
            if not reply or final_reply_hash(reply) != reply_sha:
                raise AutoanswersRuntimeError("reviewed reply hash is invalid", code="publication_reply_hash_mismatch")
            previous = str(job["state"])
            assert_transition(previous, STATE_APPROVED)
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state=?, approved_by=?, approved_at=?, updated_at=? WHERE processing_key=?",
                (STATE_APPROVED, _clean_text(actor_id), iso_utc(now), iso_utc(now), processing_key_value),
            )
            pub_key = self._create_publication_job(
                conn,
                job=job,
                reply=reply,
                reply_sha=reply_sha,
                request_source="manual" if is_manual_mode else "review_approval",
                requested_by=_clean_text(actor_id),
                mode_at_enqueue=settings.mode,
                manual_edit_revision=int(job["manual_edit_revision"] or 0) if is_manual_mode else None,
                at=now,
            )
            self._audit(
                conn,
                aggregate_type="processing_job",
                aggregate_id=processing_key_value,
                event_type="manually_approved",
                actor_type="user",
                actor_id=_clean_text(actor_id),
                details={"publication_key": pub_key},
                at=now,
                previous_state=previous,
                next_state=STATE_APPROVED,
            )
            return dict(
                conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_wb_publication_jobs WHERE publication_key=?",
                    (pub_key,),
                ).fetchone()
            )

    def claim_publication_job(self, *, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any] | None:
        """Allow mandatory GET readback while OFF, but never claim a new write."""

        now = self._now()
        lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_wb_publication_jobs
                WHERE (
                    (state=? AND available_at<=?) OR
                    (state=? AND retry_stage='readback' AND available_at<=?) OR
                    (state=? AND write_started_at IS NOT NULL AND lease_until<=?)
                )
                ORDER BY created_at, publication_key LIMIT 1
                """,
                (
                    STATE_PUBLISH_PENDING_READBACK,
                    iso_utc(now),
                    STATE_RETRYABLE_ERROR,
                    iso_utc(now),
                    STATE_PUBLISHING,
                    iso_utc(now),
                ),
            ).fetchone()
            action = "readback"
            if row is None:
                settings_row = conn.execute(
                    "SELECT master_enabled FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
                ).fetchone()
                if not settings_row or not bool(settings_row["master_enabled"]) or _force_off_from_env(self.env):
                    return None
                row = conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_wb_publication_jobs
                    WHERE (
                        (state=? AND available_at<=?) OR
                        (state=? AND write_started_at IS NULL AND lease_until<=?)
                    )
                    ORDER BY created_at, publication_key LIMIT 1
                    """,
                    (STATE_APPROVED, iso_utc(now), STATE_PUBLISHING, iso_utc(now)),
                ).fetchone()
                action = "write"
            if row is None:
                return None
            if action == "write":
                try:
                    self._assert_publication_invariants(conn, row)
                except AutoanswersRuntimeError as exc:
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_wb_publication_jobs
                        SET state=?, last_error_code=?, lease_owner=NULL, lease_until=NULL, updated_at=?
                        WHERE publication_key=?
                        """,
                        (STATE_NEEDS_REVIEW, exc.code, iso_utc(now), row["publication_key"]),
                    )
                    conn.execute(
                        "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state=?, last_error_code=?, updated_at=? WHERE processing_key=?",
                        (STATE_NEEDS_REVIEW, exc.code, iso_utc(now), row["processing_key"]),
                    )
                    self._audit(
                        conn,
                        aggregate_type="publication_job",
                        aggregate_id=str(row["publication_key"]),
                        event_type="publication_quarantined",
                        actor_type="worker",
                        actor_id=_clean_text(worker_id),
                        details={"reason": exc.code},
                        at=now,
                        previous_state=str(row["state"]),
                        next_state=STATE_NEEDS_REVIEW,
                    )
                    return None
                previous = str(row["state"])
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_publication_jobs
                    SET state=?, lease_owner=?, lease_until=?, updated_at=?
                    WHERE publication_key=?
                    """,
                    (STATE_PUBLISHING, _clean_text(worker_id), iso_utc(lease_until), iso_utc(now), row["publication_key"]),
                )
                conn.execute(
                    "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state=?, updated_at=? WHERE processing_key=?",
                    (STATE_PUBLISHING, iso_utc(now), row["processing_key"]),
                )
                self._audit(
                    conn,
                    aggregate_type="publication_job",
                    aggregate_id=str(row["publication_key"]),
                    event_type="publication_claimed",
                    actor_type="worker",
                    actor_id=_clean_text(worker_id),
                    details={"lease_until": iso_utc(lease_until)},
                    at=now,
                    previous_state=previous,
                    next_state=STATE_PUBLISHING,
                )
            else:
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_publication_jobs
                    SET state=?, lease_owner=?, lease_until=?, updated_at=?
                    WHERE publication_key=?
                    """,
                    (
                        STATE_PUBLISH_PENDING_READBACK,
                        _clean_text(worker_id),
                        iso_utc(lease_until),
                        iso_utc(now),
                        row["publication_key"],
                    ),
                )
                conn.execute(
                    "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state=?, updated_at=? WHERE processing_key=?",
                    (STATE_PUBLISH_PENDING_READBACK, iso_utc(now), row["processing_key"]),
                )
            claimed = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_publication_jobs WHERE publication_key=?",
                (row["publication_key"],),
            ).fetchone()
            return {**dict(claimed), "action": action}

    def _actor_has_ai_review_permission(self, conn: sqlite3.Connection, actor_id: str) -> bool:
        actor = _clean_text(actor_id)
        if not actor:
            return False
        source = self.env if self.env is not None else os.environ
        configured_admin = _clean_text(source.get("WB_CORE_WEB_AUTH_USERNAME"))
        if configured_admin and actor.casefold() == configured_admin.casefold():
            return True
        auth_enabled = bool(_clean_text(source.get("WB_CORE_WEB_AUTH_SESSION_SECRET")))
        if actor == "local_operator" and not auth_enabled:
            return True
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_users'"
        ).fetchone()
        if table_exists is None:
            return False
        row = conn.execute(
            """
            SELECT is_active, allowed_sections_json
            FROM sheet_vitrina_v1_users
            WHERE lower(username)=lower(?)
            LIMIT 1
            """,
            (actor,),
        ).fetchone()
        if row is None or not bool(row["is_active"]):
            return False
        try:
            sections = json.loads(str(row["allowed_sections_json"] or "[]"))
        except json.JSONDecodeError:
            return False
        return isinstance(sections, list) and "feedbacks.ai_review" in sections

    def _assert_publication_invariants(self, conn: sqlite3.Connection, publication: Mapping[str, Any]) -> None:
        processing = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
            (publication["processing_key"],),
        ).fetchone()
        feedback = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
            (publication["feedback_id"],),
        ).fetchone()
        if processing is None or feedback is None:
            raise AutoanswersRuntimeError("publication aggregate is incomplete", code="publication_incomplete")
        if int(feedback["content_version"]) != int(publication["content_version"]):
            raise AutoanswersRuntimeError("stale feedback version", code="stale_content_version")
        if str(feedback["content_version_hash"]) != str(publication["content_version_hash"]):
            raise AutoanswersRuntimeError("stale feedback hash", code="stale_content_hash")
        if feedback["answer_text"]:
            raise AutoanswersRuntimeError("WB already has an answer", code="external_answer_present")
        if final_reply_hash(publication["exact_reply"]) != str(publication["normalized_reply_sha256"]):
            raise AutoanswersRuntimeError("publication reply hash mismatch", code="publication_reply_hash_mismatch")
        if not bool(processing["node_contract_valid"]) or not bool(processing["hard_gates_passed"]):
            raise AutoanswersRuntimeError("publication hard gates failed", code="hard_gate_failed")
        if bool(processing["fallback_used"]) or bool(processing["media_uncertain"]):
            raise AutoanswersRuntimeError("uncertain result cannot be published", code="uncertain_result")
        request_source = str(publication["request_source"] or "automatic")
        if request_source in {"manual", "review_approval"}:
            actor_id = str(publication["requested_by"] or "")
            if not self._actor_has_ai_review_permission(conn, actor_id):
                raise AutoanswersRuntimeError(
                    "publication requester no longer has feedbacks.ai_review",
                    code="review_permission_revoked",
                )
        if request_source == "manual":
            settings = conn.execute(
                "SELECT master_enabled, mode FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
            ).fetchone()
            if settings is None or not bool(settings["master_enabled"]) or str(settings["mode"]) != MODE_MANUAL:
                raise AutoanswersRuntimeError("manual publication requires current manual mode", code="manual_mode_required")
            if str(publication["mode_at_enqueue"] or "") != MODE_MANUAL:
                raise AutoanswersRuntimeError("manual publication mode evidence is invalid", code="manual_mode_evidence_invalid")
            if not bool(processing["manual_guard_passed"]) or not processing["manual_reviewed_by"]:
                raise AutoanswersRuntimeError("manual final guard evidence is missing", code="manual_guard_required")
            if int(publication["manual_edit_revision"] or 0) != int(processing["manual_edit_revision"] or 0):
                raise AutoanswersRuntimeError("manual reply revision is stale", code="manual_reply_revision_stale")
            if str(processing["manual_reply_sha256"] or "") != str(publication["normalized_reply_sha256"]):
                raise AutoanswersRuntimeError("manual reply hash is stale", code="manual_reply_hash_mismatch")
        if str(processing["final_route"] or "") == "seller_chat":
            reply = str(publication["exact_reply"] or "")
            case_code = str(processing["case_code"] or "")
            if not case_code or reply.count(case_code) != 1:
                raise AutoanswersRuntimeError("seller_chat case code is invalid", code="seller_chat_case_code_invalid")
            if re.search(r"(?:фото|видео|скриншот|этикет|доказатель|материал)", reply, flags=re.IGNORECASE):
                raise AutoanswersRuntimeError(
                    "seller_chat public reply mentions prohibited materials",
                    code="seller_chat_materials_prohibited",
                )

    def begin_publication_write(self, publication_key_value: str, *, worker_id: str) -> dict[str, Any]:
        """Last durable gate immediately before the transport POST."""

        self.assert_effective_on(operation="WB answer write")
        now = self._now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_publication_jobs WHERE publication_key=?",
                (publication_key_value,),
            ).fetchone()
            if row is None:
                raise AutoanswersRuntimeError("publication job not found", code="publication_not_found")
            if str(row["state"]) != STATE_PUBLISHING or row["write_started_at"]:
                raise AutoanswersRuntimeError("publication is not write-ready", code="publication_not_write_ready")
            self._assert_publication_invariants(conn, row)
            attempt_number = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts WHERE publication_key=?",
                    (publication_key_value,),
                ).fetchone()[0]
            ) + 1
            attempt_id = uuid4().hex
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_publication_attempts(
                    attempt_id, publication_key, attempt_number,
                    request_reply_sha256, transport_outcome, write_started_at,
                    details_json
                ) VALUES(?,?,?,?, 'started', ?, '{}')
                """,
                (attempt_id, publication_key_value, attempt_number, row["normalized_reply_sha256"], iso_utc(now)),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_publication_jobs
                SET write_started_at=?, attempts=attempts+1, updated_at=?
                WHERE publication_key=?
                """,
                (iso_utc(now), iso_utc(now), publication_key_value),
            )
            return {**dict(row), "attempt_id": attempt_id, "attempt_number": attempt_number}

    def record_publication_transport(
        self,
        publication_key_value: str,
        *,
        attempt_id: str,
        outcome: str,
        http_status: int | None,
        worker_id: str,
    ) -> None:
        """Every started write goes to readback, including timeout/429/5xx."""

        now = self._now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_publication_jobs WHERE publication_key=?",
                (publication_key_value,),
            ).fetchone()
            if row is None:
                raise AutoanswersRuntimeError("publication job not found", code="publication_not_found")
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_publication_attempts
                SET transport_outcome=?, http_status=?, write_finished_at=?
                WHERE attempt_id=? AND publication_key=?
                """,
                (_clean_text(outcome), http_status, iso_utc(now), attempt_id, publication_key_value),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_publication_jobs
                SET state=?, wb_transport_status=?, lease_owner=NULL, lease_until=NULL,
                    available_at=?, retry_stage='readback', updated_at=?
                WHERE publication_key=?
                """,
                (
                    STATE_PUBLISH_PENDING_READBACK,
                    http_status,
                    iso_utc(now),
                    iso_utc(now),
                    publication_key_value,
                ),
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state=?, updated_at=? WHERE processing_key=?",
                (STATE_PUBLISH_PENDING_READBACK, iso_utc(now), row["processing_key"]),
            )
            self._audit(
                conn,
                aggregate_type="publication_job",
                aggregate_id=publication_key_value,
                event_type="wb_write_transport_finished",
                actor_type="worker",
                actor_id=_clean_text(worker_id),
                details={"attempt_id": attempt_id, "outcome": outcome, "http_status": http_status},
                at=now,
                previous_state=STATE_PUBLISHING,
                next_state=STATE_PUBLISH_PENDING_READBACK,
            )

    def record_publication_readback(
        self,
        publication_key_value: str,
        *,
        answer_text: str | None,
        worker_id: str,
    ) -> dict[str, Any]:
        now = self._now()
        observed = normalized_reply(answer_text)
        observed_hash = final_reply_hash(observed) if observed else None
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_publication_jobs WHERE publication_key=?",
                (publication_key_value,),
            ).fetchone()
            if row is None:
                raise AutoanswersRuntimeError("publication job not found", code="publication_not_found")
            if observed_hash == str(row["normalized_reply_sha256"]):
                state = STATE_PUBLISHED
                error_code = None
                event = "publication_confirmed_by_readback"
            elif observed:
                state = STATE_NEEDS_REVIEW
                error_code = "readback_external_or_different_answer"
                event = "publication_readback_mismatch"
            else:
                state = STATE_NEEDS_REVIEW
                error_code = "readback_answer_missing"
                event = "publication_readback_missing"
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_publication_jobs
                SET state=?, readback_answer=?, readback_hash=?, last_error_code=?,
                    lease_owner=NULL, lease_until=NULL, retry_stage=NULL, updated_at=?
                WHERE publication_key=?
                """,
                (state, observed, observed_hash, error_code, iso_utc(now), publication_key_value),
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state=?, last_error_code=?, updated_at=? WHERE processing_key=?",
                (state, error_code, iso_utc(now), row["processing_key"]),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_publication_attempts
                SET readback_outcome=?, readback_answer_sha256=?
                WHERE publication_key=? AND attempt_number=(
                    SELECT MAX(attempt_number) FROM sheet_vitrina_v1_wb_publication_attempts WHERE publication_key=?
                )
                """,
                (event, observed_hash, publication_key_value, publication_key_value),
            )
            self._audit(
                conn,
                aggregate_type="publication_job",
                aggregate_id=publication_key_value,
                event_type=event,
                actor_type="worker",
                actor_id=_clean_text(worker_id),
                details={"observed_hash": observed_hash, "matches": state == STATE_PUBLISHED},
                at=now,
                previous_state=str(row["state"]),
                next_state=state,
            )
            return dict(
                conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_wb_publication_jobs WHERE publication_key=?",
                    (publication_key_value,),
                ).fetchone()
            )

    def record_publication_readback_retry(
        self,
        publication_key_value: str,
        *,
        error_code: str,
        retry_after_seconds: int,
        worker_id: str,
    ) -> None:
        now = self._now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_publication_jobs WHERE publication_key=?",
                (publication_key_value,),
            ).fetchone()
            if row is None:
                raise AutoanswersRuntimeError("publication job not found", code="publication_not_found")
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_publication_jobs
                SET state=?, retry_stage='readback', last_error_code=?, available_at=?,
                    lease_owner=NULL, lease_until=NULL, updated_at=? WHERE publication_key=?
                """,
                (
                    STATE_RETRYABLE_ERROR,
                    _clean_text(error_code),
                    iso_utc(now + timedelta(seconds=max(1, int(retry_after_seconds)))),
                    iso_utc(now),
                    publication_key_value,
                ),
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET state=?, last_error_code=?, updated_at=? WHERE processing_key=?",
                (STATE_RETRYABLE_ERROR, _clean_text(error_code), iso_utc(now), row["processing_key"]),
            )
            self._audit(
                conn,
                aggregate_type="publication_job",
                aggregate_id=publication_key_value,
                event_type="publication_readback_retry",
                actor_type="worker",
                actor_id=_clean_text(worker_id),
                details={"error_code": error_code, "retry_after_seconds": retry_after_seconds},
                at=now,
                previous_state=str(row["state"]),
                next_state=STATE_RETRYABLE_ERROR,
            )

    def _backlog_candidates(self, conn: sqlite3.Connection) -> list[tuple[str, int]]:
        rows = conn.execute(
            """
            SELECT f.feedback_id, f.content_version
            FROM sheet_vitrina_v1_wb_feedbacks f
            LEFT JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
              ON j.feedback_id=f.feedback_id AND j.content_version=f.content_version
             AND j.bundle_version=?
            WHERE COALESCE(f.answer_text,'')=''
              AND COALESCE(f.created_at_wb, f.first_seen_at)>=?
              AND j.processing_key IS NULL
            ORDER BY COALESCE(f.created_at_wb, f.first_seen_at), f.feedback_id
            """,
            (PROMPT_BUNDLE_VERSION, BACKFILL_FROM_DATE),
        ).fetchall()
        return [(str(row["feedback_id"]), int(row["content_version"])) for row in rows]

    def preview_backlog(self, *, actor_id: str) -> dict[str, Any]:
        settings = self.assert_effective_on(operation="backlog preview")
        if settings.mode == MODE_MANUAL:
            raise AutoanswersRuntimeError(
                "historical backlog is disabled in manual mode",
                code="manual_mode_backlog_disabled",
            )
        now = self._now()
        expires = now + timedelta(seconds=BACKLOG_PREVIEW_TTL_SECONDS)
        with self.transaction() as conn:
            candidates = self._backlog_candidates(conn)
            snapshot = canonical_json(candidates)
            preview_id = uuid4().hex
            estimate = _money(settings.max_reservation_per_review_usd) * len(candidates)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_autoanswers_backlog_previews(
                    preview_id, snapshot_sha256, candidates_json, candidate_count,
                    max_estimated_cost_usd, enable_epoch, created_by, created_at, expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    preview_id,
                    sha256_text(snapshot),
                    snapshot,
                    len(candidates),
                    str(estimate),
                    settings.enable_epoch,
                    _clean_text(actor_id),
                    iso_utc(now),
                    iso_utc(expires),
                ),
            )
            self._audit(
                conn,
                aggregate_type="backlog_preview",
                aggregate_id=preview_id,
                event_type="backlog_preview_created",
                actor_type="user",
                actor_id=_clean_text(actor_id),
                details={"count": len(candidates), "max_estimated_cost_usd": str(estimate)},
                at=now,
            )
        return {
            "preview_id": preview_id,
            "count": len(candidates),
            "max_estimated_cost_usd": float(estimate),
            "expires_at": iso_utc(expires),
        }

    def enqueue_backlog_from_preview(self, preview_id: str, *, actor_id: str) -> dict[str, Any]:
        settings = self.assert_effective_on(operation="historical backlog enqueue")
        if settings.mode == MODE_MANUAL:
            raise AutoanswersRuntimeError(
                "historical backlog is disabled in manual mode",
                code="manual_mode_backlog_disabled",
            )
        now = self._now()
        with self.transaction() as conn:
            preview = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_backlog_previews WHERE preview_id=?",
                (_clean_text(preview_id),),
            ).fetchone()
            if preview is None:
                raise AutoanswersRuntimeError("backlog preview not found", code="preview_not_found")
            if preview["consumed_at"]:
                raise AutoanswersRuntimeError("backlog preview already consumed", code="preview_consumed")
            if parse_timestamp(preview["expires_at"]) <= now:
                raise AutoanswersRuntimeError("backlog preview expired", code="preview_expired")
            if int(preview["enable_epoch"]) != settings.enable_epoch:
                raise AutoanswersRuntimeError("master switch epoch changed", code="preview_epoch_stale")
            candidates = self._backlog_candidates(conn)
            snapshot = canonical_json(candidates)
            if sha256_text(snapshot) != str(preview["snapshot_sha256"]):
                raise AutoanswersRuntimeError("backlog changed; create a new preview", code="preview_snapshot_stale")
            enqueued = 0
            for feedback_id, version in candidates:
                feedback = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
                    (feedback_id,),
                ).fetchone()
                key = processing_key(feedback_id, version)
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswer_jobs(
                        processing_key, feedback_id, content_version,
                        content_version_hash, state, trigger_source,
                        bundle_version, evaluation_signature, policy_version,
                        enable_epoch, available_at, attempts, created_at, updated_at
                    ) VALUES(?,?,?,?,?,'explicit_backlog',?,?,?,?,?,0,?,?)
                    """,
                    (
                        key,
                        feedback_id,
                        version,
                        feedback["content_version_hash"],
                        STATE_QUEUED,
                        PROMPT_BUNDLE_VERSION,
                        EVALUATION_SIGNATURE,
                        settings.policy_version,
                        settings.enable_epoch,
                        iso_utc(now),
                        iso_utc(now),
                        iso_utc(now),
                    ),
                )
                enqueued += int(cursor.rowcount > 0)
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_autoanswers_backlog_previews SET consumed_at=? WHERE preview_id=?",
                (iso_utc(now), preview_id),
            )
            self._audit(
                conn,
                aggregate_type="backlog_preview",
                aggregate_id=preview_id,
                event_type="backlog_enqueued",
                actor_type="user",
                actor_id=_clean_text(actor_id),
                details={"enqueued": enqueued},
                at=now,
            )
        return {"preview_id": preview_id, "enqueued": enqueued}

    def list_feedbacks(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        filters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        page_number = max(1, int(page))
        size = min(100, max(1, int(page_size)))
        source = filters or {}
        clauses: list[str] = []
        params: list[Any] = []
        if source.get("unanswered") in {True, "true", "1", 1}:
            clauses.append("COALESCE(f.answer_text,'')='' ")
        if source.get("rating") not in (None, ""):
            clauses.append("f.rating=?")
            params.append(int(source["rating"]))
        if source.get("route"):
            clauses.append("j.final_route=?")
            params.append(_clean_text(source["route"]))
        if source.get("status"):
            clauses.append("COALESCE(j.state,f.sync_status)=?")
            params.append(_clean_text(source["status"]))
        if source.get("sku"):
            clauses.append("(CAST(f.nm_id AS TEXT)=? OR f.supplier_article=?)")
            params.extend([_clean_text(source["sku"]), _clean_text(source["sku"])])
        if source.get("has_photo") in {True, "true", "1", 1}:
            clauses.append("f.has_photo=1")
        if source.get("has_video") in {True, "true", "1", 1}:
            clauses.append("f.has_video=1")
        if source.get("needs_review") in {True, "true", "1", 1}:
            clauses.append("j.state=?")
            params.append(STATE_NEEDS_REVIEW)
        if source.get("published") in {True, "true", "1", 1}:
            clauses.append("j.state=?")
            params.append(STATE_PUBLISHED)
        if source.get("error") in {True, "true", "1", 1}:
            clauses.append("j.state IN (?,?)")
            params.extend([STATE_RETRYABLE_ERROR, STATE_TERMINAL_ERROR])
        if source.get("date_from"):
            clauses.append("substr(COALESCE(f.created_at_wb,f.first_seen_at),1,10)>=?")
            params.append(_clean_text(source["date_from"]))
        if source.get("date_to"):
            clauses.append("substr(COALESCE(f.created_at_wb,f.first_seen_at),1,10)<=?")
            params.append(_clean_text(source["date_to"]))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        join = """
            LEFT JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
              ON j.processing_key=(
                SELECT j2.processing_key FROM sheet_vitrina_v1_wb_autoanswer_jobs j2
                WHERE j2.feedback_id=f.feedback_id
                ORDER BY j2.content_version DESC, j2.created_at DESC LIMIT 1
              )
            LEFT JOIN sheet_vitrina_v1_wb_publication_jobs p
              ON p.publication_key=(
                SELECT p2.publication_key FROM sheet_vitrina_v1_wb_publication_jobs p2
                WHERE p2.feedback_id=f.feedback_id
                ORDER BY p2.created_at DESC LIMIT 1
              )
        """
        with closing(self._connect()) as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM sheet_vitrina_v1_wb_feedbacks f {join} {where}", params).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT f.*, j.processing_key, j.state AS processing_state,
                       j.final_route, j.final_reply, j.actual_cost_usd,
                       j.attempts AS processing_attempts, j.last_error_code,
                       p.state AS publication_state, p.attempts AS publication_attempts
                FROM sheet_vitrina_v1_wb_feedbacks f
                {join} {where}
                ORDER BY COALESCE(f.created_at_wb,f.first_seen_at) DESC, f.feedback_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, size, (page_number - 1) * size],
            ).fetchall()
        items = [self._feedback_list_item(dict(row)) for row in rows]
        return {
            "items": items,
            "page": page_number,
            "page_size": size,
            "total": total,
            "has_more": page_number * size < total,
        }

    @staticmethod
    def _feedback_list_item(row: Mapping[str, Any]) -> dict[str, Any]:
        content = json.loads(str(row["content_json"]))
        observation = json.loads(str(row["observation_json"]))
        return {
            "id": row["feedback_id"],
            "createdDate": row["created_at_wb"],
            "productValuation": row["rating"],
            "text": content["text"],
            "pros": content["pros"],
            "cons": content["cons"],
            "productDetails": content["product"],
            "tags": content["tags"],
            "answer": observation["answer"],
            "wasViewed": observation["was_viewed"],
            "orderStatus": observation["order_status"],
            "content_version": row["content_version"],
            "content_version_hash": row["content_version_hash"],
            "wb_observation_hash": row["wb_observation_hash"],
            "has_photo": bool(row["has_photo"]),
            "has_video": bool(row["has_video"]),
            "processing_status": row.get("processing_state") or row["sync_status"],
            "publication_status": row.get("publication_state") or "not_queued",
            "route": row.get("final_route"),
            "generated_reply": row.get("final_reply"),
            "cost_usd": float(row.get("actual_cost_usd") or 0),
            "attempts": int(row.get("processing_attempts") or 0),
            "publication_attempts": int(row.get("publication_attempts") or 0),
            "error": row.get("last_error_code"),
        }

    def get_feedback(self, feedback_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            feedback = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
                (_clean_text(feedback_id),),
            ).fetchone()
            if feedback is None:
                return None
            jobs = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE feedback_id=? ORDER BY content_version DESC, created_at DESC",
                (feedback_id,),
            ).fetchall()
            media = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_feedback_media WHERE feedback_id=? AND content_version=? ORDER BY kind, ordinal",
                (feedback_id, feedback["content_version"]),
            ).fetchall()
            publications = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_publication_jobs WHERE feedback_id=? ORDER BY created_at DESC",
                (feedback_id,),
            ).fetchall()
            aggregate_ids = [str(feedback_id)]
            aggregate_ids.extend(str(row["processing_key"]) for row in jobs)
            aggregate_ids.extend(str(row["publication_key"]) for row in publications)
            placeholders = ",".join("?" for _ in aggregate_ids)
            audit = conn.execute(
                f"SELECT * FROM sheet_vitrina_v1_wb_autoanswers_audit_events WHERE aggregate_id IN ({placeholders}) ORDER BY created_at, event_id",
                aggregate_ids,
            ).fetchall()
        merged = dict(feedback)
        if jobs:
            latest_job = dict(jobs[0])
            merged.update(
                {
                    "processing_key": latest_job.get("processing_key"),
                    "processing_state": latest_job.get("state"),
                    "final_route": latest_job.get("final_route"),
                    "final_reply": latest_job.get("final_reply"),
                    "actual_cost_usd": latest_job.get("actual_cost_usd"),
                    "processing_attempts": latest_job.get("attempts"),
                    "last_error_code": latest_job.get("last_error_code"),
                }
            )
        result = self._feedback_list_item(merged)
        result.update(
            {
                "media": [dict(item) for item in media],
                "ai_jobs": [dict(item) for item in jobs],
                "publications": [dict(item) for item in publications],
                "audit": [dict(item) for item in audit],
            }
        )
        return result

    def media_rows(self, feedback_id: str, content_version: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_wb_feedback_media
                WHERE feedback_id=? AND content_version=?
                ORDER BY kind, ordinal
                """,
                (_clean_text(feedback_id), int(content_version)),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_media_result(
        self,
        *,
        feedback_id: str,
        content_version: int,
        kind: str,
        ordinal: int,
        fetch_status: str,
        local_path: str | None = None,
        sha256: str | None = None,
        mime_type: str | None = None,
        byte_size: int | None = None,
        uncertainty_code: str | None = None,
    ) -> None:
        now = self._now()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_feedback_media
                SET fetch_status=?, local_path=?, sha256=?, mime_type=?, byte_size=?,
                    uncertainty_code=?, updated_at=?
                WHERE feedback_id=? AND content_version=? AND kind=? AND ordinal=?
                """,
                (
                    _clean_text(fetch_status),
                    local_path,
                    sha256,
                    mime_type,
                    byte_size,
                    _clean_text(uncertainty_code) or None,
                    iso_utc(now),
                    _clean_text(feedback_id),
                    int(content_version),
                    _clean_text(kind),
                    int(ordinal),
                ),
            )
            if cursor.rowcount != 1:
                raise AutoanswersRuntimeError("media row not found", code="media_not_found")

    def replace_video_frames(
        self,
        *,
        feedback_id: str,
        content_version: int,
        frames: Sequence[Mapping[str, Any]],
    ) -> None:
        now = self._now()
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM sheet_vitrina_v1_wb_feedback_media WHERE feedback_id=? AND content_version=? AND kind='video_frame'",
                (_clean_text(feedback_id), int(content_version)),
            )
            for ordinal, frame in enumerate(frames[:20]):
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_wb_feedback_media(
                        feedback_id, content_version, kind, ordinal,
                        stable_full_url, stable_preview_url, source_full_url,
                        source_preview_url, local_path, sha256, mime_type,
                        byte_size, fetch_status, updated_at
                    ) VALUES(?,?,'video_frame',?,'','','','',?,?,?,?, 'downloaded',?)
                    """,
                    (
                        _clean_text(feedback_id),
                        int(content_version),
                        ordinal,
                        str(frame["local_path"]),
                        str(frame["sha256"]),
                        str(frame.get("mime_type") or "image/jpeg"),
                        int(frame["byte_size"]),
                        iso_utc(now),
                    ),
                )


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_schema_migrations(
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_settings(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    master_enabled INTEGER NOT NULL DEFAULT 0 CHECK(master_enabled IN (0,1)),
    mode TEXT NOT NULL DEFAULT 'draft_only' CHECK(mode IN ('manual','draft_only','auto_safe','auto_all')),
    enable_epoch INTEGER NOT NULL DEFAULT 0,
    enabled_at TEXT,
    daily_cap_usd TEXT NOT NULL,
    monthly_cap_usd TEXT NOT NULL,
    warning_ratio TEXT NOT NULL,
    max_reservation_per_review_usd TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_feedbacks(
    feedback_id TEXT PRIMARY KEY,
    created_at_wb TEXT,
    updated_at_wb TEXT,
    content_version INTEGER NOT NULL,
    content_version_hash TEXT NOT NULL,
    wb_observation_hash TEXT NOT NULL,
    content_json TEXT NOT NULL,
    observation_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    answer_text TEXT NOT NULL DEFAULT '',
    rating INTEGER,
    nm_id INTEGER,
    supplier_article TEXT,
    product_name TEXT,
    brand_name TEXT,
    has_photo INTEGER NOT NULL DEFAULT 0,
    has_video INTEGER NOT NULL DEFAULT 0,
    source_stream TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    sync_status TEXT NOT NULL,
    auto_eligible_epoch INTEGER,
    last_sync_run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_sv1_feedbacks_created ON sheet_vitrina_v1_wb_feedbacks(created_at_wb DESC, feedback_id DESC);
CREATE INDEX IF NOT EXISTS idx_sv1_feedbacks_unanswered ON sheet_vitrina_v1_wb_feedbacks(answer_text, created_at_wb DESC);
CREATE INDEX IF NOT EXISTS idx_sv1_feedbacks_sku ON sheet_vitrina_v1_wb_feedbacks(nm_id, supplier_article);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_feedback_versions(
    feedback_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_feedbacks(feedback_id),
    content_version INTEGER NOT NULL,
    content_version_hash TEXT NOT NULL,
    content_json TEXT NOT NULL,
    source_raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(feedback_id, content_version),
    UNIQUE(feedback_id, content_version_hash)
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_feedback_media(
    feedback_id TEXT NOT NULL,
    content_version INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('photo','video','video_frame')),
    ordinal INTEGER NOT NULL,
    stable_full_url TEXT NOT NULL DEFAULT '',
    stable_preview_url TEXT NOT NULL DEFAULT '',
    source_full_url TEXT NOT NULL DEFAULT '',
    source_preview_url TEXT NOT NULL DEFAULT '',
    duration_seconds INTEGER,
    local_path TEXT,
    sha256 TEXT,
    mime_type TEXT,
    byte_size INTEGER,
    fetch_status TEXT NOT NULL DEFAULT 'pending',
    uncertainty_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(feedback_id, content_version, kind, ordinal),
    FOREIGN KEY(feedback_id, content_version)
      REFERENCES sheet_vitrina_v1_wb_feedback_versions(feedback_id, content_version)
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_sync_runs(
    sync_run_id TEXT PRIMARY KEY,
    run_kind TEXT NOT NULL,
    source_stream TEXT NOT NULL,
    state TEXT NOT NULL,
    cursor_json TEXT,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    upserted_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_sync_state(
    stream_key TEXT PRIMARY KEY,
    cursor_json TEXT NOT NULL,
    watermark_at TEXT,
    last_success_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_commands(
    command_id TEXT PRIMARY KEY,
    command_type TEXT NOT NULL CHECK(command_type IN ('sync_now')),
    request_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','processing','succeeded','retryable_error','terminal_error')),
    actor_id TEXT NOT NULL,
    available_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until TEXT,
    result_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(command_type, request_key)
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswer_jobs(
    processing_key TEXT PRIMARY KEY,
    feedback_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_feedbacks(feedback_id),
    content_version INTEGER NOT NULL,
    content_version_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    bundle_version TEXT NOT NULL,
    evaluation_signature TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    enable_epoch INTEGER NOT NULL,
    final_route TEXT,
    case_code TEXT,
    final_reply TEXT,
    final_reply_sha256 TEXT,
    result_json TEXT,
    review_reasons_json TEXT,
    hard_gates_passed INTEGER,
    fallback_used INTEGER,
    media_uncertain INTEGER,
    node_contract_valid INTEGER,
    actual_cost_usd TEXT NOT NULL DEFAULT '0',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_until TEXT,
    retry_stage TEXT,
    last_error_code TEXT,
    approved_by TEXT,
    approved_at TEXT,
    manual_reply TEXT,
    manual_reply_sha256 TEXT,
    manual_guard_passed INTEGER,
    manual_guard_errors_json TEXT,
    manual_reviewed_by TEXT,
    manual_reviewed_at TEXT,
    manual_edit_revision INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(feedback_id, content_version, bundle_version),
    FOREIGN KEY(feedback_id, content_version)
      REFERENCES sheet_vitrina_v1_wb_feedback_versions(feedback_id, content_version)
);
CREATE INDEX IF NOT EXISTS idx_sv1_ai_jobs_claim ON sheet_vitrina_v1_wb_autoanswer_jobs(state, available_at, lease_until);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_publication_jobs(
    publication_key TEXT PRIMARY KEY,
    processing_key TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_autoanswer_jobs(processing_key),
    feedback_id TEXT NOT NULL,
    content_version INTEGER NOT NULL,
    content_version_hash TEXT NOT NULL,
    exact_reply TEXT NOT NULL,
    normalized_reply_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_until TEXT,
    write_started_at TEXT,
    wb_transport_status INTEGER,
    readback_answer TEXT,
    readback_hash TEXT,
    last_error_code TEXT,
    retry_stage TEXT,
    request_source TEXT NOT NULL DEFAULT 'automatic',
    requested_by TEXT,
    mode_at_enqueue TEXT,
    manual_edit_revision INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sv1_pub_jobs_claim ON sheet_vitrina_v1_wb_publication_jobs(state, available_at, lease_until);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sv1_pub_jobs_one_create_per_version ON sheet_vitrina_v1_wb_publication_jobs(feedback_id, content_version);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_publication_attempts(
    attempt_id TEXT PRIMARY KEY,
    publication_key TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_publication_jobs(publication_key),
    attempt_number INTEGER NOT NULL,
    request_reply_sha256 TEXT NOT NULL,
    transport_outcome TEXT NOT NULL,
    http_status INTEGER,
    write_started_at TEXT NOT NULL,
    write_finished_at TEXT,
    readback_outcome TEXT,
    readback_answer_sha256 TEXT,
    details_json TEXT NOT NULL,
    UNIQUE(publication_key, attempt_number)
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_budget_reservations(
    processing_key TEXT PRIMARY KEY REFERENCES sheet_vitrina_v1_wb_autoanswer_jobs(processing_key),
    reserved_usd TEXT NOT NULL,
    actual_cost_usd TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('reserved','settled','released')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_backlog_previews(
    preview_id TEXT PRIMARY KEY,
    snapshot_sha256 TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    max_estimated_cost_usd TEXT NOT NULL,
    enable_epoch INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_audit_events(
    event_id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    previous_state TEXT,
    next_state TEXT,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    bundle_version TEXT NOT NULL,
    evaluation_signature TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sv1_autoanswers_audit ON sheet_vitrina_v1_wb_autoanswers_audit_events(aggregate_id, created_at);
"""
