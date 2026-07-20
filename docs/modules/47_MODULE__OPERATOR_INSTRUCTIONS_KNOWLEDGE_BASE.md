---
title: "Модуль: operator_instructions_knowledge_base"
doc_id: "WB-CORE-MODULE-47-OPERATOR-INSTRUCTIONS-KNOWLEDGE-BASE"
doc_type: "module"
status: "active"
purpose: "Зафиксировать внутренний repo-owned справочник WebCore: системный раздел «Инструкции», server-owned capability, revisions, реестр обновлений и первую web-native инструкцию «Ведение поставок»."
scope: "Операторский shell, защищённый HTML route, типизированные Git-tracked content/update registries и управление отдельным доступом через «Настройки → Пользователи». Это не CMS, не public document hosting и не изменение логики поставок/ФФ."
source_basis:
  - "packages/application/operator_instructions.py"
  - "packages/application/operator_instruction_models.py"
  - "packages/application/operator_instruction_registry.py"
  - "packages/application/operator_instruction_updates.py"
  - "packages/application/operator_instruction_content/supply_management.py"
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
  - "apps/operator_instruction_registry_smoke.py"
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
update_note: "Инструкции и нормализованная история их изменений публикуются из типизированных Git-tracked Python records и доступны только после server-side capability check; исходные аудио/транскрипции/DOCX не являются runtime source или downloadable surface."
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

Content разделён по устойчивым ролям:

- `operator_instruction_models.py` владеет типами instruction/section/block/update;
- каждая статья живёт отдельным structured content record под `operator_instruction_content/`;
- `operator_instruction_registry.py` владеет порядком опубликованных статей и совместимыми `list_operator_instructions()` / `get_operator_instruction()`;
- `operator_instruction_updates.py` владеет append-only-in-practice нормализованной историей изменений и NEW semantics;
- `operator_instructions.py` остаётся compatibility facade, а не монолитным content-файлом.

Каждая инструкция имеет стабильный `instruction_id` и положительный integer `revision`; каждая section — стабильный anchor, каждый block — стабильный глобально уникальный `block_id`. Registry validation fail-closed проверяет ids, anchors, block kinds, table shape и update references при импорте и отдельным smoke.

Adding a second instruction is a registry entry plus its structured sections; it does not require a new route, DB migration, CMS, browser editor or dynamically opened filesystem path.

The HTTP adapter renders fixed semantic HTML and escapes every content/update/badge field. Block ids становятся точными DOM ids; update links указывают на exact section/block DOM id. No raw HTML, Markdown HTML passthrough, user content, path-derived content or runtime database content is rendered. The browser owns only anchor navigation, current-topic highlighting, mobile disclosure and iframe height; it owns neither authorization, business date nor instruction truth.

Desktop uses a compact left list/topic navigation and a readable bounded article width. Narrow screens use a disclosure navigation; native links, headings, focus-visible styles and print CSS remain available.

# 4. Revisions, update registry and `NEW`

Update registry содержит только нормализованные записи: стабильный `update_id`, canonical publication date, `instruction_id`, revision, понятное summary, затронутые section/block ids, source type, exact link target и optional revisit condition. Raw transcription/audio, имена сотрудников, разговорные фрагменты и Git-технический журнал в него не попадают. Записи добавляются в tuple по неубывающей дате, а UI показывает их от новых к старым; история остаётся видимой после истечения `NEW`.

Revision растёт при опубликованном изменении статьи. Старые update records могут сохранять прежнюю revision, но не могут ссылаться на будущую; latest registered revision должна совпадать с current instruction revision. Неизвестные instructions/sections/blocks, некорректная дата или stale/future latest revision блокируют registry validation.

