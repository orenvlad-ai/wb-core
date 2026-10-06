"""Official daily WB Finance management report, isolated from weekly evidence."""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from packages.adapters.wb_finance_api import FinanceApiError, WbFinanceApiClient
from packages.application.wb_finance_weekly import (
    CLASSIFIER_VERSION, MOSCOW, WbFinanceWeeklyBlock,
    _nomenclature_identity_index,
)
from packages.application.canonical_wb_cost_resolver import CanonicalChannelCostSnapshot
from packages.application.wb_finance_spp import project_spp


DAILY_CONTRACT_VERSION = "wb_finance_daily_v1"
DAILY_FORMULA_VERSION = "wb_finance_daily_aggregate_v2_scoped_cost_dependencies"
DAILY_INITIAL_DAYS = 14


def closed_daily_dates(now: datetime, *, limit: int = DAILY_INITIAL_DAYS) -> list[date]:
    if limit < 1 or limit > DAILY_INITIAL_DAYS:
        raise ValueError("daily history limit must be 1..14")
    latest = now.astimezone(MOSCOW).date() - timedelta(days=1)
    return [latest - timedelta(days=offset) for offset in range(limit - 1, -1, -1)]


def _source_hash(rows: list[Mapping[str, Any]]) -> str:
    return hashlib.sha256(
        "\n".join(sorted(WbFinanceWeeklyBlock._row_hash(row) for row in rows)).encode("utf-8")
    ).hexdigest()


