const tabList = document.getElementById("tabList");
const statusEl = document.getElementById("status");
const scopeDot = document.getElementById("scopeDot");
const scopeState = document.getElementById("scopeState");

const SCOPE_LABELS = {
  active_tab: "Active tab",
  all_tabs: "All open tabs",
  selected_tabs: "Selected tabs",
  research_sites: "Research sites",
  notes_only: "Notes only",
};

function showStatus(msg, ok) {
  statusEl.textContent = msg || "";
  statusEl.className = ok ? "ok" : "err";
}

function params() {
  const q = new URLSearchParams(location.search);
  return {
    capture_scope: q.get("scope") || "",
    session_id: q.get("session") || "",
  };
}

function renderScope(session) {
  const p = params();
  const scope = (session && session.capture_scope) || p.capture_scope || "";
  const sid = (session && session.session_id) || p.session_id || "";
  if (!sid) {
    scopeDot.className = "dot";
    scopeState.textContent = "No active Tracking session — start one on Flyleaf, then refresh.";
    return;
  }
  scopeDot.className = "dot on";
  const label = SCOPE_LABELS[scope] || scope || "active_tab";
  scopeState.textContent = `${label} · ${sid}`;
}

function selectedIds() {
  return [...tabList.querySelectorAll("input[type=checkbox]:checked")].map((node) =>
    Number(node.getAttribute("data-id"))
  );
}

function persistSelection() {
  const ids = selectedIds();
  chrome.runtime.sendMessage({ kind: "set_selected_tabs", tabIds: ids }, () => {
    if (chrome.runtime.lastError) {
      showStatus(chrome.runtime.lastError.message, false);
      return;
    }
    showStatus(`Selected ${ids.length} tab(s)`, true);
  });
}

function renderTabs(tabs) {
  if (!tabs || !tabs.length) {
    tabList.innerHTML = '<div class="title">No capturable tabs open</div>';
    return;
  }
  tabList.innerHTML = "";
  for (const tab of tabs) {
    const row = document.createElement("div");
    row.className = "row";
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!tab.selected;
    cb.setAttribute("data-id", String(tab.id));
    cb.addEventListener("change", persistSelection);
    const body = document.createElement("div");
    const host = document.createElement("div");
    host.className = "host";
    host.textContent = `${tab.host}${tab.active ? " · active" : ""}`;
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = tab.title || tab.url || "";
    body.appendChild(host);
    body.appendChild(title);
    label.appendChild(cb);
    label.appendChild(body);
    row.appendChild(label);
    tabList.appendChild(row);
  }
}

function refresh() {
  chrome.runtime.sendMessage({ kind: "list_open_tabs" }, (res) => {
    if (chrome.runtime.lastError) {
      renderTabs([]);
      showStatus(chrome.runtime.lastError.message, false);
      return;
    }
    renderTabs((res && res.tabs) || []);
  });
  chrome.runtime.sendMessage({ kind: "refresh_scope" }, (res) => {
    if (chrome.runtime.lastError) return;
    renderScope(res && res.scope);
  });
}

document.getElementById("refresh").addEventListener("click", refresh);
document.getElementById("clear").addEventListener("click", () => {
  chrome.runtime.sendMessage({ kind: "set_selected_tabs", tabIds: [] }, () => {
    refresh();
    showStatus("Cleared tab selection", true);
  });
});
document.getElementById("selectAll").addEventListener("click", () => {
  tabList.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.checked = true;
  });
  persistSelection();
});
document.getElementById("done").addEventListener("click", () => {
  window.close();
});

refresh();
