"""Loopback-only listener for authenticated internal supplier GET views."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import gzip
import json
import logging
import time
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlparse

from packages.adapters import registry_upload_http_entrypoint as web
from packages.application.supplier_view import SupplierView
from packages.application.web_vitrina_window_read_context import WindowReadContextError

API = "/v1/sheet-vitrina-v1/supply/supplier-view"
PAGE = "/sheet-vitrina-v1/supplier-view"
SHELL = "/sheet-vitrina-v1/supply-view"


def build_supplier_view_server(runtime_dir: Path, *, port=8782, host="127.0.0.1"):
    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("supplier view listener must remain loopback-only")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, status, value, *, html=False):
            raw = value.encode("utf-8") if html else json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            compressed = "gzip" in self.headers.get("Accept-Encoding", "").lower() and len(raw) > 1024
            body = gzip.compress(raw, compresslevel=1) if compressed else raw
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Vary", "Accept-Encoding, Cookie")
            self.send_header("X-Content-Type-Options", "nosniff")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(body)
            logging.getLogger("supplier_view").info(json.dumps({
                "event": "supplier_view_http", "status": status,
                "path": urlparse(self.path).path, "body_bytes": len(body),
                "elapsed_ms": round(1000 * (time.monotonic() - getattr(self, "started_at", time.monotonic())), 2),
            }))

        def do_GET(self):
            self.started_at = time.monotonic()
            parsed = urlparse(self.path)
            match = re.fullmatch(re.escape(API) + r"(?:/(contracts|sup_[A-Za-z0-9_-]+)(?:/(documents|correction))?)?", parsed.path)
            if parsed.path not in {PAGE, SHELL} and not match:
                self._send(404, {"error": "not_found"})
                return
            try:
                # A fresh facade per request: no cross-user data cache or stale
                # database generation. Authentication shares the pinned snapshot.
                self.runtime_entrypoint = SupplierView(runtime_dir)
                with self.runtime_entrypoint.snapshot():
                    if not web._ensure_web_auth(self, parsed):
                        return
                    if not web._ensure_supply_operator_role(self, parsed.path):
                        return
                    if parsed.path == PAGE:
                        payload = web._render_sheet_vitrina_supplier_ui(
                            user_config_key=web._current_web_user_config_key(self),
                            can_delete_shipments=True, can_recheck_prices=True,
                            can_manage_documents=True, can_manage_financial_documents=True,
                            embedded="operator", independent_read=True)
                    elif parsed.path == SHELL:
                        payload = web._render_sheet_vitrina_operator_ui(
                            daily_report_path=web.DEFAULT_SHEET_DAILY_REPORT_PATH,
                            stock_report_path=web.DEFAULT_SHEET_STOCK_REPORT_PATH,
                            plan_report_path=web.DEFAULT_SHEET_PLAN_REPORT_PATH,
                            refresh_path=web.DEFAULT_SHEET_REFRESH_PATH, load_path=web.DEFAULT_SHEET_LOAD_PATH,
                            status_path=web.DEFAULT_SHEET_STATUS_PATH, job_path=web.DEFAULT_SHEET_JOB_PATH,
                            user_config_key=web._current_web_user_config_key(self),
                            embedded_tab="factory-order", independent_supplier_read=True)
                    else:
                        item, section = match.groups()
                        if item == "contracts" and section:
                            self._send(404, {"error": "not_found"})
                            return
                        payload = self.runtime_entrypoint.read("" if item == "contracts" else (item or ""),
                                                              "contracts" if item == "contracts" else (section or ""))
                # Release the database snapshot before network transfer.
                self._send(200, payload, html=parsed.path in {PAGE, SHELL})
            except ValueError:
                self._send(404, {"error": "supplier_order_unavailable"})
            except (sqlite3.Error, WindowReadContextError):
                self._send(503, {"error": "supplier_view_temporarily_unavailable"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self._send(503, {"error": "supplier_view_failed"})

        def do_POST(self):
            self._send(405, {"error": "read_only_endpoint"})

        do_PATCH = do_PUT = do_DELETE = do_POST

    return HTTPServer((host, port), Handler)