class WbFinanceDailyBlock(WbFinanceWeeklyBlock):
    allocation_raw_table = "wb_finance_daily_current_rows"
    allocation_sync_table = "wb_finance_daily_sync"

    def _global_capitalization_allocations(self, conn):
        # Weekly's cache key is sync-based. Daily's raw pointer advances before
        # sync after a complete fetch, so never reuse an allocation from a
        # different read connection on the same block instance.
        if self._capitalization_cache_connection is not conn:
            self._capitalization_cache_key = ""
            self._capitalization_cache = {}
            self._capitalization_cache_connection = None
        return super()._global_capitalization_allocations(conn)

    def ensure_schema(self) -> None:
        # Weekly and daily share cost-side operational dependencies, not source rows.
        super().ensure_schema()
        with self.store_registry.session(
            "finance_raw", mode="rw", operation="wb_finance_daily_raw_schema"
        ) as raw:
            raw.executescript(
                """
                CREATE TABLE IF NOT EXISTS wb_finance_daily_batches (
                    batch_id TEXT PRIMARY KEY, seller_id TEXT NOT NULL,
                    report_day TEXT NOT NULL, content_hash TEXT NOT NULL,
                    provider_digest TEXT NOT NULL, row_count INTEGER NOT NULL,
                    terminal_status INTEGER NOT NULL CHECK(terminal_status=204),
                    fetched_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS wb_finance_daily_batches_scope
                ON wb_finance_daily_batches(seller_id,report_day,fetched_at);
                CREATE TABLE IF NOT EXISTS wb_finance_daily_raw_rows (
                    batch_id TEXT NOT NULL, seller_id TEXT NOT NULL,
                    report_day TEXT NOT NULL, report_id TEXT NOT NULL,
                    rrd_id TEXT NOT NULL, row_hash TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY(batch_id,report_id,rrd_id),
                    FOREIGN KEY(batch_id) REFERENCES wb_finance_daily_batches(batch_id)
                );
                CREATE INDEX IF NOT EXISTS wb_finance_daily_rows_batch
                ON wb_finance_daily_raw_rows(batch_id,report_id,rrd_id);
                CREATE TABLE IF NOT EXISTS wb_finance_daily_pointers (
                    seller_id TEXT NOT NULL, report_day TEXT NOT NULL,
                    batch_id TEXT NOT NULL, content_hash TEXT NOT NULL,
                    row_count INTEGER NOT NULL, selected_at TEXT NOT NULL,
                    PRIMARY KEY(seller_id,report_day),
                    FOREIGN KEY(batch_id) REFERENCES wb_finance_daily_batches(batch_id)
                );
                CREATE VIEW IF NOT EXISTS wb_finance_daily_current_rows AS
                SELECT r.seller_id,r.report_day AS week_start,
                       r.report_day AS week_end,r.report_id,r.rrd_id,
                       r.row_hash,r.raw_json
                  FROM wb_finance_daily_pointers AS p
                  JOIN wb_finance_daily_raw_rows AS r ON r.batch_id=p.batch_id
                 WHERE r.seller_id=p.seller_id AND r.report_day=p.report_day;
                """
            )
            raw.commit()
        with self.store_registry.session(
            "operational", mode="rw", operation="wb_finance_daily_operational_schema"
        ) as operational:
            operational.executescript(
                """
                CREATE TABLE IF NOT EXISTS wb_finance_daily_sync (
                    seller_id TEXT NOT NULL, report_day TEXT NOT NULL,
                    week_start TEXT NOT NULL, week_end TEXT NOT NULL,
                    batch_id TEXT, content_hash TEXT, raw_row_count INTEGER,
                    status TEXT NOT NULL, unchanged_count INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    first_loaded_at TEXT, last_synced_at TEXT,
                    next_retry_at TEXT, last_error TEXT,
                    PRIMARY KEY(seller_id,report_day),
                    CHECK(week_start=report_day AND week_end=report_day)
                );
                CREATE TABLE IF NOT EXISTS wb_finance_daily_config (
                    seller_id TEXT PRIMARY KEY, initial_day TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wb_finance_daily_aggregates (
                    seller_id TEXT NOT NULL, report_day TEXT NOT NULL,
                    batch_id TEXT NOT NULL, content_hash TEXT NOT NULL,
                    raw_row_count INTEGER NOT NULL,
                    formula_version TEXT NOT NULL, classifier_version TEXT NOT NULL,
                    allocation_dependency_hash TEXT NOT NULL,
                    cost_state_hash TEXT NOT NULL, cost_source_hash TEXT NOT NULL,
                    metrics_json TEXT NOT NULL, coverage_json TEXT NOT NULL,
                    unknown_reasons_json TEXT NOT NULL, report_ids_json TEXT NOT NULL,
                    report_types_json TEXT NOT NULL, calculated_at TEXT NOT NULL,
                    PRIMARY KEY(seller_id,report_day)
                );
                """
            )
            columns = {str(row[1]) for row in operational.execute(
                "PRAGMA table_info(wb_finance_daily_aggregates)"
            )}
            for column in ("cost_state_hash", "cost_source_hash"):
                if column not in columns:
                    operational.execute(
                        f"ALTER TABLE wb_finance_daily_aggregates ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            now = self.now_factory().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            operational.execute(
                "INSERT OR IGNORE INTO wb_finance_daily_config VALUES(?,?,?)",
                (self.seller_id, closed_daily_dates(self.now_factory())[0].isoformat(), now),
            )
            operational.commit()

    def _connect_daily_read(self):
        self._pin_active_cost()
        manifest = self.store_registry.load()
        conn = self.store_registry.connect(
            "operational", mode="ro", operation="wb_finance_daily_projection_read",
            manifest=manifest,
        )
        if manifest.state == "cutover" and manifest.canonical_source == "split":
            try:
                self.store_registry.attach_readonly(
                    conn, "finance_raw", schema_name="finance_daily_raw_store",
                    operation="wb_finance_daily_raw_read", manifest=manifest,
                )
                conn.execute("PRAGMA query_only=OFF")
                conn.execute(
                    """CREATE TEMP VIEW wb_finance_daily_current_rows AS
                       SELECT seller_id,week_start,week_end,report_id,rrd_id,
                              row_hash,raw_json
                         FROM finance_daily_raw_store.wb_finance_daily_current_rows"""
                )
                conn.execute("PRAGMA query_only=ON")
            except Exception:
                conn.close()
                raise
        return conn

    def _raw_pointer_table(self, conn) -> str:
        return (
            "finance_daily_raw_store.wb_finance_daily_pointers"
            if any(str(row[1]) == "finance_daily_raw_store"
                   for row in conn.execute("PRAGMA database_list"))
            else "wb_finance_daily_pointers"
        )

    def _allocation_dependency_hash(self, conn, day: date) -> str:
        """Bind capped addbacks to the entire daily allocation universe."""

        pointers = [list(row) for row in conn.execute(
            f"""SELECT report_day,batch_id,content_hash,row_count
                  FROM {self._raw_pointer_table(conn)}
                 WHERE seller_id=? ORDER BY report_day""",
            (self.seller_id,),
        )]
        layer_exists = conn.execute(
            """SELECT 1 FROM sqlite_master WHERE type='table'
               AND name='sheet_vitrina_v1_wb_supply_cost_layers'"""
        ).fetchone()
        layers = (
            [list(row) for row in conn.execute(
                """SELECT wb_supply_cost_layer_id,wb_supply_id,nm_id,
                          transit_cost_status,transit_amount_total,
                          wb_acceptance_amount_total,inputs_hash,version,is_current
                     FROM sheet_vitrina_v1_wb_supply_cost_layers
                    ORDER BY wb_supply_cost_layer_id,version"""
            )]
            if layer_exists else []
        )
        # The shared cap allocator can resolve a missing nmId from an alias in
        # any daily report, then change another day's capped addback. Bind the
        # whole allocation universe to effective identity mappings, excluding
        # catalogue metadata/timestamps that do not affect resolution.
        alias_to_nm, ambiguous_aliases, _groups, _items = (
            _nomenclature_identity_index(conn)
        )
        return hashlib.sha256(json.dumps(
            {"pointers": pointers, "layers": layers,
             "aliases": sorted(alias_to_nm.items()),
             "ambiguous_aliases": sorted(ambiguous_aliases)},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def _cost_source_hash(self, conn, day: date,
                          snapshot: CanonicalChannelCostSnapshot | None = None) -> str:
        # This is a source dependency, separate from the COGS result hash.
        # The weekly helper resolves exact operation-date/SKU/source identities.
        exact = str(self._finance_source_dependency_fingerprint(
            conn, target_keys={(self.seller_id, day.isoformat(), day.isoformat())},
            raw_table="wb_finance_daily_current_rows", report_table=None,
            snapshot_override=snapshot, scoped_daily_dependencies=True,
        )["digest"])
        return hashlib.sha256(json.dumps(
            ["daily_cost_dependencies_v2", exact], sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")).hexdigest()

    def _store_complete_raw(self, day: date, rows: list[dict[str, Any]],
                            provider_digest: str) -> dict[str, Any]:
        day_text = day.isoformat()
        normalized: list[dict[str, Any]] = []
        identities: set[tuple[str, str]] = set()
        for source in rows:
            row = dict(source)
            report_id, rrd_id = str(row.get("reportId") or ""), str(row.get("rrdId") or "")
            if not report_id or not rrd_id or (report_id, rrd_id) in identities:
                raise ValueError("daily Finance row identity is missing or duplicated")
            if str(row.get("dateFrom") or "")[:10] != day_text or str(row.get("dateTo") or "")[:10] != day_text:
                raise ValueError("daily Finance row has a different report date")
            if str(row.get("currency") or "RUB") != "RUB":
                raise ValueError("daily Finance row currency is not RUB")
            identities.add((report_id, rrd_id))
            row["reportId"], row["rrdId"] = report_id, rrd_id
            normalized.append(row)
        if not normalized:
            raise ValueError("empty daily Finance report is not qualified")
        content_hash = _source_hash(normalized)
        batch_id = hashlib.sha256(
            f"daily/{self.seller_id}/{day_text}/{content_hash}".encode("utf-8")
        ).hexdigest()
        now = self.now_factory().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.store_registry.session(
            "finance_raw", mode="rw", operation="wb_finance_daily_raw_commit",
            isolation_level=None,
        ) as raw:
            raw.execute("BEGIN IMMEDIATE")
            raw.execute(
                """INSERT OR IGNORE INTO wb_finance_daily_batches
                   VALUES(?,?,?,?,?,?,204,?)""",
                (batch_id, self.seller_id, day_text, content_hash,
                 provider_digest, len(normalized), now),
            )
            for row in normalized:
                raw_json = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                raw.execute(
                    """INSERT OR IGNORE INTO wb_finance_daily_raw_rows
                       VALUES(?,?,?,?,?,?,?)""",
                    (batch_id, self.seller_id, day_text, row["reportId"],
                     row["rrdId"], self._row_hash(row), raw_json),
                )
            actual = raw.execute(
                "SELECT COUNT(*) FROM wb_finance_daily_raw_rows WHERE batch_id=?",
                (batch_id,),
            ).fetchone()[0]
            if int(actual) != len(normalized):
                raise ValueError("daily Finance immutable batch row count mismatch")
            previous = raw.execute(
                """SELECT batch_id FROM wb_finance_daily_pointers
                   WHERE seller_id=? AND report_day=?""",
                (self.seller_id, day_text),
            ).fetchone()
            raw.execute(
                """INSERT INTO wb_finance_daily_pointers VALUES(?,?,?,?,?,?)
                   ON CONFLICT(seller_id,report_day) DO UPDATE SET
                   batch_id=excluded.batch_id,content_hash=excluded.content_hash,
                   row_count=excluded.row_count,selected_at=excluded.selected_at""",
                (self.seller_id, day_text, batch_id, content_hash, len(normalized), now),
            )
            raw.commit()
        return {"batch_id": batch_id, "content_hash": content_hash,
                "raw_row_count": len(normalized),
                "changed": previous is None or str(previous["batch_id"]) != batch_id}

    def _prepare_projection(self, reader, day: date, *,
                            cost_snapshot: CanonicalChannelCostSnapshot | None = None) -> dict[str, Any]:
        day_text = day.isoformat()
        pointer = reader.execute(
            f"SELECT batch_id,content_hash,row_count FROM {self._raw_pointer_table(reader)} "
            "WHERE seller_id=? AND report_day=?", (self.seller_id, day_text),
        ).fetchone()
        if pointer is None:
            return {"status": "no_raw_pointer", "report_day": day_text}
        stored = reader.execute(
            """SELECT report_id,rrd_id,row_hash,raw_json
               FROM wb_finance_daily_current_rows
               WHERE seller_id=? AND week_start=? ORDER BY report_id,rrd_id""",
            (self.seller_id, day_text),
        ).fetchall()
        content_hash = hashlib.sha256(
            "\n".join(sorted(str(row["row_hash"]) for row in stored)).encode("utf-8")
        ).hexdigest()
        if content_hash != str(pointer["content_hash"]) or len(stored) != int(pointer["row_count"]):
            raise ValueError("daily Finance current raw membership mismatch")
        rows = [json.loads(row["raw_json"]) for row in stored]
        allocation_dependency_hash = self._allocation_dependency_hash(reader, day)
        metrics, coverage, unknown = self._aggregate_rows(reader, rows, day)
        cost_source_hash = self._cost_source_hash(reader, day, cost_snapshot)
        cost_state_hash = str(coverage["cost_state_hash"])
        spp = project_spp(rows)
        metrics.update(spp_fbo_pct=spp["spp_fbo_pct"], spp_fbs_pct=spp["spp_fbs_pct"])
        return {"day": day, "batch_id": str(pointer["batch_id"]),
                "content_hash": content_hash, "raw_row_count": len(stored),
                "allocation_dependency_hash": allocation_dependency_hash,
                "cost_source_hash": cost_source_hash, "cost_state_hash": cost_state_hash,
                "metrics": metrics, "coverage": coverage, "unknown": unknown,
                "spp": spp, "report_ids": sorted({str(row["reportId"]) for row in rows}),
                "report_types": sorted({int(row.get("reportType") or 0) for row in rows})}

    def project_pointer(self, day: date, *, unchanged_fetch: bool = False) -> dict[str, Any]:
        """Idempotently derive the exact raw pointer; safe after a crash."""
        with closing(self._connect_daily_read()) as reader:
            reader.execute("BEGIN")
            prepared = self._prepare_projection(reader, day)
            reader.rollback()
        return self._commit_projection(prepared, unchanged_fetch=unchanged_fetch)

    def _commit_projection(self, prepared: dict[str, Any], *,
                           unchanged_fetch: bool = False) -> dict[str, Any]:
        if prepared.get("status") == "no_raw_pointer":
            return prepared
        day = prepared["day"]
        day_text = day.isoformat()
        batch_id = prepared["batch_id"]
        content_hash = prepared["content_hash"]
        raw_row_count = prepared["raw_row_count"]
        allocation_dependency_hash = prepared["allocation_dependency_hash"]
        cost_source_hash = prepared["cost_source_hash"]
        cost_state_hash = prepared["cost_state_hash"]
        metrics, coverage, unknown = prepared["metrics"], prepared["coverage"], prepared["unknown"]
        spp, report_ids, report_types = prepared["spp"], prepared["report_ids"], prepared["report_types"]
        with closing(self._connect_daily_read()) as verifier:
            verifier.execute("BEGIN")
            pointer = verifier.execute(
                f"SELECT batch_id,content_hash,row_count FROM {self._raw_pointer_table(verifier)} "
                "WHERE seller_id=? AND report_day=?", (self.seller_id, day_text),
            ).fetchone()
            allocation_current = self._allocation_dependency_hash(verifier, day)
            verifier.rollback()
        if (pointer is None or pointer["batch_id"] != batch_id
            or pointer["content_hash"] != content_hash
            or int(pointer["row_count"]) != raw_row_count
            or allocation_current != allocation_dependency_hash):
            return {"status": "source_advanced", "report_day": day_text}
        now = self.now_factory().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.store_registry.session(
            "operational", mode="rw", operation="wb_finance_daily_projection_commit",
            isolation_level=None,
        ) as writer:
            writer.execute("BEGIN IMMEDIATE")
            previous = writer.execute(
                """SELECT batch_id,content_hash,unchanged_count,first_loaded_at
                   FROM wb_finance_daily_sync WHERE seller_id=? AND report_day=?""",
                (self.seller_id, day_text),
            ).fetchone()
            same = bool(previous and previous["batch_id"] == batch_id
                        and previous["content_hash"] == content_hash)
            unchanged_count = (int(previous["unchanged_count"] or 0) + 1
                               if same and unchanged_fetch else
                               int(previous["unchanged_count"] or 0) if same else 0)
            age = (self.now_factory().astimezone(MOSCOW).date() - day).days
            status = "completed" if age >= 3 or unchanged_count >= 1 else "loaded_preliminary"
            writer.execute(
                """INSERT INTO wb_finance_daily_sync
                   (seller_id,report_day,week_start,week_end,batch_id,content_hash,
                    raw_row_count,status,unchanged_count,attempt_count,first_loaded_at,
                    last_synced_at,next_retry_at,last_error)
                   VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,NULL)
                   ON CONFLICT(seller_id,report_day) DO UPDATE SET
                   batch_id=excluded.batch_id,content_hash=excluded.content_hash,
                   raw_row_count=excluded.raw_row_count,status=excluded.status,
                   unchanged_count=excluded.unchanged_count,
                   attempt_count=attempt_count+1,last_synced_at=excluded.last_synced_at,
                   next_retry_at=excluded.next_retry_at,last_error=NULL""",
                (self.seller_id, day_text, day_text, day_text, batch_id, content_hash,
                 raw_row_count, status, unchanged_count,
                 previous["first_loaded_at"] if previous and previous["first_loaded_at"] else now,
                 now, now if status == "loaded_preliminary" else None),
            )
            writer.execute(
                """INSERT INTO wb_finance_daily_aggregates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(seller_id,report_day) DO UPDATE SET
                   batch_id=excluded.batch_id,content_hash=excluded.content_hash,
                   raw_row_count=excluded.raw_row_count,
                   formula_version=excluded.formula_version,
                   classifier_version=excluded.classifier_version,
                   allocation_dependency_hash=excluded.allocation_dependency_hash,
                   cost_state_hash=excluded.cost_state_hash,
                   cost_source_hash=excluded.cost_source_hash,
                   metrics_json=excluded.metrics_json,coverage_json=excluded.coverage_json,
                   unknown_reasons_json=excluded.unknown_reasons_json,
                   report_ids_json=excluded.report_ids_json,
                   report_types_json=excluded.report_types_json,
                   calculated_at=excluded.calculated_at""",
                (self.seller_id, day_text, batch_id, content_hash, raw_row_count,
                 DAILY_FORMULA_VERSION, CLASSIFIER_VERSION, allocation_dependency_hash,
                 cost_state_hash, cost_source_hash,
                 json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                 json.dumps({**coverage, "spp": spp}, ensure_ascii=False, sort_keys=True, default=str),
                 json.dumps(unknown, ensure_ascii=False),
                 json.dumps(report_ids, ensure_ascii=False),
                 json.dumps(report_types, ensure_ascii=False), now),
            )
            writer.commit()
        return {"status": status, "report_day": day_text, "batch_id": batch_id,
                "raw_row_count": raw_row_count, "report_ids": report_ids,
                "report_types": report_types}

    def _record_failure(self, day: date, status: str, error: str) -> None:
        now = self.now_factory().astimezone(timezone.utc)
        next_retry = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        with self.store_registry.session(
            "operational", mode="rw", operation="wb_finance_daily_failure"
        ) as writer:
            writer.execute(
                """INSERT INTO wb_finance_daily_sync
                   (seller_id,report_day,week_start,week_end,status,attempt_count,
                    last_synced_at,next_retry_at,last_error)
                   VALUES(?,?,?,?,?,1,?,?,?)
                   ON CONFLICT(seller_id,report_day) DO UPDATE SET
                   status=excluded.status,attempt_count=attempt_count+1,
                   last_synced_at=excluded.last_synced_at,
                   next_retry_at=excluded.next_retry_at,last_error=excluded.last_error""",
                (self.seller_id, day.isoformat(), day.isoformat(), day.isoformat(),
                 status, now.isoformat().replace("+00:00", "Z"), next_retry, error[:240]),
            )
            writer.commit()

    def sync_day(self, day: date, client: WbFinanceApiClient) -> dict[str, Any]:
        self.ensure_schema()
        with self.store_registry.session(
            "operational", mode="ro", operation="wb_finance_daily_policy"
        ) as reader:
            config = reader.execute(
                "SELECT initial_day FROM wb_finance_daily_config WHERE seller_id=?",
                (self.seller_id,),
            ).fetchone()
        latest = self.now_factory().astimezone(MOSCOW).date() - timedelta(days=1)
        if config is None or not date.fromisoformat(config["initial_day"]) <= day <= latest:
            raise ValueError("daily Finance day is outside the closed-day policy")
        try:
            fetched = client.fetch_report(
                date_from=day.isoformat(), date_to=day.isoformat(), period="daily",
            )
            if fetched.terminal_status != 204 or not fetched.rows:
                self._record_failure(day, "waiting", "official daily report has no complete rows")
                return {"status": "waiting", "report_day": day.isoformat()}
            raw = self._store_complete_raw(day, fetched.rows, fetched.source_digest)
            projected = self.project_pointer(day, unchanged_fetch=not raw["changed"])
            return {**projected, "source_digest": fetched.source_digest}
        except FinanceApiError as exc:
            self._record_failure(day, "rate_limited" if exc.code == "rate_limited" else "error_loading", exc.code)
            return {"status": "rate_limited" if exc.code == "rate_limited" else "error_loading",
                    "report_day": day.isoformat(), "error": exc.code}
        except Exception as exc:
            self._record_failure(day, "error_loading", type(exc).__name__)
            raise

    def due_days(self, *, now: datetime | None = None,
                 max_days: int = 2) -> list[date]:
        if max_days < 1 or max_days > DAILY_INITIAL_DAYS:
            raise ValueError("max_days must be 1..14")
        now = now or self.now_factory()
        with self.store_registry.session(
            "operational", mode="ro", operation="wb_finance_daily_due"
        ) as reader:
            config = reader.execute(
                "SELECT initial_day FROM wb_finance_daily_config WHERE seller_id=?",
                (self.seller_id,),
            ).fetchone()
            if config is None:
                raise ValueError("daily Finance policy is not initialized")
            first = date.fromisoformat(str(config["initial_day"]))
            latest = now.astimezone(MOSCOW).date() - timedelta(days=1)
            dates = [first + timedelta(days=offset)
                     for offset in range(max(0, (latest - first).days + 1))]
            stored = {str(row["report_day"]): row for row in reader.execute(
                """SELECT report_day,status,next_retry_at FROM wb_finance_daily_sync
                   WHERE seller_id=? AND report_day>=?""",
                (self.seller_id, first.isoformat()),
            )}
        due = []
        for day in dates:
            row = stored.get(day.isoformat())
            if row and str(row["status"]) == "completed":
                continue
            if row and row["next_retry_at"]:
                try:
                    if datetime.fromisoformat(str(row["next_retry_at"]).replace("Z", "+00:00")) > now.astimezone(timezone.utc):
                        continue
                except ValueError:
                    pass
            due.append(day)
            if len(due) >= max_days:
                break
        return due

    def tick(self, client: WbFinanceApiClient, *, max_days: int = 2) -> dict[str, Any]:
        self.ensure_schema()
        # Reconcile a pointed raw batch after an interrupted previous projection.
        with closing(self._connect_daily_read()) as reader:
            pointers = reader.execute(
                f"""SELECT p.report_day FROM {self._raw_pointer_table(reader)} AS p
                    LEFT JOIN wb_finance_daily_sync AS s
                      ON s.seller_id=p.seller_id AND s.report_day=p.report_day
                   WHERE p.seller_id=? AND (s.batch_id IS NULL OR s.batch_id!=p.batch_id)
                   ORDER BY p.report_day LIMIT ?""",
                (self.seller_id, max_days),
            ).fetchall()
        recovered = [self.project_pointer(date.fromisoformat(row["report_day"])) for row in pointers]
        # A formula/source dependency change can stale the entire displayed
        # window. Restore it from completed raw before contacting WB, so one
        # failed external fetch cannot leave older days hidden. max_days bounds
        # WB requests; the local display repair is independently bounded to 14.
        recovered.extend(self._repair_visible_stale())
        try:
            results = [self.sync_day(day, client) for day in self.due_days(max_days=max_days)]
        except Exception:
            # Preserve the fetch error if cleanup itself fails. Previously
            # committed projections from complete raw remain available.
            try:
                recovered.extend(self._repair_visible_stale())
            except Exception:
                pass
            raise
        # A newly acquired pointer can redistribute capped expenses into
        # older days. Repair that same bounded display after acquisition.
        recovered.extend(self._repair_visible_stale())
        failed = any(item.get("status") in {
            "projection_error", "source_advanced", "no_raw_pointer"
        } for item in recovered) or any(item.get("status") in {
            "error_loading", "rate_limited"
        } for item in results)
        return {"status": "completed_with_errors" if failed else "ok",
                "recovered": recovered, "days": results}

    def _repair_visible_stale(self) -> list[dict[str, Any]]:
        stale_days = [date.fromisoformat(item["day"])
                      for item in self.build_daily_payload()["days"]
                      if item["status"] == "stale_projection"]
        if not stale_days:
            return []
        prepared = []
        errors = []
        with closing(self._connect_daily_read()) as reader:
            reader.execute("BEGIN")
            snapshot = CanonicalChannelCostSnapshot.from_connection(reader)
            for day in stale_days:
                try:
                    prepared.append(self._prepare_projection(
                        reader, day, cost_snapshot=snapshot))
                except (ValueError, sqlite3.Error) as exc:
                    errors.append({"status": "projection_error",
                                   "report_day": day.isoformat(),
                                   "error": type(exc).__name__})
            reader.rollback()
        return [self._commit_projection(item) for item in prepared] + errors

    def repair_visible_projections(self) -> dict[str, Any]:
        """Admitted source-free visible14 repair, followed by truthful readback."""
        repaired = self._repair_visible_stale()
        payload = self.build_daily_payload()
        bad = {'stale_projection', 'source_advanced', 'projection_error', 'no_raw_pointer'}
        failed = any(item.get('status') in bad for item in repaired + payload['days'])
        with closing(self._connect_daily_read()) as conn:
            first = closed_daily_dates(self.now_factory())[0].isoformat()
            unresolved = conn.execute(f'''SELECT count(*) FROM {self._raw_pointer_table(conn)} p
                LEFT JOIN wb_finance_daily_sync s ON s.seller_id=p.seller_id AND s.report_day=p.report_day
                WHERE p.seller_id=? AND p.report_day>=? AND
                (s.batch_id IS NULL OR s.batch_id<>p.batch_id OR s.content_hash<>p.content_hash)''',
                (self.seller_id, first)).fetchone()[0]
        failed = failed or bool(unresolved)
        return {'status': 'failed' if failed else 'ok', 'repaired': repaired,
                'days': payload['days'], 'generated_at': payload['generated_at']}

    def build_daily_payload(self) -> dict[str, Any]:
        """Light read-only payload; never fetch or recalculate on HTTP GET."""

        with closing(self._connect_daily_read()) as reader:
            reader.execute("BEGIN")
            pointer_table = self._raw_pointer_table(reader)
            first_displayed = closed_daily_dates(self.now_factory())[0].isoformat()
            rows = reader.execute(
                f"""SELECT s.*,a.metrics_json,a.coverage_json,a.unknown_reasons_json,
                           a.report_ids_json,a.report_types_json,a.formula_version,
                           a.classifier_version,a.allocation_dependency_hash,
                           a.cost_state_hash,a.cost_source_hash,
                           a.batch_id AS aggregate_batch,
                           a.content_hash AS aggregate_hash,
                           a.raw_row_count AS aggregate_count,
                           p.batch_id AS pointer_batch,p.content_hash AS pointer_hash,
                           p.row_count AS pointer_count
                      FROM wb_finance_daily_sync AS s
                      LEFT JOIN wb_finance_daily_aggregates AS a
                        ON a.seller_id=s.seller_id AND a.report_day=s.report_day
                      LEFT JOIN {pointer_table} AS p
                        ON p.seller_id=s.seller_id AND p.report_day=s.report_day
                     WHERE s.seller_id=? AND s.report_day>=? ORDER BY s.report_day""",
                (self.seller_id, first_displayed),
            ).fetchall()
            dependency_hash = self._allocation_dependency_hash(reader, self.now_factory().date())
            snapshot = CanonicalChannelCostSnapshot.from_connection(reader) if rows else None
            cost_source_hashes = {
                str(row["report_day"]): self._cost_source_hash(
                    reader, date.fromisoformat(str(row["report_day"])), snapshot
                ) for row in rows if row["pointer_batch"] is not None
            }
            reader.rollback()
        days = []
        for row in rows:
            admitted = (
                row["pointer_batch"] is not None
                and row["pointer_batch"] == row["batch_id"]
                and row["pointer_hash"] == row["content_hash"]
                and row["pointer_count"] == row["raw_row_count"]
                and row["aggregate_batch"] == row["pointer_batch"]
                and row["aggregate_hash"] == row["pointer_hash"]
                and row["aggregate_count"] == row["pointer_count"]
                and row["formula_version"] == DAILY_FORMULA_VERSION
                and row["classifier_version"] == CLASSIFIER_VERSION
                and row["allocation_dependency_hash"] == dependency_hash
                and row["cost_state_hash"]
                and row["cost_source_hash"] == cost_source_hashes.get(str(row["report_day"]))
                and row["metrics_json"] is not None
            )
            days.append({
                "day": row["report_day"],
                "status": (row["status"] if admitted or row["pointer_batch"] is None
                           else "stale_projection"),
                "metrics": json.loads(row["metrics_json"]) if admitted else {},
                "cost_coverage": json.loads(row["coverage_json"]) if admitted else {},
                "unknown_reasons": json.loads(row["unknown_reasons_json"]) if admitted else [],
                "report_ids": json.loads(row["report_ids_json"]) if admitted else [],
                "report_types": json.loads(row["report_types_json"]) if admitted else [],
                "raw_row_count": row["raw_row_count"] if admitted else 0,
                "report_count": len(json.loads(row["report_ids_json"])) if admitted else 0,
                "last_synced_at": row["last_synced_at"], "last_error": row["last_error"],
            })
        return {"status": "ok", "contract_version": DAILY_CONTRACT_VERSION,
                "days": days, "day_count": len(days),
                "generated_at": self.now_factory().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}


def daily_block_from_env(runtime_dir: Path) -> WbFinanceDailyBlock:
    import os
    return WbFinanceDailyBlock(runtime_dir,
        seller_id=os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_ID") or "canonical")
