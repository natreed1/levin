/**
 * Analyst Ledger browser capture
 * Modes: allowlist sites (toggleable) | deep research (any https) | denylist | excludes
 */

const DEFAULT_ENDPOINT = "http://127.0.0.1:8788/api/ingest-browser";

/** Preset research sites — each can be toggled on/off in the popup */
const SITE_PRESETS = [
  { id: "finance.yahoo.com", label: "Yahoo Finance" },
  { id: "tradingview.com", label: "TradingView" },
  { id: "sec.gov", label: "SEC EDGAR" },
  { id: "seekingalpha.com", label: "Seeking Alpha" },
  { id: "bloomberg.com", label: "Bloomberg" },
  { id: "ft.com", label: "FT" },
  { id: "reuters.com", label: "Reuters" },
  { id: "cnbc.com", label: "CNBC" },
  { id: "theverge.com", label: "The Verge" },
  { id: "news.cn", label: "Xinhua / news.cn" },
  { id: "news.google.com", label: "Google News" },
];

/** Always blocked (privacy / noise) — cannot be overridden */
const DENY_SUFFIXES = [
  "accounts.google.com",
  "mail.google.com",
  "gmail.com",
  "consent.google.com",
  "outlook.live.com",
  "outlook.office.com",
  "login.microsoftonline.com",
  "appleid.apple.com",
  "icloud.com",
  "chase.com",
  "bankofamerica.com",
  "wellsfargo.com",
  "paypal.com",
  "venmo.com",
  "stripe.com",
  "consent.yahoo.com",
  "guce.yahoo.com",
  "login.yahoo.com",
  "api.login.yahoo.com",
  "localhost",
  "127.0.0.1",
];

const DENIED_SCHEMES = [
  "chrome:",
  "chrome-extension:",
  "about:",
  "edge:",
  "brave:",
  "devtools:",
];

let lastKey = "";
let lastAt = 0;

function suffixMatch(hostname, suffixes) {
  const h = (hostname || "").toLowerCase();
  if (!h) return false;
  return suffixes.some((s) => h === s || h.endsWith("." + s));
}

function hostDenied(hostname) {
  return suffixMatch(hostname, DENY_SUFFIXES);
}

function defaultSiteEnabled() {
  const out = {};
  for (const s of SITE_PRESETS) out[s.id] = true;
  return out;
}

function normalizeHost(h) {
  return String(h || "")
    .toLowerCase()
    .trim()
    .replace(/^www\./, "");
}

function hostExcluded(hostname, excludedHosts) {
  const h = normalizeHost(hostname);
  const list = (excludedHosts || []).map(normalizeHost);
  return list.some((ex) => h === ex || h.endsWith("." + ex) || ex.endsWith("." + h));
}

function sitePresetEnabled(hostname, siteEnabled) {
  const h = (hostname || "").toLowerCase();
  const map = siteEnabled && typeof siteEnabled === "object" ? siteEnabled : defaultSiteEnabled();
  for (const s of SITE_PRESETS) {
    if (h === s.id || h.endsWith("." + s.id)) {
      return map[s.id] !== false;
    }
  }
  // yahoo.com root counts as Yahoo when finance.yahoo.com toggle is on
  if (h === "yahoo.com" || h.endsWith(".yahoo.com")) {
    return map["finance.yahoo.com"] !== false;
  }
  // Xinhua English mirrors
  if (h === "xinhuanet.com" || h.endsWith(".xinhuanet.com") || h.endsWith(".news.cn")) {
    return map["news.cn"] !== false;
  }
  return false;
}

