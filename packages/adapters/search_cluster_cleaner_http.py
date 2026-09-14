"""Narrow authenticated HTTP boundary. All reads use saved operational state."""
from __future__ import annotations

import hmac
from http import HTTPStatus
import json
import re
import sqlite3
from urllib.parse import parse_qs

from packages.contracts.search_cluster_cleaner import CleanerError, Principal

PREFIX = "/v1/sheet-vitrina-v1/ads/keyword-cleaner"
MAX_BODY = 65536


def handles(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def dispatch(handler, parsed, web, *, auth_config, authenticated_user, has_ads, write_json):
    """Auth functions are supplied by the established web-session boundary."""
    def respond(status, payload):
        write_json(handler, HTTPStatus(status), payload, extra_headers={"Cache-Control": "private, no-store"})

    try:
        config = auth_config()
        user = authenticated_user(handler, config) if config["enabled"] else None
        principal = Principal(str((user or {}).get("username") or ""), bool(user), bool(config["enabled"]),
                              bool(user and has_ads(user)))
        principal.require_read()
        path = parsed.path[len(PREFIX):]
        query = parse_qs(parsed.query, keep_blank_values=True)
        if handler.command == "GET":
            if path == "/summary":
                result = web.summary(principal)
            elif path == "/reviews":
                result = web.reviews(principal, cursor=query.get("cursor", [""])[0], limit=int(query.get("limit", [50])[0]))
            elif path == "/history":
                result = web.history(principal, cursor=int(query.get("cursor", [0])[0]), limit=int(query.get("limit", [50])[0]))
            elif match := re.fullmatch(r"/requests/([A-Za-z0-9_.:-]{8,120})", path):
                result = web.require_service().get_request(match[1], principal)
            elif match := re.fullmatch(r"/runs/([A-Za-z0-9-]{1,120})", path):
                result = web.require_service().run_detail(match[1], principal)
            elif match := re.fullmatch(r"/profiles/([1-9][0-9]{0,15})", path):
                result = web.require_service().get_profile(int(match[1]), principal)
            else:
                raise CleanerError("not_found", "Страница не найдена", 404)
            respond(200, result)
            return

        if handler.command != "POST":
            raise CleanerError("method_not_allowed", "Метод не поддерживается", 405)
        origin = str(handler.headers.get("Origin") or "").strip()
        site = str(handler.headers.get("Sec-Fetch-Site") or "").strip().lower()
        # Hosted origin is server-owned. Local fixture origin comes from the
        # listening socket, never from client Host/X-Forwarded-* headers.
        host, port = handler.server.server_address[:2]
        expected_origin = web.origin or (f"http://{host}:{port}" if host == "127.0.0.1" else "")
        if not (origin and expected_origin and hmac.compare_digest(origin, expected_origin)
                and site not in {"cross-site", "same-site"}
                and handler.headers.get("X-WB-Keyword-Cleaner-CSRF") == "1"
                and str(handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() == "application/json"):
            raise CleanerError("csrf_failed", "Не удалось проверить источник команды. Обновите страницу", 403)
        cleaner = web.require_mutation(principal)
        length = int(handler.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY or handler.headers.get("Transfer-Encoding"):
            raise CleanerError("invalid_body", "Некорректный размер команды", 413 if length > MAX_BODY else 400)
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise CleanerError("invalid_body", "Команда должна быть объектом", 400)
        allowed = {"request_id"}
        if path == "/settings":
            allowed |= {"expected_revision", "enabled", "schedule_time"}
            operation = lambda: cleaner.update_settings(payload, principal)
        elif path == "/runs":
            operation = lambda: cleaner.start_run(payload, principal)
        elif match := re.fullmatch(r"/reviews/([A-Za-z0-9-]{1,120})/decision", path):
            allowed |= {"expected_revision", "decision"}
            operation = lambda: cleaner.decide(match[1], payload, principal)
        elif match := re.fullmatch(r"/profiles/([1-9][0-9]{0,15})/(versions|activate)", path):
            allowed |= {"expected_revision", "profile" if match[2] == "versions" else "version"}
            operation = (lambda: cleaner.create_profile(int(match[1]), payload, principal)) if match[2] == "versions" else (lambda: cleaner.activate_profile(int(match[1]), payload, principal))
        else:
            raise CleanerError("not_found", "Команда не найдена", 404)
        if set(payload) - allowed:
            raise CleanerError("invalid_fields", "Команда содержит неизвестные поля", 422)
        if path == "/settings" and payload.get("enabled") is True:
            settings = cleaner.summary(principal)["settings"]
            if not settings["baseline_ready"] or settings["restore_hold"]:
                raise CleanerError("not_ready", "Включение доступно после сверки исходной базы и восстановления", 409)
        respond(202, operation())
    except CleanerError as exc:
        respond(exc.http_status, {"code": exc.code, "error": str(exc)})
    except (ValueError, TypeError, UnicodeError):
        respond(400, {"code": "invalid_request", "error": "Не удалось прочитать команду"})
    except (sqlite3.Error, OSError):
        respond(503, {"code": "storage_unavailable", "error": "Сохранённые данные временно недоступны"})
