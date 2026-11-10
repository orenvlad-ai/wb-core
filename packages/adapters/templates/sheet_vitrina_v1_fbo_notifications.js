  (function operationalNotifications() {
    const root = document.querySelector("[data-notifications]");
    const endpoint = WEB_VITRINA_CONFIG.fbo_notifications_path;
    const allowed = Array.isArray(WEB_VITRINA_CONFIG.allowed_tabs) && WEB_VITRINA_CONFIG.allowed_tabs.includes("factory-order");
    if (!root || !endpoint || !allowed) return;
    root.hidden = false;
    const bell = root.querySelector("[data-notification-bell]");
    const panel = root.querySelector("[data-notification-panel]");
    const dot = root.querySelector("[data-notification-dot]");
    const list = root.querySelector("[data-notification-list]");
    const feedback = root.querySelector("[data-notification-feedback]");
    const readKey = "wbc:operational-notifications:seen:v1:" + String(WEB_VITRINA_CONFIG.user_config_key || "local_operator");
    let items = [], seen = [], loading = false, available = false;
    try { const saved = JSON.parse(localStorage.getItem(readKey) || "[]"); if (Array.isArray(saved)) seen = saved; } catch (_) {}
    function marker(item) { return JSON.stringify([String(item.id || ""), String(item.reason_revision || "")]); }
    function markSeen() {
      seen = Array.from(new Set(seen.concat(items.map(marker)))).slice(-1000);
      try { localStorage.setItem(readKey, JSON.stringify(seen)); } catch (_) {}
      dot.hidden = true;
    }
    function close(restoreFocus) {
      panel.hidden = true; bell.setAttribute("aria-expanded", "false");
      if (restoreFocus) bell.focus();
    }
    function number(value) { return value === null || value === undefined ? "—" : Number(value).toLocaleString("ru-RU"); }
    function render() {
      root.querySelector("[data-notification-count]").textContent = available ? "Требуют действия · " + items.length : "Состояние не загружено";
      list.replaceChildren();
      items.forEach(function(item) {
        const article = document.createElement("article"); article.className = "notification-item";
        article.dataset.notificationId = String(item.id || "");
        const title = document.createElement("h3"); title.className = "notification-title";
        const kind = String(item.type || item.status || item.reason || "");
        title.textContent = item.title || (kind === "missing_source" ? "Укажите склад отгрузки" : kind === "reservation_pending" ? "Завершите резервирование" : kind === "shortage" ? "Не хватает товара для резерва" : kind === "fbo_cost_unallocated" ? "Уточните распределение стоимости" : "Поставка требует проверки");
        article.appendChild(title);
        const supply = item.supply || {};
        const detail = document.createElement("p"); detail.className = "notification-detail";
        const destination = item.warehouse_display || item.destination || supply.warehouse_display;
        const qty = item.quantity_added ?? item.quantity ?? supply.quantity_added;
        const facts = ["Поставка № " + String(item.number_label || supply.number_label || item.supply_id || "—"), destination].filter(Boolean).join(" · ");
        const shortages = Array.isArray(item.shortages) ? item.shortages : [];
        const shortageText = shortages.map(function(row) { return "SKU " + String(row.nm_id || row.vendor_code || "—") + ": не хватает " + number(row.missing) + " шт."; }).join("\n");
        detail.textContent = [facts, qty !== undefined && qty !== null ? number(qty) + " шт." : "", item.detail || (["missing_source", "reservation_pending"].includes(kind) ? "Товар ещё не зарезервирован." : ""), shortageText].filter(Boolean).join("\n");
        article.appendChild(detail);
        if (item.supply_id) {
          const action = document.createElement("button"); action.type = "button";
          action.textContent = kind === "fbo_cost_unallocated" ? "Открыть" : "Уточнить";
          action.addEventListener("click", function() { openSupply(String(item.supply_id)); });
          article.appendChild(action);
        }
        list.appendChild(article);
      });
      dot.hidden = !items.some(function(item) { return !seen.includes(marker(item)); });
      if (!panel.hidden) markSeen();
    }
    async function load() {
      if (loading) return;
      loading = true;
      feedback.textContent = "Читаем уведомления…";
      try {
        const response = await fetch(endpoint, {headers: {Accept: "application/json"}, cache: "no-store"});
        const payload = await response.json();
        if (!response.ok) throw new Error("HTTP " + response.status);
        const rows = Array.isArray(payload.notifications) ? payload.notifications : [];
        const unique = new Map();
        rows.forEach(function(item) { if (item && item.id) unique.set(String(item.id), item); });
        items = Array.from(unique.values()); available = true;
        feedback.textContent = items.length ? "" : ["disabled", "schema_absent"].includes(payload.status) ? "Уведомления FBO пока недоступны." : "Нет уведомлений, требующих действия.";
        render();
      } catch (_) {
        feedback.textContent = "Не удалось обновить уведомления. Откройте панель ещё раз для проверки.";
        // Keep the last known unresolved work visible on a read failure.
        render();
      } finally { loading = false; }
    }
    function openSupply(supplyId) {
      const button = document.querySelector('[data-unified-tab-button="factory-order"]');
      const frame = document.querySelector('[data-operator-embed-frame="factory-order"]');
      if (!button || !frame) { feedback.textContent = "Раздел поставок недоступен."; return; }
      close(false); button.click();
      let operatorReady = false;
      try {
        // A child redirect does not update the iframe's src attribute. Check
        // the live document, which must also have advertised WB-detail support.
        const current = new URL(frame.contentWindow.location.href);
        const operator = new URL(WEB_VITRINA_CONFIG.operator_path, location.origin);
        operatorReady = frame.dataset.notificationReady === "true" &&
          current.origin === operator.origin && current.pathname === operator.pathname;
      } catch (_) {}
      if (operatorReady && frame.contentWindow) {
        frame.contentWindow.postMessage({type: "wbc_open_notification_supply", supply_id: supplyId}, location.origin);
      } else {
        const url = new URL(WEB_VITRINA_CONFIG.operator_path, location.origin);
        url.searchParams.set("embedded_tab", "factory-order"); url.searchParams.set("supply_mode", "wildberries");
        url.searchParams.set("notification_supply_id", supplyId);
        frame.dataset.notificationReady = "false";
        frame.setAttribute("src", url.toString());
      }
    }
    bell.addEventListener("click", function() {
      if (!panel.hidden) { close(false); return; }
      panel.hidden = false; bell.setAttribute("aria-expanded", "true"); markSeen(); render(); void load();
    });
    root.querySelector("[data-notification-close]").addEventListener("click", function() { close(true); });
    document.addEventListener("pointerdown", function(event) { if (!root.contains(event.target)) close(false); });
    document.addEventListener("keydown", function(event) { if (event.key === "Escape" && !panel.hidden) { close(true); event.preventDefault(); } });
    window.addEventListener("message", function(event) {
      const frame = document.querySelector('[data-operator-embed-frame="factory-order"]');
      if (event.origin !== location.origin || !frame || event.source !== frame.contentWindow) return;
      if ((event.data || {}).type === "wbc_notification_supply_ready") {
        frame.dataset.notificationReady = event.data.can_open_notification_supply === true ? "true" : "false";
      }
      if ((event.data || {}).type === "wbc_fbo_notifications_changed") void load();
      if ((event.data || {}).type === "sheet_vitrina_v1_operator_embed_pointerdown") close(false);
    });
    window.addEventListener("storage", function(event) {
      if (event.key !== readKey) return;
      try { const value = JSON.parse(event.newValue || "[]"); if (Array.isArray(value)) seen = value; } catch (_) {}
      render();
    });
    window.addEventListener("focus", function() { void load(); });
    void load();
  }());
