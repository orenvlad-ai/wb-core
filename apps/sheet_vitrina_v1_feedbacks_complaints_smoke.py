"""Smoke checks for feedback complaint runtime journal and status contract."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
import time
from urllib import error as urllib_error, request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH,
    DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_JOB_PATH,
    DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.sheet_vitrina_v1_feedbacks_complaints import (  # noqa: E402
    COMPLAINT_STATUS_LABELS,
    JsonFileFeedbacksComplaintJournal,
    SheetVitrinaV1FeedbacksComplaintsError,
    SheetVitrinaV1FeedbacksComplaintsBlock,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    _assert_journal_create_dedupe_status_update()
    _assert_error_retry()
    _assert_table_contract_and_fake_async_sync()
    _assert_duplicate_running_job_guard()
    _assert_error_job_and_missing_run_id()
    _assert_http_sync_job_routes()
    print("sheet_vitrina_v1_feedbacks_complaints_smoke: OK")


def _assert_journal_create_dedupe_status_update() -> None:
    with TemporaryDirectory(prefix="feedbacks-complaints-journal-") as tmp:
        journal = JsonFileFeedbacksComplaintJournal(Path(tmp))
        created = journal.create_or_update(_record("feedback-1"))
        if not created.created or created.duplicate:
            raise AssertionError(f"first insert must create record: {created}")
        duplicate = journal.create_or_update(_record("feedback-1"))
        if not duplicate.duplicate or duplicate.created:
            raise AssertionError(f"second insert must be deduped by feedback_id: {duplicate}")
        updated = journal.update_status("feedback-1", status="satisfied", raw_status_text="Одобрена", wb_decision_text="Принята")
        if not updated or updated["complaint_status_label"] != COMPLAINT_STATUS_LABELS["satisfied"]:
            raise AssertionError(f"status update must set satisfied label: {updated}")
        metadata = journal.update_metadata(
            "feedback-1",
            {
                "status_sync_run_id": "sync-1",
                "status_sync_report_path": "/tmp/status-sync.json",
                "confirmation_probe_path": "/tmp/confirmation.json",
                "submit_network_evidence_summary": {"methods": ["POST"], "statuses": [200]},
            },
        )
        if not metadata or metadata["status_sync_report_path"] != "/tmp/status-sync.json":
            raise AssertionError(f"metadata update must preserve post-submit evidence paths: {metadata}")
        payload = SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=Path(tmp), journal=journal).build_table()
        if payload["contract_name"] != "sheet_vitrina_v1_feedbacks_complaints" or payload["summary"]["satisfied"] != 1:
            raise AssertionError(f"table contract mismatch: {payload}")
        if payload["meta"]["auto_sync_on_page_load"] is not False:
            raise AssertionError("complaints table must not auto-sync statuses on page load")


def _assert_error_retry() -> None:
    with TemporaryDirectory(prefix="feedbacks-complaints-retry-") as tmp:
        journal = JsonFileFeedbacksComplaintJournal(Path(tmp))
        first = journal.create_or_update({**_record("feedback-err"), "complaint_status": "error", "last_error": "timeout"})
        if not first.created:
            raise AssertionError("error record must be created")
        blocked = journal.create_or_update({**_record("feedback-err"), "complaint_status": "waiting_response"})
        if not blocked.duplicate:
            raise AssertionError("error record retry must require retry_errors flag")
        retried = journal.create_or_update(
            {**_record("feedback-err"), "complaint_status": "waiting_response"},
            retry_errors=True,
        )
        if retried.duplicate or retried.record["complaint_status"] != "waiting_response":
            raise AssertionError(f"retry_errors must update error records: {retried}")


def _assert_table_contract_and_fake_async_sync() -> None:
    with TemporaryDirectory(prefix="feedbacks-complaints-block-") as tmp:
        journal = JsonFileFeedbacksComplaintJournal(Path(tmp))
        journal.create_or_update(_record("feedback-2"))
        sync_called: list[dict[str, object]] = []

        def fake_sync(payload: object) -> dict[str, object]:
            sync_called.append(dict(payload or {}))
            journal.update_status("feedback-2", status="rejected", raw_status_text="Отклонена")
            return {
                "contract_name": "seller_portal_feedbacks_complaints_status_sync",
                "finished_at": "2026-05-02T00:00:00Z",
                "aggregate": {
                    "local_records_before": 1,
                    "statuses_updated": 1,
                    "pending_rows_read": 1,
                    "answered_rows_read": 0,
                    "matched_local_complaints": 1,
                    "weak_matches_rejected": 0,
                    "direct_matches": 1,
                    "strong_composite_matches": 0,
                },
            }

        block = SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=Path(tmp), journal=journal, status_sync_runner=fake_sync)
        started = block.sync_status({"max_complaint_rows": 3})
        if started["contract_name"] != "sheet_vitrina_v1_feedbacks_complaints_status_sync_job" or not started["run_id"]:
            raise AssertionError(f"sync route must return job contract with run_id: {started}")
        result = _wait_job(block, str(started["run_id"]), {"success"})
        if not sync_called or result["summary"]["statuses_updated"] != 1 or result["direct_matches"] != 1:
            raise AssertionError(f"fake async sync route did not complete: {result}")
        table = block.build_table()
        if table["summary"]["rejected"] != 1:
            raise AssertionError(f"table must reflect status sync update: {table}")
        labels = {column["label"] for column in table["schema"]["columns"]}
        for required in ("Статус жалобы", "Категория WB", "Текст жалобы", "Match status"):
            if required not in labels:
                raise AssertionError(f"complaint table schema missing {required!r}: {labels}")


def _assert_duplicate_running_job_guard() -> None:
    with TemporaryDirectory(prefix="feedbacks-complaints-running-") as tmp:
        journal = JsonFileFeedbacksComplaintJournal(Path(tmp))
        journal.create_or_update(_record("feedback-running"))
        runner_started = threading.Event()
        release_runner = threading.Event()

        def slow_sync(payload: object) -> dict[str, object]:
            runner_started.set()
            if not release_runner.wait(timeout=5):
                raise RuntimeError("slow sync was not released")
            journal.update_status("feedback-running", status="satisfied", raw_status_text="Одобрена")
            return {
                "contract_name": "seller_portal_feedbacks_complaints_status_sync",
                "finished_at": "2026-05-02T00:00:10Z",
                "aggregate": {
                    "local_records_before": 1,
                    "pending_rows_read": 1,
                    "answered_rows_read": 1,
                    "matched_local_complaints": 1,
                    "statuses_updated": 1,
                    "weak_matches_rejected": 1,
                    "direct_matches": 0,
                    "strong_composite_matches": 1,
                },
            }

        block = SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=Path(tmp), journal=journal, status_sync_runner=slow_sync)
        first = block.sync_status({"max_complaint_rows": 3})
        if first["status"] != "queued":
            raise AssertionError(f"first job must start as queued: {first}")
        if not runner_started.wait(timeout=2):
            raise AssertionError("background status sync runner did not start")
        running = _wait_job(block, str(first["run_id"]), {"running"})
        if running["status"] != "running":
            raise AssertionError(f"job must transition to running: {running}")
        duplicate = block.sync_status({"max_complaint_rows": 3})
        if duplicate["run_id"] != first["run_id"] or duplicate["already_running"] is not True:
            raise AssertionError(f"running job must prevent duplicate browser automation: {duplicate}")
        release_runner.set()
        final = _wait_job(block, str(first["run_id"]), {"success"})
        if final["summary"]["strong_composite_matches"] != 1 or final["summary"]["weak_rejected"] != 1:
            raise AssertionError(f"final job summary mismatch: {final}")


def _assert_error_job_and_missing_run_id() -> None:
    with TemporaryDirectory(prefix="feedbacks-complaints-error-") as tmp:
        journal = JsonFileFeedbacksComplaintJournal(Path(tmp))
        journal.create_or_update(_record("feedback-error"))

        def failing_sync(payload: object) -> dict[str, object]:
            raise RuntimeError("fake background failure")

        block = SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=Path(tmp), journal=journal, status_sync_runner=failing_sync)
        started = block.sync_status({"max_complaint_rows": 3})
        failed = _wait_job(block, str(started["run_id"]), {"error"})
        if "fake background failure" not in failed["error"]:
            raise AssertionError(f"error job must store exact error: {failed}")
        if journal.find_by_feedback_id("feedback-error")["complaint_status"] != "waiting_response":
            raise AssertionError("failed status sync job must not corrupt complaint journal")
        try:
            block.get_sync_status_job("")
        except ValueError:
            pass
        else:
            raise AssertionError("missing run_id must be rejected with a clear error")
        try:
            block.get_sync_status_job("missing-run")
        except SheetVitrinaV1FeedbacksComplaintsError as exc:
            if exc.http_status != 404:
                raise AssertionError(f"missing run_id must map to 404, got {exc.http_status}")
        else:
            raise AssertionError("unknown run_id must not return a fake job")


def _assert_http_sync_job_routes() -> None:
    with TemporaryDirectory(prefix="feedbacks-complaints-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        journal = JsonFileFeedbacksComplaintJournal(runtime_dir)
        journal.create_or_update(_record("feedback-http"))

        def fake_sync(payload: object) -> dict[str, object]:
            time.sleep(0.1)
            journal.update_status("feedback-http", status="rejected", raw_status_text="Отклонена")
            return {
                "contract_name": "seller_portal_feedbacks_complaints_status_sync",
                "finished_at": "2026-05-02T00:00:05Z",
                "aggregate": {
                    "local_records_before": 1,
                    "pending_rows_read": 1,
                    "answered_rows_read": 1,
                    "matched_local_complaints": 1,
                    "statuses_updated": 1,
                    "weak_matches_rejected": 0,
                    "direct_matches": 1,
                    "strong_composite_matches": 0,
                },
            }

        complaints_block = SheetVitrinaV1FeedbacksComplaintsBlock(
            runtime_dir=runtime_dir,
            journal=journal,
            status_sync_runner=fake_sync,
        )
        entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir, feedbacks_complaints_block=complaints_block)
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            started_at = time.monotonic()
            code, start_payload = _post_json(
                f"{base_url}{DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_PATH}",
                {"max_complaint_rows": 3},
            )
            elapsed = time.monotonic() - started_at
            if code != 200 or not start_payload.get("run_id") or not start_payload.get("poll_url"):
                raise AssertionError(f"POST sync-status must return a job payload: {code} {start_payload}")
            if elapsed > 1.0:
                raise AssertionError(f"POST sync-status must return quickly, took {elapsed:.3f}s")
            run_id = str(start_payload["run_id"])
            job_payload = _wait_http_job(base_url, run_id)
            if job_payload["status"] != "success" or job_payload["summary"]["statuses_updated"] != 1:
                raise AssertionError(f"job route must expose fake success summary: {job_payload}")
            code, table_payload = _get_json(f"{base_url}{DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH}")
            if code != 200 or table_payload["summary"]["rejected"] != 1:
                raise AssertionError(f"complaints table must refresh from updated journal: {code} {table_payload}")
            missing_code, missing_payload = _get_json(
                f"{base_url}{DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_JOB_PATH}"
            )
            if missing_code != 422 or "run_id" not in str(missing_payload.get("error")):
                raise AssertionError(f"missing run_id must return 422 JSON: {missing_code} {missing_payload}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _record(feedback_id: str) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "complaint_status": "waiting_response",
        "submitted_at": "2026-05-02T00:00:00Z",
        "wb_category_label": "Другое",
        "complaint_text": "Просим проверить отзыв: тестовое описание.",
        "match_status": "exact",
        "match_score": "1.0",
        "rating": "1",
        "review_created_at": "2026-05-01T12:00:00Z",
        "nm_id": "123456",
        "supplier_article": "ART-1",
        "product_name": "Товар",
        "review_text": "Отзыв",
        "ai_complaint_fit": "yes",
        "ai_complaint_fit_label": "Да",
        "ai_category_label": "Другое",
        "ai_reason": "Просим проверить отзыв: тестовое описание.",
        "ai_confidence": "high",
        "ai_confidence_label": "Высокая",
    }


def _wait_job(
    block: SheetVitrinaV1FeedbacksComplaintsBlock,
    run_id: str,
    expected_statuses: set[str],
) -> dict[str, object]:
    deadline = time.monotonic() + 5
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        latest = block.get_sync_status_job(run_id)
        if latest.get("status") in expected_statuses:
            return latest
        time.sleep(0.02)
    raise AssertionError(f"job {run_id} did not reach {expected_statuses}, latest={latest}")


def _wait_http_job(base_url: str, run_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        code, latest = _get_json(
            f"{base_url}{DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_JOB_PATH}?run_id={run_id}"
        )
        if code != 200:
            raise AssertionError(f"job route returned {code}: {latest}")
        if latest.get("status") in {"success", "error"}:
            return latest
        time.sleep(0.05)
    raise AssertionError(f"http job {run_id} did not finish, latest={latest}")


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    return _read_json_response(request)


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="GET")
    return _read_json_response(request)


def _read_json_response(request: urllib_request.Request) -> tuple[int, dict[str, object]]:
    try:
        with urllib_request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
