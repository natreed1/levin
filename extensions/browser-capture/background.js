/**
 * Analyst Ledger browser capture
 * Modes: allowlist sites (toggleable) | deep research (any https) | denylist | excludes
 */

const DEFAULT_ENDPOINT = "http://127.0.0.1:8790/api/ingest-browser";

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
let lastScopeSessionId = "";
let cachedScope = { session_id: null, capture_scope: null, at: 0 };

function suffixMatch(hostname, suffixes) {
  const h = (hostname || "").toLowerCase();
  if (!h) return false;
  return suffixes.some((s) => h === s || h.endsWith("." + s));
}

function hostDenied(hostname) {
  return suffixMatch(hostname, DENY_SUFFIXES);
}

/** Local Workflow / Analyst Ledger UIs — never capture, never surface as errors. */
function isLedgerAppUrl(url, endpoint) {
  try {
    const u = new URL(url);
    const h = (u.hostname || "").toLowerCase();
    if (h !== "127.0.0.1" && h !== "localhost" && h !== "::1") {
      return false;
    }
    const port = parseInt(u.port, 10) || (u.protocol === "https:" ? 443 : 80);
    const ports = new Set([8788, 8790]);
    try {
      const ep = new URL(endpoint || DEFAULT_ENDPOINT);
      const epPort = parseInt(ep.port, 10) || 80;
      ports.add(epPort);
    } catch {
      /* ignore */
    }
    return ports.has(port);
  } catch {
    return false;
  }
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
    "selectedTabIds",
    "lastStatus",
  ]);
  return {
    endpoint: stored.endpoint || DEFAULT_ENDPOINT,
    autoSession: stored.autoSession !== false,
    enabled: stored.enabled !== false,
    deepResearch: !!stored.deepResearch,
    siteEnabled: stored.siteEnabled || defaultSiteEnabled(),
    excludedHosts: Array.isArray(stored.excludedHosts) ? stored.excludedHosts : [],
    selectedTabIds: Array.isArray(stored.selectedTabIds)
      ? stored.selectedTabIds.map(Number).filter((n) => Number.isFinite(n))
      : [],
    lastStatus: stored.lastStatus || null,
  };
}

function summaryUrl(endpoint) {
  try {
    const u = new URL(endpoint || DEFAULT_ENDPOINT);
    return `${u.origin}/api/tracking/summary`;
  } catch {
    return "http://127.0.0.1:8790/api/tracking/summary";
  }
}

