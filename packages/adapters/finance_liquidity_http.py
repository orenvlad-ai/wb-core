"""Optional loopback HTTP adapter for the isolated Finance cash store."""

from __future__ import annotations

import hashlib
import html
import hmac
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from packages.application.business_data_write_barrier import barrier_status

from packages.adapters.finance_liquidity_auth import (
    FinanceAuthDenied,
    FinanceAuthUnavailable,
)
from packages.application.finance_liquidity_cash import (
    FinanceCashError,
    FinanceCashService,
)
from packages.contracts.finance_liquidity_cash import (
    FINANCE_CASH_API_PREFIX,
    FINANCE_CASH_CONTRACT,
    FINANCE_CASH_UI_PREFIX,
)


class FinanceHttpApp:
    def __init__(
        self,
        service: FinanceCashService,
        auth: Any,
        *,
        read_enabled: bool,
        write_enabled: bool,
        csrf_secret: str,
        static_dir: Path,
        allowed_origin: str,
        business_runtime_dir: Path | None = None,
        instance_label: str = "",
        store_id: str = "",
        store_mode: str = "",
    ) -> None:
        self.service, self.auth = service, auth
        self.read_enabled, self.write_enabled = read_enabled, write_enabled
        self.csrf_secret, self.static_dir = csrf_secret, Path(static_dir)
        self.allowed_origin = allowed_origin.rstrip("/")
        self.instance_label = str(instance_label or "").strip()
        self.store_id = str(store_id or "").strip()
        self.store_mode = str(store_mode or "").strip()
        self.business_runtime_dir = (
            Path(business_runtime_dir).resolve() if business_runtime_dir else None
        )

    def csrf(self, actor: str) -> str:
        return hmac.new(
            self.csrf_secret.encode(), actor.encode(), hashlib.sha256
        ).hexdigest()


