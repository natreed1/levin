/**
 * Forward TradingView capture events to the local analyst ledger dashboard.
 * Default: http://127.0.0.1:8788/api/ingest-tv
 */

const DEFAULT_ENDPOINT = "http://127.0.0.1:8788/api/ingest-tv";

async function getEndpoint() {
  const stored = await chrome.storage.local.get(["endpoint", "autoSession"]);
  return {
    endpoint: stored.endpoint || DEFAULT_ENDPOINT,
    autoSession: stored.autoSession !== false,
  };
}

async function postEvent(body) {
  const { endpoint, autoSession } = await getEndpoint();
  const res = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...body, auto_session: autoSession }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`ingest failed ${res.status}: ${text}`);
  }
  return res.json();
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.kind !== "tv_event") return false;
  postEvent({
    type: msg.type,
    payload: msg.payload || {},
    sensitivity: msg.sensitivity || "internal",
  })
    .then((data) => sendResponse({ ok: true, data }))
    .catch((err) => sendResponse({ ok: false, error: String(err) }));
  return true; // async
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({
    endpoint: DEFAULT_ENDPOINT,
    autoSession: true,
  });
});