function isPresetHost(hostname) {
  const h = (hostname || "").toLowerCase();
  return (
    SITE_PRESETS.some((s) => h === s.id || h.endsWith("." + s.id)) ||
    h === "yahoo.com" ||
    h.endsWith(".yahoo.com") ||
    h === "xinhuanet.com" ||
    h.endsWith(".xinhuanet.com") ||
    h.endsWith(".news.cn")
  );
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
    "deepResearch",
    "siteEnabled",
    "excludedHosts",
    "lastStatus",
  ]);
  return {
    endpoint: stored.endpoint || DEFAULT_ENDPOINT,
    autoSession: stored.autoSession !== false,
    enabled: stored.enabled !== false,
    deepResearch: !!stored.deepResearch,
    siteEnabled: stored.siteEnabled || defaultSiteEnabled(),
    excludedHosts: Array.isArray(stored.excludedHosts) ? stored.excludedHosts : [],
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

/**
 * Decide whether this URL may be captured.
 * manual=true bypasses allowlist (still respects denylist + excludes).
 */
function decideCapture(url, cfg, reason) {
  const manual = reason === "manual";
  let host = "";
  let scheme = "";
  try {
    const u = new URL(url);
    host = u.hostname || "";
    scheme = (u.protocol || "").toLowerCase();
  } catch {
    return { ok: false, error: "bad url" };
  }

  if (DENIED_SCHEMES.some((s) => url.toLowerCase().startsWith(s) || scheme === s)) {
    return { ok: false, error: "Browser internal pages are never captured" };
  }
  if (scheme !== "http:" && scheme !== "https:") {
    return { ok: false, error: "Only http(s) pages can be captured" };
  }
  if (hostDenied(host)) {
    return {
      ok: false,
      error: "Denied host (" + host + ") — login/mail/bank/localhost blocked",
    };
  }
  if (hostExcluded(host, cfg.excludedHosts)) {
    return { ok: false, error: "Excluded: " + host + " (remove in popup)" };
  }

  const onPreset = isPresetHost(host) && sitePresetEnabled(host, cfg.siteEnabled);
  if (onPreset) {
    return { ok: true, allowAny: false, host };
  }
  if (manual || cfg.deepResearch) {
    return { ok: true, allowAny: true, host };
  }
  return {
    ok: false,
    error:
      "Not on enabled research sites (" +
      host +
      "). Turn on Deep research, enable the site, or Capture this tab.",
  };
}

async function ingest(url, title, reason, scrape) {
  const cfg = await settings();
  if (!cfg.enabled) {
    await setStatus(false, "Capture is disabled in the popup");
    return { ok: false, error: "disabled" };
  }

  const decision = decideCapture(url, cfg, reason);
  if (!decision.ok) {
    await setStatus(false, decision.error, { url, host: decision.host });
    return { ok: false, error: decision.error };
  }

  const key = normalizeKey(url);
  const now = Date.now();
  const richScrape = !!(scrape && scrape.price != null);
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
      allow_any: !!decision.allowAny,
      deep_research: !!cfg.deepResearch,
      manual: reason === "manual",
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
    const hostLabel = (data.payload && data.payload.host) || decision.host || "";
    const msg = sym
      ? price != null
        ? `Logged ${sym} @ ${price}`
        : `Logged ${sym}`
      : decision.allowAny
        ? `Logged ${hostLabel}`
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
  if (changeInfo.url && tab.active) {
    try {
      await ingest(changeInfo.url, tab.title, "url_changed");
    } catch {
      /* ignore */
    }
  }
});

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
  if (msg && msg.kind === "get_capture_config") {
    settings()
      .then((cfg) =>
        sendResponse({
          ok: true,
          presets: SITE_PRESETS,
          deny: DENY_SUFFIXES,
          ...cfg,
        })
      )
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  if (msg && msg.kind === "active_tab_host") {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tab = tabs[0];
      let host = "";
      try {
        host = tab && tab.url ? new URL(tab.url).hostname : "";
      } catch {
        host = "";
      }
      sendResponse({ ok: true, host, url: (tab && tab.url) || "", title: (tab && tab.title) || "" });
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
    deepResearch: false,
    siteEnabled: defaultSiteEnabled(),
    excludedHosts: [],
  });
});
