const enabledEl = document.getElementById("enabled");
const deepEl = document.getElementById("deepResearch");
const autoEl = document.getElementById("autoSession");
const endpointEl = document.getElementById("endpoint");
const statusEl = document.getElementById("status");
const siteListEl = document.getElementById("siteList");
const excludeListEl = document.getElementById("excludeList");
const activeHostEl = document.getElementById("activeHost");
const excludeInput = document.getElementById("excludeInput");
const tabDot = document.getElementById("tabDot");
const tabState = document.getElementById("tabState");
const tabToggle = document.getElementById("tabToggle");

const DEFAULT_ORIGIN = "https://levin.fly.dev";
const DEFAULT_ENDPOINT = DEFAULT_ORIGIN + "/api/ingest-browser";

// Kept in sync with background.js
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
const DENY_SUFFIXES = [
  "accounts.google.com", "mail.google.com", "gmail.com", "consent.google.com",
  "outlook.live.com", "outlook.office.com", "login.microsoftonline.com",
  "appleid.apple.com", "icloud.com", "chase.com", "bankofamerica.com",
  "wellsfargo.com", "paypal.com", "venmo.com", "stripe.com",
  "consent.yahoo.com", "guce.yahoo.com", "login.yahoo.com", "api.login.yahoo.com",
  "localhost", "127.0.0.1",
];
const DENIED_SCHEMES = ["chrome:", "chrome-extension:", "about:", "edge:", "brave:", "devtools:"];

let siteEnabled = {};
let excludedHosts = [];
let activeHost = "";
let activeUrl = "";

function showStatus(msg, ok) {
  statusEl.textContent = msg;
  statusEl.className = ok ? "ok" : "err";
}

function apiBase() {
  const ep = (endpointEl.value || "").trim() || DEFAULT_ENDPOINT;
  try {
    const u = new URL(ep);
    return `${u.protocol}//${u.host}`;
  } catch {
    return DEFAULT_ORIGIN;
  }
}

function isLedgerAppUrl(url, endpoint) {
  try {
    const u = new URL(url);
    const h = (u.hostname || "").toLowerCase();
    if (h === "levin.fly.dev" || h.endsWith(".levin.fly.dev")) return true;
    if (h !== "127.0.0.1" && h !== "localhost" && h !== "::1") return false;
    const port = parseInt(u.port, 10) || (u.protocol === "https:" ? 443 : 80);
    const ports = new Set([8788, 8790]);
    try {
      const ep = new URL(endpoint || DEFAULT_ENDPOINT);
      if (ep.hostname === "127.0.0.1" || ep.hostname === "localhost") {
        ports.add(parseInt(ep.port, 10) || 80);
      }
    } catch {
      /* ignore */
    }
    return ports.has(port);
  } catch {
    return false;
  }
}

