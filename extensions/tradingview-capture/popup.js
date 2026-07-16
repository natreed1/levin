const endpointEl = document.getElementById("endpoint");
const autoEl = document.getElementById("autoSession");
const noteEl = document.getElementById("note");
const statusEl = document.getElementById("status");

function setStatus(msg, ok) {
  statusEl.textContent = msg;
  statusEl.style.color = ok ? "#2ecc71" : "#e74c3c";
}

chrome.storage.local.get(["endpoint", "autoSession"], (stored) => {
  endpointEl.value = stored.endpoint || "http://127.0.0.1:8788/api/ingest-tv";
  autoEl.checked = stored.autoSession !== false;
});

endpointEl.addEventListener("change", () => {
  chrome.storage.local.set({ endpoint: endpointEl.value.trim() });
});
autoEl.addEventListener("change", () => {
  chrome.storage.local.set({ autoSession: autoEl.checked });
});

async function post(body) {
  const endpoint = endpointEl.value.trim();
  const res = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...body, auto_session: autoEl.checked }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

document.getElementById("sendNote").addEventListener("click", async () => {
  const text = noteEl.value.trim();
  if (!text) {
    setStatus("Write a note first", false);
    return;
  }
  try {
    // Prefer routing through ledger note via ingest as type note
    await post({
      type: "note",
      sensitivity: "internal",
      payload: { text, source: "tv_extension" },
    });
    noteEl.value = "";
    setStatus("Note logged", true);
  } catch (err) {
    setStatus(String(err), false);
  }
});

document.getElementById("snapshot").addEventListener("click", async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id) throw new Error("No active tab");
    const snap = await chrome.tabs.sendMessage(tab.id, { kind: "tv_snapshot" });
    await post({
      type: "symbol_focus",
      sensitivity: "internal",
      payload: {
        symbol: snap.symbol,
        interval: snap.interval,
        url: snap.url,
        source: "popup_snapshot",
      },
    });
    if (snap.drawings > 0) {
      await post({
        type: "drawing_meta",
        sensitivity: "internal",
        payload: {
          symbol: snap.symbol,
          interval: snap.interval,
          count: snap.drawings,
          source: "popup_snapshot",
        },
      });
    }
    setStatus(`Captured ${snap.symbol || "?"} @ ${snap.interval || "?"}`, true);
  } catch (err) {
    setStatus(String(err), false);
  }
});
