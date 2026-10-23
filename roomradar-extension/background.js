const PRIMARY_API = "https://roomradar.onrender.com/api/v1";
const FALLBACK_API = "https://exeter-hmo-tracker.onrender.com/api/v1";

async function resilientFetch(path, options) {
  try {
    const res = await fetch(`${PRIMARY_API}${path}`, options);
    if (res.ok) {
      return await res.json();
    }
  } catch (e) {
    // Primary failed, proceed to fallback
  }

  // Fallback to initial Render URL
  const res = await fetch(`${FALLBACK_API}${path}`, options);
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return await res.json();
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "LOOKUP_SPAREROOM_AD") {
    resilientFetch("/lookup/spareroom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(message.payload),
    })
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));

    return true;
  }

  if (message.type === "SUBMIT_SPAREROOM_REVIEW") {
    resilientFetch("/reviews/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(message.payload),
    })
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));

    return true;
  }
});
