(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const state = {
    me: null,
    tab: "chats",
    kind: null, // "people" | "agents"
    roomId: null,
    threadId: null,
    rooms: [],
    threads: [],
    specialists: [],
    compute: null,
    modelProfiles: [],
    activeProfileId: null,
    ws: null,
    shareUrl: null,
    shareRoomId: null,
    debateAction: "debate",
    resetToken: null,
    specialistJob: null,
    specialistPoll: null,
    devAutoLogin: false,
    devUser: null,
  };

  const THEME_KEY = "flyleaf-theme";
  function applyTheme(choice) {
    const preferred = choice || "system";
    const resolved = preferred === "system"
      ? (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")
      : preferred;
    document.documentElement.dataset.theme = resolved;
  }
  applyTheme(localStorage.getItem(THEME_KEY) || "system");

  function show(el) {
    if (!el) return;
    el.classList.remove("hidden");
    el.hidden = false;
  }
  function hide(el) {
    if (!el) return;
    el.classList.add("hidden");
    el.hidden = true;
  }
  function setError(id, msg) {
    const el = $(id);
    if (!el) return;
    if (msg) { el.textContent = msg; show(el); }
    else { el.textContent = ""; hide(el); }
  }

  function fmtTime(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
      return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    } catch { return iso; }
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    return { res, data };
  }

  // --- Auth -----------------------------------------------------------------

  function showAuth(opts = {}) {
    show($("#auth-screen"));
    hide($("#shell"));
    renderModelStatus(null);
    if (opts.dev_auto_login != null) state.devAutoLogin = !!opts.dev_auto_login;
    if (opts.dev_user !== undefined) state.devUser = opts.dev_user;
    const panel = $("#dev-login-panel");
    const btn = $("#dev-login-btn");
    if (panel && state.devAutoLogin) {
      show(panel);
      const name = state.devUser?.display_name || "Dev";
      if (btn) btn.textContent = `Continue as ${name}`;
    } else if (panel) {
      hide(panel);
    }
  }
  function showShell() {
    hide($("#auth-screen"));
    show($("#shell"));
    $("#who-label").textContent = state.me?.display_name || state.me?.name || "";
  }

  function setAuthBanner(msg, ok) {
    const el = $("#auth-banner");
    if (!el) return;
    if (!msg) {
      el.textContent = "";
      hide(el);
      el.classList.remove("ok");
      return;
    }
    el.textContent = msg;
    el.classList.toggle("ok", !!ok);
    show(el);
  }

  function showAuthPanel(which) {
    const panels = {
      login: "#login-form",
      otp: "#otp-form",
      signup: "#signup-form",
      forgot: "#forgot-form",
      reset: "#reset-form",
    };
    Object.entries(panels).forEach(([key, sel]) => {
      const el = $(sel);
      if (!el) return;
      if (key === which) show(el);
      else hide(el);
    });
    if (which === "login" || which === "signup") {
      $("#tab-login").classList.toggle("active", which === "login");
      $("#tab-signup").classList.toggle("active", which === "signup");
      show($("#tab-login"));
      show($("#tab-signup"));
    } else if (which === "otp") {
      hide($("#tab-login"));
      hide($("#tab-signup"));
    }
  }

  function beginOtpChallenge(data) {
    state.otpChallengeId = data.challenge_id || "";
    $("#otp-challenge-id").value = state.otpChallengeId;
    $("#otp-code").value = data.dev_otp_code || "";
    const email = data.email || $("#login-email").value || "your email";
    $("#otp-hint").textContent = `Enter the 6-digit code we sent to ${email}.`;
    setError("#otp-error", "");
    let banner = data.message || "Check your email for a sign-in code.";
    if (data.dev_otp_code) banner += ` Dev code: ${data.dev_otp_code}`;
    setAuthBanner(banner, true);
    showAuthPanel("otp");
    $("#otp-code").focus();
  }

  $("#tab-login").addEventListener("click", () => {
    setError("#login-error", "");
    hide($("#resend-verify-btn"));
    showAuthPanel("login");
  });
  $("#tab-signup").addEventListener("click", () => {
    setError("#signup-error", "");
    showAuthPanel("signup");
  });
  $("#show-forgot-btn").addEventListener("click", () => {
    setError("#forgot-error", "");
    $("#forgot-email").value = $("#login-email").value || "";
    showAuthPanel("forgot");
  });
  $("#forgot-back-btn").addEventListener("click", () => showAuthPanel("login"));
  $("#otp-back-btn")?.addEventListener("click", () => {
    state.otpChallengeId = null;
    $("#otp-challenge-id").value = "";
    showAuthPanel("login");
  });

  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("#login-error", "");
    hide($("#resend-verify-btn"));
    const email = $("#login-email").value;
    const { res, data } = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        email,
        password: $("#login-password").value,
      }),
    });
    if (!res.ok) {
      if (data?.error === "email_unverified") {
        setError("#login-error", data.message || "Verify your email first");
        show($("#resend-verify-btn"));
        return;
      }
      if (data?.error === "rate_limited") {
        setError(
          "#login-error",
          data.message || "Too many login attempts. Wait a few minutes and try again."
        );
        return;
      }
      setError("#login-error", (data && (data.message || data.error)) || "Login failed");
      return;
    }
    if (data?.requires_2fa) {
      beginOtpChallenge(data);
      return;
    }
    await bootstrap();
  });

  $("#otp-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("#otp-error", "");
    const challengeId = $("#otp-challenge-id").value || state.otpChallengeId || "";
    const { res, data } = await api("/api/auth/verify-2fa", {
      method: "POST",
      body: JSON.stringify({
        challenge_id: challengeId,
        code: $("#otp-code").value,
      }),
    });
    if (!res.ok) {
      setError("#otp-error", (data && (data.message || data.error)) || "Invalid code");
      return;
    }
    state.otpChallengeId = null;
    await bootstrap();
  });

  $("#resend-otp-btn")?.addEventListener("click", async () => {
    const challengeId = $("#otp-challenge-id").value || state.otpChallengeId || "";
    if (!challengeId) {
      setError("#otp-error", "Sign in again to request a new code.");
      return;
    }
    const { res, data } = await api("/api/auth/resend-2fa", {
      method: "POST",
      body: JSON.stringify({ challenge_id: challengeId }),
    });
    if (!res.ok) {
      setError("#otp-error", (data && (data.message || data.error)) || "Could not resend");
      return;
    }
    if (data?.dev_otp_code) $("#otp-code").value = data.dev_otp_code;
    let msg = data?.message || "A new code is on the way.";
    if (data?.dev_otp_code) msg += ` Dev code: ${data.dev_otp_code}`;
    setAuthBanner(msg, true);
  });

  $("#resend-verify-btn").addEventListener("click", async () => {
    const email = $("#login-email").value.trim();
    if (!email) {
      setError("#login-error", "Enter your email first");
      return;
    }
    const { data } = await api("/api/auth/resend-verification", {
      method: "POST",
      body: JSON.stringify({ email }),
    });
    let msg = data?.message || "If needed, a new verification email was sent.";
    if (data?.dev_verify_url) {
      msg += ` Dev link: ${data.dev_verify_url}`;
    }
    setAuthBanner(msg, true);
  });

  $("#signup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("#signup-error", "");
    const email = $("#signup-email").value;
    const { res, data } = await api("/api/auth/signup", {
      method: "POST",
      body: JSON.stringify({
        display_name: $("#signup-name").value,
        email,
        password: $("#signup-password").value,
      }),
    });
    if (!res.ok) {
      setError("#signup-error", (data && (data.message || data.error)) || "Signup failed");
      return;
    }
    // AUTO_VERIFY / break-glass signup already sets the session cookie — enter the app.
    if (data?.auto_verified) {
      await bootstrap();
      return;
    }
    let msg = data?.message || "Check your email to verify, then log in.";
    if (data?.dev_verify_url) {
      msg += ` Dev link: ${data.dev_verify_url}`;
    }
    setAuthBanner(msg, true);
    $("#login-email").value = email;
    showAuthPanel("login");
  });

  $("#forgot-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("#forgot-error", "");
    const email = $("#forgot-email").value;
    const { res, data } = await api("/api/auth/forgot-password", {
      method: "POST",
      body: JSON.stringify({ email }),
    });
    if (!res.ok) {
      setError("#forgot-error", (data && (data.message || data.error)) || "Request failed");
      return;
    }
    let msg = data?.message || "Check your email for a reset link.";
    if (data?.dev_reset_url) {
      msg += ` Dev link: ${data.dev_reset_url}`;
    }
    setAuthBanner(msg, true);
    $("#login-email").value = email;
    showAuthPanel("login");
  });

  $("#reset-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("#reset-error", "");
    const password = $("#reset-password").value;
    const password2 = $("#reset-password2").value;
    if (password !== password2) {
      setError("#reset-error", "Passwords do not match");
      return;
    }
    const params = new URLSearchParams(location.search);
    const token = params.get("reset") || state.resetToken || "";
    const { res, data } = await api("/api/auth/reset-password", {
      method: "POST",
      body: JSON.stringify({ token, password }),
    });
    if (!res.ok) {
      setError("#reset-error", (data && (data.message || data.error)) || "Reset failed");
      return;
    }
    setAuthBanner(data?.message || "Password updated. Log in.", true);
    state.resetToken = null;
    history.replaceState({}, "", "/");
    showAuthPanel("login");
  });

  $("#logout-btn").addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST" });
    closeWs();
    state.me = null;
    // Re-probe so the local Dev button stays available after logout.
    const { data } = await api("/api/me");
    showAuth({
      dev_auto_login: !!data?.dev_auto_login,
      dev_user: data?.dev_user || null,
    });
  });

  // Capture room invite + handle verify / reset deep links (login required).
  (() => {
    const params = new URLSearchParams(location.search);
    const invite = params.get("invite");
    const room = params.get("room");
    if (invite && room) {
      try {
        sessionStorage.setItem(
          "flyleaf-pending-invite",
          JSON.stringify({ invite, room })
        );
      } catch {}
      setAuthBanner("Log in or create an account to join this room.", true);
    }
    if (params.get("verified") === "1") {
      setAuthBanner("Email verified. You can log in.", true);
      const keep = new URLSearchParams();
      if (invite) keep.set("invite", invite);
      if (room) keep.set("room", room);
      const q = keep.toString();
      history.replaceState({}, "", q ? `/?${q}` : "/");
    }
    if (params.get("reset")) {
      state.resetToken = params.get("reset");
      history.replaceState({}, "", "/");
      showAuthPanel("reset");
      setAuthBanner("Choose a new password.", true);
    }
  })();

  async function consumePendingInvite() {
    let pending = null;
    const params = new URLSearchParams(location.search);
    if (params.get("invite") && params.get("room")) {
      pending = { invite: params.get("invite"), room: params.get("room") };
    } else {
      try {
        const raw = sessionStorage.getItem("flyleaf-pending-invite");
        if (raw) pending = JSON.parse(raw);
      } catch {
        pending = null;
      }
    }
    if (!pending?.invite || !pending?.room) return null;
    const { res, data } = await api("/api/join", {
      method: "POST",
      body: JSON.stringify({
        invite: pending.invite,
        room_id: pending.room,
      }),
    });
    try {
      sessionStorage.removeItem("flyleaf-pending-invite");
    } catch {}
    history.replaceState({}, "", "/");
    if (!res.ok) {
      setAuthBanner(
        (data && (data.message || data.error)) || "Could not join that room.",
        false
      );
      return null;
    }
    return data;
  }

  // --- Tabs ------------------------------------------------------------------

  $$(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  function switchTab(tab) {
    // Review / Tracking UI is temporarily retired from the main shell.
    if (tab === "review" || tab === "tracking") tab = "chats";
    state.tab = tab;
    $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    $$(".tab-panel").forEach((p) => hide(p));
    show($(`#tab-${tab}`));
    if (tab === "agents") loadAgentsStudio();
    if (tab === "settings") {
      loadAccountSettings();
      loadSettings();
    }
    if (tab === "chats") refreshChatRails();
  }

  // --- Chats -----------------------------------------------------------------

  async function refreshChatRails() {
    if (state.me?.authenticated) {
      const [rooms, threads, specialists, models] = await Promise.all([
        api("/api/rooms/mine"),
        api("/api/agent-chats"),
        api("/api/specialists"),
        api("/api/settings/models"),
      ]);
      state.rooms = (rooms.data && rooms.data.rooms) || [];
      state.threads = (threads.data && threads.data.threads) || [];
      state.specialists = (specialists.data && specialists.data.specialists) || [];
      state.compute = models.data?.active || null;
      state.modelProfiles = models.data?.profiles || [];
      state.activeProfileId = models.data?.active_profile_id || null;
    } else {
      state.rooms = state.me?.room_id
        ? [{ room_id: state.me.room_id, title: state.me.room_title || "Room" }]
        : [];
      state.threads = [];
    }
    renderRails();
  }

  function roomAgents(room) {
    const config = room?.config || {};
    return config.agents || config.specialists || [];
  }

  function allRoomEntries() {
    return [
      ...state.rooms.map((room) => ({
        id: room.room_id,
        title: room.title,
        surface: "people",
        room,
      })),
      ...state.threads.map((thread) => ({
        id: thread.session_id,
        title: thread.title,
        surface: "agent",
        thread,
      })),
    ];
  }

  function renderRails() {
    const list = $("#room-list");
    const palette = $("#agent-palette");
    list.innerHTML = "";
    palette.innerHTML = "";
    const entries = allRoomEntries();

    if (!entries.length) {
      const li = document.createElement("li");
      li.innerHTML = '<button type="button" class="muted" disabled>No teams yet</button>';
      list.appendChild(li);
    }
    entries.forEach((entry) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      const agents = entry.room ? roomAgents(entry.room) : [];
      const compute = entry.room?.compute || (entry.thread ? state.compute : null);
      const meta = compute
        ? `${compute.local ? "Local" : "API"} · ${compute.label || compute.model}`
        : (entry.thread?.master ? "Private room" : "Ongoing room");
      btn.innerHTML = `
        <span class="room-name"><span class="room-icon">#</span><span class="room-title">${escapeHtml(entry.title || entry.id)}</span></span>
        <span class="meta">${escapeHtml(meta)}</span>
        ${agents.length ? `<span class="room-agents" aria-label="${agents.length} agents">${agents.map(() => '<i class="room-agent-dot"></i>').join("")}</span>` : ""}
      `;
      if (
        (entry.surface === "people" && state.kind === "people" && state.roomId === entry.id) ||
        (entry.surface === "agent" && state.kind === "agents" && state.threadId === entry.id)
      ) {
        btn.classList.add("active");
      }
      if (entry.surface === "people") {
        btn.addEventListener("click", () => selectPeople(entry.id, entry.title, entry.room));
        makeRoomDropTarget(btn, entry.id);
      } else {
        btn.addEventListener("click", () => {
          selectAgent(entry.id, entry.title, !!entry.thread?.master);
        });
      }
      li.appendChild(btn);
      list.appendChild(li);
    });

    if (!state.specialists.length) {
      const li = document.createElement("li");
      li.className = "muted tiny-hint";
      li.textContent = "No agents available";
      palette.appendChild(li);
    }
    state.specialists.forEach((agent) => {
      const li = document.createElement("li");
      li.className = "agent-card";
      li.draggable = true;
      li.dataset.agentId = agent.id;
      const kind = agent.kind === "operator" ? "operator" : "lens";
      li.title = `${agent.name} (${kind}) — click to add to the open room, or drag onto a room`;
      const caps = (agent.capabilities || []).length
        ? ` · ${(agent.capabilities || []).slice(0, 2).join(", ")}`
        : " · prompt only";
      li.innerHTML = `<strong>${escapeHtml(agent.name)}</strong><span>${escapeHtml(agent.mention || agent.role)}${escapeHtml(caps)}</span>`;
      li.addEventListener("dragstart", (event) => {
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData("application/x-workflow-agent", agent.id);
        event.dataTransfer.setData("text/plain", agent.id);
        li.classList.add("dragging");
      });
      li.addEventListener("dragend", () => li.classList.remove("dragging"));
      // Click also adds to the current people room (drag is easy to miss).
      li.addEventListener("click", async () => {
        if (state.kind === "people" && state.roomId) {
          await addAgentToRoom(state.roomId, agent.id);
        }
      });
      palette.appendChild(li);
    });
  }

  function makeRoomDropTarget(element, roomId) {
    element.addEventListener("dragover", (event) => {
      if (!event.dataTransfer.types.includes("application/x-workflow-agent")) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      element.classList.add("drop-ready");
    });
    element.addEventListener("dragleave", () => element.classList.remove("drop-ready"));
    element.addEventListener("drop", async (event) => {
      event.preventDefault();
      element.classList.remove("drop-ready");
      const agentId =
        event.dataTransfer.getData("application/x-workflow-agent") ||
        event.dataTransfer.getData("text/plain");
      if (agentId) await addAgentToRoom(roomId, agentId);
    });
  }

  async function addAgentToRoom(roomId, agentId) {
    setError("#chat-error", "");
    const { res, data } = await api(`/api/rooms/${encodeURIComponent(roomId)}/agents`, {
      method: "POST",
      body: JSON.stringify({ agent_id: agentId }),
    });
    if (!res.ok) {
      setError("#chat-error", data?.error || "Could not add agent");
      return;
    }
    await refreshChatRails();
    if (state.kind === "people" && state.roomId === roomId) {
      const room = currentRoom();
      updateRoomContext(room);
      updateSpecialistActions(room);
      const hasAgents = roomAgents(room).length > 0;
      enableComposer(
        true,
        hasAgents
          ? "Message the team… /automate opens Harness"
          : "Message… @Analyst · /automate opens Harness"
      );
    }
  }

  function currentRoom() {
    return state.rooms.find((r) => r.room_id === state.roomId) || null;
  }

  function updateSpecialistActions(room) {
    // Present / Debate / Ideas trimmed — Orchestrator handles multi-agent from chat.
    // Keep stop/banner wiring for Orchestrator + Workflow harness runs.
    const hasAgents = roomAgents(room).length > 0;
    if (!hasAgents) {
      setSpecialistRunUi(null);
    }
  }

  function syncAutonomyToggle(room) {
    const wrap = $("#autonomy-toggle-wrap");
    const toggle = $("#autonomy-toggle");
    if (!wrap || !toggle) return;
    const orch = String(room?.config?.orchestrator || "chat").toLowerCase();
    const enabled = !!(room?.config?.autonomy?.enabled);
    // Only show for Workflow mode — Debate/Present UI was removed.
    if (orch === "workflow") {
      show(wrap);
      toggle.checked = enabled;
    } else {
      hide(wrap);
      toggle.checked = false;
    }
  }

  function updateRoomContext(room) {
    const badge = $("#compute-badge");
    const members = $("#room-members");
    const modelWrap = $("#room-model-wrap");
    const modelSelect = $("#room-model-select");
    const startLocal = $("#start-local-model-btn");
    members.innerHTML = "";
    if (room && state.me?.authenticated && modelSelect) {
      fillRoomModelSelect(room);
      show(modelWrap);
      syncComputeBadgeFromSelect(room);
    } else if (room?.compute) {
      const source = room.compute.local ? "Local" : "API";
      badge.textContent = `Using · ${room.compute.label || room.compute.model} · ${source}`;
      show(badge);
      hide(modelWrap);
    } else {
      hide(modelWrap);
      hide(badge);
    }
    if (state.me?.authenticated && startLocal) show(startLocal);
    else hide(startLocal);
    roomAgents(room).forEach((agentId) => {
      const agent = state.specialists.find((item) => item.id === agentId);
      const chip = document.createElement("span");
      chip.className = "member-chip";
      const mention = agent?.mention ? ` ${agent.mention}` : "";
      chip.appendChild(
        document.createTextNode(`${agent?.name || agentId}${mention}`)
      );
      if (room?.owner_user_id === state.me?.user_id) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.setAttribute("aria-label", `Remove ${agent?.name || agentId}`);
        remove.textContent = "×";
        remove.addEventListener("click", async () => {
          const { res, data } = await api(
            `/api/rooms/${encodeURIComponent(room.room_id)}/agents/${encodeURIComponent(agentId)}`,
            { method: "DELETE" }
          );
          if (!res.ok) {
            setError("#chat-error", data?.error || "Could not remove agent");
            return;
          }
          await refreshChatRails();
          updateRoomContext(currentRoom());
          updateSpecialistActions(currentRoom());
        });
        chip.appendChild(remove);
      }
      members.appendChild(chip);
    });
  }

  function syncComputeBadgeFromSelect(room) {
    const badge = $("#compute-badge");
    const select = $("#room-model-select");
    if (!badge || !select) return;
    const profiles = state.modelProfiles || [];
    const accountActiveId = state.compute?.id || state.compute?.profile_id || null;
    const selectedId = select.value || accountActiveId || "";
    const profile =
      (selectedId && profiles.find((p) => p.id === selectedId)) ||
      profiles.find((p) => p.id === accountActiveId) ||
      null;
    if (!profile && !room?.compute) {
      hide(badge);
      return;
    }
    const label =
      (profile && (profile.label || profile.model)) ||
      room?.compute?.label ||
      room?.compute?.model ||
      "Model";
    const isLocal = profile
      ? !!profile.is_local
      : !!room?.compute?.local;
    const unreachable =
      profile &&
      profile.category === "open_source" &&
      profile.reachable === false;
    badge.textContent = unreachable
      ? `Using · ${label} · offline`
      : `Using · ${label} · ${isLocal ? "Local" : "API"}`;
    badge.classList.toggle("warn", !!unreachable);
    show(badge);
  }

  function fillRoomModelSelect(room) {
    const select = $("#room-model-select");
    if (!select) return;
    const profiles = state.modelProfiles || [];
    const activeId = state.compute?.id || state.compute?.profile_id || null;
    const roomOverride = (room?.config || {}).model_profile_id || null;
    const current = roomOverride || "";
    select.innerHTML = "";
    const accountOpt = document.createElement("option");
    accountOpt.value = "";
    const activeProfile = profiles.find((p) => p.id === activeId);
    const activeLabel =
      (activeProfile && (activeProfile.label || activeProfile.model)) ||
      (state.compute && (state.compute.label || state.compute.model)) ||
      "";
    accountOpt.textContent = activeLabel
      ? `Account default · ${activeLabel}`
      : "Account default (set in Settings)";
    select.appendChild(accountOpt);
    profiles.forEach((p) => {
      // Show every saved profile so Local and Claude both appear in one menu.
      if (p.category === "open_source" && p.setup_complete === false) return;
      const opt = document.createElement("option");
      opt.value = p.id;
      const kind = p.is_local
        ? "Local"
        : p.provider_label || p.provider || "API";
      const offline =
        p.category === "open_source" && p.reachable === false ? " · offline" : "";
      const onMark = p.id === (roomOverride || activeId) ? " ✓" : "";
      opt.textContent = `${p.label || p.model} · ${kind}${offline}${onMark}`;
      select.appendChild(opt);
    });
    select.value = current;
    // If stored override was removed from profiles, fall back to account default.
    if (current && select.value !== current) {
      select.value = "";
    }
    select.disabled = room?.owner_user_id && room.owner_user_id !== state.me?.user_id;
  }

  function setSpecialistRunUi(job) {
    const banner = $("#specialist-run-banner");
    const stopBtn = $("#specialist-stop-btn");
    const text = $("#specialist-run-text");
    if (!job || job.status !== "running") {
      hide(banner);
      if (stopBtn) hide(stopBtn);
      state.specialistJob = null;
      if (state.specialistPoll) {
        clearInterval(state.specialistPoll);
        state.specialistPoll = null;
      }
      return;
    }
    state.specialistJob = job;
    const loopBit = job.continuous
      ? `loop ${job.round_num || "…"}`
      : `round ${job.round_num || "?"}/${job.rounds || "?"}`;
    const topicBit = job.topic ? ` “${job.topic}”` : "";
    if (text) {
      text.textContent = job.continuous
        ? `Looping${topicBit || " harness"} (${loopBit}) — safe to leave; turns keep posting.`
        : `Running ${job.action || "harness"}${topicBit} (${loopBit}) — safe to leave this team.`;
    }
    show(banner);
    if (stopBtn) show(stopBtn);
    if (!state.specialistPoll && state.roomId) {
      state.specialistPoll = setInterval(() => {
        if (state.roomId) refreshSpecialistStatus(state.roomId);
      }, 4000);
    }
  }

  async function refreshSpecialistStatus(roomId) {
    if (!roomId) {
      setSpecialistRunUi(null);
      return;
    }
    const room = currentRoom() || state.rooms.find((r) => r.room_id === roomId);
    const agents = room ? roomAgents(room) : [];
    const skills = room?.config?.skills || [];
    if (room && agents.length === 0 && skills.length === 0) {
      setSpecialistRunUi(null);
      return;
    }
    const { res, data } = await api(`/api/rooms/${roomId}/specialist-status`);
    if (!res.ok) {
      setSpecialistRunUi(null);
      return;
    }
    setSpecialistRunUi(data?.running ? data.job : null);
  }

  async function stopSpecialistRun() {
    if (!state.roomId) return;
    setError("#chat-error", "");
    const { res, data } = await api(`/api/rooms/${state.roomId}/specialist-stop`, {
      method: "POST",
      body: "{}",
    });
    if (!res.ok) {
      setError("#chat-error", data?.error || "Stop failed");
      return;
    }
    if (data?.job) {
      setSpecialistRunUi({ ...data.job, status: "running", stop_requested: true });
      $("#specialist-run-text").textContent =
        "Stop requested — finishing the current turn…";
    } else {
      setSpecialistRunUi(null);
    }
  }

  function modelStatusLabel(profile, { isActive, inRoom }) {
    const label = (profile && (profile.label || profile.model)) || "Model";
    const provider = (profile && (profile.provider_label || profile.provider)) || "";
    const name = provider && provider !== label ? `${label} · ${provider}` : label;
    if (profile && profile.category === "open_source" && profile.reachable === false) {
      if (inRoom && isActive) return `${name} — unreachable (Start local model)`;
      return isActive
        ? `${name} is on but unreachable`
        : `${name} is unreachable`;
    }
    if (inRoom && isActive) return `${name} — this room`;
    return isActive ? `${name} is active` : `${name} is available`;
  }

  function renderModelStatus(payload) {
    // Room model UI lives only in the top-bar select + "Using · …" badge.
    // Keep profiles in state for the select; do not render the old status panel.
    if (payload && Array.isArray(payload.profiles)) {
      state.modelProfiles = payload.profiles;
    }
    if (state.kind === "people") {
      const room = currentRoom();
      if (room) {
        fillRoomModelSelect(room);
        syncComputeBadgeFromSelect(room);
      }
    }
  }

  async function refreshModelStatus() {
    if (!state.me?.authenticated) {
      renderModelStatus(null);
      return;
    }
    const { res, data } = await api("/api/settings/models");
    if (!res.ok) {
      renderModelStatus({ profiles: [], active_profile_id: null });
      return;
    }
    renderModelStatus(data);
  }

  async function selectPeople(roomId, title, roomMeta) {
    closeWs();
    state.kind = "people";
    state.roomId = roomId;
    state.threadId = null;
    const room = roomMeta || currentRoom() || { room_id: roomId, title, kind: "people" };
    const hasAgents = roomAgents(room).length > 0;
    $("#stage-kind").textContent = "Team";
    $("#stage-title").textContent = title || room.title || "Team";
    show($("#clear-chat"));
    if (room?.owner_user_id && room.owner_user_id === state.me?.user_id) {
      show($("#delete-room"));
    } else {
      hide($("#delete-room"));
    }
    if (state.me?.authenticated) {
      show($("#invite-friend-btn"));
      show($("#room-design-btn"));
      syncAutonomyToggle(room);
    } else {
      hide($("#invite-friend-btn"));
      hide($("#room-design-btn"));
      hide($("#autonomy-toggle-wrap"));
    }
    enableComposer(
      true,
      hasAgents
        ? "Message the team… /automate opens Harness · agents use the model above"
        : "Message… open Harness, or @Bullish for a lens"
    );
    if (state.shareRoomId !== roomId && typeof closeShareDialog === "function") {
      closeShareDialog();
    }
    updateRoomContext(room);
    updateSpecialistActions(room);
    renderRails();
    await refreshModelStatus();

    if (state.me?.authenticated) {
      await api("/api/rooms/select", {
        method: "POST",
        body: JSON.stringify({ room_id: roomId }),
      });
    }
    const { data } = await api("/api/messages?limit=200");
    $("#messages").innerHTML = "";
    (data?.messages || []).forEach((m) => appendPeopleMessage(m, data?.me));
    openWs();
    if (hasAgents) await refreshSpecialistStatus(roomId);
    else setSpecialistRunUi(null);
  }

  async function selectAgent(threadId, title, isMaster) {
    closeWs();
    state.kind = "agents";
    state.roomId = null;
    hide($("#clear-chat"));
    hide($("#delete-room"));
    hide($("#invite-friend-btn"));
    hide($("#room-design-btn"));
    hide($("#autonomy-toggle-wrap"));
    closeShareDialog();
    if (state.compute) {
      const source = state.compute.is_local ? "Local compute" : "API compute";
      $("#compute-badge").textContent =
        `${source} · ${state.compute.label || state.compute.model}`;
      show($("#compute-badge"));
    } else {
      hide($("#compute-badge"));
    }
    hide($("#room-model-wrap"));
    if (state.me?.authenticated) show($("#start-local-model-btn"));
    else hide($("#start-local-model-btn"));
    $("#room-members").innerHTML = "";
    updateSpecialistActions(null);
    setSpecialistRunUi(null);
    $("#stage-kind").textContent = "Room";
    $("#stage-title").textContent = title || "Room";
    enableComposer(true, "Message this team… /automate opens Harness");
    await refreshModelStatus();

    if (!threadId) {
      // Ensure master exists
      const { data } = await api("/api/agent-chats");
      const master = (data?.threads || []).find((t) => t.master);
      threadId = master?.session_id;
      title = master?.title || "Master workflows";
      state.threads = data?.threads || [];
      $("#stage-title").textContent = title;
    }
    state.threadId = threadId;
    renderRails();
    $("#messages").innerHTML = "";
    if (!threadId) return;
    const { data } = await api(`/api/agent-chats/messages?thread_id=${encodeURIComponent(threadId)}`);
    (data?.messages || []).forEach((ev) => appendAgentMessage(ev));
  }

  function enableComposer(on, placeholder) {
    const input = $("#body");
    const btn = $("#send-form button");
    input.disabled = !on;
    btn.disabled = !on;
    if (placeholder) input.placeholder = placeholder;
    if (!on) hideMentionSuggest();
  }

  function mentionCandidates() {
    const room = currentRoom();
    const roster = new Set(roomAgents(room));
    const byId = new Map();
    (state.specialists || []).forEach((a) => {
      if (a?.id) byId.set(a.id, a);
    });
    // Prefer roster agents when a team is open; otherwise full palette.
    const ids = roster.size ? [...roster] : [...byId.keys()];
    return ids
      .map((id) => {
        const a = byId.get(id) || { id, name: id, mention: `@${id}` };
        const handle = (a.mention && String(a.mention).startsWith("@")
          ? a.mention
          : `@${String(a.mention || a.name || id).replace(/\s+/g, "")}`
        );
        return {
          id: a.id || id,
          name: a.name || id,
          mention: handle,
          insert: handle.startsWith("@") ? handle : `@${handle}`,
        };
      })
      .filter((a) => a.insert && a.insert !== "@");
  }

  function hideMentionSuggest() {
    const el = $("#mention-suggest");
    if (!el) return;
    el.innerHTML = "";
    hide(el);
    state.mentionActiveIndex = -1;
    state.mentionQuery = null;
  }

  function renderMentionSuggest(items, activeIndex) {
    const el = $("#mention-suggest");
    if (!el) return;
    if (!items.length) {
      hideMentionSuggest();
      return;
    }
    el.innerHTML = "";
    items.forEach((item, idx) => {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", idx === activeIndex ? "true" : "false");
      li.innerHTML = `<span class="mention-handle"></span><span class="mention-meta"></span>`;
      li.querySelector(".mention-handle").textContent = item.insert;
      li.querySelector(".mention-meta").textContent = item.name;
      li.addEventListener("mousedown", (e) => {
        e.preventDefault();
        applyMentionSuggestion(item);
      });
      el.appendChild(li);
    });
    state.mentionItems = items;
    state.mentionActiveIndex = activeIndex;
    show(el);
  }

  function mentionQueryAtCaret(value, caret) {
    const left = String(value || "").slice(0, caret ?? 0);
    const m = left.match(/(^|[\s([{])@([A-Za-z0-9_-]*)$/);
    if (!m) return null;
    return { start: left.length - (m[2].length + 1), query: m[2] };
  }

  function refreshMentionSuggest() {
    const input = $("#body");
    if (!input || input.disabled || state.kind !== "people") {
      hideMentionSuggest();
      return;
    }
    const at = mentionQueryAtCaret(input.value, input.selectionStart);
    if (!at) {
      hideMentionSuggest();
      return;
    }
    const q = at.query.toLowerCase();
    const items = mentionCandidates()
      .filter((a) => {
        const hay = `${a.insert} ${a.name} ${a.id}`.toLowerCase();
        return !q || hay.includes(q) || a.insert.toLowerCase().includes(`@${q}`);
      })
      .slice(0, 8);
    state.mentionQuery = at;
    renderMentionSuggest(items, items.length ? 0 : -1);
  }

  function applyMentionSuggestion(item) {
    const input = $("#body");
    if (!input || !item) return;
    const at = state.mentionQuery || mentionQueryAtCaret(input.value, input.selectionStart);
    if (!at) return;
    const before = input.value.slice(0, at.start);
    const after = input.value.slice(input.selectionStart || at.start);
    const insert = item.insert + " ";
    input.value = before + insert + after;
    const pos = before.length + insert.length;
    input.setSelectionRange(pos, pos);
    hideMentionSuggest();
    input.focus();
  }

  $("#body")?.addEventListener("input", () => refreshMentionSuggest());
  $("#body")?.addEventListener("keydown", (e) => {
    const el = $("#mention-suggest");
    if (!el || el.classList.contains("hidden")) return;
    const items = state.mentionItems || [];
    if (!items.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      const next = (state.mentionActiveIndex + 1) % items.length;
      renderMentionSuggest(items, next);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      const next = (state.mentionActiveIndex - 1 + items.length) % items.length;
      renderMentionSuggest(items, next);
    } else if (e.key === "Enter" || e.key === "Tab") {
      const item = items[state.mentionActiveIndex];
      if (item) {
        e.preventDefault();
        applyMentionSuggestion(item);
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      hideMentionSuggest();
    }
  });
  $("#body")?.addEventListener("blur", () => {
    setTimeout(() => hideMentionSuggest(), 120);
  });

  function appendPeopleMessage(msg, me) {
    const div = document.createElement("div");
    const mine = msg.author === (me || state.me?.name);
    const author = msg.author || "";
    const knownAgent = (state.specialists || []).some(
      (a) => a.name === author || (a.legacy_names || []).includes(author)
    );
    const agent =
      knownAgent ||
      /^(Qwen|Workflow|Analyst|Bullish Agent|Contrarian Agent|Synthesizer Agent|Moderator)/i.test(
        author
      );
    div.className = "msg" + (mine ? " mine" : "") + (agent ? " agent" : "");
    div.innerHTML =
      '<div class="meta"><span class="author"></span><span class="time"></span></div>' +
      '<div class="body"></div>';
    div.querySelector(".author").textContent = author;
    div.querySelector(".time").textContent = fmtTime(msg.created_at);
    div.querySelector(".body").textContent = msg.body || "";
    $("#messages").appendChild(div);
    $("#messages").scrollTop = $("#messages").scrollHeight;
  }

  function appendAgentMessage(ev) {
    const payload = ev.payload || {};
    const role = payload.role || "assistant";
    const div = document.createElement("div");
    div.className = "msg" + (role === "user" ? " mine" : " agent");
    if (ev.event_id) div.dataset.eventId = ev.event_id;
    if (ev.session_id) div.dataset.sessionId = ev.session_id;
    div.innerHTML =
      '<div class="meta"><span class="author"></span><span class="time"></span></div>' +
      '<div class="body"></div>';
    div.querySelector(".author").textContent = role;
    div.querySelector(".time").textContent = fmtTime(ev.ts);
    div.querySelector(".body").textContent = payload.content || "";
    const kind = ev.resolved_kind || payload.resolved_kind;
    if (kind) {
      const chip = document.createElement("span");
      chip.className = "badge kind";
      chip.textContent = kind;
      div.querySelector(".meta").appendChild(chip);
    }
    $("#messages").appendChild(div);
    $("#messages").scrollTop = $("#messages").scrollHeight;
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  $("#send-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("#chat-error", "");
    const body = $("#body").value.trim();
    if (!body) return;
    if (/^\/automate(?:\s|$)/i.test(body)) {
      $("#body").value = "";
      openRoomDesign();
      return;
    }
    if (state.kind === "people") {
      if (state.ws && state.ws.readyState === 1) {
        state.ws.send(JSON.stringify({ type: "message", body }));
        $("#body").value = "";
      } else {
        const { res, data } = await api("/api/messages", {
          method: "POST",
          body: JSON.stringify({ body }),
        });
        if (!res.ok) {
          setError("#chat-error", data?.error || "Send failed");
          return;
        }
        $("#body").value = "";
        if (data?.message) appendPeopleMessage(data.message, data.me);
      }
      return;
    }
    if (state.kind === "agents" && state.threadId) {
      $("#body").value = "";
      appendAgentMessage({
        ts: new Date().toISOString(),
        payload: { role: "user", content: body },
      });
      const { res, data } = await api("/api/agent-chats/message", {
        method: "POST",
        body: JSON.stringify({ thread_id: state.threadId, content: body }),
      });
      if (!res.ok) {
        setError("#chat-error", data?.error || data?.message || "Send failed");
        return;
      }
      if (data?.job?.job_id) pollJob(data.job.job_id);
    }
  });

  async function pollJob(jobId) {
    for (let i = 0; i < 90; i++) {
      await sleep(1500);
      const { data } = await api(`/api/agent-chats/jobs/${encodeURIComponent(jobId)}`);
      const job = data?.job;
      if (!job) continue;
      if (job.status === "completed" || job.status === "failed") {
        // Reload thread messages
        if (state.threadId) {
          const msgs = await api(
            `/api/agent-chats/messages?thread_id=${encodeURIComponent(state.threadId)}`
          );
          $("#messages").innerHTML = "";
          (msgs.data?.messages || []).forEach((ev) => appendAgentMessage(ev));
        }
        return;
      }
    }
  }

  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  function openWs() {
    closeWs();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    state.ws = ws;
    ws.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }
      if (data.type === "history") {
        $("#messages").innerHTML = "";
        (data.messages || []).forEach((m) => appendPeopleMessage(m, state.me?.name));
      } else if (data.type === "message" && data.message) {
        appendPeopleMessage(data.message, state.me?.name);
      } else if (data.type === "orch_run") {
        const job = data.job || {};
        if (job.status === "running") {
          const banner = $("#specialist-run-banner");
          const text = $("#specialist-run-text");
          const stopBanner = $("#specialist-stop-banner-btn");
          if (text) {
            text.textContent =
              job.message ||
              "Orchestrator working — safe to leave; report posts when done.";
          }
          if (stopBanner) hide(stopBanner);
          show(banner);
        } else {
          setSpecialistRunUi(null);
          const stopBanner = $("#specialist-stop-banner-btn");
          if (stopBanner) show(stopBanner);
        }
      } else if (data.type === "cleared") {
        $("#messages").innerHTML = "";
      } else if (data.type === "room_deleted") {
        leaveDeletedRoom(data.room_id);
      } else if (data.type === "error") {
        setError("#chat-error", data.error);
      }
    };
    ws.onclose = () => {
      if (state.kind === "people" && state.ws === ws) {
        setTimeout(() => { if (state.kind === "people") openWs(); }, 1500);
      }
    };
  }
  function closeWs() {
    if (state.ws) {
      try { state.ws.close(); } catch {}
      state.ws = null;
    }
  }

  $("#clear-chat").addEventListener("click", async () => {
    if (!confirm("Clear this room’s messages for everyone?")) return;
    if (state.ws && state.ws.readyState === 1) {
      state.ws.send(JSON.stringify({ type: "clear" }));
    } else {
      await api("/api/messages", { method: "DELETE" });
      $("#messages").innerHTML = "";
    }
  });

  async function leaveDeletedRoom(roomId) {
    if (state.roomId && roomId && state.roomId !== roomId) return;
    closeWs();
    state.rooms = (state.rooms || []).filter((r) => r.room_id !== roomId);
    if (state.shareRoomId === roomId) {
      state.shareRoomId = null;
      state.shareUrl = null;
      closeShareDialog();
    }
    hide($("#clear-chat"));
    hide($("#delete-room"));
    hide($("#invite-friend-btn"));
    hide($("#room-design-btn"));
    hide($("#autonomy-toggle-wrap"));
    updateSpecialistActions(null);
    setSpecialistRunUi(null);
    $("#messages").innerHTML = "";
    $("#room-members").innerHTML = "";
    hide($("#compute-badge"));
    hide($("#room-model-wrap"));
    await refreshChatRails();
    const next = state.rooms[0];
    if (next) {
      await selectPeople(next.room_id, next.title, next);
      return;
    }
    if (state.threads[0]) {
      await selectAgent(
        state.threads[0].session_id,
        state.threads[0].title,
        !!state.threads[0].master
      );
      return;
    }
    state.kind = null;
    state.roomId = null;
    state.threadId = null;
    $("#stage-kind").textContent = "Room";
    $("#stage-title").textContent = "No room selected";
    enableComposer(false, "Create or open a room to chat");
    renderRails();
  }

  $("#delete-room").addEventListener("click", async () => {
    const room = currentRoom();
    const roomId = state.roomId;
    if (!roomId || roomId === "legacy") return;
    if (room?.owner_user_id && room.owner_user_id !== state.me?.user_id) {
      setError("#chat-error", "Only the room owner can delete this room");
      return;
    }
    if (
      !confirm(
        "Delete this room for everyone? Messages and membership are permanently removed."
      )
    ) {
      return;
    }
    setError("#chat-error", "");
    const { res, data } = await api(`/api/rooms/${encodeURIComponent(roomId)}`, {
      method: "DELETE",
    });
    if (!res.ok) {
      setError("#chat-error", data?.error || "Could not delete room");
      return;
    }
    await leaveDeletedRoom(roomId);
  });

  async function loadSpecialists() {
    const { res, data } = await api("/api/specialists");
    if (!res.ok) return;
    state.specialists = data.specialists || [];
  }

  $("#new-room-btn").addEventListener("click", async () => {
    await loadSpecialists();
    setError("#new-room-error", "");
    const title = $("#new-room-title");
    if (title) title.value = "";
    const dlg = $("#new-room-dialog");
    try {
      dlg?.showModal?.();
    } catch (err) {
      setError("#chat-error", String(err.message || err));
    }
  });
  $("#new-room-cancel").addEventListener("click", () => {
    $("#new-room-dialog").close();
  });
  $("#new-room-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("#new-room-error", "");
    const title = $("#new-room-title").value.trim();
    const payload = {
      title,
      name: state.me?.name || state.me?.display_name,
      kind: "people",
    };
    const { res, data } = await api("/api/rooms", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      setError("#new-room-error", data?.error || "Create failed");
      return;
    }
    $("#new-room-dialog").close();
    await refreshChatRails();
    await selectPeople(data.room_id, data.room_title, {
      room_id: data.room_id,
      title: data.room_title,
      kind: data.kind || "people",
      config: data.config || {},
      owner_user_id: data.owner_user_id || state.me?.user_id,
    });
    // Design is the room setup surface (objective / skills / prompts).
    openRoomDesign();
  });

  async function runSpecialistAction(action, topic, rounds, continuous) {
    if (!state.roomId) return;
    setError("#chat-error", "");
    const payload = { action, topic: topic || "" };
    if (action === "debate") {
      if (continuous) {
        payload.continuous = true;
      } else {
        payload.rounds = Math.max(1, Math.min(5, Number(rounds) || 1));
      }
    }
    const { res, data } = await api(`/api/rooms/${state.roomId}/specialist-run`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      setError(
        "#chat-error",
        data?.message || data?.error || "Agent run failed"
      );
      if (data?.job) setSpecialistRunUi(data.job);
      return;
    }
    if (data?.job) setSpecialistRunUi(data.job);
  }

  // Present / Debate / Ideas header buttons removed — use chat + Orchestrator.

  $("#specialist-stop-banner-btn")?.addEventListener("click", () => stopSpecialistRun());

  function openShareDialog(shareUrl, roomId) {
    state.shareUrl = shareUrl || null;
    state.shareRoomId = roomId || null;
    $("#share-url").value = shareUrl || "";
    const dialog = $("#share-dialog");
    if (dialog && !dialog.open) dialog.showModal();
  }

  function closeShareDialog() {
    const dialog = $("#share-dialog");
    if (dialog?.open) dialog.close();
  }

  $("#copy-share").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#share-url").value);
    } catch {}
  });

  $("#invite-friend-btn").addEventListener("click", async () => {
    if (!state.roomId) return;
    setError("#chat-error", "");
    const { res, data } = await api(
      `/api/rooms/${encodeURIComponent(state.roomId)}/invite`,
      { method: "POST", body: "{}" }
    );
    if (!res.ok) {
      setError("#chat-error", data?.error || "Could not create invite");
      return;
    }
    openShareDialog(data.share_url, state.roomId);
  });

  ["dragover", "dragleave", "drop"].forEach((eventName) => {
    $("#chat-stage").addEventListener(eventName, async (event) => {
      if (!state.roomId || state.kind !== "people") return;
      if (eventName === "dragover") {
        if (!event.dataTransfer.types.includes("application/x-workflow-agent")) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        $("#chat-stage").classList.add("drop-ready");
        return;
      }
      $("#chat-stage").classList.remove("drop-ready");
      if (eventName !== "drop") return;
      event.preventDefault();
      const agentId =
        event.dataTransfer.getData("application/x-workflow-agent") ||
        event.dataTransfer.getData("text/plain");
      if (agentId) await addAgentToRoom(state.roomId, agentId);
    });
  });

  // Keyboard: j/k moves through the single room list.
  document.addEventListener("keydown", (e) => {
    if (state.tab !== "chats") return;
    if (e.target.matches("input, textarea")) return;
    if (e.key === "j" || e.key === "k") {
      const items = [
        ...state.rooms.map((r) => ({ kind: "people", id: r.room_id, title: r.title })),
        ...state.threads.map((t) => ({
          kind: "agents",
          id: t.session_id,
          title: t.title,
          master: t.master,
        })),
      ];
      if (!items.length) return;
      const cur = items.findIndex(
        (it) =>
          (state.kind === "people" && it.kind === "people" && it.id === state.roomId) ||
          (state.kind === "agents" && it.kind === "agents" && it.id === state.threadId)
      );
      const next = e.key === "j"
        ? Math.min(items.length - 1, cur + 1)
        : Math.max(0, cur < 0 ? 0 : cur - 1);
      const it = items[next];
      if (it.kind === "people") selectPeople(it.id, it.title);
      else selectAgent(it.id, it.title, !!it.master);
      e.preventDefault();
    }
  });



  function updateHarnessFlowStrip(room) {
    const strip = $("#harness-flow-strip");
    if (!strip) return;
    if (!room || state.kind !== "people") {
      hide(strip);
      strip.innerHTML = "";
      return;
    }
    const config = room.config || {};
    const agents = roomAgents(room);
    const roles = config.roles || {};
    const skills = config.skills || [];
    const orch = config.orchestrator || "chat";
    const chips = [];
    agents.forEach((id) => {
      const role = roles[id] ? ` · ${roles[id]}` : "";
      chips.push(`<span class="harness-flow-chip">${escapeHtml(id)}${escapeHtml(role)}</span>`);
    });
    if (skills.length) {
      chips.push(`<span class="harness-flow-chip">→ ${escapeHtml(skills.join(" → "))}</span>`);
    }
    chips.push(`<span class="harness-flow-chip">${escapeHtml(orch)}</span>`);
    strip.innerHTML = chips.join("") || "";
    if (chips.length) show(strip);
    else hide(strip);
  }

  async function openRoomDesign() {
    setError("#room-design-error", "");
    if (state.kind !== "people" || !state.roomId) {
      showFlowToast("Open a team first to design its harness.");
      switchTab("chats");
      return;
    }
    const room = currentRoom() || {};
    const config = room.config || {};
    $("#room-objective").value = config.objective || "";
    $("#room-prompts").value = (config.prompts || []).join("\n");
    const orch = $("#room-orchestrator");
    if (orch) orch.value = config.orchestrator || "chat";
    await Promise.all([
      fillHarnessRoles(room),
      fillRoomSkillsPicker(config.skills || []),
      fillHarnessLoops(),
    ]);
    updateHarnessCanvasPreview();
    $("#room-design-dialog")?.showModal?.();
  }

  async function fillHarnessRoles(room) {
    const list = $("#harness-roles-list");
    const select = $("#harness-add-agent-select");
    if (!list) return;
    list.innerHTML = "";
    const config = room?.config || {};
    const roles = config.roles || {};
    const agents = roomAgents(room);
    const { data } = await api("/api/registry/agents");
    const byId = {};
    (data?.agents || []).forEach((a) => {
      byId[a.id] = a;
    });
    agents.forEach((id) => {
      const row = document.createElement("div");
      row.className = "harness-role-row";
      const name = document.createElement("span");
      name.textContent = byId[id]?.name || id;
      const roleInput = document.createElement("input");
      roleInput.type = "text";
      roleInput.maxLength = 80;
      roleInput.placeholder = "Role (e.g. SEO research)";
      roleInput.dataset.agentId = id;
      roleInput.className = "harness-role-input";
      roleInput.value = roles[id] || "";
      roleInput.addEventListener("input", updateHarnessCanvasPreview);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "ghost tiny danger";
      remove.textContent = "Remove";
      remove.addEventListener("click", () => {
        row.remove();
        updateHarnessCanvasPreview();
      });
      row.appendChild(name);
      row.appendChild(roleInput);
      row.appendChild(remove);
      list.appendChild(row);
    });
    if (select) {
      select.innerHTML = "";
      const onTeam = new Set(
        $$("#harness-roles-list .harness-role-input").map((el) => el.dataset.agentId)
      );
      (data?.agents || [])
        .filter((a) => a.room_palette !== false && !onTeam.has(a.id))
        .forEach((a) => {
          const opt = document.createElement("option");
          opt.value = a.id;
          opt.textContent = a.name;
          select.appendChild(opt);
        });
    }
  }

  async function fillRoomSkillsPicker(selected) {
    const list = $("#room-skills-list");
    if (!list) return;
    list.innerHTML = "";
    const sel = new Set(selected || []);
    const { data } = await api("/api/registry/capabilities");
    const caps = (data?.capabilities || []).filter((c) => c.kind === "builtin" || c.approved);
    caps.forEach((c) => {
      const label = document.createElement("label");
      label.className = "cap-pick-row";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "room-skill";
      input.value = c.id;
      input.checked = sel.has(c.id);
      input.addEventListener("change", updateHarnessCanvasPreview);
      const text = document.createElement("span");
      const badge = c.executable
        ? '<span class="cap-badge exec">executable</span>'
        : '<span class="cap-badge prompt">prompt-only</span>';
      text.innerHTML = `<strong>${escapeHtml(c.name || c.id)}</strong>${badge}
        <span class="muted tiny-hint">${escapeHtml(c.summary || "")}</span>`;
      label.appendChild(input);
      label.appendChild(text);
      list.appendChild(label);
    });
  }

  async function fillHarnessLoops() {
    const list = $("#harness-loops-list");
    if (!list || !state.roomId) return;
    list.innerHTML = "";
    const { data } = await api("/api/automations");
    const rows = (data?.automations || []).filter(
      (a) => String(a.room_id || "") === String(state.roomId)
    );
    if (!rows.length) {
      list.innerHTML = '<p class="muted tiny-hint">No loops bound to this team yet.</p>';
      return;
    }
    rows.forEach((a) => {
      const row = document.createElement("div");
      row.className = "harness-loop-row";
      const steps = (a.capability_ids || a.steps || []).join
        ? (a.capability_ids || []).join(", ")
        : "";
      row.innerHTML = `<strong>${escapeHtml(a.name || a.ritual_id || "?")}</strong>
        <span class="muted tiny-hint">${escapeHtml(steps || a.schedule || "")}</span>
        <span class="muted tiny-hint">${a.approved ? "approved" : "draft"}</span>`;
      list.appendChild(row);
    });
  }

  function updateHarnessCanvasPreview() {
    const el = $("#harness-canvas-preview");
    if (!el) return;
    const roles = $$("#harness-roles-list .harness-role-input").map((input) => {
      const label = input.value.trim();
      const name =
        input.closest(".harness-role-row")?.querySelector("span")?.textContent ||
        input.dataset.agentId;
      return label ? `${name} (${label})` : name;
    });
    const skills = $$('#room-skills-list input[name="room-skill"]:checked').map((el) => el.value);
    const orch = $("#room-orchestrator")?.value || "chat";
    const left = roles.length ? roles.join(" · ") : "(no agents)";
    const right = skills.length ? skills.join(" → ") : "(no capabilities)";
    el.textContent = `${left}  →  ${right}  ·  ${orch}`;
  }

  $("#harness-add-agent-btn")?.addEventListener("click", async () => {
    const id = $("#harness-add-agent-select")?.value;
    if (!id || !state.roomId) return;
    await addAgentToRoom(state.roomId, id);
    await refreshChatRails();
    const room = (state.rooms || []).find((r) => r.room_id === state.roomId);
    await fillHarnessRoles(room || currentRoom());
    updateHarnessCanvasPreview();
  });

  $("#harness-loop-create-btn")?.addEventListener("click", async () => {
    setError("#room-design-error", "");
    if (!state.roomId) return;
    const name = ($("#harness-loop-name")?.value || "").trim();
    const stepsRaw = ($("#harness-loop-steps")?.value || "").trim();
    const schedule = ($("#harness-loop-schedule")?.value || "").trim() || null;
    const steps = stepsRaw
      .split(/[,\n]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (!name || !steps.length) {
      setError("#room-design-error", "Loop name and at least one capability id required");
      return;
    }
    const { res, data } = await api("/api/automations/from-chat", {
      method: "POST",
      body: JSON.stringify({
        name,
        steps,
        schedule,
        room_id: state.roomId,
      }),
    });
    if (!res.ok) {
      setError("#room-design-error", data?.error || "Could not create loop");
      return;
    }
    if ($("#harness-loop-name")) $("#harness-loop-name").value = "";
    if ($("#harness-loop-steps")) $("#harness-loop-steps").value = "";
    if ($("#harness-loop-schedule")) $("#harness-loop-schedule").value = "";
    showFlowToast(`Draft loop “${data?.ritual_id || name}” created — approve in Hire.`);
    await fillHarnessLoops();
  });

  $("#harness-run-loop-btn")?.addEventListener("click", async () => {
    setError("#room-design-error", "");
    if (!state.roomId) return;
    // Persist orchestrator as workflow for this run context if needed
    const { res, data } = await api(
      `/api/rooms/${encodeURIComponent(state.roomId)}/harness-run`,
      { method: "POST", body: JSON.stringify({ stub: false }) }
    );
    if (!res.ok) {
      setError("#room-design-error", data?.message || data?.error || "Harness run failed");
      return;
    }
    $("#room-design-dialog")?.close();
    showFlowToast(data?.message || "Workflow started");
    if (data?.job) setSpecialistRunUi(data.job);
    const msgs = await api(`/api/messages?room_id=${encodeURIComponent(state.roomId)}`);
    $("#messages").innerHTML = "";
    (msgs.data?.messages || []).forEach((m) => appendPeopleMessage(m, state.me?.name));
  });

  $("#room-orchestrator")?.addEventListener("change", updateHarnessCanvasPreview);

  $("#room-design-btn")?.addEventListener("click", () => openRoomDesign());
  $("#room-design-cancel")?.addEventListener("click", () => $("#room-design-dialog")?.close());
  $("#room-design-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("#room-design-error", "");
    if (!state.roomId) return;
    const skills = $$('#room-skills-list input[name="room-skill"]:checked').map((el) => el.value);
    const agents = $$("#harness-roles-list .harness-role-input").map((el) => el.dataset.agentId);
    const roles = {};
    $$("#harness-roles-list .harness-role-input").forEach((el) => {
      roles[el.dataset.agentId] = el.value.trim();
    });
    const { res, data } = await api(`/api/rooms/${encodeURIComponent(state.roomId)}/config`, {
      method: "PATCH",
      body: JSON.stringify({
        objective: $("#room-objective").value,
        prompts: $("#room-prompts").value,
        skills,
        agents,
        roles,
        orchestrator: $("#room-orchestrator")?.value || "chat",
      }),
    });
    if (!res.ok) {
      setError("#room-design-error", data?.error || "Could not save");
      return;
    }
    $("#room-design-dialog")?.close();
    showFlowToast("Harness saved — turn on Run harness to let the team work.");
    await refreshChatRails();
    if (state.roomId) {
      const room = (state.rooms || []).find((r) => r.room_id === state.roomId);
      if (room) await selectPeople(room.room_id, room.title, room);
    }
  });

  $("#autonomy-toggle")?.addEventListener("change", async () => {
    if (!state.roomId) return;
    const enabled = !!$("#autonomy-toggle").checked;
    const { res, data } = await api(`/api/rooms/${encodeURIComponent(state.roomId)}/autonomy`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    if (!res.ok) {
      $("#autonomy-toggle").checked = !enabled;
      setError("#chat-error", data?.message || data?.error || "Harness failed");
      return;
    }
    showFlowToast(data?.message || (enabled ? "Harness on" : "Harness off"));
    await refreshChatRails();
    if (state.roomId) {
      const msgs = await api(`/api/messages?room_id=${encodeURIComponent(state.roomId)}`);
      $("#messages").innerHTML = "";
      (msgs.data?.messages || []).forEach((m) => appendPeopleMessage(m, state.me?.name));
    }
  });

  // --- Hire studio ---------------------------------------------------------

  state.builderDropped = [];
  state.editingAgentId = null;
  state.teamPickerAgentId = null;
  state.capCreateMode = "describe";

  function setCapCreateMode(mode) {
    state.capCreateMode = mode === "manual" ? "manual" : "describe";
    const describe = state.capCreateMode === "describe";
    $("#cap-mode-describe")?.classList.toggle("active", describe);
    $("#cap-mode-describe")?.classList.toggle("ghost", !describe);
    $("#cap-mode-manual")?.classList.toggle("active", !describe);
    $("#cap-mode-manual")?.classList.toggle("ghost", describe);
    if (describe) {
      show($("#cap-describe-panel"));
      hide($("#cap-manual-panel"));
    } else {
      hide($("#cap-describe-panel"));
      show($("#cap-manual-panel"));
    }
  }

  async function openTeamPicker(agent) {
    setError("#team-picker-error", "");
    state.teamPickerAgentId = agent?.id || null;
    const label = $("#team-picker-agent-label");
    if (label) label.textContent = agent ? `Assign ${agent.name} to a team.` : "";
    const select = $("#team-picker-select");
    if (!select) return;
    select.innerHTML = "";
    await refreshChatRails();
    const rooms = (state.rooms || []).filter((r) => (r.kind || "people") !== "agent");
    if (!rooms.length) {
      showFlowToast("Create a team first, then assign agents.");
      switchTab("chats");
      return;
    }
    rooms.forEach((r) => {
      const opt = document.createElement("option");
      opt.value = r.room_id;
      opt.textContent = r.title || r.room_id;
      if (state.roomId && state.roomId === r.room_id) opt.selected = true;
      select.appendChild(opt);
    });
    $("#team-picker-dialog")?.showModal?.();
  }

  $("#team-picker-cancel")?.addEventListener("click", () => $("#team-picker-dialog")?.close());
  $("#team-picker-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("#team-picker-error", "");
    const roomId = $("#team-picker-select")?.value;
    const agentId = state.teamPickerAgentId;
    if (!roomId || !agentId) return;
    await addAgentToRoom(roomId, agentId);
    $("#team-picker-dialog")?.close();
    showFlowToast("Agent assigned to team");
    await refreshChatRails();
    const room = (state.rooms || []).find((r) => r.room_id === roomId);
    if (room) await selectPeople(room.room_id, room.title, room);
    switchTab("chats");
  });

  function showFlowToast(message, { tab } = {}) {
    const el = $("#flow-toast");
    if (!el) return;
    el.textContent = message;
    show(el);
    clearTimeout(showFlowToast._t);
    showFlowToast._t = setTimeout(() => hide(el), 5200);
    if (tab) switchTab(tab);
  }

  function openStudioDialog(sel) {
    const dlg = $(sel);
    if (!dlg?.showModal) {
      setError("#agents-tab-error", "Dialog unavailable in this browser");
      return;
    }
    try {
      dlg.showModal();
    } catch (err) {
      setError("#agents-tab-error", String(err.message || err));
    }
  }

  async function approveCapability(ritualId) {
    const { res, data } = await api("/api/capabilities/approve", {
      method: "POST",
      body: JSON.stringify({ ritual_id: ritualId }),
    });
    if (!res.ok) {
      setError("#agents-tab-error", data?.error || "Approve failed");
      return false;
    }
    showFlowToast(`“${ritualId}” approved — add it to an agent or a team harness allowlist.`, {
      tab: "agents",
    });
    loadAgentsStudio();
    return true;
  }

  function studioCard(title, meta, body, actions) {
    const card = document.createElement("article");
    card.className = "agent-dir-card";
    card.innerHTML = `<header><strong></strong><span class="badge"></span></header><p class="muted tiny-hint"></p><p></p><div class="card-actions"></div>`;
    card.querySelector("strong").textContent = title;
    card.querySelector(".badge").textContent = meta;
    card.querySelector(".tiny-hint").textContent = body || "";
    const actionsEl = card.querySelector(".card-actions");
    (actions || []).forEach((btn) => actionsEl.appendChild(btn));
    return card;
  }

  function selectedStudioAgentIds() {
    return $$("#studio-agents input.studio-agent-check:checked").map((el) => el.value);
  }

  function syncStudioBulkButtons() {
    const ids = selectedStudioAgentIds();
    const editBtn = $("#edit-selected-agent-btn");
    const delBtn = $("#delete-selected-agents-btn");
    if (editBtn) editBtn.disabled = ids.length !== 1;
    if (delBtn) delBtn.disabled = ids.length < 1;
  }

  function compactLibraryRow(title, meta, body, actions) {
    const row = document.createElement("div");
    row.className = "studio-compact-row";
    const text = document.createElement("div");
    text.innerHTML = `<strong></strong><p class="muted tiny-hint"></p>`;
    text.querySelector("strong").textContent = title;
    const hint = meta ? `${meta}${body ? " · " + body : ""}` : body || "";
    text.querySelector(".tiny-hint").textContent = hint;
    row.appendChild(text);
    if (actions?.length) {
      const wrap = document.createElement("div");
      wrap.className = "card-actions";
      actions.forEach((btn) => wrap.appendChild(btn));
      row.appendChild(wrap);
    }
    return row;
  }

  async function loadAgentsStudio() {
    setError("#agents-tab-error", "");
    const [agents, lenses, caps] = await Promise.all([
      api("/api/registry/agents"),
      api("/api/registry/lenses"),
      api("/api/registry/capabilities"),
    ]);
    const agentsEl = $("#studio-agents");
    const lensesEl = $("#studio-lenses");
    const capsEl = $("#studio-caps");
    const emptyEl = $("#studio-agents-empty");
    if (agentsEl) agentsEl.innerHTML = "";
    if (lensesEl) lensesEl.innerHTML = "";
    if (capsEl) capsEl.innerHTML = "";
    const selectAll = $("#studio-select-all");
    if (selectAll) selectAll.checked = false;

    const allAgents = agents.data?.agents || [];
    const yours = allAgents.filter((a) => a.editable);
    const builtins = allAgents.filter(
      (a) => !a.editable && a.id !== "master" && a.room_palette !== false
    );

    if (emptyEl) emptyEl.classList.toggle("hidden", yours.length > 0);

    function renderAgentCard(a) {
      const card = document.createElement("article");
      card.className = "studio-agent-card" + (a.editable ? " editable" : " builtin");
      card.dataset.agentId = a.id;

      if (a.editable) {
        const label = document.createElement("label");
        label.className = "studio-card-check";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.className = "studio-agent-check";
        box.value = a.id;
        box.addEventListener("change", syncStudioBulkButtons);
        label.appendChild(box);
        label.appendChild(document.createTextNode(" Select"));
        card.appendChild(label);
      }

      const head = document.createElement("header");
      const title = document.createElement("h3");
      title.textContent = a.name;
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = a.editable ? "yours" : a.kind || "built-in";
      head.appendChild(title);
      head.appendChild(badge);
      card.appendChild(head);

      const capsLabel =
        (a.capability_details || []).map((c) => c.name || c.id).join(", ") || "prompt only";
      const meta = document.createElement("p");
      meta.className = "meta";
      meta.textContent = `${a.mention || ""} · ${capsLabel}`;
      card.appendChild(meta);

      const actions = document.createElement("div");
      actions.className = "card-actions";
      const addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = a.editable ? "" : "tiny";
      addBtn.textContent = "Assign to team";
      addBtn.addEventListener("click", () => openTeamPicker(a));
      actions.appendChild(addBtn);
      if (a.editable) {
        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "ghost tiny";
        editBtn.textContent = "Edit";
        editBtn.addEventListener("click", () => openAgentBuilder(a.id));
        actions.appendChild(editBtn);
      }
      card.appendChild(actions);
      return card;
    }

    yours.forEach((a) => agentsEl?.appendChild(renderAgentCard(a)));

    const addTile = document.createElement("button");
    addTile.type = "button";
    addTile.className = "studio-add-tile";
    addTile.innerHTML = `<span class="plus">+</span><strong>Hire agent</strong><span class="muted tiny-hint">Compose from lenses &amp; capabilities</span>`;
    addTile.addEventListener("click", () => openAgentBuilder(null));
    agentsEl?.appendChild(addTile);
    syncStudioBulkButtons();

    const builtinsEl = $("#studio-builtins");
    if (builtinsEl) {
      builtinsEl.innerHTML = "";
      builtins.forEach((a) => builtinsEl.appendChild(renderAgentCard(a)));
    }
    const builtinsCount = $("#studio-builtins-count");
    if (builtinsCount) builtinsCount.textContent = `${builtins.length}`;

    const lensRows = lenses.data?.lenses || [];
    const capRows = caps.data?.capabilities || [];
    const lensCount = $("#studio-lenses-count");
    const capCount = $("#studio-caps-count");
    if (lensCount) lensCount.textContent = `${lensRows.length}`;
    if (capCount) capCount.textContent = `${capRows.length}`;

    lensRows.forEach((ln) => {
      lensesEl?.appendChild(
        compactLibraryRow(
          ln.name,
          ln.kind === "builtin" ? "built-in" : "yours",
          ln.summary || ln.mention || "",
          []
        )
      );
    });

    capRows.forEach((c) => {
      const actions = [];
      if (c.kind === "user" && !c.approved && c.ritual_id) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "tiny";
        btn.textContent = "Approve";
        btn.addEventListener("click", () => approveCapability(c.ritual_id));
        actions.push(btn);
      }
      const badge = c.executable ? "executable" : "prompt-only";
      const kindLabel = `${c.kind || "cap"} · ${badge}`;
      capsEl?.appendChild(
        compactLibraryRow(c.name || c.id, kindLabel, c.summary || c.invoke || "", actions)
      );
    });
  }

  function addBuilderItem(item) {
    if (!item?.id) return;
    if (state.builderDropped.some((x) => x.id === item.id && x.kind === item.kind)) return;
    state.builderDropped.push(item);
    renderBuilderDropped();
  }

  function renderBuilderDropped() {
    const ul = $("#builder-dropped");
    if (!ul) return;
    ul.innerHTML = "";
    state.builderDropped.forEach((item, idx) => {
      const li = document.createElement("li");
      li.textContent = `${item.kind}: ${item.name || item.id}`;
      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "ghost tiny";
      rm.textContent = "×";
      rm.addEventListener("click", () => {
        state.builderDropped.splice(idx, 1);
        renderBuilderDropped();
      });
      li.appendChild(rm);
      ul.appendChild(li);
    });
  }

  async function openAgentBuilder(editId) {
    setError("#agent-builder-error", "");
    setError("#agents-tab-error", "");
    state.builderDropped = [];
    state.editingAgentId = editId || null;
    const nameEl = $("#agent-builder-name");
    const promptEl = $("#agent-builder-prompt");
    const idEl = $("#agent-builder-id");
    const titleEl = $("#agent-builder-title");
    const saveEl = $("#agent-builder-save");
    const droppedEl = $("#builder-dropped");
    if (!nameEl || !droppedEl) {
      setError("#agents-tab-error", "Agent builder markup missing — refresh the page.");
      return;
    }
    nameEl.value = "";
    if (promptEl) promptEl.value = "";
    if (idEl) idEl.value = editId || "";
    if (titleEl) titleEl.textContent = editId ? "Edit agent" : "New agent";
    if (saveEl) saveEl.textContent = editId ? "Save changes" : "Create agent";
    droppedEl.innerHTML = "";

    const [lenses, caps] = await Promise.all([
      api("/api/registry/lenses"),
      api("/api/registry/capabilities"),
    ]);
    if (editId) {
      const detail = await api(`/api/registry/agents/${encodeURIComponent(editId)}`);
      if (!detail.res.ok || !detail.data?.agent) {
        setError("#agents-tab-error", detail.data?.error || "Could not load agent");
        return;
      }
      const a = detail.data.agent;
      nameEl.value = a.name || "";
      if (promptEl) promptEl.value = a.prompt || "";
      (a.lens_ids || []).forEach((id) => {
        const ln = (lenses.data?.lenses || []).find((x) => x.id === id);
        addBuilderItem({ kind: "lens", id, name: ln?.name || id });
      });
      (a.capabilities || []).forEach((id) => {
        const c = (caps.data?.capabilities || []).find((x) => x.id === id);
        addBuilderItem({ kind: "capability", id, name: c?.name || id });
      });
    }

    const lensList = $("#builder-lenses");
    const capList = $("#builder-caps");
    if (!lensList || !capList) {
      setError("#agents-tab-error", "Builder palette missing — refresh the page.");
      return;
    }
    lensList.innerHTML = "";
    capList.innerHTML = "";
    function chip(item, kind) {
      const li = document.createElement("li");
      li.className = "builder-chip";
      li.draggable = true;
      li.dataset.kind = kind;
      li.dataset.id = item.id;
      li.textContent = item.name || item.id;
      li.title = "Click to add";
      li.addEventListener("click", () =>
        addBuilderItem({ kind, id: item.id, name: item.name || item.id })
      );
      li.addEventListener("dragstart", (ev) => {
        ev.dataTransfer.setData(
          "application/x-builder",
          JSON.stringify({ kind, id: item.id, name: item.name })
        );
      });
      return li;
    }
    (lenses.data?.lenses || []).forEach((ln) => lensList.appendChild(chip(ln, "lens")));
    (caps.data?.capabilities || [])
      .filter((c) => c.kind === "builtin" || c.approved)
      .forEach((c) => capList.appendChild(chip(c, "capability")));
    openStudioDialog("#agent-builder-dialog");
  }

  const canvas = $("#builder-canvas");
  canvas?.addEventListener("dragover", (e) => {
    e.preventDefault();
    canvas.classList.add("drop-ready");
  });
  canvas?.addEventListener("dragleave", () => canvas.classList.remove("drop-ready"));
  canvas?.addEventListener("drop", (e) => {
    e.preventDefault();
    canvas.classList.remove("drop-ready");
    try {
      const item = JSON.parse(e.dataTransfer.getData("application/x-builder") || "{}");
      addBuilderItem(item);
    } catch (_) {}
  });

  $("#new-agent-btn")?.addEventListener("click", () => openAgentBuilder(null));
  $("#agent-builder-cancel")?.addEventListener("click", () => $("#agent-builder-dialog")?.close());
  $("#agent-builder-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("#agent-builder-error", "");
    const lens_ids = state.builderDropped.filter((x) => x.kind === "lens").map((x) => x.id);
    const capability_ids = state.builderDropped
      .filter((x) => x.kind === "capability")
      .map((x) => x.id);
    const prompt = ($("#agent-builder-prompt")?.value || "").trim();
    if (!lens_ids.length && !capability_ids.length && !prompt) {
      setError("#agent-builder-error", "Add a lens, capability, or prompt");
      return;
    }
    const payload = {
      name: $("#agent-builder-name").value,
      lens_ids,
      capability_ids,
      prompt,
    };
    const editId = state.editingAgentId || $("#agent-builder-id")?.value || "";
    const { res, data } = editId
      ? await api(`/api/registry/agents/${encodeURIComponent(editId)}`, {
          method: "PATCH",
          body: JSON.stringify(payload),
        })
      : await api("/api/registry/agents", {
          method: "POST",
          body: JSON.stringify(payload),
        });
    if (!res.ok) {
      setError("#agent-builder-error", data?.error || "Could not save");
      return;
    }
    $("#agent-builder-dialog")?.close();
    state.editingAgentId = null;
    showFlowToast(
      editId
        ? `Updated “${data.agent?.name || "agent"}”`
        : `Agent “${data.agent?.name || "created"}” ready — add it to a room.`
    );
    loadAgentsStudio();
    refreshChatRails();
  });

  $("#studio-select-all")?.addEventListener("change", () => {
    const on = !!$("#studio-select-all").checked;
    $$("#studio-agents input.studio-agent-check").forEach((el) => {
      el.checked = on;
    });
    syncStudioBulkButtons();
  });

  $("#edit-selected-agent-btn")?.addEventListener("click", () => {
    const ids = selectedStudioAgentIds();
    if (ids.length === 1) openAgentBuilder(ids[0]);
  });

  $("#delete-selected-agents-btn")?.addEventListener("click", async () => {
    const ids = selectedStudioAgentIds();
    if (!ids.length) return;
    if (!confirm(`Delete ${ids.length} custom agent${ids.length === 1 ? "" : "s"}? Built-ins are never deleted.`)) {
      return;
    }
    const { res, data } = await api("/api/registry/agents/delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    if (!res.ok) {
      setError("#agents-tab-error", data?.error || "Delete failed");
      return;
    }
    showFlowToast(`Deleted ${(data.deleted || []).length} agent(s)`);
    loadAgentsStudio();
    refreshChatRails();
  });

  $("#new-lens-btn")?.addEventListener("click", () => {
    setError("#lens-new-error", "");
    $("#lens-name").value = "";
    $("#lens-prompt").value = "";
    openStudioDialog("#lens-new-dialog");
  });
  $("#lens-new-cancel")?.addEventListener("click", () => $("#lens-new-dialog")?.close());
  $("#lens-new-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const { res, data } = await api("/api/registry/lenses", {
      method: "POST",
      body: JSON.stringify({ name: $("#lens-name").value, prompt: $("#lens-prompt").value }),
    });
    if (!res.ok) {
      setError("#lens-new-error", data?.error || "Failed");
      return;
    }
    $("#lens-new-dialog")?.close();
    showFlowToast("Lens created");
    loadAgentsStudio();
  });

  $("#new-cap-btn")?.addEventListener("click", () => {
    setError("#cap-new-error", "");
    if ($("#cap-name")) $("#cap-name").value = "";
    if ($("#cap-summary")) $("#cap-summary").value = "";
    if ($("#cap-describe")) $("#cap-describe").value = "";
    setCapCreateMode("describe");
    openStudioDialog("#cap-new-dialog");
  });
  $("#cap-mode-describe")?.addEventListener("click", () => setCapCreateMode("describe"));
  $("#cap-mode-manual")?.addEventListener("click", () => setCapCreateMode("manual"));
  $("#cap-new-cancel")?.addEventListener("click", () => $("#cap-new-dialog")?.close());
  $("#cap-new-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("#cap-new-error", "");
    if (state.capCreateMode === "describe") {
      const description = ($("#cap-describe")?.value || "").trim();
      if (description.length < 8) {
        setError("#cap-new-error", "Describe what the capability should do (at least a sentence).");
        return;
      }
      const { res, data } = await api("/api/registry/capabilities/draft-from-prompt", {
        method: "POST",
        body: JSON.stringify({ description }),
      });
      if (!res.ok) {
        setError("#cap-new-error", data?.error || "Draft failed");
        return;
      }
      $("#cap-new-dialog")?.close();
      if (data?.needs_script || data?.in_scope === false) {
        showFlowToast(
          data?.reason || "Out of scope for allowlisted runners — Phase 2 custom scripts needed."
        );
      } else {
        showFlowToast(
          `Draft “${data?.capability?.name || "capability"}” created — approve before live use.`
        );
      }
      loadAgentsStudio();
      return;
    }
    const { res, data } = await api("/api/registry/capabilities", {
      method: "POST",
      body: JSON.stringify({ name: $("#cap-name").value, summary: $("#cap-summary").value }),
    });
    if (!res.ok) {
      setError("#cap-new-error", data?.error || "Failed");
      return;
    }
    $("#cap-new-dialog")?.close();
    showFlowToast("Capability created");
    loadAgentsStudio();
  });

  $("#refresh-agents-tab-btn")?.addEventListener("click", loadAgentsStudio);

  // --- Account settings ------------------------------------------------------

  function settingsMessage(selector, message) {
    const el = $(selector);
    if (!el) return;
    el.textContent = message || "";
    if (message) show(el);
    else hide(el);
  }

  function switchSettingsPanel(name) {
    $$(".settings-panel").forEach((panel) => {
      panel.classList.toggle("hidden", panel.id !== `settings-panel-${name}`);
    });
    $$(".settings-nav-btn").forEach((button) => {
      button.classList.toggle("active", button.dataset.settingsPanel === name);
    });
    if (name === "models") loadSettings();
  }

  async function loadAccountSettings() {
    const { res, data } = await api("/api/auth/me");
    if (!res.ok || !data?.authenticated) return;
    state.me = { ...(state.me || {}), ...data };
    $("#settings-display-name").value = data.display_name || "";
    $("#settings-email").value = data.email || "";
    $("#who-label").textContent = data.display_name || data.name || "";
    const status = $("#settings-email-status");
    status.textContent = data.email_verified ? "Email verified" : "Email not verified";
    status.classList.toggle("unverified", !data.email_verified);
    $("#settings-member-since").textContent = data.created_at
      ? `Member since ${new Date(data.created_at).toLocaleDateString()}`
      : "";
    const count = Number(data.session_count || 1);
    $("#settings-session-count").textContent =
      `${count} active session${count === 1 ? "" : "s"}, including this browser.`;
    const twoFaOn = !!data.email_2fa_enabled;
    state.email2faEnabled = twoFaOn;
    const twoFaStatus = $("#settings-2fa-status");
    if (twoFaStatus) {
      twoFaStatus.textContent = twoFaOn
        ? "On. We’ll email a one-time code every time you log in."
        : "Off. We’ll email a one-time code at each login when enabled.";
    }
    const toggleBtn = $("#toggle-2fa-btn");
    if (toggleBtn) toggleBtn.textContent = twoFaOn ? "Disable email 2FA" : "Enable email 2FA";
  }

  $$(".settings-nav-btn").forEach((button) => {
    button.addEventListener("click", () => switchSettingsPanel(button.dataset.settingsPanel));
  });

  $("#profile-settings-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    setError("#profile-settings-error", "");
    settingsMessage("#profile-settings-message", "");
    const { res, data } = await api("/api/auth/profile", {
      method: "PATCH",
      body: JSON.stringify({ display_name: $("#settings-display-name").value }),
    });
    if (!res.ok) {
      setError(
        "#profile-settings-error",
        data?.message ||
          (data?.error === "bad_name"
            ? "Enter a display name (not just spaces)."
            : data?.error) ||
          "Could not update profile"
      );
      return;
    }
    settingsMessage("#profile-settings-message", data.message || "Profile updated.");
    await loadAccountSettings();
  });

  $("#change-password-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    setError("#password-settings-error", "");
    settingsMessage("#password-settings-message", "");
    const current = $("#settings-current-password").value;
    const next = $("#settings-new-password").value;
    const confirm = $("#settings-confirm-password").value;
    if (next !== confirm) {
      setError("#password-settings-error", "New passwords do not match.");
      return;
    }
    const { res, data } = await api("/api/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
    if (!res.ok) {
      const message = data?.error === "bad_current_password"
        ? "Current password is incorrect."
        : (data?.message || data?.error || "Could not change password");
      setError("#password-settings-error", message);
      return;
    }
    $("#change-password-form").reset();
    settingsMessage("#password-settings-message", data.message || "Password changed.");
    await loadAccountSettings();
  });

  $("#email-2fa-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    setError("#twofa-settings-error", "");
    settingsMessage("#twofa-settings-message", "");
    const enable = !state.email2faEnabled;
    const { res, data } = await api("/api/auth/email-2fa", {
      method: "POST",
      body: JSON.stringify({
        enabled: enable,
        password: $("#settings-2fa-password").value,
      }),
    });
    if (!res.ok) {
      setError(
        "#twofa-settings-error",
        data?.message || data?.error || "Could not update two-factor settings"
      );
      return;
    }
    $("#email-2fa-form").reset();
    settingsMessage("#twofa-settings-message", data.message || "Updated.");
    await loadAccountSettings();
  });

  $("#logout-other-sessions-btn")?.addEventListener("click", async () => {
    const { res, data } = await api("/api/auth/logout-other-sessions", { method: "POST" });
    if (!res.ok) {
      setError("#password-settings-error", data?.error || "Could not sign out other sessions");
      return;
    }
    settingsMessage("#password-settings-message", data.message || "Other sessions signed out.");
    await loadAccountSettings();
  });

  const themeSelect = $("#settings-theme");
  if (themeSelect) {
    themeSelect.value = localStorage.getItem(THEME_KEY) || "system";
    themeSelect.addEventListener("change", () => {
      localStorage.setItem(THEME_KEY, themeSelect.value);
      applyTheme(themeSelect.value);
      settingsMessage("#preferences-message", "Appearance saved on this browser.");
    });
  }
  const timezoneInput = $("#settings-timezone");
  if (timezoneInput) {
    timezoneInput.value = Intl.DateTimeFormat().resolvedOptions().timeZone || "Browser default";
  }
  window.matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", () => {
    if ((localStorage.getItem(THEME_KEY) || "system") === "system") applyTheme("system");
  });

  // --- Settings → Models -----------------------------------------------------

  const FRONTIER_PRESETS = {
    openrouter: {
      hint: "One key for many models. Paste your OpenRouter key (sk-or-…). Billing is on your OpenRouter account.",
      model: "anthropic/claude-sonnet-4",
      models: [
        "anthropic/claude-sonnet-4",
        "openai/gpt-4o",
        "google/gemini-2.5-flash",
      ],
    },
    anthropic: {
      hint: "Paste your Anthropic API key (sk-ant-…). Billing is on your Anthropic account.",
      model: "claude-sonnet-5",
      models: ["claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5-20251001"],
    },
    openai: {
      hint: "Paste your OpenAI API key (sk-…). Billing is on your OpenAI account.",
      model: "gpt-4o",
      models: ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o4-mini"],
    },
  };

  const settingsState = {
    profiles: [],
    activeId: null,
    companion: null,
    selectedCandidate: null,
    draftProfileId: null,
    recommendedModel: "qwen3:8b",
  };

  function applyFrontierPreset() {
    const id = $("#frontier-provider")?.value || "openrouter";
    const preset = FRONTIER_PRESETS[id] || FRONTIER_PRESETS.openrouter;
    const hint = $("#frontier-hint");
    if (hint) hint.textContent = preset.hint;
    const modelInput = $("#frontier-model");
    if (modelInput) modelInput.value = preset.model;
    const list = $("#frontier-model-suggestions");
    if (list) {
      list.innerHTML = "";
      (preset.models || []).forEach((m) => {
        const opt = document.createElement("option");
        opt.value = m;
        list.appendChild(opt);
      });
    }
  }

  function statusDot(ok) {
    return `<span class="status-dot ${ok ? "ok" : "off"}" aria-hidden="true"></span>`;
  }

  function renderProfileRow(p, { isActive }) {
    const li = document.createElement("li");
    li.className = "profile-row";
    const ready = p.setup_complete;
    const label = p.label || p.model || p.provider_label || p.provider;
    let meta;
    if (p.category === "open_source") {
      if (!ready) meta = "Needs setup";
      else if (p.reachable === false) meta = p.enabled ? "Unreachable · On" : "Unreachable · Off";
      else meta = p.enabled ? "Ready · On" : "Ready · Off";
    } else {
      meta = isActive ? "Active" : "Saved";
    }
    const routeOk = p.category !== "open_source" || p.reachable !== false;
    li.innerHTML = `
      <div class="profile-main">
        ${statusDot((isActive || p.enabled) && routeOk)}
        <div>
          <strong>${escapeHtml(label)}</strong>
          <div class="muted tiny-hint">${escapeHtml(p.provider_label || p.provider)} · ${escapeHtml(p.model || "")} · ${escapeHtml(meta)}</div>
        </div>
      </div>
      <div class="profile-actions"></div>
    `;
    const actions = li.querySelector(".profile-actions");
    if (p.category === "frontier") {
      if (!isActive) {
        const act = document.createElement("button");
        act.type = "button";
        act.className = "ghost tiny";
        act.textContent = "Set active";
        act.addEventListener("click", async () => {
          await api(`/api/settings/models/${p.id}/activate`, { method: "POST" });
          await loadSettings();
        });
        actions.appendChild(act);
      }
    } else {
      if (ready) {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = p.enabled ? "tiny" : "ghost tiny";
        toggle.textContent = p.enabled ? "On" : "Off";
        toggle.addEventListener("click", async () => {
          setError("#settings-error", "");
          const path = p.enabled
            ? `/api/settings/models/${p.id}/disable`
            : `/api/settings/models/${p.id}/enable`;
          const { res, data } = await api(path, { method: "POST" });
          if (!res.ok) {
            setError("#settings-error", data?.message || data?.error || "Could not update");
            return;
          }
          await loadSettings();
        });
        actions.appendChild(toggle);
      } else {
        const finish = document.createElement("button");
        finish.type = "button";
        finish.className = "ghost tiny";
        finish.textContent = "Finish setup";
        finish.addEventListener("click", () => {
          settingsState.draftProfileId = p.id;
          settingsState.selectedCandidate = {
            id: (p.source && p.source.candidate_id) || p.id,
            label: p.model,
            runtime: p.runtime || "ollama",
            model: p.model,
          };
          openOsWizard("connect");
        });
        actions.appendChild(finish);
      }
    }
    const del = document.createElement("button");
    del.type = "button";
    del.className = "ghost tiny danger";
    del.textContent = "Remove";
    del.addEventListener("click", async () => {
      await api(`/api/settings/models/${p.id}`, { method: "DELETE" });
      await loadSettings();
    });
    actions.appendChild(del);
    return li;
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function loadSettings() {
    setError("#settings-error", "");
    const { res, data } = await api("/api/settings/models");
    if (!res.ok) {
      $("#settings-active-line").textContent = "Could not load settings";
      return;
    }
    settingsState.profiles = data.profiles || [];
    settingsState.activeId = data.active_profile_id;
    settingsState.companion = data.companion || {};
    const active = data.active;
    if (active) {
      $("#settings-active-line").textContent =
        `${active.label || active.model} · ${active.provider_label || active.provider}` +
        (active.is_local ? " · local" : "");
    } else {
      $("#settings-active-line").textContent = "None — add a frontier or local model below";
    }
    renderModelStatus(data);

    const frontier = $("#frontier-list");
    const osList = $("#os-list");
    frontier.innerHTML = "";
    osList.innerHTML = "";
    (settingsState.profiles || []).forEach((p) => {
      const row = renderProfileRow(p, { isActive: p.id === settingsState.activeId });
      if (p.category === "open_source") osList.appendChild(row);
      else frontier.appendChild(row);
    });
    if (![...frontier.children].length) {
      frontier.innerHTML = `<li class="muted tiny-hint">No frontier models yet.</li>`;
    }
    if (![...osList.children].length) {
      osList.innerHTML = `<li class="muted tiny-hint">No open-source models yet. Click “Add your own”.</li>`;
    }
    await refreshDurableTunnelCard();
  }

  async function refreshDurableTunnelCard() {
    const statusEl = $("#durable-tunnel-status");
    if (!statusEl) return;
    setError("#durable-tunnel-error", "");
    try {
      const res = await fetch("http://127.0.0.1:8791/tunnel/config");
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        statusEl.textContent = "Local Companion not reachable — start it on this Mac first.";
        return;
      }
      if (data.configured) {
        const bits = [
          data.reachable ? "online" : data.running ? "starting…" : "offline",
          data.public_base_url || "",
        ].filter(Boolean);
        statusEl.textContent = `Durable · ${bits.join(" · ")}`;
        if (data.public_base_url && $("#durable-tunnel-url")) {
          $("#durable-tunnel-url").value = data.public_base_url;
        }
      } else {
        statusEl.textContent =
          "Using temporary tunnels (new random name each time). Set up durable hostname below.";
      }
    } catch {
      statusEl.textContent =
        "Local Companion not running. Start Companion on this Mac, then set up a durable tunnel.";
    }
  }

  async function saveDurableTunnelFromBrowser() {
    setError("#durable-tunnel-error", "");
    const token = ($("#durable-tunnel-token")?.value || "").trim();
    const public_base_url = ($("#durable-tunnel-url")?.value || "").trim();
    const btn = $("#durable-tunnel-save-btn");
    if (btn) btn.disabled = true;
    try {
      const res = await fetch("http://127.0.0.1:8791/tunnel/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, public_base_url }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError("#durable-tunnel-error", data.error || "Could not save durable tunnel");
        return;
      }
      if ($("#durable-tunnel-token")) $("#durable-tunnel-token").value = "";
      await refreshDurableTunnelCard();
      setError("#chat-error", "");
      const statusEl = $("#durable-tunnel-status");
      if (statusEl) statusEl.textContent = data.message || statusEl.textContent;
    } catch {
      setError(
        "#durable-tunnel-error",
        "Could not reach Local Companion at 127.0.0.1:8791. Start it on this Mac first."
      );
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function clearDurableTunnelFromBrowser() {
    setError("#durable-tunnel-error", "");
    try {
      await fetch("http://127.0.0.1:8791/tunnel/config", { method: "DELETE" });
      if ($("#durable-tunnel-url")) $("#durable-tunnel-url").value = "";
      if ($("#durable-tunnel-token")) $("#durable-tunnel-token").value = "";
      await refreshDurableTunnelCard();
    } catch {
      setError("#durable-tunnel-error", "Could not reach Local Companion.");
    }
  }

  function showWizardStep(name) {
    ["companion", "search", "connect", "done"].forEach((s) => {
      const el = $(`#os-step-${s}`);
      if (!el) return;
      if (s === name) show(el);
      else hide(el);
    });
  }

  function openOsWizard(step) {
    show($("#os-wizard"));
    const comp = settingsState.companion || {};
    if (step === "connect" || step === "done") {
      showWizardStep(step);
      if (step === "connect" && settingsState.selectedCandidate) {
        $("#os-selected-label").textContent =
          settingsState.selectedCandidate.label || settingsState.selectedCandidate.model || "";
      }
      return;
    }
    if (!comp.linked || !comp.reachable) showWizardStep("companion");
    else showWizardStep(step || "search");
  }

  function closeOsWizard() {
    hide($("#os-wizard"));
    settingsState.selectedCandidate = null;
    settingsState.draftProfileId = null;
  }

  async function runDiscover() {
    setError("#settings-error", "");
    hide($("#os-empty"));
    const list = $("#os-candidates");
    list.innerHTML = `<li class="muted">Searching…</li>`;
    const { res, data } = await api("/api/settings/local-model/discover", { method: "POST" });
    list.innerHTML = "";
    if (!res.ok) {
      if (data?.error === "needs_companion" || data?.error === "companion_unreachable") {
        showWizardStep("companion");
        setError("#companion-error", data.message || data.error);
        return;
      }
      list.innerHTML = `<li class="error">${escapeHtml(data?.message || data?.error || "Search failed")}</li>`;
      return;
    }
    settingsState.recommendedModel = data.recommended_model || "qwen3:8b";
    const candidates = data.candidates || [];
    if (!candidates.length) {
      show($("#os-empty"));
      const ollama = data.ollama || {};
      if (!ollama.installed) {
        $("#os-empty-msg").textContent = "Ollama isn’t installed on this computer.";
        show($("#os-install-ollama"));
      } else if (!ollama.reachable) {
        $("#os-empty-msg").textContent = "Ollama is installed but not running. Open the Ollama app, then search again.";
        hide($("#os-install-ollama"));
      } else {
        $("#os-empty-msg").textContent = "No models found. Download a recommended model to get started.";
        hide($("#os-install-ollama"));
      }
      return;
    }
    hide($("#os-empty"));
    candidates.forEach((c) => {
      const li = document.createElement("li");
      li.className = "profile-row";
      li.innerHTML = `
        <div class="profile-main">
          ${statusDot(true)}
          <div>
            <strong>${escapeHtml(c.label)}</strong>
            <div class="muted tiny-hint">${escapeHtml(c.runtime)}${c.size_bytes ? " · " + Math.round(c.size_bytes / 1e9 * 10) / 10 + " GB" : ""}</div>
          </div>
        </div>
        <div class="profile-actions"></div>
      `;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tiny";
      btn.textContent = "Select";
      btn.addEventListener("click", () => {
        settingsState.selectedCandidate = {
          ...c,
          model: c.label,
        };
        settingsState.draftProfileId = null;
        $("#os-selected-label").textContent = c.label;
        showWizardStep("connect");
      });
      li.querySelector(".profile-actions").appendChild(btn);
      list.appendChild(li);
    });
  }

  async function pollPull(jobId) {
    const statusEl = $("#os-pull-status");
    show(statusEl);
    for (let i = 0; i < 120; i++) {
      const { res, data } = await api(`/api/settings/local-model/pull/${jobId}`);
      const job = data?.job || {};
      statusEl.textContent = job.message || "Downloading…";
      if (!res.ok || job.status === "error") {
        statusEl.textContent = job.error || data?.error || "Download failed";
        return false;
      }
      if (job.status === "done") {
        statusEl.textContent = job.message || "Ready.";
        return true;
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
    statusEl.textContent = "Still downloading — search again when it finishes.";
    return false;
  }

  $("#settings-refresh-btn")?.addEventListener("click", () => loadSettings());
  $("#frontier-add-btn")?.addEventListener("click", () => {
    applyFrontierPreset();
    show($("#frontier-form"));
  });
  $("#frontier-cancel-btn")?.addEventListener("click", () => hide($("#frontier-form")));
  $("#frontier-provider")?.addEventListener("change", () => applyFrontierPreset());
  $("#frontier-save-btn")?.addEventListener("click", async () => {
    setError("#frontier-error", "");
    const provider = $("#frontier-provider").value;
    const api_key = $("#frontier-api-key").value.trim();
    const model = $("#frontier-model").value.trim();
    const { res, data } = await api("/api/settings/models", {
      method: "POST",
      body: JSON.stringify({ provider, api_key, model, activate: true }),
    });
    if (!res.ok) {
      setError("#frontier-error", data?.error || "Save failed");
      return;
    }
    $("#frontier-api-key").value = "";
    hide($("#frontier-form"));
    await loadSettings();
  });

  $("#os-add-btn")?.addEventListener("click", async () => {
    await loadSettings();
    openOsWizard("search");
    if ((settingsState.companion || {}).reachable) {
      showWizardStep("search");
    }
  });
  $("#durable-tunnel-save-btn")?.addEventListener("click", () => saveDurableTunnelFromBrowser());
  $("#durable-tunnel-clear-btn")?.addEventListener("click", () => clearDurableTunnelFromBrowser());
  $("#os-wizard-close")?.addEventListener("click", () => closeOsWizard());
  $("#os-done-btn")?.addEventListener("click", () => {
    closeOsWizard();
    loadSettings();
  });
  $("#os-search-btn")?.addEventListener("click", () => runDiscover());
  $("#os-search-retry")?.addEventListener("click", () => runDiscover());
  $("#os-back-btn")?.addEventListener("click", () => showWizardStep("search"));

  function isFlyleafHost() {
    const h = (location.hostname || "").toLowerCase();
    return h.endsWith(".fly.dev") || h === "levin.fly.dev";
  }

  function isLoopbackCompanionUrl(url) {
    try {
      const u = new URL(url);
      return ["127.0.0.1", "localhost", "::1"].includes(u.hostname);
    } catch {
      return false;
    }
  }

  async function prepareCompanionForCloud(localUrl, token) {
    const base = String(localUrl || "").replace(/\/$/, "");
    const res = await fetch(`${base}/prepare-cloud-link`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: "{}",
    });
    let data = {};
    try {
      data = await res.json();
    } catch {
      data = {};
    }
    return { res, data };
  }

  async function startLocalModelFromBrowser() {
    const btn = $("#start-local-model-btn");
    const LOCAL = "http://127.0.0.1:8791";
    setError("#chat-error", "");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Connecting…";
    }
    try {
      let healthRes;
      try {
        healthRes = await fetch(`${LOCAL}/healthz`);
      } catch {
        setError(
          "#chat-error",
          "Local Companion isn’t running on this computer. In a terminal: python -m messenger.companion_app — then click Start local model again."
        );
        return;
      }
      if (!healthRes.ok) {
        setError("#chat-error", "Local Companion isn’t healthy. Restart it, then try again.");
        return;
      }
      const infoRes = await fetch(`${LOCAL}/browser-link-info`);
      let info = {};
      try {
        info = await infoRes.json();
      } catch {
        info = {};
      }
      if (!infoRes.ok || !info.token) {
        const code = info?.error || `HTTP ${infoRes.status}`;
        if (infoRes.status === 401 || code === "unauthorized") {
          setError(
            "#chat-error",
            "Local Companion is running an old build. Stop it and restart: python -m messenger.companion_app"
          );
        } else {
          setError(
            "#chat-error",
            info?.error || "Could not read companion link info from this browser."
          );
        }
        return;
      }
      let base_url = String(info.base_url || LOCAL).replace(/\/$/, "");
      const token = String(info.token);
      if (isFlyleafHost()) {
        if (btn) btn.textContent = "Opening tunnel…";
        let prep;
        try {
          prep = await prepareCompanionForCloud(base_url, token);
        } catch {
          setError("#chat-error", "Could not reach Local Companion to open a tunnel.");
          return;
        }
        if (!prep.res.ok || !prep.data?.public_base_url) {
          setError(
            "#chat-error",
            prep.data?.message ||
              prep.data?.error ||
              "Could not open a tunnel. Install cloudflared (`brew install cloudflared`) and retry."
          );
          return;
        }
        base_url = String(prep.data.public_base_url).replace(/\/$/, "");
        if (prep.data.tunnel_mode === "quick" || prep.data.stable === false) {
          // Soft nudge — still proceed.
          console.info(
            "Flyleaf: temporary tunnel in use. Configure Settings → Open source → Durable tunnel for a stable hostname."
          );
        }
      }
      if (btn) btn.textContent = "Linking…";
      const linked = await api("/api/companion/link", {
        method: "POST",
        body: JSON.stringify({ base_url, token }),
      });
      if (!linked.res.ok) {
        setError(
          "#chat-error",
          linked.data?.message || linked.data?.error || "Could not link companion"
        );
        return;
      }
      if (btn) btn.textContent = "Finding models…";
      const disc = await api("/api/settings/local-model/discover", { method: "POST" });
      if (!disc.res.ok) {
        setError(
          "#chat-error",
          disc.data?.message || disc.data?.error || "Linked, but model search failed. Open Settings → Open source."
        );
        await refreshChatRails();
        return;
      }
      let candidates = disc.data?.candidates || [];
      const recommended = disc.data?.recommended_model || "qwen3:8b";
      if (!candidates.length) {
        if (disc.data?.ollama?.reachable) {
          if (btn) btn.textContent = "Downloading model…";
          const pull = await api("/api/settings/local-model/pull", {
            method: "POST",
            body: JSON.stringify({ model: recommended }),
          });
          if (pull.res.ok && pull.data?.job?.id) {
            await pollPull(pull.data.job.id);
            const again = await api("/api/settings/local-model/discover", { method: "POST" });
            candidates = again.data?.candidates || [];
          }
        }
      }
      if (!candidates.length) {
        setError(
          "#chat-error",
          "Companion linked, but no local models found. Open Ollama (or install from ollama.com), then click Start local model again."
        );
        await refreshChatRails();
        return;
      }
      const prefer =
        candidates.find((c) => String(c.label || "").includes(recommended.split(":")[0])) ||
        candidates[0];
      if (btn) btn.textContent = "Connecting model…";
      const draft = await api("/api/settings/models/open-source/draft", {
        method: "POST",
        body: JSON.stringify({
          candidate_id: prefer.id,
          runtime: prefer.runtime || "ollama",
          model: prefer.label || prefer.model || recommended,
          label: prefer.label || prefer.model || recommended,
        }),
      });
      if (!draft.res.ok) {
        setError("#chat-error", draft.data?.error || "Could not create local model profile");
        return;
      }
      const profileId = draft.data.profile.id;
      const established = await api(`/api/settings/models/${profileId}/establish`, {
        method: "POST",
      });
      if (!established.res.ok) {
        setError(
          "#chat-error",
          established.data?.message || established.data?.error || "Could not establish local model route"
        );
        return;
      }
      await api(`/api/settings/models/${profileId}/enable`, { method: "POST" });
      if (state.kind === "people" && state.roomId) {
        const room = currentRoom();
        if (room?.owner_user_id === state.me?.user_id) {
          await api(`/api/rooms/${encodeURIComponent(state.roomId)}/model`, {
            method: "POST",
            body: JSON.stringify({ profile_id: profileId }),
          });
        }
      }
      await refreshChatRails();
      if (state.kind === "people") updateRoomContext(currentRoom());
      setError("#chat-error", "");
      if (btn) btn.textContent = "Local model on";
      setTimeout(() => {
        if (btn) btn.textContent = "Start local model";
      }, 2500);
    } finally {
      if (btn) {
        const busy = [
          "Connecting…",
          "Opening tunnel…",
          "Linking…",
          "Finding models…",
          "Downloading model…",
          "Connecting model…",
        ].includes(btn.textContent);
        btn.disabled = false;
        if (busy) btn.textContent = "Start local model";
      }
    }
  }

  async function linkCompanion() {
    setError("#companion-error", "");
    let base_url = $("#companion-url")?.value.trim() || "";
    const token = $("#companion-token")?.value.trim() || "";
    if (!base_url) {
      setError("#companion-error", "Enter the companion URL (http://127.0.0.1:8791).");
      return;
    }
    if (!token) {
      setError("#companion-error", "Paste the companion token from the terminal (or companion_token file).");
      return;
    }
    const btn = $("#companion-link-btn");
    if (btn) btn.disabled = true;
    try {
      // On Flyleaf, the server cannot reach your laptop's localhost. The browser can —
      // ask Companion to open a public tunnel, then register that URL with the site.
      if (isFlyleafHost() && isLoopbackCompanionUrl(base_url)) {
        setError("#companion-error", "");
        if (btn) btn.textContent = "Opening secure tunnel…";
        let prep;
        try {
          prep = await prepareCompanionForCloud(base_url, token);
        } catch (err) {
          setError(
            "#companion-error",
            "Could not reach Local Companion at that URL. Is `python -m messenger.companion_app` running?"
          );
          return;
        }
        if (!prep.res.ok || !prep.data?.public_base_url) {
          setError(
            "#companion-error",
            prep.data?.message ||
              prep.data?.error ||
              "Could not open a tunnel for the website. Install cloudflared (`brew install cloudflared`) and retry."
          );
          return;
        }
        base_url = String(prep.data.public_base_url).replace(/\/$/, "");
        const urlInput = $("#companion-url");
        if (urlInput) urlInput.value = base_url;
        if (btn) btn.textContent = "Link companion";
      }

      const { res, data } = await api("/api/companion/link", {
        method: "POST",
        body: JSON.stringify({ base_url, token }),
      });
      if (!res.ok) {
        setError(
          "#companion-error",
          data?.message || data?.error || data?.detail || "Link failed"
        );
        return;
      }
      await loadSettings();
      if ((settingsState.companion || {}).reachable || data?.reachable) {
        showWizardStep("search");
        return;
      }
      setError(
        "#companion-error",
        data?.message ||
          "Linked, but companion is not reachable yet. Keep Companion running and try again."
      );
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Link companion";
      }
    }
  }
  $("#companion-link-btn")?.addEventListener("click", () => { linkCompanion(); });
  $("#companion-link-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    linkCompanion();
  });
  $("#start-local-model-btn")?.addEventListener("click", () => {
    startLocalModelFromBrowser();
  });
  $("#room-model-select")?.addEventListener("change", async () => {
    if (!state.roomId || state.kind !== "people") return;
    const profileId = $("#room-model-select").value || null;
    setError("#chat-error", "");
    syncComputeBadgeFromSelect(currentRoom());
    const { res, data } = await api(
      `/api/rooms/${encodeURIComponent(state.roomId)}/model`,
      {
        method: "POST",
        body: JSON.stringify({ profile_id: profileId }),
      }
    );
    if (!res.ok) {
      setError("#chat-error", data?.error || "Could not change room model");
      return;
    }
    await refreshChatRails();
    updateRoomContext(currentRoom());
  });

  $("#os-pull-btn")?.addEventListener("click", async () => {
    hide($("#os-pull-status"));
    const { res, data } = await api("/api/settings/local-model/pull", {
      method: "POST",
      body: JSON.stringify({ model: settingsState.recommendedModel }),
    });
    if (!res.ok) {
      show($("#os-pull-status"));
      $("#os-pull-status").textContent = data?.message || data?.error || "Pull failed";
      return;
    }
    const jobId = data?.job?.id;
    if (jobId) {
      const ok = await pollPull(jobId);
      if (ok) await runDiscover();
    }
  });

  $("#os-establish-btn")?.addEventListener("click", async () => {
    setError("#os-establish-error", "");
    const cand = settingsState.selectedCandidate;
    if (!cand) {
      setError("#os-establish-error", "Select a model first");
      return;
    }
    $$("#os-connect-checklist li").forEach((li) => li.classList.remove("done", "active"));
    const mark = (step, cls) => {
      const li = $(`#os-connect-checklist li[data-step="${step}"]`);
      if (li) li.classList.add(cls);
    };
    mark("runtime", "active");
    let profileId = settingsState.draftProfileId;
    if (!profileId) {
      const draft = await api("/api/settings/models/open-source/draft", {
        method: "POST",
        body: JSON.stringify({
          candidate_id: cand.id,
          runtime: cand.runtime || "ollama",
          model: cand.model || cand.label,
          label: cand.label || cand.model,
        }),
      });
      if (!draft.res.ok) {
        setError("#os-establish-error", draft.data?.error || "Could not create draft");
        return;
      }
      profileId = draft.data.profile.id;
      settingsState.draftProfileId = profileId;
    }
    mark("runtime", "done");
    mark("gateway", "active");
    mark("route", "active");
    mark("probe", "active");
    mark("save", "active");
    const { res, data } = await api(`/api/settings/models/${profileId}/establish`, {
      method: "POST",
    });
    if (!res.ok) {
      setError("#os-establish-error", data?.message || data?.error || "Connect failed");
      return;
    }
    ["runtime", "gateway", "route", "probe", "save"].forEach((s) => mark(s, "done"));
    const route = data?.profile?.pipeline_route;
    $("#os-tech-details").textContent = route
      ? `${route.gateway_mode || ""} · ${route.base_url || ""}`
      : "Saved.";
    settingsState.draftProfileId = profileId;
    showWizardStep("done");
    await loadSettings();
  });

  $("#os-done-enable-btn")?.addEventListener("click", async () => {
    const id = settingsState.draftProfileId;
    if (!id) {
      closeOsWizard();
      return;
    }
    const { res, data } = await api(`/api/settings/models/${id}/enable`, { method: "POST" });
    if (!res.ok) {
      setError("#os-establish-error", data?.message || data?.error || "Could not turn on");
      showWizardStep("connect");
      return;
    }
    closeOsWizard();
    await loadSettings();
  });

  applyFrontierPreset();

  async function tryDevLogin() {
    const { res, data } = await api("/api/auth/dev-login", {
      method: "POST",
      body: "{}",
    });
    return !!(res.ok && data?.ok);
  }

  $("#dev-login-btn")?.addEventListener("click", async () => {
    setError("#login-error", "");
    const ok = await tryDevLogin();
    if (!ok) {
      setError("#login-error", "Local auto-login failed. Is MESSENGER_DEV_AUTO_LOGIN=1?");
      return;
    }
    await bootstrap();
  });

  // --- Bootstrap -------------------------------------------------------------

  async function bootstrap() {
    let { res, data } = await api("/api/me");
    // App shell requires a real account — invite-only / guest sessions stay on login.
    if ((!res.ok || !data?.authenticated) && data?.dev_auto_login) {
      if (await tryDevLogin()) {
        ({ res, data } = await api("/api/me"));
      }
    }
    if (!res.ok || !data?.authenticated) {
      showAuth({
        dev_auto_login: !!data?.dev_auto_login,
        dev_user: data?.dev_user || null,
      });
      return;
    }
    const joined = await consumePendingInvite();
    const me = joined
      ? {
          ...data,
          authenticated: true,
          room_id: joined.room_id || data.room_id,
          room_title: joined.room_title || data.room_title,
          name: joined.name || data.name,
        }
      : data;
    state.me = me;
    showShell();
    switchTab("chats");
    await refreshChatRails();
    // Auto-open current room if present
    if (me.room_id) {
      await selectPeople(me.room_id, me.room_title || "Room");
    } else if (state.threads[0]) {
      await selectAgent(state.threads[0].session_id, state.threads[0].title, !!state.threads[0].master);
    } else {
      await refreshModelStatus();
    }
  }

  bootstrap();
})();
