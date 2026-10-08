/* Facility source identity is opaque and actor-scoped. Recovery never submits. */
(function () {
  "use strict";
  function canonical(value) {
    return value && typeof value === "object" && !Array.isArray(value)
      ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
  }
  async function hash(text) {
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return "sha256:" + Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
  }
  function create(options) {
    const key = "wbc_facility_source_pending_v1:" + options.scope;
    const box = options.container;
    let pending = null, inFlight = null;
    function load() {
      pending = JSON.parse(localStorage.getItem(key) || "null");
      if (pending && (!pending.request_id || !pending.action || !pending.digest || typeof pending.entity_id !== "string"
          || (pending.operation_id && !/^ff_directory_[a-f0-9]{32}$/.test(pending.operation_id))))
        throw new Error("Не удалось прочитать ID изменения склада.");
      options.onLock(Boolean(pending)); return pending;
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
      window.OperatorAcceptance.renderReceipt(box, result.acceptance, {
        onClose: () => {box.hidden = true;}, onJournal: receipt => showOperation(receipt.operation_id).catch(options.onError)
      });
    }
    async function fetchRead(query) {
      const response = await fetch(options.path + "/facility-operations?" + query, {headers: {Accept: "application/json"}});
      return response.ok ? response.json() : null;
    }
    async function read(expected) {
      const result = await fetchRead("request_id=" + encodeURIComponent(expected.request_id));
      if (!result || result.domain !== "facility_mapping" || result.request_id !== expected.request_id
          || result.action !== expected.action || result.wire_digest !== expected.digest || result.settled !== true) {
        unknown(); throw new Error("Проверяем сохранение. Повторно отправлять изменение не нужно.");
      }
      if (result.status === "rejected" && !result.acceptance) {
        clear(expected); box.hidden = false; box.replaceChildren();
        const message = document.createElement("p"); message.textContent = result.error || "Изменение не сохранено."; box.appendChild(message);
        throw new Error(message.textContent);
      }
      const receipt = result.acceptance;
      if (!window.OperatorAcceptance.acceptedOperation(receipt) || receipt.domain !== result.domain
          || (expected.operation_id && receipt.operation_id !== expected.operation_id)
          || receipt.source_ref.action !== expected.action || (expected.entity_id && receipt.source_ref.entity_id !== expected.entity_id)
          || (await window.OperatorAcceptance.readSameOperation({operation_id: expected.operation_id || receipt.operation_id, domain: result.domain}, () => Promise.resolve(result))).status !== "accepted") {
        unknown(); throw new Error("Подтверждение этой операции пока не получено.");
      }
      clear(expected); present(result); await options.onRecovered(result); return result;
    }
    async function recover() {
      load(); if (!pending) return null;
      try {return await read(pending);} catch (error) {load(); if (pending) unknown(); throw error;}
    }
    async function mutate(url, action, operands, entityId) {
      if (inFlight) return inFlight;
      if (!navigator.locks) throw new Error("Браузер не поддерживает безопасное сохранение изменения.");
      inFlight = navigator.locks.request(key, {ifAvailable: true}, async lock => {
        if (!lock) {
          load(); if (pending) return recover();
          throw new Error("Изменение уже сохраняется в другой вкладке. Дождитесь подтверждения.");
        }
        load(); if (pending) return recover();
        const wire = JSON.stringify(canonical(operands));
        const expected = {request_id: "facility_" + crypto.randomUUID().replaceAll("-", ""), action, entity_id: entityId || "", digest: await hash(wire)};
        persist(expected); unknown();
        try {
          const response = await fetch(url, {method: "POST", headers: {Accept: "application/json", "Content-Type": "application/json", "X-Request-ID": expected.request_id, "X-WB-FF-Pool-CSRF": "1"},
            body: JSON.stringify({...operands, request_id: expected.request_id, operator_wire_json: wire})});
          if (response.status === 423) {
            const refusal = await response.json();
            if (refusal.code === "business_data_maintenance" && refusal.status === "blocked" && refusal.request_id === expected.request_id) {
              clear(expected); box.hidden = false; box.replaceChildren();
              const message = document.createElement("p"); message.textContent = refusal.message || "Изменение не сохранено: режим обслуживания."; box.appendChild(message);
              throw {definitive: true, message: message.textContent};
            }
          }
          // A received source-specific refusal is definitive even where the
          // native audit has no facility FK to retain an absent preview alias.
          if (response.ok) {
            const result = await response.json();
            if (result && result.domain === "facility_mapping" && result.request_id === expected.request_id
                && result.action === expected.action && result.wire_digest === expected.digest && result.status === "accepted"
                && window.OperatorAcceptance.acceptedOperation(result.acceptance) && result.acceptance.domain === result.domain
                && result.acceptance.source_ref.action === expected.action
                && (!expected.entity_id || result.acceptance.source_ref.entity_id === expected.entity_id)) {
              expected.operation_id = result.acceptance.operation_id; persist(expected);
            }
            if (result && result.domain === "facility_mapping" && result.request_id === expected.request_id
                && result.action === expected.action && result.wire_digest === expected.digest && result.status === "rejected" && result.settled === true && !result.acceptance) {
              clear(expected); box.hidden = false; box.replaceChildren();
              const message = document.createElement("p"); message.textContent = result.error || "Изменение не сохранено."; box.appendChild(message);
              throw {definitive: true, message: message.textContent};
            }
          }
        } catch (error) {if (error && error.definitive) throw new Error(error.message);}
        return read(expected);
      });
      try {return await inFlight;} finally {inFlight = null;}
    }
    async function showOperation(id) {
      load(); if (pending) return recover();
      if (!/^ff_directory_[a-f0-9]{32}$/.test(id)) return;
      const result = await fetchRead("operation_id=" + encodeURIComponent(id));
      if (!result || result.domain !== "facility_mapping" || !result.acceptance || result.acceptance.domain !== result.domain
          || (await window.OperatorAcceptance.readSameOperation({operation_id: id, domain: result.domain}, () => Promise.resolve(result))).status !== "accepted") {unknown(); return;}
      present(result);
    }
    load(); window.addEventListener("storage", event => {if (event.key === key) {load(); if (pending) recover().catch(options.onError);}});
    return {mutate, recover, showOperation, isPending: () => Boolean(load())};
  }
  window.FacilitySourceAcceptance = {create};
})();
