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
    AUTOANSWERS_CONTRACT_VERSION,
    AUTOANSWER_MODES,
    AUTO_SAFE_ROUTES,
    BACKFILL_FROM_DATE,
    CONTENT_CLASS_CONTENT_BEARING,
    CONTENT_CLASS_INDETERMINATE,
    CONTENT_CLASS_RATING_ONLY,
    EVALUATION_SIGNATURE,
    MODE_AUTO_ALL,
    MODE_AUTO_SAFE,
    MODE_DRAFT_ONLY,
    MODE_MANUAL,
    NODE_BOUNDARY_VERSION,
    PROCESSING_KIND_FROZEN_AI,
    PROCESSING_KIND_RATING_ONLY_TEMPLATE,
    PROMPT_BUNDLE_VERSION,
    ROUTE_RATING_ONLY_TEMPLATE,
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


SCHEMA_VERSION = 6
DEFAULT_DAILY_CAP_USD = Decimal("5.00")
DEFAULT_MONTHLY_CAP_USD = Decimal("50.00")
DEFAULT_HOURLY_CAP_USD = Decimal("0.50")
DEFAULT_MAX_PAID_REVIEWS_PER_HOUR = 20
DEFAULT_GLOBAL_PAID_REVIEW_CONCURRENCY = 1
DEFAULT_MAX_INFLIGHT_ROLE_CALLS = 1
DEFAULT_MAX_MATERIALIZED_PROCESSING_JOBS = 5
DEFAULT_WARNING_RATIO = Decimal("0.70")
# Conservative upper reservation covers the frozen pipeline's bounded normal
# path plus two rewrite/validator cycles.  Settlement releases the difference.
DEFAULT_JOB_RESERVATION_USD = Decimal("0.10")
DEFAULT_ESTIMATED_REVIEW_COST_USD = Decimal("0.03")
DEFAULT_POLICY_VERSION = "owner-policy-2026-07-21-v3"
RATING_ONLY_TEMPLATE_POLICY_VERSION = "owner-policy-2026-07-21-v2"
DEFAULT_LEASE_SECONDS = 300
BACKLOG_PREVIEW_TTL_SECONDS = 900
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
COMPRESSED_SCHEMA_BACKUP_CONTRACT = "wb_autoanswers_compressed_schema_backup_v6"
RATING_ONLY_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "wb_autoanswers_rating_only_policy_v2.json"
)


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


def _automatic_priority_order_sql(
    alias: str,
    *,
    manual_predicate: str | None = None,
) -> str:
    """Return the canonical automatic queue order for a feedback SQL alias."""

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("invalid automatic priority SQL alias")
    if manual_predicate not in {
        None,
        "j.trigger_source='manual_generate'",
        "p.request_source='manual'",
    }:
        raise ValueError("invalid manual priority SQL predicate")
    manual_prefix = (
        f"""
        CASE WHEN {manual_predicate} THEN 0 ELSE 1 END,
        CASE WHEN {manual_predicate} THEN 0 ELSE
          CASE {alias}.content_classification
            WHEN '{CONTENT_CLASS_CONTENT_BEARING}' THEN 0
            WHEN '{CONTENT_CLASS_INDETERMINATE}' THEN 1
            ELSE 2 END
        END,
        CASE WHEN {manual_predicate} THEN 0 ELSE
          CASE
            WHEN {alias}.content_classification='{CONTENT_CLASS_CONTENT_BEARING}'
              THEN CASE WHEN {alias}.rating BETWEEN 1 AND 5 THEN {alias}.rating ELSE 6 END
            ELSE 0 END
        END,
        """
        if manual_predicate
        else f"""
        CASE {alias}.content_classification
          WHEN '{CONTENT_CLASS_CONTENT_BEARING}' THEN 0
          WHEN '{CONTENT_CLASS_INDETERMINATE}' THEN 1
          ELSE 2 END,
        CASE
          WHEN {alias}.content_classification='{CONTENT_CLASS_CONTENT_BEARING}'
            THEN CASE WHEN {alias}.rating BETWEEN 1 AND 5 THEN {alias}.rating ELSE 6 END
          ELSE 0 END,
        """
    )
    return f"""
        {manual_prefix}
        CASE WHEN {alias}.created_at_wb IS NULL OR trim({alias}.created_at_wb)=''
             THEN {alias}.first_seen_at ELSE {alias}.created_at_wb END DESC,
        {alias}.feedback_id DESC
    """.strip()


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


def _progress_percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    if numerator >= denominator:
        return 100.0
    return min(99.9, round(100 * max(0, numerator) / denominator, 1))


def _rating_only_policy() -> dict[str, Any]:
    policy = json.loads(RATING_ONLY_POLICY_PATH.read_text(encoding="utf-8"))
    if (
        policy.get("policy_version") != RATING_ONLY_TEMPLATE_POLICY_VERSION
        or policy.get("route") != ROUTE_RATING_ONLY_TEMPLATE
        or policy.get("openai_calls") != 0
    ):
        raise AutoanswersRuntimeError(
            "rating-only policy identity mismatch",
            code="rating_only_policy_mismatch",
        )
    return policy


def rating_only_template(feedback_id: str, rating: int) -> dict[str, Any]:
    normalized_rating = int(rating)
    templates = _rating_only_policy().get("templates") or {}
    choices = templates.get(str(normalized_rating))
    if normalized_rating not in {1, 2, 3, 4, 5} or not isinstance(choices, list) or not choices:
        raise AutoanswersRuntimeError(
            "rating-only template is unavailable",
            code="rating_only_template_missing",
        )
    index = int(hashlib.sha256(str(feedback_id).encode("utf-8")).hexdigest(), 16) % len(choices)
    return {
        "route": ROUTE_RATING_ONLY_TEMPLATE,
        "subcategory": f"rating_{normalized_rating}_empty",
        "template_id": f"rating_{normalized_rating}_empty_v{index + 1}",
        "reply": str(choices[index]),
        "policy_version": DEFAULT_POLICY_VERSION,
        "template_policy_version": RATING_ONLY_TEMPLATE_POLICY_VERSION,
    }


def _content_is_rating_only(content_json: Any) -> bool:
    return classify_feedback_content(content_json) == CONTENT_CLASS_RATING_ONLY