def build_finance_http_server(
    host: str, port: int, app: FinanceHttpApp
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            self._handle(False)

        def do_POST(self) -> None:
            self._handle(True)

        def do_PATCH(self) -> None:
            self._handle(True)

        def _handle(self, mutation: bool) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path.startswith(FINANCE_CASH_UI_PREFIX):
                    if mutation:
                        self._fail(405, "method_not_allowed", "Method is not allowed")
                        return
                    self._static(parsed.path)
                    return
                if not parsed.path.startswith(FINANCE_CASH_API_PREFIX):
                    self._fail(404, "not_found", "Route not found")
                    return
                if not app.read_enabled:
                    self._fail(503, "finance_read_disabled", "Finance read is disabled")
                    return
                principal = app.auth.authenticate(self.headers)
                actor, capabilities = (
                    str(principal["username"]),
                    set(principal.get("capabilities") or []),
                )
                if mutation:
                    if not app.write_enabled:
                        self._fail(
                            503, "finance_write_disabled", "Finance write is disabled"
                        )
                        return
                    if not app.allowed_origin or not hmac.compare_digest(
                        str(self.headers.get("Origin") or "").rstrip("/"),
                        app.allowed_origin,
                    ):
                        self._fail(403, "origin_failed", "Same-origin request required")
                        return
                    if not hmac.compare_digest(
                        str(self.headers.get("X-Finance-CSRF") or ""), app.csrf(actor)
                    ):
                        self._fail(403, "csrf_failed", "CSRF validation failed")
                        return
                    # Finance is an isolated store, but it is still business
                    # data.  The canonical maintenance barrier is read only
                    # and checked after identity/CSRF but before any Finance
                    # object lookup or write.  Missing owner context blocks.
                    if app.business_runtime_dir is None:
                        self._fail(
                            503,
                            "business_data_barrier_unavailable",
                            "Business-data write barrier is unavailable",
                        )
                        return
                    barrier = barrier_status(app.business_runtime_dir)
                    if bool(barrier.get("active")):
                        self._fail(
                            423,
                            "business_data_maintenance",
                            "Business-data maintenance is active",
                        )
                        return
                    payload = self._payload()
                    key = str(self.headers.get("Idempotency-Key") or "")
                    operation_id = str(self.headers.get("X-Operation-Id") or "")
                else:
                    payload = {}
                    key = operation_id = ""
                self._route(
                    parsed.path,
                    parse_qs(parsed.query),
                    mutation,
                    payload,
                    actor,
                    capabilities,
                    operation_id,
                    key,
                )
            except FinanceAuthUnavailable:
                self._fail(
                    503,
                    "finance_auth_unavailable",
                    "Finance authorization is unavailable",
                )
            except FinanceAuthDenied:
                self._fail(401, "authentication_required", "Authentication required")
            except FinanceCashError as exc:
                self._fail(exc.status, exc.code, str(exc), exc.data)
            except ValueError as exc:
                self._fail(400, "invalid_request", str(exc))

        def _route(
            self,
            path: str,
            query: Mapping[str, list[str]],
            mutation: bool,
            payload: Mapping[str, Any],
            actor: str,
            caps: set[str],
            operation_id: str,
            key: str,
        ) -> None:
            def need(cap: str) -> None:
                if cap not in caps:
                    raise FinanceCashError(
                        "finance_capability_denied", "Finance capability denied", 403
                    )

            suffix = path[len(FINANCE_CASH_API_PREFIX) :] or "/"
            if not mutation:
                need("finance")
                if suffix == "/capabilities":
                    self._ok(
                        {
                            "capabilities": sorted(caps),
                            "csrf_token": app.csrf(actor),
                            "read_enabled": app.read_enabled,
                            "write_enabled": app.write_enabled,
                            "instance_label": app.instance_label,
                            "store_id": app.store_id,
                            "store_mode": app.store_mode,
                        }
                    )
                    return
                if suffix == "/accounts":
                    self._ok({"accounts": app.service.list_accounts()})
                    return
                if suffix.startswith("/accounts/") and suffix.endswith("/movements"):
                    ident = suffix.split("/")[2]
                    self._ok(
                        app.service.movements(ident, (query.get("as_of") or [None])[0])
                    )
                    return
                if suffix.startswith("/accounts/"):
                    self._ok({"account": app.service.get_account(suffix.split("/")[2])})
                    return
                if suffix == "/categories":
                    self._ok({"categories": app.service.list_categories()})
                    return
                if suffix == "/counterparties":
                    self._ok({"counterparties": app.service.list_counterparties()})
                    return
                if suffix == "/audit":
                    need("finance_admin")
                    self._ok({"events": app.service.list_audit_events()})
                    return
                if suffix == "/documents":
                    self._ok(
                        {
                            "documents": app.service.list_documents(
                                account_id=(query.get("account_id") or [None])[0]
                            )
                        }
                    )
                    return
                if suffix.startswith("/documents/"):
                    self._ok(app.service.get_document(suffix.split("/")[2]))
                    return
                if suffix == "/cash-reconciliations":
                    self._ok(
                        {
                            "reconciliations": app.service.list_reconciliations(
                                (query.get("account_id") or [None])[0]
                            )
                        }
                    )
                    return
                if suffix.startswith("/operations/"):
                    self._ok(
                        app.service.get_operation(
                            suffix.split("/")[2], actor, "finance_admin" in caps
                        )
                    )
                    return
                raise FinanceCashError("not_found", "Route not found", 404)
            if suffix == "/accounts":
                need("finance_admin")
                self._ok(
                    app.service.create_account(payload, actor, operation_id, key), 201
                )
                return
            if suffix == "/categories":
                need("finance_admin")
                self._ok(
                    app.service.create_category(payload, actor, operation_id, key), 201
                )
                return
            if suffix == "/counterparties":
                need("finance_operate")
                self._ok(app.service.create_counterparty(payload, actor, operation_id, key), 201)
                return
            if suffix == "/openings/common-draft":
                need("finance_operate")
                self._ok(app.service.prepare_common_openings(payload, actor, operation_id, key), 201)
                return
            parts = suffix.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "directories" and self.command == "POST":
                need("finance_admin")
                self._ok(app.service.update_directory(parts[1], parts[2], payload, actor, operation_id, key))
                return
            if suffix == "/documents":
                need("finance_operate")
                self._ok(
                    app.service.create_document(payload, actor, operation_id, key), 201
                )
                return
            if len(parts) >= 2 and parts[0] == "documents":
                document_id = parts[1]
                if len(parts) == 2 and self.command == "PATCH":
                    need("finance_operate")
                    self._ok(
                        app.service.patch_document(
                            document_id, payload, actor, operation_id, key
                        )
                    )
                    return
                if len(parts) == 3 and parts[2] == "post":
                    need("finance_operate")
                    self._ok(
                        app.service.post_document(
                            document_id, payload, actor, operation_id, key
                        )
                    )
                    return
                if len(parts) == 3 and parts[2] == "reverse":
                    need("finance_admin")
                    self._ok(
                        app.service.reverse_document(
                            document_id, payload, actor, operation_id, key
                        )
                    )
                    return
                if len(parts) == 3 and parts[2] == "replace-opening":
                    need("finance_admin")
                    self._ok(
                        app.service.replace_opening(
                            document_id, payload, actor, operation_id, key
                        )
                    )
                    return
            if (
                len(parts) == 3
                and parts[0] == "transfers"
                and parts[2] in {"complete", "cancel"}
            ):
                need("finance_operate")
                self._ok(
                    app.service.transfer_transition(
                        parts[1], parts[2], payload, actor, operation_id, key
                    )
                )
                return
            if suffix == "/cash-reconciliations":
                need("finance_operate")
                self._ok(
                    app.service.record_reconciliation(
                        payload, actor, operation_id, key
                    ),
                    201,
                )
                return
            if (
                len(parts) == 3
                and parts[0] == "cash-reconciliations"
                and parts[2] == "resolve"
            ):
                need("finance_operate")
                self._ok(
                    app.service.resolve_reconciliation(
                        parts[1],
                        payload,
                        actor,
                        "finance_admin" in caps,
                        operation_id,
                        key,
                    )
                )
                return
            raise FinanceCashError("not_found", "Route not found", 404)

        def _payload(self) -> Mapping[str, Any]:
            if (
                str(self.headers.get("Content-Type") or "")
                .split(";", 1)[0]
                .strip()
                .lower()
                != "application/json"
            ):
                raise ValueError("application/json required")
            length = int(str(self.headers.get("Content-Length") or "0"))
            if not 0 < length <= 64 * 1024:
                raise ValueError("invalid request size")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON object required")
            return value

        def _static(self, path: str) -> None:
            relative = path[len(FINANCE_CASH_UI_PREFIX) :] or "index.html"
            if relative == "":
                relative = "index.html"
            if "/" in relative or ".." in relative:
                self._fail(404, "not_found", "Static path not found")
                return
            file = app.static_dir / relative
            if not file.is_file():
                self._fail(404, "not_found", "Static path not found")
                return
            mime = (
                "text/html; charset=utf-8"
                if file.suffix == ".html"
                else "text/css; charset=utf-8"
                if file.suffix == ".css"
                else "application/javascript; charset=utf-8"
            )
            raw = file.read_bytes()
            if file.name == "index.html":
                banner = (
                    '<div class="environment-badge" data-instance-label '
                    'role="status">' + html.escape(app.instance_label) + "</div>"
                    if app.instance_label
                    else ""
                )
                raw = raw.replace(
                    b"{{FINANCE_INSTANCE_BANNER}}",
                    banner.encode("utf-8"),
                )
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _ok(self, data: Mapping[str, Any], status: int = 200) -> None:
            self._json(
                status,
                {
                    "contract": FINANCE_CASH_CONTRACT,
                    "data": data,
                    "request_id": hashlib.sha256(
                        (self.path + str(id(self))).encode()
                    ).hexdigest()[:24],
                },
            )

        def _fail(
            self,
            status: int,
            code: str,
            message: str,
            data: Mapping[str, Any] | None = None,
        ) -> None:
            self._json(
                status,
                {
                    "contract": FINANCE_CASH_CONTRACT,
                    "error": {"code": code, "message": message, **dict(data or {})},
                },
            )

        def _json(self, status: int, value: Mapping[str, Any]) -> None:
            raw = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return ThreadingHTTPServer((host, port), Handler)