function normalizeHost(h) {
  return String(h || "")
    .toLowerCase()
    .trim()
    .replace(/^https?:\/\//, "")
    .replace(/\/.*$/, "")
    .replace(/^www\./, "");
}

function suffixMatch(host, suffixes) {
  const h = (host || "").toLowerCase();
  if (!h) return false;
  return suffixes.some((s) => h === s || h.endsWith("." + s));
}

function defaultSiteEnabled() {
  const out = {};
  for (const s of SITE_PRESETS) out[s.id] = true;
  return out;
}

function isPresetHost(host) {
  const h = (host || "").toLowerCase();
  return (
    SITE_PRESETS.some((s) => h === s.id || h.endsWith("." + s.id)) ||
    h === "yahoo.com" ||
    h.endsWith(".yahoo.com") ||
    h === "xinhuanet.com" ||
    h.endsWith(".xinhuanet.com") ||
    h.endsWith(".news.cn")
  );
}

function presetEnabled(host) {
  const h = (host || "").toLowerCase();
  for (const s of SITE_PRESETS) {
    if (h === s.id || h.endsWith("." + s.id)) return siteEnabled[s.id] !== false;
  }
  if (h === "yahoo.com" || h.endsWith(".yahoo.com")) {
    return siteEnabled["finance.yahoo.com"] !== false;
  }
  if (h === "xinhuanet.com" || h.endsWith(".xinhuanet.com") || h.endsWith(".news.cn")) {
    return siteEnabled["news.cn"] !== false;
  }
  return false;
}

function hostExcluded(host) {
  const h = normalizeHost(host);
  return excludedHosts.map(normalizeHost).some(
    (ex) => h === ex || h.endsWith("." + ex) || ex.endsWith("." + h)
  );
}

/** Returns { kind: 'auto'|'manual'|'deny'|'excluded'|'internal', text } */
function tabCaptureState() {
  if (!activeUrl) return { kind: "internal", text: "No capturable tab" };
  const lower = activeUrl.toLowerCase();
  if (DENIED_SCHEMES.some((s) => lower.startsWith(s))) {
    return { kind: "internal", text: "Browser page — never captured" };
  }
  if (!/^https?:/i.test(lower)) {
    return { kind: "internal", text: "Not an http(s) page" };
  }
  if (isLedgerAppUrl(activeUrl, endpointEl.value || DEFAULT_ENDPOINT)) {
    return { kind: "internal", text: "Workflow / ledger UI — not captured" };
  }
  if (suffixMatch(activeHost, DENY_SUFFIXES)) {
    return { kind: "deny", text: "Denied (login/mail/bank/localhost)" };
  }
  if (hostExcluded(activeHost)) {
    return { kind: "excluded", text: "Excluded — won't capture" };
  }
  if (isPresetHost(activeHost) && presetEnabled(activeHost)) {
    return { kind: "auto", text: "Auto-capturing this site" };
  }
  if (deepEl.checked) {
    return { kind: "auto", text: "Deep research: auto-capturing" };
  }
  return { kind: "manual", text: "Manual only — click Capture" };
}

function renderTabState() {
  activeHostEl.textContent = activeHost || "(no host)";
  const st = tabCaptureState();
  tabState.textContent = st.text;
  tabDot.className = "dot " + (st.kind === "auto" ? "on" : st.kind === "deny" || st.kind === "excluded" ? "deny" : "off");

  const excluded = hostExcluded(activeHost);
  tabToggle.textContent = excluded ? "Un-exclude this site" : "Exclude this site";
  const canToggle = !!activeHost && st.kind !== "internal";
  tabToggle.disabled = !canToggle;
  tabToggle.style.opacity = canToggle ? "1" : "0.5";
  // Capture button: disable only for internal/deny
  const cap = document.getElementById("capture");
  const capturable = st.kind === "auto" || st.kind === "manual";
  cap.disabled = !capturable;
  cap.style.opacity = capturable ? "1" : "0.5";
  cap.textContent = st.kind === "auto" ? "Capture now (also auto)" : "Capture this tab now";
}

function persist(opts) {
  const payload = {
    endpoint: endpointEl.value.trim() || DEFAULT_ENDPOINT,
    autoSession: autoEl.checked,
    enabled: enabledEl.checked,
    deepResearch: deepEl.checked,
    siteEnabled,
    excludedHosts,
  };
  if (opts && opts.pinEndpoint) payload.endpointPinned = true;
  chrome.storage.local.set(payload);
}

function renderSites() {
  siteListEl.innerHTML = "";
  for (const s of SITE_PRESETS) {
    const row = document.createElement("div");
    row.className = "site-row";
    const lab = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = siteEnabled[s.id] !== false;
    cb.addEventListener("change", () => {
      siteEnabled[s.id] = cb.checked;
      persist();
      renderTabState();
      showStatus((cb.checked ? "On: " : "Off: ") + s.label, true);
    });
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(s.label));
    row.appendChild(lab);
    const code = document.createElement("span");
    code.className = "hint";
    code.textContent = s.id;
    row.appendChild(code);
    siteListEl.appendChild(row);
  }
}

function renderExcludes() {
  excludeListEl.innerHTML = "";
  if (!excludedHosts.length) {
    excludeListEl.innerHTML = '<div class="hint">None</div>';
    return;
  }
  for (const host of excludedHosts) {
    const row = document.createElement("div");
    row.className = "ex-row";
    const span = document.createElement("span");
    span.textContent = host;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "secondary tiny";
    btn.textContent = "Remove";
    btn.addEventListener("click", () => {
      excludedHosts = excludedHosts.filter((h) => h !== host);
      persist();
      renderExcludes();
      renderTabState();
      showStatus("Un-excluded " + host, true);
    });
    row.appendChild(span);
    row.appendChild(btn);
    excludeListEl.appendChild(row);
  }
}

