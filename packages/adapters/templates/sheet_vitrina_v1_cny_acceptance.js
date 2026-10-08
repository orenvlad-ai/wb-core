/* Direct CNY source: persistent opaque alias, one submit, exact GET recovery. */
(function () {
  "use strict";
  function canonical(v) {
    if (Array.isArray(v)) return v.map(canonical);
    return v && typeof v === "object" ? Object.fromEntries(Object.keys(v).sort().map(k => [k, canonical(v[k])])) : v;
  }
  async function hash(text) {
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return "sha256:" + Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
  }
  function create(options) {
    const key = "wbc_cny_source_pending_v1:" + options.scope;
    const box = options.container;
    let pending;
    function load() {
      pending = JSON.parse(localStorage.getItem(key) || "null");
      if (pending && (!pending.request_id || !pending.action || !pending.digest)) throw new Error("Не удалось прочитать ID операции CNY.");
      options.onLock(Boolean(pending));
    }
    function persist(value) {
      if (value) localStorage.setItem(key, JSON.stringify(value)); else localStorage.removeItem(key);
      load();
    }
    function clear(expected) {
      load();
      if (pending && pending.request_id === expected.request_id) persist(null);
    }
    function unknown() {
      box.hidden = false;
      window.OperatorAcceptance.renderState(box, null);
      const check = document.createElement("button");
      check.type = "button"; check.textContent = "Проверить статус";
      check.addEventListener("click", () => recover().catch(options.onError));
      box.appendChild(check);
    }
    function present(result) {
      box.hidden = false;
      window.OperatorAcceptance.renderReceipt(box, result.acceptance, {onClose: () => {box.hidden = true;}});
      const replacement = result.acceptance.processing && result.acceptance.processing.superseded_by;
      if (replacement && ["cny_account_document", "supplier_financial_document"].includes(replacement.domain)
          && /^supplier_financial_(?:batch_)?[a-f0-9]{32}$/.test(replacement.operation_id)) {
        const link = document.createElement("a"); link.textContent = "Следующая операция";
        link.href = (replacement.domain === "cny_account_document" ? "/sheet-vitrina-v1/operator?embedded_tab=factory-order&operation_id=" : "/sheet-vitrina-v1/supplier?operation_id=") + encodeURIComponent(replacement.operation_id);
        box.appendChild(link);
      }
      for (const child of result.acceptance.children || []) {
        const detail = document.createElement("p");
        detail.textContent = child.financial_applied === true ? "Финансовая обработка этой версии подтверждена."
          : child.financial_applied === false ? "Финансовая обработка требует внимания." : "Финансовая обработка ожидает подтверждения.";
        box.appendChild(detail);
      }
    }
    async function read(expected) {
      const response = await fetch(options.path + "?request_id=" + encodeURIComponent(expected.request_id), {headers: {Accept: "application/json"}});
      const result = response.ok ? await response.json() : null;
      if (!result || result.domain !== "cny_account_document" || result.request_id !== expected.request_id
          || result.action !== "financial_" + expected.action || result.wire_digest !== expected.digest || result.settled !== true) {
        unknown(); throw new Error("Проверяем сохранение. Повторно отправлять документ не нужно.");
      }
      if (result.status === "rejected" && !result.acceptance) {
        clear(expected); box.hidden = true; throw new Error(result.error || "Документ не сохранён.");
      }
      if (result.status === "preview" && !result.acceptance) {
        clear(expected); box.hidden = true; return result;
      }
      if (!window.OperatorAcceptance.acceptedOperation(result.acceptance)
          || result.acceptance.domain !== result.domain || result.acceptance.source_ref.action !== result.action
          || (expected.document_id && !(result.acceptance.children || []).some(c => c.source_ref.document_id === expected.document_id))
          || (await window.OperatorAcceptance.readSameOperation({operation_id: result.acceptance.operation_id, domain: result.domain}, () => Promise.resolve(result))).status !== "accepted") {
        unknown(); throw new Error("Подтверждение этой операции пока не получено.");
      }
      clear(expected); present(result); await options.onRecovered(result); return result;
    }
    async function recover() {
      load(); if (!pending) return null;
      try {return await read(pending);} catch (error) {load(); if (pending) unknown(); throw error;}
    }
    async function mutate(url, action, operands, method, file, documentId) {
      if (!navigator.locks) throw new Error("Браузер не поддерживает безопасное сохранение CNY.");
      return navigator.locks.request(key, async () => {
        load(); if (pending) return recover();
        const wire = JSON.stringify(canonical(operands));
        const expected = {request_id: "cny_" + crypto.randomUUID().replaceAll("-", ""), action, digest: await hash(wire)};
        if (documentId) expected.document_id = documentId;
        persist(expected);
        let body = {...operands, request_id: expected.request_id, operator_wire_json: wire};
        const headers = {Accept: "application/json", "X-Request-ID": expected.request_id};
        if (file) {
          const form = new FormData(); form.append("file", file, file.name);
          for (const [k,v] of Object.entries(body)) form.append(k, v);
          body = form;
        } else {headers["Content-Type"] = "application/json"; body = JSON.stringify(body);}
        try {
          const response = await fetch(url, {method: method || "POST", headers, body});
          if (response.status === 423) {
            const rejection = await response.json();
            if (rejection.code === "business_data_maintenance" && rejection.status === "blocked" && rejection.request_id === expected.request_id) {
              clear(expected); box.hidden = false; box.replaceChildren();
              const message = document.createElement("p"); message.textContent = rejection.message || "Документ не сохранён: режим обслуживания.";
              box.appendChild(message); throw {definitive: true, message: message.textContent};
            }
          }
        } catch (error) {
          if (error && error.definitive) throw new Error(error.message);
        }
        return read(expected);
      });
    }
    async function showOperation(id) {
      load(); if (pending) return recover();
      if (!/^supplier_financial_(?:batch_)?[a-f0-9]{32}$/.test(id)) return;
      const response = await fetch(options.operationPath + "?operation_id=" + encodeURIComponent(id), {headers: {Accept: "application/json"}});
      const result = response.ok ? await response.json() : null;
      if (!result || result.domain !== "cny_account_document" || !result.acceptance || result.acceptance.domain !== result.domain
          || (await window.OperatorAcceptance.readSameOperation({operation_id: id, domain: result.domain}, () => Promise.resolve(result))).status !== "accepted") {
        box.hidden = false; window.OperatorAcceptance.renderState(box, null); return;
      }
      present(result);
    }
    load();
    window.addEventListener("storage", event => {if (event.key === key) {load(); if (pending) recover().catch(options.onError);}});
    return {mutate, recover, showOperation};
  }
  window.CnySourceAcceptance = {create};
})();
