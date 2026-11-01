"""Supplier read isolation, authorization, financial parity and HTTP smoke."""
from __future__ import annotations
import base64
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import time
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.adapters.supplier_view_http import build_supplier_view_server, API, PAGE, SHELL
from packages.adapters.registry_upload_http_entrypoint import WEB_AUTH_COOKIE_NAME
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.supplier_view import SupplierView
from packages.application.warehouse_functional import ensure_warehouse_functional_schema
from packages.application.supplier_shipment_factual_correction import _ensure_correction_schema, CORRECTION_TABLE


def cookie(username="viewer", role="admin", *, expired=False):
    def b64(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")
    payload = b64(json.dumps({"u": username, "r": role, "exp": int(time.time()) + (-10 if expired else 600)}).encode())
    signature = b64(hmac.new(b"isolated-test-secret", payload.encode(), hashlib.sha256).digest())
    return WEB_AUTH_COOKIE_NAME + "=" + payload + "." + signature


def seed(runtime_dir):
    runtime = RegistryUploadDbBackedRuntime(runtime_dir)
    runtime.list_supplier_shipments()  # Fixture bootstrap only, never used by view.
    runtime.save_supplier_shipment(header={
        "shipment_id": "sup_test", "created_at": "2026-11-01T00:00:00Z",
        "updated_at": "2026-11-01T00:00:00Z", "shipment_date": "2026-11-01",
        "invoice_no": "TEST", "currency": "CNY", "product_qty_total": 10,
        "product_amount_total": 20, "invoice_amount_total": 20,
    }, lines=[{"line_id": "line_test", "line_type": "product", "qty": 10,
               "unit_price": 2, "amount": 20, "internal_nm_id": 123, "currency": "CNY"}])
    with sqlite3.connect(runtime.db_path) as conn:
        ensure_warehouse_functional_schema(conn)
        _ensure_correction_schema(conn)
        conn.execute(f"INSERT INTO {CORRECTION_TABLE}(correction_id,request_fingerprint,shipment_id,actor,source,reason,status,phase,progress_text,requested_at,updated_at,report_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("correction_test", "request_test", "sup_test", "test", "test", "test", "success", "done", "done",
             "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z", json.dumps({"scope": {"date_from": "2026-11-01"}, "business_projection": {"revision": "test"}, "replay_rows": "x" * 1000000})))
    runtime.save_sheet_vitrina_user({"user_id": "supply_reader", "username": "supply_reader",
        "display_name": "Supply", "role": "supply_operator", "is_active": True,
        "password_hash": "unused", "allowed_sections": ["supply"], "manage_users": False,
        "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-01T00:00:00Z"})
    runtime.save_sheet_vitrina_user({"user_id": "other_reader", "username": "other_reader",
        "display_name": "Other", "role": "operator", "is_active": True,
        "password_hash": "unused", "allowed_sections": ["vitrina"], "manage_users": False,
        "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-01T00:00:00Z"})
    return runtime


def main():
    with TemporaryDirectory(prefix="supplier-view-test-") as tmp, patch.dict(os.environ, {
        "WB_CORE_WEB_AUTH_USERNAME": "viewer", "WB_CORE_WEB_AUTH_PASSWORD_HASH": "unused",
        "WB_CORE_WEB_AUTH_SESSION_SECRET": "isolated-test-secret", "WB_CORE_WEB_AUTH_REQUIRED": "1",
        "WB_CORE_SUPPLIER_AUTH_USERNAME": "supplier", "WB_CORE_SUPPLIER_AUTH_PASSWORD_HASH": "unused",
    }):
        runtime = seed(Path(tmp))
        before = hashlib.sha256(runtime.db_path.read_bytes()).hexdigest()
        view = SupplierView(Path(tmp))
        with view.snapshot() as v:
            detail = v.read("sup_test")
            assert detail["exact_landed_cost_total_rub"] is None
            assert "product_lines" not in detail and len(detail["lines"]) == 1
            correction = detail["factual_date_correction"]
            assert correction["finished"] and not correction["active"]
            assert correction["report"] == {"scope": {"date_from": "2026-11-01"}, "business_projection": {"revision": "test"}}
            assert len(json.dumps(correction)) < 1024
            assert "expense_allocation" in v.read()["shipments"][0]
            conn = v.runtime._rows("PRAGMA query_only")[0][0]
            assert conn == 1
            try:
                v.runtime._rows("DELETE FROM sheet_vitrina_v1_supplier_shipments")
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("view allowed a database write")
        server = build_supplier_view_server(Path(tmp), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = "http://127.0.0.1:" + str(server.server_address[1])
        def request(path, token=None, method="GET"):
            headers = {"Accept": "application/json", "Accept-Encoding": "gzip"}
            if token: headers["Cookie"] = token
            try: response = urlopen(Request(origin + path, headers=headers, method=method), timeout=5)
            except HTTPError as exc: response = exc
            body = response.read()
            if response.headers.get("Content-Encoding") == "gzip": body = gzip.decompress(body)
            return response.status, body
        try:
            assert request(API)[0] == 401
            assert request(API, cookie(expired=True))[0] == 401
            assert request(API, cookie("supplier", "supplier"))[0] == 403
            assert request(API, cookie("unknown", "operator"))[0] == 401
            for path in (API, API + "/sup_test", API + "/sup_test/documents", API + "/contracts", PAGE, SHELL):
                status, body = request(path, cookie())
                assert status == 200, (path, status, body[:200])
            assert request(API, cookie("other_reader", "operator"))[0] == 403
            assert request(API, cookie("supply_reader", "supply_operator"))[0] == 200
            for method in ("POST", "PATCH", "DELETE", "PUT"):
                assert request(API, cookie(), method)[0] == 405
            assert request(API + "/sup_missing", cookie())[0] == 404
            assert request(API + "/sup_test/invoice", cookie())[0] == 404
            assert request("/v1/sheet-vitrina-v1/settings/users", cookie())[0] == 404
            assert hashlib.sha256(runtime.db_path.read_bytes()).hexdigest() == before
            # Permissions are re-read on every request, not cached in the process.
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute("UPDATE sheet_vitrina_v1_users SET is_active=0 WHERE username='supply_reader'")
            assert request(API, cookie("supply_reader", "supply_operator"))[0] == 401
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute("UPDATE sheet_vitrina_v1_supplier_shipments SET archived_at='2026-11-01T00:00:00Z' WHERE shipment_id='sup_test'")
            assert json.loads(request(API, cookie())[1])["shipments"] == []
            assert request(API + "/sup_test", cookie())[0] == 404
            assert request(API + "/sup_test/documents", cookie())[0] == 404
        finally:
            server.shutdown(); server.server_close(); thread.join()
    print("supplier_view_smoke: PASS (auth, revocation, read-only, null cost, routes, pages)")


if __name__ == "__main__":
    main()