function addExclude(raw) {
  const host = normalizeHost(raw);
  if (!host) {
    showStatus("Enter a host like arxiv.org", false);
    return;
  }
  if (!excludedHosts.includes(host)) {
    excludedHosts = [...excludedHosts, host];
    persist();
    renderExcludes();
    renderTabState();
  }
  showStatus("Excluded " + host, true);
  excludeInput.value = "";
}

// --- Load config directly from storage (no dependency on the worker) ---
chrome.storage.local.get(
  ["endpoint", "autoSession", "enabled", "deepResearch", "siteEnabled", "excludedHosts", "lastStatus"],
  (s) => {
    endpointEl.value = s.endpoint || DEFAULT_ENDPOINT;
    autoEl.checked = s.autoSession !== false;
    enabledEl.checked = s.enabled !== false;
    deepEl.checked = !!s.deepResearch;
    siteEnabled = s.siteEnabled && typeof s.siteEnabled === "object" ? s.siteEnabled : defaultSiteEnabled();
    excludedHosts = Array.isArray(s.excludedHosts) ? s.excludedHosts : [];
    renderSites();
    renderExcludes();
    renderTabState();
    if (s.lastStatus) {
      const age = Math.round((Date.now() - (s.lastStatus.at || 0)) / 1000);
      const isLedgerNoise =
        !s.lastStatus.ok &&
        s.lastStatus.message &&
        /Denied host \(127\.0\.0\.1\)|localhost blocked/i.test(s.lastStatus.message);
      if (!isLedgerNoise) {
        showStatus(
          (s.lastStatus.ok ? "Last: " : "Last error: ") +
            (s.lastStatus.message || "") +
            (age < 3600 ? ` (${age}s ago)` : ""),
          !!s.lastStatus.ok
        );
      }
    }
  }
);

// --- Active tab host directly (popup has tabs permission) ---
chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  const tab = tabs && tabs[0];
  activeUrl = (tab && tab.url) || "";
  try {
    activeHost = activeUrl ? new URL(activeUrl).hostname : "";
  } catch {
    activeHost = "";
  }
  renderTabState();
});

enabledEl.addEventListener("change", () => {
  persist();
  renderTabState();
});
deepEl.addEventListener("change", () => {
  persist();
  renderTabState();
  showStatus(
    deepEl.checked
      ? "Deep research ON — any https (minus denylist/excludes)"
      : "Deep research OFF — enabled research sites only",
    true
  );
});
autoEl.addEventListener("change", persist);
endpointEl.addEventListener("change", () => persist({ pinEndpoint: true }));

document.getElementById("capture").addEventListener("click", () => {
  persist();
  showStatus("Capturing…", true);
  chrome.runtime.sendMessage({ kind: "capture_active" }, (res) => {
    if (chrome.runtime.lastError) {
      showStatus("Worker asleep — try again: " + chrome.runtime.lastError.message, false);
      return;
    }
    if (!res || !res.ok) {
      showStatus((res && res.error) || "Capture failed — check dashboard/denylist", false);
      return;
    }
    const p = res.data && res.data.payload;
    const sym = p && p.symbol;
    const host = p && p.host;
    const price = p && p.quote && p.quote.price;
    showStatus(
      sym ? (price != null ? `Logged ${sym} @ ${price}` : `Logged ${sym}`) : `Logged ${host || "page"}`,
      true
    );
  });
});

tabToggle.addEventListener("click", () => {
  if (!activeHost) {
    showStatus("No active tab host", false);
    return;
  }
  if (hostExcluded(activeHost)) {
    const n = normalizeHost(activeHost);
    excludedHosts = excludedHosts.filter(
      (h) => normalizeHost(h) !== n && !n.endsWith("." + h) && !h.endsWith("." + n)
    );
    persist();
    renderExcludes();
    renderTabState();
    showStatus("Un-excluded " + n, true);
  } else {
    addExclude(activeHost);
  }
});

