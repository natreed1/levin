# Flyleaf QA bug log

## Status (2026-07-23)
Code fixes deployed; **site restored** after sjc capacity outage (forked volume → new machine). Only open ops item: BUG-007 (Resend).

## Fixed & deployed
- Tracking rewrite (no `:8790` copy; Capture status / Start tracking / Select tabs)
- BUG-004 stale tunnel auto-recover + Unreachable · On
- PR #7: auth fail-closed (forgot/resend), auto-verify → app, agent click-to-add, Present refresh, mobile overflow
- Favicon
- Specialist empty/unknown action → 400
- Review invalid Anthropic key → local_stub
- Companion SIGHUP ignore + LaunchAgent scripts

## Still open (ops)
| ID | Issue | Action |
|----|-------|--------|
| BUG-007 | Signup email unavailable on Fly | Set `MESSENGER_RESEND_API_KEY` (+ `MESSENGER_EMAIL_FROM`) via `fly secrets set` |

## Loop
New long-horizon debug loop started after this deploy — see agent session.

---

## Pass A — post-deploy retest (2026-07-23)

**Surface:** live `https://levin.fly.dev` + local messenger (`127.0.0.1:8790`, `MESSENGER_AUTO_VERIFY=1`) for authenticated UI. Companion LaunchAgent up on `:8791`; public tunnel hostname currently dead (NXDOMAIN).

### Results

| Area | Live | Local (auth) | Verdict |
|------|------|--------------|---------|
| Tracking: no `:8790`; capture-status; Start tracking; Select tabs | PASS (markup + static) | PASS (UI) | Fixed |
| Auth forgot-password fail-closed | PASS API **503** + UI error (not fake success) | n/a (console mail) | Fixed; BUG-007 ops |
| Auth signup messaging | PASS API **503** + UI “temporarily unavailable” | PASS auto-verify → shell | Fixed; BUG-007 ops |
| Rooms: click agent to add; Present/Debate no reload | Code present on live; **not retested logged-in** (no live session; signup blocked) | PASS Present after 1; Debate after 2 | Fixed (local proof) |
| Settings Models Unreachable · On vs Ready | JS string on live; **not retested logged-in** | PASS enabled+unreachable → **Unreachable · On**; establish recovers via companion | Fixed (local proof) |
| Favicon 200 | PASS `GET /static/favicon.svg`, `GET /favicon.ico` | PASS | Fixed |
| Review stub path | Auth required on live | PASS `destination=local_stub` (no Anthropic key) | Fixed |

### Confirmed open (ops)

| ID | Severity | Notes |
|----|----------|-------|
| BUG-007 | Ops / P1 product | No Resend on Fly. Fail-closed UX confirmed (forgot + signup). Do not fake email. |

### New findings

| ID | Severity | Issue | Action |
|----|----------|-------|--------|
| BUG-008 | P3 | `GET /favicon.svg` (bare) → **404**; HTML correctly uses `/static/favicon.svg`. `HEAD /favicon.ico` → **405** (GET 200). | **Fixed in tree** — `/favicon.ico` + `/favicon.svg` accept GET/HEAD |
| BUG-009 | P3 / ops note | Companion public tunnel (`*.trycloudflare.com` in `pipeline_state`) is dead; local `:8791` healthy. Live Models with a linked open-source profile will show Unreachable until tunnel re-established. | Re-open tunnel from Companion / Start local model on Fly-linked account. |
| BUG-010 | P3 UX | Signup form subcopy still says email verification is coming while Fly returns email unavailable (error banner is correct). | **Fixed in tree** — softer signup subcopy |

### Fixes this pass
None (no clear P0/P1 code defects). No commits.

### Follow-up after Pass A
- BUG-008 / BUG-010 patched in working tree (await next deploy with Pass B).
- QA loop v2 wake still armed (~30m).

### Confidence
**Medium-high (~0.8)** on unauthenticated live + local authenticated paths. **Medium (~0.55)** on live authenticated rooms/settings/tracking interactions — blocked by BUG-007 (cannot create/login a fresh live account without Resend or an existing session).

---

## Pass B — 2026-07-23

- Tracking rewrite still live; forgot-password **503** fail-closed confirmed.
- Deployed BUG-008/010 (favicon HEAD + `/favicon.svg`, softer signup copy).
- Unit regression: **33 passed**.
- **No new product P0/P1.** Only open: BUG-007 (Resend ops).
- **QA loop v2 stopped** (stop condition met).

### Outage / restore (same night)

Root cause: a root-level `fly deploy` (without `--config messenger/fly.toml`) left the app suspended; recreate on the old zone-`53b6` volume failed with **insufficient resources**.

**Restore:**
1. Forked volume → `vol_vz8q7kek0q2532zv` (zone `86e4`)
2. Machine `287090ea1d7628` brought up; empty Launch machines/volumes removed
3. Correct deploy: `fly deploy --config messenger/fly.toml --dockerfile Dockerfile` (v40+)

**Smoke:** `/healthz` **200**, Tracking (Start tracking / Select tabs / capture-status, no `:8790`), favicon **200**, checks **1/1**. Only open: BUG-007.
