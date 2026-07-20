"""Targeted auth, content and responsive-shell smoke for operator instructions."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from http.cookiejar import CookieJar
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from unittest.mock import patch
from urllib import error as urllib_error, parse as urllib_parse, request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_INSTRUCTIONS_UI_PATH,
    DEFAULT_SETTINGS_USERS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    InstructionBlock,
    _render_operator_instruction_block,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    admin_username = "owner"
    admin_password = "instructions-admin-password"
    allowed_password = "instructions-allowed-password"
    denied_password = "instructions-denied-password"
    supplier_password = "instructions-supplier-password"
    with TemporaryDirectory(prefix="webcore-instructions-smoke-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
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
        with _patched_env(
            {
                "WB_CORE_WEB_AUTH_REQUIRED": "1",
                "WB_CORE_WEB_AUTH_USERNAME": admin_username,
                "WB_CORE_WEB_AUTH_PASSWORD_HASH": _password_hash(admin_password),
                "WB_CORE_WEB_AUTH_SESSION_SECRET": "instructions-smoke-session-secret",
                "WB_CORE_SUPPLIER_AUTH_USERNAME": "supplier-user",
                "WB_CORE_SUPPLIER_AUTH_PASSWORD_HASH": _password_hash(supplier_password),
            }
        ), patch(
            "packages.adapters.registry_upload_http_entrypoint.current_business_date_iso",
            return_value="2026-07-20",
        ):
            server = build_registry_upload_http_server(
                config,
                entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{config.port}"
                admin = _opener()
                admin_code, admin_body = _login(admin, base_url, admin_username, admin_password, DEFAULT_INSTRUCTIONS_UI_PATH)
                _assert(admin_code == 200, "admin must open instructions shell")
                _assert('"initial_tab": "instructions"' in admin_body, "direct instructions route must select system section")
                _assert('data-unified-tab-button="instructions"' in admin_body, "shell must include instructions system action")
                _assert(
                    admin_body.index('data-unified-tab-button="instructions"')
                    < admin_body.index('data-unified-tab-button="settings"')
                    < admin_body.index('data-logout-link'),
                    "instructions must sit beside settings and logout in the right system actions",
                )
                embedded_code, embedded_body = _opener_text(
                    admin,
                    f"{base_url}{DEFAULT_INSTRUCTIONS_UI_PATH}?embedded=1",
                )
                _assert(embedded_code == 200, "admin must open embedded instructions content")
                for marker in (
                    "Ведение поставок",
                    'id="role"',
                    'id="find-shipment"',
                    'id="shipment-dates"',
                    'id="documents"',
                    'id="wb-warehouse-selection"',
                    'href="#wb-warehouse-selection"',
                    'id="fulfillment-services"',
                    'href="#fulfillment-services"',
                    "Счёт ФФ передаётся на оплату только после успешной загрузки расчёта",
                    "STORAGE",
                    "Обновления инструкций",
                    "Подбор складов WB по направлениям",
                    "Рекомендуемый склад",
                    'id="wb-warehouse-selection-exact-composition"',
                    "Точный состав поставки",
                    "полный фактический список SKU",
                    "точное количество каждого SKU",
                ):
                    _assert(marker in embedded_body, f"instruction content marker missing: {marker}")
                _assert("Инструкция_менеджера_по_поставкам" not in embedded_body, "source DOCX must not be published")
                unknown_code, unknown_body = _opener_text(
                    admin,
                    f"{base_url}{DEFAULT_INSTRUCTIONS_UI_PATH}?instruction=missing",
                )
                _assert(unknown_code == 404 and "Инструкция не найдена" in unknown_body, "unknown instruction id must be controlled 404")
                repeated_code, repeated_body = _opener_text(
                    admin,
                    f"{base_url}{DEFAULT_INSTRUCTIONS_UI_PATH}?instruction=supply-management&instruction=missing",
                )
                _assert(repeated_code == 400 and "Некорректный идентификатор инструкции" in repeated_body, "repeated instruction id must be controlled 400")
                docx_code, _ = _opener_text(
                    admin,
                    f"{base_url}/%D0%98%D0%BD%D1%81%D1%82%D1%80%D1%83%D0%BA%D1%86%D0%B8%D1%8F_%D0%BC%D0%B5%D0%BD%D0%B5%D0%B4%D0%B6%D0%B5%D1%80%D0%B0.docx",
                )
                _assert(docx_code == 404, "DOCX source must not be reachable as a public runtime file")

                denied = _create_user(
                    admin,
                    base_url,
                    "instructions-denied",
                    denied_password,
                    ["vitrina"],
                )
                allowed = _create_user(
                    admin,
                    base_url,
                    "instructions-allowed",
                    allowed_password,
                    ["vitrina", "instructions"],
                )
                allowed_id = str(allowed["user"]["user_id"])
                _assert(allowed["user"]["allowed_sections"] == ["vitrina", "instructions"], "create must persist instructions capability")
                users_code, users_payload = _opener_json(admin, f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}")
                _assert(users_code == 200, "admin users read must work")
                section_ids = [item.get("section_id") for item in users_payload.get("available_sections", []) if isinstance(item, dict)]
                _assert("instructions" in section_ids, "users read model must expose instructions capability")

                allowed_user = _opener()
                allowed_code, allowed_body = _login(
                    allowed_user,
                    base_url,
                    "instructions-allowed",
                    allowed_password,
                    DEFAULT_INSTRUCTIONS_UI_PATH,
                )
                _assert(allowed_code == 200 and '"instructions"' in allowed_body, "capability user must see instructions shell action")
                allowed_embedded_code, allowed_embedded = _opener_text(
                    allowed_user,
                    f"{base_url}{DEFAULT_INSTRUCTIONS_UI_PATH}?embedded=1",
                )
                _assert(allowed_embedded_code == 200 and "Ведение поставок" in allowed_embedded, "capability user must receive content")

                denied_user = _opener()
                denied_code, denied_body = _login(
                    denied_user,
                    base_url,
                    "instructions-denied",
                    denied_password,
                    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                )
                _assert(denied_code == 200 and '"instructions"' not in _allowed_tabs_json(denied_body), "denied user shell must omit instructions capability")
                denied_route_code, denied_route_body = _opener_text(denied_user, f"{base_url}{DEFAULT_INSTRUCTIONS_UI_PATH}")
                _assert(
                    denied_route_code == 403
                    and "Ведение поставок" not in denied_route_body
                    and "Подбор складов WB по направлениям" not in denied_route_body,
                    "denied user direct route must be controlled forbidden without content leakage",
                )

                supplier = _opener()
                supplier_code, _ = _login(supplier, base_url, "supplier-user", supplier_password, DEFAULT_INSTRUCTIONS_UI_PATH)
                _assert(supplier_code == 200, "supplier login must complete through its allowed fallback")
                supplier_route_code, supplier_route_body = _opener_text(supplier, f"{base_url}{DEFAULT_INSTRUCTIONS_UI_PATH}")
                _assert(
                    supplier_route_code == 403
                    and "Ведение поставок" not in supplier_route_body
                    and "Подбор складов WB по направлениям" not in supplier_route_body,
                    "supplier-only user must not receive internal instructions",
                )

                patched = _opener_patch_json(
                    admin,
                    f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}/{urllib_parse.quote(allowed_id)}",
                    {"allowed_sections": ["vitrina"]},
                )
                _assert(
                    patched[0] == 200 and patched[1]["user"]["allowed_sections"] == ["vitrina"],
                    "disabling instructions must persist without losing remaining access",
                )
                reread_code, reread_payload = _opener_json(admin, f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}")
                reread = next((row for row in reread_payload.get("users", []) if row.get("user_id") == allowed_id), {})
                _assert(reread_code == 200 and reread.get("allowed_sections") == ["vitrina"], "users reread must return saved capability state")
                revoked_code, revoked_body = _opener_text(allowed_user, f"{base_url}{DEFAULT_INSTRUCTIONS_UI_PATH}")
                _assert(
                    revoked_code == 403
                    and "Ведение поставок" not in revoked_body
                    and "Подбор складов WB по направлениям" not in revoked_body,
                    "session recheck must revoke route after normal update",
                )

                _assert_browser_ui(base_url, admin_username, admin_password)
                _assert_safe_renderer()
                _assert(denied.get("user", {}).get("allowed_sections") == ["vitrina"], "denied test user must remain without capability")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    print("operator_instructions_smoke: OK")


def _assert_browser_ui(base_url: str, username: str, password: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            page.set_default_timeout(8_000)
            page.goto(f"{base_url}/login?next={urllib_parse.quote(DEFAULT_INSTRUCTIONS_UI_PATH)}", wait_until="domcontentloaded")
            page.fill('input[name="username"]', username)
            page.fill('input[name="password"]', password)
            page.click('button[type="submit"]')
            page.wait_for_selector('[data-unified-tab-button="instructions"]:not([hidden])')
            page.locator('[data-unified-tab-button="instructions"]').click()
            frame = page.frame_locator('[data-instructions-embed-frame]')
            frame.locator("#wb-warehouse-selection").wait_for()
            if "Ведение поставок" not in frame.locator("article").inner_text():
                raise AssertionError("instructions iframe must show web-native article content")
            if page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"):
                raise AssertionError("desktop shell must not overflow horizontally")
            if frame.locator("html").evaluate("element => element.scrollWidth > element.clientWidth"):
                raise AssertionError("desktop instructions content must not overflow horizontally")
            if frame.locator(".instruction-updates").get_attribute("open") is None:
                raise AssertionError("active instruction updates must open automatically")
            if frame.locator('.instruction-link[aria-current="page"] .new-badge').count() != 1:
                raise AssertionError("active article navigation must show one NEW badge")
            if frame.locator(".article-header .new-badge").count() != 1:
                raise AssertionError("active article heading must show one NEW badge")
            if frame.locator('.knowledge-sidebar > .topic-nav a[href="#wb-warehouse-selection"] .new-badge').count() != 1:
                raise AssertionError("desktop topic navigation must show NEW")
            if frame.locator("#wb-warehouse-selection > h2 .new-badge").count() != 1:
                raise AssertionError("new section heading must show NEW")
            exact_block = frame.locator("#wb-warehouse-selection > #wb-warehouse-selection-exact-composition")
            if exact_block.count() != 1:
                raise AssertionError("exact composition block must render inside the warehouse-selection section")
            if exact_block.locator(":scope > h3 .new-badge").count() != 1:
                raise AssertionError("later exact-composition block must show its own NEW badge")
            if frame.locator("#wb-warehouse-selection .block .new-badge").count() != 1:
                raise AssertionError("only the later block may duplicate the still-active parent section NEW")
            if frame.locator('[data-update-id="supply-management-r3-exact-wb-supply-composition"]').count() != 1:
                raise AssertionError("revision 3 update must appear once in the update registry")
            update_link = frame.locator('.instruction-update-link[href$="#wb-warehouse-selection-exact-composition"]')
            if update_link.count() != 1:
                raise AssertionError("revision 3 update registry item must link to the exact new block")
            update_link.click()
            exact_block.wait_for()
            frame.locator("body").wait_for()
            if not frame.locator("body").evaluate("() => window.location.hash === '#wb-warehouse-selection-exact-composition'"):
                raise AssertionError("update registry navigation must land on the exact block DOM id")
            desktop_link = frame.locator('.knowledge-sidebar > .topic-nav a[href="#documents"]')
            desktop_link.focus()
            desktop_link.press("Enter")
            page.wait_for_timeout(150)
            if desktop_link.get_attribute("aria-current") != "true":
                current_hash = frame.locator("body").evaluate("() => window.location.hash")
                current_value = desktop_link.get_attribute("aria-current")
                raise AssertionError(
                    "instruction topic links must be keyboard-operable and mark the current section: "
                    f"hash={current_hash!r}, aria-current={current_value!r}"
                )
            if ":focus-visible" not in frame.locator("style").inner_text():
                raise AssertionError("instruction topic links must define a visible keyboard focus state")
            page.set_viewport_size({"width": 390, "height": 844})
            if not frame.locator(".topics-mobile").is_visible():
                raise AssertionError("mobile instructions navigation must become a compact disclosure")
            if frame.locator(".knowledge-sidebar > .topic-nav").is_visible():
                raise AssertionError("desktop topic navigation must not stay expanded on mobile")
            if page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"):
                raise AssertionError("mobile shell must not overflow horizontally")
            if frame.locator("html").evaluate("element => element.scrollWidth > element.clientWidth"):
                raise AssertionError("mobile instructions content must not overflow horizontally")
            frame.locator(".topics-mobile summary").click()
            if not frame.locator('.topics-mobile a[href="#wb-warehouse-selection"] .new-badge').is_visible():
                raise AssertionError("mobile topic disclosure must show NEW for the new section")
            exact_block.scroll_into_view_if_needed()
            if not exact_block.is_visible() or not exact_block.locator(":scope > h3 .new-badge").is_visible():
                raise AssertionError("exact composition block and its own NEW must remain visible on mobile")
            if not frame.locator(".instruction-update-item").first.is_visible():
                raise AssertionError("instruction updates must remain readable on narrow viewport")
        finally:
            browser.close()


def _assert_safe_renderer() -> None:
    rendered = _render_operator_instruction_block(
        InstructionBlock(block_id="safe-renderer-test", kind="important", title="<img src=x>", text="<script>alert(1)</script>")
    )
    _assert("<script>" not in rendered and "&lt;script&gt;" in rendered, "instruction renderer must escape dangerous HTML")
    _assert("<img" not in rendered and "&lt;img" in rendered, "instruction renderer must not inject title HTML")


def _create_user(opener: urllib_request.OpenerDirector, base_url: str, username: str, password: str, sections: list[str]) -> dict[str, object]:
    code, payload = _opener_post_json(
        opener,
        f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}",
        {
            "username": username,
            "display_name": username,
            "password": password,
            "allowed_sections": sections,
            "manage_users": False,
            "is_active": True,
        },
    )
    _assert(code == 201, f"user create failed: {code} {payload}")
    return payload


def _allowed_tabs_json(body: str) -> str:
    marker = '"allowed_tabs": '
    start = body.find(marker)
    return body[start : start + 240] if start >= 0 else ""


def _opener() -> urllib_request.OpenerDirector:
    return urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))


def _login(opener: urllib_request.OpenerDirector, base_url: str, username: str, password: str, next_path: str) -> tuple[int, str]:
    body = urllib_parse.urlencode({"username": username, "password": password, "next": next_path}).encode("utf-8")
    request = urllib_request.Request(
        f"{base_url}/login",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return _open_text(opener, request)


def _opener_text(opener: urllib_request.OpenerDirector, url: str) -> tuple[int, str]:
    return _open_text(opener, urllib_request.Request(url, headers={"Accept": "text/html"}, method="GET"))


def _open_text(opener: urllib_request.OpenerDirector, request: urllib_request.Request) -> tuple[int, str]:
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _opener_json(opener: urllib_request.OpenerDirector, url: str) -> tuple[int, dict[str, object]]:
    return _open_json(opener, urllib_request.Request(url, headers={"Accept": "application/json"}, method="GET"))


def _opener_post_json(opener: urllib_request.OpenerDirector, url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    return _open_json(
        opener,
        urllib_request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
            method="POST",
        ),
    )


def _opener_patch_json(opener: urllib_request.OpenerDirector, url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    return _open_json(
        opener,
        urllib_request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
            method="PATCH",
        ),
    )


def _open_json(opener: urllib_request.OpenerDirector, request: urllib_request.Request) -> tuple[int, dict[str, object]]:
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@contextmanager
def _patched_env(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _password_hash(password: str) -> str:
    salt = b"instructions-smoke-static-salt"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return "pbkdf2_sha256$260000$" + _b64(salt) + "$" + _b64(digest)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