document.getElementById("addExclude").addEventListener("click", () => {
  addExclude(excludeInput.value);
});

document.querySelectorAll("button[data-tag]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const tag = btn.getAttribute("data-tag");
    showStatus("Tagging " + tag + "…", true);
    try {
      const res = await fetch(apiBase() + "/api/tracking/session/tag", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ tag }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showStatus(data.error || "Tag failed — start Tracking first", false);
        return;
      }
      showStatus("Outcome: " + tag, true);
    } catch (e) {
      showStatus(String(e), false);
    }
  });
});

const SCOPE_LABELS = {
  active_tab: "Active tab",
  all_tabs: "All open tabs",
  selected_tabs: "Selected tabs",
  research_sites: "Research sites",
  notes_only: "Notes only",
};

function renderScope(session) {
  const scopeDot = document.getElementById("scopeDot");
  const scopeState = document.getElementById("scopeState");
  const scopeHint = document.getElementById("scopeHint");
  if (!scopeDot || !scopeState) return;
  if (!session || !session.session_id) {
    scopeDot.className = "dot off";
    scopeState.textContent = "No active Workflow session";
    if (scopeHint) {
      scopeHint.textContent = "Start Tracking in Workflow to set Active / All / Selected tabs.";
    }
    return;
  }
  const label = SCOPE_LABELS[session.capture_scope] || session.capture_scope || "active_tab";
  scopeDot.className = "dot on";
  scopeState.textContent = `${label} · ${session.session_id}`;
  if (scopeHint) {
    scopeHint.textContent =
      session.capture_scope === "selected_tabs"
        ? "Check the tabs below to include them in this session."
        : session.capture_scope === "all_tabs"
          ? "Extension will snapshot every open http(s) tab once."
          : "Capturing according to this session scope.";
  }
}

function renderSelectedTabs(tabs) {
  const el = document.getElementById("selectedTabList");
  if (!el) return;
  if (!tabs || !tabs.length) {
    el.innerHTML = '<div class="hint">No capturable tabs open</div>';
    return;
  }
  el.innerHTML = "";
  for (const tab of tabs) {
    const row = document.createElement("div");
    row.className = "site-row";
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!tab.selected;
    cb.setAttribute("data-id", String(tab.id));
    cb.addEventListener("change", () => {
      const checked = [...el.querySelectorAll("input[type=checkbox]:checked")].map((node) =>
        Number(node.getAttribute("data-id"))
      );
      chrome.runtime.sendMessage({ kind: "set_selected_tabs", tabIds: checked }, () => {
        showStatus(`Selected ${checked.length} tab(s)`, true);
      });
    });
    label.appendChild(cb);
    const text = document.createElement("span");
    text.textContent = `${tab.host}${tab.active ? " · active" : ""} — ${tab.title}`;
    label.appendChild(text);
    row.appendChild(label);
    el.appendChild(row);
  }
}

function refreshSelectedTabs() {
  chrome.runtime.sendMessage({ kind: "list_open_tabs" }, (res) => {
    if (chrome.runtime.lastError) {
      renderSelectedTabs([]);
      return;
    }
    renderSelectedTabs((res && res.tabs) || []);
  });
  chrome.runtime.sendMessage({ kind: "refresh_scope" }, (res) => {
    if (chrome.runtime.lastError) return;
    renderScope(res && res.scope);
  });
}

const refreshTabsBtn = document.getElementById("refreshTabs");
if (refreshTabsBtn) refreshTabsBtn.addEventListener("click", refreshSelectedTabs);
const clearSelectedBtn = document.getElementById("clearSelected");
if (clearSelectedBtn) {
  clearSelectedBtn.addEventListener("click", () => {
    chrome.runtime.sendMessage({ kind: "set_selected_tabs", tabIds: [] }, () => {
      refreshSelectedTabs();
      showStatus("Cleared tab selection", true);
    });
  });
}
refreshSelectedTabs();

document.getElementById("openDash").addEventListener("click", () => {
  chrome.tabs.create({ url: apiBase() + "/" });
});