async function fetchSessionScope(force) {
  const now = Date.now();
  if (!force && cachedScope.at && now - cachedScope.at < 4000) {
    return cachedScope;
  }
  const cfg = await settings();
  try {
    const res = await fetch(summaryUrl(cfg.endpoint), {
      method: "GET",
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!res.ok) {
      cachedScope = { session_id: null, capture_scope: null, at: now, error: "HTTP " + res.status };
      return cachedScope;
    }
    const data = await res.json();
    const active = data && data.active_session;
    cachedScope = {
      session_id: active ? active.session_id : null,
      capture_scope: active ? active.capture_scope || "active_tab" : null,
      at: now,
    };
    return cachedScope;
  } catch (err) {
    cachedScope = { session_id: null, capture_scope: null, at: now, error: String(err) };
    return cachedScope;
  }
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
function decideCapture(url, cfg, reason, scope) {
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
  if (isLedgerAppUrl(url, cfg.endpoint)) {
    return { ok: false, skipped: true, reason: "ledger_app", host };
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

  const captureScope = scope || null;
  if (!manual && captureScope === "notes_only") {
    return { ok: false, skipped: true, reason: "notes_only", host };
  }

  const onPreset = isPresetHost(host) && sitePresetEnabled(host, cfg.siteEnabled);
  if (captureScope === "research_sites") {
    if (onPreset || manual) return { ok: true, allowAny: false, host };
    return {
      ok: false,
      error: "Research sites scope — " + host + " is not an enabled preset",
    };
  }

  if (onPreset) {
    return { ok: true, allowAny: false, host };
  }
  if (
    manual ||
    cfg.deepResearch ||
    captureScope === "active_tab" ||
    captureScope === "all_tabs" ||
    captureScope === "selected_tabs"
  ) {
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

async function shouldCaptureTab(tab, cfg, reason, scopeInfo) {
  if (!tab || !tab.url) return { ok: false, error: "No tab url" };
  const scope = scopeInfo && scopeInfo.capture_scope;
  if (!scopeInfo.session_id && reason !== "manual") {
    // No Workflow tracking session — fall back to extension-only rules
    return decideCapture(tab.url, cfg, reason, null);
  }
  if (reason !== "manual" && scope === "notes_only") {
    return { ok: false, skipped: true, reason: "notes_only" };
  }
  if (reason !== "manual" && scope === "selected_tabs") {
    const id = Number(tab.id);
    if (!cfg.selectedTabIds.includes(id)) {
      return { ok: false, skipped: true, reason: "not_selected" };
    }
  }
  if (
    reason !== "manual" &&
    scope === "active_tab" &&
    reason !== "all_tabs_snapshot" &&
    tab.active === false
  ) {
    return { ok: false, skipped: true, reason: "inactive_tab" };
  }
  return decideCapture(tab.url, cfg, reason === "all_tabs_snapshot" ? "manual" : reason, scope);
}

async function ingest(url, title, reason, scrape, tabMeta) {
  const cfg = await settings();
  if (!cfg.enabled) {
    await setStatus(false, "Capture is disabled in the popup");
    return { ok: false, error: "disabled" };
  }

  const scopeInfo = await fetchSessionScope(false);
  const tab = tabMeta || { url, title, active: true, id: null };
  const decision = await shouldCaptureTab(tab, cfg, reason, scopeInfo);
  if (!decision.ok) {
    if (decision.skipped) {
      return { ok: true, skipped: true, reason: decision.reason };
    }
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
    reason !== "all_tabs_snapshot" &&
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
      auto_session: cfg.autoSession && !scopeInfo.session_id,
      sensitivity: "internal",
      allow_any: !!decision.allowAny,
      deep_research: !!cfg.deepResearch || scopeInfo.capture_scope === "all_tabs",
      manual: reason === "manual" || reason === "all_tabs_snapshot",
    };
    if (scrape && typeof scrape === "object") {
      body.scrape = scrape;
      if (richScrape) body.force = reason === "hydrate" || reason === "manual";
    }
    const res = await fetch(cfg.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
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
      session_id: data.session_id || scopeInfo.session_id,
      reason,
      capture_scope: scopeInfo.capture_scope,
    });
    return { ok: true, data };
  } catch (err) {
    await setStatus(
      false,
      "Cannot reach Workflow — is it running on :8790 (or set Endpoint)?",
      { url }
    );
    return { ok: false, error: String(err) };
  }
}

async function snapshotAllTabs(scopeInfo) {
  if (!scopeInfo || !scopeInfo.session_id || scopeInfo.capture_scope !== "all_tabs") {
    return { ok: true, skipped: true };
  }
  if (lastScopeSessionId === scopeInfo.session_id) {
    return { ok: true, deduped: true };
  }
  lastScopeSessionId = scopeInfo.session_id;
  const tabs = await chrome.tabs.query({});
  let logged = 0;
  for (const tab of tabs) {
    if (!tab.url) continue;
    const r = await ingest(tab.url, tab.title, "all_tabs_snapshot", null, tab);
    if (r && r.ok && !r.skipped && !r.deduped) logged += 1;
  }
  await setStatus(true, `All-tabs snapshot: logged ${logged} page(s)`, {
    session_id: scopeInfo.session_id,
  });
  return { ok: true, logged };
}

async function maybeSnapshotForScope() {
  const scopeInfo = await fetchSessionScope(true);
  if (scopeInfo.capture_scope === "all_tabs") {
    return snapshotAllTabs(scopeInfo);
  }
  if (!scopeInfo.session_id) {
    lastScopeSessionId = "";
  }
  return scopeInfo;
}

chrome.tabs.onActivated.addListener(async (activeInfo) => {
  try {
    await maybeSnapshotForScope();
    const tab = await chrome.tabs.get(activeInfo.tabId);
    if (tab.url) await ingest(tab.url, tab.title, "tab_activated", null, tab);
  } catch {
    /* ignore */
  }
});

chrome.tabs.onUpdated.addListener(async (_tabId, changeInfo, tab) => {
  const scopeInfo = await fetchSessionScope(false);
  const trackAll = scopeInfo.capture_scope === "all_tabs" || scopeInfo.capture_scope === "selected_tabs";
  if (changeInfo.status === "complete" && tab.url && (tab.active || trackAll)) {
    try {
      await maybeSnapshotForScope();
      await ingest(tab.url, tab.title, "tab_complete", null, tab);
    } catch {
      /* ignore */
    }
  }
  if (changeInfo.url && (tab.active || trackAll)) {
    try {
      await ingest(changeInfo.url, tab.title, "url_changed", null, { ...tab, url: changeInfo.url });
    } catch {
      /* ignore */
    }
  }
});

chrome.webNavigation.onHistoryStateUpdated.addListener(async (details) => {
  if (details.frameId !== 0) return;
  try {
    const tab = await chrome.tabs.get(details.tabId);
    if (tab.active) await ingest(details.url, tab.title, "history", null, tab);
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
      const r = await ingest(tab.url, tab.title, "manual", null, tab);
      sendResponse(r);
    });
    return true;
  }
  if (msg && msg.kind === "refresh_scope") {
    maybeSnapshotForScope()
      .then((r) => sendResponse({ ok: true, scope: r }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  if (msg && msg.kind === "list_open_tabs") {
    chrome.tabs.query({}, async (tabs) => {
      const cfg = await settings();
      const selected = new Set(cfg.selectedTabIds);
      const rows = [];
      for (const tab of tabs) {
        if (!tab.url) continue;
        let host = "";
        try {
          host = new URL(tab.url).hostname;
        } catch {
          continue;
        }
        if (isLedgerAppUrl(tab.url, cfg.endpoint)) continue;
        if (DENIED_SCHEMES.some((s) => tab.url.toLowerCase().startsWith(s))) continue;
        rows.push({
          id: tab.id,
          title: (tab.title || host || "Tab").slice(0, 80),
          host,
          url: tab.url,
          active: !!tab.active,
          selected: selected.has(Number(tab.id)),
        });
      }
      sendResponse({ ok: true, tabs: rows });
    });
    return true;
  }
  if (msg && msg.kind === "set_selected_tabs") {
    const ids = Array.isArray(msg.tabIds)
      ? msg.tabIds.map(Number).filter((n) => Number.isFinite(n))
      : [];
    chrome.storage.local.set({ selectedTabIds: ids }, () => {
      sendResponse({ ok: true, selectedTabIds: ids });
    });
    return true;
  }
  if (msg && msg.kind === "get_capture_config") {
    Promise.all([settings(), fetchSessionScope(true)])
      .then(([cfg, scope]) =>
        sendResponse({
          ok: true,
          presets: SITE_PRESETS,
          deny: DENY_SUFFIXES,
          ...cfg,
          session: scope,
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
    selectedTabIds: [],
  });
});

// Pick up a newly started all_tabs session even if the user stays on one page.
setInterval(() => {
  maybeSnapshotForScope().catch(() => {});
}, 8000);