`NEW` рассчитывается server-side по canonical business date `Asia/Yekaterinburg`: publication date и следующие 29 календарных дней активны, на `published_on + 30 days` badge исчезает. Cookies, localStorage и персонального acknowledgement нет. Новый section получает badge в article/topic navigation и section heading; blocks, добавленные той же update-записью вместе с новым section, не получают дублирующие дочерние badges. Если более поздняя update-запись добавляет block внутрь section, чей прежний section-level `NEW` ещё активен, новый block получает собственный badge, а parent topic и вся instruction продолжают отражать новый материал. `NEW` — текстовая screen-reader-доступная метка, а не только цвет.

Компактный disclosure `Обновления инструкций` открыт автоматически, пока есть активные новые элементы, и остаётся доступным в collapsed state после их истечения. Он показывает дату, статью, summary и exact anchor link на desktop/narrow viewport.

# 5. Published instruction: «Ведение поставок»

The first record is a practical reference for:

- manager responsibility and boundaries;
- invoice-based supplier-order lookup and guarded factual dates;
- current server checklist for logistics documents;
- operations currently outside the manager boundary;
- `Поставки → Расчёты → Поставка на Wildberries`: динамический подбор складов отдельно по направлениям, актуальный rank `#1` / `Рекомендуемый склад`, обязательная проверка кабинета WB и escalation при расхождении;
- `Поставки → ФФ → Услуги ФФ`, including exact WB supply ids, `STORAGE`, validation and the system payment visa;
- final verification and escalation cases.

Current revision: `3`. Update registry содержит две последовательные записи с business date `2026-07-20` и source type `owner_audio_instruction`: revision `2` добавляет section `wb-warehouse-selection`, а revision `3` добавляет в него exact block `wb-warehouse-selection-exact-composition` с правилом сразу указывать полный фактический список SKU, точное количество каждого SKU и правильное общее количество поставки WB. Временная подстановка всего количества в одну условную SKU запрещена. Формулировка явно фиксирует, что система использует этот состав далее при синхронизации и обработке движения товара — для учёта перемещения, списания остатков ФФ и расчёта себестоимости, а не утверждает немедленное списание в момент создания поставки.

Warehouse-selection wording follows current operator UI labels and warehouse-planning behavior, but intentionally never lists named primary/reserve warehouses. Состав, доступность, priority и dates принадлежат актуальному WebCore result и кабинету WB; Git-tracked operational instruction cannot duplicate the mutable warehouse registry or replace current selection with a memorized list. System quantities пока не заменяют Excel allocation, полученное от руководителя.

The rest of the wording follows the live supplier shipment/factual-date-correction and fulfillment-services contracts. It never changes those business semantics and does not claim that a factual date can be freely overwritten.

# 6. Non-scope

This module does not implement:

- a CMS, browser editor, comments or personal acknowledgement workflow;
- database content/version history or arbitrary document upload;
- PDF/DOCX publication, conversion or download;
- Google Sheets/GAS integration;
- supplier access by default;
- changes to supplier shipments, factual-date correction, financial documents or fulfillment-services validation.
- changes to supply calculation, warehouse ranking, warehouse registry, WB API, supply creation/booking or production business data.

# 7. Verification

`apps/operator_instruction_registry_smoke.py` covers unique/stable ids, section/block references, sequential revisions/dates, exact-composition wording, active/future/expired 30-day semantics with an injected date, whole-section inheritance without child duplication, a later block badge inside a still-new section, exact update targets, exact UI-label drift guards, current warehouse-registry name exclusion and escaped update/badge content.

`apps/operator_instructions_smoke.py` covers admin/capability/denied/supplier access, direct-route forbidden behavior, user capability persistence and revocation, controlled `400/404`, lack of DOCX route, escaped renderer output, shell placement, exact update-to-block navigation, article/topic/section badges plus the later block's own badge, keyboard-operable navigation and desktop/narrow overflow checks. Its browser NEW assertions patch a fixed business date and therefore do not expire with wall-clock time.

Existing auth/users/public-route smokes cover the shared session, runtime user update and managed nginx allowlist regression contours.
