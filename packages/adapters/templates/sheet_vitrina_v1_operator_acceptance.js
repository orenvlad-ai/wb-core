/* Shared receipt presentation. Native forms own submission, identity and recovery. */
(function () {
  "use strict";

  const labels = Object.freeze({
    accepted: "Ожидает обработки",
    processing: "Обрабатывается",
    completed: "Обработано",
    delayed: "Обработка задерживается",
    needs_attention: "Требует внимания"
  });
  const unaccepted = new Set(["draft", "preview", "staged", "parsed", "validation"]);

  function object(value) {
    return Boolean(value && typeof value === "object" && !Array.isArray(value));
  }

  function text(value) {
    return typeof value === "string" || typeof value === "number" || typeof value === "boolean"
      ? String(value) : "";
  }

  function identity(value) {
    return typeof value === "string" && value.trim() === value && value.length > 0;
  }

  function sourceStage(receipt) {
    if (!object(receipt)) return "";
    return [receipt.primary_effect, receipt.source_state, receipt.status, receipt.state]
      .find(function (value) { return unaccepted.has(value); }) || "";
  }

  function acceptedOperation(receipt) {
    return Boolean(object(receipt) && receipt.durable_saved === true
      && identity(receipt.operation_id) && identity(receipt.accepted_at)
      && Object.hasOwn(labels, receipt.state) && !sourceStage(receipt));
  }

  function make(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = text(value);
    return node;
  }

  function stateNode(receipt) {
    const accepted = acceptedOperation(receipt);
    const stage = sourceStage(receipt);
    const label = accepted ? (receipt.primary_effect === "source_saved" && receipt.state === "completed" ? "Сохранено" : labels[receipt.state])
      : stage === "draft" ? "Черновик сохранён"
      : stage ? "Предпросмотр" : "Проверяем сохранение";
    const node = make("span", "ff-operation-status", label);
    node.dataset.state = accepted ? (receipt.state === "completed" ? "processed" : receipt.state)
      : stage || "unknown";
    return node;
  }

  function renderState(container, receipt) {
    const section = make("section", "ff-operation-detail");
    section.setAttribute("role", "status");
    section.appendChild(stateNode(receipt));
    const accepted = acceptedOperation(receipt);
    const stage = sourceStage(receipt);
    const reason = accepted ? text(receipt.reason_ru)
      : stage === "draft" ? "Черновик сохранён. Документ ещё не принят."
      : stage ? "Документ ещё не принят."
      : "Подтверждение ещё не получено. Повторно отправлять документ не нужно.";
    if (reason) section.appendChild(make("p", "ff-pool-note", reason));
    container.replaceChildren(section);
    return section;
  }

  function localPath(value) {
    if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")
        || /[\\\u0000-\u0020\u007f]/.test(value) || /%(?:5c|0a|0d)/i.test(value)) return "";
    // Root-relative paths cannot select another origin. Also reject browser URL
    // normalization which would turn a backslash/authority into an external link.
    try {
      const base = new URL("https://operator.invalid/");
      const target = new URL(value, base);
      return target.origin === base.origin ? value : "";
    } catch (_) { return ""; }
  }

  function journalLink(receipt, options) {
    options = options || {};
    const callback = typeof options.onJournal === "function" ? options.onJournal : null;
    const path = object(receipt) ? localPath(receipt.journal_path) || localPath(receipt.detail_path) : "";
    const link = make(callback || path ? "a" : "span", "ff-operation-link", "Журнал операций");
    if (callback || path) {
      link.href = path || "#";
      if (callback) link.addEventListener("click", function (event) {
        event.preventDefault();
        callback(receipt, event);
      });
    } else {
      link.setAttribute("aria-disabled", "true");
    }
    return link;
  }

  function renderReceipt(container, receipt, options) {
    if (!acceptedOperation(receipt)) return renderState(container, receipt);
    options = options || {};
    const section = make("section", "ff-operation-receipt");
    section.dataset.ffOperationReceipt = receipt.operation_id;
    section.setAttribute("role", "status");
    section.setAttribute("tabindex", "-1");
    const check = make("span", "ff-operation-check", "✓");
    check.setAttribute("aria-hidden", "true");
    section.append(check, make("h3", "", "Принято"), make("p", "", receipt.primary_effect === "source_saved" ? "Изменение сохранено." : "Документ сохранён."));
    if (text(receipt.title_ru)) section.appendChild(make("p", "ff-operation-title", receipt.title_ru));
    const fields = Array.isArray(receipt.fields) ? receipt.fields : [];
    if (fields.length) {
      const summary = make("dl", "ff-operation-summary");
      fields.forEach(function (field) {
        if (!object(field) || !text(field.label)) return;
        const row = make("div");
        row.append(make("dt", "", field.label), make("dd", "", text(field.value) || "—"));
        summary.appendChild(row);
      });
      if (summary.childElementCount) section.appendChild(summary);
    }
    section.appendChild(stateNode(receipt));
    if (text(receipt.reason_ru)) section.appendChild(make("p", "ff-pool-note", receipt.reason_ru));
    const actions = make("div", "ff-operation-actions");
    const close = make("button", "button primary", "Закрыть");
    close.type = "button";
    close.disabled = typeof options.onClose !== "function";
    if (!close.disabled) close.addEventListener("click", function (event) {
      options.onClose(receipt, event);
    });
    actions.append(close, journalLink(receipt, options));
    section.appendChild(actions);
    container.replaceChildren(section);
    if (!section.closest("[hidden]") && section.getClientRects().length) section.focus({preventScroll: true});
    return section;
  }

  function domain(value) {
    if (!object(value)) return "";
    const direct = value.domain;
    const source = object(value.source_ref) ? value.source_ref.domain : undefined;
    if (direct !== undefined && source !== undefined && direct !== source) return null;
    const selected = direct === undefined ? source : direct;
    return selected === undefined ? "" : identity(selected) ? selected : null;
  }

  function unknown(reason) {
    return {status: "unknown", operation: null, reason_code: reason};
  }

  async function readSameOperation(operationRef, readCallback) {
    const ref = typeof operationRef === "string" ? {operation_id: operationRef} : operationRef;
    const expectedDomain = domain(ref);
    if (!object(ref) || !identity(ref.operation_id) || expectedDomain === null
        || typeof readCallback !== "function") return unknown("invalid_operation_ref");
    const expectedOperationId = ref.operation_id;
    let payload;
    try { payload = await readCallback(operationRef); }
    catch (_) { return unknown("read_failed"); }
    if (!object(payload)) return unknown("invalid_readback");
    const wrappers = ["operation", "acceptance"].filter(function (key) { return Object.hasOwn(payload, key); });
    if (wrappers.length > 1 || (wrappers.length && Object.hasOwn(payload, "operation_id"))) {
      return unknown("ambiguous_readback");
    }
    const receipt = wrappers.length ? payload[wrappers[0]] : payload;
    if (!object(receipt) || receipt.operation_id !== expectedOperationId) return unknown("operation_mismatch");
    const actualDomain = domain(receipt);
    if (actualDomain === null || (expectedDomain && actualDomain !== expectedDomain)) return unknown("domain_mismatch");
    if (!acceptedOperation(receipt)) return unknown("source_not_accepted");
    return {status: "accepted", operation: receipt, reason_code: ""};
  }

  window.OperatorAcceptance = Object.freeze({acceptedOperation, renderReceipt, renderState, readSameOperation, journalLink});
}());
