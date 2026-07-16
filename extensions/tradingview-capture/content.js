/**
 * Content script: observe TradingView chart chrome for symbol / interval.
 * Does NOT record mouse trajectories or keystrokes.
 */
(function () {
  const STATE = {
    symbol: null,
    interval: null,
    lastEmit: 0,
  };

  function textOf(el) {
    return (el && el.textContent ? el.textContent : "").trim();
  }

  function guessSymbol() {
    // TradingView DOM varies; try common selectors then URL.
    const selectors = [
      "[data-name='legend-source-item'] .title-l31H9iuA",
      "[class*='titleWrapper'] [class*='title']",
      "#header-toolbar-symbol-search",
      "button[aria-label*='Symbol']",
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      const t = textOf(el);
      if (t && t.length < 32 && /[A-Z0-9.]/i.test(t)) {
        // Often "NVDA · 1D" style — take first token
        return t.split(/[\s·|]/)[0];
      }
    }
    const m = location.pathname.match(/\/chart\/(?:[^/]+\/)?([A-Z0-9._-]+)/i);
    if (m) return m[1];
    const q = new URLSearchParams(location.search).get("symbol");
    return q || null;
  }

  function guessInterval() {
    const selectors = [
      "#header-toolbar-intervals",
      "button[data-tooltip*='interval' i]",
      "[class*='interval'] button[aria-checked='true']",
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      const t = textOf(el);
      if (t && t.length < 12) return t;
    }
    return null;
  }

  function countDrawingHints() {
    // Metadata only: count elements that look like drawing tool markers if present.
    const nodes = document.querySelectorAll(
      "[class*='drawing'], [data-name*='drawing'], .drawing-source"
    );
    return nodes.length;
  }

  function emit(type, payload) {
    const now = Date.now();
    if (now - STATE.lastEmit < 800 && type === "symbol_focus") return;
    STATE.lastEmit = now;
    chrome.runtime.sendMessage({
      kind: "tv_event",
      type,
      payload,
      sensitivity: "internal",
    });
  }

  function poll() {
    const symbol = guessSymbol();
    const interval = guessInterval();
    if (symbol && symbol !== STATE.symbol) {
      STATE.symbol = symbol;
      emit("symbol_focus", { symbol, interval: interval || STATE.interval, source: "dom_poll" });
    }
    if (interval && interval !== STATE.interval) {
      STATE.interval = interval;
      emit("interval_change", { symbol: STATE.symbol, interval, source: "dom_poll" });
    }
  }

  setInterval(poll, 2000);
  poll();

  // Expose a tiny API for the popup to request a snapshot / drawing meta
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.kind === "tv_snapshot") {
      const symbol = guessSymbol();
      const interval = guessInterval();
      const drawings = countDrawingHints();
      sendResponse({
        symbol,
        interval,
        drawings,
        url: location.href,
      });
      if (symbol) {
        emit("symbol_focus", { symbol, interval, source: "snapshot" });
      }
      if (drawings > 0) {
        emit("drawing_meta", { symbol, interval, count: drawings, source: "snapshot" });
      }
      return true;
    }
    return false;
  });
})();
