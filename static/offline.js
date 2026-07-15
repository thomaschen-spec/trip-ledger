// 離線記帳佇列：手動記帳表單斷網時先存 IndexedDB，回網路後自動補傳到 /trip/<id>/api/manual。
// 只處理 manual 模式（表單有 id="offline-manual-form"），edit 模式維持原本整頁提交不受影響。
(function () {
  const DB_NAME = "tripledger";
  const STORE = "pending_receipts";
  const DB_VERSION = 1;
  const MAX_FAIL_COUNT = 5;

  function openDB() {
    return new Promise((resolve, reject) => {
      if (!("indexedDB" in window)) {
        reject(new Error("no-indexeddb"));
        return;
      }
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: "client_uuid" });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function idbPut(record) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put(record);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  async function idbGetAll() {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  }

  async function idbDelete(client_uuid) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).delete(client_uuid);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  function collectFormData(form) {
    const fd = new FormData(form);
    return {
      client_uuid: crypto.randomUUID(),
      trip_id: form.dataset.tripId,
      amount: fd.get("amount"),
      tax: fd.get("tax"),
      category: fd.get("category"),
      payment_method: fd.get("payment_method"),
      txn_date: fd.get("txn_date"),
      store_name: fd.get("store_name"),
      payer_id: fd.get("payer_id"),
      split_ids: fd.getAll("split_ids"),
      created_at: Date.now(),
      failCount: 0,
    };
  }

  function toApiPayload(record) {
    return {
      client_uuid: record.client_uuid,
      amount: record.amount,
      tax: record.tax,
      category: record.category,
      payment_method: record.payment_method,
      txn_date: record.txn_date,
      store_name: record.store_name,
      payer_id: record.payer_id,
      split_ids: (record.split_ids || []).map((x) => parseInt(x, 10)).filter((x) => !Number.isNaN(x)),
    };
  }

  function postManual(tripId, payload) {
    return fetch(`/trip/${tripId}/api/manual`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function showBanner(msg) {
    const wrap = document.querySelector(".wrap");
    if (!wrap) return;
    let el = document.getElementById("offline-banner");
    if (!el) {
      el = document.createElement("div");
      el.id = "offline-banner";
      el.className = "card taped";
      el.style.textAlign = "center";
      wrap.prepend(el);
    }
    el.textContent = msg;
  }

  async function renderPendingList() {
    const container = document.getElementById("offline-pending");
    if (!container) return;
    const tripId = container.dataset.tripId;
    let records = [];
    try {
      records = (await idbGetAll()).filter((r) => String(r.trip_id) === String(tripId));
    } catch (e) {
      return;
    }
    if (!records.length) {
      container.style.display = "none";
      container.innerHTML = "";
      return;
    }
    container.style.display = "block";
    const items = records
      .map((r) => {
        const dead = (r.failCount || 0) >= MAX_FAIL_COUNT;
        const amt = Number(r.amount || 0).toLocaleString();
        return (
          '<div class="card" style="padding:10px 12px;">' +
          '<span class="stamp' + (dead ? " red" : "") + '">' + (dead ? "⚠️ 補傳失敗" : "⏳ 尚未同步") + "</span> " +
          escapeHtml(r.store_name || "未命名") + " — ¥" + amt +
          "</div>"
        );
      })
      .join("");
    container.innerHTML =
      '<h2 class="serif" style="font-size:0.95rem;margin:0 0 8px;">📥 待同步（' + records.length + '）</h2>' + items;
  }

  async function flushQueue() {
    let records;
    try {
      records = await idbGetAll();
    } catch (e) {
      return;
    }
    if (!records.length) return;

    let anySynced = false;
    let anyNeedsLogin = false;

    for (const record of records) {
      if ((record.failCount || 0) >= MAX_FAIL_COUNT) continue;
      let resp;
      try {
        resp = await postManual(record.trip_id, toApiPayload(record));
      } catch (e) {
        continue; // 還是連不上網，留著下次再試，不算失敗次數
      }
      if (resp.status === 401) {
        anyNeedsLogin = true;
        continue;
      }
      if (resp.ok) {
        await idbDelete(record.client_uuid);
        anySynced = true;
      } else {
        record.failCount = (record.failCount || 0) + 1;
        try {
          await idbPut(record);
        } catch (e) {
          /* ignore */
        }
      }
    }

    if (anyNeedsLogin) {
      showBanner("有帳目還沒補傳，請重新登入，登入後會自動補傳。");
    }
    if (anySynced) {
      location.reload();
    } else {
      renderPendingList();
    }
  }

  function hookManualForm() {
    const form = document.getElementById("offline-manual-form");
    if (!form) return;
    if (!("indexedDB" in window)) return; // 舊瀏覽器：不攔截，走原本整頁表單提交

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const tripId = form.dataset.tripId;
      const record = collectFormData(form);
      const payload = toApiPayload(record);

      let resp;
      try {
        resp = await postManual(tripId, payload);
      } catch (e) {
        // 網路不通：存進 IndexedDB，之後自動補傳
        try {
          await idbPut(record);
        } catch (idbErr) {
          alert("離線儲存失敗，請有網路時再試一次。");
          return;
        }
        sessionStorage.setItem("offline_saved_msg", "沒有網路，已經先存在手機裡，等有網路會自動補傳。");
        location.href = `/trip/${tripId}/dashboard`;
        return;
      }

      if (resp.ok) {
        location.href = `/trip/${tripId}/dashboard`;
        return;
      }
      if (resp.status === 401) {
        try {
          await idbPut(record);
        } catch (e) {
          /* ignore */
        }
        sessionStorage.setItem("offline_saved_msg", "已先存在手機裡，重新登入後會自動補傳。");
        location.href = `/trip/${tripId}/dashboard`;
        return;
      }
      alert("這筆記帳送出失敗，請檢查欄位再試一次。");
    });
  }

  function showSavedBannerIfAny() {
    const msg = sessionStorage.getItem("offline_saved_msg");
    if (msg) {
      sessionStorage.removeItem("offline_saved_msg");
      showBanner(msg);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    hookManualForm();
    showSavedBannerIfAny();
    renderPendingList();
    flushQueue();
  });

  window.addEventListener("online", () => {
    flushQueue();
  });
})();