def classify_feedback_content(
    content_json: Any,
    *,
    has_photo: Any = False,
    has_video: Any = False,
    canonical_media_present: bool = False,
) -> str:
    """Classify one current content version conservatively and deterministically.

    Persisted canonical media evidence always wins.  Malformed or contradictory
    content can never enter the zero-cost rating-only route.
    """

    try:
        content = json.loads(str(content_json or "{}"))
    except (TypeError, json.JSONDecodeError):
        return CONTENT_CLASS_INDETERMINATE
    if not isinstance(content, Mapping):
        return CONTENT_CLASS_INDETERMINATE
    if any(_clean_text(content.get(field)) for field in ("text", "pros", "cons")):
        return CONTENT_CLASS_CONTENT_BEARING
    if bool(has_photo) or bool(has_video) or bool(canonical_media_present):
        return CONTENT_CLASS_CONTENT_BEARING

    tags = content.get("tags", [])
    if not isinstance(tags, list):
        return CONTENT_CLASS_INDETERMINATE
    for tag in tags:
        if isinstance(tag, Mapping):
            value = tag.get("name") or tag.get("label") or tag.get("text")
        else:
            value = tag
        if _clean_text(value):
            return CONTENT_CLASS_CONTENT_BEARING

    media = content.get("media", [])
    if not isinstance(media, list):
        return CONTENT_CLASS_INDETERMINATE
    if any(bool(item) for item in media):
        return CONTENT_CLASS_CONTENT_BEARING

    rating = _safe_int(content.get("rating"))
    if rating in {1, 2, 3, 4, 5}:
        return CONTENT_CLASS_RATING_ONLY
    return CONTENT_CLASS_INDETERMINATE


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
            self._migrate_schema_v6(conn)
            conn.execute(
                "INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswers_schema_migrations(version, applied_at) VALUES(?, ?)",
                (SCHEMA_VERSION, iso_utc(self._now())),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswers_settings(
                    singleton, master_enabled, mode, enable_epoch, policy_epoch, enabled_at,
                    daily_cap_usd, monthly_cap_usd, hourly_cap_usd,
                    max_paid_reviews_per_hour, global_paid_review_concurrency,
                    max_inflight_role_calls, max_materialized_processing_jobs, warning_ratio,
                    max_reservation_per_review_usd, policy_version, updated_at
                ) VALUES(1, 0, ?, 0, 0, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    MODE_DRAFT_ONLY,
                    str(DEFAULT_DAILY_CAP_USD),
                    str(DEFAULT_MONTHLY_CAP_USD),
                    str(DEFAULT_HOURLY_CAP_USD),
                    DEFAULT_MAX_PAID_REVIEWS_PER_HOUR,
                    DEFAULT_GLOBAL_PAID_REVIEW_CONCURRENCY,
                    DEFAULT_MAX_INFLIGHT_ROLE_CALLS,
                    DEFAULT_MAX_MATERIALIZED_PROCESSING_JOBS,
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

    @staticmethod
    def _migrate_schema_v3(conn: sqlite3.Connection) -> None:
        """Add media-aware regeneration and resumable policy reconciliation.

        All changes are additive after the v2 mode widening.  Existing
        publication attempts are immutable and are deliberately excluded from
        the migration that quarantines text-only media failures.
        """

        AutoanswersRepository._migrate_schema_v2(conn)

        def add_column(table: str, column: str, declaration: str) -> None:
            columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

        settings_table = "sheet_vitrina_v1_wb_autoanswers_settings"
        add_column(settings_table, "policy_epoch", "INTEGER NOT NULL DEFAULT 0")

        media_table = "sheet_vitrina_v1_wb_feedback_media"
        for name, declaration in (
            ("preview_local_path", "TEXT"),
            ("preview_sha256", "TEXT"),
            ("preview_mime_type", "TEXT"),
            ("preview_byte_size", "INTEGER"),
            ("expires_at", "TEXT"),
        ):
            add_column(media_table, name, declaration)

        job_table = "sheet_vitrina_v1_wb_autoanswer_jobs"
        for name, declaration in (
            ("policy_epoch", "INTEGER NOT NULL DEFAULT 0"),
            ("media_processing_version", "INTEGER NOT NULL DEFAULT 1"),
            ("regeneration_required", "INTEGER NOT NULL DEFAULT 0"),
            ("regeneration_reason", "TEXT"),
            ("manual_started", "INTEGER NOT NULL DEFAULT 0"),
        ):
            add_column(job_table, name, declaration)

        publication_table = "sheet_vitrina_v1_wb_publication_jobs"
        add_column(publication_table, "policy_epoch", "INTEGER NOT NULL DEFAULT 0")

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswer_job_revisions(
                revision_id TEXT PRIMARY KEY,
                processing_key TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_autoanswer_jobs(processing_key),
                media_processing_version INTEGER NOT NULL,
                previous_state TEXT NOT NULL,
                result_json TEXT,
                final_route TEXT,
                final_reply TEXT,
                final_reply_sha256 TEXT,
                media_uncertain INTEGER,
                actual_cost_usd TEXT NOT NULL DEFAULT '0',
                reason TEXT NOT NULL,
                archived_at TEXT NOT NULL,
                UNIQUE(processing_key, media_processing_version)
            );

            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_cost_events(
                event_id TEXT PRIMARY KEY,
                processing_key TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_autoanswer_jobs(processing_key),
                media_processing_version INTEGER NOT NULL,
                actual_cost_usd TEXT NOT NULL,
                incurred_at TEXT NOT NULL,
                UNIQUE(processing_key, media_processing_version)
            );

            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_transition_previews(
                preview_id TEXT PRIMARY KEY,
                target_selector_state TEXT NOT NULL,
                scope_from TEXT NOT NULL,
                scope_to TEXT,
                snapshot_sha256 TEXT NOT NULL,
                counts_json TEXT NOT NULL,
                estimated_cost_usd TEXT NOT NULL,
                budget_json TEXT NOT NULL,
                enable_epoch INTEGER NOT NULL,
                policy_epoch INTEGER NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps(
                sweep_id TEXT PRIMARY KEY,
                preview_id TEXT,
                policy_epoch INTEGER NOT NULL UNIQUE,
                target_mode TEXT NOT NULL,
                scope_from TEXT NOT NULL,
                scope_to TEXT,
                state TEXT NOT NULL CHECK(state IN ('queued','processing','succeeded','retryable_error','terminal_error')),
                cursor_json TEXT NOT NULL,
                totals_json TEXT NOT NULL,
                progress_json TEXT NOT NULL,
                lease_owner TEXT,
                lease_until TEXT,
                last_error_code TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sv1_policy_sweeps_claim
            ON sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps(state, lease_until, created_at);
            """
        )
        sweep_table = "sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps"
        add_column(sweep_table, "preview_id", "TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sv1_policy_sweeps_preview "
            "ON sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps(preview_id) "
            "WHERE preview_id IS NOT NULL"
        )

        # Only unpublished, unanswered text-only results are invalidated.  A
        # publication attempt is durable proof that the old aggregate must not
        # be rewritten by this migration.
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_wb_autoanswer_jobs AS j
            SET regeneration_required=1,
                regeneration_reason='media_fetch_failed',
                state='needs_review',
                review_reasons_json='["media_uncertain","regeneration_required"]',
                updated_at=?
            WHERE j.media_uncertain=1
              AND COALESCE(j.regeneration_required,0)=0
              AND j.state<>'published'
              AND NOT EXISTS (
                    SELECT 1 FROM sheet_vitrina_v1_wb_publication_jobs p
                    JOIN sheet_vitrina_v1_wb_publication_attempts a
                      ON a.publication_key=p.publication_key
                    WHERE p.processing_key=j.processing_key
              )
              AND NOT EXISTS (
                    SELECT 1 FROM sheet_vitrina_v1_wb_feedbacks f
                    WHERE f.feedback_id=j.feedback_id AND COALESCE(f.answer_text,'')<>''
              )
            """,
            (iso_utc(),),
        )

    @staticmethod
    def _migrate_schema_v4(conn: sqlite3.Connection) -> None:
        """Add bounded spend control, lazy-run evidence and zero-cost templates."""

        first_application = conn.execute(
            "SELECT 1 FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations WHERE version=4"
        ).fetchone() is None
        AutoanswersRepository._migrate_schema_v3(conn)

        def add_column(table: str, column: str, declaration: str) -> None:
            columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

        settings_table = "sheet_vitrina_v1_wb_autoanswers_settings"
        for name, declaration in (
            ("hourly_cap_usd", f"TEXT NOT NULL DEFAULT '{DEFAULT_HOURLY_CAP_USD}'"),
            ("max_paid_reviews_per_hour", f"INTEGER NOT NULL DEFAULT {DEFAULT_MAX_PAID_REVIEWS_PER_HOUR}"),
            ("global_paid_review_concurrency", f"INTEGER NOT NULL DEFAULT {DEFAULT_GLOBAL_PAID_REVIEW_CONCURRENCY}"),
            ("max_inflight_role_calls", f"INTEGER NOT NULL DEFAULT {DEFAULT_MAX_INFLIGHT_ROLE_CALLS}"),
            ("max_materialized_processing_jobs", f"INTEGER NOT NULL DEFAULT {DEFAULT_MAX_MATERIALIZED_PROCESSING_JOBS}"),
        ):
            add_column(settings_table, name, declaration)

        job_table = "sheet_vitrina_v1_wb_autoanswer_jobs"
        add_column(job_table, "processing_kind", f"TEXT NOT NULL DEFAULT '{PROCESSING_KIND_FROZEN_AI}'")
        add_column(job_table, "transition_run_id", "TEXT")

        publication_table = "sheet_vitrina_v1_wb_publication_jobs"
        add_column(publication_table, "transition_run_id", "TEXT")

        reservation_table = "sheet_vitrina_v1_wb_autoanswers_budget_reservations"
        for name, declaration in (
            ("transition_run_id", "TEXT"),
            ("expires_at", "TEXT"),
            ("provider_call_started_at", "TEXT"),
            ("released_reason", "TEXT"),
            ("settled_at", "TEXT"),
        ):
            add_column(reservation_table, name, declaration)

        preview_table = "sheet_vitrina_v1_wb_autoanswers_transition_previews"
        add_column(preview_table, "run_max_usd", "TEXT")
        add_column(preview_table, "run_max_paid_reviews", "INTEGER")
        add_column(preview_table, "estimated_unit_cost_usd", "TEXT")

        sweep_table = "sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps"
        for name, declaration in (
            ("transition_run_id", "TEXT"),
            ("run_max_usd", "TEXT"),
            ("run_max_paid_reviews", "INTEGER"),
            ("pause_reason", "TEXT"),
        ):
            add_column(sweep_table, name, declaration)

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_budget_adjustments(
                adjustment_id TEXT PRIMARY KEY,
                processing_key TEXT,
                amount_usd TEXT NOT NULL,
                reason TEXT NOT NULL,
                effective_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_failed_cost_events(
                event_id TEXT PRIMARY KEY,
                processing_key TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_autoanswer_jobs(processing_key),
                attempt_number INTEGER NOT NULL,
                transition_run_id TEXT,
                actual_cost_usd TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                role_calls INTEGER NOT NULL,
                error_code TEXT NOT NULL,
                incurred_at TEXT NOT NULL,
                UNIQUE(processing_key, attempt_number)
            );

            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_runtime_state(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                stop_reason TEXT,
                stop_details_json TEXT NOT NULL DEFAULT '{}',
                last_scheduler_tick_at TEXT,
                last_successful_ai_call_at TEXT,
                last_confirmed_publication_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_reconciliation_scope(
                sweep_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps(sweep_id),
                feedback_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_feedbacks(feedback_id),
                content_version_at_preview INTEGER NOT NULL,
                content_version_hash_at_preview TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                PRIMARY KEY(sweep_id,feedback_id)
            );

            CREATE INDEX IF NOT EXISTS idx_sv1_ai_jobs_run_state
            ON sheet_vitrina_v1_wb_autoanswer_jobs(transition_run_id, state, available_at);
            CREATE INDEX IF NOT EXISTS idx_sv1_budget_reservation_expiry
            ON sheet_vitrina_v1_wb_autoanswers_budget_reservations(status, expires_at);
            CREATE INDEX IF NOT EXISTS idx_sv1_failed_cost_incurred
            ON sheet_vitrina_v1_wb_autoanswers_failed_cost_events(incurred_at, transition_run_id);
            CREATE INDEX IF NOT EXISTS idx_sv1_reconciliation_scope_feedback
            ON sheet_vitrina_v1_wb_autoanswers_reconciliation_scope(feedback_id,sweep_id);
            """
        )
        now = iso_utc()
        conn.execute(
            "INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswers_runtime_state(singleton,updated_at) VALUES(1,?)",
            (now,),
        )
        if first_application:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_settings
                SET hourly_cap_usd=?, max_paid_reviews_per_hour=?,
                    global_paid_review_concurrency=?, max_inflight_role_calls=?,
                    max_materialized_processing_jobs=?, max_reservation_per_review_usd=?,
                    policy_version=?
                WHERE singleton=1
                """,
                (
                    str(DEFAULT_HOURLY_CAP_USD),
                    DEFAULT_MAX_PAID_REVIEWS_PER_HOUR,
                    DEFAULT_GLOBAL_PAID_REVIEW_CONCURRENCY,
                    DEFAULT_MAX_INFLIGHT_ROLE_CALLS,
                    DEFAULT_MAX_MATERIALIZED_PROCESSING_JOBS,
                    str(DEFAULT_JOB_RESERVATION_USD),
                    DEFAULT_POLICY_VERSION,
                ),
            )
        conn.execute(
            "UPDATE sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps "
            "SET transition_run_id=COALESCE(transition_run_id,sweep_id)"
        )
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
            SET processing_kind=?
            WHERE EXISTS(
                SELECT 1 FROM sheet_vitrina_v1_wb_feedbacks f
                WHERE f.feedback_id=sheet_vitrina_v1_wb_autoanswer_jobs.feedback_id
                  AND f.content_version=sheet_vitrina_v1_wb_autoanswer_jobs.content_version
                  AND COALESCE(json_extract(f.content_json,'$.text'),'')=''
                  AND COALESCE(json_extract(f.content_json,'$.pros'),'')=''
                  AND COALESCE(json_extract(f.content_json,'$.cons'),'')=''
                  AND CAST(json_extract(f.content_json,'$.rating') AS INTEGER) BETWEEN 1 AND 5
            )
            """,
            (PROCESSING_KIND_RATING_ONLY_TEMPLATE,),
        )
        # Preserve the immutable incident row and correct future budget totals
        # with an additive adjustment. A terminal reservation is not evidence
        # of provider usage and must never be counted as actual spend.
        terminal_rows = conn.execute(
            """
            SELECT r.processing_key,r.actual_cost_usd,r.updated_at
            FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations r
            JOIN sheet_vitrina_v1_wb_autoanswer_jobs j USING(processing_key)
            WHERE r.status='settled' AND j.state='terminal_error'
              AND CAST(r.actual_cost_usd AS REAL)>0
              AND CAST(COALESCE(j.actual_cost_usd,'0') AS REAL)=0
            """
        ).fetchall()
        for row in terminal_rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswers_budget_adjustments(
                    adjustment_id,processing_key,amount_usd,reason,effective_at,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    f"v4-terminal-release:{row['processing_key']}",
                    row["processing_key"],
                    str(-_money(row["actual_cost_usd"])),
                    "terminal_reservation_was_not_actual_usage",
                    row["updated_at"],
                    now,
                ),
            )

    @staticmethod
    def _migrate_schema_v5(conn: sqlite3.Connection) -> None:
        """Persist canonical content classification and immutable run taxonomy."""

        first_application = conn.execute(
            "SELECT 1 FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations WHERE version=5"
        ).fetchone() is None
        AutoanswersRepository._migrate_schema_v4(conn)

        def add_column(table: str, column: str, declaration: str) -> None:
            columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

        feedback_table = "sheet_vitrina_v1_wb_feedbacks"
        scope_table = "sheet_vitrina_v1_wb_autoanswers_reconciliation_scope"
        add_column(
            feedback_table,
            "content_classification",
            f"TEXT NOT NULL DEFAULT '{CONTENT_CLASS_INDETERMINATE}'",
        )
        add_column(
            scope_table,
            "content_classification_at_preview",
            f"TEXT NOT NULL DEFAULT '{CONTENT_CLASS_INDETERMINATE}'",
        )
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_sv1_feedbacks_class_priority
            ON sheet_vitrina_v1_wb_feedbacks(
                content_classification,
                created_at_wb DESC,
                first_seen_at DESC,
                feedback_id DESC
            );
            CREATE INDEX IF NOT EXISTS idx_sv1_scope_class_priority
            ON sheet_vitrina_v1_wb_autoanswers_reconciliation_scope(
                sweep_id,
                content_classification_at_preview,
                ordinal
            );
            """
        )
        conn.execute(
            f"""
            UPDATE {feedback_table} AS f
            SET content_classification = CASE
                WHEN COALESCE(f.has_photo,0)=1 OR COALESCE(f.has_video,0)=1
                  OR EXISTS(
                      SELECT 1 FROM sheet_vitrina_v1_wb_feedback_media m
                      WHERE m.feedback_id=f.feedback_id
                        AND m.content_version=f.content_version
                        AND m.kind IN ('photo','video','video_frame')
                  )
                  THEN '{CONTENT_CLASS_CONTENT_BEARING}'
                WHEN json_valid(f.content_json)=0
                  THEN '{CONTENT_CLASS_INDETERMINATE}'
                WHEN trim(COALESCE(json_extract(f.content_json,'$.text'),''))<>''
                  OR trim(COALESCE(json_extract(f.content_json,'$.pros'),''))<>''
                  OR trim(COALESCE(json_extract(f.content_json,'$.cons'),''))<>''
                  OR (
                    json_type(f.content_json,'$.tags')='array'
                    AND EXISTS(
                      SELECT 1 FROM json_each(json_extract(f.content_json,'$.tags'))
                      WHERE trim(COALESCE(CAST(value AS TEXT),''))<>''
                    )
                  )
                  OR (
                    json_type(f.content_json,'$.media')='array'
                    AND json_array_length(json_extract(f.content_json,'$.media'))>0
                  )
                  THEN '{CONTENT_CLASS_CONTENT_BEARING}'
                WHEN COALESCE(json_type(f.content_json,'$.tags'),'array')<>'array'
                  OR COALESCE(json_type(f.content_json,'$.media'),'array')<>'array'
                  THEN '{CONTENT_CLASS_INDETERMINATE}'
                WHEN CAST(json_extract(f.content_json,'$.rating') AS INTEGER) BETWEEN 1 AND 5
                  THEN '{CONTENT_CLASS_RATING_ONLY}'
                ELSE '{CONTENT_CLASS_INDETERMINATE}'
            END
            """
        )
        conn.execute(
            f"""
            UPDATE {scope_table}
            SET content_classification_at_preview=COALESCE((
                SELECT f.content_classification FROM {feedback_table} f
                WHERE f.feedback_id={scope_table}.feedback_id
                  AND f.content_version={scope_table}.content_version_at_preview
            ),'{CONTENT_CLASS_INDETERMINATE}')
            """
        )
        conn.execute(
            f"""
            UPDATE sheet_vitrina_v1_wb_autoanswer_jobs AS j
            SET processing_kind=CASE
                WHEN EXISTS(
                    SELECT 1 FROM {feedback_table} f
                    WHERE f.feedback_id=j.feedback_id
                      AND f.content_version=j.content_version
                      AND f.content_classification='{CONTENT_CLASS_RATING_ONLY}'
                ) THEN ? ELSE ? END
            WHERE EXISTS(
                SELECT 1 FROM {feedback_table} current_feedback
                WHERE current_feedback.feedback_id=j.feedback_id
                  AND current_feedback.content_version=j.content_version
            )
            """,
            (PROCESSING_KIND_RATING_ONLY_TEMPLATE, PROCESSING_KIND_FROZEN_AI),
        )
        # A v2 zero-cost result for a newly content-bearing review is retained
        # as evidence but may never be published or treated as a current draft.
        conn.execute(
            f"""
            UPDATE sheet_vitrina_v1_wb_autoanswer_jobs AS j
            SET regeneration_required=1,
                regeneration_reason='content_classification_v3_changed',
                state='{STATE_NEEDS_REVIEW}',
                review_reasons_json='["content_classification_v3_changed","regeneration_required"]',
                updated_at=?
            WHERE j.final_route=?
              AND j.state<>'{STATE_PUBLISHED}'
              AND EXISTS(
                  SELECT 1 FROM {feedback_table} f
                  WHERE f.feedback_id=j.feedback_id
                    AND f.content_version=j.content_version
                    AND f.content_classification<>'{CONTENT_CLASS_RATING_ONLY}'
              )
              AND NOT EXISTS(
                  SELECT 1 FROM sheet_vitrina_v1_wb_publication_jobs p
                  JOIN sheet_vitrina_v1_wb_publication_attempts a
                    ON a.publication_key=p.publication_key
                  WHERE p.processing_key=j.processing_key
              )
            """,
            (iso_utc(), ROUTE_RATING_ONLY_TEMPLATE),
        )
        if first_application:
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_autoanswers_settings SET policy_version=?, updated_at=? WHERE singleton=1",
                (DEFAULT_POLICY_VERSION, iso_utc()),
            )

    @staticmethod
    def _migrate_schema_v6(conn: sqlite3.Connection) -> None:
        """Add immutable conservative holds for provider-cost uncertainty."""

        AutoanswersRepository._migrate_schema_v5(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds(
                hold_id TEXT PRIMARY KEY,
                processing_key TEXT NOT NULL
                    REFERENCES sheet_vitrina_v1_wb_autoanswer_jobs(processing_key),
                transition_run_id TEXT,
                upper_bound_usd TEXT NOT NULL,
                effective_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(processing_key)
            );
            CREATE INDEX IF NOT EXISTS idx_sv1_budget_uncertainty_effective
            ON sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds(
                effective_at,
                transition_run_id
            );
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
            policy_epoch=int(row["policy_epoch"]),
            enabled_at=str(row["enabled_at"]) if row["enabled_at"] else None,
            daily_cap_usd=float(row["daily_cap_usd"]),
            monthly_cap_usd=float(row["monthly_cap_usd"]),
            hourly_cap_usd=float(row["hourly_cap_usd"]),
            max_paid_reviews_per_hour=int(row["max_paid_reviews_per_hour"]),
            global_paid_review_concurrency=int(row["global_paid_review_concurrency"]),
            max_inflight_role_calls=int(row["max_inflight_role_calls"]),
            max_materialized_processing_jobs=int(row["max_materialized_processing_jobs"]),
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
        hourly_cap_usd: Any | None = None,
        max_paid_reviews_per_hour: int | None = None,
        global_paid_review_concurrency: int | None = None,
        max_inflight_role_calls: int | None = None,
        max_materialized_processing_jobs: int | None = None,
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
            hourly = _money(current["hourly_cap_usd"] if hourly_cap_usd is None else hourly_cap_usd)
            paid_per_hour = int(current["max_paid_reviews_per_hour"] if max_paid_reviews_per_hour is None else max_paid_reviews_per_hour)
            paid_concurrency = int(current["global_paid_review_concurrency"] if global_paid_review_concurrency is None else global_paid_review_concurrency)
            role_concurrency = int(current["max_inflight_role_calls"] if max_inflight_role_calls is None else max_inflight_role_calls)
            materialized_limit = int(current["max_materialized_processing_jobs"] if max_materialized_processing_jobs is None else max_materialized_processing_jobs)
            ratio = Decimal(str(current["warning_ratio"] if warning_ratio is None else warning_ratio))
            if hourly <= 0 or daily <= 0 or monthly <= 0 or daily < hourly or monthly < daily:
                raise ValueError("budget caps must be positive and hourly <= daily <= monthly")
            if min(paid_per_hour, paid_concurrency, role_concurrency, materialized_limit) < 1:
                raise ValueError("throughput limits must be positive")
            if ratio <= 0 or ratio >= 1:
                raise ValueError("warning_ratio must be between 0 and 1")
            epoch = int(current["enable_epoch"])
            policy_epoch = int(current["policy_epoch"])
            enabled_at = current["enabled_at"]
            if next_master and not bool(current["master_enabled"]):
                epoch += 1
                enabled_at = iso_utc(now)
            if next_master != bool(current["master_enabled"]) or next_mode != str(current["mode"]):
                policy_epoch += 1
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_settings
                SET master_enabled=?, mode=?, enable_epoch=?, policy_epoch=?, enabled_at=?,
                    daily_cap_usd=?, monthly_cap_usd=?, hourly_cap_usd=?,
                    max_paid_reviews_per_hour=?, global_paid_review_concurrency=?,
                    max_inflight_role_calls=?, max_materialized_processing_jobs=?,
                    warning_ratio=?, updated_at=?
                WHERE singleton=1
                """,
                (
                    int(next_master), next_mode, epoch, policy_epoch, enabled_at,
                    str(daily), str(monthly), str(hourly), paid_per_hour,
                    paid_concurrency, role_concurrency, materialized_limit,
                    str(ratio), iso_utc(now),
                ),
            )
            if policy_epoch != int(current["policy_epoch"]):
                stale_publications = conn.execute(
                    """
                    SELECT publication_key,processing_key,state
                    FROM sheet_vitrina_v1_wb_publication_jobs
                    WHERE policy_epoch<>? AND write_started_at IS NULL
                      AND state IN (?,?)
                    """,
                    (policy_epoch, STATE_APPROVED, STATE_PUBLISHING),
                ).fetchall()
                for publication in stale_publications:
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_wb_publication_jobs
                        SET state=?, last_error_code='policy_epoch_stale',
                            lease_owner=NULL, lease_until=NULL, updated_at=?
                        WHERE publication_key=?
                        """,
                        (STATE_NEEDS_REVIEW, iso_utc(now), publication["publication_key"]),
                    )
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                        SET state=?, last_error_code='policy_epoch_stale', updated_at=?
                        WHERE processing_key=? AND state<>?
                        """,
                        (
                            STATE_NEEDS_REVIEW,
                            iso_utc(now),
                            publication["processing_key"],
                            STATE_PUBLISHED,
                        ),
                    )
                    self._audit(
                        conn,
                        aggregate_type="publication_job",
                        aggregate_id=str(publication["publication_key"]),
                        event_type="publication_paused_by_policy_change",
                        actor_type="user",
                        actor_id=actor,
                        details={"policy_epoch": policy_epoch, "mode": next_mode},
                        at=now,
                        previous_state=str(publication["state"]),
                        next_state=STATE_NEEDS_REVIEW,
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
                    "policy_epoch": policy_epoch,
                    "daily_cap_usd": str(daily),
                    "monthly_cap_usd": str(monthly),
                    "hourly_cap_usd": str(hourly),
                    "max_paid_reviews_per_hour": paid_per_hour,
                    "global_paid_review_concurrency": paid_concurrency,
                    "max_inflight_role_calls": role_concurrency,
                    "max_materialized_processing_jobs": materialized_limit,
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

    def record_scheduler_tick(self, *, errors: Sequence[Mapping[str, Any]]) -> None:
        now = self._now()
        with self.transaction() as conn:
            current = conn.execute(
                """
                SELECT stop_reason,stop_details_json
                FROM sheet_vitrina_v1_wb_autoanswers_runtime_state
                WHERE singleton=1
                """
            ).fetchone()
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_runtime_state
                SET last_scheduler_tick_at=?, updated_at=? WHERE singleton=1
                """,
                (iso_utc(now), iso_utc(now)),
            )
            if errors:
                code = _clean_text(errors[0].get("code"))
                lower_code = code.casefold()
                reason = (
                    "budget_state_unknown"
                    if code in {"node_timeout", "node_invalid_json"} or code.startswith("node_process_exit_")
                    else "openai_quota_exhausted" if "insufficient_quota" in lower_code
                    else "rate_limited" if "429" in code
                    else "retry_backoff" if bool(errors[0].get("retryable"))
                    else "worker_error"
                )
                # Unknown provider cost and exhausted quota are global paid
                # processing latches.  A later sync/publication error in the
                # same scheduler tick must not accidentally clear or obscure
                # the stronger fail-closed reason.
                current_reason = str(current["stop_reason"] or "") if current is not None else ""
                if current_reason not in {"budget_state_unknown", "openai_quota_exhausted"}:
                    self._set_stop_reason(
                        conn,
                        reason,
                        details={
                            "code": code,
                            "stage": _clean_text(errors[0].get("stage")),
                        },
                        at=now,
                    )
            elif current is not None and str(current["stop_reason"] or "") == "worker_error":
                try:
                    current_details = json.loads(str(current["stop_details_json"] or "{}"))
                except json.JSONDecodeError:
                    current_details = {}
                if (
                    str(current_details.get("code") or "") == "publication_already_exists"
                    and str(current_details.get("stage") or "") in {"", "reconciliation"}
                ):
                    settings = conn.execute(
                        "SELECT policy_epoch FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
                    ).fetchone()
                    sweep = (
                        conn.execute(
                            """
                            SELECT sweep_id,transition_run_id
                            FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
                            WHERE policy_epoch=?
                            ORDER BY created_at DESC
                            LIMIT 1
                            """,
                            (int(settings["policy_epoch"]),),
                        ).fetchone()
                        if settings is not None
                        else None
                    )
                    remaining_conflicts = 1
                    if sweep is not None:
                        remaining_conflicts = int(
                            conn.execute(
                                """
                                SELECT COUNT(*)
                                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope rs
                                JOIN sheet_vitrina_v1_wb_feedbacks f
                                  ON f.feedback_id=rs.feedback_id
                                 AND f.content_version=rs.content_version_at_preview
                                 AND f.content_version_hash=rs.content_version_hash_at_preview
                                JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
                                  ON j.feedback_id=f.feedback_id
                                 AND j.content_version=f.content_version
                                 AND j.bundle_version=?
                                JOIN sheet_vitrina_v1_wb_publication_jobs p
                                  ON p.processing_key=j.processing_key
                                WHERE rs.sweep_id=?
                                  AND COALESCE(f.answer_text,'')=''
                                  AND COALESCE(j.policy_epoch,-1)<>?
                                  AND (COALESCE(j.regeneration_required,0)=1
                                       OR COALESCE(j.media_uncertain,0)=1)
                                  AND p.write_started_at IS NULL
                                """,
                                (
                                    PROMPT_BUNDLE_VERSION,
                                    sweep["sweep_id"],
                                    int(settings["policy_epoch"]),
                                ),
                            ).fetchone()[0]
                        )
                    active_reservations = int(
                        conn.execute(
                            """
                            SELECT COUNT(*)
                            FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
                            WHERE status='reserved'
                            """
                        ).fetchone()[0]
                    )
                    unresolved_uncertainty = len(
                        self._budget_uncertainty_candidates(conn)
                    )
                    if (
                        remaining_conflicts == 0
                        and active_reservations == 0
                        and unresolved_uncertainty == 0
                    ):
                        recovery_details = {
                            "recovered_code": "publication_already_exists",
                            "transition_run_id": str(
                                sweep["transition_run_id"] or sweep["sweep_id"]
                            ),
                            "policy_epoch": int(settings["policy_epoch"]),
                            "clean_scheduler_tick_at": iso_utc(now),
                            "remaining_publication_bound_conflicts": 0,
                            "active_reservations": 0,
                            "unresolved_uncertainty": 0,
                        }
                        self._set_stop_reason(
                            conn,
                            None,
                            details=recovery_details,
                            at=now,
                        )
                        self._audit(
                            conn,
                            aggregate_type="runtime",
                            aggregate_id="singleton",
                            event_type="publication_conflict_worker_latch_reconciled",
                            actor_type="worker",
                            actor_id="scheduler",
                            details=recovery_details,
                            at=now,
                            previous_state="worker_error",
                            next_state="running",
                        )

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
        content_classification = classify_feedback_content(
            canonical_json(content),
            has_photo=any(item["kind"] == "photo" for item in media),
            has_video=any(item["kind"] == "video" for item in media),
            canonical_media_present=bool(media),
        )
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
            active_sweep = conn.execute(
                """
                SELECT sweep_id,transition_run_id
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
                WHERE policy_epoch=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (int(settings["policy_epoch"]),),
            ).fetchone()
            is_new = current is None
            content_changed = is_new or str(current["content_version_hash"]) != content_hash
            observation_changed = is_new or str(current["wb_observation_hash"]) != observation_hash
            content_version = 1 if is_new else int(current["content_version"]) + int(content_changed)
            effective_on = bool(settings["master_enabled"]) and not _force_off_from_env(self.env)
            eligible_epoch: int | None = None
            automatic_mode = str(settings["mode"]) != MODE_MANUAL
            if (
                is_new
                and run_kind == "steady"
                and effective_on
                and automatic_mode
                and not answer_text
                and active_sweep is None
            ):
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
                    , content_classification
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    last_sync_run_id=excluded.last_sync_run_id,
                    content_classification=excluded.content_classification
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
                    content_classification,
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
            "content_classification": content_classification,
            "auto_eligible_epoch": eligible_epoch,
            "auto_enqueue": bool(
                run_kind == "steady"
                and content_changed
                and eligible_epoch is not None
                and effective_on
                and automatic_mode
                and active_sweep is None
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
            regeneration_required = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE regeneration_required=1"
                ).fetchone()[0]
            )
            sweep_rows = conn.execute(
                "SELECT state, COUNT(*) AS count FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps GROUP BY state"
            ).fetchall()
            settings_row = conn.execute(
                "SELECT master_enabled,mode,policy_epoch FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
            ).fetchone()
            claimable_ai_jobs = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswer_jobs
                    WHERE policy_epoch=? AND state IN (?,?,?)
                      AND (?<>? OR trigger_source='manual_generate')
                    """,
                    (
                        int(settings_row["policy_epoch"]),
                        STATE_QUEUED,
                        STATE_PROCESSING,
                        STATE_RETRYABLE_ERROR,
                        str(settings_row["mode"]),
                        MODE_MANUAL,
                    ),
                ).fetchone()[0]
            )
            claimable_publication_writes = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs
                    WHERE policy_epoch=? AND write_started_at IS NULL AND state IN (?,?)
                    """,
                    (int(settings_row["policy_epoch"]), STATE_APPROVED, STATE_PUBLISHING),
                ).fetchone()[0]
            )
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
                "policy_epoch": settings.policy_epoch,
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
            "claimable_ai_jobs": claimable_ai_jobs,
            "claimable_publication_writes": claimable_publication_writes,
            "regeneration_required": regeneration_required,
            "reconciliation_sweeps": {str(row["state"]): int(row["count"]) for row in sweep_rows},
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
            "progress": self.progress_status(),
        }

    def progress_status(self) -> dict[str, Any]:
        """Build queue and progress evidence entirely from the local database."""

        settings = self.settings()
        now = self._now()
        hour_start = iso_utc(now - timedelta(hours=1))
        with closing(self._connect()) as conn:
            sweep = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            scope_from = str(sweep["scope_from"]) if sweep is not None else BACKFILL_FROM_DATE
            scope_to = str(sweep["scope_to"]) if sweep is not None and sweep["scope_to"] else None
            membership_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope WHERE sweep_id=?",
                    (str(sweep["sweep_id"]) if sweep is not None else "",),
                ).fetchone()[0]
            )
            scope_exact = bool(sweep is not None and membership_count > 0)
            progress_policy_epoch = int(sweep["policy_epoch"]) if sweep is not None else settings.policy_epoch
            if scope_exact:
                scope_clause = (
                    "EXISTS(SELECT 1 FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope rs "
                    "WHERE rs.sweep_id=? AND rs.feedback_id=f.feedback_id)"
                )
                scope_params: list[Any] = [str(sweep["sweep_id"])]
            else:
                scope_clause = "substr(COALESCE(f.created_at_wb,f.first_seen_at),1,10)>=?"
                scope_params = [scope_from]
                if scope_to:
                    scope_clause += " AND substr(COALESCE(f.created_at_wb,f.first_seen_at),1,10)<=?"
                    scope_params.append(scope_to)
            if scope_exact:
                grouped_scope_join = (
                    "JOIN sheet_vitrina_v1_wb_autoanswers_reconciliation_scope prs "
                    "ON prs.sweep_id=? AND prs.feedback_id=f.feedback_id"
                )
                grouped_scope_clause = "1=1"
                grouped_join_params: list[Any] = [str(sweep["sweep_id"])]
                grouped_where_params: list[Any] = []
                grouped_classification = "prs.content_classification_at_preview"
                grouped_job_version = "prs.content_version_at_preview"
            else:
                grouped_scope_join = ""
                grouped_scope_clause = scope_clause
                grouped_join_params = []
                grouped_where_params = list(scope_params)
                grouped_classification = "f.content_classification"
                grouped_job_version = "f.content_version"
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS scope_total,
                       SUM(CASE WHEN COALESCE(f.answer_text,'')='' THEN 1 ELSE 0 END) AS unanswered,
                       SUM(CASE WHEN COALESCE(f.answer_text,'')<>'' THEN 1 ELSE 0 END) AS wb_answered,
                       SUM(CASE WHEN j.final_reply IS NOT NULL THEN 1 ELSE 0 END) AS system_reply_created,
                       SUM(CASE WHEN COALESCE(f.answer_text,'')='' AND j.final_reply IS NOT NULL THEN 1 ELSE 0 END) AS system_reply_created_unanswered,
                       SUM(CASE WHEN j.final_reply IS NULL THEN 1 ELSE 0 END) AS system_reply_missing,
                       SUM(CASE WHEN COALESCE(f.answer_text,'')='' AND (j.processing_key IS NULL OR COALESCE(j.policy_epoch,-1)<>?) THEN 1 ELSE 0 END) AS awaiting_materialization,
                       SUM(CASE WHEN j.state='queued' THEN 1 ELSE 0 END) AS processing_queue,
                       SUM(CASE WHEN j.state='processing' THEN 1 ELSE 0 END) AS processing_now,
                       SUM(CASE WHEN j.state='processing' AND j.lease_until<=? THEN 1 ELSE 0 END) AS stale_leases,
                       SUM(CASE WHEN j.state='retryable_error' AND j.retry_stage='processing' THEN 1 ELSE 0 END) AS retry_backoff,
                       SUM(CASE WHEN j.state='needs_review' THEN 1 ELSE 0 END) AS needs_review,
                       SUM(CASE WHEN j.state='approved' THEN 1 ELSE 0 END) AS ready_for_publication,
                       SUM(CASE WHEN p.state='approved' THEN 1 ELSE 0 END) AS publication_queue,
                       SUM(CASE WHEN p.state='publish_pending_readback' OR (p.state='retryable_error' AND p.retry_stage='readback') THEN 1 ELSE 0 END) AS readback_pending,
                       SUM(CASE WHEN p.state='published' THEN 1 ELSE 0 END) AS published_confirmed,
                       SUM(CASE WHEN j.state='terminal_error' OR p.state='terminal_error' THEN 1 ELSE 0 END) AS errors,
                       SUM(CASE WHEN j.processing_kind=? THEN 1 ELSE 0 END) AS zero_cost_template_jobs,
                       SUM(CASE WHEN j.completed_at>=? THEN 1 ELSE 0 END) AS completed_last_hour,
                       COUNT(j.processing_key) AS materialized_jobs
                FROM sheet_vitrina_v1_wb_feedbacks f
                LEFT JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
                  ON j.feedback_id=f.feedback_id AND j.content_version=f.content_version
                 AND j.bundle_version=? AND j.policy_epoch=?
                LEFT JOIN sheet_vitrina_v1_wb_publication_jobs p ON p.processing_key=j.processing_key
                WHERE {scope_clause}
                """,
                [
                    progress_policy_epoch,
                    iso_utc(now),
                    PROCESSING_KIND_RATING_ONLY_TEMPLATE,
                    hour_start,
                    PROMPT_BUNDLE_VERSION,
                    progress_policy_epoch,
                    *scope_params,
                ],
            ).fetchone()
            runtime = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_runtime_state WHERE singleton=1"
            ).fetchone()
            last_sync = conn.execute(
                "SELECT MAX(last_success_at) FROM sheet_vitrina_v1_wb_sync_state"
            ).fetchone()[0]
            run_spend = conn.execute(
                """
                SELECT
                    (SELECT COALESCE(SUM(CAST(actual_cost_usd AS REAL)),0)
                     FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
                     WHERE transition_run_id=?)
                    +
                    (SELECT COALESCE(SUM(CAST(actual_cost_usd AS REAL)),0)
                     FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events
                     WHERE transition_run_id=?) AS actual,
                    (SELECT COALESCE(SUM(CASE WHEN status='reserved' THEN CAST(reserved_usd AS REAL) ELSE 0 END),0)
                     FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
                     WHERE transition_run_id=?) AS reserved,
                    (SELECT COALESCE(SUM(CAST(upper_bound_usd AS REAL)),0)
                     FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds
                     WHERE transition_run_id=?) AS uncertainty
                """,
                (
                    str(sweep["transition_run_id"]) if sweep is not None else "",
                    str(sweep["transition_run_id"]) if sweep is not None else "",
                    str(sweep["transition_run_id"]) if sweep is not None else "",
                    str(sweep["transition_run_id"]) if sweep is not None else "",
                ),
            ).fetchone()
            prepared_sql = f"""
                p.state='{STATE_PUBLISHED}' OR (
                    j.final_reply IS NOT NULL
                    AND j.content_version=f.content_version
                    AND j.content_version_hash=f.content_version_hash
                    AND COALESCE(j.regeneration_required,0)=0
                    AND COALESCE(j.media_uncertain,0)=0
                    AND COALESCE(j.fallback_used,0)=0
                    AND COALESCE(j.hard_gates_passed,0)=1
                    AND COALESCE(j.node_contract_valid,0)=1
                    AND j.state<>'{STATE_NEEDS_REVIEW}'
                    AND j.state<>'{STATE_TERMINAL_ERROR}'
                )
            """
            grouped = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS all_total,
                    SUM(CASE WHEN {grouped_classification}=? THEN 1 ELSE 0 END) AS content_total,
                    SUM(CASE WHEN {grouped_classification}=? THEN 1 ELSE 0 END) AS rating_total,
                    SUM(CASE WHEN {grouped_classification}=? THEN 1 ELSE 0 END) AS indeterminate_total,
                    SUM(CASE WHEN {prepared_sql} THEN 1 ELSE 0 END) AS all_prepared,
                    SUM(CASE WHEN {grouped_classification}=? AND ({prepared_sql}) THEN 1 ELSE 0 END) AS content_prepared,
                    SUM(CASE WHEN {grouped_classification}=? AND ({prepared_sql}) THEN 1 ELSE 0 END) AS rating_prepared,
                    SUM(CASE WHEN p.state=? THEN 1 ELSE 0 END) AS all_published,
                    SUM(CASE WHEN {grouped_classification}=? AND p.state=? THEN 1 ELSE 0 END) AS content_published,
                    SUM(CASE WHEN {grouped_classification}=? AND p.state=? THEN 1 ELSE 0 END) AS rating_published,
                    SUM(CASE WHEN {grouped_classification}=? AND j.state=? THEN 1 ELSE 0 END) AS content_needs_review,
                    SUM(CASE WHEN {grouped_classification}=? AND (
                        j.state=? OR j.state=? OR j.state=? OR j.state=? OR
                        p.state=? OR p.state=?
                    ) THEN 1 ELSE 0 END) AS content_active,
                    SUM(CASE WHEN {grouped_classification}=? AND j.completed_at>=? THEN 1 ELSE 0 END) AS content_completed_last_hour,
                    SUM(CASE WHEN COALESCE(f.answer_text,'')<>'' AND COALESCE(p.state,'')<>? THEN 1 ELSE 0 END) AS external_answer,
                    SUM(CASE WHEN {grouped_classification}=? AND COALESCE(f.answer_text,'')<>'' AND COALESCE(p.state,'')<>? THEN 1 ELSE 0 END) AS content_external_answer,
                    SUM(CASE WHEN {grouped_classification}=? AND (
                        COALESCE(j.regeneration_required,0)=1 OR
                        (j.processing_key IS NOT NULL AND j.content_version_hash<>f.content_version_hash)
                    ) THEN 1 ELSE 0 END) AS content_stale_or_regeneration,
                    SUM(CASE WHEN {grouped_classification}=? AND (
                        p.state=? OR (p.state=? AND p.retry_stage='readback')
                    ) THEN 1 ELSE 0 END) AS content_readback_pending
                FROM sheet_vitrina_v1_wb_feedbacks f
                {grouped_scope_join}
                LEFT JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
                  ON j.feedback_id=f.feedback_id AND j.content_version={grouped_job_version}
                 AND j.bundle_version=?
                LEFT JOIN sheet_vitrina_v1_wb_publication_jobs p
                  ON p.processing_key=j.processing_key
                WHERE {grouped_scope_clause}
                """,
                [
                    CONTENT_CLASS_CONTENT_BEARING,
                    CONTENT_CLASS_RATING_ONLY,
                    CONTENT_CLASS_INDETERMINATE,
                    CONTENT_CLASS_CONTENT_BEARING,
                    CONTENT_CLASS_RATING_ONLY,
                    STATE_PUBLISHED,
                    CONTENT_CLASS_CONTENT_BEARING,
                    STATE_PUBLISHED,
                    CONTENT_CLASS_RATING_ONLY,
                    STATE_PUBLISHED,
                    CONTENT_CLASS_CONTENT_BEARING,
                    STATE_NEEDS_REVIEW,
                    CONTENT_CLASS_CONTENT_BEARING,
                    STATE_QUEUED,
                    STATE_PROCESSING,
                    STATE_RETRYABLE_ERROR,
                    STATE_APPROVED,
                    STATE_PUBLISHING,
                    STATE_PUBLISH_PENDING_READBACK,
                    CONTENT_CLASS_CONTENT_BEARING,
                    hour_start,
                    STATE_PUBLISHED,
                    CONTENT_CLASS_CONTENT_BEARING,
                    STATE_PUBLISHED,
                    CONTENT_CLASS_CONTENT_BEARING,
                    CONTENT_CLASS_CONTENT_BEARING,
                    STATE_PUBLISH_PENDING_READBACK,
                    STATE_RETRYABLE_ERROR,
                    *grouped_join_params,
                    PROMPT_BUNDLE_VERSION,
                    *grouped_where_params,
                ],
            ).fetchone()
            outside_current_run = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM sheet_vitrina_v1_wb_feedbacks f
                    WHERE COALESCE(f.answer_text,'')=''
                      AND NOT EXISTS(
                        SELECT 1 FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope rs
                        WHERE rs.sweep_id=? AND rs.feedback_id=f.feedback_id
                          AND rs.content_version_at_preview=f.content_version
                      )
                    """,
                    (str(sweep["sweep_id"]) if sweep is not None else "",),
                ).fetchone()[0]
            )
            automatic_content_pending = self._automatic_content_pending_count(
                conn,
                transition_run_id=(
                    str(sweep["transition_run_id"] or sweep["sweep_id"])
                    if sweep is not None
                    else None
                ),
                policy_epoch=progress_policy_epoch,
                target_mode=(str(sweep["target_mode"]) if sweep is not None else settings.mode),
            )
        counters = {key: int(row[key] or 0) for key in row.keys()}
        if sweep is not None and not scope_exact:
            # Schema-v3 sweeps stored an immutable hash/count but not members.
            # Use that exact total and current policy-epoch aggregates without
            # pretending that post-preview history membership is recoverable.
            totals = json.loads(str(sweep["totals_json"] or "{}"))
            legacy_total = int(totals.get("unanswered_total") or counters["scope_total"])
            counters["scope_total"] = legacy_total
            counters["wb_answered"] = counters["published_confirmed"]
            counters["unanswered"] = max(0, legacy_total - counters["wb_answered"])
            counters["system_reply_missing"] = max(
                0, legacy_total - counters["system_reply_created"]
            )
            counters["awaiting_materialization"] = max(
                0, legacy_total - counters["materialized_jobs"]
            )
        grouped_counters = {key: int(grouped[key] or 0) for key in grouped.keys()}
        counters["scope_total"] = grouped_counters["all_total"]
        preparation_done = grouped_counters["all_prepared"]
        remaining = max(0, grouped_counters["all_total"] - preparation_done)
        throughput = counters["completed_last_hour"]
        eta = round(remaining / throughput, 1) if throughput > 0 else None
        stop_reason = str(runtime["stop_reason"] or "") if runtime is not None else ""
        if not settings.effective_enabled:
            stop_reason = "emergency_stop" if settings.force_off else "master_switch_off"
        elif settings.mode == MODE_MANUAL:
            stop_reason = "manual_pause"
        elif stop_reason in {
            "hourly_budget_reached",
            "daily_budget_reached",
            "monthly_budget_reached",
            "run_budget_reached",
            "run_review_limit_reached",
            "run_cap_missing",
            "paid_reviews_hourly_limit",
            "concurrency_limit",
            "openai_quota_exhausted",
            "budget_state_unknown",
            "rate_limited",
            "retry_backoff",
            "worker_error",
        }:
            pass
        elif sweep is not None and sweep["pause_reason"]:
            stop_reason = str(sweep["pause_reason"])
        elif counters["stale_leases"] > 0:
            stop_reason = "stale_lease"
        elif settings.mode != MODE_MANUAL:
            last_tick = parse_timestamp(runtime["last_scheduler_tick_at"] if runtime is not None else None)
            if last_tick is None or last_tick < now - timedelta(minutes=3):
                stop_reason = "worker_unavailable"
        all_total = grouped_counters["all_total"]
        content_total = grouped_counters["content_total"]
        rating_total = grouped_counters["rating_total"]
        all_prepared = grouped_counters["all_prepared"]
        content_prepared = grouped_counters["content_prepared"]
        rating_prepared = grouped_counters["rating_prepared"]
        all_published = grouped_counters["all_published"]
        content_published = grouped_counters["content_published"]
        content_preparation_remaining = max(0, content_total - content_prepared)
        content_publication_remaining = max(0, content_total - content_published)
        content_speed = grouped_counters["content_completed_last_hour"]
        content_eta = (
            round(content_preparation_remaining / content_speed, 1)
            if content_speed > 0
            else None
        )
        if settings.mode == MODE_MANUAL:
            current_operation = "paused_manual"
        elif grouped_counters["content_readback_pending"] > 0:
            current_operation = "content_readback"
        elif grouped_counters["content_active"] > 0:
            current_operation = "content_processing"
        elif automatic_content_pending > 0:
            current_operation = "content_waiting"
        elif rating_total > rating_prepared:
            current_operation = "rating_only_waiting"
        else:
            current_operation = "complete"
        pause_reason = stop_reason or "no_eligible_jobs"
        all_status = "paused_manual" if settings.mode == MODE_MANUAL else current_operation
        content_status = "paused_manual" if settings.mode == MODE_MANUAL else current_operation
        return {
            **counters,
            "preparation_done": preparation_done,
            "remaining": remaining,
            "preparation_percent": _progress_percent(all_prepared, all_total),
            "publication_percent": _progress_percent(all_published, all_total),
            "all_preparation": {
                "done": all_prepared,
                "total": all_total,
                "remaining": max(0, all_total - all_prepared),
                "percent": _progress_percent(all_prepared, all_total),
                "status": all_status,
                "pause_reason": pause_reason,
            },
            "all_publication": {
                "done": all_published,
                "total": all_total,
                "remaining": max(0, all_total - all_published),
                "percent": _progress_percent(all_published, all_total),
                "status": all_status,
                "pause_reason": pause_reason,
            },
            "content_bearing_preparation": {
                "done": content_prepared,
                "total": content_total,
                "remaining": content_preparation_remaining,
                "percent": _progress_percent(content_prepared, content_total),
                "needs_review": grouped_counters["content_needs_review"],
                "current_operation": current_operation,
                "pause_reason": pause_reason,
                "eta_hours": content_eta,
                "throughput_last_hour": content_speed,
            },
            "content_bearing_publication": {
                "done": content_published,
                "total": content_total,
                "remaining": content_publication_remaining,
                "percent": _progress_percent(content_published, content_total),
                "needs_review": grouped_counters["content_needs_review"],
                "current_operation": current_operation,
                "pause_reason": pause_reason,
                "eta_hours": content_eta,
                "throughput_last_hour": content_speed,
            },
            "content_bearing_total": content_total,
            "rating_only_total": rating_total,
            "indeterminate_total": grouped_counters["indeterminate_total"],
            "content_bearing_remaining": content_preparation_remaining,
            "content_bearing_needs_review": grouped_counters["content_needs_review"],
            "rating_only_remaining": max(0, rating_total - rating_prepared),
            "outside_current_run": outside_current_run,
            "external_answer": grouped_counters["external_answer"],
            "content_bearing_external_answer": grouped_counters["content_external_answer"],
            "content_bearing_stale_or_regeneration": grouped_counters["content_stale_or_regeneration"],
            "automatic_content_pending": automatic_content_pending,
            "current_operation": current_operation,
            "effective_mode": settings.mode if settings.effective_enabled else "off",
            "policy_epoch": settings.policy_epoch,
            "scope": {"from": scope_from, "to": scope_to},
            "scope_membership_exact": scope_exact,
            "transition_run_id": str(sweep["transition_run_id"]) if sweep is not None else None,
            "run_actual_usd": float(run_spend["actual"] or 0),
            "run_active_reserved_usd": float(run_spend["reserved"] or 0),
            "run_uncertainty_hold_usd": float(run_spend["uncertainty"] or 0),
            "throughput_last_hour": throughput,
            "eta_hours": eta,
            "stop_reason": stop_reason or "no_eligible_jobs",
            "last_sync_at": last_sync,
            "last_scheduler_tick_at": runtime["last_scheduler_tick_at"] if runtime is not None else None,
            "last_successful_ai_call_at": runtime["last_successful_ai_call_at"] if runtime is not None else None,
            "last_confirmed_publication_at": runtime["last_confirmed_publication_at"] if runtime is not None else None,
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
            active_sweep = conn.execute(
                """
                SELECT sweep_id,transition_run_id
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
                WHERE policy_epoch=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (settings.policy_epoch,),
            ).fetchone()
            transition_run_id: str | None = None
            if active_sweep is not None and _clean_text(trigger_source) != "manual_generate":
                membership = conn.execute(
                    """
                    SELECT 1 FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope
                    WHERE sweep_id=? AND feedback_id=?
                      AND content_version_at_preview=?
                      AND content_version_hash_at_preview=?
                    """,
                    (
                        active_sweep["sweep_id"],
                        feedback["feedback_id"],
                        version,
                        feedback["content_version_hash"],
                    ),
                ).fetchone()
                if membership is None:
                    raise AutoanswersRuntimeError(
                        "feedback is outside the immutable transition-run scope",
                        code="outside_transition_run_scope",
                    )
                transition_run_id = str(active_sweep["transition_run_id"] or active_sweep["sweep_id"])
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
                processing_kind = (
                    PROCESSING_KIND_RATING_ONLY_TEMPLATE
                    if str(feedback["content_classification"]) == CONTENT_CLASS_RATING_ONLY
                    else PROCESSING_KIND_FROZEN_AI
                )
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_wb_autoanswer_jobs(
                        processing_key, feedback_id, content_version,
                        content_version_hash, state, trigger_source,
                        bundle_version, evaluation_signature, policy_version,
                        enable_epoch, policy_epoch, processing_kind, manual_started,
                        transition_run_id, available_at, attempts, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
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
                        settings.policy_epoch,
                        processing_kind,
                        int(_clean_text(trigger_source) == "manual_generate"),
                        transition_run_id,
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
            elif allow_history and _clean_text(trigger_source) == "manual_generate":
                # An explicit manual click adopts the one durable aggregate for
                # this exact content version.  It never creates a parallel job,
                # but makes preserved work from an older automatic epoch
                # claimable under the current manual policy.
                if existing["final_reply"]:
                    return dict(existing)
                next_state = (
                    STATE_PROCESSING
                    if str(existing["state"]) == STATE_PROCESSING
                    and parse_timestamp(existing["lease_until"]) is not None
                    and parse_timestamp(existing["lease_until"]) > now
                    else STATE_QUEUED
                )
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                    SET state=?, trigger_source='manual_generate', manual_started=1,
                        enable_epoch=?, policy_epoch=?, policy_version=?, transition_run_id=NULL,
                        available_at=?, last_error_code=NULL,
                        lease_owner=CASE WHEN ?=? THEN lease_owner ELSE NULL END,
                        lease_until=CASE WHEN ?=? THEN lease_until ELSE NULL END,
                        updated_at=?
                    WHERE processing_key=?
                    """,
                    (
                        next_state,
                        settings.enable_epoch,
                        settings.policy_epoch,
                        settings.policy_version,
                        iso_utc(now),
                        next_state,
                        STATE_PROCESSING,
                        next_state,
                        STATE_PROCESSING,
                        iso_utc(now),
                        key,
                    ),
                )
                self._audit(
                    conn,
                    aggregate_type="processing_job",
                    aggregate_id=key,
                    event_type="processing_adopted_for_manual",
                    actor_type="user",
                    actor_id=_clean_text(actor_id),
                    details={
                        "enable_epoch": settings.enable_epoch,
                        "policy_epoch": settings.policy_epoch,
                    },
                    at=now,
                    previous_state=str(existing["state"]),
                    next_state=next_state,
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

    def request_regeneration(
        self,
        processing_key_value: str,
        *,
        actor_id: str,
        trigger_source: str = "manual_generate",
        transition_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Archive an unpublished media-uncertain result and requeue it once.

        The processing key remains stable for the exact content version.  An
        append-only revision preserves the previous output and cost while
        ``media_processing_version`` makes the new attempt auditable.
        """

        settings = self.assert_effective_on(operation="media-aware regeneration")
        if trigger_source == "manual_generate" and settings.mode != MODE_MANUAL:
            raise AutoanswersRuntimeError("manual mode is required", code="manual_mode_required")
        now = self._now()
        key = _clean_text(processing_key_value)
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (key,),
            ).fetchone()
            if job is None:
                raise AutoanswersRuntimeError("processing job not found", code="job_not_found")
            feedback = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
                (job["feedback_id"],),
            ).fetchone()
            if feedback is None or int(feedback["content_version"]) != int(job["content_version"]):
                raise AutoanswersRuntimeError("stale feedback version", code="stale_content_version")
            if feedback["answer_text"]:
                raise AutoanswersRuntimeError("WB already has an answer", code="external_answer_present")
            if str(job["state"]) in {STATE_QUEUED, STATE_PROCESSING, STATE_RETRYABLE_ERROR} and bool(
                job["regeneration_required"]
            ):
                return dict(job)
            publication = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_publication_jobs WHERE processing_key=?",
                (key,),
            ).fetchone()
            if publication is not None:
                raise AutoanswersRuntimeError(
                    "publication aggregate already exists; regeneration is blocked",
                    code="publication_already_exists",
                )
            if not bool(job["media_uncertain"]) and not bool(job["regeneration_required"]):
                raise AutoanswersRuntimeError("result does not require regeneration", code="regeneration_not_required")
            media_version = int(job["media_processing_version"] or 1)
            previous_cost = _money(job["actual_cost_usd"])
            reservation = conn.execute(
                "SELECT status,actual_cost_usd FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations WHERE processing_key=?",
                (key,),
            ).fetchone()
            settled_attempt_cost = (
                _money(reservation["actual_cost_usd"])
                if reservation is not None and str(reservation["status"]) == "settled"
                else Decimal(0)
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswer_job_revisions(
                    revision_id, processing_key, media_processing_version,
                    previous_state, result_json, final_route, final_reply,
                    final_reply_sha256, media_uncertain, actual_cost_usd,
                    reason, archived_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    uuid4().hex,
                    key,
                    media_version,
                    str(job["state"]),
                    job["result_json"],
                    job["final_route"],
                    job["final_reply"],
                    job["final_reply_sha256"],
                    int(bool(job["media_uncertain"])),
                    str(previous_cost),
                    "media_aware_regeneration",
                    iso_utc(now),
                ),
            )
            if settled_attempt_cost > 0:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswers_cost_events(
                        event_id, processing_key, media_processing_version,
                        actual_cost_usd, incurred_at
                    ) VALUES(?,?,?,?,?)
                    """,
                    (uuid4().hex, key, media_version, str(settled_attempt_cost), str(job["completed_at"] or iso_utc(now))),
                )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations
                SET reserved_usd=0, actual_cost_usd=0, status='released', updated_at=?
                WHERE processing_key=?
                """,
                (iso_utc(now), key),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_feedback_media
                SET fetch_status='pending', local_path=NULL, sha256=NULL,
                    mime_type=NULL, byte_size=NULL, preview_local_path=NULL,
                    preview_sha256=NULL, preview_mime_type=NULL,
                    preview_byte_size=NULL, uncertainty_code=NULL, updated_at=?
                WHERE feedback_id=? AND content_version=?
                  AND kind IN ('photo','video') AND fetch_status='fetch_failed'
                """,
                (iso_utc(now), job["feedback_id"], job["content_version"]),
            )
            conn.execute(
                "DELETE FROM sheet_vitrina_v1_wb_feedback_media WHERE feedback_id=? AND content_version=? AND kind='video_frame'",
                (job["feedback_id"], job["content_version"]),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state=?, trigger_source=?, enable_epoch=?, policy_epoch=?,
                    policy_version=?, transition_run_id=?,
                    media_processing_version=?, regeneration_required=1,
                    regeneration_reason='media_fetch_failed', manual_started=?,
                    final_route=NULL, case_code=NULL, final_reply=NULL,
                    final_reply_sha256=NULL, result_json=NULL,
                    review_reasons_json='["regeneration_required"]',
                    hard_gates_passed=NULL, fallback_used=NULL,
                    media_uncertain=NULL, node_contract_valid=NULL,
                    lease_owner=NULL, lease_until=NULL, retry_stage=NULL,
                    last_error_code=NULL, approved_by=NULL, approved_at=NULL,
                    manual_reply=NULL, manual_reply_sha256=NULL,
                    manual_guard_passed=NULL, manual_guard_errors_json=NULL,
                    manual_reviewed_by=NULL, manual_reviewed_at=NULL,
                    manual_edit_revision=0, completed_at=NULL,
                    available_at=?, updated_at=?
                WHERE processing_key=?
                """,
                (
                    STATE_QUEUED,
                    _clean_text(trigger_source),
                    settings.enable_epoch,
                    settings.policy_epoch,
                    settings.policy_version,
                    _clean_text(transition_run_id) or None,
                    media_version + 1,
                    int(trigger_source == "manual_generate" or bool(job["manual_started"])),
                    iso_utc(now),
                    iso_utc(now),
                    key,
                ),
            )
            self._audit(
                conn,
                aggregate_type="processing_job",
                aggregate_id=key,
                event_type="media_regeneration_enqueued",
                actor_type="user" if trigger_source == "manual_generate" else "policy",
                actor_id=_clean_text(actor_id),
                details={"media_processing_version": media_version + 1, "policy_epoch": settings.policy_epoch},
                at=now,
                previous_state=str(job["state"]),
                next_state=STATE_QUEUED,
            )
            return dict(
                conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                    (key,),
                ).fetchone()
            )

    def assert_processing_execution_allowed(self, processing_key_value: str) -> AutoanswersSettings:
        """Recheck mode/version invariants immediately before paid work."""

        settings = self.assert_effective_on(operation="frozen AI invocation")
        with closing(self._connect()) as conn:
            job = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (_clean_text(processing_key_value),),
            ).fetchone()
            if job is None or str(job["state"]) != STATE_PROCESSING:
                raise AutoanswersRuntimeError("processing lease is no longer current", code="processing_lease_stale")
            feedback = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
                (job["feedback_id"],),
            ).fetchone()
        if int(job["enable_epoch"] or 0) != settings.enable_epoch:
            raise AutoanswersRuntimeError("processing enable epoch is stale", code="enable_epoch_stale")
        if int(job["policy_epoch"] or 0) != settings.policy_epoch:
            raise AutoanswersRuntimeError("processing policy epoch is stale", code="policy_epoch_stale")
        if settings.mode == MODE_MANUAL and str(job["trigger_source"] or "") != "manual_generate":
            raise AutoanswersRuntimeError("automatic processing is paused in manual mode", code="manual_pause")
        if feedback is None or int(feedback["content_version"]) != int(job["content_version"]):
            raise AutoanswersRuntimeError("stale feedback version", code="stale_content_version")
        if feedback["answer_text"]:
            raise AutoanswersRuntimeError("WB already has an answer", code="external_answer_present")
        return settings

    @staticmethod
    def _period_bounds(now: datetime) -> tuple[str, str]:
        day = now.astimezone(timezone.utc).date().isoformat()
        month = day[:7]
        return day, month

    @staticmethod
    def _budget_uncertainty_candidates(
        conn: sqlite3.Connection,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT
                r.processing_key,
                r.transition_run_id,
                r.provider_call_started_at,
                r.released_reason,
                r.created_at AS reservation_created_at,
                r.updated_at AS reservation_updated_at,
                j.last_error_code,
                j.attempts
            FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations r
            JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
              ON j.processing_key=r.processing_key
            WHERE r.provider_call_started_at IS NOT NULL
              AND r.status='released'
              AND CAST(COALESCE(r.actual_cost_usd,'0') AS REAL)=0
              AND (
                    j.last_error_code IN ('node_timeout','node_invalid_json')
                    OR j.last_error_code LIKE 'node_process_exit_%'
                  )
              AND NOT EXISTS(
                    SELECT 1
                    FROM sheet_vitrina_v1_wb_autoanswers_cost_events c
                    WHERE c.processing_key=r.processing_key
                  )
              AND NOT EXISTS(
                    SELECT 1
                    FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events f
                    WHERE f.processing_key=r.processing_key
                  )
              AND NOT EXISTS(
                    SELECT 1
                    FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds h
                    WHERE h.processing_key=r.processing_key
                  )
            ORDER BY r.provider_call_started_at,r.processing_key
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def budget_reconciliation_plan(self) -> dict[str, Any]:
        """Build an exact read-only plan for unknown provider-cost boundaries."""

        settings = self.settings()
        with closing(self._connect()) as conn:
            candidates = self._budget_uncertainty_candidates(conn)
            runtime = conn.execute(
                """
                SELECT stop_reason,stop_details_json,updated_at
                FROM sheet_vitrina_v1_wb_autoanswers_runtime_state
                WHERE singleton=1
                """
            ).fetchone()
        upper_bound = _money(settings.max_reservation_per_review_usd)
        holds = [
            {
                **candidate,
                "upper_bound_usd": str(upper_bound),
                "upper_bound_kind": "conservative_contract_hold_not_actual_cost",
                "effective_at": str(candidate["provider_call_started_at"]),
            }
            for candidate in candidates
        ]
        identity = {
            "contract": "wb_autoanswers_budget_reconciliation_v1",
            "policy_epoch": int(settings.policy_epoch),
            "max_reservation_per_review_usd": str(upper_bound),
            "runtime_stop_reason": (
                str(runtime["stop_reason"] or "") if runtime is not None else ""
            ),
            "holds": holds,
        }
        plan_fingerprint = "sha256:" + sha256_text(canonical_json(identity))
        return {
            **identity,
            "plan_fingerprint": plan_fingerprint,
            "pre_change_digest": plan_fingerprint,
            "candidate_count": len(holds),
            "expected_affected_records": {
                "uncertainty_holds_inserted": len(holds),
                "audit_events_appended": len(holds),
                "runtime_state_rows_updated": 1 if holds else 0,
                "provider_calls_created": 0,
                "cost_events_created": 0,
                "wb_writes_created": 0,
            },
            "non_target_invariants": {
                "provider_calls_unchanged": True,
                "cost_events_unchanged": True,
                "wb_writes_unchanged": True,
                "reservation_and_job_evidence_unchanged": True,
            },
            "reversibility": {
                "kind": "append_only_conservative_accounting",
                "backup_required": False,
                "reason": (
                    "The exact reservation/job evidence remains immutable; "
                    "apply only appends conservative holds and audit, never "
                    "deletes evidence or asserts actual provider cost."
                ),
            },
            "captured_at": iso_utc(self._now()),
            "runtime": {
                "stop_reason": (
                    str(runtime["stop_reason"] or "") if runtime is not None else ""
                ),
                "stop_details": (
                    json.loads(str(runtime["stop_details_json"] or "{}"))
                    if runtime is not None
                    else {}
                ),
                "updated_at": (
                    str(runtime["updated_at"] or "") if runtime is not None else ""
                ),
            },
        }

    def _applied_budget_reconciliation_readback(
        self,
        conn: sqlite3.Connection,
        *,
        expected_fingerprint: str,
    ) -> dict[str, Any] | None:
        """Return exact prior-apply proof only when no uncertainty remains."""

        if self._budget_uncertainty_candidates(conn):
            return None
        runtime = conn.execute(
            """
            SELECT stop_reason
            FROM sheet_vitrina_v1_wb_autoanswers_runtime_state
            WHERE singleton=1
            """
        ).fetchone()
        if (
            runtime is not None
            and str(runtime["stop_reason"] or "") == "budget_state_unknown"
        ):
            return None
        rows = conn.execute(
            """
            SELECT aggregate_id,details_json,created_at
            FROM sheet_vitrina_v1_wb_autoanswers_audit_events
            WHERE aggregate_type='budget_uncertainty'
              AND event_type='conservative_uncertainty_hold_appended'
            ORDER BY created_at,aggregate_id
            """
        ).fetchall()
        matching: list[sqlite3.Row] = []
        for row in rows:
            try:
                details = json.loads(str(row["details_json"] or "{}"))
            except (TypeError, ValueError):
                continue
            if (
                isinstance(details, Mapping)
                and str(details.get("plan_fingerprint") or "")
                == expected_fingerprint
            ):
                matching.append(row)
        if not matching:
            return None
        hold_ids = [str(row["aggregate_id"]) for row in matching]
        placeholders = ",".join("?" for _ in hold_ids)
        persisted_holds = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds
                WHERE hold_id IN ({placeholders})
                """,
                hold_ids,
            ).fetchone()[0]
        )
        if persisted_holds != len(hold_ids):
            return None
        return {
            "hold_count": len(hold_ids),
            "hold_ids": hold_ids,
            "confirmed_at": max(str(row["created_at"] or "") for row in matching),
        }

    def apply_budget_reconciliation(
        self,
        *,
        expected_fingerprint: str,
        actor_id: str,
    ) -> dict[str, Any]:
        """Append conservative holds; never label an unknown amount as spend."""

        actor = _clean_text(actor_id)
        if not actor:
            raise ValueError("actor_id is required")
        plan = self.budget_reconciliation_plan()
        fingerprint = _clean_text(expected_fingerprint)
        if str(plan["plan_fingerprint"]) != fingerprint:
            with self.transaction() as conn:
                replay = self._applied_budget_reconciliation_readback(
                    conn,
                    expected_fingerprint=fingerprint,
                )
            if replay is not None:
                return {
                    "status": "already_reconciled",
                    "idempotent": True,
                    "plan_fingerprint": fingerprint,
                    "holds_appended": 0,
                    "previous_holds_appended": int(replay["hold_count"]),
                    "affected_records": {
                        "uncertainty_holds_inserted": 0,
                        "audit_events_appended": 0,
                        "runtime_state_rows_updated": 0,
                        "provider_calls_created": 0,
                        "cost_events_created": 0,
                        "wb_writes_created": 0,
                    },
                    "non_target_invariants_preserved": True,
                    "prior_apply": replay,
                    "budget": self.budget_status(),
                    "readback": self.budget_reconciliation_status(),
                }
            raise AutoanswersRuntimeError(
                "budget reconciliation evidence changed; create a new plan",
                code="budget_reconciliation_stale",
            )
        if not int(plan["candidate_count"]):
            raise AutoanswersRuntimeError(
                "budget reconciliation has no unresolved provider boundary",
                code="budget_reconciliation_evidence_missing",
            )
        if str(plan["runtime"].get("stop_reason") or "") != "budget_state_unknown":
            raise AutoanswersRuntimeError(
                "budget reconciliation cannot clear a different runtime stop reason",
                code="budget_reconciliation_stop_reason_changed",
            )
        now = self._now()
        with self.transaction() as conn:
            current = self._budget_uncertainty_candidates(conn)
            current_settings = conn.execute(
                """
                SELECT policy_epoch,max_reservation_per_review_usd
                FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1
                """
            ).fetchone()
            current_runtime = conn.execute(
                """
                SELECT stop_reason
                FROM sheet_vitrina_v1_wb_autoanswers_runtime_state WHERE singleton=1
                """
            ).fetchone()
            if current_settings is None:
                raise AutoanswersRuntimeError(
                    "Autoanswers settings are missing",
                    code="settings_missing",
                )
            current_holds = [
                {
                    **candidate,
                    "upper_bound_usd": str(
                        _money(current_settings["max_reservation_per_review_usd"])
                    ),
                    "upper_bound_kind": (
                        "conservative_contract_hold_not_actual_cost"
                    ),
                    "effective_at": str(candidate["provider_call_started_at"]),
                }
                for candidate in current
            ]
            identity = {
                "contract": "wb_autoanswers_budget_reconciliation_v1",
                "policy_epoch": int(current_settings["policy_epoch"]),
                "max_reservation_per_review_usd": str(
                    _money(current_settings["max_reservation_per_review_usd"])
                ),
                "runtime_stop_reason": (
                    str(current_runtime["stop_reason"] or "")
                    if current_runtime is not None
                    else ""
                ),
                "holds": current_holds,
            }
            current_fingerprint = "sha256:" + sha256_text(
                canonical_json(identity)
            )
            if current_fingerprint != fingerprint:
                raise AutoanswersRuntimeError(
                    "budget reconciliation evidence changed during apply",
                    code="budget_reconciliation_stale",
                )
            if (
                current_runtime is None
                or str(current_runtime["stop_reason"] or "")
                != "budget_state_unknown"
            ):
                raise AutoanswersRuntimeError(
                    "budget reconciliation runtime stop reason changed during apply",
                    code="budget_reconciliation_stop_reason_changed",
                )
            for hold in current_holds:
                hold_id = "uncertainty:" + sha256_text(
                    f"{hold['processing_key']}:{hold['provider_call_started_at']}"
                )
                evidence = {
                    key: hold.get(key)
                    for key in (
                        "provider_call_started_at",
                        "released_reason",
                        "reservation_created_at",
                        "reservation_updated_at",
                        "last_error_code",
                        "attempts",
                        "upper_bound_kind",
                    )
                }
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds(
                        hold_id,processing_key,transition_run_id,upper_bound_usd,
                        effective_at,reason,evidence_json,created_by,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        hold_id,
                        hold["processing_key"],
                        hold["transition_run_id"],
                        hold["upper_bound_usd"],
                        hold["effective_at"],
                        "provider_boundary_without_usage_readback",
                        canonical_json(evidence),
                        actor,
                        iso_utc(now),
                    ),
                )
                self._audit(
                    conn,
                    aggregate_type="budget_uncertainty",
                    aggregate_id=hold_id,
                    event_type="conservative_uncertainty_hold_appended",
                    actor_type="operator",
                    actor_id=actor,
                    details={
                        "processing_key": hold["processing_key"],
                        "transition_run_id": hold["transition_run_id"],
                        "upper_bound_usd": hold["upper_bound_usd"],
                        "amount_semantics": (
                            "conservative_cap_hold_not_actual_cost"
                        ),
                        "plan_fingerprint": fingerprint,
                    },
                    at=now,
                )
            unresolved = self._budget_uncertainty_candidates(conn)
            if unresolved:
                raise AutoanswersRuntimeError(
                    "budget uncertainty remains after reconciliation",
                    code="budget_reconciliation_incomplete",
                )
            self._set_stop_reason(
                conn,
                None,
                details={
                    "budget_reconciliation_fingerprint": fingerprint,
                    "conservative_holds_appended": len(current_holds),
                },
                at=now,
            )
        return {
            "status": "reconciled",
            "idempotent": False,
            "plan_fingerprint": fingerprint,
            "holds_appended": len(plan["holds"]),
            "affected_records": dict(plan["expected_affected_records"]),
            "non_target_invariants_preserved": True,
            "budget": self.budget_status(),
            "readback": self.budget_reconciliation_status(),
        }

    def budget_reconciliation_status(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            holds = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT hold_id,processing_key,transition_run_id,
                           upper_bound_usd,effective_at,reason,created_by,created_at
                    FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds
                    ORDER BY effective_at,hold_id
                    """
                ).fetchall()
            ]
            unresolved = self._budget_uncertainty_candidates(conn)
            runtime = conn.execute(
                "SELECT stop_reason FROM sheet_vitrina_v1_wb_autoanswers_runtime_state WHERE singleton=1"
            ).fetchone()
        return {
            "contract": "wb_autoanswers_budget_reconciliation_v1",
            "holds": holds,
            "hold_count": len(holds),
            "unresolved_count": len(unresolved),
            "stop_reason": str(runtime["stop_reason"] or "") if runtime else "",
            "confirmed": not unresolved
            and (runtime is None or str(runtime["stop_reason"] or "") != "budget_state_unknown"),
        }

    def budget_status(self) -> dict[str, Any]:
        settings = self.settings()
        now = self._now()
        day, month = self._period_bounds(now)
        hour_start = iso_utc(now - timedelta(hours=1))
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN substr(COALESCE(settled_at,updated_at),1,10)=? THEN actual_cost_usd ELSE 0 END),0) AS daily_actual,
                    COALESCE(SUM(CASE WHEN substr(COALESCE(settled_at,updated_at),1,7)=? THEN actual_cost_usd ELSE 0 END),0) AS monthly_actual,
                    COALESCE(SUM(CASE WHEN COALESCE(settled_at,updated_at)>=? THEN actual_cost_usd ELSE 0 END),0) AS hourly_actual,
                    COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_usd ELSE 0 END),0) AS active_reserved,
                    COALESCE(SUM(CASE WHEN status='settled' AND CAST(actual_cost_usd AS REAL)>0 AND COALESCE(settled_at,updated_at)>=? THEN 1 ELSE 0 END),0) AS paid_reviews_hour
                FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
                """,
                (day, month, hour_start, hour_start),
            ).fetchone()
            archived = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN substr(incurred_at,1,10)=? THEN actual_cost_usd ELSE 0 END),0) AS daily_actual,
                    COALESCE(SUM(CASE WHEN substr(incurred_at,1,7)=? THEN actual_cost_usd ELSE 0 END),0) AS monthly_actual,
                    COALESCE(SUM(CASE WHEN incurred_at>=? THEN actual_cost_usd ELSE 0 END),0) AS hourly_actual,
                    COALESCE(SUM(CASE WHEN incurred_at>=? AND CAST(actual_cost_usd AS REAL)>0 THEN 1 ELSE 0 END),0) AS paid_reviews_hour,
                    MAX(incurred_at) AS last_cost_at
                FROM sheet_vitrina_v1_wb_autoanswers_cost_events
                """,
                (day, month, hour_start, hour_start),
            ).fetchone()
            adjustments = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN substr(effective_at,1,10)=? THEN amount_usd ELSE 0 END),0) AS daily_actual,
                    COALESCE(SUM(CASE WHEN substr(effective_at,1,7)=? THEN amount_usd ELSE 0 END),0) AS monthly_actual,
                    COALESCE(SUM(CASE WHEN effective_at>=? THEN amount_usd ELSE 0 END),0) AS hourly_actual,
                    COALESCE(SUM(CASE WHEN substr(effective_at,1,10)=? AND CAST(amount_usd AS REAL)<0 THEN -CAST(amount_usd AS REAL) ELSE 0 END),0) AS daily_unverified,
                    COALESCE(SUM(CASE WHEN substr(effective_at,1,7)=? AND CAST(amount_usd AS REAL)<0 THEN -CAST(amount_usd AS REAL) ELSE 0 END),0) AS monthly_unverified,
                    COALESCE(SUM(CASE WHEN effective_at>=? AND CAST(amount_usd AS REAL)<0 THEN -CAST(amount_usd AS REAL) ELSE 0 END),0) AS hourly_unverified,
                    MAX(effective_at) AS last_adjustment_at
                FROM sheet_vitrina_v1_wb_autoanswers_budget_adjustments
                """,
                (day, month, hour_start, day, month, hour_start),
            ).fetchone()
            failed = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN substr(incurred_at,1,10)=? THEN actual_cost_usd ELSE 0 END),0) AS daily_actual,
                    COALESCE(SUM(CASE WHEN substr(incurred_at,1,7)=? THEN actual_cost_usd ELSE 0 END),0) AS monthly_actual,
                    COALESCE(SUM(CASE WHEN incurred_at>=? THEN actual_cost_usd ELSE 0 END),0) AS hourly_actual,
                    COALESCE(SUM(CASE WHEN incurred_at>=? AND CAST(actual_cost_usd AS REAL)>0 THEN 1 ELSE 0 END),0) AS paid_reviews_hour,
                    MAX(incurred_at) AS last_cost_at
                FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events
                """,
                (day, month, hour_start, hour_start),
            ).fetchone()
            uncertainty = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN substr(effective_at,1,10)=? THEN upper_bound_usd ELSE 0 END),0) AS daily_hold,
                    COALESCE(SUM(CASE WHEN substr(effective_at,1,7)=? THEN upper_bound_usd ELSE 0 END),0) AS monthly_hold,
                    COALESCE(SUM(CASE WHEN effective_at>=? THEN upper_bound_usd ELSE 0 END),0) AS hourly_hold,
                    COALESCE(SUM(upper_bound_usd),0) AS all_hold,
                    COUNT(*) AS hold_count,
                    MAX(created_at) AS last_hold_at
                FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds
                """,
                (day, month, hour_start),
            ).fetchone()
            unresolved_uncertainty = len(
                self._budget_uncertainty_candidates(conn)
            )
            latest = conn.execute(
                """
                SELECT MAX(updated_at) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
                """
            ).fetchone()[0]
        daily_actual = _money(row["daily_actual"]) + _money(archived["daily_actual"]) + _money(failed["daily_actual"]) + _money(adjustments["daily_actual"])
        monthly_actual = _money(row["monthly_actual"]) + _money(archived["monthly_actual"]) + _money(failed["monthly_actual"]) + _money(adjustments["monthly_actual"])
        hourly_actual = _money(row["hourly_actual"]) + _money(archived["hourly_actual"]) + _money(failed["hourly_actual"]) + _money(adjustments["hourly_actual"])
        active_reserved = _money(row["active_reserved"])
        # Historical negative adjustments remove unsupported "actual" labels,
        # but their absolute value remains held against every applicable cap.
        # This keeps the ledger truthful and the budget conservative.
        daily_unverified = _money(adjustments["daily_unverified"])
        monthly_unverified = _money(adjustments["monthly_unverified"])
        hourly_unverified = _money(adjustments["hourly_unverified"])
        daily_uncertainty = _money(uncertainty["daily_hold"])
        monthly_uncertainty = _money(uncertainty["monthly_hold"])
        hourly_uncertainty = _money(uncertainty["hourly_hold"])
        daily = daily_actual + daily_unverified + active_reserved
        monthly = monthly_actual + monthly_unverified + active_reserved
        hourly = hourly_actual + hourly_unverified + active_reserved
        daily += daily_uncertainty
        monthly += monthly_uncertainty
        hourly += hourly_uncertainty
        daily_cap = _money(settings.daily_cap_usd)
        monthly_cap = _money(settings.monthly_cap_usd)
        hourly_cap = _money(settings.hourly_cap_usd)
        ratio = Decimal(str(settings.warning_ratio))
        return {
            "active_reserved_usd": float(active_reserved),
            "hourly_used_and_reserved_usd": float(hourly),
            "daily_used_and_reserved_usd": float(daily),
            "monthly_used_and_reserved_usd": float(monthly),
            "hourly_actual_usd": float(hourly_actual),
            "daily_actual_usd": float(daily_actual),
            "monthly_actual_usd": float(monthly_actual),
            "hourly_unverified_legacy_usd": float(hourly_unverified),
            "daily_unverified_legacy_usd": float(daily_unverified),
            "monthly_unverified_legacy_usd": float(monthly_unverified),
            "hourly_uncertainty_hold_usd": float(hourly_uncertainty),
            "daily_uncertainty_hold_usd": float(daily_uncertainty),
            "monthly_uncertainty_hold_usd": float(monthly_uncertainty),
            "all_time_uncertainty_hold_usd": float(
                _money(uncertainty["all_hold"])
            ),
            "uncertainty_hold_count": int(uncertainty["hold_count"] or 0),
            "unresolved_uncertainty_count": unresolved_uncertainty,
            "budget_state": (
                "unknown"
                if unresolved_uncertainty
                else "conservative_unverified"
                if int(uncertainty["hold_count"] or 0)
                else "confirmed"
            ),
            "hourly_cap_usd": float(hourly_cap),
            "daily_cap_usd": float(daily_cap),
            "monthly_cap_usd": float(monthly_cap),
            "available_hourly_usd": float(max(Decimal(0), hourly_cap - hourly)),
            "available_daily_usd": float(max(Decimal(0), daily_cap - daily)),
            "available_monthly_usd": float(max(Decimal(0), monthly_cap - monthly)),
            "paid_reviews_last_hour": int(row["paid_reviews_hour"] or 0) + int(archived["paid_reviews_hour"] or 0) + int(failed["paid_reviews_hour"] or 0),
            "max_paid_reviews_per_hour": settings.max_paid_reviews_per_hour,
            "warning_ratio": float(ratio),
            "warning": hourly >= hourly_cap * ratio or daily >= daily_cap * ratio or monthly >= monthly_cap * ratio,
            "hard_cap_reached": hourly >= hourly_cap or daily >= daily_cap or monthly >= monthly_cap,
            "updated_at": max(str(latest or ""), str(archived["last_cost_at"] or ""), str(failed["last_cost_at"] or ""), str(adjustments["last_adjustment_at"] or ""), str(uncertainty["last_hold_at"] or "")) or None,
        }

    def _set_stop_reason(
        self,
        conn: sqlite3.Connection,
        reason: str | None,
        *,
        details: Mapping[str, Any] | None = None,
        at: datetime | None = None,
    ) -> None:
        stamp = at or self._now()
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_wb_autoanswers_runtime_state
            SET stop_reason=?, stop_details_json=?, updated_at=? WHERE singleton=1
            """,
            (_clean_text(reason) or None, canonical_json(dict(details or {})), iso_utc(stamp)),
        )

    def reconcile_stale_reservations(self) -> int:
        """Release reservations that no longer protect an actively leased call."""

        now = self._now()
        with self.transaction() as conn:
            uncertain = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations AS r
                    WHERE r.status='reserved' AND r.provider_call_started_at IS NOT NULL AND (
                        r.expires_at IS NULL OR r.expires_at<=? OR NOT EXISTS(
                            SELECT 1 FROM sheet_vitrina_v1_wb_autoanswer_jobs j
                            WHERE j.processing_key=r.processing_key AND j.state=?
                              AND j.lease_until IS NOT NULL AND j.lease_until>?
                        )
                    )
                    """,
                    (iso_utc(now), STATE_PROCESSING, iso_utc(now)),
                ).fetchone()[0]
            )
            cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations AS r
                SET reserved_usd=0, status='released', released_reason='stale_or_orphaned',
                    expires_at=NULL, updated_at=?
                WHERE r.status='reserved' AND (
                    r.expires_at IS NULL OR r.expires_at<=? OR NOT EXISTS(
                        SELECT 1 FROM sheet_vitrina_v1_wb_autoanswer_jobs j
                        WHERE j.processing_key=r.processing_key AND j.state=?
                          AND j.lease_until IS NOT NULL AND j.lease_until>?
                    )
                )
                """,
                (iso_utc(now), iso_utc(now), STATE_PROCESSING, iso_utc(now)),
            )
            released = int(cursor.rowcount or 0)
            if uncertain:
                # The monetary outcome of a process that lost its lease is not
                # provable from local state.  Release the capacity hold as
                # required, but latch paid processing closed until an operator
                # reconciles provider usage.
                self._set_stop_reason(
                    conn,
                    "budget_state_unknown",
                    details={
                        "released_stale_reservations": released,
                        "provider_started_reservations": uncertain,
                    },
                    at=now,
                )
            return released

    def _reserve_budget(
        self,
        conn: sqlite3.Connection,
        *,
        key: str,
        settings: AutoanswersSettings,
        at: datetime,
        expires_at: datetime,
        transition_run_id: str | None,
    ) -> str | None:
        existing = conn.execute(
            "SELECT status FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations WHERE processing_key=?",
            (key,),
        ).fetchone()
        if existing is not None and str(existing["status"]) != "released":
            return None
        day, month = self._period_bounds(at)
        hour_start = iso_utc(at - timedelta(hours=1))
        totals = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN substr(COALESCE(settled_at,updated_at),1,10)=? THEN actual_cost_usd ELSE 0 END),0) + COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_usd ELSE 0 END),0) AS daily_total,
                COALESCE(SUM(CASE WHEN substr(COALESCE(settled_at,updated_at),1,7)=? THEN actual_cost_usd ELSE 0 END),0) + COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_usd ELSE 0 END),0) AS monthly_total,
                COALESCE(SUM(CASE WHEN COALESCE(settled_at,updated_at)>=? THEN actual_cost_usd ELSE 0 END),0) + COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_usd ELSE 0 END),0) AS hourly_total,
                COALESCE(SUM(CASE WHEN status='settled' AND CAST(actual_cost_usd AS REAL)>0 AND COALESCE(settled_at,updated_at)>=? THEN 1 ELSE 0 END),0) AS paid_reviews_hour
            FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
            """,
            (day, month, hour_start, hour_start),
        ).fetchone()
        archived = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN substr(incurred_at,1,10)=? THEN actual_cost_usd ELSE 0 END),0) AS daily_total,
                COALESCE(SUM(CASE WHEN substr(incurred_at,1,7)=? THEN actual_cost_usd ELSE 0 END),0) AS monthly_total,
                COALESCE(SUM(CASE WHEN incurred_at>=? THEN actual_cost_usd ELSE 0 END),0) AS hourly_total,
                COALESCE(SUM(CASE WHEN incurred_at>=? AND CAST(actual_cost_usd AS REAL)>0 THEN 1 ELSE 0 END),0) AS paid_reviews_hour
            FROM sheet_vitrina_v1_wb_autoanswers_cost_events
            """,
            (day, month, hour_start, hour_start),
        ).fetchone()
        failed = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN substr(incurred_at,1,10)=? THEN actual_cost_usd ELSE 0 END),0) AS daily_total,
                COALESCE(SUM(CASE WHEN substr(incurred_at,1,7)=? THEN actual_cost_usd ELSE 0 END),0) AS monthly_total,
                COALESCE(SUM(CASE WHEN incurred_at>=? THEN actual_cost_usd ELSE 0 END),0) AS hourly_total,
                COALESCE(SUM(CASE WHEN incurred_at>=? AND CAST(actual_cost_usd AS REAL)>0 THEN 1 ELSE 0 END),0) AS paid_reviews_hour
            FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events
            """,
            (day, month, hour_start, hour_start),
        ).fetchone()
        adjustments = conn.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN substr(effective_at,1,10)=? THEN amount_usd ELSE 0 END),0) AS daily_total,
                   COALESCE(SUM(CASE WHEN substr(effective_at,1,7)=? THEN amount_usd ELSE 0 END),0) AS monthly_total,
                   COALESCE(SUM(CASE WHEN effective_at>=? THEN amount_usd ELSE 0 END),0) AS hourly_total,
                   COALESCE(SUM(CASE WHEN substr(effective_at,1,10)=? AND CAST(amount_usd AS REAL)<0 THEN -CAST(amount_usd AS REAL) ELSE 0 END),0) AS daily_unverified,
                   COALESCE(SUM(CASE WHEN substr(effective_at,1,7)=? AND CAST(amount_usd AS REAL)<0 THEN -CAST(amount_usd AS REAL) ELSE 0 END),0) AS monthly_unverified,
                   COALESCE(SUM(CASE WHEN effective_at>=? AND CAST(amount_usd AS REAL)<0 THEN -CAST(amount_usd AS REAL) ELSE 0 END),0) AS hourly_unverified
            FROM sheet_vitrina_v1_wb_autoanswers_budget_adjustments
            """,
            (day, month, hour_start, day, month, hour_start),
        ).fetchone()
        uncertainty = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN substr(effective_at,1,10)=? THEN upper_bound_usd ELSE 0 END),0) AS daily_total,
                COALESCE(SUM(CASE WHEN substr(effective_at,1,7)=? THEN upper_bound_usd ELSE 0 END),0) AS monthly_total,
                COALESCE(SUM(CASE WHEN effective_at>=? THEN upper_bound_usd ELSE 0 END),0) AS hourly_total
            FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds
            """,
            (day, month, hour_start),
        ).fetchone()
        reservation = _money(settings.max_reservation_per_review_usd)
        hourly_total = _money(totals["hourly_total"]) + _money(archived["hourly_total"]) + _money(failed["hourly_total"]) + _money(adjustments["hourly_total"]) + _money(adjustments["hourly_unverified"]) + _money(uncertainty["hourly_total"])
        daily_total = _money(totals["daily_total"]) + _money(archived["daily_total"]) + _money(failed["daily_total"]) + _money(adjustments["daily_total"]) + _money(adjustments["daily_unverified"]) + _money(uncertainty["daily_total"])
        monthly_total = _money(totals["monthly_total"]) + _money(archived["monthly_total"]) + _money(failed["monthly_total"]) + _money(adjustments["monthly_total"]) + _money(adjustments["monthly_unverified"]) + _money(uncertainty["monthly_total"])
        if hourly_total + reservation > _money(settings.hourly_cap_usd):
            self._set_stop_reason(conn, "hourly_budget_reached", at=at)
            return "hourly_budget_reached"
        if daily_total + reservation > _money(settings.daily_cap_usd):
            self._set_stop_reason(conn, "daily_budget_reached", at=at)
            return "daily_budget_reached"
        if monthly_total + reservation > _money(settings.monthly_cap_usd):
            self._set_stop_reason(conn, "monthly_budget_reached", at=at)
            return "monthly_budget_reached"
        paid_reviews_hour = int(totals["paid_reviews_hour"] or 0) + int(archived["paid_reviews_hour"] or 0) + int(failed["paid_reviews_hour"] or 0)
        if paid_reviews_hour >= settings.max_paid_reviews_per_hour:
            self._set_stop_reason(conn, "paid_reviews_hourly_limit", at=at)
            return "paid_reviews_hourly_limit"
        if transition_run_id:
            sweep = conn.execute(
                "SELECT run_max_usd,run_max_paid_reviews FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps WHERE transition_run_id=?",
                (transition_run_id,),
            ).fetchone()
            if sweep is None or (sweep["run_max_usd"] is None and sweep["run_max_paid_reviews"] is None):
                self._set_stop_reason(conn, "run_cap_missing", at=at)
                return "run_cap_missing"
            run = conn.execute(
                """
                SELECT COALESCE(SUM(CAST(r.actual_cost_usd AS REAL)),0) AS actual,
                       COALESCE(SUM(CASE WHEN r.status='reserved' THEN CAST(r.reserved_usd AS REAL) ELSE 0 END),0) AS reserved,
                       COALESCE(SUM(CASE WHEN r.status='settled' AND CAST(r.actual_cost_usd AS REAL)>0 THEN 1 ELSE 0 END),0) AS paid
                FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations r
                WHERE r.transition_run_id=?
                """,
                (transition_run_id,),
            ).fetchone()
            failed_run = conn.execute(
                """
                SELECT COALESCE(SUM(CAST(actual_cost_usd AS REAL)),0) AS actual,
                       COALESCE(SUM(CASE WHEN CAST(actual_cost_usd AS REAL)>0 THEN 1 ELSE 0 END),0) AS paid
                FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events
                WHERE transition_run_id=?
                """,
                (transition_run_id,),
            ).fetchone()
            uncertain_run = _money(
                conn.execute(
                    """
                    SELECT COALESCE(SUM(upper_bound_usd),0)
                    FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds
                    WHERE transition_run_id=?
                    """,
                    (transition_run_id,),
                ).fetchone()[0]
            )
            if sweep["run_max_usd"] is not None and _money(run["actual"]) + _money(failed_run["actual"]) + _money(run["reserved"]) + uncertain_run + reservation > _money(sweep["run_max_usd"]):
                self._set_stop_reason(conn, "run_budget_reached", details={"transition_run_id": transition_run_id}, at=at)
                return "run_budget_reached"
            if sweep["run_max_paid_reviews"] is not None and int(run["paid"] or 0) + int(failed_run["paid"] or 0) >= int(sweep["run_max_paid_reviews"]):
                self._set_stop_reason(conn, "run_review_limit_reached", details={"transition_run_id": transition_run_id}, at=at)
                return "run_review_limit_reached"
        if existing is None:
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_autoanswers_budget_reservations(
                    processing_key, reserved_usd, actual_cost_usd, status,
                    transition_run_id, expires_at, provider_call_started_at, released_reason, settled_at,
                    created_at, updated_at
                ) VALUES(?,?,0,'reserved',?,?,NULL,NULL,NULL,?,?)
                """,
                (key, str(reservation), transition_run_id, iso_utc(expires_at), iso_utc(at), iso_utc(at)),
            )
        else:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations
                SET reserved_usd=?, actual_cost_usd=0, status='reserved', transition_run_id=?,
                    expires_at=?, provider_call_started_at=NULL, released_reason=NULL,
                    settled_at=NULL, created_at=?, updated_at=?
                WHERE processing_key=?
                """,
                (str(reservation), transition_run_id, iso_utc(expires_at), iso_utc(at), iso_utc(at), key),
            )
        return None

    @staticmethod
    def _automatic_content_pending_count(
        conn: sqlite3.Connection,
        *,
        transition_run_id: str | None,
        policy_epoch: int,
        target_mode: str,
    ) -> int:
        """Count scoped content reviews for which automation still has a next step."""

        run_id = _clean_text(transition_run_id)
        if not run_id:
            return 0
        generated_clause = " OR j.state='generated'" if target_mode != MODE_DRAFT_ONLY else ""
        return int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope rs
                JOIN sheet_vitrina_v1_wb_feedbacks f
                  ON f.feedback_id=rs.feedback_id
                 AND f.content_version=rs.content_version_at_preview
                 AND f.content_version_hash=rs.content_version_hash_at_preview
                LEFT JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
                  ON j.feedback_id=f.feedback_id
                 AND j.content_version=f.content_version
                 AND j.bundle_version=?
                LEFT JOIN sheet_vitrina_v1_wb_publication_jobs p
                  ON p.processing_key=j.processing_key
                WHERE rs.sweep_id=?
                  AND rs.content_classification_at_preview=?
                  AND COALESCE(f.answer_text,'')=''
                  AND (
                    j.processing_key IS NULL
                    OR COALESCE(j.regeneration_required,0)=1
                    OR (
                      COALESCE(j.policy_epoch,-1)<>?
                      AND j.state NOT IN ('needs_review','terminal_error','skipped','published')
                    )
                    OR j.state IN ('queued','processing','retryable_error','approved','publishing','publish_pending_readback')
                    {generated_clause}
                    OR p.state IN ('approved','publishing','publish_pending_readback')
                    OR (p.state='retryable_error' AND p.retry_stage='readback')
                  )
                """,
                (
                    PROMPT_BUNDLE_VERSION,
                    run_id,
                    CONTENT_CLASS_CONTENT_BEARING,
                    int(policy_epoch),
                ),
            ).fetchone()[0]
        )

    def claim_processing_job(self, *, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any] | None:
        settings = self.assert_effective_on(operation="AI processing")
        now = self._now()
        lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self.transaction() as conn:
            uncertain = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations AS r
                    WHERE r.status='reserved' AND r.provider_call_started_at IS NOT NULL AND (
                        r.expires_at IS NULL OR r.expires_at<=? OR NOT EXISTS(
                            SELECT 1 FROM sheet_vitrina_v1_wb_autoanswer_jobs j
                            WHERE j.processing_key=r.processing_key AND j.state=?
                              AND j.lease_until IS NOT NULL AND j.lease_until>?
                        )
                    )
                    """,
                    (iso_utc(now), STATE_PROCESSING, iso_utc(now)),
                ).fetchone()[0]
            )
            released = int(conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations AS r
                SET reserved_usd=0, status='released', released_reason='stale_or_orphaned',
                    expires_at=NULL, updated_at=?
                WHERE r.status='reserved' AND (
                    r.expires_at IS NULL OR r.expires_at<=? OR NOT EXISTS(
                        SELECT 1 FROM sheet_vitrina_v1_wb_autoanswer_jobs j
                        WHERE j.processing_key=r.processing_key AND j.state=?
                          AND j.lease_until IS NOT NULL AND j.lease_until>?
                    )
                )
                """,
                (iso_utc(now), iso_utc(now), STATE_PROCESSING, iso_utc(now)),
            ).rowcount or 0)
            if uncertain:
                self._set_stop_reason(
                    conn,
                    "budget_state_unknown",
                    details={
                        "released_stale_reservations": released,
                        "provider_started_reservations": uncertain,
                    },
                    at=now,
                )
            runtime = conn.execute(
                "SELECT stop_reason FROM sheet_vitrina_v1_wb_autoanswers_runtime_state WHERE singleton=1"
            ).fetchone()
            if runtime is not None and str(runtime["stop_reason"] or "") in {
                "budget_state_unknown",
                "openai_quota_exhausted",
            }:
                return None
            active_paid = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswer_jobs
                    WHERE state=? AND lease_until>? AND processing_kind=?
                    """,
                    (STATE_PROCESSING, iso_utc(now), PROCESSING_KIND_FROZEN_AI),
                ).fetchone()[0]
            )
            effective_concurrency = min(
                settings.global_paid_review_concurrency,
                settings.max_inflight_role_calls,
            )
            if active_paid >= effective_concurrency:
                self._set_stop_reason(conn, "concurrency_limit", at=now)
                return None
            # Retain stale jobs as review evidence, but remove them from every
            # automatic claim path before applying content priority.  Joining
            # claims to the current content version alone would make these
            # rows invisible without recording why they stopped.
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs AS j
                SET state=?, last_error_code='stale_content_version', updated_at=?
                WHERE j.policy_epoch=?
                  AND j.state IN (?,?,?)
                  AND (j.state<>? OR j.lease_until IS NULL OR j.lease_until<=?)
                  AND EXISTS(
                    SELECT 1 FROM sheet_vitrina_v1_wb_feedbacks f
                    WHERE f.feedback_id=j.feedback_id
                      AND (
                        f.content_version<>j.content_version OR
                        f.content_version_hash<>j.content_version_hash
                      )
                  )
                """,
                (
                    STATE_NEEDS_REVIEW,
                    iso_utc(now),
                    settings.policy_epoch,
                    STATE_QUEUED,
                    STATE_PROCESSING,
                    STATE_RETRYABLE_ERROR,
                    STATE_PROCESSING,
                    iso_utc(now),
                ),
            )
            sweep = conn.execute(
                "SELECT transition_run_id,target_mode FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps WHERE policy_epoch=? ORDER BY created_at DESC LIMIT 1",
                (settings.policy_epoch,),
            ).fetchone()
            content_pending = self._automatic_content_pending_count(
                conn,
                transition_run_id=(sweep["transition_run_id"] if sweep is not None else None),
                policy_epoch=settings.policy_epoch,
                target_mode=(str(sweep["target_mode"]) if sweep is not None else settings.mode),
            )
            active_run_id = (
                str(sweep["transition_run_id"] or "") if sweep is not None else ""
            )
            row = conn.execute(
                f"""
                SELECT j.* FROM sheet_vitrina_v1_wb_autoanswer_jobs j
                JOIN sheet_vitrina_v1_wb_feedbacks f
                  ON f.feedback_id=j.feedback_id AND f.content_version=j.content_version
                WHERE (
                    (j.state=? AND j.available_at<=?) OR
                    (j.state=? AND j.lease_until IS NOT NULL AND j.lease_until<=?) OR
                    (j.state=? AND j.retry_stage='processing' AND j.available_at<=?)
                )
                  AND j.policy_epoch=?
                  AND (? <> ? OR j.trigger_source='manual_generate')
                  AND (j.trigger_source='manual_generate' OR ?='' OR j.transition_run_id=?)
                  AND (?=0 OR j.trigger_source='manual_generate' OR f.content_classification=?)
                ORDER BY {_automatic_priority_order_sql(
                    "f",
                    manual_predicate="j.trigger_source='manual_generate'",
                )}
                LIMIT 1
                """,
                (
                    STATE_QUEUED,
                    iso_utc(now),
                    STATE_PROCESSING,
                    iso_utc(now),
                    STATE_RETRYABLE_ERROR,
                    iso_utc(now),
                    settings.policy_epoch,
                    settings.mode,
                    MODE_MANUAL,
                    active_run_id,
                    active_run_id,
                    content_pending,
                    CONTENT_CLASS_CONTENT_BEARING,
                ),
            ).fetchone()
            if row is None:
                self._set_stop_reason(
                    conn,
                    "manual_pause" if settings.mode == MODE_MANUAL else "no_eligible_jobs",
                    at=now,
                )
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
            if int(row["policy_epoch"] or 0) != settings.policy_epoch:
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                    SET state=?, last_error_code='policy_epoch_stale', updated_at=?
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
            if str(row["processing_kind"] or PROCESSING_KIND_FROZEN_AI) == PROCESSING_KIND_FROZEN_AI:
                budget_stop = self._reserve_budget(
                    conn,
                    key=str(row["processing_key"]),
                    settings=settings,
                    at=now,
                    expires_at=lease_until,
                    transition_run_id=str(row["transition_run_id"]) if row["transition_run_id"] else None,
                )
                if budget_stop:
                    return None
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
                details={
                    "lease_until": iso_utc(lease_until),
                    "processing_kind": str(row["processing_kind"] or PROCESSING_KIND_FROZEN_AI),
                    "transition_run_id": row["transition_run_id"],
                },
                at=now,
                previous_state=previous,
                next_state=STATE_PROCESSING,
            )
            claimed = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (row["processing_key"],),
            ).fetchone()
            self._set_stop_reason(conn, None, at=now)
            return dict(claimed)

    def mark_provider_call_started(self, processing_key_value: str, *, worker_id: str) -> None:
        """Persist the exact point after which crash cost may be unknowable."""

        now = self._now()
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (_clean_text(processing_key_value),),
            ).fetchone()
            reservation = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations WHERE processing_key=?",
                (_clean_text(processing_key_value),),
            ).fetchone()
            if job is None or str(job["state"]) != STATE_PROCESSING:
                raise AutoanswersRuntimeError("processing lease is no longer current", code="processing_lease_stale")
            if reservation is None or str(reservation["status"]) != "reserved":
                raise AutoanswersRuntimeError("budget reservation is not active", code="reservation_missing")
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations
                SET provider_call_started_at=COALESCE(provider_call_started_at,?), updated_at=?
                WHERE processing_key=?
                """,
                (iso_utc(now), iso_utc(now), processing_key_value),
            )
            self._audit(
                conn,
                aggregate_type="processing_job",
                aggregate_id=processing_key_value,
                event_type="provider_call_boundary_entered",
                actor_type="worker",
                actor_id=_clean_text(worker_id),
                details={},
                at=now,
            )

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
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations
                SET reserved_usd=0, status='released', expires_at=NULL,
                    released_reason='processing_retry', updated_at=?
                WHERE processing_key=? AND status='reserved'
                """,
                (iso_utc(now), processing_key_value),
            )
            code = _clean_text(error_code)
            lower_code = code.casefold()
            stop_reason = (
                "budget_state_unknown"
                if code == "node_timeout" or code == "node_invalid_json" or code.startswith("node_process_exit_")
                else "openai_quota_exhausted" if "insufficient_quota" in lower_code
                else "rate_limited" if "429" in code
                else "retry_backoff"
            )
            self._set_stop_reason(conn, stop_reason, details={"code": code}, at=now)
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
                SET actual_cost_usd=?, reserved_usd=0, status='settled',
                    expires_at=NULL, released_reason=NULL, settled_at=?, updated_at=?
                WHERE processing_key=?
                """,
                (str(actual), iso_utc(now), iso_utc(now), processing_key_value),
            )
            archived_total = _money(
                conn.execute(
                    "SELECT COALESCE(SUM(actual_cost_usd),0) FROM sheet_vitrina_v1_wb_autoanswers_cost_events WHERE processing_key=?",
                    (processing_key_value,),
                ).fetchone()[0]
            )
            failed_total = _money(
                conn.execute(
                    "SELECT COALESCE(SUM(actual_cost_usd),0) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events WHERE processing_key=?",
                    (processing_key_value,),
                ).fetchone()[0]
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET actual_cost_usd=?, updated_at=? WHERE processing_key=?",
                (str(archived_total + failed_total + actual), iso_utc(now), processing_key_value),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_runtime_state
                SET last_successful_ai_call_at=?, updated_at=? WHERE singleton=1
                """,
                (iso_utc(now), iso_utc(now)),
            )

    def record_failed_processing_usage(
        self,
        processing_key_value: str,
        *,
        actual_cost_usd: Any,
        usage: Mapping[str, Any],
        role_calls: int,
        error_code: str,
        worker_id: str,
    ) -> None:
        """Account provider-reported role usage even when the pipeline fails."""

        actual = _money(actual_cost_usd)
        if actual <= 0:
            return
        now = self._now()
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (_clean_text(processing_key_value),),
            ).fetchone()
            if job is None:
                raise AutoanswersRuntimeError("processing job not found", code="job_not_found")
            attempt = max(1, int(job["attempts"] or 0))
            conn.execute(
                """
                INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswers_failed_cost_events(
                    event_id,processing_key,attempt_number,transition_run_id,
                    actual_cost_usd,usage_json,role_calls,error_code,incurred_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    uuid4().hex,
                    processing_key_value,
                    attempt,
                    job["transition_run_id"],
                    str(actual),
                    canonical_json(dict(usage)),
                    max(0, int(role_calls)),
                    _clean_text(error_code),
                    iso_utc(now),
                ),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations
                SET reserved_usd=0, actual_cost_usd=0, status='released',
                    expires_at=NULL, released_reason='processing_failed_after_usage',
                    updated_at=?
                WHERE processing_key=? AND status='reserved'
                """,
                (iso_utc(now), processing_key_value),
            )
            total = _money(
                conn.execute(
                    """
                    SELECT
                        (SELECT COALESCE(SUM(actual_cost_usd),0)
                         FROM sheet_vitrina_v1_wb_autoanswers_cost_events WHERE processing_key=?)
                        +
                        (SELECT COALESCE(SUM(actual_cost_usd),0)
                         FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events WHERE processing_key=?)
                    """,
                    (processing_key_value, processing_key_value),
                ).fetchone()[0]
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET actual_cost_usd=?, updated_at=? WHERE processing_key=?",
                (str(total), iso_utc(now), processing_key_value),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_runtime_state
                SET last_successful_ai_call_at=?, updated_at=? WHERE singleton=1
                """,
                (iso_utc(now), iso_utc(now)),
            )
            self._audit(
                conn,
                aggregate_type="processing_job",
                aggregate_id=processing_key_value,
                event_type="failed_processing_usage_recorded",
                actor_type="worker",
                actor_id=_clean_text(worker_id),
                details={
                    "attempt": attempt,
                    "actual_cost_usd": str(actual),
                    "role_calls": max(0, int(role_calls)),
                    "error_code": _clean_text(error_code),
                },
                at=now,
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

    def complete_rating_only_template(
        self,
        processing_key_value: str,
        *,
        worker_id: str,
    ) -> dict[str, Any]:
        """Complete one empty review deterministically without any model call."""

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
        if str(job["state"]) != STATE_PROCESSING:
            raise AutoanswersRuntimeError("processing job is not claimed", code="job_not_processing")
        if str(job["processing_kind"]) != PROCESSING_KIND_RATING_ONLY_TEMPLATE:
            raise AutoanswersRuntimeError("job is not rating-only", code="processing_kind_mismatch")
        if feedback is None or str(feedback["content_classification"]) != CONTENT_CLASS_RATING_ONLY:
            raise AutoanswersRuntimeError("rating-only input changed", code="stale_content_version")
        selected = rating_only_template(str(job["feedback_id"]), int(feedback["rating"]))
        stored = self.complete_generation(
            processing_key_value,
            result={
                "final_route": selected["route"],
                "final_reply": selected["reply"],
                "case_code": None,
                "pipeline_result": {
                    "route": selected["route"],
                    "subcategory": selected["subcategory"],
                    "template_id": selected["template_id"],
                    "publication_action": "draft",
                    "deterministic": True,
                    "model_calls": 0,
                },
                "usage": {},
                "hard_gates_passed": True,
                "fallback_used": False,
                "media_uncertain": False,
                "node_contract_valid": True,
            },
            worker_id=worker_id,
        )
        now = self._now()
        with self.transaction() as conn:
            self._audit(
                conn,
                aggregate_type="processing_job",
                aggregate_id=processing_key_value,
                event_type="rating_only_template_completed",
                actor_type="deterministic_policy",
                actor_id=DEFAULT_POLICY_VERSION,
                details={
                    "rating": int(feedback["rating"]),
                    "template_id": selected["template_id"],
                    "model_calls": 0,
                    "actual_cost_usd": "0",
                },
                at=now,
            )
        return stored

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

    def complete_media_uncertainty(
        self,
        processing_key_value: str,
        *,
        uncertainty: Sequence[Any],
        worker_id: str,
    ) -> dict[str, Any]:
        """Fail closed before any paid AI call when required media is unavailable."""

        now = self._now()
        codes = sorted({_clean_text(item) for item in uncertainty if _clean_text(item)})
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                (processing_key_value,),
            ).fetchone()
            if job is None:
                raise AutoanswersRuntimeError("processing job not found", code="job_not_found")
            if str(job["state"]) != STATE_PROCESSING:
                raise AutoanswersRuntimeError("processing job is not claimed", code="job_not_processing")
            assert_transition(STATE_PROCESSING, STATE_NEEDS_REVIEW)
            reasons = ["media_uncertain", "regeneration_required"]
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state=?, media_uncertain=1, regeneration_required=1,
                    regeneration_reason='media_fetch_failed',
                    review_reasons_json=?, result_json=NULL,
                    final_route=NULL, case_code=NULL, final_reply=NULL,
                    final_reply_sha256=NULL, hard_gates_passed=NULL,
                    fallback_used=NULL, node_contract_valid=NULL,
                    lease_owner=NULL, lease_until=NULL, completed_at=?, updated_at=?
                WHERE processing_key=?
                """,
                (
                    STATE_NEEDS_REVIEW,
                    canonical_json(reasons),
                    iso_utc(now),
                    iso_utc(now),
                    processing_key_value,
                ),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations
                SET reserved_usd=0, actual_cost_usd=0, status='released', updated_at=?
                WHERE processing_key=?
                """,
                (iso_utc(now), processing_key_value),
            )
            self._audit(
                conn,
                aggregate_type="processing_job",
                aggregate_id=processing_key_value,
                event_type="media_uncertainty_blocked_ai",
                actor_type="worker",
                actor_id=_clean_text(worker_id),
                details={"uncertainty_codes": codes, "model_calls": 0},
                at=now,
                previous_state=STATE_PROCESSING,
                next_state=STATE_NEEDS_REVIEW,
            )
            return dict(
                conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE processing_key=?",
                    (processing_key_value,),
                ).fetchone()
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
                    media_uncertain=?, node_contract_valid=?,
                    regeneration_required=?, regeneration_reason=?, lease_owner=NULL,
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
                    int(media_uncertain),
                    "media_fetch_failed" if media_uncertain else None,
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
                review_reasons.append("regeneration_required")
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
                    SET reserved_usd=0, status='released',
                        expires_at=NULL, released_reason='terminal_error_without_usage', updated_at=?
                    WHERE processing_key=?
                    """,
                    (iso_utc(now), processing_key_value),
                )
            code = _clean_text(error_code)
            if (
                code == "node_timeout"
                or code == "node_invalid_json"
                or code.startswith("node_process_exit_")
            ):
                self._set_stop_reason(conn, "budget_state_unknown", details={"code": code}, at=now)
            elif "insufficient_quota" in code.casefold():
                self._set_stop_reason(conn, "openai_quota_exhausted", details={"code": code}, at=now)
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
                mode_at_enqueue, manual_edit_revision, policy_epoch,
                transition_run_id, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?)
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
                int(job["policy_epoch"] or 0),
                dict(job).get("transition_run_id"),
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
        if bool(job["regeneration_required"]):
            raise AutoanswersRuntimeError("media-aware regeneration is required", code="regeneration_required")
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
                    manual_edit_revision=?, policy_epoch=?, updated_at=?
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
                    self.settings().policy_epoch,
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
            if bool(job["regeneration_required"]):
                raise AutoanswersRuntimeError("media-aware regeneration is required", code="regeneration_required")
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
            stale_writes = conn.execute(
                """
                SELECT p.publication_key,p.processing_key,p.state
                FROM sheet_vitrina_v1_wb_publication_jobs p
                WHERE p.write_started_at IS NULL
                  AND p.state IN (?,?)
                  AND EXISTS(
                    SELECT 1 FROM sheet_vitrina_v1_wb_feedbacks f
                    WHERE f.feedback_id=p.feedback_id
                      AND (
                        f.content_version<>p.content_version OR
                        f.content_version_hash<>p.content_version_hash
                      )
                  )
                """,
                (STATE_APPROVED, STATE_PUBLISHING),
            ).fetchall()
            for stale in stale_writes:
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_publication_jobs
                    SET state=?, last_error_code='stale_content_version',
                        lease_owner=NULL, lease_until=NULL, updated_at=?
                    WHERE publication_key=?
                    """,
                    (STATE_NEEDS_REVIEW, iso_utc(now), stale["publication_key"]),
                )
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                    SET state=?, last_error_code='stale_content_version', updated_at=?
                    WHERE processing_key=?
                    """,
                    (STATE_NEEDS_REVIEW, iso_utc(now), stale["processing_key"]),
                )
                self._audit(
                    conn,
                    aggregate_type="publication_job",
                    aggregate_id=str(stale["publication_key"]),
                    event_type="publication_quarantined",
                    actor_type="worker",
                    actor_id=_clean_text(worker_id),
                    details={"code": "stale_content_version"},
                    at=now,
                    previous_state=str(stale["state"]),
                    next_state=STATE_NEEDS_REVIEW,
                )
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
                    "SELECT master_enabled,mode,policy_epoch FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
                ).fetchone()
                if not settings_row or not bool(settings_row["master_enabled"]) or _force_off_from_env(self.env):
                    return None
                sweep = conn.execute(
                    "SELECT transition_run_id,target_mode FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps WHERE policy_epoch=? ORDER BY created_at DESC LIMIT 1",
                    (int(settings_row["policy_epoch"]),),
                ).fetchone()
                content_pending = self._automatic_content_pending_count(
                    conn,
                    transition_run_id=(sweep["transition_run_id"] if sweep is not None else None),
                    policy_epoch=int(settings_row["policy_epoch"]),
                    target_mode=(str(sweep["target_mode"]) if sweep is not None else str(settings_row["mode"])),
                )
                active_run_id = (
                    str(sweep["transition_run_id"] or "") if sweep is not None else ""
                )
                row = conn.execute(
                    f"""
                    SELECT p.* FROM sheet_vitrina_v1_wb_publication_jobs p
                    JOIN sheet_vitrina_v1_wb_feedbacks f
                      ON f.feedback_id=p.feedback_id AND f.content_version=p.content_version
                    WHERE (
                        (p.state=? AND p.available_at<=?) OR
                        (p.state=? AND p.write_started_at IS NULL AND p.lease_until<=?)
                    )
                      AND p.policy_epoch=?
                      AND (p.request_source='manual' OR ?='' OR p.transition_run_id=?)
                      AND (
                        p.request_source='manual' OR ?=0
                        OR f.content_classification=?
                      )
                    ORDER BY {_automatic_priority_order_sql(
                        "f",
                        manual_predicate="p.request_source='manual'",
                    )}
                    LIMIT 1
                    """,
                    (
                        STATE_APPROVED,
                        iso_utc(now),
                        STATE_PUBLISHING,
                        iso_utc(now),
                        int(settings_row["policy_epoch"]),
                        active_run_id,
                        active_run_id,
                        content_pending,
                        CONTENT_CLASS_CONTENT_BEARING,
                    ),
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
        if bool(processing["regeneration_required"]):
            raise AutoanswersRuntimeError("media-aware regeneration is required", code="regeneration_required")
        classification = str(
            feedback["content_classification"] or CONTENT_CLASS_INDETERMINATE
        )
        if str(processing["final_route"] or "") == ROUTE_RATING_ONLY_TEMPLATE:
            if classification != CONTENT_CLASS_RATING_ONLY:
                raise AutoanswersRuntimeError(
                    "rating-only publication classification is stale",
                    code="rating_only_classification_mismatch",
                )
        elif str(processing["processing_kind"] or "") == PROCESSING_KIND_RATING_ONLY_TEMPLATE:
            raise AutoanswersRuntimeError(
                "rating-only processing route is invalid",
                code="rating_only_route_mismatch",
            )
        settings = conn.execute(
            "SELECT policy_epoch FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
        ).fetchone()
        if settings is None or int(publication["policy_epoch"] or 0) != int(settings["policy_epoch"]):
            raise AutoanswersRuntimeError("publication policy epoch is stale", code="policy_epoch_stale")
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
            if state == STATE_PUBLISHED:
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswers_runtime_state
                    SET last_confirmed_publication_at=?, updated_at=? WHERE singleton=1
                    """,
                    (iso_utc(now), iso_utc(now)),
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
        raise AutoanswersRuntimeError(
            "legacy backlog materialization is disabled; use a capped mode-transition preview",
            code="legacy_backlog_disabled",
        )

    def enqueue_backlog_from_preview(self, preview_id: str, *, actor_id: str) -> dict[str, Any]:
        raise AutoanswersRuntimeError(
            "legacy backlog materialization is disabled; use a capped mode-transition preview",
            code="legacy_backlog_disabled",
        )

    def _transition_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        scope_from: str,
        scope_to: str | None,
    ) -> tuple[list[dict[str, Any]], str]:
        clauses = ["COALESCE(f.answer_text,'')=''", "substr(COALESCE(f.created_at_wb,f.first_seen_at),1,10)>=?"]
        params: list[Any] = [_clean_text(scope_from)]
        if scope_to:
            clauses.append("substr(COALESCE(f.created_at_wb,f.first_seen_at),1,10)<=?")
            params.append(_clean_text(scope_to))
        rows = conn.execute(
            f"""
            SELECT f.feedback_id, f.content_version, f.content_version_hash,
                   f.created_at_wb, f.first_seen_at, f.has_photo, f.has_video,
                   f.rating,
                   f.content_classification,
                   j.processing_key, j.state AS processing_state,
                   j.trigger_source, j.manual_started, j.final_route,
                   j.final_reply, j.final_reply_sha256, j.manual_reply,
                   j.manual_reply_sha256, j.manual_guard_passed,
                   j.hard_gates_passed, j.node_contract_valid,
                   j.fallback_used, j.media_uncertain,
                   j.regeneration_required, j.policy_epoch AS job_policy_epoch,
                   j.updated_at AS job_updated_at,
                   p.publication_key, p.state AS publication_state,
                   p.write_started_at, p.normalized_reply_sha256
            FROM sheet_vitrina_v1_wb_feedbacks f
            LEFT JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
              ON j.feedback_id=f.feedback_id
             AND j.content_version=f.content_version
             AND j.bundle_version=?
            LEFT JOIN sheet_vitrina_v1_wb_publication_jobs p
              ON p.processing_key=j.processing_key
            WHERE {' AND '.join(clauses)}
            ORDER BY {_automatic_priority_order_sql("f")}
            """,
            [PROMPT_BUNDLE_VERSION, *params],
        ).fetchall()
        projection = [dict(row) for row in rows]
        identity = [
            {
                "feedback_id": row["feedback_id"],
                "content_version": row["content_version"],
                "content_version_hash": row["content_version_hash"],
                "content_classification": row["content_classification"],
                "processing_key": row["processing_key"],
                "processing_state": row["processing_state"],
                "job_updated_at": row["job_updated_at"],
                "publication_key": row["publication_key"],
                "publication_state": row["publication_state"],
            }
            for row in projection
        ]
        return projection, sha256_text(canonical_json(identity))

    @staticmethod
    def _transition_counts(rows: Sequence[Mapping[str, Any]], *, target_mode: str) -> dict[str, int]:
        counts = {
            "unanswered_total": len(rows),
            "content_bearing": 0,
            "rating_only": 0,
            "indeterminate": 0,
            "content_bearing_current_ready": 0,
            "content_bearing_requires_openai": 0,
            "content_bearing_regeneration_required": 0,
            "content_bearing_needs_review": 0,
            "current_ready": 0,
            "zero_cost_templates": 0,
            "requires_openai": 0,
            "needs_generation": 0,
            "needs_regeneration": 0,
            "automatic_publication": 0,
            "expected_wb_writes": 0,
            "maximum_wb_writes": 0,
            "needs_review": 0,
            "expected_wb_writes_content_bearing": 0,
            "expected_wb_writes_rating_only": 0,
            "terminal_error": 0,
            "content_bearing_terminal_error": 0,
        }
        for row in rows:
            classification = str(row.get("content_classification") or CONTENT_CLASS_INDETERMINATE)
            if classification == CONTENT_CLASS_CONTENT_BEARING:
                counts["content_bearing"] += 1
            elif classification == CONTENT_CLASS_RATING_ONLY:
                counts["rating_only"] += 1
            else:
                counts["indeterminate"] += 1
                counts["needs_review"] += 1
                continue
            if classification == CONTENT_CLASS_RATING_ONLY and not row.get("final_reply"):
                counts["zero_cost_templates"] += 1
                counts["needs_generation"] += 1
                if target_mode in {MODE_AUTO_SAFE, MODE_AUTO_ALL}:
                    counts["expected_wb_writes"] += 1
                    counts["expected_wb_writes_rating_only"] += 1
                continue
            if not row.get("processing_key"):
                counts["needs_generation"] += 1
                counts["requires_openai"] += 1
                if classification == CONTENT_CLASS_CONTENT_BEARING:
                    counts["content_bearing_requires_openai"] += 1
                continue
            if bool(row.get("regeneration_required")) or bool(row.get("media_uncertain")):
                counts["needs_regeneration"] += 1
                counts["requires_openai"] += 1
                counts["needs_review"] += 1
                if classification == CONTENT_CLASS_CONTENT_BEARING:
                    counts["content_bearing_requires_openai"] += 1
                    counts["content_bearing_regeneration_required"] += 1
                    counts["content_bearing_needs_review"] += 1
                continue
            valid = bool(row.get("final_reply")) and bool(row.get("hard_gates_passed")) and bool(
                row.get("node_contract_valid")
            ) and not bool(row.get("fallback_used"))
            if row.get("processing_state") == STATE_TERMINAL_ERROR:
                counts["terminal_error"] += 1
                if classification == CONTENT_CLASS_CONTENT_BEARING:
                    counts["content_bearing_terminal_error"] += 1
                continue
            if row.get("processing_state") == STATE_NEEDS_REVIEW and not valid:
                counts["needs_review"] += 1
                if classification == CONTENT_CLASS_CONTENT_BEARING:
                    counts["content_bearing_needs_review"] += 1
                continue
            if not valid:
                if row.get("processing_state") in {
                    STATE_QUEUED,
                    STATE_PROCESSING,
                    STATE_RETRYABLE_ERROR,
                }:
                    counts["needs_generation"] += 1
                    counts["requires_openai"] += 1
                    if classification == CONTENT_CLASS_CONTENT_BEARING:
                        counts["content_bearing_requires_openai"] += 1
                    continue
                counts["needs_generation"] += 1
                counts["requires_openai"] += 1
                if classification == CONTENT_CLASS_CONTENT_BEARING:
                    counts["content_bearing_requires_openai"] += 1
                continue
            counts["current_ready"] += 1
            if classification == CONTENT_CLASS_CONTENT_BEARING:
                counts["content_bearing_current_ready"] += 1
            route = str(row.get("final_route") or "")
            # A human-edited reply is never silently adopted by an automatic
            # transition.  Even prior guard evidence belongs to that explicit
            # manual review flow; the preview must match reconciliation and
            # count it as review-only.
            manual_needs_validation = bool(row.get("manual_reply"))
            auto_allowed = (
                target_mode != MODE_DRAFT_ONLY
                and route != "seller_chat"
                and not manual_needs_validation
                and (target_mode == MODE_AUTO_ALL or route in AUTO_SAFE_ROUTES)
            )
            if auto_allowed:
                counts["automatic_publication"] += 1
                counts["expected_wb_writes"] += 1
                if classification == CONTENT_CLASS_CONTENT_BEARING:
                    counts["expected_wb_writes_content_bearing"] += 1
                else:
                    counts["expected_wb_writes_rating_only"] += 1
            elif target_mode != MODE_DRAFT_ONLY:
                counts["needs_review"] += 1
                if classification == CONTENT_CLASS_CONTENT_BEARING:
                    counts["content_bearing_needs_review"] += 1
        counts["maximum_wb_writes"] = counts["expected_wb_writes"] + (
            counts["requires_openai"]
            if target_mode in {MODE_AUTO_SAFE, MODE_AUTO_ALL}
            else 0
        )
        return counts

    def preview_mode_transition(
        self,
        target_selector_state: str,
        *,
        actor_id: str,
        scope_from: str = BACKFILL_FROM_DATE,
        scope_to: str | None = None,
        run_max_usd: Any | None = None,
        run_max_paid_reviews: int | None = None,
    ) -> dict[str, Any]:
        target = _clean_text(target_selector_state)
        if target not in AUTOANSWER_MODES:
            raise ValueError("transition preview requires an enabled mode")
        if target == MODE_MANUAL:
            raise ValueError("manual mode does not require a history sweep preview")
        try:
            datetime.fromisoformat(_clean_text(scope_from))
            if scope_to:
                datetime.fromisoformat(_clean_text(scope_to))
        except ValueError as exc:
            raise ValueError("scope dates must use YYYY-MM-DD") from exc
        max_usd = _money(run_max_usd) if run_max_usd not in (None, "") else None
        max_reviews = int(run_max_paid_reviews) if run_max_paid_reviews not in (None, "") else None
        if max_usd is None and max_reviews is None:
            raise AutoanswersRuntimeError(
                "a transition run requires max USD or max paid reviews",
                code="run_cap_required",
            )
        if max_usd is not None and max_usd <= 0:
            raise ValueError("run_max_usd must be positive")
        if max_reviews is not None and max_reviews <= 0:
            raise ValueError("run_max_paid_reviews must be positive")
        now = self._now()
        expires = now + timedelta(seconds=BACKLOG_PREVIEW_TTL_SECONDS)
        settings = self.settings()
        with self.transaction() as conn:
            rows, snapshot = self._transition_snapshot(conn, scope_from=scope_from, scope_to=scope_to)
            counts = self._transition_counts(rows, target_mode=target)
            budget = self.budget_status()
            estimated = DEFAULT_ESTIMATED_REVIEW_COST_USD * counts["requires_openai"]
            preview_id = uuid4().hex
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_autoanswers_transition_previews(
                    preview_id, target_selector_state, scope_from, scope_to,
                    snapshot_sha256, counts_json, estimated_cost_usd,
                    budget_json, enable_epoch, policy_epoch, created_by,
                    created_at, expires_at, run_max_usd, run_max_paid_reviews,
                    estimated_unit_cost_usd
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    preview_id,
                    target,
                    _clean_text(scope_from),
                    _clean_text(scope_to) or None,
                    snapshot,
                    canonical_json(counts),
                    str(estimated),
                    canonical_json(budget),
                    settings.enable_epoch,
                    settings.policy_epoch,
                    _clean_text(actor_id),
                    iso_utc(now),
                    iso_utc(expires),
                    str(max_usd) if max_usd is not None else None,
                    max_reviews,
                    str(DEFAULT_ESTIMATED_REVIEW_COST_USD),
                ),
            )
        return {
            "contract_version": AUTOANSWERS_CONTRACT_VERSION,
            "policy_version": DEFAULT_POLICY_VERSION,
            "preview_id": preview_id,
            "target_selector_state": target,
            "scope": {"from": _clean_text(scope_from), "to": _clean_text(scope_to) or None},
            "counts": counts,
            "estimated_cost_usd": float(estimated),
            "estimated_unit_cost_usd": float(DEFAULT_ESTIMATED_REVIEW_COST_USD),
            "run_cap": {
                "max_usd": float(max_usd) if max_usd is not None else None,
                "max_paid_reviews": max_reviews,
            },
            "estimated_duration_hours": (
                round(counts["requires_openai"] / max(1, settings.max_paid_reviews_per_hour), 2)
            ),
            "estimated_content_duration_hours": (
                round(
                    counts["content_bearing_requires_openai"]
                    / max(1, settings.max_paid_reviews_per_hour),
                    2,
                )
            ),
            "estimated_full_duration_hours": (
                round(
                    max(counts["content_bearing_requires_openai"], counts["unanswered_total"])
                    / max(1, settings.max_paid_reviews_per_hour),
                    2,
                )
            ),
            "budget": budget,
            "budget_after_estimate": {
                "daily_used_and_reserved_usd": budget["daily_used_and_reserved_usd"] + float(estimated),
                "monthly_used_and_reserved_usd": budget["monthly_used_and_reserved_usd"] + float(estimated),
                "daily_cap_usd": budget["daily_cap_usd"],
                "monthly_cap_usd": budget["monthly_cap_usd"],
            },
            "expires_at": iso_utc(expires),
        }

    def apply_mode_transition(
        self,
        target_selector_state: str,
        *,
        actor_id: str,
        preview_id: str | None = None,
    ) -> dict[str, Any]:
        target = _clean_text(target_selector_state)
        if target == "off":
            settings = self.update_settings(master_enabled=False, actor_id=actor_id)
            return {"settings": settings, "sweep": None}
        target = validate_mode(target)
        if target == MODE_MANUAL:
            settings = self.update_settings(master_enabled=True, mode=MODE_MANUAL, actor_id=actor_id)
            return {"settings": settings, "sweep": None}
        if self.settings().force_off:
            raise AutoanswersRuntimeError(
                "WB autoanswers is forced OFF by environment",
                code="emergency_force_off",
            )
        now = self._now()
        with self.transaction() as conn:
            preview = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_transition_previews WHERE preview_id=?",
                (_clean_text(preview_id),),
            ).fetchone()
            if preview is None:
                raise AutoanswersRuntimeError("mode transition preview is required", code="transition_preview_required")
            if preview["consumed_at"]:
                existing = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps WHERE preview_id=?",
                    (preview["preview_id"],),
                ).fetchone()
                if existing is not None:
                    return {"settings": self.settings(), "sweep": self._reconciliation_row(existing)}
                raise AutoanswersRuntimeError("transition preview already consumed", code="preview_consumed")
            if str(preview["created_by"]) != _clean_text(actor_id):
                raise AutoanswersRuntimeError(
                    "transition preview belongs to another actor",
                    code="preview_actor_mismatch",
                )
            if str(preview["target_selector_state"]) != target:
                raise AutoanswersRuntimeError("transition target changed", code="preview_target_mismatch")
            if parse_timestamp(preview["expires_at"]) <= now:
                raise AutoanswersRuntimeError("transition preview expired", code="preview_expired")
            current = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
            ).fetchone()
            if current is None:
                raise AutoanswersRuntimeError("autoanswers settings missing", code="settings_missing")
            if int(preview["enable_epoch"]) != int(current["enable_epoch"]) or int(preview["policy_epoch"]) != int(
                current["policy_epoch"]
            ):
                raise AutoanswersRuntimeError("mode state changed; create a new preview", code="preview_epoch_stale")
            rows, snapshot = self._transition_snapshot(
                conn, scope_from=str(preview["scope_from"]), scope_to=preview["scope_to"]
            )
            if snapshot != str(preview["snapshot_sha256"]):
                raise AutoanswersRuntimeError("review scope changed; create a new preview", code="preview_snapshot_stale")
            # A fresh, owner-confirmed capped preview is also the explicit way
            # to continue after the previous run cap while retaining the same
            # automatic mode.  It therefore creates a new policy epoch/run;
            # replaying the *same* consumed preview remains idempotent above.
            next_enable = int(current["enable_epoch"]) + int(not bool(current["master_enabled"]))
            next_policy = int(current["policy_epoch"]) + 1
            enabled_at = current["enabled_at"] or iso_utc(now)
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_settings
                SET master_enabled=1, mode=?, enable_epoch=?, policy_epoch=?,
                    enabled_at=?, updated_at=? WHERE singleton=1
                """,
                (target, next_enable, next_policy, enabled_at, iso_utc(now)),
            )
            counts = json.loads(str(preview["counts_json"]))
            sweep_id = uuid4().hex
            transition_run_id = sweep_id
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps(
                    sweep_id, preview_id, policy_epoch, target_mode, scope_from, scope_to,
                    state, cursor_json, totals_json, progress_json,
                    created_by, created_at, updated_at, transition_run_id,
                    run_max_usd, run_max_paid_reviews, pause_reason
                ) VALUES(?,?,?,?,?,?,'queued','{}',?,'{}',?,?,?,?,?,?,NULL)
                """,
                (
                    sweep_id,
                    preview["preview_id"],
                    next_policy,
                    target,
                    preview["scope_from"],
                    preview["scope_to"],
                    canonical_json(counts),
                    _clean_text(actor_id),
                    iso_utc(now),
                    iso_utc(now),
                    transition_run_id,
                    preview["run_max_usd"],
                    preview["run_max_paid_reviews"],
                ),
            )
            conn.executemany(
                """
                INSERT INTO sheet_vitrina_v1_wb_autoanswers_reconciliation_scope(
                    sweep_id,feedback_id,content_version_at_preview,
                    content_version_hash_at_preview,ordinal,
                    content_classification_at_preview
                ) VALUES(?,?,?,?,?,?)
                """,
                [
                    (
                        sweep_id,
                        str(row["feedback_id"]),
                        int(row["content_version"]),
                        str(row["content_version_hash"]),
                        ordinal,
                        str(row["content_classification"]),
                    )
                    for ordinal, row in enumerate(rows)
                ],
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_autoanswers_transition_previews SET consumed_at=? WHERE preview_id=?",
                (iso_utc(now), preview["preview_id"]),
            )
            self._audit(
                conn,
                aggregate_type="settings",
                aggregate_id="singleton",
                event_type="mode_transition_applied",
                actor_type="user",
                actor_id=_clean_text(actor_id),
                details={
                    "mode": target,
                    "policy_epoch": next_policy,
                    "preview_id": preview["preview_id"],
                    "sweep_id": sweep_id,
                    "transition_run_id": transition_run_id,
                    "run_max_usd": preview["run_max_usd"],
                    "run_max_paid_reviews": preview["run_max_paid_reviews"],
                },
                at=now,
            )
        return {"settings": self.settings(), "sweep": self.reconciliation_status(sweep_id)}

    @staticmethod
    def _reconciliation_row(row: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["totals"] = json.loads(str(result.pop("totals_json")))
        result["progress"] = json.loads(str(result.pop("progress_json")))
        result.pop("cursor_json", None)
        return result

    def reconciliation_status(self, sweep_id: str | None = None) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            if sweep_id:
                row = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps WHERE sweep_id=?",
                    (_clean_text(sweep_id),),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
        if row is None:
            return None
        return self._reconciliation_row(row)

    def _reconcile_feedback_for_policy(
        self,
        *,
        feedback_id: str,
        enable_epoch: int,
        policy_epoch: int,
        target_mode: str,
        transition_run_id: str,
        actor_id: str,
    ) -> str:
        now = self._now()
        regeneration_key: str | None = None
        outcome = "unchanged"
        with self.transaction() as conn:
            feedback = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
                (_clean_text(feedback_id),),
            ).fetchone()
            if feedback is None:
                return "missing"
            job = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs
                WHERE feedback_id=? AND content_version=? AND bundle_version=?
                """,
                (feedback_id, feedback["content_version"], PROMPT_BUNDLE_VERSION),
            ).fetchone()
            publication = (
                conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_wb_publication_jobs WHERE processing_key=?",
                    (job["processing_key"],),
                ).fetchone()
                if job is not None
                else None
            )
            if feedback["answer_text"]:
                if job is not None and str(job["state"]) != STATE_PUBLISHED:
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                        SET state=?, policy_epoch=?, policy_version=?, last_error_code='external_answer_present', updated_at=?
                        WHERE processing_key=?
                        """,
                        (STATE_SKIPPED, policy_epoch, DEFAULT_POLICY_VERSION, iso_utc(now), job["processing_key"]),
                    )
                return "external_answer_skipped"
            classification = str(
                feedback["content_classification"] or CONTENT_CLASS_INDETERMINATE
            )
            if classification == CONTENT_CLASS_INDETERMINATE:
                if job is None:
                    key = processing_key(str(feedback["feedback_id"]), int(feedback["content_version"]))
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswer_jobs(
                            processing_key, feedback_id, content_version,
                            content_version_hash, state, trigger_source,
                            bundle_version, evaluation_signature, policy_version,
                            enable_epoch, policy_epoch, processing_kind,
                            transition_run_id, review_reasons_json, available_at,
                            attempts, created_at, updated_at
                        ) VALUES(?,?,?,?,?,'policy_reconciliation',?,?,?,?,?,?,?,?,?,0,?,?)
                        """,
                        (
                            key,
                            feedback["feedback_id"],
                            feedback["content_version"],
                            feedback["content_version_hash"],
                            STATE_NEEDS_REVIEW,
                            PROMPT_BUNDLE_VERSION,
                            EVALUATION_SIGNATURE,
                            DEFAULT_POLICY_VERSION,
                            enable_epoch,
                            policy_epoch,
                            PROCESSING_KIND_FROZEN_AI,
                            transition_run_id,
                            canonical_json(["content_classification_indeterminate"]),
                            iso_utc(now),
                            iso_utc(now),
                            iso_utc(now),
                        ),
                    )
                    outcome = "classification_review_required"
                elif str(job["state"]) == STATE_PUBLISHED:
                    return "published_preserved"
                else:
                    publication = conn.execute(
                        "SELECT * FROM sheet_vitrina_v1_wb_publication_jobs WHERE processing_key=?",
                        (job["processing_key"],),
                    ).fetchone()
                    if publication is not None and publication["write_started_at"]:
                        return "readback_preserved"
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                        SET state=?, enable_epoch=?, policy_epoch=?, policy_version=?,
                            processing_kind=?, transition_run_id=?,
                            review_reasons_json=?, last_error_code='content_classification_indeterminate',
                            updated_at=? WHERE processing_key=?
                        """,
                        (
                            STATE_NEEDS_REVIEW,
                            enable_epoch,
                            policy_epoch,
                            DEFAULT_POLICY_VERSION,
                            PROCESSING_KIND_FROZEN_AI,
                            transition_run_id,
                            canonical_json(["content_classification_indeterminate"]),
                            iso_utc(now),
                            job["processing_key"],
                        ),
                    )
                    if publication is not None:
                        conn.execute(
                            "UPDATE sheet_vitrina_v1_wb_publication_jobs SET state=?, last_error_code='content_classification_indeterminate', updated_at=? WHERE publication_key=?",
                            (STATE_NEEDS_REVIEW, iso_utc(now), publication["publication_key"]),
                        )
                    outcome = "classification_review_required"
            elif job is None:
                key = processing_key(str(feedback["feedback_id"]), int(feedback["content_version"]))
                processing_kind = (
                    PROCESSING_KIND_RATING_ONLY_TEMPLATE
                    if str(feedback["content_classification"]) == CONTENT_CLASS_RATING_ONLY
                    else PROCESSING_KIND_FROZEN_AI
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswer_jobs(
                        processing_key, feedback_id, content_version,
                        content_version_hash, state, trigger_source,
                        bundle_version, evaluation_signature, policy_version,
                        enable_epoch, policy_epoch, processing_kind,
                        transition_run_id, available_at, attempts,
                        created_at, updated_at
                    ) VALUES(?,?,?,?,?,'policy_reconciliation',?,?,?,?,?,?,?,?,0,?,?)
                    """,
                    (
                        key,
                        feedback["feedback_id"],
                        feedback["content_version"],
                        feedback["content_version_hash"],
                        STATE_QUEUED,
                        PROMPT_BUNDLE_VERSION,
                        EVALUATION_SIGNATURE,
                        DEFAULT_POLICY_VERSION,
                        enable_epoch,
                        policy_epoch,
                        processing_kind,
                        transition_run_id,
                        iso_utc(now),
                        iso_utc(now),
                        iso_utc(now),
                    ),
                )
                outcome = "generation_queued"
            elif int(job["policy_epoch"] or 0) == policy_epoch:
                return "already_reconciled"
            elif str(job["state"]) == STATE_PUBLISHED:
                return "published_preserved"
            elif publication is not None and publication["write_started_at"]:
                # A possible write is reconciled only by readback; policy
                # transitions must never manufacture a second POST.
                conn.execute(
                    "UPDATE sheet_vitrina_v1_wb_autoanswer_jobs SET enable_epoch=?, policy_epoch=?, updated_at=? WHERE processing_key=?",
                    (enable_epoch, policy_epoch, iso_utc(now), job["processing_key"]),
                )
                return "readback_preserved"
            elif str(job["state"]) == STATE_SKIPPED:
                # A frozen prefilter result is already a terminal, zero-cost
                # evaluation for this immutable content/bundle identity.
                # Adopting a new policy epoch must not claim the same key
                # again: its settled reservation is immutable evidence and a
                # second claim would otherwise reach the provider boundary
                # without an active reservation.
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                    SET enable_epoch=?, policy_epoch=?, policy_version=?,
                        transition_run_id=?, updated_at=?
                    WHERE processing_key=?
                    """,
                    (
                        enable_epoch,
                        policy_epoch,
                        DEFAULT_POLICY_VERSION,
                        transition_run_id,
                        iso_utc(now),
                        job["processing_key"],
                    ),
                )
                outcome = "skipped_preserved"
            elif (
                str(job["state"]) == STATE_TERMINAL_ERROR
                or (
                    str(job["state"]) == STATE_NEEDS_REVIEW
                    and (
                        publication is not None
                        or not (
                            bool(job["regeneration_required"])
                            or bool(job["media_uncertain"])
                        )
                    )
                )
            ):
                # Human-only and terminal outcomes remain visible but are not
                # automatic work.  Adopting the new run identity must not turn
                # them back into paid generation or hold the rating-only gate.
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                    SET enable_epoch=?, policy_epoch=?, policy_version=?,
                        transition_run_id=?, updated_at=?
                    WHERE processing_key=?
                    """,
                    (
                        enable_epoch,
                        policy_epoch,
                        DEFAULT_POLICY_VERSION,
                        transition_run_id,
                        iso_utc(now),
                        job["processing_key"],
                    ),
                )
                if publication is not None:
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_wb_publication_jobs
                        SET policy_epoch=?, transition_run_id=?, updated_at=?
                        WHERE publication_key=?
                        """,
                        (
                            policy_epoch,
                            transition_run_id,
                            iso_utc(now),
                            publication["publication_key"],
                        ),
                    )
                outcome = (
                    "review_required_preserved"
                    if str(job["state"]) == STATE_NEEDS_REVIEW
                    else "terminal_error_preserved"
                )
            elif bool(job["regeneration_required"]) or bool(job["media_uncertain"]):
                if publication is None:
                    regeneration_key = str(job["processing_key"])
                    outcome = "regeneration_queued"
                else:
                    # Publication aggregates are immutable evidence for an
                    # exact reply.  They cannot be replaced by regeneration,
                    # so retain the row for explicit review instead of
                    # repeatedly calling request_regeneration(), which must
                    # continue to reject publication-bound jobs.
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                        SET state=?, enable_epoch=?, policy_epoch=?,
                            policy_version=?, transition_run_id=?,
                            last_error_code='publication_bound_regeneration_requires_review',
                            updated_at=? WHERE processing_key=?
                        """,
                        (
                            STATE_NEEDS_REVIEW,
                            enable_epoch,
                            policy_epoch,
                            DEFAULT_POLICY_VERSION,
                            transition_run_id,
                            iso_utc(now),
                            job["processing_key"],
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_wb_publication_jobs
                        SET state=?, policy_epoch=?, transition_run_id=?,
                            last_error_code='publication_bound_regeneration_requires_review',
                            updated_at=? WHERE publication_key=?
                        """,
                        (
                            STATE_NEEDS_REVIEW,
                            policy_epoch,
                            transition_run_id,
                            iso_utc(now),
                            publication["publication_key"],
                        ),
                    )
                    outcome = "publication_bound_regeneration_preserved"
            else:
                ready = (
                    bool(job["final_reply"])
                    and bool(job["hard_gates_passed"])
                    and bool(job["node_contract_valid"])
                    and not bool(job["fallback_used"])
                )
                if ready:
                    route = str(job["final_route"] or "")
                    manual_revalidation = bool(job["manual_reply"])
                    auto_allowed = (
                        target_mode != MODE_DRAFT_ONLY
                        and route != "seller_chat"
                        and not manual_revalidation
                        and (target_mode == MODE_AUTO_ALL or route in AUTO_SAFE_ROUTES)
                    )
                    next_state = STATE_APPROVED if auto_allowed else (
                        STATE_GENERATED if target_mode == MODE_DRAFT_ONLY else STATE_NEEDS_REVIEW
                    )
                    reasons: list[str] = []
                    if route == "seller_chat":
                        reasons.append("seller_chat_review_only")
                    if manual_revalidation:
                        reasons.append("manual_revalidation_required")
                    if target_mode == MODE_AUTO_SAFE and route not in AUTO_SAFE_ROUTES:
                        reasons.append("route_not_in_auto_safe_allowlist")
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                        SET state=?, enable_epoch=?, policy_epoch=?, policy_version=?, transition_run_id=?, review_reasons_json=?, updated_at=?
                        WHERE processing_key=?
                        """,
                        (next_state, enable_epoch, policy_epoch, DEFAULT_POLICY_VERSION, transition_run_id, canonical_json(reasons), iso_utc(now), job["processing_key"]),
                    )
                    adopted = dict(job)
                    adopted["policy_epoch"] = policy_epoch
                    adopted["transition_run_id"] = transition_run_id
                    if auto_allowed:
                        reply = str(job["final_reply"])
                        reply_sha = str(job["final_reply_sha256"])
                        if publication is None:
                            self._create_publication_job(
                                conn,
                                job=adopted,
                                reply=reply,
                                reply_sha=reply_sha,
                                request_source="automatic",
                                requested_by=None,
                                mode_at_enqueue=target_mode,
                                manual_edit_revision=None,
                                at=now,
                            )
                        elif not publication["write_started_at"] and str(
                            publication["normalized_reply_sha256"]
                        ) == reply_sha:
                            conn.execute(
                                """
                                UPDATE sheet_vitrina_v1_wb_publication_jobs
                                SET state=?, policy_epoch=?, mode_at_enqueue=?,
                                    transition_run_id=?, last_error_code=NULL, available_at=?, updated_at=?
                                WHERE publication_key=?
                                """,
                                (
                                    STATE_APPROVED,
                                    policy_epoch,
                                    target_mode,
                                    transition_run_id,
                                    iso_utc(now),
                                    iso_utc(now),
                                    publication["publication_key"],
                                ),
                            )
                        outcome = "publication_queued"
                    else:
                        if publication is not None and not publication["write_started_at"]:
                            conn.execute(
                                """
                                UPDATE sheet_vitrina_v1_wb_publication_jobs
                                SET state=?, last_error_code='policy_epoch_stale', updated_at=?
                                WHERE publication_key=?
                                """,
                                (STATE_NEEDS_REVIEW, iso_utc(now), publication["publication_key"]),
                            )
                        outcome = "draft_reused" if next_state == STATE_GENERATED else "review_required"
                elif str(job["state"]) in {STATE_QUEUED, STATE_PROCESSING, STATE_RETRYABLE_ERROR}:
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                        SET enable_epoch=?, policy_epoch=?, policy_version=?, trigger_source=CASE
                              WHEN manual_started=1 THEN trigger_source ELSE 'policy_reconciliation' END,
                            transition_run_id=?,
                            state=CASE WHEN state=? THEN ? ELSE state END,
                            available_at=CASE WHEN state=? THEN ? ELSE available_at END,
                            updated_at=? WHERE processing_key=?
                        """,
                        (
                            enable_epoch,
                            policy_epoch,
                            DEFAULT_POLICY_VERSION,
                            transition_run_id,
                            STATE_RETRYABLE_ERROR,
                            STATE_QUEUED,
                            STATE_RETRYABLE_ERROR,
                            iso_utc(now),
                            iso_utc(now),
                            job["processing_key"],
                        ),
                    )
                    outcome = "inflight_adopted"
                else:
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                        SET state=?, enable_epoch=?, policy_epoch=?, policy_version=?, transition_run_id=?, trigger_source='policy_reconciliation',
                            available_at=?, last_error_code=NULL, updated_at=?
                        WHERE processing_key=?
                        """,
                        (STATE_QUEUED, enable_epoch, policy_epoch, DEFAULT_POLICY_VERSION, transition_run_id, iso_utc(now), iso_utc(now), job["processing_key"]),
                    )
                    outcome = "generation_queued"
            self._audit(
                conn,
                aggregate_type="feedback",
                aggregate_id=str(feedback["feedback_id"]),
                event_type="policy_reconciled",
                actor_type="policy",
                actor_id=_clean_text(actor_id),
                details={"policy_epoch": policy_epoch, "mode": target_mode, "outcome": outcome, "transition_run_id": transition_run_id},
                at=now,
            )
        if regeneration_key:
            self.request_regeneration(
                regeneration_key,
                actor_id=actor_id,
                trigger_source="policy_reconciliation",
                transition_run_id=transition_run_id,
            )
        return outcome

    def reconcile_policy_sweep_once(
        self,
        *,
        worker_id: str,
        batch_size: int = 25,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        settings = self.assert_effective_on(operation="policy reconciliation")
        if settings.mode == MODE_MANUAL:
            return None
        now = self._now()
        lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
        queue_blocked = False
        paid_blocked = False
        pause_reason: str | None = None
        budget = self.budget_status()
        reservation = _money(settings.max_reservation_per_review_usd)
        if _money(budget["available_hourly_usd"]) < reservation:
            paid_blocked = True
            pause_reason = "hourly_budget_reached"
        elif _money(budget["available_daily_usd"]) < reservation:
            paid_blocked = True
            pause_reason = "daily_budget_reached"
        elif _money(budget["available_monthly_usd"]) < reservation:
            paid_blocked = True
            pause_reason = "monthly_budget_reached"
        elif budget["paid_reviews_last_hour"] >= settings.max_paid_reviews_per_hour:
            paid_blocked = True
            pause_reason = "paid_reviews_hourly_limit"
        with self.transaction() as conn:
            sweep = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
                WHERE policy_epoch=? AND (
                    state IN ('queued','retryable_error') OR
                    (state='processing' AND lease_until<=?)
                ) ORDER BY created_at LIMIT 1
                """,
                (settings.policy_epoch, iso_utc(now)),
            ).fetchone()
            if sweep is None:
                return None
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
                SET state='processing', lease_owner=?, lease_until=?, updated_at=?
                WHERE sweep_id=?
                """,
                (_clean_text(worker_id), iso_utc(lease_until), iso_utc(now), sweep["sweep_id"]),
            )
            content_pending = self._automatic_content_pending_count(
                conn,
                transition_run_id=str(sweep["transition_run_id"] or sweep["sweep_id"]),
                policy_epoch=settings.policy_epoch,
                target_mode=settings.mode,
            )
            outstanding = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sheet_vitrina_v1_wb_autoanswer_jobs j
                    JOIN sheet_vitrina_v1_wb_feedbacks f
                      ON f.feedback_id=j.feedback_id AND f.content_version=j.content_version
                    WHERE j.policy_epoch=? AND j.state IN (?,?,?)
                      AND (?=0 OR f.content_classification=?)
                    """,
                    (
                        settings.policy_epoch,
                        STATE_QUEUED,
                        STATE_PROCESSING,
                        STATE_RETRYABLE_ERROR,
                        content_pending,
                        CONTENT_CLASS_CONTENT_BEARING,
                    ),
                ).fetchone()[0]
            )
            capacity = max(0, settings.max_materialized_processing_jobs - outstanding)
            if capacity == 0:
                queue_blocked = True
                pause_reason = "processing_queue_depth_limit"
            run_usage = conn.execute(
                """
                SELECT COALESCE(SUM(CAST(actual_cost_usd AS REAL)),0) AS actual,
                       COALESCE(SUM(CASE WHEN status='reserved' THEN CAST(reserved_usd AS REAL) ELSE 0 END),0) AS reserved,
                       COALESCE(SUM(CASE WHEN status='settled' AND CAST(actual_cost_usd AS REAL)>0 THEN 1 ELSE 0 END),0) AS paid
                FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
                WHERE transition_run_id=?
                """,
                (str(sweep["transition_run_id"] or sweep["sweep_id"]),),
            ).fetchone()
            failed_run_usage = conn.execute(
                """
                SELECT COALESCE(SUM(CAST(actual_cost_usd AS REAL)),0) AS actual,
                       COALESCE(SUM(CASE WHEN CAST(actual_cost_usd AS REAL)>0 THEN 1 ELSE 0 END),0) AS paid
                FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events
                WHERE transition_run_id=?
                """,
                (str(sweep["transition_run_id"] or sweep["sweep_id"]),),
            ).fetchone()
            uncertainty_run_usage = _money(
                conn.execute(
                    """
                    SELECT COALESCE(SUM(upper_bound_usd),0)
                    FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds
                    WHERE transition_run_id=?
                    """,
                    (str(sweep["transition_run_id"] or sweep["sweep_id"]),),
                ).fetchone()[0]
            )
            if sweep["run_max_usd"] is not None and _money(run_usage["actual"]) + _money(failed_run_usage["actual"]) + _money(run_usage["reserved"]) + uncertainty_run_usage + _money(settings.max_reservation_per_review_usd) > _money(sweep["run_max_usd"]):
                paid_blocked = True
                pause_reason = "run_budget_reached"
            if sweep["run_max_paid_reviews"] is not None and int(run_usage["paid"] or 0) + int(failed_run_usage["paid"] or 0) >= int(sweep["run_max_paid_reviews"]):
                paid_blocked = True
                pause_reason = "run_review_limit_reached"
            has_exact_scope = bool(
                conn.execute(
                    "SELECT 1 FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope WHERE sweep_id=? LIMIT 1",
                    (sweep["sweep_id"],),
                ).fetchone()
            )
            membership_clause = (
                "AND EXISTS(SELECT 1 FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope rs "
                "WHERE rs.sweep_id=? AND rs.feedback_id=f.feedback_id "
                "AND rs.content_version_at_preview=f.content_version "
                "AND rs.content_version_hash_at_preview=f.content_version_hash)"
                if has_exact_scope
                else ""
            )
            scope_clause = "AND substr(COALESCE(f.created_at_wb,f.first_seen_at),1,10)<=?" if sweep["scope_to"] else ""
            params: list[Any] = [
                PROMPT_BUNDLE_VERSION,
                sweep["scope_from"],
                settings.policy_epoch,
                content_pending,
                CONTENT_CLASS_CONTENT_BEARING,
                int(queue_blocked),
                int(paid_blocked),
                CONTENT_CLASS_RATING_ONLY,
                CONTENT_CLASS_INDETERMINATE,
                STATE_NEEDS_REVIEW,
                STATE_TERMINAL_ERROR,
            ]
            if has_exact_scope:
                params.append(sweep["sweep_id"])
            if sweep["scope_to"]:
                params.append(sweep["scope_to"])
            params.append(min(25, max(1, min(int(batch_size), capacity if not queue_blocked else 25))))
            candidates = conn.execute(
                f"""
                SELECT f.feedback_id
                FROM sheet_vitrina_v1_wb_feedbacks f
                LEFT JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
                  ON j.feedback_id=f.feedback_id AND j.content_version=f.content_version
                 AND j.bundle_version=?
                WHERE COALESCE(f.answer_text,'')=''
                  AND substr(COALESCE(f.created_at_wb,f.first_seen_at),1,10)>=?
                  AND COALESCE(j.policy_epoch,-1)<>?
                  AND (?=0 OR f.content_classification=?)
                  AND (?=0 OR j.final_reply IS NOT NULL)
                  AND (?=0 OR j.final_reply IS NOT NULL
                       OR f.content_classification IN (?,?)
                       OR j.state IN (?,?))
                  {membership_clause}
                  {scope_clause}
                ORDER BY {_automatic_priority_order_sql("f")}
                LIMIT ?
                """,
                params,
            ).fetchall()
        outcomes: dict[str, int] = {}
        for candidate in candidates:
            name = self._reconcile_feedback_for_policy(
                feedback_id=str(candidate["feedback_id"]),
                enable_epoch=settings.enable_epoch,
                policy_epoch=settings.policy_epoch,
                target_mode=settings.mode,
                transition_run_id=str(sweep["transition_run_id"] or sweep["sweep_id"]),
                actor_id=worker_id,
            )
            outcomes[name] = outcomes.get(name, 0) + 1
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps WHERE sweep_id=?",
                (sweep["sweep_id"],),
            ).fetchone()
            progress = json.loads(str(current["progress_json"] or "{}"))
            for name, count in outcomes.items():
                progress[name] = int(progress.get(name) or 0) + count
            remaining_automatic_content = self._automatic_content_pending_count(
                conn,
                transition_run_id=str(sweep["transition_run_id"] or sweep["sweep_id"]),
                policy_epoch=settings.policy_epoch,
                target_mode=settings.mode,
            )
            state = (
                "queued"
                if candidates
                or queue_blocked
                or (paid_blocked and remaining_automatic_content > 0)
                else "succeeded"
            )
            cursor = {
                "last_feedback_id": str(candidates[-1]["feedback_id"]) if candidates else None,
                "materialized_total": sum(int(value) for value in progress.values()),
                "queue_depth": outstanding,
            }
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
                SET state=?, cursor_json=?, progress_json=?, pause_reason=?, lease_owner=NULL, lease_until=NULL,
                    completed_at=CASE WHEN ?='succeeded' THEN ? ELSE completed_at END,
                    updated_at=? WHERE sweep_id=?
                """,
                (
                    state,
                    canonical_json(cursor),
                    canonical_json(progress),
                    pause_reason,
                    state,
                    iso_utc(now),
                    iso_utc(now),
                    sweep["sweep_id"],
                ),
            )
        return self.reconciliation_status(str(sweep["sweep_id"]))

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
        if source.get("content_classification"):
            classification = _clean_text(source["content_classification"])
            if classification not in {
                CONTENT_CLASS_CONTENT_BEARING,
                CONTENT_CLASS_RATING_ONLY,
                CONTENT_CLASS_INDETERMINATE,
            }:
                raise ValueError("unsupported content classification filter")
            clauses.append("f.content_classification=?")
            params.append(classification)
        system_answer = _clean_text(source.get("system_answer"))
        system_filters = {
            "created": "j.final_reply IS NOT NULL",
            "missing": "j.final_reply IS NULL",
            "awaiting_generation": "(j.processing_key IS NULL OR j.state='queued')",
            "processing": "j.state='processing'",
            "needs_review": "j.state='needs_review'",
            "ready_publication": "j.state='approved'",
            "publication_queue": "p.state IN ('approved','publishing')",
            "published": "p.state='published'",
            "error": "(j.state IN ('retryable_error','terminal_error') OR p.state IN ('retryable_error','terminal_error'))",
        }
        if system_answer:
            if system_answer not in system_filters:
                raise ValueError("unsupported system_answer filter")
            clauses.append(system_filters[system_answer])
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
        latest_rows = """
            WITH latest_jobs AS (
                SELECT * FROM (
                    SELECT j2.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY j2.feedback_id
                               ORDER BY j2.content_version DESC, j2.created_at DESC, j2.processing_key DESC
                           ) AS latest_rank
                    FROM sheet_vitrina_v1_wb_autoanswer_jobs j2
                ) WHERE latest_rank=1
            ),
            latest_publications AS (
                SELECT * FROM (
                    SELECT p2.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY p2.feedback_id
                               ORDER BY p2.created_at DESC, p2.publication_key DESC
                           ) AS latest_rank
                    FROM sheet_vitrina_v1_wb_publication_jobs p2
                ) WHERE latest_rank=1
            )
        """
        join = """
            LEFT JOIN latest_jobs j ON j.feedback_id=f.feedback_id
            LEFT JOIN latest_publications p ON p.feedback_id=f.feedback_id
        """
        with closing(self._connect()) as conn:
            total = int(
                conn.execute(
                    f"{latest_rows} SELECT COUNT(*) FROM sheet_vitrina_v1_wb_feedbacks f {join} {where}",
                    params,
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                {latest_rows}
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
            "filter_hash": sha256_text(canonical_json(dict(sorted((str(key), str(value)) for key, value in source.items())))),
            "next_cursor": (
                f"{page_number + 1}:{sha256_text(canonical_json(dict(sorted((str(key), str(value)) for key, value in source.items()))))[:16]}"
                if page_number * size < total else None
            ),
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
            "content_classification": row.get("content_classification") or CONTENT_CLASS_INDETERMINATE,
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
            revisions = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_wb_autoanswer_job_revisions
                WHERE processing_key IN (
                    SELECT processing_key FROM sheet_vitrina_v1_wb_autoanswer_jobs WHERE feedback_id=?
                ) ORDER BY archived_at, revision_id
                """,
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
                "ai_revisions": [dict(item) for item in revisions],
                "audit": [dict(item) for item in audit],
            }
        )
        return result

    def media_asset(
        self,
        feedback_id: str,
        *,
        content_version: int,
        kind: str,
        ordinal: int,
        asset: str = "primary",
    ) -> tuple[Path, str] | None:
        normalized_kind = _clean_text(kind)
        normalized_asset = _clean_text(asset) or "primary"
        if normalized_kind not in {"photo", "video", "video_frame"} or normalized_asset not in {
            "primary",
            "preview",
        }:
            raise ValueError("unsupported media asset")
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_wb_feedback_media
                WHERE feedback_id=? AND content_version=? AND kind=? AND ordinal=?
                """,
                (_clean_text(feedback_id), int(content_version), normalized_kind, int(ordinal)),
            ).fetchone()
        if row is None:
            return None
        if normalized_asset == "preview":
            path_value = row["preview_local_path"]
            mime = str(row["preview_mime_type"] or "")
        else:
            path_value = row["local_path"]
            mime = str(row["mime_type"] or "")
        if not path_value:
            return None
        root = (self.runtime_dir / "wb_autoanswers_media").resolve()
        path = Path(str(path_value)).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise AutoanswersRuntimeError("media path escaped private storage", code="media_path_invalid") from exc
        if not path.is_file() or mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            return None
        return path, mime

    @staticmethod
    def _public_media_row(row: Mapping[str, Any]) -> dict[str, Any]:
        """Expose media state without private paths or signed WB URLs."""

        return {
            "kind": row.get("kind"),
            "ordinal": int(row.get("ordinal") or 0),
            "fetch_status": row.get("fetch_status"),
            "mime_type": row.get("mime_type"),
            "byte_size": row.get("byte_size"),
            "uncertainty_code": row.get("uncertainty_code"),
            "primary_available": bool(row.get("local_path")) and str(row.get("kind")) != "video",
            "preview_available": bool(row.get("preview_local_path")),
            "duration_seconds": row.get("duration_seconds"),
        }

    @staticmethod
    def _redact_public_value(value: Any, *, key: str = "") -> Any:
        """Remove private paths and signed URL queries from technical evidence."""

        normalized_key = key.casefold()
        if "local_path" in normalized_key or normalized_key in {"source_full_url", "source_url"}:
            return "[private]"
        if isinstance(value, Mapping):
            return {
                str(child_key): AutoanswersRepository._redact_public_value(child, key=str(child_key))
                for child_key, child in value.items()
            }
        if isinstance(value, list):
            return [AutoanswersRepository._redact_public_value(child, key=key) for child in value]
        if isinstance(value, str):
            return re.sub(
                r"https://[^\s\"'<>\]\)},]+",
                lambda match: stable_media_url(match.group(0)),
                value,
            )
        return value

    def public_feedback(self, feedback_id: str) -> dict[str, Any] | None:
        result = self.get_feedback(feedback_id)
        if result is None:
            return None
        result["media"] = [self._public_media_row(item) for item in result.get("media") or []]
        for job in result.get("ai_jobs") or []:
            if job.get("result_json"):
                try:
                    job["result_json"] = canonical_json(
                        self._redact_public_value(json.loads(str(job["result_json"])))
                    )
                except json.JSONDecodeError:
                    job["result_json"] = "[invalid technical payload]"
        for event in result.get("audit") or []:
            if event.get("details_json"):
                try:
                    event["details_json"] = canonical_json(
                        self._redact_public_value(json.loads(str(event["details_json"])))
                    )
                except json.JSONDecodeError:
                    event["details_json"] = "[invalid technical payload]"
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

    def media_canary_candidate(self, kind: str) -> dict[str, Any] | None:
        normalized = _clean_text(kind)
        if normalized not in {"photo", "video"}:
            raise ValueError("media canary kind must be photo or video")
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT f.feedback_id, f.content_version, m.fetch_status
                FROM sheet_vitrina_v1_wb_feedbacks f
                JOIN sheet_vitrina_v1_wb_feedback_media m
                  ON m.feedback_id=f.feedback_id
                 AND m.content_version=f.content_version
                 AND m.kind=?
                WHERE COALESCE(m.source_full_url,'')<>''
                  AND NOT EXISTS(
                    SELECT 1 FROM sheet_vitrina_v1_wb_publication_attempts a
                    JOIN sheet_vitrina_v1_wb_publication_jobs p
                      ON p.publication_key=a.publication_key
                    WHERE p.feedback_id=f.feedback_id
                  )
                ORDER BY CASE m.fetch_status WHEN 'fetch_failed' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                         COALESCE(f.created_at_wb,f.first_seen_at) DESC,
                         f.feedback_id DESC, m.ordinal
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return dict(row) if row is not None else None

    def expire_media_directory(self, directory: Path) -> None:
        """Make TTL cleanup observable so deleted bytes are never treated as downloaded."""

        prefix = str(Path(directory).absolute()) + os.sep
        now = self._now()
        with self.transaction() as conn:
            conn.execute(
                """
                DELETE FROM sheet_vitrina_v1_wb_feedback_media
                WHERE kind='video_frame' AND substr(local_path,1,?)=?
                """,
                (len(prefix), prefix),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_feedback_media
                SET fetch_status='pending', local_path=NULL, sha256=NULL,
                    mime_type=NULL, byte_size=NULL, preview_local_path=NULL,
                    preview_sha256=NULL, preview_mime_type=NULL,
                    preview_byte_size=NULL, uncertainty_code=NULL, updated_at=?
                WHERE kind IN ('photo','video')
                  AND (substr(local_path,1,?)=? OR substr(preview_local_path,1,?)=?)
                """,
                (iso_utc(now), len(prefix), prefix, len(prefix), prefix),
            )

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
        preview: Mapping[str, Any] | None = None,
    ) -> None:
        now = self._now()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_feedback_media
                SET fetch_status=?, local_path=?, sha256=?, mime_type=?, byte_size=?,
                    preview_local_path=?, preview_sha256=?, preview_mime_type=?,
                    preview_byte_size=?, uncertainty_code=?, updated_at=?
                WHERE feedback_id=? AND content_version=? AND kind=? AND ordinal=?
                """,
                (
                    _clean_text(fetch_status),
                    local_path,
                    sha256,
                    mime_type,
                    byte_size,
                    (preview or {}).get("local_path"),
                    (preview or {}).get("sha256"),
                    (preview or {}).get("mime_type"),
                    (preview or {}).get("byte_size"),
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
            for ordinal, frame in enumerate(frames[:4]):
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
    policy_epoch INTEGER NOT NULL DEFAULT 0,
    enabled_at TEXT,
    daily_cap_usd TEXT NOT NULL,
    monthly_cap_usd TEXT NOT NULL,
    hourly_cap_usd TEXT NOT NULL DEFAULT '0.50',
    max_paid_reviews_per_hour INTEGER NOT NULL DEFAULT 20,
    global_paid_review_concurrency INTEGER NOT NULL DEFAULT 1,
    max_inflight_role_calls INTEGER NOT NULL DEFAULT 1,
    max_materialized_processing_jobs INTEGER NOT NULL DEFAULT 5,
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
    last_sync_run_id TEXT,
    content_classification TEXT NOT NULL DEFAULT 'indeterminate'
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
    preview_local_path TEXT,
    preview_sha256 TEXT,
    preview_mime_type TEXT,
    preview_byte_size INTEGER,
    expires_at TEXT,
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
    policy_epoch INTEGER NOT NULL DEFAULT 0,
    media_processing_version INTEGER NOT NULL DEFAULT 1,
    regeneration_required INTEGER NOT NULL DEFAULT 0,
    regeneration_reason TEXT,
    processing_kind TEXT NOT NULL DEFAULT 'frozen_ai',
    transition_run_id TEXT,
    manual_started INTEGER NOT NULL DEFAULT 0,
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
    policy_epoch INTEGER NOT NULL DEFAULT 0,
    transition_run_id TEXT,
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
    transition_run_id TEXT,
    expires_at TEXT,
    provider_call_started_at TEXT,
    released_reason TEXT,
    settled_at TEXT,
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
    consumed_at TEXT,
    run_max_usd TEXT,
    run_max_paid_reviews INTEGER,
    estimated_unit_cost_usd TEXT
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswer_job_revisions(
    revision_id TEXT PRIMARY KEY,
    processing_key TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_autoanswer_jobs(processing_key),
    media_processing_version INTEGER NOT NULL,
    previous_state TEXT NOT NULL,
    result_json TEXT,
    final_route TEXT,
    final_reply TEXT,
    final_reply_sha256 TEXT,
    media_uncertain INTEGER,
    actual_cost_usd TEXT NOT NULL DEFAULT '0',
    reason TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    UNIQUE(processing_key, media_processing_version)
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_cost_events(
    event_id TEXT PRIMARY KEY,
    processing_key TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_autoanswer_jobs(processing_key),
    media_processing_version INTEGER NOT NULL,
    actual_cost_usd TEXT NOT NULL,
    incurred_at TEXT NOT NULL,
    UNIQUE(processing_key, media_processing_version)
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_transition_previews(
    preview_id TEXT PRIMARY KEY,
    target_selector_state TEXT NOT NULL,
    scope_from TEXT NOT NULL,
    scope_to TEXT,
    snapshot_sha256 TEXT NOT NULL,
    counts_json TEXT NOT NULL,
    estimated_cost_usd TEXT NOT NULL,
    budget_json TEXT NOT NULL,
    enable_epoch INTEGER NOT NULL,
    policy_epoch INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps(
    sweep_id TEXT PRIMARY KEY,
    preview_id TEXT,
    policy_epoch INTEGER NOT NULL UNIQUE,
    target_mode TEXT NOT NULL,
    scope_from TEXT NOT NULL,
    scope_to TEXT,
    state TEXT NOT NULL CHECK(state IN ('queued','processing','succeeded','retryable_error','terminal_error')),
    cursor_json TEXT NOT NULL,
    totals_json TEXT NOT NULL,
    progress_json TEXT NOT NULL,
    lease_owner TEXT,
    lease_until TEXT,
    last_error_code TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    transition_run_id TEXT,
    run_max_usd TEXT,
    run_max_paid_reviews INTEGER,
    pause_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_sv1_policy_sweeps_claim
ON sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps(state, lease_until, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sv1_policy_sweeps_preview
ON sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps(preview_id)
WHERE preview_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_budget_adjustments(
    adjustment_id TEXT PRIMARY KEY,
    processing_key TEXT,
    amount_usd TEXT NOT NULL,
    reason TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_failed_cost_events(
    event_id TEXT PRIMARY KEY,
    processing_key TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_autoanswer_jobs(processing_key),
    attempt_number INTEGER NOT NULL,
    transition_run_id TEXT,
    actual_cost_usd TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    role_calls INTEGER NOT NULL,
    error_code TEXT NOT NULL,
    incurred_at TEXT NOT NULL,
    UNIQUE(processing_key, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_sv1_failed_cost_incurred
ON sheet_vitrina_v1_wb_autoanswers_failed_cost_events(incurred_at, transition_run_id);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds(
    hold_id TEXT PRIMARY KEY,
    processing_key TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_autoanswer_jobs(processing_key),
    transition_run_id TEXT,
    upper_bound_usd TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(processing_key)
);
CREATE INDEX IF NOT EXISTS idx_sv1_budget_uncertainty_effective
ON sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds(effective_at, transition_run_id);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_reconciliation_scope(
    sweep_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps(sweep_id),
    feedback_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_wb_feedbacks(feedback_id),
    content_version_at_preview INTEGER NOT NULL,
    content_version_hash_at_preview TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    content_classification_at_preview TEXT NOT NULL DEFAULT 'indeterminate',
    PRIMARY KEY(sweep_id,feedback_id)
);
CREATE INDEX IF NOT EXISTS idx_sv1_reconciliation_scope_feedback
ON sheet_vitrina_v1_wb_autoanswers_reconciliation_scope(feedback_id,sweep_id);

CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_autoanswers_runtime_state(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    stop_reason TEXT,
    stop_details_json TEXT NOT NULL DEFAULT '{}',
    last_scheduler_tick_at TEXT,
    last_successful_ai_call_at TEXT,
    last_confirmed_publication_at TEXT,
    updated_at TEXT NOT NULL
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
