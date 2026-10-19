/* Supplier source action recovery. The shared component only presents receipts. */
(function () {
  "use strict";
  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
    return value;
  }
  async function digest(text) {
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return "sha256:" + Array.from(new Uint8Array(bytes), item => item.toString(16).padStart(2, "0")).join("");
  }
  async function boundedRequest(url, options, timeoutMs, consume) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try { return await consume(await fetch(url, Object.assign({}, options, {signal: controller.signal}))); }
    finally { window.clearTimeout(timer); }
  }
  function create(options) {
    const key = "wbc_supplier_source_pending_v1:" + options.scope;
    const container = options.container;
    let pending = null;
    let readSequence = 0;
    function samePending(expected) {
      const current = JSON.parse(localStorage.getItem(key) || "null");
      return current && ["request_id", "action", "entity_id", "payload_digest"].every(name => current[name] === expected[name]);
    }
    function load() {
      const raw = localStorage.getItem(key);
      pending = raw ? JSON.parse(raw) : null;
      if (pending && (!pending.request_id || !pending.action || !pending.payload_digest)) throw new Error("Не удалось прочитать сохранённый ID операции.");
      options.onLock(Boolean(pending));
    }
    function persist(value) {
      // Failure to retain the identity stops submission before a native write.
      if (value) localStorage.setItem(key, JSON.stringify(value)); else localStorage.removeItem(key);
      pending = value;
      options.onLock(Boolean(pending));
    }
    function unknown() {
      container.hidden = false;
      window.OperatorAcceptance.renderState(container, null);
      const button = document.createElement("button");
      button.type = "button"; button.textContent = "Проверить статус";
      button.addEventListener("click", () => {button.disabled = true; read(true).catch(() => {if (pending) unknown();});});
      container.appendChild(button);
    }
    function present(result) {
      container.hidden = false;
      window.OperatorAcceptance.renderReceipt(container, result.acceptance, {onClose: () => {container.hidden = true;}});
      if (result.domain === "supplier_financial_document" && Array.isArray(result.results)) {
        const list = document.createElement("ul");
        for (const child of result.results) {
          const item = document.createElement("li");
          item.textContent = (child.document_id || "Документ") + ": " + (child.status === "accepted" ? "сохранён" + (child.acceptance && child.acceptance.reason_ru ? ". " + child.acceptance.reason_ru : "") : child.status === "preview" ? "ожидает подтверждения позиций" : child.error || "не сохранён");
          list.appendChild(item);
        }
        container.appendChild(list);
      }
      const replacement = result.acceptance.processing && result.acceptance.processing.superseded_by;
      if (replacement && (replacement.domain === "supplier_shipment" && /^supplier_[a-f0-9]{32}$/.test(replacement.operation_id) || replacement.domain === "supplier_financial_document" && /^supplier_financial_[a-f0-9]{32}$/.test(replacement.operation_id))) {
        const link = document.createElement("a");
        link.textContent = "Следующая операция";
        link.href = "/sheet-vitrina-v1/supplier?operation_id=" + encodeURIComponent(replacement.operation_id);
        container.appendChild(link);
      }
      if (result.acceptance.processing && result.acceptance.processing.kind === "source_only") {
        const state = container.querySelector(".ff-operation-status");
        if (state) state.textContent = "Сохранение завершено. Расчёт не требуется.";
      }
    }
    async function showOperation(operationId) {
      const domain = /^supplier_financial_(?:batch_)?[a-f0-9]{32}$/.test(operationId) ? "supplier_financial_document" : /^ssfc_job_[a-f0-9]{32}$/.test(operationId) ? "supplier_factual_date"
        : /^supplier_[a-f0-9]{32}$/.test(operationId) ? "supplier_shipment" : "";
      if (!domain) return;
      const sequence = ++readSequence;
      const result = await boundedRequest(options.path + "?operation_id=" + encodeURIComponent(operationId), {headers: {Accept: "application/json"}}, 5000,
        async response => response.ok ? await response.json() : null);
      if (sequence !== readSequence || localStorage.getItem(key)) return;
      if (!result || result.domain !== domain
          || !result.acceptance || result.acceptance.domain !== domain || !result.acceptance.source_ref
          || !result.shipment || result.shipment.shipment_id !== result.acceptance.source_ref.entity_id
          || (domain === "supplier_factual_date" && result.acceptance.source_ref.native_id !== operationId)
          || (await window.OperatorAcceptance.readSameOperation({operation_id: operationId, domain}, () => Promise.resolve(result))).status !== "accepted") {
        container.hidden = false;
        window.OperatorAcceptance.renderState(container, null);
        return;
      }
      // Known saved-operation detail is GET-only. It does not replace an
      // editable card with an older source revision or clear a pending action.
      if (sequence !== readSequence || localStorage.getItem(key)) return;
      present(result);
    }
    async function read(recover, submitted) {
      load();
      if (!pending) return null;
      if (submitted && !samePending(submitted)) return null;
      const expected = pending;
      const sequence = ++readSequence;
      const currentRead = () => sequence === readSequence && samePending(expected);
      const financial = expected.action.startsWith("financial_");
      const domain = financial ? "supplier_financial_document" : expected.action === "factual_date" ? "supplier_factual_date" : "supplier_shipment";
      const readPath = expected.action === "factual_date"
        ? options.path + "/" + encodeURIComponent(expected.entity_id) + "/factual-dates/status"
        : financial ? options.path + "/" + encodeURIComponent(expected.entity_id) + "/financial-documents" : options.path;
      let result;
      try {
        result = await boundedRequest(readPath + "?request_id=" + encodeURIComponent(expected.request_id), {headers: {Accept: "application/json"}}, 5000,
          async response => {if (!response.ok) throw new Error("Проверка недоступна"); return await response.json();});
        if (!currentRead()) return null;
        if (result.domain === domain && result.request_id === expected.request_id
            && result.action === expected.action && (result.wire_digest || result.payload_digest) === expected.payload_digest
            && result.status === "rejected" && result.acceptance === null) {
          const current = JSON.parse(localStorage.getItem(key) || "null");
          if (current && current.request_id === expected.request_id) persist(null); else load();
          container.hidden = true;
          container.replaceChildren();
          throw {nativeRejected: true, message: result.error || "Заказ не принят."};
        }
        if (financial && result.domain === domain && result.request_id === expected.request_id
            && result.action === expected.action && result.wire_digest === expected.payload_digest
            && result.status === "preview" && result.acceptance === null && result.settled === true) {
          const current = JSON.parse(localStorage.getItem(key) || "null");
          if (current && current.request_id === expected.request_id) persist(null); else load();
          container.hidden = false;
          container.replaceChildren();
          const message = document.createElement("p");message.textContent = "Документ ожидает подтверждения позиций.";container.appendChild(message);
          return result;
        }
        if (financial && result.settled !== true) throw new Error("Сохранение пакета ещё выполняется");
        const receipt = result.acceptance;
        if (result.domain !== domain || result.request_id !== expected.request_id
            || result.action !== expected.action || (result.wire_digest || result.payload_digest) !== expected.payload_digest
            || !window.OperatorAcceptance.acceptedOperation(receipt) || receipt.domain !== result.domain
            || !receipt.source_ref || receipt.source_ref.action !== expected.action
            || (expected.action === "factual_date" && receipt.source_ref.native_id !== receipt.operation_id)
            || (expected.entity_id && receipt.source_ref.entity_id !== expected.entity_id)
            || !result.shipment || result.shipment.shipment_id !== receipt.source_ref.entity_id) throw new Error("Подтверждение той же операции ещё не получено");
        const exact = await window.OperatorAcceptance.readSameOperation({operation_id: receipt.operation_id, domain: result.domain}, () => Promise.resolve(result));
        if (exact.status !== "accepted") throw new Error("Не удалось проверить ID операции");
      } catch (error) {
        if (error && error.nativeRejected) throw new Error(error.message);
        if (!currentRead()) return null;
        unknown();
        throw new Error("Проверяем сохранение. Повторно отправлять заказ не нужно.");
      }
      // Another tab may have completed and begun a new action during this GET.
      if (!currentRead()) return null;
      persist(null);
      present(result);
      if (financial) {
        if (recover && options.onFinancialRecovered) await options.onFinancialRecovered(result);
        return result;
      }
      const shipment = Object.assign({}, result.shipment, {acceptance: result.acceptance});
      if (recover) await options.onRecovered(shipment);
      return shipment;
    }
    function classify(url, method) {
      const path = new URL(url, location.origin).pathname;
      if (path === options.path && method === "POST") return {action: "create", entity_id: ""};
      if (!path.startsWith(options.path + "/")) return null;
      const rest = path.slice(options.path.length + 1).split("/");
      if (rest[1] === "financial-documents") {
        const action = rest.length === 3 && rest[2] === "confirm-upload" && method === "POST" ? "financial_confirm_upload"
          : rest.length === 4 && rest[3] === "confirm-import" && method === "POST" ? "financial_confirm_import"
          : rest.length === 4 && rest[3] === "delete-confirm" && method === "POST" ? "financial_exclude"
          : rest.length === 3 && method === "PATCH" ? "financial_status" : "";
        if (action) return {action,entity_id:decodeURIComponent(rest[0])};
      }
      if (rest.length === 5 && rest[1] === "documents" && rest[2] === "payments" && rest[4] === "zero-fee" && method === "POST") return {action:"financial_zero_fee",entity_id:decodeURIComponent(rest[0])};
      if (rest.length === 3 && rest[1] === "factual-dates" && rest[2] === "confirm" && method === "POST") return {action: "factual_date", entity_id: decodeURIComponent(rest[0])};
      if (rest.length === 1 && (method === "PATCH" || method === "DELETE")) return {action: method === "DELETE" ? "archive" : "edit", entity_id: decodeURIComponent(rest[0])};
      const action = {"rematch": "rematch", "price-check": "price_check", "expense-completeness": "completeness"}[rest[1]];
      return rest.length === 2 && action && (method === "POST" || method === "PATCH") ? {action, entity_id: decodeURIComponent(rest[0])} : null;
    }
    async function submit(url, requestOptions, action) {
      if (!navigator.locks) throw new Error("Для безопасного сохранения нужен браузер с Web Locks.");
      return navigator.locks.request(key, async () => {
        load();
        if (pending) {unknown(); throw new Error("Сначала проверьте сохранение предыдущей операции.");}
        const payload = requestOptions.body ? JSON.parse(requestOptions.body) : {};
        delete payload.request_id;
        delete payload.operator_wire_json;
        const request_id = "supplier_" + crypto.randomUUID().replaceAll("-", "");
        // Retain only the digest. The server independently binds the exact
        // wire text to its native semantic operands before storing its hash.
        const wire = JSON.stringify(canonical(payload));
        const submitted = Object.assign({request_id, payload_digest: await digest(wire)}, action);
        persist(submitted);
        const body = Object.assign({}, payload, {request_id, operator_wire_json: wire});
        const target = requestOptions.method === "DELETE" ? url + "?request_id=" + encodeURIComponent(request_id) : url;
        const financial = action.action.startsWith("financial_");
        const headers = new Headers(requestOptions.headers || {});
        if (financial) headers.set("X-Request-ID", request_id);
        let response, refusal;
        // Aborting the transport never proves that the native write stopped.
        try {
          await boundedRequest(target, Object.assign({}, requestOptions, {headers}, requestOptions.method === "DELETE" ? {} : {body: JSON.stringify(body)}), 8000,
            async result => {response = result; if (financial && result.status === 423) {try {refusal = await result.json();} catch (_) {}}});
        } catch (_) {}
        // The existing global admission gate returns this exact ID before a
        // business writer can run. A lost response still follows unknown GET.
        if (financial && response && response.status === 423) {
          if (samePending(submitted) && refusal && refusal.code === "business_data_maintenance" && refusal.status === "blocked" && refusal.request_id === request_id) {
            const current = JSON.parse(localStorage.getItem(key) || "null");
            if (current && current.request_id === request_id) persist(null); else load();
            container.hidden = false;container.replaceChildren();
            const message = document.createElement("p");message.textContent = refusal.message || "Документ не сохранён: режим обслуживания.";container.appendChild(message);
            throw new Error(message.textContent);
          }
        }
        return read(false, submitted);
      });
    }
    window.addEventListener("storage", event => {if (event.key === key) {try {load(); if (pending) unknown();} catch (_) {options.onLock(true); unknown();}}});
    // Initial/new-tab recovery is GET only, including a closed previous tab.
    Promise.resolve().then(() => {load(); if (pending) return read(true);
      const identity = new URL(location.href).searchParams.get("operation_id");
      if (identity) return showOperation(identity);
    }).catch(() => {if (pending) {options.onLock(true); unknown();} else {container.hidden = false; window.OperatorAcceptance.renderState(container, null);}});
    return {classify, submit, locked: () => Boolean(pending)};
  }
  window.SupplierSourceAcceptance = Object.freeze({create});
}());
