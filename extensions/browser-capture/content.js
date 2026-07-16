/**
 * Yahoo Finance content script — SPA navigations + optional quote scrape.
 */
(function () {
  let lastUrl = "";
  let lastScrapeKey = "";

  function parseNum(raw) {
    if (raw == null || raw === "") return null;
    const s = String(raw).replace(/[%,+\s,]/g, "").trim();
    if (!s || s === "—" || s === "-") return null;
    const n = Number(s);
    return Number.isFinite(n) ? n : null;
  }

  function textOf(el) {
    return el ? (el.textContent || "").trim() : "";
  }

  function streamer(field) {
    const el = document.querySelector(`fin-streamer[data-field="${field}"]`);
    if (!el) return null;
    const v = el.getAttribute("value") || el.getAttribute("data-value") || textOf(el);
    return v;
  }

  function scrapeQuote() {
    const out = {};
    const price =
      parseNum(streamer("regularMarketPrice")) ||
      parseNum(textOf(document.querySelector('[data-testid="qsp-price"]')));
    if (price != null) out.price = price;

    const change =
      parseNum(streamer("regularMarketChange")) ||
      parseNum(textOf(document.querySelector('[data-testid="qsp-price-change"]')));
    if (change != null) out.change = change;

    let changePct =
      parseNum(streamer("regularMarketChangePercent")) ||
      parseNum(textOf(document.querySelector('[data-testid="qsp-price-change-percent"]')));
    // Yahoo sometimes embeds "(−0.22%)" — already stripped by parseNum
    if (changePct != null) out.change_pct = changePct;

    const currency = (streamer("currency") || "").trim();
    if (currency) out.currency = currency;

    // Earnings / next event — best-effort, several Yahoo layouts
    const earnEl =
      document.querySelector('[data-testid="earnings"]') ||
      document.querySelector('[data-test="EARNINGS"]');
    if (earnEl) {
      const t = textOf(earnEl);
      if (t) out.earnings = t.replace(/\s+/g, " ").slice(0, 100);
    }
    const banner = textOf(
      document.querySelector('[data-testid="event-notification"]')
    );
    if (banner && /earnings/i.test(banner)) {
      out.earnings = banner.replace(/\s+/g, " ").slice(0, 100);
    }

    out.as_of = new Date().toISOString();
    out.market_state = document.body.innerText.includes("At close")
      ? "closed"
      : document.body.innerText.includes("Market open")
        ? "open"
        : "";
    return out;
  }

  function interestingPath(url) {
    try {
      const u = new URL(url);
      if (!/finance\.yahoo\.com$/i.test(u.hostname) && !/\.finance\.yahoo\.com$/i.test(u.hostname)) {
        return false;
      }
      const p = u.pathname || "/";
      // Home, quote summary, and all quote sub-tabs (statistics, news, …)
      if (p === "/" || p === "") return true;
      if (/^\/quote\//i.test(p)) return true;
      if (/^\/(news|video|research|sectors|calendar)\//i.test(p)) return true;
      return false;
    } catch {
      return false;
    }
  }

  function notify(reason) {
    const url = location.href;
    if (!interestingPath(url)) return;

    const isQuote = /\/quote\//i.test(url);
    const scrape = isQuote ? scrapeQuote() : null;
    const scrapeKey = scrape
      ? [scrape.price, scrape.change_pct, scrape.earnings || ""].join("|")
      : "";

    // Skip pure duplicate polls with same URL + same numbers
    if (
      url === lastUrl &&
      scrapeKey === lastScrapeKey &&
      reason !== "force" &&
      reason !== "pushState" &&
      reason !== "replaceState" &&
      reason !== "popstate"
    ) {
      return;
    }
    lastUrl = url;
    lastScrapeKey = scrapeKey;

    chrome.runtime.sendMessage({
      kind: "yahoo_url",
      url,
      title: document.title || "",
      reason: reason || "poll",
      scrape: scrape && Object.keys(scrape).length ? scrape : null,
    });
  }

  const _push = history.pushState;
  history.pushState = function () {
    const r = _push.apply(this, arguments);
    setTimeout(() => notify("pushState"), 80);
    return r;
  };
  const _replace = history.replaceState;
  history.replaceState = function () {
    const r = _replace.apply(this, arguments);
    setTimeout(() => notify("replaceState"), 80);
    return r;
  };
  window.addEventListener("popstate", () => notify("popstate"));

  // Yahoo hydrates prices async — notify on load and once after settle
  notify("load");
  setTimeout(() => notify("hydrate"), 1200);
  setInterval(() => notify("poll"), 8000);
})();
