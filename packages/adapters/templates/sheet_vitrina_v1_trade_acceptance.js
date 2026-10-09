/* Library source only: opaque persistent identity; recovery never resubmits. */
(function () {
  "use strict";
  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    return value && typeof value === "object" ? Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])])) : value;
  }
  async function hash(text) {
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return "sha256:" + Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
  }
  function create(options) {
    const key = "wbc_trade_source_pending_v1:" + options.scope;
    const box = options.container;
    let pending;
    let inFlight = null;
    function load() {
      pending = JSON.parse(localStorage.getItem(key) || "null");
      if (pending && (!pending.request_id || !pending.action || !pending.digest)) throw new Error("Не удалось прочитать ID изменения библиотеки.");
      options.onLock(Boolean(pending));
    }
    function persist(value) {
      if (value) localStorage.setItem(key, JSON.stringify(value)); else localStorage.removeItem(key);
      load();
    }
    function clear(expected) {load(); if (pending && pending.request_id === expected.request_id) persist(null);}
    function unknown() {
      box.hidden = false; window.OperatorAcceptance.renderState(box, null);
      const check = document.createElement("button"); check.type = "button"; check.textContent = "Проверить статус";
      check.addEventListener("click", () => recover().catch(options.onError)); box.appendChild(check);
    }
    function present(result) {
      box.hidden = false;
      window.OperatorAcceptance.renderReceipt(box, result.acceptance, {onClose: () => {box.hidden = true;}});
      const state = document.createElement("p"); state.textContent = "Изменение библиотеки сохранено."; box.appendChild(state);
    }
    async function read(expected) {
      const response = await fetch(options.path + "?request_id=" + encodeURIComponent(expected.request_id), {headers: {Accept: "application/json"}});
      const result = response.ok ? await response.json() : null;
      if (!result || result.domain !== "trade_document_library" || result.request_id !== expected.request_id
          || result.action !== expected.action || result.wire_digest !== expected.digest || result.settled !== true) {
        unknown(); throw new Error("Проверяем сохранение. Повторно отправлять документ не нужно.");
      }
      if (result.status === "rejected" && !result.acceptance) {clear(expected); box.hidden = true; throw new Error(result.error || "Документ не сохранён.");}
      const receipt = result.acceptance;
      if (!window.OperatorAcceptance.acceptedOperation(receipt) || receipt.domain !== result.domain
          || receipt.source_ref.action !== expected.action || (expected.document_id && receipt.source_ref.document_id !== expected.document_id)
          || (await window.OperatorAcceptance.readSameOperation({operation_id: receipt.operation_id, domain: result.domain}, () => Promise.resolve(result))).status !== "accepted") {
        unknown(); throw new Error("Подтверждение этой операции пока не получено.");
      }
      clear(expected); present(result); await options.onRecovered(result); return result;
    }
    async function recover() {load(); if (!pending) return null; try {return await read(pending);} catch (error) {load(); if (pending) unknown(); throw error;}}
    async function mutate(url, action, operands, method, file, documentId) {
      if (inFlight) return inFlight;
      if (!navigator.locks) throw new Error("Браузер не поддерживает безопасное сохранение документа.");
      inFlight = navigator.locks.request(key, {ifAvailable: true}, async lock => {
        if (!lock) {
          load(); if (pending) return recover();
          throw new Error("Документ уже сохраняется в другой вкладке. Дождитесь подтверждения.");
        }
        load(); if (pending) return recover();
        const wire = JSON.stringify(canonical(operands));
        const expected = {request_id: "trade_" + crypto.randomUUID().replaceAll("-", ""), action, digest: await hash(wire)};
        if (documentId) expected.document_id = documentId;
        persist(expected); unknown();
        let body = {...operands, request_id: expected.request_id, operator_wire_json: wire};
        const headers = {Accept: "application/json", "X-Request-ID": expected.request_id};
        if (file) {const form = new FormData(); form.append("file", file, file.name); for (const [k,v] of Object.entries(body)) form.append(k, v); body = form;}
        else {headers["Content-Type"] = "application/json"; body = JSON.stringify(body);}
        try {
          const response = await fetch(url, {method: method || "POST", headers, body});
          if (response.status === 423) {
            const refusal = await response.json();
            if (refusal.code === "business_data_maintenance" && refusal.status === "blocked" && refusal.request_id === expected.request_id) {
              clear(expected); box.hidden = false; box.replaceChildren();
              const text = document.createElement("p"); text.textContent = refusal.message || "Документ не сохранён: режим обслуживания."; box.appendChild(text);
              throw {definitive: true, message: text.textContent};
            }
          }
        } catch (error) {if (error && error.definitive) throw new Error(error.message);}
        return read(expected);
      });
      try {return await inFlight;} finally {inFlight = null;}
    }
    async function showOperation(id) {
      load(); if (pending) return recover();
      if (!/^trade_library_[a-f0-9]{32}$/.test(id)) return;
      const response = await fetch(options.path + "?operation_id=" + encodeURIComponent(id), {headers: {Accept: "application/json"}});
      const result = response.ok ? await response.json() : null;
      if (!result || result.domain !== "trade_document_library" || !result.acceptance || result.acceptance.domain !== result.domain
          || (await window.OperatorAcceptance.readSameOperation({operation_id: id, domain: result.domain}, () => Promise.resolve(result))).status !== "accepted") {unknown(); return;}
      present(result); await options.onRecovered(result);
    }
    load(); window.addEventListener("storage", e => {if (e.key === key) {load(); if (pending) recover().catch(options.onError);}});
    return {mutate, recover, showOperation};
  }
  window.TradeSourceAcceptance = {create};
})();
