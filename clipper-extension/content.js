"use strict";

// Hover save: injected on demand from the popup. Hovering any sizeable image shows a
// "Save to UH Homes" button; clicking it clips that image to the chosen collection.
(() => {
  if (window.__uhClipperHover) return;
  window.__uhClipperHover = true;

  const button = document.createElement("button");
  button.textContent = "✂ Save to UH Homes";
  Object.assign(button.style, {
    position: "fixed", zIndex: 2147483647, display: "none", padding: "8px 12px",
    background: "#8d7342", color: "#fff", border: "0", borderRadius: "5px",
    font: "600 12px Arial, sans-serif", cursor: "pointer", boxShadow: "0 4px 14px rgba(0,0,0,.25)",
  });
  document.documentElement.appendChild(button);

  const toast = (message, error) => {
    const note = document.createElement("div");
    note.textContent = message;
    Object.assign(note.style, {
      position: "fixed", right: "18px", bottom: "18px", zIndex: 2147483647,
      background: error ? "#874c3b" : "#6b5426", color: "#fff", padding: "12px 16px",
      borderRadius: "6px", font: "13px Arial, sans-serif", boxShadow: "0 6px 20px rgba(0,0,0,.3)",
    });
    document.documentElement.appendChild(note);
    setTimeout(() => note.remove(), error ? 8000 : 4000);
  };

  // The spec-sheet fields the page's structured data offers, read once per hover save.
  const productInfo = () => {
    const product = {};
    const nameOf = (node) => String((node && typeof node === "object" ? node.name : node) || "").trim();
    const walk = (node) => {
      if (Array.isArray(node)) { node.forEach(walk); return; }
      if (!node || typeof node !== "object") return;
      if ([].concat(node["@type"] || []).some((k) => typeof k === "string" && k.toLowerCase() === "product")) {
        const offers = Array.isArray(node.offers) ? node.offers[0] : node.offers;
        if (!product.price && offers && typeof offers === "object" && (offers.price || offers.lowPrice)) {
          const amount = String(offers.price || offers.lowPrice);
          const currency = String(offers.priceCurrency || "");
          product.price = currency && !amount.toUpperCase().includes(currency.toUpperCase()) ? `${amount} ${currency.toUpperCase()}` : amount;
        }
        product.brand = product.brand || nameOf(node.brand);
        product.model = product.model || String(node.sku || node.mpn || "").trim() || nameOf(node.model);
        product.finish = product.finish || nameOf(node.color);
        product.size = product.size || nameOf(node.size);
        product.materials = product.materials || [].concat(node.material || []).map(nameOf).filter(Boolean).join(", ");
      }
      Object.values(node).forEach(walk);
    };
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
      try { walk(JSON.parse(script.textContent)); } catch { /* unreadable structured data is skipped */ }
    }
    return { price: (product.price || "").slice(0, 50), brand: (product.brand || "").slice(0, 200),
             model: (product.model || "").slice(0, 100), finish: (product.finish || "").slice(0, 200),
             size: (product.size || "").slice(0, 200), materials: (product.materials || "").slice(0, 500) };
  };

  let current = null;
  document.addEventListener("mouseover", (event) => {
    const img = event.target instanceof HTMLImageElement ? event.target : null;
    if (img && (img.naturalWidth * img.naturalHeight >= 10000) && /^https?:/.test(img.currentSrc || img.src)) {
      current = img;
      const box = img.getBoundingClientRect();
      button.style.left = `${Math.max(6, box.left + 8)}px`;
      button.style.top = `${Math.max(6, box.top + 8)}px`;
      button.style.display = "block";
    } else if (!button.contains(event.target)) {
      button.style.display = "none";
    }
  }, true);

  button.addEventListener("click", () => {
    if (!current) return;
    button.disabled = true;
    button.textContent = "Saving…";
    chrome.runtime.sendMessage({
      type: "uh-hover-clip",
      body: {
        image_url: current.currentSrc || current.src,
        source_url: location.href,
        source_site: location.hostname.replace(/^www\./, ""),
        title: current.alt || document.title || "",
        description: "",
        ...productInfo(),
      },
    }, (response) => {
      button.disabled = false;
      button.textContent = "✂ Save to UH Homes";
      button.style.display = "none";
      if (response?.ok) toast("Saved to UH Homes.");
      else toast(response?.error || "The inspiration could not be saved.", true);
    });
  });
})();
