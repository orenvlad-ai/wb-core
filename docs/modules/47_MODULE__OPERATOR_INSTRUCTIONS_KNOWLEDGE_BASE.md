---
title: "Модуль: operator_instructions_knowledge_base"
doc_id: "WB-CORE-MODULE-47-OPERATOR-INSTRUCTIONS-KNOWLEDGE-BASE"
doc_type: "module"
status: "active"
purpose: "Зафиксировать внутренний repo-owned справочник WebCore: системный раздел «Инструкции», server-owned capability и первую web-native инструкцию «Ведение поставок»."
scope: "Операторский shell, защищённый HTML route, статический структурированный registry контента и управление отдельным доступом через «Настройки → Пользователи». Это не CMS, не public document hosting и не изменение логики поставок/ФФ."
source_basis:
  - "packages/application/operator_instructions.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_instructions.html"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/39_MODULE__FULFILLMENT_SERVICES_BLOCK.md"
related_modules:
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/application/operator_instructions.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
related_tables:
  - "sheet_vitrina_v1_users (allowed_sections_json only; no knowledge-base content table)"
related_endpoints:
  - "GET /sheet-vitrina-v1/instructions"
  - "GET /sheet-vitrina-v1/instructions?embedded=1"
  - "GET/POST/PATCH/DELETE /v1/sheet-vitrina-v1/settings/users"
related_runners:
  - "apps/operator_instructions_smoke.py"
  - "apps/registry_upload_http_entrypoint_users_admin_smoke.py"
  - "apps/registry_upload_http_entrypoint_auth_smoke.py"
  - "apps/registry_upload_http_entrypoint_supplier_auth_smoke.py"
  - "apps/registry_upload_http_entrypoint_public_routes_smoke.py"
related_docs:
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/39_MODULE__FULFILLMENT_SERVICES_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Инструкции публикуются из Git-tracked structured Python registry и доступны только после server-side capability check; исходный DOCX служил источником операционного текста, но не runtime source и не downloadable surface."
---

# 1. Current contract

`Инструкции` — системный раздел общего operator shell. Он расположен в правой системной группе рядом с `Настройки` и `Выйти`, а не среди основных рабочих вкладок.

Canonical route:

- `GET /sheet-vitrina-v1/instructions` — common shell с выбранным системным разделом;
- `GET /sheet-vitrina-v1/instructions?embedded=1` — same-origin web-native content для внутренней shell-панели;
- `instruction=<id>` выбирает опубликованную инструкцию; неизвестный или повторённый id даёт controlled `404`/`400` до рендеринга контента.

Nginx публикует только exact protected route через repo-owned `public_route_allowlist.json`. Route не является public: nginx publication не заменяет app-level session/capability guard.

# 2. Access boundary

Capability id: `instructions`, label: `Инструкции`.

- Source of access truth — `sheet_vitrina_v1_users.allowed_sections_json` и env-admin semantic, а не browser/localStorage.
- Admin получает capability через текущую административную семантику.
- Existing и newly role-default non-admin operator records не получают capability автоматически; администратор включает его явно в `Настройки → Пользователи`.
- `Настройки → Пользователи` returns the capability in `available_sections`, stores it through the existing create/patch flow and keeps all other selected capabilities intact.
- Session lookup rereads active runtime user state on every protected request, so normal user-data/session refresh applies an enable/disable without client-side capability override.
- User without `instructions` does not receive the shell action after normal client access filtering and gets the existing controlled forbidden response on the direct HTML route. Supplier-only access is denied by the same server guard.
- The full instruction body is rendered only inside the authorized route; neither the generic shell config nor an unauthorized response contains the article content.

# 3. Content model and rendering

`packages/application/operator_instructions.py` is the canonical, append-only-in-practice registry of `OperatorInstruction` records. Each record has an id, title, summary, anchorable sections and renderer-owned semantic blocks (`numbered`, `checklist`, callouts and compact tables).

Adding a second instruction is a registry entry plus its structured sections; it does not require a new route, DB migration, CMS, browser editor or dynamically opened filesystem path.

The HTTP adapter renders fixed semantic HTML and escapes every text field. No raw HTML, Markdown HTML passthrough, user content, path-derived content or runtime database content is rendered. The browser owns only anchor navigation, current-topic highlighting, mobile disclosure and iframe height; it owns neither authorization nor instruction truth.

Desktop uses a compact left list/topic navigation and a readable bounded article width. Narrow screens use a disclosure navigation; native links, headings, focus-visible styles and print CSS remain available.

# 4. Published instruction: «Ведение поставок»

The first record is a practical reference for:

- manager responsibility and boundaries;
- invoice-based supplier-order lookup and guarded factual dates;
- current server checklist for logistics documents;
- operations currently outside the manager boundary;
- `Поставки → ФФ → Услуги ФФ`, including exact WB supply ids, `STORAGE`, validation and the system payment visa;
- final verification and escalation cases.

Its wording follows the live supplier shipment/factual-date-correction and fulfillment-services contracts. It never changes those business semantics and does not claim that a factual date can be freely overwritten.

# 5. Non-scope

This module does not implement:

- a CMS, browser editor, comments, acknowledgement or testing workflow;
- database content/version history or arbitrary document upload;
- PDF/DOCX publication, conversion or download;
- Google Sheets/GAS integration;
- supplier access by default;
- changes to supplier shipments, factual-date correction, financial documents or fulfillment-services validation.

# 6. Verification

`apps/operator_instructions_smoke.py` covers admin/capability/denied/supplier access, direct-route forbidden behavior, user capability persistence and revocation, missing instruction id, lack of DOCX route, escaped renderer output, shell placement, content anchors, keyboard-operable navigation and desktop/mobile overflow checks.

Existing auth/users/public-route smokes cover the shared session, runtime user update and managed nginx allowlist regression contours.
