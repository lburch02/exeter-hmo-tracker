const API_BASE = "https://exeter-hmo-tracker.onrender.com/api/v1";

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "LOOKUP_SPAREROOM_AD") {
    fetch(`${API_BASE}/lookup/spareroom`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(message.payload),
    })
      .then((res) => res.json())
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));

    // Return true to keep the message channel open for async sendResponse
    return true;
  }

  if (message.type === "SUBMIT_SPAREROOM_REVIEW") {
    fetch(`${API_BASE}/reviews/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(message.payload),
    })
      .then((res) => {
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        return res.json();
      })
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));

    return true;
  }
});
