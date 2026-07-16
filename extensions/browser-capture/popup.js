const enabledEl = document.getElementById("enabled");
const autoEl = document.getElementById("autoSession");
const endpointEl = document.getElementById("endpoint");
const statusEl = document.getElementById("status");

function showStatus(msg, ok) {
  statusEl.textContent = msg;
  statusEl.className = ok ? "ok" : "err";
}

function apiBase() {
  const ep = (endpointEl.value || "").trim() || "http://127.0.0.1:8788/api/ingest-browser";
  try {
    const u = new URL(ep);
    return `${u.protocol}//${u.host}`;
  } catch {
    return "http://127.0.0.1:8788";
  }
}

chrome.storage.local.get(["endpoint", "autoSession", "enabled", "lastStatus"], (s) => {
  endpointEl.value = s.endpoint || "http://127.0.0.1:8788/api/ingest-browser";
  autoEl.checked = s.autoSession !== false;
  enabledEl.checked = s.enabled !== false;
  if (s.lastStatus) {
    const age = Math.round((Date.now() - (s.lastStatus.at || 0)) / 1000);
    showStatus(
      (s.lastStatus.ok ? "Last: " : "Last error: ") +
        (s.lastStatus.message || "") +
        (age < 3600 ? ` (${age}s ago)` : ""),
      !!s.lastStatus.ok
    );
  }
});

function persist() {
  chrome.storage.local.set({
    endpoint: endpointEl.value.trim(),
    autoSession: autoEl.checked,
    enabled: enabledEl.checked,
  });
}
enabledEl.addEventListener("change", persist);
autoEl.addEventListener("change", persist);
endpointEl.addEventListener("change", persist);

document.getElementById("capture").addEventListener("click", () => {
  persist();
  showStatus("Capturing…", true);
  chrome.runtime.sendMessage({ kind: "capture_active" }, (res) => {
    if (chrome.runtime.lastError) {
      showStatus(chrome.runtime.lastError.message, false);
      return;
    }
    if (!res || !res.ok) {
      showStatus((res && res.error) || "Capture failed — is the dashboard running?", false);
      return;
    }
    const payload = res.data && res.data.payload;
    const sym = payload && payload.symbol;
    const price = payload && payload.quote && payload.quote.price;
    showStatus(
      sym ? (price != null ? `Logged ${sym} @ ${price}` : `Logged ${sym}`) : "Logged",
      true
    );
  });
});

document.querySelectorAll("button[data-tag]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const tag = btn.getAttribute("data-tag");
    showStatus("Tagging " + tag + "…", true);
    try {
      const res = await fetch(apiBase() + "/api/session/tag", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showStatus(data.error || "Tag failed — start a session first", false);
        return;
      }
      showStatus("Outcome: " + tag, true);
    } catch (e) {
      showStatus(String(e), false);
    }
  });
});

document.getElementById("openDash").addEventListener("click", () => {
  chrome.tabs.create({ url: apiBase() + "/" });
});
