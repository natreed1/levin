/**
 * Page bridge for Flyleaf / local Workflow Tracking.
 * Lets the website detect Capture and open the tab picker — websites cannot
 * open chrome.action popups themselves.
 */
(function () {
  const PAGE_SOURCE = "flyleaf-tracking";
  const EXT_SOURCE = "flyleaf-capture";

  function reply(type, payload) {
    window.postMessage(
      Object.assign({ source: EXT_SOURCE, type }, payload || {}),
      window.location.origin
    );
  }

  function syncOrigin() {
    chrome.runtime.sendMessage(
      {
        kind: "sync_workflow_origin",
        origin: window.location.origin,
      },
      () => {
        void chrome.runtime.lastError;
      }
    );
  }

  // Announce immediately so Tracking can show "connected".
  syncOrigin();
  reply("ready", { version: chrome.runtime.getManifest().version });

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.source !== PAGE_SOURCE) return;

    if (data.type === "ping") {
      syncOrigin();
      reply("pong", { version: chrome.runtime.getManifest().version });
      return;
    }

    if (data.type === "sync_origin") {
      syncOrigin();
      reply("synced", { origin: window.location.origin });
      return;
    }

    if (data.type === "open_picker" || data.type === "session_started") {
      chrome.runtime.sendMessage(
        {
          kind: "open_tab_picker",
          capture_scope: data.capture_scope || null,
          session_id: data.session_id || null,
          reason: data.type,
        },
        (res) => {
          if (chrome.runtime.lastError) {
            reply("error", { message: chrome.runtime.lastError.message });
            return;
          }
          reply("opened_picker", {
            ok: !!(res && res.ok),
            capture_scope: data.capture_scope || null,
            version: chrome.runtime.getManifest().version,
          });
        }
      );
    }
  });
})();
