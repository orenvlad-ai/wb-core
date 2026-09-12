"""B2-03 disposable crash, exact lookup, owner and HTTP authorization fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import hashlib
import json
import sqlite3
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.warehouse_current_sync_job_smoke import entry_fixture, wait_terminal
from apps.registry_upload_http_entrypoint_auth_smoke import _password_hash
from ci.fixture_process import checkpoint, fixture_process
from packages.application.warehouse_update_journal import WarehouseUpdateJournal, WarehouseRequestConflict, validate_warehouse_request, PHASES
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, WarehouseJobOwnershipError
from packages.application.registry_upload_http_entrypoint import SheetVitrinaV1OperatorJobStore
from packages.adapters.registry_upload_http_entrypoint import build_registry_upload_http_server
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

KEY = "warehouse_request_00000001"
SCOPE = "local_operator"
PATH = "/v1/sheet-vitrina-v1/warehouses/sync"


def accept(journal, key=KEY, scope=SCOPE, payload=None):
    body = {"request_key": key, **(payload or {})}
    key, fingerprint, encoded = validate_warehouse_request(body, scope)
    return journal.accept(request_key=key, request_scope=scope, payload_fingerprint=fingerprint, request_payload_json=encoded)


def crash_run(channel, root, boundary):
    entry, effects, _ = entry_fixture(root)
    if boundary == "before_claim":
        entry.warehouse_update_journal.claim = lambda run_id: threading.Event().wait(15)
    if boundary == "after_effect":
        def transit():
            checkpoint(channel, "after_effect")
            return {}
        entry.wb_supplies_block.collect_all_due_transit_costs = transit
        entry.wb_supplies_block.sync_functional_sources = lambda **kw: {"confirmed_ids": list(range(81)), "receipt": "committed-before-crash"}
    with patch("packages.application.fbs_accounting_runtime.refresh", return_value={}):
        job = entry.handle_warehouse_manual_sync_start_request({"request_key": KEY})
        if boundary == "before_claim":
            checkpoint(channel, boundary)
        elif boundary == "terminal":
            assert wait_terminal(entry, job["run_id"])["status"] == "success"
            checkpoint(channel, boundary)
        else:
            threading.Event().wait(15)


def crash_cases(root):
    for boundary in ("before_claim", "after_effect", "terminal"):
        case = root / boundary
        with fixture_process(crash_run, case, boundary) as child:
            child.wait(boundary)
            # Reader constructed without schema ownership, as on a GET.
            journal = WarehouseUpdateJournal.__new__(WarehouseUpdateJournal)
            journal.db_path, journal.runtime_dir = case / "registry_upload_runtime.sqlite3", case
            prior = journal.lookup(request_key=KEY, request_scope=SCOPE)
            assert prior and prior["job_id"]
            if boundary == "before_claim":
                assert prior["status"] == "accepted" and not prior["attempt_id"]
            child.crash()
        restarted, effects, _ = entry_fixture(case)
        with patch("packages.application.fbs_accounting_runtime.refresh", return_value={}):
            picker = restarted.operator_jobs.resume_warehouse_pending(
                runtime_dir=case, journal=restarted.warehouse_update_journal,
                runner=restarted._run_warehouse_manual_sync_job,
            )
            if picker:
                picker.join(8)
                assert not picker.is_alive()
        result = restarted.handle_warehouse_manual_sync_status_request(prior["job_id"])
        assert result["run_id"] == prior["job_id"]
        replay = restarted.handle_warehouse_manual_sync_start_request({"request_key": KEY})
        assert replay["run_id"] == prior["job_id"]
        if boundary == "before_claim":
            assert result["status"] == "success" and effects.count("network") == 1
        elif boundary == "after_effect":
            assert result["status"] == "interrupted" and not effects and not result["can_start_new"]
            phases = result["durable_journal"]["phases"]
            assert phases[0]["status"] == "success"
            assert phases[0]["details"]["confirmed_ids"] == list(range(81))
            assert phases[1]["status"] == "failed"
        else:
            assert result["status"] == "success" and not effects
            assert result["technical_details"] == restarted.handle_warehouse_manual_sync_status_request(prior["job_id"])["technical_details"]
        print("durable restart:", boundary, result["status"], "same ID; no duplicate effect")


def identity_cases(root):
    entry, _, _ = entry_fixture(root)
    journal = entry.warehouse_update_journal
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda _: accept(journal, payload={"b": 2, "a": {"x": 1}}), range(8)))
    assert sum(created for _, created in jobs) == 1
    job = jobs[0][0]
    same, created = accept(journal, payload={"a": {"x": 1}, "b": 2})
    assert same["job_id"] == job["job_id"] and not created
    try:
        accept(journal, payload={"a": 999})
        raise AssertionError("conflict accepted")
    except WarehouseRequestConflict:
        pass
    assert journal.lookup(request_key=KEY, request_scope="other") is None
    assert journal.lookup(public_id=job["job_id"], request_scope="other") is None
    assert journal.lookup(public_id="lost_legacy_uuid", request_scope=SCOPE) is None
    with warehouse_functional_job_lock(root):
        assert journal.claim(job["durable_run_id"])
        assert not journal.claim(job["durable_run_id"])
        journal.finish(job["durable_run_id"], status="success", result={"exact_ids": list(range(81))})
    other, created = accept(journal, scope="other", payload={"a": 999})
    assert created and other["job_id"] != job["job_id"]
    with warehouse_functional_job_lock(root):
        assert journal.claim(other["durable_run_id"])
        journal.finish(other["durable_run_id"], status="success")
        for _ in range(55):
            old = journal.start(trigger_source="hourly")
            journal.finish(old, status="success")
    # Reads deny DDL/DML, including a hidden schema bootstrap in either route.
    original_connect = sqlite3.connect
    writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_ALTER_TABLE}
    statements = []
    def readonly_connect(*args, **kw):
        assert kw.get("uri") and "mode=ro" in str(args[0]), args
        conn = original_connect(*args, **kw)
        conn.set_authorizer(lambda action, *rest: sqlite3.SQLITE_DENY if action in writes else sqlite3.SQLITE_OK)
        conn.set_trace_callback(statements.append)
        return conn
    before = journal.db_path.read_bytes()
    with patch("packages.application.warehouse_update_journal.sqlite3.connect", readonly_connect), patch(
        "packages.application.warehouse_update_journal.ensure_warehouse_update_journal_schema", side_effect=AssertionError("GET bootstrap"),
    ):
        by_id = entry.handle_warehouse_manual_sync_status_request(job["job_id"])
        by_key = entry.handle_warehouse_manual_sync_status_request(request_key=KEY)
        assert by_id["run_id"] == by_key["run_id"] == job["job_id"]
        assert by_id["technical_details"]["exact_ids"] == list(range(81))
        assert by_id["durable_journal"]["phases"][0]["run_id"] == job["durable_run_id"]
    assert journal.db_path.read_bytes() == before
    assert statements and not any("CREATE " in sql or "UPDATE " in sql or "INSERT " in sql for sql in statements)
    print("identity: 8 concurrent accepts one row; canonical conflict; scope isolation; exact >50; read-only SQL")


@contextmanager
def http_fixture(root):
    entry, effects, _ = entry_fixture(root)
    for username, role, sections in (("other", "supply_operator", ["supply"]), ("forbidden", "operator", ["reports"])):
        entry.runtime.save_sheet_vitrina_user({"user_id": username, "username": username, "display_name": username,
            "role": role, "allowed_sections": sections, "manage_users": False, "password_hash": _password_hash("fixture-password"),
            "is_active": True, "created_at": "2026-09-12T00:00:00Z", "updated_at": "2026-09-12T00:00:00Z"})
    env = {"WB_CORE_WEB_AUTH_REQUIRED": "1", "WB_CORE_WEB_AUTH_USERNAME": "owner",
           "WB_CORE_WEB_AUTH_PASSWORD_HASH": _password_hash("fixture-password"), "WB_CORE_WEB_AUTH_SESSION_SECRET": "disposable-test-secret"}
    with patch.dict("os.environ", env):
        server = build_registry_upload_http_server(RegistryUploadHttpEntrypointConfig(host="127.0.0.1", port=0, runtime_dir=root,
            upload_path="/v1/registry-upload", sheet_plan_path="/v1/sheet-vitrina-v1/plan", sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path="/v1/sheet-vitrina-v1/status", sheet_operator_ui_path="/sheet-vitrina-v1/operator"), entrypoint=entry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield entry, effects, server.server_port
        finally:
            server.shutdown(); server.server_close(); thread.join(3)


def request(port, path, *, body=None, cookie="", form=False):
    client = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Accept": "application/json"}
    if cookie: headers["Cookie"] = cookie
    encoded = None if body is None else body if form else json.dumps(body)
    if body is not None: headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    client.request("GET" if body is None else "POST", path, body=encoded, headers=headers)
    response = client.getresponse()
    raw = response.read()
    result = (response.status, dict(response.getheaders()), json.loads(raw) if raw.startswith(b"{") else raw.decode())
    client.close()
    return result


def login(port, username):
    status, headers, _ = request(port, "/login", body=f"username={username}&password=fixture-password", form=True)
    assert status == 303, status
    return headers["Set-Cookie"].split(";", 1)[0]


def http_cases(root):
    with http_fixture(root) as (entry, effects, port):
        cookies = {name: login(port, name) for name in ("owner", "other", "forbidden")}
        assert request(port, PATH + "/status?request_key=" + KEY)[0] == 401
        for path, body in ((PATH, {"request_key": KEY}), (PATH + "/status?request_key=" + KEY, None)):
            assert request(port, path, body=body, cookie=cookies["forbidden"])[0] == 403
        with patch("packages.application.fbs_accounting_runtime.refresh", return_value={}):
            code, _, job = request(port, PATH, body={"request_key": KEY, "fixture": {"b": 2, "a": 1}}, cookie=cookies["owner"])
            assert code == 202 and job["run_id"], job
            owner_scope = "webcore_user_" + hashlib.sha256(b"admin:owner").hexdigest()[:32]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = entry.handle_warehouse_manual_sync_status_request(job["run_id"], request_scope=owner_scope)
                if status["status"] == "success": break
                time.sleep(.02)
            assert status["status"] == "success", status
        for query in ("run_id=" + job["run_id"], "request_key=" + KEY):
            assert request(port, PATH + "/status?" + query, cookie=cookies["other"])[0] == 404
            assert request(port, PATH + "/status?" + query, cookie=cookies["owner"])[0] == 200
        code, _, repeated = request(port, PATH, body={"fixture": {"a": 1, "b": 2}, "request_key": KEY}, cookie=cookies["owner"])
        assert code == 202 and repeated["run_id"] == job["run_id"] and effects.count("network") == 1
        assert request(port, PATH, body={"request_key": KEY, "fixture": "changed"}, cookie=cookies["owner"])[0] == 409
        assert request(port, PATH, body={"request_key": "short"}, cookie=cookies["owner"])[0] == 422
        for method in (entry.handle_sheet_operator_job_request, entry.handle_sheet_operator_job_text_request):
            try:
                method(job["run_id"])
                raise AssertionError("generic reader exposed warehouse result")
            except ValueError:
                pass
        # Schema is prepared once before server construction; exact GET never migrates.
        with patch("packages.application.warehouse_update_journal.ensure_warehouse_update_journal_schema", side_effect=AssertionError("route bootstrap")):
            assert request(port, PATH + "/status?run_id=" + job["run_id"], cookie=cookies["owner"])[0] == 200
        print("HTTP: real session 401/403; body/key propagation; other principal 404; mismatch 409; generic readers denied")


def main():
    with TemporaryDirectory(prefix="warehouse-durable-") as raw:
        root = Path(raw)
        identity_cases(root / "identity")
        crash_cases(root / "crash")
        http_cases(root / "http")
    print("warehouse_durable_identity_smoke: OK")


if __name__ == "__main__":
    main()
