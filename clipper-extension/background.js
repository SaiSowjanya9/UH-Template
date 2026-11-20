"use strict";

// The service worker does the talking to the UH Homes app, so the token never
// rides along inside a page. It holds no credentials of its own: the clipper
// token comes from extension storage, and no Supabase key ever reaches here.

async function clipToApp(settings, body) {
  const send = (payload) => fetch(`${settings.appUrl}/api/catalog/clip`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-UH-Clipper": settings.token },
    body: JSON.stringify(payload),
  });
  let response = await send(body);
  if (!response.ok && body.image_url && settings.direct) {
    // The app's own download was refused (hotlink protection, sign-in walls). With the
    // optional permission granted, fetch the image here and hand the bytes over instead.
    try {
      const image = await fetch(body.image_url);
      if (image.ok) {
        const buffer = await image.arrayBuffer();
        if (buffer.byteLength <= 5 * 1024 * 1024) {
          let binary = "";
          const bytes = new Uint8Array(buffer);
          for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
          response = await send({ ...body, image_url: "", image_data: btoa(binary) });
        }
      }
    } catch { /* fall through to the first response's error */ }
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) return { ok: false, error: data.error || `The app answered ${response.status}.` };
  return { ok: true, collection: data.collection, item: data.item };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "uh-clip") {
    clipToApp(message.settings, message.body)
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, error: error?.message || "The app could not be reached." }));
    return true;        // answered asynchronously
  }
  if (message?.type === "uh-hover-clip") {
    chrome.storage.local.get(["appUrl", "token", "direct", "hoverCollection"], (stored) => {
      if (!stored.appUrl || !stored.token || !stored.hoverCollection) {
        sendResponse({ ok: false, error: "Open the UH Homes Clipper and connect it first." });
        return;
      }
      clipToApp({ appUrl: stored.appUrl, token: stored.token, direct: !!stored.direct },
                { ...message.body, collection: stored.hoverCollection })
        .then(sendResponse)
        .catch((error) => sendResponse({ ok: false, error: error?.message || "The app could not be reached." }));
    });
    return true;
  }
  return false;
});
