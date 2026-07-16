/**
 * Yahoo-first capture → local ledger at http://127.0.0.1:8788/api/ingest-browser
 */

const DEFAULT_ENDPOINT = "http://127.0.0.1:8788/api/ingest-browser";

const ALLOWED_SUFFIXES = [
  "finance.yahoo.com",
  "yahoo.com",
  "tradingview.com",
  "sec.gov",
  "seekingalpha.com",
  "bloomberg.com",
  "ft.com",
  "reuters.com",
  "cnbc.com",
];

let lastKey = "";
let lastAt = 0;

function hostAllowed(hostname) {
  const h = (hostname || "").toLowerCase();
  return ALLOWED_SUFFIXES.some((s) => h === s || h.endsWith("." + s));
}

function normalizeKey(u) {
  try {
    const x = new URL(u);
    let path = x.pathname || "/";
    if (path.length > 1 && path.endsWith("/")) path = path.slice(0, -1);
    return (x.hostname + path).toLowerCase();
  } catch {
    return String(u || "");
  }
}

async function settings() {
  const stored = await chrome.storage.local.get([
    "endpoint",
    "autoSession",
    "enabled",
    "lastStatus",
  ]);
  return {
    endpoint: stored.endpoint || DEFAULT_ENDPOINT,
    autoSession: stored.autoSession !== false,
    enabled: stored.enabled !== false,
    lastStatus: stored.lastStatus || null,
  };
}

async function setStatus(ok, message, extra) {
  await chrome.storage.local.set({
    lastStatus: {
      ok,
      message,
      at: Date.now(),
      ...(extra || {}),
    },
  });
}

async function ingest(url, title, reason, scrape) {
  const cfg = await settings();
  if (!cfg.enabled) {
    await setStatus(false, "Capture is disabled in the popup");
    return { ok: false, error: "disabled" };
  }
  let host = "";
  try {
    host = new URL(url).hostname;
  } catch {
    return { ok: false, error: "bad url" };
  }
  if (!hostAllowed(host)) {
    await setStatus(false, "Host not allowlisted: " + host);
    return { ok: false, error: "not allowlisted" };
  }

  const key = normalizeKey(url);
  const now = Date.now();
  const richScrape = !!(scrape && scrape.price != null);
  // Allow one hydrate pass with price after a bare URL log
  if (
    key === lastKey &&
    now - lastAt < 60000 &&
    reason !== "manual" &&
    !(richScrape && (reason === "hydrate" || reason === "content"))
  ) {
    return { ok: true, deduped: true };
  }
  lastKey = key;
  lastAt = now;

  try {
    const body = {
      url,
      title: title || "",
      auto_session: cfg.autoSession,
      sensitivity: "internal",
    };
    if (scrape && typeof scrape === "object") {
      body.scrape = scrape;
      if (richScrape) body.force = reason === "hydrate" || reason === "manual";
    }
    const res = await fetch(cfg.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      await setStatus(false, data.error || "HTTP " + res.status, { url });
      return { ok: false, error: data.error || res.status };
    }
    const sym = (data.payload && data.payload.symbol) || "";
    const price = data.payload && data.payload.quote && data.payload.quote.price;
    const msg = sym
      ? price != null
        ? `Logged ${sym} @ ${price}`
        : `Logged ${sym}`
      : "Logged page";
    await setStatus(true, msg, {
      url,
      symbol: sym,
      session_id: data.session_id,
      reason,
    });
    return { ok: true, data };
  } catch (err) {
    await setStatus(
      false,
      "Cannot reach dashboard — is it running on :8788?",
      { url }
    );
    return { ok: false, error: String(err) };
  }
}

chrome.tabs.onActivated.addListener(async (activeInfo) => {
  try {
    const tab = await chrome.tabs.get(activeInfo.tabId);
    if (tab.url) await ingest(tab.url, tab.title, "tab_activated");
  } catch {
    /* ignore */
  }
});

chrome.tabs.onUpdated.addListener(async (_tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.active && tab.url) {
    try {
      await ingest(tab.url, tab.title, "tab_complete");
    } catch {
      /* ignore */
    }
  }
  // Yahoo SPA often updates URL without full reload
  if (changeInfo.url && tab.active) {
    try {
      await ingest(changeInfo.url, tab.title, "url_changed");
    } catch {
      /* ignore */
    }
  }
});

// Catch history.pushState navigations on Yahoo
chrome.webNavigation.onHistoryStateUpdated.addListener(async (details) => {
  if (details.frameId !== 0) return;
  try {
    const tab = await chrome.tabs.get(details.tabId);
    if (tab.active) await ingest(details.url, tab.title, "history");
  } catch {
    /* ignore */
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.kind === "yahoo_url") {
    ingest(msg.url, msg.title || "", msg.reason || "content", msg.scrape || null)
      .then((r) => sendResponse(r))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  if (msg && msg.kind === "capture_active") {
    chrome.tabs.query({ active: true, currentWindow: true }, async (tabs) => {
      const tab = tabs[0];
      if (!tab || !tab.url) {
        sendResponse({ ok: false, error: "No active tab" });
        return;
      }
      const r = await ingest(tab.url, tab.title, "manual", null);
      sendResponse(r);
    });
    return true;
  }
  return false;
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({
    endpoint: DEFAULT_ENDPOINT,
    autoSession: true,
    enabled: true,
  });
});
