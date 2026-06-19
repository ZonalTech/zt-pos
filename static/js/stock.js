/* Stock page: scan a barcode to receive/adjust stock, log movements live. */
(function () {
  const form = document.getElementById("receive-form");
  const barcodeEl = document.getElementById("r-barcode");
  const qtyEl = document.getElementById("r-qty");
  const noteEl = document.getElementById("r-note");
  const productEl = document.getElementById("r-product");
  const errEl = document.getElementById("r-error");
  const okEl = document.getElementById("r-success");

  // Live preview of the scanned product so the cashier knows what they hit.
  let lookupTimer = null;
  barcodeEl.addEventListener("input", () => {
    clearTimeout(lookupTimer);
    const code = barcodeEl.value.trim();
    if (!code) { productEl.textContent = "No item selected."; productEl.classList.add("muted"); return; }
    lookupTimer = setTimeout(() => previewProduct(code), 200);
  });

  async function previewProduct(code) {
    try {
      const res = await fetch(`/api/product/${encodeURIComponent(code)}`);
      if (res.status === 404) {
        productEl.textContent = `Unknown barcode "${code}" — add it on the Products page first.`;
        productEl.classList.remove("muted");
        return;
      }
      const data = await res.json();
      if (data.found) {
        const p = data.product;
        productEl.innerHTML = `<strong>${escapeHtml(p.name)}</strong> — on hand: ${p.quantity}`;
        productEl.classList.remove("muted");
      }
    } catch (_) { /* ignore */ }
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errEl.textContent = ""; okEl.textContent = "";
    const payload = {
      barcode: barcodeEl.value.trim(),
      quantity: parseInt(qtyEl.value, 10),
      note: noteEl.value.trim(),
    };
    if (!payload.barcode) { errEl.textContent = "Scan or enter a barcode."; return; }
    if (!payload.quantity) { errEl.textContent = "Quantity must not be zero."; return; }

    try {
      const res = await fetch("/api/stock/receive", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!data.ok) {
        errEl.textContent = data.error === "unknown_barcode"
          ? `Unknown barcode "${payload.barcode}". Add the product first.`
          : (data.error || "Failed.");
        return;
      }
      okEl.textContent = `${data.product.name} updated — now ${data.product.quantity} on hand.`;
      // reset for the next scan
      barcodeEl.value = ""; qtyEl.value = "1"; noteEl.value = "";
      productEl.textContent = "No item selected."; productEl.classList.add("muted");
      barcodeEl.focus();
      setTimeout(() => location.reload(), 700); // refresh tables
    } catch (_) {
      errEl.textContent = "Could not reach the server.";
    }
  });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
})();
