(() => {
  "use strict";
  if (window.top === window.self && new URLSearchParams(window.location.search).get("embedded") !== "1") {
    window.location.replace("/sheet-vitrina-v1/vitrina?tab=finance");
    return;
  }
  const apiRoot = "/v1/finance";
  const operationReadbackDeadlineMs = 5000;
  const app = document.querySelector("[data-finance-app]");
  const $ = (selector, root = document) => root.querySelector(selector);
  const state = { capabilities: null, accounts: [], categories: [], counterparties: [], documents: [], reconciliations: [], auditEvents: [], csrf: "", inFlight: new Map() };
  const ui = { accounts: $("[data-accounts]"), accountsEmpty: $("[data-accounts-empty]"), history: $("[data-history]"), historyEmpty: $("[data-history-empty]"), reconciliations: $("[data-reconciliations]"), attention: $("[data-attention]"), settings: $("[data-settings]"), audit: $("[data-settings-audit]"), notice: $("[data-notice]"), error: $("[data-error]"), session: $("[data-session-state]"), dialog: $("[data-dialog]"), dialogTitle: $("[data-dialog-title]"), dialogKicker: $("[data-dialog-kicker]"), dialogContent: $("[data-dialog-content]"), dialogSubmit: $("[data-dialog-submit]"), dialogSaveDraft: $("[data-dialog-save-draft]") };
  const has = (grant) => Boolean(state.capabilities?.capabilities?.includes?.(grant) || state.capabilities?.grants?.includes?.(grant) || state.capabilities?.[grant]);
  const canRead = () => has("finance") || has("finance_operate") || has("finance_admin");
  const canOperate = () => has("finance_operate") || has("finance_admin");
  const canAdmin = () => has("finance_admin");
  function setText(node, value) { node.textContent = value == null ? "" : String(value); }
  function show(node, visible) { node.classList.toggle("is-hidden", !visible); }
  function notice(message = "") { setText(ui.notice, message); show(ui.notice, Boolean(message)); }
  function error(message = "") { setText(ui.error, message); show(ui.error, Boolean(message)); }
  function money(value, currency = "RUB") { if (typeof value !== "string" || !/^[+-]?\d+(\.\d+)?$/.test(value)) return "Нет данных"; const [raw, fraction = ""] = value.split("."); const sign = raw.startsWith("-") ? "−" : raw.startsWith("+") ? "+" : ""; const digits = raw.replace(/^[+-]/, "").replace(/^0+(?=\d)/, "") || "0"; const grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, " "); return `${sign}${grouped}${fraction ? `,${fraction.slice(0, 2).padEnd(2, "0")}` : ",00"} ${currency}`; }
  function operationId() { return crypto.randomUUID ? crypto.randomUUID() : `finance-${Date.now()}-${Math.random().toString(16).slice(2)}`; }
  const friendlyErrors = { authentication_required: "Войдите в рабочую сессию и повторите попытку.", finance_capability_denied: "Доступ к разделу не выдан.", csrf_failed: "Сессия обновилась. Перезагрузите страницу и повторите действие.", finance_read_disabled: "Раздел временно недоступен.", finance_write_disabled: "Операции пока недоступны.", finance_auth_unavailable: "Не удалось проверить доступ. Попробуйте позже.", finance_store_unavailable: "Данные финансов временно недоступны. Попробуйте позже.", finance_integrity_unavailable: "Не удалось безопасно проверить данные. Попробуйте позже.", ledger_integrity_unavailable: "Не удалось безопасно проверить данные. Попробуйте позже.", balance_projection_unavailable: "Не удалось рассчитать остаток. Попробуйте позже.", finance_unavailable: "Данные финансов временно недоступны. Попробуйте позже.", finance_schema_unavailable: "Данные финансов временно недоступны. Попробуйте позже.", finance_storage_busy: "Данные финансов временно заняты. Попробуйте позже.", business_data_maintenance: "Данные временно обслуживаются. Попробуйте позже.", invalid_money: "Проверьте сумму: используйте число с копейками.", account_uninitialized: "Сначала укажите начальный остаток для этого счёта.", reconciliation_explanation_required: "Опишите причину расхождения перед сохранением.", duplicate_confirmation_required: "Похожая операция уже есть.", duplicate_confirmation_stale: "Данные похожей операции изменились. Обновите страницу и примите решение ещё раз.", idempotency_conflict: "Эта операция уже была отправлена с другими данными. Обновите страницу." };
  function escapeError(payload) { const code = payload?.error?.code; return friendlyErrors[code] || payload?.error?.message || code || "Не удалось выполнить запрос. Попробуйте обновить страницу."; }
  function uncertainOperation(message, operation) {
    return Object.assign(new Error(message), { uncertain: true, operation });
  }
  async function request(path, { method = "GET", body, operation, timeoutMs = 0 } = {}) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) {
      state.inFlight.set(operation.id, operation);
      headers["Content-Type"] = "application/json";
      headers["X-Finance-CSRF"] = state.csrf;
      headers["Idempotency-Key"] = operation.key;
      headers["X-Operation-Id"] = operation.id;
    }
    let payload;
    let response;
    const controller = timeoutMs > 0 ? new AbortController() : null;
    const timeout = controller ? window.setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      try { response = await fetch(`${apiRoot}${path}`, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body), signal: controller?.signal }); }
      catch (networkError) { if (operation) throw uncertainOperation("Сеть не ответила. Операция не отправлена повторно; проверяем её результат.", operation); throw networkError; }
      try { payload = await response.json(); }
      catch {
        if (operation) throw uncertainOperation("Ответ на запись не удалось подтвердить. Новая отправка не выполняется; проверяем результат.", operation);
        throw new Error("Сервер вернул неожиданный ответ.");
      }
    } finally {
      if (timeout !== null) window.clearTimeout(timeout);
    }
    if (payload.contract !== "finance_cash_v1") {
      if (operation) throw uncertainOperation("Ответ на запись пришёл не в ожидаемом виде. Новая отправка не выполняется; проверяем результат.", operation);
      throw new Error("Получен ответ другого сервиса.");
    }
    state.inFlight.delete(operation?.id);
    if (!response.ok) throw Object.assign(new Error(escapeError(payload)), { status: response.status, code: payload?.error?.code, payload });
    return payload.data;
  }
  function dataList(data, key) { return Array.isArray(data) ? data : (data?.[key] || []); }
  function accountBalance(account) { return account.balance ?? account.current_balance ?? account.balance_amount ?? null; }
  function accountState(account) { return account.balance_state || account.initialization_state || "unknown"; }
  function accountLabel(account) { return [account.name, account.responsible_name].filter(Boolean).join(" · "); }
  function normalizeAccount(account) { return {...account, id: account.id || account.account_id}; }
  function normalizeCategory(category) { return {...category, id: category.id || category.category_id}; }
  function normalizeCounterparty(counterparty) { return {...counterparty, id: counterparty.id || counterparty.counterparty_id}; }
  function normalizeDocument(doc) { return {...doc, id: doc.id || doc.document_id}; }
  function normalizeReconciliation(rec) { return {...rec, id: rec.id || rec.reconciliation_id}; }
  function clear(node) { node.replaceChildren(); }
  function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text != null) node.textContent = text; return node; }
  function renderAccounts() {
    clear(ui.accounts); const selectable = $("[data-account-select]"); clear(selectable); selectable.append(new Option("Все счета", ""));
    for (const account of state.accounts.filter(item => item.is_active)) {
      selectable.append(new Option(accountLabel(account), account.id));
      const card = element("button", "account-card"); card.type = "button"; card.dataset.accountId = account.id; card.setAttribute("aria-label", `Открыть историю: ${accountLabel(account)}`);
      const head = element("div", "account-head"); const heading = element("div"); heading.append(element("div", "account-name", account.name), element("div", "account-meta", [account.account_type === "cash" ? "Касса" : "Счёт", account.responsible_name, account.currency].filter(Boolean).join(" · "))); head.append(heading);
      const status = accountState(account); head.append(element("span", status === "current" ? "pill" : "pill warn", status === "uninitialized" ? "Не задан" : status === "in_transit" ? "В пути" : "Актуально")); card.append(head);
      const value = accountBalance(account); const negative = account.negative_balance_warning || account.balance_state === "negative" || (typeof value === "string" && value.startsWith("-")); card.append(element("div", "balance", status === "uninitialized" ? "Не задан" : money(value, account.currency)), element("div", "balance-note", status === "uninitialized" ? "Укажите начальный остаток отдельной операцией" : negative ? "Отрицательный остаток · требуется разбор пояснения к расходу" : "Текущий остаток по данным сервера"));
      ui.accounts.append(card);
    }
    show(ui.accountsEmpty, state.accounts.filter(item => item.is_active).length === 0); renderActionAccess();
  }
  function isCorrection(doc) { return Boolean(doc.reversal_of_document_id || doc.reversal_of || doc.reversal_reason); }
  function documentTypeName(doc) { if (isCorrection(doc)) return doc.document_type === "transfer" ? "Отмена перевода" : "Исправление"; if (doc.funding_kind === "owner_funding") return "Пополнение собственными средствами"; return ({ opening: "Начальный остаток", income: "Поступление", expense: "Расход", transfer: "Перевод" })[doc.document_type || doc.type] || "Операция"; }
  function documentAccounts(doc) { const name = id => state.accounts.find(account => account.id === id)?.name || ""; const source = doc.source_account_name || name(doc.source_account_id); const target = doc.target_account_name || name(doc.target_account_id); return (isCorrection(doc) && doc.document_type === "transfer" ? [target, source] : [source, target, doc.account_name]).filter(Boolean).join(" → "); }
  function negateAmount(amount) { if (typeof amount !== "string" || !amount) return amount; if (/^[+-]?0(?:\.0+)?$/.test(amount)) return amount.replace(/^[+-]/, ""); if (amount.startsWith("-")) return `+${amount.slice(1)}`; if (amount.startsWith("+")) return `-${amount.slice(1)}`; return `-${amount}`; }
  function documentAmount(doc) { const amount = doc.amount || doc.amount_decimal || doc.total_amount; if (doc.document_type === "transfer") return amount; if (doc.document_type === "opening") return isCorrection(doc) ? negateAmount(amount) : amount; const expenseEffect = doc.document_type === "expense"; return isCorrection(doc) !== expenseEffect ? negateAmount(amount) : `+${amount}`; }
  function visibleDocuments() { const form = $("[data-history-filters]"); const filters = new FormData(form); return state.documents.filter(doc => (!filters.get("account_id") || doc.source_account_id === filters.get("account_id") || doc.target_account_id === filters.get("account_id")) && (!filters.get("status") || doc.status === filters.get("status")) && (!filters.get("type") || doc.document_type === filters.get("type"))); }
  function historyAction(label, attribute, value) {
    const button = element("button", "history-action", ` ${label}`);
    button.type = "button";
    button.dataset[attribute] = value;
    return button;
  }
  function renderHistory() {
    clear(ui.history);
    const visible = visibleDocuments();
    for (const doc of visible) {
      const row = element("article", "history-row");
      const main = element("div", "history-main");
      const inTransit = doc.transfer_state === "in_transit";
      const statusText = doc.status === "draft" ? "Черновик" : inTransit ? "В пути" : doc.status === "reversed" ? "Исправлено" : "Проведено";
      main.append(element("div", "history-title", documentTypeName(doc)));
      main.append(element("div", doc.status === "draft" ? "history-meta draft-badge" : "history-meta", [documentAccounts(doc), doc.occurred_at ? businessDateTime(doc.occurred_at) : "", statusText].filter(Boolean).join(" · ")));
      const purpose = isCorrection(doc) ? (doc.reversal_reason || doc.purpose?.replace(doc.document_type === "opening" ? /^Opening reversal:\s*/i : /^Reversal:\s*/i, doc.document_type === "opening" ? "Исправление начального остатка: " : "Исправление: ")) : doc.purpose;
      if (purpose) main.append(element("div", "history-meta", purpose));
      const categoryName = doc.category_name_snapshot || state.categories.find(item => item.id === doc.category_id)?.name;
      const counterpartyName = doc.counterparty_name_snapshot || state.counterparties.find(item => item.id === doc.counterparty_id)?.name;
      if (categoryName || counterpartyName) main.append(element("div", "history-meta", [categoryName, counterpartyName].filter(Boolean).join(" · ")));
      if (doc.negative_balance_explanation) main.append(element("div", "history-meta", `Пояснение: ${doc.negative_balance_explanation}`));
      if (doc.reversal_of_document_id) main.append(element("div", "history-meta", "Связано с исходной операцией"));
      if (doc.replaces_opening_document_id) main.append(element("div", "history-meta", "Заменяет прежний начальный остаток"));
      const right = element("div", "history-amount", money(documentAmount(doc), doc.currency || "RUB"));
      if (doc.status === "draft" && canOperate()) {
        right.append(historyAction("Изменить", "editDraft", doc.id), historyAction("Провести", "postDraft", doc.id));
      } else if (inTransit && canOperate()) {
        right.append(historyAction("Завершить", "transferTransition", `${doc.id}:complete`), historyAction("Отменить", "transferTransition", `${doc.id}:cancel`));
      } else if (doc.status === "posted" && doc.document_type === "opening" && !isCorrection(doc) && canAdmin()) {
        right.append(historyAction("Заменить", "replaceOpening", doc.id));
      } else if (doc.status === "posted" && canAdmin() && !isCorrection(doc)) {
        right.append(historyAction("Исправить", "reverse", doc.id));
      }
      row.append(main, right);
      ui.history.append(row);
    }
    show(ui.historyEmpty, visible.length === 0);
  }
  function renderReconciliations() {
    clear(ui.reconciliations);
    ui.reconciliations.append(element("h3", "", "Сверки"), element("p", "readonly", "Здесь остаются и совпадения, и расхождения. Сверка не меняет деньги."));
    if (!state.reconciliations.length) { ui.reconciliations.append(element("p", "readonly", "Сверок ещё нет.")); return; }
    for (const rec of state.reconciliations) {
      const account = state.accounts.find(item => item.id === rec.account_id);
      const stateLabel = rec.status === "matched" ? "Совпало" : rec.status === "resolved" ? "Разобрано" : "Есть расхождение";
      const row = element("article", `reconciliation-row ${rec.status === "matched" ? "" : "has-difference"}`);
      row.append(element("strong", "", `${businessDate(`${rec.week_ending}T18:59:59Z`)} · ${account?.name || "Касса"}`), element("div", "history-meta", stateLabel));
      const amounts = element("div", "reconciliation-amounts");
      amounts.append(element("span", "", `Расчётный: ${money(rec.expected_amount, rec.currency || account?.currency || "RUB")}`), element("span", "", `Фактический: ${money(rec.actual_amount, rec.currency || account?.currency || "RUB")}`), element("span", "", `Разница: ${money(rec.difference_amount, rec.currency || account?.currency || "RUB")}`));
      row.append(amounts);
      if (rec.checked_at) row.append(element("div", "history-meta", `Зафиксировано: ${businessDateTime(rec.checked_at)}`));
      if (rec.comment) row.append(element("div", "history-meta", rec.comment));
      ui.reconciliations.append(row);
    }
  }
  function renderAttention() { clear(ui.attention); const items = []; for (const account of state.accounts) { const status = accountState(account); if (status === "uninitialized") items.push(["Нужен начальный остаток", accountLabel(account), "warn"]); if (account.negative_balance_warning || account.balance_state === "negative") items.push(["Отрицательный остаток", accountLabel(account), "danger"]); if (status === "in_transit") items.push(["Перевод в пути", accountLabel(account), "warn"]); }
    for (const doc of state.documents) if (doc.transfer_state === "in_transit") items.push(["Перевод в пути", documentAccounts(doc) || "Счета", "warn"]);
    for (const rec of state.reconciliations) if (rec.status === "discrepancy" || Number(rec.difference_minor) !== 0) items.push(["Расхождение при сверке", rec.account_name || state.accounts.find(account => account.id === rec.account_id)?.name || "Касса", "warn"]);
    if (!items.length) { ui.attention.append(element("p", "readonly", "Важных замечаний нет.")); return; }
    for (const [title, detail, level] of items) { const item = element("div", "attention-item"); item.append(element("strong", level === "danger" ? "pill danger" : "pill warn", title), element("p", "", detail)); ui.attention.append(item); }
  }
  function renderActionAccess() { for (const button of document.querySelectorAll("[data-action]")) { const action = button.dataset.action; button.classList.toggle("is-hidden", ["new-cash","new-category","new-counterparty"].includes(action) ? !canAdmin() : action === "reload" ? false : !canOperate()); } $("[data-settings-link]").classList.toggle("is-hidden", !canAdmin()); show(ui.settings, canAdmin()); }
  function renderSettings() {
    for (const [kind, items] of [["accounts", state.accounts.filter(item => item.account_type === "cash")], ["categories", state.categories.filter(item => item.direction === "expense")], ["counterparties", state.counterparties]]) {
      const container = $(`[data-settings-${kind}]`); clear(container);
      if (!items.length) container.append(element("p", "readonly", "Пока пусто."));
      for (const item of items) {
        const row = element("div", "settings-entry"); row.append(element("strong", "", item.name));
        if (!item.is_active) row.append(element("div", "history-meta", "В архиве"));
        for (const [label, action] of [["Переименовать", "rename"], [item.is_active ? "В архив" : "Восстановить", item.is_active ? "archive" : "restore"], ["Удалить", "delete"]]) {
          const button = element("button", "history-action", label); button.type = "button"; button.dataset.directory = `${kind}:${item.id}:${action}`; row.append(button);
        }
        container.append(row);
      }
    }
    clear(ui.audit);
    if (!state.auditEvents.length) ui.audit.append(element("p", "readonly", "Изменений пока нет."));
    for (const event of state.auditEvents) {
      const [kind, action] = String(event.event_type || "").split(".");
      const noun = {account:"Касса",accounts:"Касса",category:"Статья",categories:"Статья",counterparty:"Контрагент",counterparties:"Контрагент"}[kind] || "Запись";
      const change = {seeded:"Начальное добавление",created:"Добавление",rename:"Переименование",archive:"Перенос в архив",restore:"Восстановление",delete:"Удаление"}[action] || "Изменение";
      let details = {}; try { details = JSON.parse(event.payload_json || "{}"); } catch { /* A legacy digest-only event remains readable by type and time. */ }
      const oldName = String(details.old_name || details.name || "").trim();
      const newName = String(details.new_name || "").trim();
      const label = action === "rename" && oldName && newName ? `${noun}: «${oldName}» → «${newName}»` : `${noun}: ${change}${oldName ? ` «${oldName}»` : ""}`;
      const row = element("div", "audit-entry");
      row.append(element("strong", "", label), element("span", "history-meta", `${businessDateTime(event.created_at)} · ${event.actor === "system" ? "Система" : event.actor || "Пользователь"}`));
      ui.audit.append(row);
    }
  }
  async function loadAll() { error(); notice(); ui.session.textContent = "Обновляем данные…"; try { const caps = await request("/capabilities"); state.capabilities = caps; state.csrf = caps.csrf_token || ""; if (!canRead()) { ui.session.textContent = "Нет доступа к финансам"; error("Доступ к разделу не выдан. Обратитесь к администратору."); renderActionAccess(); return; }
      const [accounts, categories, counterparties, docs, reconciliations, audit] = await Promise.all([request("/accounts"), request("/categories"), request("/counterparties"), request("/documents"), request("/cash-reconciliations"), canAdmin() ? request("/audit?scope=directories") : Promise.resolve({events:[]})]); state.accounts = dataList(accounts, "accounts").map(normalizeAccount); state.categories = dataList(categories, "categories").map(normalizeCategory); state.counterparties = dataList(counterparties, "counterparties").map(normalizeCounterparty); state.documents = dataList(docs, "documents").map(normalizeDocument); state.reconciliations = dataList(reconciliations, "reconciliations").map(normalizeReconciliation); state.auditEvents = dataList(audit, "events"); ui.session.textContent = canOperate() ? "Операции доступны" : "Только просмотр"; renderAccounts(); renderHistory(); renderReconciliations(); renderAttention(); renderSettings(); }
    catch (caught) { ui.session.textContent = "Данные недоступны"; error(caught.code === "finance_capability_denied" ? "Доступ к разделу не выдан." : caught.message); renderActionAccess(); }
  }
  function field(label, name, options = {}) {
    const { type = "text", required = false, value = "", placeholder = "", help, full = false } = options;
    const wrapper = element("label", `field${full ? " full" : ""}`);
    wrapper.append(element("span", "", label));
    let control;
    if (options.options) {
      control = document.createElement("select");
      for (const [text, optionValue] of options.options) control.append(new Option(text, optionValue));
    } else if (type === "textarea") {
      control = document.createElement("textarea");
    } else {
      control = document.createElement("input");
      control.type = type;
    }
    control.name = name;
    control.required = required;
    if (!options.options || value) control.value = value;
    control.placeholder = placeholder;
    wrapper.append(control);
    if (help) wrapper.append(element("span", "help", help));
    return wrapper;
  }
  function accountOptions({ cashOnly = false, initialized = false } = {}) { return [["Выберите счёт", ""], ...state.accounts.filter(a => a.is_active && (!cashOnly || a.account_type === "cash") && (!initialized || accountState(a) !== "uninitialized")).map(a => [accountLabel(a), a.id])]; }
  function categoryOptions(direction) { return [["Выберите статью", ""], ...state.categories.filter(c => c.is_active && c.direction === direction).map(c => [c.name, c.id])]; }
  function counterpartyOptions() { return [["Без контрагента", ""], ...state.counterparties.filter(c => c.is_active).map(c => [c.name, c.id])]; }
  const businessParts = value => Object.fromEntries(new Intl.DateTimeFormat("en-CA", {timeZone:"Asia/Yekaterinburg", year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hourCycle:"h23"}).formatToParts(new Date(value)).filter(part => part.type !== "literal").map(part => [part.type,part.value]));
  function dateValue(value = Date.now()) { const part = businessParts(value); return `${part.year}-${part.month}-${part.day}T${part.hour}:${part.minute}`; }
  function businessDayValue(value = Date.now()) { const part = businessParts(value); return `${part.year}-${part.month}-${part.day}`; }
  function businessDate(value) { const part = businessParts(value); return `${part.day}.${part.month}.${part.year}`; }
  function businessDateTime(value) { const part = businessParts(value); return `${part.day}.${part.month}.${part.year} ${part.hour}:${part.minute} ЕКТ`; }
  function toBusinessUtc(value) { if (!value) return value; const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value); if (!match) return value; const [, year, month, day, hour, minute] = match; return new Date(Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour) - 5, Number(minute))).toISOString(); }
  function buildDialog(kind, source) { const form = $("[data-dialog-form]"); const documentKind = kind === "edit-draft" ? source.document_type : kind; form.reset(); clear(ui.dialogContent); ui.dialogContent.dataset.kind = kind; ui.dialogContent.dataset.documentKind = documentKind; ui.dialogContent.dataset.sourceId = source?.id || ""; ui.dialogKicker.textContent = kind === "edit-draft" ? "Черновик" : "Финансы";
    const grid = element("div", "dialog-grid"); const submit = ui.dialogSubmit; submit.disabled = false; ui.dialogSaveDraft.classList.add("is-hidden"); ui.dialogContent.dataset.submitMode = "post";
    if (kind === "new-cash") { ui.dialogTitle.textContent = "Новая касса"; grid.append(field("Название", "name", { required:true, placeholder:"Например, Касса офиса" }), field("Ответственный", "responsible_name", { required:true, placeholder:"Кто отвечает за кассу" }), field("Валюта", "currency", { options:[["Рубли (RUB)","RUB"]], help:"Другие валюты появятся в отдельном этапе." })); submit.textContent = "Создать кассу"; }
    if (kind === "new-category") { ui.dialogTitle.textContent = "Новая статья расходов"; grid.append(field("Название", "name", {required:true}), field("Назначение", "analytic_class", {options:[["Текущие расходы", "operating_expense"],["Личное изъятие", "owner_draw"],["Распределение прибыли", "profit_distribution"],["Платёж по долгу (без разнесения)", "debt_service_unallocated"]],help:"Назначение сохраняется с проведённой операцией."})); submit.textContent = "Добавить статью"; }
    if (kind === "new-counterparty") { ui.dialogTitle.textContent = "Новый контрагент"; grid.append(field("Имя или название", "name", {required:true,full:true})); submit.textContent = "Добавить контрагента"; }
    if (kind === "directory") { ui.dialogTitle.textContent = ({rename:"Переименовать",archive:"В архив",restore:"Восстановить",delete:"Удалить"})[source.action]; ui.dialogContent.dataset.directoryKind = source.kind; ui.dialogContent.dataset.directoryAction = source.action; if (source.action === "rename") grid.append(field("Новое название", "name", {required:true,value:source.item.name,full:true})); else ui.dialogContent.append(element("div", "warning-box", source.action === "delete" ? "Удаление возможно только если запись не используется, в том числе в черновиках. История денежных операций сохранится." : `${source.item.name}: подтвердите действие.`)); submit.textContent = ui.dialogTitle.textContent; }
    if (kind === "common-opening") { ui.dialogTitle.textContent = "Общий старт трёх касс"; grid.append(field("Одна дата и время для всех", "occurred_at", {required:true,type:"datetime-local",help:"Выберите согласованную дату. Суммы не подставляются автоматически.",full:true})); for (const account of state.accounts.filter(item => item.code && item.account_type === "cash")) grid.append(field(account.name, `opening_${account.id}`, {required:true,placeholder:"0,00"})); grid.append(field("Основание", "opening_evidence_ref", {type:"textarea",full:true})); ui.dialogContent.append(element("div", "warning-box", "Будут подготовлены три черновика с одной датой. Деньги изменятся только после отдельного проведения каждого.")); submit.textContent = "Подготовить черновики"; }
    if (documentKind === "opening") { ui.dialogTitle.textContent = kind === "edit-draft" ? "Изменить черновик" : "Начальный остаток"; grid.append(field("Счёт", "target_account_id", { required:true, options:accountOptions() }), field("Дата и время (Екатеринбург)", "occurred_at", { required:true,type:"datetime-local",value:dateValue() }), field("Сумма", "amount", { required:true, placeholder:"0,00", help:"Укажите сумму явно, в том числе ноль." }), field("Комментарий", "opening_evidence_ref", {type:"textarea",placeholder:"Например, остаток пересчитан вместе с ответственным",help:"Основание отмечается как подтверждённое вручную.",full:true})); submit.textContent = kind === "edit-draft" ? "Сохранить изменения" : "Провести"; if (kind !== "edit-draft") ui.dialogSaveDraft.classList.remove("is-hidden"); }
    if (documentKind === "income" || documentKind === "expense" || documentKind === "funding") { const isExpense = documentKind === "expense"; const isFunding = documentKind === "funding" || source?.funding_kind === "owner_funding"; ui.dialogTitle.textContent = kind === "edit-draft" ? "Изменить черновик" : isExpense ? "Расход" : isFunding ? "Пополнить кассу" : "Поступление"; grid.append(field(isExpense ? "Касса списания" : "Касса пополнения", isExpense ? "source_account_id" : "target_account_id", {required:true,options:accountOptions({initialized:true})})); if (!isFunding) grid.append(field("Статья", "category_id", {required:true,options:categoryOptions(isExpense ? "expense" : "income")})); grid.append(field("Дата и время (Екатеринбург)", "occurred_at", {required:true,type:"datetime-local",value:dateValue()}), field("Сумма", "amount", {required:true,placeholder:"0,00"})); if (!isFunding) grid.append(field("Контрагент", "counterparty_id", {options:counterpartyOptions(),help:"Необязательно."}), field("Новый контрагент", "new_counterparty_name", {placeholder:"Имя или название, если его ещё нет",help:"Добавится прямо при сохранении операции.",full:true})); grid.append(field("Комментарий", "purpose", {type:"textarea",full:true,help:isFunding ? "Внешние собственные деньги; выручка не создаётся." : "Для статьи «Прочие расходы» обязателен."})); if (isExpense) grid.append(field("Почему остаток может стать отрицательным", "negative_balance_explanation", {type:"textarea",full:true,help:"Заполняется только при предупреждении сервера."})); submit.textContent = kind === "edit-draft" ? "Сохранить изменения" : "Провести"; if (kind !== "edit-draft") ui.dialogSaveDraft.classList.remove("is-hidden"); }
    if (documentKind === "transfer") { ui.dialogTitle.textContent = kind === "edit-draft" ? "Изменить черновик" : "Перевод"; grid.append(field("Откуда", "source_account_id", {required:true,options:accountOptions({initialized:true})}), field("Куда", "target_account_id", {required:true,options:accountOptions({initialized:true})}), field("Дата и время (Екатеринбург)", "occurred_at", {required:true,type:"datetime-local",value:dateValue()}), field("Сумма", "amount", {required:true,placeholder:"0,00"}), field("Комментарий", "purpose", {type:"textarea",full:true}), field("Режим", "transfer_mode", {options:[["Сразу провести", "instant"],["В пути", "two_phase"]], help:"В пути можно полностью завершить или отменить тому же оператору."}), field("Почему остаток может стать отрицательным", "negative_balance_explanation", {type:"textarea",full:true,help:"Заполните после предупреждения сервера. Это пояснение к тому же переводу."})); submit.textContent = kind === "edit-draft" ? "Сохранить изменения" : "Провести"; if (kind !== "edit-draft") ui.dialogSaveDraft.classList.remove("is-hidden"); }
    if (kind === "reconcile") { ui.dialogTitle.textContent = "Сверка кассы"; grid.append(field("Касса", "account_id", {required:true,options:accountOptions({cashOnly:true})}), field("Дата сверки", "week_ending", {required:true,type:"date",value:businessDayValue(),help:"Остаток рассчитывается на конец этого дня по Екатеринбургу (UTC+5)."}), field("Фактический остаток", "actual_amount", {required:true,placeholder:"0,00"}), field("Комментарий", "comment", {type:"textarea",placeholder:"При расхождении укажите причину",help:"При совпадении можно оставить пустым.",full:true})); ui.dialogContent.append(element("div", "warning-box", "Сверка сохранит расхождение на конец выбранного дня и не создаст движение денег.")); submit.textContent = "Зафиксировать сверку"; }
    if (kind === "transfer-transition") { const isCancel = source.transition === "cancel"; ui.dialogTitle.textContent = isCancel ? "Отменить перевод" : "Завершить перевод"; grid.append(field(isCancel ? "Дата отмены (Екатеринбург)" : "Дата завершения (Екатеринбург)", "occurred_at", {required:true,type:"datetime-local",value:dateValue()}), field("Комментарий", "purpose", {type:"textarea",full:true})); ui.dialogContent.dataset.transition = source.transition; submit.textContent = isCancel ? "Отменить перевод" : "Завершить перевод"; }
    if (kind === "reverse") { ui.dialogTitle.textContent = "Исправление операции"; grid.append(field("Дата исправления", "occurred_at", {required:true,type:"datetime-local",value:dateValue()}), field("Причина исправления", "reason", {required:true,type:"textarea",full:true,help:"Будет создана связанная обратная операция; исходная останется в истории."})); submit.textContent = "Создать исправление"; }
    if (kind === "replace-opening") { ui.dialogTitle.textContent = "Заменить начальный остаток"; grid.append(field("Дата и время (Екатеринбург)", "occurred_at", {required:true,type:"datetime-local",value:dateValue()}), field("Новая сумма", "amount", {required:true,placeholder:"0,00",help:"Новый остаток заменит прежний через сохранённую корректировку."}), field("Почему исправляем", "reason", {required:true,type:"textarea",placeholder:"Уточнили остаток после пересчёта",full:true})); submit.textContent = "Заменить"; }
    ui.dialogContent.prepend(grid);
    if (kind === "edit-draft") {
      for (const [name, value] of Object.entries(source)) {
        const control = $(`[name="${name}"]`, ui.dialogContent);
        if (control && value != null) control.value = name === "amount" ? value : String(value);
      }
      const amount = $(`[name="amount"]`, ui.dialogContent);
      if (amount && source.amount) amount.value = source.amount;
      const occurred = $(`[name="occurred_at"]`, ui.dialogContent);
      if (occurred && source.occurred_at) occurred.value = dateValue(source.occurred_at);
    }
    updateCommentRequirement(); ui.dialog.showModal(); const first = $("input,select,textarea", ui.dialogContent); first?.focus();
  }
  function updateCommentRequirement() { const category = $("[name='category_id']", ui.dialogContent); const comment = $("[name='purpose']", ui.dialogContent); if (category && comment) comment.required = Boolean(state.categories.find(item => item.id === category.value)?.requires_comment); }
  function normalizeValue(name, value) { if (name === "occurred_at") return toBusinessUtc(value); if (name === "amount" || name === "actual_amount") return value.trim().replace(",", "."); return value.trim(); }
  function formData(form) { const body = {}; for (const [name,value] of new FormData(form).entries()) if (value !== "") body[name] = normalizeValue(name, String(value)); return body; }
  function operation() { return { id: operationId(), key: operationId() }; }
  async function readUncertainOperation(op) {
    state.inFlight.set(op.id, op);
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        const result = await request(`/operations/${encodeURIComponent(op.id)}`, {timeoutMs:operationReadbackDeadlineMs});
        if (result) { state.inFlight.delete(op.id); await loadAll(); if (result.action_required || result.status === "action_required") notice("Запрос сохранён и требует вашего решения. Деньги пока не изменены."); else notice("Результат операции подтверждён."); return true; }
      } catch (caught) { if (caught.status && caught.status !== 404) break; }
      await new Promise(resolve => window.setTimeout(resolve, 500));
    }
    error("Связь прервалась. Новую операцию не создавали; обновьте страницу, чтобы проверить результат.");
    return false;
  }
  async function createDraft(body) { const draft = await request("/documents", {method:"POST",body,operation:operation()}); await loadAll(); notice("Черновик сохранён. Проведите его из истории после проверки."); return draft; }
  async function attachCounterparty(body) {
    const name = String(body.new_counterparty_name || "").trim(); delete body.new_counterparty_name;
    if (!name) return body;
    const existing = state.counterparties.find(item => item.is_active && item.name.toLocaleLowerCase("ru") === name.toLocaleLowerCase("ru"));
    const created = existing || await request("/counterparties", {method:"POST",body:{name},operation:operation()});
    body.counterparty_id = created.id || created.counterparty_id;
    return body;
  }
  async function submitDialog(event) { if (event.submitter?.value === "cancel") { ui.dialog.close(); return; } event.preventDefault(); const form = event.currentTarget; const kind = ui.dialogContent.dataset.kind; const sourceId = ui.dialogContent.dataset.sourceId; const comment = $("[name='purpose']", ui.dialogContent); if (comment?.required && !comment.value.trim()) { error("Для этой статьи напишите комментарий."); comment.focus(); return; } const body = formData(form); const submit = ui.dialogSubmit; let successMessage = ""; submit.disabled = true; error(); try { if (kind === "new-cash") { await request("/accounts", {method:"POST",body:{...body,account_type:"cash",currency:"RUB"},operation:operation()}); successMessage = "Касса создана. Укажите начальный остаток отдельной операцией."; }
      else if (kind === "new-category") { await request("/categories", {method:"POST",body:{...body,direction:"expense",posting_class:"external_outflow"},operation:operation()}); successMessage = "Статья добавлена."; }
      else if (kind === "new-counterparty") { await request("/counterparties", {method:"POST",body,operation:operation()}); successMessage = "Контрагент добавлен."; }
      else if (kind === "directory") { const directoryKind = ui.dialogContent.dataset.directoryKind; const item = ({accounts:state.accounts,categories:state.categories,counterparties:state.counterparties})[directoryKind].find(entry => entry.id === sourceId); await request(`/directories/${directoryKind}/${encodeURIComponent(sourceId)}`, {method:"POST",body:{...body,action:ui.dialogContent.dataset.directoryAction,base_revision:item.revision},operation:operation()}); successMessage = "Изменение сохранено."; }
      else if (kind === "common-opening") { const amounts = {}; for (const [name,value] of Object.entries(body)) if (name.startsWith("opening_")) { amounts[name.slice(8)] = value.replace(",", "."); delete body[name]; } await request("/openings/common-draft", {method:"POST",body:{...body,amounts},operation:operation()}); successMessage = "Три черновика с общей датой подготовлены. Проверьте и проведите каждый."; }
      else if (kind === "reconcile") { await request("/cash-reconciliations", {method:"POST",body,operation:operation()}); successMessage = "Сверка сохранена. Остаток по ней не изменён."; }
      else if (kind === "edit-draft") { const draft = state.documents.find(item => item.id === sourceId); await attachCounterparty(body); await request(`/documents/${encodeURIComponent(sourceId)}`, {method:"PATCH",body:{...body,base_revision:draft?.revision},operation:operation()}); successMessage = "Черновик обновлён. Проверьте его и проведите, когда всё готово."; }
      else if (kind === "reverse") { const doc = state.documents.find(item => item.id === sourceId); await request(`/documents/${encodeURIComponent(sourceId)}/reverse`, {method:"POST",body:{...body,base_revision:doc?.revision ?? doc?.base_revision},operation:operation()}); successMessage = "Исправление создано. Исходная операция сохранена в истории."; }
      else if (kind === "replace-opening") { const doc = state.documents.find(item => item.id === sourceId); await request(`/documents/${encodeURIComponent(sourceId)}/replace-opening`, {method:"POST",body:{...body,base_revision:doc?.revision,opening_evidence_type:"manual_confirmation"},operation:operation()}); successMessage = "Начальный остаток заменён. Прежний факт сохранён в истории."; }
      else if (kind === "transfer-transition") { const doc = state.documents.find(item => item.id === sourceId); await request(`/transfers/${encodeURIComponent(sourceId)}/${ui.dialogContent.dataset.transition}`, {method:"POST",body:{base_revision:doc?.revision ?? doc?.base_revision, ...body},operation:operation()}); successMessage = ui.dialogContent.dataset.transition === "cancel" ? "Перевод отменён." : "Перевод завершён."; }
      else { await attachCounterparty(body); const draft = await createDraft({document_type:kind === "funding" ? "income" : kind,...body, ...(kind === "opening" ? {opening_evidence_type:"manual_confirmation"} : {}), ...(kind === "funding" ? {funding_kind:"owner_funding"} : {})}); if (ui.dialogContent.dataset.submitMode === "post") await postDraft(draft.document_id); ui.dialog.close(); return; } await loadAll(); notice(successMessage); ui.dialog.close(); }
    catch (caught) { if (caught.code === "negative_balance_explanation_required" || /negative cash/i.test(caught.message)) { error("Для этого расхода добавьте пояснение к отрицательному остатку."); } else if (caught.code === "version_conflict") { error("Данные изменились у другого пользователя. Форма сохранена; обновите данные и проверьте её ещё раз."); await loadAll(); } else if (caught.uncertain) { await readUncertainOperation(caught.operation); } else { error(caught.message); } submit.disabled = false; }
  }
  async function postDraft(id, duplicateToken) { const doc = state.documents.find(d => d.id === id); if (!doc) return; const op = operation(); try { const body = {base_revision:doc.revision ?? doc.base_revision}; if (duplicateToken) body.duplicate_confirmation_token = duplicateToken; await request(`/documents/${encodeURIComponent(id)}/post`, {method:"POST",body,operation:op}); await loadAll(); notice("Операция проведена и зафиксирована."); }
    catch (caught) { if (caught.code === "duplicate_confirmation_required") { const token = caught.payload?.error?.duplicate_confirmation_token || caught.payload?.data?.duplicate_confirmation_token; if (token && window.confirm("Похожая операция уже есть. Провести эту операцию всё равно?")) return postDraft(id, token); } if (caught.uncertain) await readUncertainOperation(caught.operation); else if (caught.code === "negative_balance_explanation_required" || /negative cash/i.test(caught.message)) error("Для этой операции добавьте пояснение к отрицательному остатку."); else error(caught.message); } }
  function actionAllowed(kind) { return ["new-cash","new-category","new-counterparty"].includes(kind) ? canAdmin() : canOperate(); }
  document.addEventListener("click", (event) => { if (event.target.closest("[data-dialog] [value='cancel']")) { ui.dialog.close(); return; } const action = event.target.closest("[data-action]")?.dataset.action; if (action) { if (action === "reload") loadAll(); else if (actionAllowed(action)) buildDialog(action); return; } if (event.target.closest("[data-dialog-save-draft]")) { ui.dialogContent.dataset.submitMode = "draft"; $("[data-dialog-form]").requestSubmit(); return; } const directory = event.target.closest("[data-directory]"); if (directory && canAdmin()) { const [kind,id,actionName] = directory.dataset.directory.split(":"); const item = ({accounts:state.accounts,categories:state.categories,counterparties:state.counterparties})[kind].find(entry => entry.id === id); buildDialog("directory", {id,kind,action:actionName,item}); return; } const account = event.target.closest("[data-account-id]"); if (account) { const select = $("[data-account-select]"); select.value = account.dataset.accountId; $("[data-history-filters]").requestSubmit(); return; } const edit = event.target.closest("[data-edit-draft]"); if (edit) { buildDialog("edit-draft", state.documents.find(d => d.id === edit.dataset.editDraft)); return; } const post = event.target.closest("[data-post-draft]"); if (post) postDraft(post.dataset.postDraft); const transition = event.target.closest("[data-transfer-transition]"); if (transition) { const [id, actionName] = transition.dataset.transferTransition.split(":"); buildDialog("transfer-transition", {id,transition:actionName}); return; } const replace = event.target.closest("[data-replace-opening]"); if (replace) { buildDialog("replace-opening", state.documents.find(d => d.id === replace.dataset.replaceOpening)); return; } const reverse = event.target.closest("[data-reverse]"); if (reverse) buildDialog("reverse", state.documents.find(d => d.id === reverse.dataset.reverse)); });
  ui.dialogContent.addEventListener("change", updateCommentRequirement);
  $("[data-dialog-form]").addEventListener("submit", submitDialog); $("[data-history-filters]").addEventListener("submit", async (event) => { event.preventDefault(); renderHistory(); });
  if (!app) return; loadAll();
})();
