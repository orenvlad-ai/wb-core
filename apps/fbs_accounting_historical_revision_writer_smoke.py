#!/usr/bin/env python3
"""LOCAL temporary native-source integration and staging crash/CAS fixtures.

No runner, external service or production path is used. All data are TEMP.
The existing native fixture posts through _build_posting_plan/_apply_plan. Its
direct posting requests are then given the exact posted workflow header written
by FfPoolDocuments' outer posting transaction (without running that service).
WB/retained rollover inputs remain explicitly synthetic/unpublished, as in A2.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.fbs_accounting_historical_revision_smoke import native_writer_fixture, END
from apps import fbs_accounting_backdated_documents_smoke as backdated_fixture
from packages.application import fbs_accounting_historical_revision as planner
from packages.application import fbs_accounting_historical_revision_writer as writer
from packages.application import fbs_accounting_runtime as accounting
from packages.application import ready_publication as ready
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.ff_pool_documents import DOCUMENTS_TABLE, REQUESTS_TABLE, _build_posting_plan, _apply_plan
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.wb_fbs_warehouse_registry import REGISTRY_RUNS_TABLE, STOCK_RUNS_TABLE, STOCK_ROWS_TABLE

NOW = datetime.fromisoformat(END + "T14:00:00+00:00")


def rehash(value):
    value["manifest_digest"] = fingerprint({key: item for key, item in value.items() if key != "manifest_digest"})


class HistoricalRevisionWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="historical-staging-local-")
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.db = self.runtime / "registry_upload_runtime.sqlite3"
        self.book, self.capture, self.receipt, _ = native_writer_fixture(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            # Actual native workflow publication fields, not constructed money.
            for request in conn.execute(f"SELECT * FROM {REQUESTS_TABLE}").fetchall():
                raw = conn.execute(f"SELECT posted_manifest_json,posted_at FROM {DOCUMENTS_TABLE} WHERE request_id=? AND document_id=root_document_id",
                                   (request["request_id"],)).fetchone()
                posted = json.loads(raw[0])
                original = {key: value for key, value in posted.items() if key not in {"document_id", "document_role", "lines"}}
                original["document_kind"] = request["document_kind"]
                conn.execute(f"UPDATE {REQUESTS_TABLE} SET state='posted',posted_document_id=?,posted_manifest_sha256=?,posted_at=? WHERE request_id=?",
                             (posted["primary_document_id"], fingerprint(original), raw[1], request["request_id"]))
        with closing(sqlite3.connect(self.db)) as conn:
            writer.ensure_staging_schema(conn)
        self.before_version = accounting._save_book(self.runtime, self.book, expected=None, operation_id="fixture-initial-book")
        self.plan = planner.build_historical_revision_plan(self.book, self.capture, receipt_document_ids=[self.receipt])
        self.assertEqual(self.plan["status"], "ready", self.plan.get("blocker"))
        self.manifest = self.prepare()

    def prepare(self, **kwargs):
        return writer.prepare_historical_revision_stage(self.plan, runtime_dir=self.runtime, owner_generation="owner-generation-1",
            operation_id="operator-receipt-1", attempt_id="1", now=kwargs.pop("now", NOW), **kwargs)

    def stage(self, manifest=None, **kwargs):
        with warehouse_functional_job_lock(self.runtime):
            return writer.stage_historical_revision(manifest or self.manifest, runtime_dir=self.runtime, now=NOW, **kwargs)

    def read(self):
        return writer.read_historical_revision_stage(runtime_dir=self.runtime, operation_id="operator-receipt-1", attempt_id="1")

    def sql(self, sql, args=()):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            return conn.execute(sql, args).fetchall()

    def book_rows(self):
        with closing(sqlite3.connect(accounting.path(self.runtime))) as conn:
            return tuple(conn.execute("SELECT version,operation_id,payload,previous_version FROM accounting_revisions ORDER BY version")), tuple(conn.execute("SELECT digest,payload FROM accounting_blobs ORDER BY digest"))

    def assert_current_preserved(self):
        self.assertEqual(accounting.load(self.runtime), (self.book, self.before_version))
        self.assertEqual(accounting.load(self.runtime, version=self.before_version), (self.book, self.before_version))

    def test_actual_native_sources_staged_numeric_candidate_old_current_and_blobs_immutable(self):
        old_rows, old_blobs = self.book_rows()
        result = self.stage()
        self.assertEqual(result["state"], "staged")
        self.assertTrue(result["revision_present"])
        self.assertFalse(result["activation_allowed"])
        self.assertFalse(result["operator_completion"])
        candidate, version = accounting.load(self.runtime, version=result["staged_book_version"])
        self.assertEqual(candidate, self.plan["candidate_book"])
        self.assertEqual(candidate["state"]["periods"]["2026-09-08"]["rows"]["A:1"]["wac_rub"], "150.00")
        self.assertEqual(version, self.plan["after_book_digest"])
        new_rows, new_blobs = self.book_rows()
        self.assertTrue(set(old_rows).issubset(new_rows))
        self.assertTrue(set(old_blobs).issubset(new_blobs))
        self.assertEqual(set(r[0] for r in self.sql("SELECT name FROM sqlite_master WHERE type='table'") if r[0].endswith("ready_publications")), set())
        self.assert_current_preserved()

    def test_exact_confirmation_actor_request_manifest(self):
        source = self.manifest["source_confirmation"][self.receipt]
        self.assertEqual(source["document"]["actor"], "fixture-operator")
        self.assertEqual(source["request"]["actor"], source["document"]["actor"])
        self.assertEqual(source["request"]["posted_document_id"], self.receipt)
        self.assertTrue(source["request"]["request_payload_json"])

    def test_identical_retry_does_not_duplicate_revision_or_intent(self):
        first = self.stage()
        rows = self.book_rows()
        self.assertEqual(self.stage(), first)
        self.assertEqual(self.book_rows(), rows)
        self.assertEqual(self.sql(f"SELECT count(*) FROM {writer.TABLE}"), [(1,)])

    def test_observation_and_request_progress_metadata_do_not_false_stale(self):
        self.sql(f"UPDATE {REQUESTS_TABLE} SET state='replay',updated_at='2026-09-10T14:01:00Z' WHERE posted_document_id=?", (self.receipt,))
        repeated = self.prepare(now=NOW + timedelta(minutes=1))
        self.assertEqual(repeated, self.manifest)
        self.assertEqual(self.stage()["state"], "staged")

    def test_missing_live_cycle_owner_rejects(self):
        with self.assertRaisesRegex(RuntimeError, "live admission"):
            writer.stage_historical_revision(self.manifest, runtime_dir=self.runtime, now=NOW)
        self.assertIsNone(self.read())

    def test_crash_matrix_same_exact_identity_resume(self):
        for boundary in ("before_intent", "after_intent", "after_revision", "after_stage"):
            with self.subTest(boundary=boundary):
                # Separate local fixture per crash boundary.
                other = HistoricalRevisionWriterTests()
                other.setUp()
                try:
                    def fail(at):
                        if at == boundary:
                            raise RuntimeError("fixture interruption")
                    with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
                        other.stage(fault_injector=fail)
                    interrupted = other.read()
                    if boundary == "before_intent":
                        self.assertIsNone(interrupted)
                    else:
                        self.assertEqual(interrupted["state"], "staged" if boundary == "after_stage" else "prepared")
                        self.assertEqual(interrupted["revision_present"], boundary in {"after_revision", "after_stage"})
                    result = other.stage()
                    self.assertEqual(result["state"], "staged")
                    other.assert_current_preserved()
                    self.assertEqual(len(other.book_rows()[0]), 2)
                finally:
                    other.doCleanups()

    def test_source_change_and_aba_block_even_when_document_restored(self):
        self.sql(f"UPDATE {REQUESTS_TABLE} SET actor='foreign' WHERE posted_document_id=?", (self.receipt,))
        self.sql(f"UPDATE {REQUESTS_TABLE} SET actor='fixture-operator' WHERE posted_document_id=?", (self.receipt,))
        with self.assertRaisesRegex(ValueError, "material_input_changed"):
            self.stage()
        self.assertIsNone(self.read())
        self.assert_current_preserved()

    def test_native_actor_mismatch_and_unposted_confirmation_block_prepare(self):
        self.sql(f"UPDATE {REQUESTS_TABLE} SET actor='foreign' WHERE posted_document_id=?", (self.receipt,))
        with self.assertRaisesRegex(ValueError, "actor_authority_mismatch"):
            self.prepare()
        self.sql(f"UPDATE {REQUESTS_TABLE} SET actor='fixture-operator' WHERE posted_document_id=?", (self.receipt,))
        self.sql(f"UPDATE {REQUESTS_TABLE} SET state='ready' WHERE posted_document_id=?", (self.receipt,))
        with self.assertRaisesRegex(ValueError, "not_posted"):
            self.prepare()

    def test_native_request_manifest_or_source_identity_mismatch_block_prepare(self):
        self.sql(f"UPDATE {REQUESTS_TABLE} SET posted_manifest_sha256='sha256:wrong' WHERE posted_document_id=?", (self.receipt,))
        with self.assertRaisesRegex(ValueError, "request_manifest_mismatch"):
            self.prepare()

    def test_changed_owner_or_plan_never_overwrites_existing_intent(self):
        self.stage()
        for key, value in (("owner_generation", "foreign-owner"), ("unresolved_obligations", ["forged"])):
            with self.subTest(key=key):
                forged = deepcopy(self.manifest)
                forged[key] = value
                rehash(forged)
                with self.assertRaisesRegex(ValueError, "intent_identity_conflict|manifest_contract_mismatch"):
                    self.stage(forged)
        self.assertEqual(self.read()["owner_generation"], "owner-generation-1")

    def test_forged_numeric_candidate_rehashed_manifest_rejected_independently(self):
        forged = deepcopy(self.manifest)
        forged["plan"]["candidate_book"]["state"]["periods"]["2026-09-08"]["rows"]["A:1"]["wac_rub"] = "999"
        rehash(forged)
        with self.assertRaises(ValueError):
            self.stage(forged)
        self.assertIsNone(self.read())
        self.assert_current_preserved()

    def test_book_current_cas_and_append_lineage_aba(self):
        other = deepcopy(self.book)
        other["fixture_only"] = "advanced"
        advanced = accounting._save_book(self.runtime, other, expected=self.before_version, operation_id="fixture-advance")
        with self.assertRaisesRegex(ValueError, "compare_and_swap"):
            self.stage()
        # Local adversarial pointer restoration is not an ordinary writer action.
        with closing(sqlite3.connect(accounting.path(self.runtime))) as conn, conn:
            conn.execute("UPDATE accounting_current SET version=? WHERE version=?", (self.before_version, advanced))
        with self.assertRaisesRegex(ValueError, "compare_and_swap"):
            self.stage()
        self.assertIsNone(self.read())

    def test_storage_authority_generation_drift_rejects(self):
        bad = deepcopy(self.manifest)
        bad["authority"]["manifest"] = "sha256:foreign"
        rehash(bad)
        with self.assertRaisesRegex(ValueError, "storage_authority_changed"):
            self.stage(bad)
        self.assertIsNone(self.read())

    def test_code_authority_drift_rejects_before_intent(self):
        bad = deepcopy(self.manifest)
        name = next(iter(bad["code_authority"]))
        bad["code_authority"][name] = "sha256:foreign"
        rehash(bad)
        with self.assertRaisesRegex(ValueError, "code_authority_changed"):
            self.stage(bad)
        self.assertIsNone(self.read())

    def test_current_stock_aba_and_unsupported_trigger_reject(self):
        self.sql("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_rows SET amount=amount+1 WHERE nm_id=1")
        self.sql("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_rows SET amount=amount-1 WHERE nm_id=1")
        with self.assertRaisesRegex(ValueError, "material_input_changed"):
            self.stage()
        self.sql("DROP TRIGGER historical_stage_request_update")
        self.sql(f"CREATE TRIGGER historical_stage_request_update AFTER UPDATE ON {REQUESTS_TABLE} BEGIN SELECT 1; END")
        with self.assertRaisesRegex(ValueError, "trigger_authority_mismatch"):
            self.prepare()

    def test_same_path_identical_book_replacement_is_not_storage_authority(self):
        book = accounting.path(self.runtime)
        replacement = book.with_suffix(".fixture-copy")
        shutil.copyfile(book, replacement)
        replacement.replace(book)
        with self.assertRaisesRegex(ValueError, "source_file_replaced"):
            self.stage()
        self.assertIsNone(self.read())
        self.assert_current_preserved()

    def test_intent_database_immutable_identity_and_delete_protected(self):
        self.stage()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.sql(f"UPDATE {writer.TABLE} SET owner_generation='foreign'")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.sql(f"DELETE FROM {writer.TABLE}")
        self.assertEqual(self.read()["state"], "staged")

    def test_recovery_source_drift_does_not_append_again_or_clear_prepared(self):
        def fail(at):
            if at == "after_revision":
                raise RuntimeError("fixture interruption")
        with self.assertRaises(RuntimeError):
            self.stage(fault_injector=fail)
        rows = self.book_rows()
        self.sql(f"UPDATE {REQUESTS_TABLE} SET actor='foreign' WHERE posted_document_id=?", (self.receipt,))
        with self.assertRaises(ValueError):
            self.stage()
        self.assertEqual(self.book_rows(), rows)
        self.assertEqual(self.read()["state"], "prepared")
        self.assert_current_preserved()

    def test_readback_never_claims_active_after_unrelated_later_book_advance(self):
        self.stage()
        other = deepcopy(self.book)
        other["fixture_only"] = "ordinary-next"
        accounting._save_book(self.runtime, other, expected=self.before_version, operation_id="fixture-later-current")
        result = self.read()
        self.assertEqual(result["state"], "staged")
        self.assertFalse(result["current_matches_expected"])
        self.assertFalse(result["operator_completion"])

    def test_existing_closed_day_guard_still_rejects_candidate_activation(self):
        self.stage()
        with self.assertRaisesRegex(ValueError, "closed_.*_day_is_immutable"):
            accounting._save_book(self.runtime, self.plan["candidate_book"], expected=self.before_version, operation_id="ordinary-must-reject")
        self.assert_current_preserved()

    def test_real_new_current_overhead_is_explicitly_blocked_until_cohort_adapter(self):
        source = dict(facility_id="A", scope="FBS", amount_rub="100", category="other",
                      comment="Local synthetic current cohort", source_mode="manual")
        at = END + "T14:01:00Z"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            old = dict(conn.execute(f"SELECT * FROM {REQUESTS_TABLE} WHERE posted_document_id=?", (self.receipt,)).fetchone())
            fields = ("request_id", "request_identity", "client_request_id", "document_kind", "state", "source_system",
                      "source_type", "source_id", "source_revision", "idempotency_epoch", "actor", "business_date",
                      "source_filename", "source_content_type", "source_sha256", "template_fingerprint", "request_payload_json",
                      "accepted_at", "updated_at")
            request = {key: old[key] for key in fields}
            request.update(request_id="native:current-overhead", request_identity=fingerprint("current-overhead"),
                client_request_id="current-overhead", document_kind="pool_overhead", state="accepted",
                source_type="fixture:pool_overhead", source_id="current-overhead", source_revision="revision:current-overhead",
                business_date=END, request_payload_json=canonical(source), accepted_at=at, updated_at=at)
            conn.execute(f"INSERT INTO {REQUESTS_TABLE}({','.join(request)}) VALUES({','.join('?' for _ in request)})", tuple(request.values()))
            native = _build_posting_plan(conn, request=request, manifest=source, epoch=1)
            _apply_plan(conn, request=request, plan=native, epoch=1, posted_at=at)
            conn.execute(f"UPDATE {REQUESTS_TABLE} SET state='posted',posted_document_id=?,posted_manifest_sha256=?,posted_at=? WHERE request_id=?",
                         (native["primary_document_id"], fingerprint(native["posted_manifest"]), at, request["request_id"]))
        with self.assertRaisesRegex(ValueError, "revision_material_changed"):
            self.prepare(now=NOW + timedelta(minutes=2))
        with ready.readonly(self.db) as conn:
            fresh = writer._capture(conn, self.db, self.plan, NOW + timedelta(minutes=2), self.book)
        blocked = planner.build_historical_revision_plan(self.book, fresh, receipt_document_ids=[self.receipt])
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["blocker"]["code"], "unapproved_new_document_scope")
        self.assertIsNone(self.read())
        self.assert_current_preserved()


class SelectedGenerationBudget(backdated_fixture.BackdatedDocuments):
    """Real native backdated publication and selected-generation bounds."""

    def selected_stock_run(self, conn):
        return dict(conn.execute(f'SELECT * FROM {STOCK_RUNS_TABLE} WHERE registry_run_id=? ORDER BY seller_warehouse_id LIMIT 1',
                                 (self.fixture.cap['quantity_snapshot']['id'],)).fetchone())

    def corrupt_stock(self, conn, trigger, sql, args):
        # Adversarial LOCAL corruption; restore the exact immutable trigger so
        # subsequent refusal proves row authority, not a missing schema fence.
        original = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)).fetchone()[0]
        conn.execute('DROP TRIGGER '+trigger)
        conn.execute(sql, args)
        conn.execute(original)

    def add_large_stock_archive(self):
        # A complete older, wider catalog generation. Its rows are unrelated
        # to the selected current generation and to the saved revised dates.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            generation = dict(conn.execute(f'SELECT * FROM {REGISTRY_RUNS_TABLE} WHERE run_id=?',
                                           (self.fixture.cap['quantity_snapshot']['id'],)).fetchone())
            stock = self.selected_stock_run(conn)
            catalog = json.loads(generation['catalog_scope_json'])
            catalog.update(requested_chrt_count=100001, active_nm_id_count=100001)
            warehouses = json.loads(generation['warehouse_scope_json'])
            warehouses.update(warehouse_count=1, warehouses=warehouses['warehouses'][:1])
            generation.update(run_id='LOCAL wide archived generation', run_sequence=0,
                catalog_scope_json=canonical(catalog), warehouse_scope_json=canonical(warehouses),
                generation_digest=fingerprint('LOCAL wide archived generation'),
                started_at='2026-09-01T13:55:00Z', completed_at='2026-09-01T13:56:00Z')
            stock.update(run_id='LOCAL wide archived stock', registry_run_id=generation['run_id'],
                requested_chrt_count=100001, dense_row_count=100001, explicit_chrt_count=100001,
                omitted_zero_count=0, source_digest=fingerprint('LOCAL wide archived stock'),
                snapshot_at='2026-09-01T13:55:00Z')
            for table, row in ((REGISTRY_RUNS_TABLE, generation), (STOCK_RUNS_TABLE, stock)):
                conn.execute(f'INSERT INTO {table}('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')', tuple(row.values()))
            conn.executemany(f'INSERT INTO {STOCK_ROWS_TABLE}(run_id,chrt_id,nm_id,amount,provenance) VALUES(?,?,?,0,?)',
                ((stock['run_id'], i+1000, i+1, 'explicit_wb_row') for i in range(100001)))

    def test_large_unselected_archive_real_prepare_publication_preserves_full_catalog_and_native_once(self):
        self.add_large_stock_archive()
        before_docs = self.document_image()
        with ready.readonly(self.db) as conn:
            before_counts = {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                             for table in (STOCK_ROWS_TABLE, 'sheet_vitrina_v1_ff_pool_movement_lines')}
            self.assertGreater(before_counts[STOCK_ROWS_TABLE], 100000)
            selected_count = conn.execute(f'SELECT count(*) FROM {STOCK_ROWS_TABLE} r JOIN {STOCK_RUNS_TABLE} s ON s.run_id=r.run_id WHERE s.registry_run_id=?',
                                         (self.fixture.cap['quantity_snapshot']['id'],)).fetchone()[0]
            self.assertEqual(selected_count, 4)
        self.fixture.manifest = self.fixture.prepare()
        captured = self.fixture.manifest['original_capture']['quantity_snapshot']
        self.assertEqual(captured, self.fixture.cap['quantity_snapshot'])
        self.assertEqual({(row['facility_id'], row['nm_id'], row['quantity']) for row in captured['rows']},
                         {('A', 1, 1000), ('B', 1, 1000), ('A', 2, 0), ('B', 2, 0)})
        result = self.fixture.publish()
        self.assertEqual(result['status'], 'published')
        self.assertTrue(result['ready'])
        self.assertFalse(result['operator_complete'])
        self.assertEqual(self.fixture.publish()['manifest_digest'], result['manifest_digest'])
        self.assertEqual(self.document_image(), before_docs)
        self.assertEqual(accounting.load(self.fixture.runtime, version=self.fixture.before), (self.fixture.book, self.fixture.before))
        with ready.readonly(self.db) as conn:
            self.assertEqual({table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in before_counts}, before_counts)

    def assert_stock_preflight_refuses_without_capture_or_publication(self):
        before_docs = self.document_image()
        with patch.object(writer, 'capture_current', side_effect=AssertionError('unbounded capture must not start')) as capture:
            with self.assertRaisesRegex(writer.HistoricalRevisionStageError, 'stage_native_source_size_limit'):
                self.fixture.prepare()
        capture.assert_not_called()
        self.assertIsNone(self.status())
        self.assertEqual(accounting.load(self.fixture.runtime)[1], self.fixture.before)
        self.assertEqual(self.document_image(), before_docs)
        with ready.readonly(self.db) as conn:
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {writer.TABLE}').fetchone()[0], 0)

    def test_oversized_selected_generation_rows_refuse_before_capture(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            runs = [row['run_id'] for row in conn.execute(f'SELECT run_id FROM {STOCK_RUNS_TABLE} WHERE registry_run_id=?',
                                                        (self.fixture.cap['quantity_snapshot']['id'],))]
            self.assertEqual(len(runs), 2)
            conn.executemany(f'INSERT INTO {STOCK_ROWS_TABLE}(run_id,chrt_id,nm_id,amount,provenance) VALUES(?,?,1,0,?)',
                ((run, i+1000, 'explicit_wb_row') for run in runs for i in range(50000)))
            # Each warehouse fits; their selected full generation does not.
            self.assertEqual([conn.execute(f'SELECT count(*) FROM {STOCK_ROWS_TABLE} WHERE run_id=?', (run,)).fetchone()[0]
                              for run in runs], [50002, 50002])
        self.assert_stock_preflight_refuses_without_capture_or_publication()

    def test_oversized_selected_provenance_bytes_refuse_before_capture(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            stock = self.selected_stock_run(conn)
            # TEXT characters fit the limit; actual UTF-8 bytes exceed it.
            self.corrupt_stock(conn, 'wb_fbs_stock_rows_no_update',
                f'UPDATE {STOCK_ROWS_TABLE} SET provenance=? WHERE run_id=? AND chrt_id=101',
                ('я'*(writer.MAX_NATIVE_BYTES//2+1), stock['run_id']))
            chars, size = conn.execute(f'SELECT length(provenance),length(CAST(provenance AS BLOB)) FROM {STOCK_ROWS_TABLE} WHERE run_id=? AND chrt_id=101',
                                       (stock['run_id'],)).fetchone()
            self.assertLess(chars, writer.MAX_NATIVE_BYTES)
            self.assertGreater(size, writer.MAX_NATIVE_BYTES)
        self.assert_stock_preflight_refuses_without_capture_or_publication()

    def test_complete_capture_must_match_preflight_generation_identity_and_digest(self):
        actual_capture = writer.capture_current
        for field in ('id', 'digest'):
            with self.subTest(field=field):
                def foreign(*args, **kwargs):
                    result = actual_capture(*args, **kwargs)
                    self.assertTrue(result['quantity_snapshot']['complete'])
                    result['quantity_snapshot'][field] = 'LOCAL foreign authority'
                    return result
                with patch.object(writer, 'capture_current', side_effect=foreign):
                    with self.assertRaisesRegex(writer.HistoricalRevisionStageError, 'stage_native_source_generation_changed'):
                        self.fixture.prepare()
                self.assertIsNone(self.status())
                self.assertEqual(accounting.load(self.fixture.runtime)[1], self.fixture.before)

    def test_missing_catalog_only_dense_zero_still_refuses(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            stock = self.selected_stock_run(conn)
            self.corrupt_stock(conn, 'wb_fbs_stock_rows_no_delete',
                f'DELETE FROM {STOCK_ROWS_TABLE} WHERE run_id=? AND nm_id=2', (stock['run_id'],))
        actual_capture = writer.capture_current
        with patch.object(writer, 'capture_current', wraps=actual_capture) as capture:
            with self.assertRaises(ValueError):
                self.fixture.prepare()
        capture.assert_called_once()
        self.assertIsNone(self.status())
        self.assertEqual(accounting.load(self.fixture.runtime)[1], self.fixture.before)


if __name__ == "__main__":
    unittest.main()
