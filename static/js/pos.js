/* POS screen: barcode scanning, live search, cart, payment, checkout. */
(function () {
  const cfg = window.POS_CONFIG || { currency: "KES", taxRate: 0 };
  const base = cfg.baseCurrency || cfg.currency;
  const cart = []; // { id, barcode, name, price, quantity, stock }

  // FX rates as foreign-per-base (how many of a currency equal 1 base unit).
  // Refreshed from the server, which fetches live and caches for offline use.
  const rates = { [base]: 1 };
  const rateFor = (cur) => Number(rates[cur] || 1);

  const scanInput = document.getElementById("scan-input");
  const scanForm = document.getElementById("scan-form");
  const scanMsg = document.getElementById("scan-msg");
  const grid = document.getElementById("product-grid");
  const cartBody = document.getElementById("cart-body");
  const btnPay = document.getElementById("btn-pay");
  const btnClear = document.getElementById("btn-clear");

  const money = (n, cur) => `${cur || cfg.currency} ${Number(n).toFixed(2)}`;

  let allProducts = [];  // full active product list for the grid

  // --- Exchange rates --------------------------------------------------
  async function loadRates() {
    try {
      const res = await fetch("/api/rates");
      const data = await res.json();
      Object.assign(rates, data.rates || {});
      rates[base] = 1;
    } catch (_) {
      /* offline: keep whatever we have (at least base = 1) */
    }
  }

  // --- Product grid ----------------------------------------------------
  async function loadProducts() {
    try {
      const res = await fetch("/api/products/grid");
      allProducts = await res.json();
      renderGrid(allProducts);
    } catch (_) {
      grid.innerHTML = `<div class="grid-empty muted">Couldn't load products.</div>`;
    }
  }

  function renderGrid(items) {
    if (!items.length) {
      grid.innerHTML = `<div class="grid-empty muted">No products. Add some on the Products page.</div>`;
      return;
    }
    grid.innerHTML = "";
    items.forEach((p) => {
      const out = p.quantity < 1;
      const card = document.createElement("button");
      card.type = "button";
      card.className = "product-card" + (out ? " out" : "");
      card.disabled = out;
      const thumb = p.image_url
        ? `<img class="pc-img" src="${escapeHtml(p.image_url)}" alt="" loading="lazy">`
        : `<span class="pc-img pc-img-empty">${escapeHtml(p.name.slice(0, 1).toUpperCase())}</span>`;
      card.innerHTML = `
        ${thumb}
        <span class="pc-name">${escapeHtml(p.name)}</span>
        <span class="pc-price">${money(p.price)}${p.uom ? ` <span class="pc-uom">/ ${escapeHtml(p.uom)}</span>` : ""}</span>
        <span class="pc-stock">${out ? "Out of stock" : "In stock: " + p.quantity}</span>`;
      if (!out) {
        card.addEventListener("click", () => { addToCart(p); scanInput.focus(); });
      }
      grid.appendChild(card);
    });
  }

  function filterGrid(q) {
    q = q.trim().toLowerCase();
    if (!q) { renderGrid(allProducts); return; }
    renderGrid(allProducts.filter(
      (p) => p.name.toLowerCase().includes(q) || (p.barcode || "").toLowerCase().includes(q)
    ));
  }

  // --- Scanning / filtering -------------------------------------------
  // A USB barcode scanner types the code then presses Enter (submit).
  scanForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = scanInput.value.trim();
    if (!code) return;
    // If it matches a single grid product by name, add that; else treat as barcode.
    const matches = allProducts.filter(
      (p) => p.barcode === code ||
             p.name.toLowerCase().includes(code.toLowerCase())
    );
    if (matches.length === 1) {
      addToCart(matches[0]);
    } else {
      await addByBarcode(code);
    }
    scanInput.value = "";
    filterGrid("");
  });

  // Typing filters the grid live.
  scanInput.addEventListener("input", () => filterGrid(scanInput.value));

  async function addByBarcode(code) {
    try {
      const res = await fetch(`/api/product/${encodeURIComponent(code)}`);
      if (res.status === 404) { flashScan(`No product for "${code}"`); return; }
      const data = await res.json();
      if (data.found) addToCart(data.product);
    } catch (_) {
      flashScan("Lookup failed — is the server running?");
    }
  }

  function flashScan(msg) {
    scanMsg.textContent = msg;
    scanMsg.classList.add("show");
    setTimeout(() => { scanMsg.textContent = ""; scanMsg.classList.remove("show"); }, 2500);
  }

  // --- Cart ------------------------------------------------------------
  function qtyFmt(n) { return (Math.round((n || 0) * 1000) / 1000).toString(); }

  function addToCart(p) {
    // No selling without an open shift — prompt to start one.
    if (!window.HAS_OPEN_SHIFT) {
      const m = document.getElementById("start-shift-modal");
      if (m) m.hidden = false;
      flashScan("Open a shift before selling.");
      return;
    }
    const existing = cart.find((c) => c.id === p.id);
    if (existing) {
      if (existing.quantity + 1 > p.quantity) { flashScan(`Only ${qtyFmt(p.quantity)} in stock`); return; }
      existing.quantity += 1;
    } else {
      if (p.quantity < 1) { flashScan(`${p.name} is out of stock`); return; }
      cart.push({
        id: p.id, barcode: p.barcode, name: p.name, price: p.price,
        quantity: 1, stock: p.quantity, fractional: !!p.fractional,
      });
    }
    renderCart();
  }

  function changeQty(id, delta) {
    const item = cart.find((c) => c.id === id);
    if (!item) return;
    const next = item.quantity + delta;
    if (next <= 0) { removeItem(id); return; }
    if (next > item.stock) { flashScan(`Only ${qtyFmt(item.stock)} in stock`); return; }
    item.quantity = next;
    renderCart();
  }

  // Set an exact quantity (used when the cashier types into the qty box).
  function setQty(id, raw, commit) {
    const item = cart.find((c) => c.id === id);
    if (!item) return;
    let q = parseFloat(raw);
    if (isNaN(q)) { if (commit) renderCart(); return; }
    if (!item.fractional) q = Math.round(q);
    if (q > item.stock) { flashScan(`Only ${qtyFmt(item.stock)} in stock`); q = item.stock; }
    if (commit && q <= 0) { removeItem(id); return; }
    item.quantity = q > 0 ? q : item.quantity;
    if (commit) {
      renderCart();
    } else {
      const cell = cartBody.querySelector(`.line-cell[data-id="${id}"]`);
      if (cell) cell.textContent = money(item.price * item.quantity);
      updateTotals();
    }
  }

  function removeItem(id) {
    const i = cart.findIndex((c) => c.id === id);
    if (i > -1) cart.splice(i, 1);
    renderCart();
  }

  function renderCart() {
    if (cart.length === 0) {
      cartBody.innerHTML = `<tr class="empty-row"><td colspan="5" class="muted">Cart is empty — scan an item to begin.</td></tr>`;
      btnPay.disabled = true;
    } else {
      cartBody.innerHTML = "";
      cart.forEach((item) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${escapeHtml(item.name)}</td>
          <td>${money(item.price)}</td>
          <td>
            <div class="qty-stepper">
              <button class="qty-btn" data-act="dec" data-id="${item.id}">−</button>
              <input class="qty-input" type="number" data-id="${item.id}"
                     value="${qtyFmt(item.quantity)}" min="0"
                     step="${item.fractional ? '0.001' : '1'}">
              <button class="qty-btn" data-act="inc" data-id="${item.id}">+</button>
            </div>
          </td>
          <td class="line-cell" data-id="${item.id}">${money(item.price * item.quantity)}</td>
          <td><button class="cart-remove" data-act="rm" data-id="${item.id}">×</button></td>`;
        cartBody.appendChild(tr);
      });
      btnPay.disabled = false;
    }
    updateTotals();
  }

  cartBody.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const id = Number(btn.dataset.id);
    if (btn.dataset.act === "inc") changeQty(id, 1);
    else if (btn.dataset.act === "dec") changeQty(id, -1);
    else if (btn.dataset.act === "rm") removeItem(id);
  });
  // Typing an exact quantity: update totals live, normalise on blur/enter.
  cartBody.addEventListener("input", (e) => {
    const inp = e.target.closest(".qty-input");
    if (inp) setQty(Number(inp.dataset.id), inp.value, false);
  });
  cartBody.addEventListener("change", (e) => {
    const inp = e.target.closest(".qty-input");
    if (inp) setQty(Number(inp.dataset.id), inp.value, true);
  });

  function totals() {
    const subtotal = cart.reduce((s, c) => s + c.price * c.quantity, 0);
    const tax = subtotal * (cfg.taxRate || 0);
    return { subtotal, tax, total: subtotal + tax };
  }

  function updateTotals() {
    const t = totals();
    document.getElementById("t-subtotal").textContent = money(t.subtotal);
    const taxEl = document.getElementById("t-tax");
    if (taxEl) taxEl.textContent = money(t.tax);
    document.getElementById("t-total").textContent = money(t.total);
  }

  btnClear.addEventListener("click", () => { cart.length = 0; renderCart(); scanInput.focus(); });

  // --- Loyalty customer (searchable picker by phone) -------------------
  let customer = null;  // { id, phone, name, points } once selected
  const custPhone = document.getElementById("cust-phone");
  const btnCustFind = document.getElementById("btn-cust-find");
  const custMsg = document.getElementById("customer-msg");
  const custSuggestions = document.getElementById("cust-suggestions");
  const ncModal = document.getElementById("new-cust-modal");
  const ncPhoneLabel = document.getElementById("nc-phone-label");
  const ncName = document.getElementById("nc-name");
  const ncError = document.getElementById("nc-error");

  function renderCustomer() {
    custMsg.textContent = customer ? `Customer: ${customer.name || customer.phone}` : "";
  }

  function hideSuggestions() {
    custSuggestions.hidden = true;
    custSuggestions.innerHTML = "";
    custPhone.setAttribute("aria-expanded", "false");
  }

  function renderSuggestions(list) {
    if (!list.length) { hideSuggestions(); return; }
    custSuggestions.innerHTML = "";
    list.forEach((c) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "cust-suggestion";
      item.innerHTML =
        `<span>${escapeHtml(c.name || "—")}</span>` +
        `<span class="muted">${escapeHtml(c.phone)}</span>`;
      item.addEventListener("click", () => attachCustomer(c));
      custSuggestions.appendChild(item);
    });
    custSuggestions.hidden = false;
    custPhone.setAttribute("aria-expanded", "true");
  }

  function attachCustomer(c) {
    customer = c;
    custPhone.value = c.name ? `${c.name} · ${c.phone}` : c.phone;
    hideSuggestions();
    renderCustomer();
  }

  function detachCustomer() {
    customer = null;
    custPhone.value = "";
    hideSuggestions();
    renderCustomer();
  }

  async function findCustomer() {
    const phone = custPhone.value.trim();
    if (!phone) { custMsg.textContent = "Enter a phone number."; return; }
    try {
      const res = await fetch(`/api/customer/lookup?phone=${encodeURIComponent(phone)}`);
      const data = await res.json();
      if (data.found) { attachCustomer(data.customer); return; }
      ncPhoneLabel.textContent = data.phone || phone;
      ncName.value = ""; ncError.textContent = "";
      ncModal.hidden = false; ncName.focus();
    } catch (_) {
      custMsg.textContent = "Lookup failed — is the server running?";
    }
  }

  btnCustFind.addEventListener("click", findCustomer);

  // Search saved customers by phone (or name) as the cashier types, and pick one.
  let custTimer = null;
  custPhone.addEventListener("input", () => {
    customer = null;                       // typing changes the selection
    clearTimeout(custTimer);
    const q = custPhone.value.trim();
    if (q.length < 2) { hideSuggestions(); custMsg.textContent = ""; return; }
    custTimer = setTimeout(async () => {
      if (custPhone.value.trim() !== q) return;
      try {
        const res = await fetch(`/api/customer/search?q=${encodeURIComponent(q)}`);
        const data = await res.json();
        renderSuggestions(data.results || []);
        custMsg.textContent = (data.results || []).length ? "" : "No saved customer — click Find to add.";
      } catch (_) { hideSuggestions(); }
    }, 250);
  });
  custPhone.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const first = custSuggestions.querySelector(".cust-suggestion");
    if (!custSuggestions.hidden && first) first.click();   // pick the top match
    else findCustomer();
  });
  // Close the dropdown when clicking outside the picker.
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".cust-picker")) hideSuggestions();
  });

  document.getElementById("btn-nc-cancel").addEventListener("click", () => {
    ncModal.hidden = true; custPhone.focus();
  });
  document.getElementById("btn-nc-save").addEventListener("click", async () => {
    ncError.textContent = "";
    try {
      const res = await fetch("/api/customer/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone: custPhone.value.trim(), name: ncName.value.trim() }),
      });
      const data = await res.json();
      if (!data.ok) { ncError.textContent = data.error || "Could not add customer."; return; }
      ncModal.hidden = true;
      attachCustomer(data.customer);
    } catch (_) {
      ncError.textContent = "Could not reach the server.";
    }
  });

  // --- Payment modal ---------------------------------------------------
  const payModal = document.getElementById("pay-modal");
  const tendered = document.getElementById("tendered");
  const changeDue = document.getElementById("change-due");
  const payError = document.getElementById("pay-error");
  const payCurrencySel = document.getElementById("pay-currency");
  const convertLine = document.getElementById("convert-line");
  const tenderCur = document.getElementById("tender-cur");
  const mpesaFields = document.getElementById("mpesa-fields");
  const bankFields = document.getElementById("bank-fields");
  const mpesaPhone = document.getElementById("mpesa-phone");
  const mpesaStatus = document.getElementById("mpesa-status");
  const bankRef = document.getElementById("bank-ref");
  const bankInfo = document.getElementById("bank-info");
  const btnStk = document.getElementById("btn-stk");
  const confirmBtn = document.getElementById("btn-confirm-pay");
  let method = "cash";
  let payCurrency = base;  // the currency the customer pays in
  let mpesaCheckoutId = null;  // set after an STK prompt is accepted
  let mpesaPaid = false;       // true once Daraja confirms payment
  let pollTimer = null;        // status-poll handle

  const cartPayload = () =>
    cart.map((c) => ({ id: c.id, name: c.name, quantity: c.quantity }));

  function stopPolling() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function resetMpesa() {
    stopPolling();
    mpesaCheckoutId = null;
    mpesaPaid = false;
    if (mpesaStatus) { mpesaStatus.textContent = ""; mpesaStatus.className = "mpesa-status muted small"; }
    if (btnStk) { btnStk.disabled = false; btnStk.textContent = "Send M-Pesa prompt"; }
  }

  btnPay.addEventListener("click", () => {
    if (cart.length === 0) return;
    if (btnPay.dataset.noShift) {
      flashScan("Open a shift before charging.");
      window.location.href = "/shift";
      return;
    }
    payCurrency = base;
    if (payCurrencySel) payCurrencySel.value = base;
    tendered.value = "";
    bankRef.value = "";
    mpesaPhone.value = "";
    payError.textContent = "";
    populateBankInfo();
    setMethod("cash");
    updatePayDisplay();
    payModal.hidden = false;
    tendered.focus();
  });

  function populateBankInfo() {
    const b = (cfg.bank || {});
    const lines = [];
    if (b.name) lines.push(`<strong>${escapeHtml(b.name)}</strong>`);
    if (b.account_name) lines.push(`Name: ${escapeHtml(b.account_name)}`);
    if (b.account_number) lines.push(`A/C: ${escapeHtml(b.account_number)}`);
    if (b.branch) lines.push(`Branch: ${escapeHtml(b.branch)}`);
    if (b.paybill) lines.push(`Paybill/Till: ${escapeHtml(b.paybill)}`);
    bankInfo.innerHTML = lines.length
      ? "Customer pays to:<br>" + lines.join("<br>")
      : "Record the customer's bank/transfer reference below.";
  }

  if (payCurrencySel) {
    payCurrencySel.addEventListener("change", () => {
      payCurrency = payCurrencySel.value || base;
      updatePayDisplay();
    });
  }

  // Refresh the total / conversion line / change for the selected currency.
  function updatePayDisplay() {
    const totalBase = totals().total;
    document.getElementById("pay-total").textContent = money(totalBase, base);
    if (tenderCur) tenderCur.textContent = payCurrency;
    if (convertLine) {
      if (payCurrency === base) {
        convertLine.hidden = true;
      } else {
        const rate = rateFor(payCurrency);
        const perUnit = rate ? 1 / rate : 0;  // base units per 1 foreign unit
        convertLine.hidden = false;
        convertLine.textContent =
          `≈ ${money(totalBase * rate, payCurrency)}  ·  ` +
          `1 ${payCurrency} = ${perUnit.toFixed(4)} ${base}`;
      }
    }
    updateChange();
  }

  function updateChange() {
    const totalForeign = totals().total * rateFor(payCurrency);
    const given = Number(tendered.value || 0);  // entered in payCurrency
    const change = given - totalForeign;
    changeDue.textContent = money(change > 0 ? change : 0, payCurrency);
  }

  // Only ready methods are clickable (disabled buttons are skipped).
  document.querySelectorAll(".method").forEach((b) =>
    b.addEventListener("click", () => { if (!b.disabled) setMethod(b.dataset.method); }));

  function setMethod(m) {
    method = m;
    resetMpesa();
    document.querySelectorAll(".method").forEach((b) =>
      b.classList.toggle("active", b.dataset.method === m));
    document.getElementById("cash-fields").style.display = m === "cash" ? "" : "none";
    mpesaFields.hidden = m !== "mpesa";
    bankFields.hidden = m !== "bank";

    // M-Pesa and bank settle in the base currency only — lock the selector.
    const baseOnly = m === "mpesa" || m === "bank";
    if (payCurrencySel) {
      if (baseOnly) { payCurrencySel.value = base; payCurrency = base; }
      payCurrencySel.disabled = baseOnly;
    }
    updatePayDisplay();
    updateConfirmState();
    if (m === "cash") tendered.focus();
    else if (m === "mpesa") mpesaPhone.focus();
    else if (m === "bank") bankRef.focus();
  }

  // Confirm is gated for M-Pesa until the prompt is actually paid.
  function updateConfirmState() {
    confirmBtn.disabled = method === "mpesa" && !mpesaPaid;
  }

  tendered.addEventListener("input", updateChange);

  // --- M-Pesa STK Push -------------------------------------------------
  btnStk.addEventListener("click", sendStkPush);

  async function sendStkPush() {
    payError.textContent = "";
    resetMpesa();
    btnStk.disabled = true;
    btnStk.textContent = "Sending…";
    try {
      const res = await fetch("/api/mpesa/stk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: cartPayload(), phone: mpesaPhone.value.trim() }),
      });
      const data = await res.json();
      if (!data.ok) {
        mpesaStatus.textContent = data.error || "Couldn't send the prompt.";
        mpesaStatus.className = "mpesa-status text-danger small";
        btnStk.disabled = false;
        btnStk.textContent = "Send M-Pesa prompt";
        return;
      }
      mpesaCheckoutId = data.checkout_id;
      mpesaStatus.textContent = data.message || "Prompt sent — waiting for the customer…";
      btnStk.textContent = "Resend prompt";
      pollStatus(0);
    } catch (_) {
      mpesaStatus.textContent = "Could not reach the server.";
      mpesaStatus.className = "mpesa-status text-danger small";
      btnStk.disabled = false;
      btnStk.textContent = "Send M-Pesa prompt";
    }
  }

  // Poll for the payment result for ~90s (the prompt expires around then).
  function pollStatus(attempt) {
    if (!mpesaCheckoutId || attempt > 30) {
      if (!mpesaPaid && mpesaCheckoutId) {
        mpesaStatus.textContent = "No confirmation yet. Ask the customer to retry, or resend.";
        mpesaStatus.className = "mpesa-status text-danger small";
        btnStk.disabled = false;
      }
      return;
    }
    pollTimer = setTimeout(async () => {
      try {
        const res = await fetch(`/api/mpesa/status/${encodeURIComponent(mpesaCheckoutId)}`);
        const data = await res.json();
        if (data.ok && data.status === "success") {
          mpesaPaid = true;
          mpesaStatus.textContent = `Paid ✓ ${data.mpesa_receipt || ""}`.trim();
          mpesaStatus.className = "mpesa-status text-ok small";
          btnStk.disabled = true;
          updateConfirmState();
          return;
        }
        if (data.ok && data.status === "failed") {
          mpesaStatus.textContent = data.result_desc || "Payment failed or was cancelled.";
          mpesaStatus.className = "mpesa-status text-danger small";
          btnStk.disabled = false;
          return;
        }
      } catch (_) { /* keep polling through transient errors */ }
      pollStatus(attempt + 1);
    }, 3000);
  }

  document.getElementById("btn-cancel-pay").addEventListener("click", () => {
    payModal.hidden = true; scanInput.focus();
  });

  document.getElementById("btn-confirm-pay").addEventListener("click", confirmPayment);

  async function confirmPayment() {
    payError.textContent = "";
    const t = totals();
    // Amounts in the payment modal are expressed in the selected currency.
    const totalForeign = t.total * rateFor(payCurrency);

    const payload = {
      items: cartPayload(),
      payment_method: method,
      currency: payCurrency,
      customer_id: customer ? customer.id : null,  // loyalty customer, if attached
      // The cashier is the signed-in user — resolved server-side from the session.
      // The exchange rate is resolved server-side too; we never send it.
    };

    if (method === "cash") {
      const amt = Number(tendered.value || 0);
      if (amt + 1e-9 < totalForeign) { payError.textContent = "Amount tendered is less than total."; return; }
      payload.amount_tendered = amt;
    } else if (method === "mpesa") {
      if (!mpesaPaid || !mpesaCheckoutId) { payError.textContent = "Send the M-Pesa prompt and wait for payment first."; return; }
      payload.mpesa_checkout_id = mpesaCheckoutId;
    } else if (method === "bank") {
      const ref = bankRef.value.trim();
      if (!ref) { payError.textContent = "Enter the bank/transfer reference."; return; }
      payload.payment_ref = ref;
    }

    confirmBtn.disabled = true;
    try {
      const res = await fetch("/api/checkout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!data.ok) { payError.textContent = data.message || data.error || "Checkout failed."; return; }
      stopPolling();
      finishSale(data.sale);
    } catch (_) {
      payError.textContent = "Could not reach the server.";
    } finally {
      updateConfirmState();
    }
  }

  function finishSale(sale) {
    payModal.hidden = true;
    cart.length = 0;
    renderCart();
    detachCustomer();  // each sale starts with no customer attached
    loadProducts();    // refresh grid stock counts after the sale
    // Skip the "Sale complete" prompt — show the receipt directly in the drawer.
    window.openReceipt(sale.id);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Keep focus on the scanner unless a modal/input is active.
  document.addEventListener("click", (e) => {
    const inModal = e.target.closest(".modal");
    const inField = e.target.closest("input, button, a, .product-card");
    if (!inModal && !inField) scanInput.focus();
  });

  renderCart();
  renderCustomer();
  loadProducts();
  loadRates();
  // Keep till FX rates current during a long shift (server caches/refreshes).
  setInterval(loadRates, 5 * 60 * 1000);
})();
