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
  const popups = new WeakMap();
  const dismissedOperations = new WeakMap();
  let lastTrigger = null;
  document.addEventListener("click", function (event) {
    const control = event.target.closest && event.target.closest("button, a, input, select, textarea");
    if (control) lastTrigger = control;
  }, {capture:true});

  function viewportDocument() {
    let host = window;
    // A dialog inside an auto-height iframe is centered in that frame, not in
    // the operator's viewport. Only traverse accessible same-origin parents.
    try {
      while (host.parent !== host && host.parent.location.origin === window.location.origin) {
        void host.parent.document.body;
        host = host.parent;
      }
    } catch (_) { /* Cross-origin embedding keeps its own document boundary. */ }
    return host.document;
  }

  function dismissPopup(container, notify, userDismiss) {
    const popup = popups.get(container);
    if (popup) popup.dismiss(notify, userDismiss);
  }

  function popupFocus(dialog) {
    const active = dialog.ownerDocument.activeElement;
    return dialog.contains(active) ? {tag:active.tagName, href:active.getAttribute("href"),
      text:active.textContent, operation:active.dataset.ffOperationReceipt} : null;
  }

  function restorePopupFocus(dialog, focus) {
    if (!focus) return true;
    const node = Array.from(dialog.querySelectorAll(focus.tag)).find(node =>
      focus.operation ? node.dataset.ffOperationReceipt === focus.operation
        : node.getAttribute("href") === focus.href && node.textContent === focus.text);
    if (node) node.focus({preventScroll:true});
    return Boolean(node);
  }

  function receiptPopup(container, section, receipt, options) {
    const doc = viewportDocument();
    const parent = container.parentNode;
    if (!parent) return null;
    // Recovery may finish before its source tab/modal is opened. A top-layer
    // dialog still inherits display:none from an ancestor, so mirror that
    // presentation at BODY just as for an embedded source document.
    let concealed = false;
    for (let node = parent; node; node = node.parentElement) {
      const style = container.ownerDocument.defaultView.getComputedStyle(node);
      if (node.hidden || style.display === "none" || style.visibility === "hidden"
          || style.visibility === "collapse") { concealed = true; break; }
    }
    const mirrored = doc !== container.ownerDocument || concealed;
    const active = container.ownerDocument.activeElement;
    // Native submit locks can disable the clicked button before the receipt
    // arrives, which makes the browser focus BODY. Retain that real trigger.
    const focus = active && active !== container.ownerDocument.body ? active
      : lastTrigger && lastTrigger.isConnected ? lastTrigger : active;
    const dialog = doc.createElement("dialog");
    dialog.className = "ff-operation-popup";
    dialog.setAttribute("aria-label", "Принято");
    Object.assign(dialog.style, {position:"fixed", inset:"0", margin:"auto", padding:"0",
      width:"min(660px, calc(100vw - 32px))", maxHeight:"calc(100dvh - 32px)",
      overflow:"auto", border:"1px solid var(--border, #d8dee6)", borderRadius:"16px",
      background:"var(--panel-bg, white)", color:"var(--text, #182230)"});
    let observer = null;
    let closed = false;
    let pendingFocus = null;
    const frames = [];
    for (let child = window; child.frameElement && child.document !== doc; child = child.parent) {
      frames.push({frame:child.frameElement, document:child.document});
    }
    const lifetime = new MutationObserver(checkOwner);
    const display = container.style.getPropertyValue("display");
    const priority = container.style.getPropertyPriority("display");
    // Keep source-document IDs and ancestor queries valid. Mirror presentation
    // only when needed; the original controllers retain their nodes.
    function sync(focusState) {
      if (container.hidden || !container.querySelector("[data-ff-operation-receipt]")) { dismiss(false); return; }
      const active = focusState || pendingFocus || popupFocus(dialog);
      const view = container.cloneNode(true);
      view.hidden = false;
      view.style.removeProperty("display");
      dialog.replaceChildren(view);
      const close = view.querySelector(".ff-operation-actions button");
      if (close) close.addEventListener("click", () => dismiss(true));
      const journal = view.querySelector(".ff-operation-actions .ff-operation-link");
      if (journal) journal.addEventListener("click", function (event) {
        dismiss(false, true);
        if (typeof options.onJournal === "function") {
          event.preventDefault(); options.onJournal(receipt, event);
        }
      });
      pendingFocus = restorePopupFocus(dialog, active) ? null : active;
    }
    if (mirrored) {
      container.style.setProperty("display", "none", "important");
      doc.body.appendChild(dialog);
      sync();
      observer = new MutationObserver(() => sync());
      observer.observe(container, {subtree:true,childList:true,characterData:true,attributes:true});
    } else {
      // Native top-layer layout works from the original DOM location, so even
      // polling callers that query their ancestor retain the same container.
      parent.insertBefore(dialog, container);
      dialog.appendChild(container);
      observer = new MutationObserver(function () {
        if (container.hidden || !container.querySelector("[data-ff-operation-receipt]")) dismiss(false);
        else if (pendingFocus && restorePopupFocus(dialog, pendingFocus)) pendingFocus = null;
      });
      observer.observe(container, {childList:true,attributes:true});
    }
    function dismiss(notify, userDismiss) {
      if (closed) return;
      closed = true;
      if (observer) observer.disconnect();
      lifetime.disconnect();
      frames.forEach(owner => owner.frame.removeEventListener("load", checkOwner));
      popups.delete(container);
      window.removeEventListener("pagehide", onPageHide);
      if (dialog.open) dialog.close();
      if (mirrored) {
        if (display) container.style.setProperty("display", display, priority);
        else container.style.removeProperty("display");
      } else if (dialog.parentNode) dialog.replaceWith(container);
      dialog.remove();
      if (notify || userDismiss) {
        const dismissed = dismissedOperations.get(container) || new Set();
        dismissed.add(receipt.operation_id);
        dismissedOperations.set(container, dismissed);
        container.hidden = true;
      }
      if (notify && typeof options.onClose === "function") options.onClose(receipt);
      if (focus && focus.isConnected) focus.focus({preventScroll:true});
    }
    function checkOwner() {
      if (!container.isConnected || !dialog.isConnected || frames.some(owner =>
        !owner.frame.isConnected || owner.frame.contentDocument !== owner.document)) dismiss(false);
    }
    function onPageHide() { dismiss(false); }
    dialog.addEventListener("cancel", function (event) { event.preventDefault(); dismiss(true); });
    dialog.addEventListener("close", function () { dismiss(true); });
    dialog.addEventListener("keydown", function (event) {
      if (event.key !== "Tab") return;
      const controls = Array.from(dialog.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]'))
        .filter(node => node.getClientRects().length);
      if (!controls.length) { event.preventDefault(); return; }
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && (doc.activeElement === first || !controls.includes(doc.activeElement))) {
        event.preventDefault(); last.focus({preventScroll:true});
      } else if (!event.shiftKey && doc.activeElement === last) {
        event.preventDefault(); first.focus({preventScroll:true});
      }
    });
    window.addEventListener("pagehide", onPageHide);
    lifetime.observe(doc.documentElement, {childList:true,subtree:true});
    frames.forEach(owner => owner.frame.addEventListener("load", checkOwner));
    const popup = {dismiss, operationId:receipt.operation_id, focus:() => popupFocus(dialog),
      refresh(nextReceipt, nextOptions, focusState) {
        receipt = nextReceipt; options = nextOptions;
        if (mirrored) sync(focusState);
        else pendingFocus = restorePopupFocus(dialog, focusState) ? null : focusState;
      }};
    popups.set(container, popup);
    dialog.showModal();
    (mirrored ? dialog.querySelector(".ff-operation-receipt") : section).focus({preventScroll:true});
    return popup;
  }

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
    const sourceOnly = receipt && receipt.primary_effect === "source_saved"
      && (receipt.calculation_completed === false || (object(receipt.processing) && receipt.processing.kind === "source_only"));
    const label = accepted && receipt.primary_effect === "reply_draft_saved" ? "Черновик сохранён" : accepted && receipt.primary_effect === "external_command" && receipt.state === "completed" ? "Подтверждено WB" : accepted ? (sourceOnly && receipt.state === "completed" ? "Сохранено" : labels[receipt.state])
      : stage === "draft" ? "Черновик сохранён"
      : stage ? "Предпросмотр" : "Проверяем сохранение";
    const node = make("span", "ff-operation-status", label);
    node.dataset.state = accepted ? (receipt.state === "completed" ? "processed" : receipt.state)
      : stage || "unknown";
    return node;
  }

  function renderState(container, receipt) {
    dismissPopup(container, false);
    const section = make("section", "ff-operation-detail");
    section.setAttribute("role", "status");
    section.appendChild(stateNode(receipt));
    const accepted = acceptedOperation(receipt);
    const stage = sourceStage(receipt);
    const reason = accepted || receipt && receipt.source_confirmation_missing === true ? text(receipt.reason_ru)
      : stage === "draft" ? "Черновик сохранён. Документ ещё не принят."
      : stage ? "Документ ещё не принят."
      : "Подтверждение ещё не получено. Повторно отправлять документ не нужно.";
    if (reason) section.appendChild(make("p", "ff-pool-note", reason));
    appendNativeLink(section, receipt);
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

  function appendNativeLink(section, receipt) {
    const path=object(receipt) && localPath(receipt.native_path);
    if(path){const link=make('a','ff-operation-link','Исходное задание');link.href=path;section.appendChild(link);}
  }

  function renderReceipt(container, receipt, options) {
    if (!acceptedOperation(receipt)) return renderState(container, receipt);
    options = options || {};
    const current = popups.get(container);
    const refresh = current && current.operationId === receipt.operation_id && options.mode !== "inline";
    const focusState = refresh ? current.focus() : null;
    if (!refresh) dismissPopup(container, false);
    const modal = Boolean(refresh) || options.mode !== "inline" && !container.closest("dialog");
    const section = make("section", "ff-operation-receipt");
    section.dataset.ffOperationReceipt = receipt.operation_id;
    section.setAttribute("role", "status");
    section.setAttribute("tabindex", "-1");
    const check = make("span", "ff-operation-check", "✓");
    check.setAttribute("aria-hidden", "true");
    section.append(check, make("h3", "", receipt.primary_effect==='reply_draft_saved'?'Черновик сохранён':'Принято'), make("p", "", receipt.primary_effect === "external_command" ? "Команда сохранена." : receipt.primary_effect==='native_job'?'Задание сохранено.':receipt.primary_effect==='reply_draft_saved'?'Ответ ещё не опубликован в WB.':receipt.primary_effect === "source_saved" ? "Изменение сохранено." : "Документ сохранён."));
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
    if (["external_command", "external_job"].includes(receipt.primary_effect) && Array.isArray(receipt.children) && receipt.children.length) {
      const children = make("ul", "ff-operation-children");
      receipt.children.forEach(function (child) {
        if (!object(child)) return;
        children.appendChild(make("li", "", (text(child.label_ru)||"SKU " + text(child.nm_id) + ": " + text(child.parameter_field))
          + " — " + (child.external_confirmed === true ? "Подтверждено WB" : ({created:"Не отправлено",submitted:"Ожидает WB",
            ambiguous:"Результат неизвестен",failed:"Ошибка",rejected:"Отклонено",cancelled:"Отменено"})[child.outcome] || "Ожидает подтверждения")));
      });
      section.appendChild(children);
    }
    appendNativeLink(section, receipt);
    const actions = make("div", "ff-operation-actions");
    const close = make("button", "button primary", "Закрыть");
    close.type = "button";
    close.disabled = !modal && typeof options.onClose !== "function";
    if (!close.disabled) close.addEventListener("click", function (event) {
      if (popups.has(container)) dismissPopup(container, true);
      else if (typeof options.onClose === "function") options.onClose(receipt, event);
    });
    const journal = journalLink(receipt, options);
    if (modal) journal.addEventListener("click", function () { dismissPopup(container, false, true); }, {capture:true});
    actions.append(close, journal);
    section.appendChild(actions);
    container.replaceChildren(section);
    if (refresh) current.refresh(receipt, options, focusState);
    else if (modal && dismissedOperations.get(container)?.has(receipt.operation_id)) container.hidden = true;
    else if (modal) { container.hidden = false; receiptPopup(container, section, receipt, options); }
    else if (!section.closest("[hidden]") && section.getClientRects().length) section.focus({preventScroll: true});
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

  function exactExternalReceipt(receipt,domainName,nativeId){
    return acceptedOperation(receipt) && domain(receipt)===domainName
      && object(receipt.source_ref) && receipt.source_ref.native_idempotency_key===nativeId;
  }

  async function readExternalNative(domainName, nativeId) {
    if (!identity(domainName) || !identity(nativeId)) return null;
    try {
      const response = await fetch("/v1/sheet-vitrina-v1/operations/external?domain=" + encodeURIComponent(domainName)
        + "&native_id=" + encodeURIComponent(nativeId), {headers:{Accept:"application/json"},cache:"no-store",credentials:"same-origin"});
      if (!response.ok) return null;
      const payload = await response.json(), receipt = payload && payload.operation;
      return exactExternalReceipt(receipt,domainName,nativeId) ? receipt : null;
    } catch (_) { return null; }
  }

  function externalHttpError(response, payload, fallback) {
    const error=new Error(object(payload) && typeof payload.error === 'string' ? payload.error : fallback);
    error.definitiveNativeRejection=!!(response && object(payload) && typeof payload.error === 'string'
      && ((response.status>=400 && response.status<500)
        || (response.status===503 && ['price_prestate_unavailable','registry_fail_closed'].includes(payload.reason))));
    return error;
  }

  async function onceExternal(domainName, nativeId, submitCallback) {
    if (!identity(domainName) || !identity(nativeId) || typeof submitCallback !== "function") throw new Error("Точная ссылка исходной команды не получена.");
    const key = "operator-external-attempt:" + domainName + ":" + nativeId;
    let submit = false, rejected = "";
    try {
      const saved=localStorage.getItem(key);
      if (!saved) { localStorage.setItem(key, "attempted"); submit = true; }
      else { try { const value=JSON.parse(saved);if(object(value)&&value.rejected===true)rejected=String(value.reason||''); } catch (_) {} }
    } catch (_) { /* Without durable browser identity, submit stays blocked. */ }
    if (submit) {
      try {
        const result=await submitCallback();
        if(object(result)&&(!Object.hasOwn(result,'acceptance')||exactExternalReceipt(result.acceptance,domainName,nativeId)))return result;
        // A foreign/malformed POST receipt cannot paint green; read only this native source.
      }
      catch (error) {
        if(error && error.definitiveNativeRejection===true){
          rejected=String(error.message||"Команда отклонена сервером.");
          try{localStorage.setItem(key,JSON.stringify({rejected:true,reason:rejected}));}catch(_){}
        }
        // Even a server rejection may follow a saved source. Exact native GET
        // takes precedence; uncertain replies never authorize a second POST.
      }
    }
    const receipt = await readExternalNative(domainName, nativeId);
    if(!receipt && rejected){const error=new Error(rejected+' Для новой отправки создайте новый preview.');error.definitiveNativeRejection=true;throw error;}
    return {status:receipt ? "native_readback" : "unknown", acceptance:receipt,
      native_rejection_reason:receipt && receipt.state!=="completed" ? rejected : "",
      operator_projection:{status:receipt ? "ready" : "unknown",reason_code:"exact_native_read_only"}};
  }

  function exactFeedbackReceipt(receipt, domainName, nativeId) {
    if(!acceptedOperation(receipt) || domain(receipt)!==domainName || !object(receipt.source_ref))return false;
    const ref=receipt.source_ref;
    if(domainName==='buyer_support')return ref.request_id===nativeId;
    if(domainName==='feedback_complaint')return ref.request_key===nativeId;
    if(domainName!=='feedback_reply')return false;
    if(nativeId.startsWith('feedback-version:')){
      const match=nativeId.match(/^feedback-version:([1-9][0-9]*):(.*)$/);
      return !!match && Number(ref.content_version)===Number(match[1]) && ref.feedback_id===match[2];
    }
    return ref.entity_id===nativeId;
  }

  async function readFeedbackNative(domainName, nativeId) {
    if(!identity(domainName)||!identity(nativeId))return null;
    try{
      const response=await fetch('/v1/sheet-vitrina-v1/operations/feedback?domain='+encodeURIComponent(domainName)+'&native_id='+encodeURIComponent(nativeId),
        {headers:{Accept:'application/json'},cache:'no-store',credentials:'same-origin'});
      if(!response.ok)return null;
      const payload=await response.json();
      return exactFeedbackReceipt(payload.operation,domainName,nativeId)?payload.operation:null;
    }catch(_){return null;}
  }

  function feedbackExpected(receipt, expected) {
    const ref=receipt.source_ref;
    return (!expected.manual_reply_sha256 || ref.manual_reply_sha256===expected.manual_reply_sha256)
      && (!expected.publication_required || identity(ref.publication_key))
      && (!expected.publication_reply_sha256 || ref.publication_reply_sha256===expected.publication_reply_sha256)
      && (!expected.min_media_processing_version || Number(ref.media_processing_version)>=expected.min_media_processing_version);
  }

  function withFeedbackLock(commandKey, callback) {
    if(!identity(commandKey)||typeof callback!=='function')throw new Error('Точная ссылка исходной команды не получена.');
    if(!navigator.locks || !navigator.locks.request)throw new Error('Браузер не может безопасно согласовать отправку между вкладками. Новая отправка остановлена.');
    return navigator.locks.request('operator-feedback-lock:'+commandKey,{mode:'exclusive'},callback);
  }

  async function onceFeedback(domainName, nativeId, commandKey, submitCallback, expected) {
    if(!['feedback_reply','feedback_complaint','buyer_support'].includes(domainName)||!identity(nativeId)||!identity(commandKey)||typeof submitCallback!=='function')throw new Error('Точная ссылка исходной команды не получена.');
    expected=expected || {};
    const key='operator-feedback-attempt:'+domainName+':'+nativeId+':'+commandKey;
    // This differs from the selected-request lock: nested acquisition cannot
    // deadlock, while exact commands from different tabs share this lock.
    return withFeedbackLock('attempt:'+key,async function(){
      let send=false;
      try{if(!localStorage.getItem(key)){localStorage.setItem(key,'attempted');send=true;}}
      catch(_){throw new Error('Не удалось сохранить номер операции в браузере. Новая отправка остановлена.');}
      if(send){
        try{
          const result=await submitCallback();
          if(object(result)&&exactFeedbackReceipt(result.acceptance,domainName,nativeId)&&feedbackExpected(result.acceptance,expected))return result;
          if(object(result)&&result.not_accepted===true && !result.run_id){
            try{localStorage.removeItem(key);}catch(_){}
            return result;
          }
        }catch(_){/* Lost response permits only the exact native read below. */}
      }
      let receipt=await readFeedbackNative(domainName,nativeId);
      if(receipt && !feedbackExpected(receipt,expected))receipt=null;
      return {status:receipt?'native_readback':'unknown',acceptance:receipt};
    });
  }

  function showFeedbackReceipt(result) {
    let dialog=document.getElementById('operator-feedback-receipt');
    if(!dialog){dialog=make('dialog','');dialog.id='operator-feedback-receipt';document.body.appendChild(dialog);}
    renderReceipt(dialog,result && result.acceptance || result,{onClose:()=>dialog.close()});
    if(!dialog.open && typeof dialog.showModal==='function')dialog.showModal();
    // Unknown receipts still need a close control, but never a submit action.
    if(!acceptedOperation(result && result.acceptance || result)){
      const close=make('button','button','Закрыть');close.type='button';close.onclick=()=>dialog.close();dialog.appendChild(close);
    }
  }

  function refreshFeedbackReceipt(result) {
    const dialog=document.getElementById('operator-feedback-receipt');
    if(dialog && dialog.open)showFeedbackReceipt(result);
  }

  window.OperatorAcceptance = Object.freeze({acceptedOperation, renderReceipt, renderState, stateNode, readSameOperation, journalLink, readExternalNative, onceExternal, externalHttpError,readFeedbackNative,withFeedbackLock,onceFeedback,showFeedbackReceipt,refreshFeedbackReceipt});
}());
