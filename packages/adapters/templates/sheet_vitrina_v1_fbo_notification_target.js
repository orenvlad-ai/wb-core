      async function openNotificationSupply(supplyId) {
        if (!supplyId || wbSupplyFfOriginSaving) return;
        if (config.independent_supplier_read) {
          // This document cannot open WB details. Keep the target across its
          // redirect, including messages from an already open older parent.
          const target = new URL("/sheet-vitrina-v1/operator", window.location.origin);
          target.searchParams.set("embedded_tab", "factory-order");
          target.searchParams.set("supply_mode", "wildberries");
          target.searchParams.set("notification_supply_id", String(supplyId));
          window.location.replace(target.pathname + target.search);
          return;
        }
        activateTab("factory-order"); activateSupplyMode("wildberries");
        await openWbSupplyDetail(String(supplyId));
        const target = document.getElementById("wbSupplyFfOriginPanel");
        if (target) { target.scrollIntoView({block: "center"}); if (wbSupplyFfOriginSelect) wbSupplyFfOriginSelect.focus(); }
      }
      window.addEventListener("message", function(event) {
        if (event.origin !== window.location.origin || event.source !== window.parent || window.parent === window) return;
        if ((event.data || {}).type === "wbc_open_notification_supply") void openNotificationSupply(String(event.data.supply_id || ""));
      });
