/* Settings host owns submissions; opaque identity survives close/reload. */
(function () {
  "use strict";
  const ID = /^opsku_[a-f0-9]{32}$/;
  function create(config, container, expectedRevision, send) {
    const storageKey = "wbc.nomenclature.operations." + config.operator_actor_scope;
    const lockKey = storageKey + ":mutation";
    let ids=[];
    function readIds() {
      try {
        const saved=JSON.parse(localStorage.getItem(storageKey) || "[]");
        if (!Array.isArray(saved) || saved.some(id=>!ID.test(id))) throw new Error("invalid identity store");
        return [...new Set(saved)];
      } catch (_) {
        throw new Error("Не удалось прочитать сохранённые номера операций. Новая отправка остановлена.");
      }
    }
    try {ids=readIds();} catch (_) {}
    const unknown = new Set(ids);
    const known = new Map();
    const previous = new Map();
    let busy = false, generation=0;
    // Only the actor/account-scoped lock holder changes the durable registry.
    // Other tabs may remove only their own explicitly rejected request.
    function refreshIds() {
      ids=readIds();
      // A journal URL is a readback request, not evidence of a new submission.
      const queryId = new URL(location.href).searchParams.get("operation_id");
      if (ID.test(queryId || "") && !ids.includes(queryId)) ids.push(queryId);
      for (const id of unknown) if (!ids.includes(id)) unknown.delete(id);
      for (const id of ids) if (!known.has(id)) unknown.add(id);
    }
    function persist() { localStorage.setItem(storageKey, JSON.stringify(ids)); }
    function locked(callback) {
      if (!navigator.locks || !navigator.locks.request) {
        throw new Error("Браузер не может безопасно согласовать отправку между вкладками. Новая отправка остановлена.");
      }
      return navigator.locks.request(lockKey, {mode:"exclusive"}, callback);
    }
    function show(receipt) {
      container.hidden = false;
      OperatorAcceptance.renderReceipt(container, receipt, {onClose: () => {container.hidden = true;}});
    }
    function showUnknown(id) {
      container.hidden = false;
      OperatorAcceptance.renderState(container, null);
      const button = document.createElement("button");
      button.type = "button"; button.textContent = "Проверить сохранение";
      button.addEventListener("click", () => restore());
      container.appendChild(button);
    }
    async function readReceipt(id,capturedGeneration) {
      const knownBeforeRead=known.get(id);
      const read = await OperatorAcceptance.readSameOperation({operation_id:id, domain:"nomenclature"},
        () => send(config.nomenclature_operations_path + id, {headers:{Accept:"application/json"}}));
      if (read.status === "accepted") {
        unknown.delete(id); known.set(id, read.operation); if (capturedGeneration===generation) show(read.operation);
        return read.operation;
      }
      // A GET started before the successful POST readback may return an older
      // 404. Keep the newer exact accepted receipt; it is append-only on server.
      if (known.has(id) && known.get(id)!==knownBeforeRead) return known.get(id);
      unknown.add(id); if (capturedGeneration===generation) showUnknown(id); return null;
    }
    async function recover(id) {return readReceipt(id,++generation);}
    function handles(url, options) {
      const method = String(options && options.method || "GET").toUpperCase();
      const target=new URL(url,location.href);
      if (["1","true","yes","on"].includes(String(target.searchParams.get("dry_run") || "").toLowerCase())) return false;
      if (url.startsWith(config.nomenclature_operations_path)) return false;
      return ["POST","PATCH","DELETE"].includes(method)
        && (url === config.nomenclature_path || url.startsWith(config.nomenclature_path + "/")
          || url === config.sku_groups_path || url.startsWith(config.sku_groups_path + "/"));
    }
    async function mutate(url, options) {
      if (busy) throw new Error("Проверяем сохранение. Повторно отправлять не нужно.");
      busy = true;
      try {
        // Capture the caller's exact operands and before-image synchronously.
        // Waiting for another tab or GET must not pair old input with a newer
        // revision, or read a FormData/options object changed after the click.
        const revision = expectedRevision(url);
        const expected = revision == null ? revision : JSON.parse(JSON.stringify(revision));
        const opts = {...options, headers:{...(options.headers || {})}};
        const isForm = options.body instanceof FormData;
        if (isForm) {
          opts.body = new FormData();
          for (const [name,value] of options.body.entries()) opts.body.append(name,value);
        }
        return await locked(async () => {
        // A tab opened before another submit must read the fresh registry after
        // acquiring the lock, then recover unknown identities by GET only.
        refreshIds();
        const capturedGeneration=++generation;
        for (const id of [...unknown]) await readReceipt(id,capturedGeneration);
        if (unknown.size) {showUnknown(unknown.values().next().value); throw new Error("Результат предыдущей операции пока неизвестен. Проверьте её сохранение.");}
        let content=opts.body;
        if (isForm) {
          content=await Promise.all([...content.entries()].map(async ([name,value]) => {
            if (!(value instanceof File)) return [name,String(value)];
            const hash=await crypto.subtle.digest("SHA-256",await value.arrayBuffer());
            return [name,value.name,Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,"0")).join("")];
          }));
        }
        const key = JSON.stringify([url,opts.method,expected,content]);
        if (previous.has(key)) {const result=previous.get(key); show(result.acceptance); return result;}
        if (!crypto.randomUUID) throw new Error("Браузер не может сохранить номер операции.");
        container.hidden=false; OperatorAcceptance.renderState(container,null);
        const id = "opsku_" + crypto.randomUUID().replace(/-/g, "");
        ids.push(id); unknown.add(id);
        // Fail before POST if durable browser identity cannot be retained.
        persist();
        opts.headers["X-Operator-Request-ID"] = id;
        if (isForm) {
          const form=opts.body;
          if (expected !== undefined && expected !== null) form.append("operator_expected_revision",JSON.stringify(expected));
          opts.body=form;
        } else if (expected !== undefined && expected !== null) opts.headers["X-Operator-Expected-Revision"] = JSON.stringify(expected);
        if (String(opts.method).toUpperCase() !== "DELETE" && !isForm) {
          const body = opts.body ? JSON.parse(opts.body) : {};
          body._operator_request_id=id;
          if (expected !== undefined && expected !== null) body._operator_expected_revision=expected;
          opts.headers["Content-Type"]="application/json"; opts.body=JSON.stringify(body);
        }
        let payload;
        try {payload = await send(url,opts);}
        catch (error) {
          // Only explicit business validation proves no source commit. A network
          // failure, proxy HTML, timeout or 5xx always reads this exact identity.
          if ([400,404].includes(error.httpStatus) && error.sourceNotSaved===true && error.operationId===id) {
            unknown.delete(id); ids=ids.filter(value=>value!==id); persist(); container.hidden=true; throw error;
          }
          const receipt=await readReceipt(id,capturedGeneration);
          if (receipt) return {status:"ok",acceptance:receipt};
          throw new Error("Подтверждение ещё не получено. Повторно отправлять не нужно.");
        }
        const read=await OperatorAcceptance.readSameOperation({operation_id:id,domain:"nomenclature"},async()=>({acceptance:payload.acceptance}));
        if (read.status !== "accepted") {
          const receipt=await readReceipt(id,capturedGeneration);
          if (receipt) return {status:"ok",acceptance:receipt};
          throw new Error("Подтверждение ещё не получено. Повторно отправлять не нужно.");
        }
        unknown.delete(id); known.set(id,read.operation); show(read.operation);
        previous.set(key,payload); return payload;
      });} finally {busy=false;}
    }
    async function restore() {
      try {
        refreshIds();
        const capturedGeneration=++generation;
        for (const id of [...ids]) {
          await readReceipt(id,capturedGeneration);
          if (capturedGeneration!==generation) return;
        }
        if (unknown.size) showUnknown(unknown.values().next().value);
      } catch (_) {showUnknown(null);}
    }
    function restoreOnReturn() {
      restore().catch(() => {container.hidden=false; OperatorAcceptance.renderState(container,null);});
    }
    window.addEventListener("storage", event => {if (event.key===storageKey || event.key===null) restoreOnReturn();});
    window.addEventListener("pageshow", restoreOnReturn);
    window.addEventListener("focus", restoreOnReturn);
    document.addEventListener("visibilitychange", () => {if (document.visibilityState==="visible") restoreOnReturn();});
    return Object.freeze({handles,mutate,recover,restore,sourceRefreshed:()=>previous.clear()});
  }
  window.OperatorNomenclature=Object.freeze({create});
}());
