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

  function showAuth() {
    show($("#auth-screen"));
    hide($("#shell"));
    renderModelStatus(null);
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
    showAuth();
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
    state.tab = tab;
    $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    $$(".tab-panel").forEach((p) => hide(p));
    show($(`#tab-${tab}`));
    if (tab === "capabilities") loadCapabilities();
    if (tab === "agents") loadAgentsCatalog();
    if (tab === "automations") loadAutomations();
    if (tab === "review") loadReview();
    if (tab === "tracking") loadTracking();
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
      li.innerHTML = '<button type="button" class="muted" disabled>No rooms yet</button>';
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
        <span class="room-name"><span class="room-icon">#</span>${escapeHtml(entry.title || entry.id)}</span>
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
      li.title = `${agent.name} — click to add to the open room, or drag onto a room`;
      li.innerHTML = `<strong>${escapeHtml(agent.name)}</strong><span>${escapeHtml(agent.mention || agent.role)}</span>`;
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
          ? "Message the room… /automate · agents use capabilities"
          : "Message… @Analyst or /automate"
      );
    }
  }

  function currentRoom() {
    return state.rooms.find((r) => r.room_id === state.roomId) || null;
  }

  function updateSpecialistActions(room) {
    const agentCount = roomAgents(room).length;
    const hasAgents = agentCount > 0;
    ["#specialist-present-btn", "#specialist-idea-btn"].forEach((sel) => {
      const el = $(sel);
      if (!el) return;
      if (hasAgents) show(el);
      else hide(el);
    });
    if (agentCount > 1) show($("#specialist-debate-btn"));
    else hide($("#specialist-debate-btn"));
    if (!hasAgents) {
      setSpecialistRunUi(null);
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
      hide(stopBtn);
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
    text.textContent = job.continuous
      ? `Looping${topicBit || " debate"} (${loopBit}) — safe to leave; turns keep posting.`
      : `Running ${job.action || "specialists"}${topicBit} (${loopBit}) — safe to leave this room.`;
    show(banner);
    show(stopBtn);
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
    if (room && roomAgents(room).length === 0) {
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
    $("#stage-kind").textContent = "Room";
    $("#stage-title").textContent = title || room.title || "Room";
    show($("#clear-chat"));
    if (room?.owner_user_id && room.owner_user_id === state.me?.user_id) {
      show($("#delete-room"));
    } else {
      hide($("#delete-room"));
    }
    if (state.me?.authenticated) {
      show($("#invite-friend-btn"));
      show($("#automate-room-btn"));
    } else {
      hide($("#invite-friend-btn"));
      hide($("#automate-room-btn"));
    }
    if (state.shareRoomId !== roomId) closeShareDialog();
    updateRoomContext(room);
    updateSpecialistActions(room);
    enableComposer(
      true,
      hasAgents
        ? "Message the room… /automate · specialists use the model above"
        : "Message… Automate in the header, or @Bullish for a lens"
    );
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
    hide($("#automate-room-btn"));
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
    enableComposer(true, "Message this room… /automate to loop capabilities");
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
  }

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
      openAutomateEditor();
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

  function openAutomateEditor() {
    setError("#automate-error", "");
    if (state.kind !== "people" || !state.roomId) {
      showFlowToast("Open a people room first, then click Automate.");
      switchTab("chats");
      return;
    }
    const lines = [];
    $$("#messages .msg").forEach((el) => {
      const author = el.querySelector(".author")?.textContent?.trim() || "";
      const text = el.querySelector(".body")?.textContent?.trim() || "";
      if (text) lines.push(author ? `${author}: ${text}` : text);
    });
    $("#automate-transcript").value =
      lines.slice(-40).join("\n") || "(empty room — pick capabilities on the right)";
    const base = ($("#stage-title")?.textContent || "room_loop")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_|_$/g, "")
      .slice(0, 40) || "room_loop";
    $("#automate-name").value = base;
    const list = $("#automate-cap-list");
    if (list && !list.dataset.ready) {
      // filled async below
    }
    fillAutomateCapPicker().then(() => {
      const dlg = $("#automate-dialog");
      if (dlg?.showModal) dlg.showModal();
    });
  }

  async function fillAutomateCapPicker() {
    const list = $("#automate-cap-list");
    if (!list) return;
    list.innerHTML = "";
    const { data } = await api("/api/capabilities");
    const caps = (data?.capabilities || []).filter(
      (c) =>
        c.kind === "builtin" &&
        (c.schedulable || c.id === "web_research" || c.id === "find_files")
    );
    const defaults = new Set(["web_research", "sec_filings_check", "note_digest"]);
    caps.forEach((c) => {
      const id = `cap-pick-${c.id}`;
      const label = document.createElement("label");
      label.className = "cap-pick-row";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "cap-step";
      input.value = c.id;
      input.id = id;
      input.checked = defaults.has(c.id);
      const text = document.createElement("span");
      text.innerHTML = `<strong>${escapeHtml(c.name || c.id)}</strong>
        <span class="muted tiny-hint">${escapeHtml(c.summary || "")}</span>`;
      label.appendChild(input);
      label.appendChild(text);
      list.appendChild(label);
    });
    list.dataset.ready = "1";
  }

  $("#automate-cancel")?.addEventListener("click", () => {
    $("#automate-dialog")?.close();
  });

  $("#automate-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("#automate-error", "");
    const name = $("#automate-name").value.trim();
    const steps = $$('#automate-cap-list input[name="cap-step"]:checked').map(
      (el) => el.value
    );
    if (!steps.length) {
      setError("#automate-error", "Pick at least one capability");
      return;
    }
    const preset = $("#automate-schedule-preset")?.value || "";
    let schedule = null;
    if (preset === "custom") schedule = $("#automate-schedule").value.trim() || null;
    else if (preset) schedule = preset;
    const { res, data } = await api("/api/automations/from-chat", {
      method: "POST",
      body: JSON.stringify({
        name,
        steps,
        schedule,
        room_id: state.roomId || null,
        transcript: $("#automate-transcript").value,
      }),
    });
    if (!res.ok) {
      setError("#automate-error", data?.error || "Could not save draft");
      return;
    }
    $("#automate-dialog")?.close();
    state.highlightCapability = data.ritual_id;
    showFlowToast(`Draft “${data.ritual_id}” saved — approve it to start the loop.`, {
      tab: "capabilities",
    });
    loadCapabilities();
  });

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
    hide($("#automate-room-btn"));
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
    $("#new-room-dialog").showModal();
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
    openShareDialog(data.share_url, data.room_id);
    await refreshChatRails();
    await selectPeople(data.room_id, data.room_title, {
      room_id: data.room_id,
      title: data.room_title,
      kind: data.kind || "people",
      config: data.config || {},
      owner_user_id: data.owner_user_id || state.me?.user_id,
    });
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
        data?.message || data?.error || "Specialist run failed"
      );
      if (data?.job) setSpecialistRunUi(data.job);
      return;
    }
    if (data?.job) setSpecialistRunUi(data.job);
  }

  $("#specialist-present-btn").addEventListener("click", () => {
    runSpecialistAction("present", "");
  });
  $("#specialist-debate-btn").addEventListener("click", () => {
    state.debateAction = "debate";
    $("#debate-dialog-title").textContent = "Specialist debate";
    $("#debate-topic").value = "";
    $("#debate-continuous").checked = false;
    show($("#debate-rounds-wrap"));
    show($("#debate-loop-wrap"));
    show($("#debate-rounds-hint"));
    hide($("#debate-loop-hint"));
    $("#debate-rounds").disabled = false;
    setError("#debate-error", "");
    $("#debate-dialog").showModal();
  });
  $("#specialist-idea-btn").addEventListener("click", () => {
    state.debateAction = "idea";
    $("#debate-dialog-title").textContent = "Idea pass";
    $("#debate-topic").value = "";
    $("#debate-continuous").checked = false;
    hide($("#debate-rounds-wrap"));
    hide($("#debate-loop-wrap"));
    hide($("#debate-rounds-hint"));
    hide($("#debate-loop-hint"));
    setError("#debate-error", "");
    $("#debate-dialog").showModal();
  });
  $("#debate-continuous").addEventListener("change", () => {
    const on = $("#debate-continuous").checked;
    $("#debate-rounds").disabled = on;
    if (on) {
      hide($("#debate-rounds-hint"));
      show($("#debate-loop-hint"));
    } else {
      show($("#debate-rounds-hint"));
      hide($("#debate-loop-hint"));
    }
  });
  $("#debate-cancel").addEventListener("click", () => {
    $("#debate-dialog").close();
  });
  $("#debate-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const topic = $("#debate-topic").value.trim();
    if (!topic) {
      setError("#debate-error", "Topic required");
      return;
    }
    const action = state.debateAction || "debate";
    const continuous =
      action === "debate" && $("#debate-continuous").checked;
    const rounds = action === "debate" ? ($("#debate-rounds").value || "2") : "1";
    $("#debate-dialog").close();
    await runSpecialistAction(action, topic, rounds, continuous);
  });
  $("#specialist-stop-btn").addEventListener("click", () => stopSpecialistRun());
  $("#specialist-stop-banner-btn").addEventListener("click", () => stopSpecialistRun());

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

  // --- Capabilities / Agents / Automations (three layers) --------------------

  function showFlowToast(message, { tab } = {}) {
    const el = $("#flow-toast");
    if (!el) return;
    el.textContent = message;
    show(el);
    clearTimeout(showFlowToast._t);
    showFlowToast._t = setTimeout(() => hide(el), 5200);
    if (tab) switchTab(tab);
  }

  function friendlySchedule(cron) {
    const map = {
      "0 7 * * 1-5": "Weekday mornings (7:00)",
      "0 12 * * 1-5": "Weekday noon",
      "0 18 * * 1-5": "Weekday evenings (18:00)",
    };
    if (!cron) return "On demand";
    return map[cron] || cron;
  }

  async function approveCapability(ritualId) {
    const { res, data } = await api("/api/capabilities/approve", {
      method: "POST",
      body: JSON.stringify({ ritual_id: ritualId }),
    });
    if (!res.ok) {
      setError("#caps-error", data?.error || "Approve failed");
      return false;
    }
    showFlowToast(`“${ritualId}” approved — it’s now a running automation.`, {
      tab: "automations",
    });
    loadAutomations();
    return true;
  }

  async function loadCapabilities() {
    setError("#caps-error", "");
    const { res, data } = await api("/api/capabilities");
    const draftsBody = $("#caps-drafts-table tbody");
    const readyBody = $("#caps-ready-table tbody");
    if (draftsBody) draftsBody.innerHTML = "";
    if (readyBody) readyBody.innerHTML = "";
    const rows = data?.capabilities || [];
    if (!res.ok) {
      setError("#caps-error", data?.error || "Failed to load");
      return;
    }
    const MICRO = new Set([
      "fetch_quote",
      "fetch_calendar",
      "fetch_headlines",
      "public_web_search",
      "classify_message",
    ]);
    const drafts = rows.filter((c) => c.kind === "user" && !c.approved);
    const readyPrimary = rows.filter(
      (c) =>
        (c.kind === "builtin" || c.approved) &&
        !(c.kind === "builtin" && MICRO.has(c.id))
    );
    const readyMicro = rows.filter(
      (c) => c.kind === "builtin" && MICRO.has(c.id)
    );
    $("#caps-drafts-empty")?.classList.toggle("hidden", drafts.length > 0);
    $("#caps-empty")?.classList.add("hidden");

    const callout = $("#caps-next");
    if (callout) {
      if (drafts.length) {
        callout.innerHTML = `<strong>Next:</strong> Approve a draft below to turn it into an Automation loop.`;
        show(callout);
      } else {
        callout.innerHTML = `<strong>Tip:</strong> In a room, click <em>Automate</em> to draft a loop from the conversation.`;
        show(callout);
      }
    }

    drafts.forEach((c) => {
      const tr = document.createElement("tr");
      if (state.highlightCapability === c.ritual_id) tr.classList.add("row-highlight");
      const source =
        c.proposed_by === "room_automate"
          ? "from chat"
          : c.proposed_by === "chat_mining"
            ? "from chat gaps"
            : c.proposed_by === "claude_review"
              ? "from review"
              : "from your work";
      const steps = (c.steps || []).map((s) => escapeHtml(String(s))).join(" → ")
        || escapeHtml(c.runner || "—");
      tr.innerHTML = `
        <td>
          <strong>${escapeHtml(c.name || c.id || "")}</strong>
          <div class="muted tiny-hint">${escapeHtml(c.summary || "")}</div>
        </td>
        <td><span class="badge draft">${escapeHtml(source)}</span></td>
        <td class="muted tiny-hint">${steps}</td>
        <td></td>`;
      const td = tr.querySelector("td:last-child");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tiny";
      btn.textContent = "Approve & enable";
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        const ok = await approveCapability(c.ritual_id);
        if (!ok) btn.disabled = false;
        else loadCapabilities();
      });
      td.appendChild(btn);
      draftsBody?.appendChild(tr);
    });

    readyPrimary.forEach((c) => {
      const tr = document.createElement("tr");
      const type = c.kind === "builtin" ? "built-in" : "yours";
      tr.innerHTML = `
        <td>
          <strong>${escapeHtml(c.name || c.id || "")}</strong>
          <div class="muted tiny-hint">${escapeHtml(c.summary || "")}</div>
        </td>
        <td><span class="badge approved">${escapeHtml(type)}</span></td>
        <td class="muted tiny-hint">${escapeHtml(c.invoke || "")}</td>
        <td></td>`;
      const td = tr.querySelector("td:last-child");
      if (c.kind === "user" && c.ritual_id) {
        const run = document.createElement("button");
        run.type = "button";
        run.className = "tiny";
        run.textContent = "Run";
        run.addEventListener("click", async () => {
          await api("/api/capabilities/run", {
            method: "POST",
            body: JSON.stringify({ ritual_id: c.ritual_id, stub: true }),
          });
          showFlowToast(`Ran “${c.ritual_id}” (stub).`);
          loadCapabilities();
        });
        td.appendChild(run);
      }
      readyBody?.appendChild(tr);
    });
    if (readyMicro.length && readyBody) {
      const tr = document.createElement("tr");
      tr.className = "micro-caps-row";
      tr.innerHTML = `<td colspan="4" class="muted tiny-hint">Also available inside workflows: ${readyMicro
        .map((c) => escapeHtml(c.name || c.id))
        .join(" · ")}</td>`;
      readyBody.appendChild(tr);
    }
    state.highlightCapability = null;
  }

  function renderAgentCard(a) {
    const card = document.createElement("article");
    card.className = "agent-dir-card" + (a.kind === "lens" ? " lens" : " operator");
    const caps = (a.capability_details || [])
      .map((c) => escapeHtml(c.name || c.id))
      .join(", ");
    card.innerHTML = `
      <header>
        <strong>${escapeHtml(a.name || a.id || "")}</strong>
        <span class="badge ${a.kind === "lens" ? "draft" : "approved"}">${escapeHtml(a.kind || "")}</span>
      </header>
      <p class="muted tiny-hint">${escapeHtml(a.mention || "")}</p>
      <p>${escapeHtml(a.summary || "")}</p>
      <p class="muted tiny-hint"><strong>Uses:</strong> ${caps || (a.kind === "lens" ? "none — prompt only" : "—")}</p>
      <p class="muted tiny-hint">${escapeHtml(a.how || "")}</p>`;
    return card;
  }

  async function loadAgentsCatalog() {
    setError("#agents-tab-error", "");
    const ops = $("#agents-operators");
    const lenses = $("#agents-lenses");
    if (ops) ops.innerHTML = "";
    if (lenses) lenses.innerHTML = "";
    const { res, data } = await api("/api/agents");
    const rows = data?.agents || [];
    $("#agents-tab-empty")?.classList.toggle("hidden", rows.length > 0);
    if (!res.ok) {
      setError("#agents-tab-error", data?.error || "Failed to load");
      return;
    }
    rows.forEach((a) => {
      const card = renderAgentCard(a);
      if (a.kind === "lens") lenses?.appendChild(card);
      else ops?.appendChild(card);
    });
  }

  async function loadAutomations() {
    setError("#autos-error", "");
    const { res, data } = await api("/api/automations/loops");
    const tbody = $("#autos-table tbody");
    if (tbody) tbody.innerHTML = "";
    const rows = data?.automations || [];
    const empty = $("#autos-empty");
    if (empty) empty.classList.toggle("hidden", rows.length > 0);
    if (tbody) tbody.closest(".table-wrap")?.classList.toggle("hidden", rows.length === 0);
    if (!res.ok) {
      setError("#autos-error", data?.error || "Failed to load");
      return;
    }
    rows.forEach((a) => {
      const tr = document.createElement("tr");
      const caps = (a.capabilities || []).map((c) => escapeHtml(String(c))).join(" → ")
        || escapeHtml(a.runner || "—");
      const last = a.last_run?.ts ? fmtTime(a.last_run.ts) : "Never";
      tr.innerHTML = `
        <td>
          <strong>${escapeHtml(a.name || a.id || "")}</strong>
          <div class="muted tiny-hint">from ${escapeHtml((a.source || "capability").replace(/_/g, " "))}</div>
        </td>
        <td class="muted tiny-hint">${caps}</td>
        <td>${escapeHtml(friendlySchedule(a.schedule))}</td>
        <td class="muted tiny-hint">${escapeHtml(last)}</td>
        <td></td>`;
      const td = tr.querySelector("td:last-child");
      if (a.ritual_id) {
        const run = document.createElement("button");
        run.type = "button";
        run.className = "tiny";
        run.textContent = "Run now";
        run.addEventListener("click", async () => {
          await api("/api/automations/run", {
            method: "POST",
            body: JSON.stringify({ ritual_id: a.ritual_id, stub: true }),
          });
          showFlowToast(`Ran “${a.ritual_id}”.`);
          loadAutomations();
        });
        td.appendChild(run);
      }
      tbody?.appendChild(tr);
    });
  }

  $("#mine-btn")?.addEventListener("click", async () => {
    await api("/api/capabilities/mine", { method: "POST", body: "{}" });
    showFlowToast("Looked for repeated workflows in your tracking data.");
    loadCapabilities();
  });
  $("#refresh-caps-btn")?.addEventListener("click", loadCapabilities);
  $("#refresh-agents-tab-btn")?.addEventListener("click", loadAgentsCatalog);
  $("#refresh-autos-btn")?.addEventListener("click", loadAutomations);
  $("#goto-automate-btn")?.addEventListener("click", () => switchTab("chats"));
  $("#autos-empty-rooms-btn")?.addEventListener("click", () => switchTab("chats"));
  $("#automate-room-btn")?.addEventListener("click", () => openAutomateEditor());

  $("#automate-schedule-preset")?.addEventListener("change", () => {
    const preset = $("#automate-schedule-preset").value;
    const wrap = $("#automate-schedule-custom-wrap");
    if (preset === "custom") show(wrap);
    else hide(wrap);
  });

  // --- Review ----------------------------------------------------------------

  async function loadReview() {
    setError("#review-error", "");
    $("#review-status").textContent = "";
    const { res, data } = await api("/api/review");
    const tbody = $("#review-proposals-table tbody");
    tbody.innerHTML = "";
    if (!res.ok) {
      setError("#review-error", data?.error || "Failed to load review");
      return;
    }
    const rows = data?.proposals || [];
    $("#review-empty").classList.toggle("hidden", rows.length > 0);
    rows.forEach((p) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(p.ritual_id || p.name || "")}</td>
        <td><span class="badge draft">${escapeHtml(p.proposed_by || "review")}</span></td>
        <td>${escapeHtml(p.runner || "")}</td>
        <td></td>`;
      const td = tr.querySelector("td:last-child");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ghost tiny";
      btn.textContent = "Approve & enable";
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        const ok = await approveCapability(p.ritual_id);
        if (!ok) btn.disabled = false;
        loadReview();
      });
      td.appendChild(btn);
      tbody.appendChild(tr);
    });
    const memo = data?.memo_text;
    $("#review-memo").textContent = memo || "(no reviews yet)";
    $("#review-memo").classList.toggle("muted", !memo);
  }

  $("#run-review-btn").addEventListener("click", async () => {
    setError("#review-error", "");
    const days = parseInt($("#review-days").value, 10) || 14;
    $("#review-status").textContent = "Reviewing ledger…";
    $("#run-review-btn").disabled = true;
    try {
      const { res, data } = await api("/api/review/run", {
        method: "POST",
        body: JSON.stringify({ days }),
      });
      if (!res.ok) throw new Error(data?.error || "review failed");
      const n = (data?.proposals_written || []).length;
      const dest = data?.destination || "local";
      const fallback = data?.fallback_from
        ? ` (fell back from ${data.fallback_from})`
        : "";
      $("#review-status").textContent =
        `Done via ${dest}` + fallback + (n ? ` — ${n} proposal(s).` : ".");
      await loadReview();
    } catch (e) {
      const raw = String(e.message || e);
      const friendly =
        /authentication_error|API key|401/i.test(raw)
          ? "Review model unavailable (check Claude under Models). Try again after linking a key, or the server will use a local stub."
          : raw;
      setError("#review-error", friendly);
      $("#review-status").textContent = "";
    } finally {
      $("#run-review-btn").disabled = false;
    }
  });
  $("#refresh-review-btn").addEventListener("click", loadReview);

  // --- Tracking --------------------------------------------------------------

  const SCOPE_LABELS = {
    active_tab: "Active tab",
    all_tabs: "All open tabs",
    selected_tabs: "Selected tabs",
    research_sites: "Research sites",
    notes_only: "Notes only",
  };

  const CAPTURE_PAGE = "flyleaf-tracking";
  const CAPTURE_EXT = "flyleaf-capture";
  const BROWSER_SCOPES = new Set([
    "active_tab",
    "all_tabs",
    "selected_tabs",
    "research_sites",
  ]);

  let trackingVocab = { kinds: ["research", "build", "observation", "idea", "question"] };
  let captureExt = { connected: false, version: null, lastAt: 0 };

  function selectedCaptureScope() {
    const el = document.querySelector('input[name="capture-scope"]:checked');
    return (el && el.value) || "active_tab";
  }

  function postToCapture(type, payload) {
    window.postMessage(
      Object.assign({ source: CAPTURE_PAGE, type }, payload || {}),
      window.location.origin
    );
  }

  function setCaptureStatus(state, message) {
    const el = $("#capture-status");
    if (!el) return;
    el.dataset.state = state;
    el.textContent = message;
    const openBtn = $("#open-capture-btn");
    if (openBtn) openBtn.disabled = state === "missing";
    const hint = $("#capture-hint");
    if (!hint) return;
    if (state === "connected") {
      hint.textContent =
        "Capture is linked to this Flyleaf account. Start tracking to log tab visits; Select tabs opens the picker.";
    } else if (state === "missing") {
      hint.textContent =
        "Install / reload Analyst Ledger Capture, stay signed in here, then refresh. Notes and chat still track without it.";
    } else {
      hint.textContent =
        "Browser tab capture needs the Capture extension. Notes and chat still work without it.";
    }
  }

  function markCaptureConnected(version) {
    captureExt = {
      connected: true,
      version: version || captureExt.version,
      lastAt: Date.now(),
    };
    const ver = captureExt.version ? ` v${captureExt.version}` : "";
    setCaptureStatus("connected", `Capture extension connected${ver}`);
  }

  function pingCaptureExtension() {
    postToCapture("ping");
    postToCapture("sync_origin");
    window.setTimeout(() => {
      if (Date.now() - (captureExt.lastAt || 0) > 1500) {
        captureExt.connected = false;
        setCaptureStatus(
          "missing",
          "Capture extension not detected — tab visits will not be recorded"
        );
      }
    }, 900);
  }

  function openCapturePicker(opts) {
    const capture_scope = (opts && opts.capture_scope) || selectedCaptureScope();
    const session_id = (opts && opts.session_id) || null;
    setError("#track-error", "");
    postToCapture("open_picker", { capture_scope, session_id });
    const wasConnected = captureExt.connected;
    window.setTimeout(() => {
      if (!captureExt.connected && !wasConnected) {
        setError(
          "#track-error",
          "Capture extension not connected. Install Analyst Ledger Capture, reload it on chrome://extensions, then try again."
        );
      }
    }, 900);
  }

  window.addEventListener("message", (event) => {
    if (event.origin !== window.location.origin) return;
    const data = event.data;
    if (!data || data.source !== CAPTURE_EXT) return;
    if (data.type === "ready" || data.type === "pong" || data.type === "synced") {
      markCaptureConnected(data.version);
      return;
    }
    if (data.type === "opened_picker") {
      markCaptureConnected(data.version);
      if (data.ok === false) {
        setError("#track-error", "Could not open the Capture tab picker");
      }
      return;
    }
    if (data.type === "error" && data.message) {
      setError("#track-error", data.message);
    }
  });

  function labelChipsHtml(labels) {
    const list = Array.isArray(labels) ? labels : [];
    if (!list.length) return '<span class="muted">—</span>';
    return (
      '<span class="label-chips">' +
      list
        .map((lbl) => `<span class="badge kind">${escapeHtml(lbl)}</span>`)
        .join("") +
      "</span>"
    );
  }

  function eventTargetId(ev) {
    if (ev.type === "chat_message") return ev.event_id || "";
    const payload = ev.payload || {};
    return payload.target_event_id || "";
  }

  function renderEventRow(ev) {
    const li = document.createElement("li");
    li.className = "event-row";
    const payload = ev.payload || {};
    const scope =
      ev.type === "session_start" && payload.capture_scope
        ? ` · ${payload.capture_scope}`
        : "";
    const kind = ev.resolved_kind || "";
    const labels = Array.isArray(payload.labels) ? payload.labels.join(" · ") : "";
    const source = payload.source ? ` · ${payload.source}` : "";
    const mainBits = [`${fmtTime(ev.ts)} · ${ev.type} · ${ev.surface || ""}${scope}`];
    if (kind) mainBits.push(`kind:${kind}`);
    else if (labels) mainBits.push(labels);
    if (source && (ev.type === "label" || kind)) mainBits.push(source.trim());

    const main = document.createElement("div");
    main.className = "event-main";
    main.textContent = mainBits.filter(Boolean).join(" · ");
    if (ev.message_excerpt) {
      const ex = document.createElement("span");
      ex.className = "event-excerpt";
      ex.textContent = ev.message_excerpt;
      main.appendChild(ex);
    }
    li.appendChild(main);

    if (kind) {
      const chip = document.createElement("span");
      chip.className = "badge kind" + (payload.source === "human" ? " human" : "");
      chip.textContent = kind;
      li.appendChild(chip);
    }

    const targetId = eventTargetId(ev);
    const canFix =
      targetId &&
      ev.session_id &&
      (ev.type === "chat_message" || ev.type === "label") &&
      (kind || ev.type === "chat_message");
    if (canFix) {
      const fix = document.createElement("div");
      fix.className = "fix-kind";
      const sel = document.createElement("select");
      sel.setAttribute("aria-label", "Fix kind");
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "Fix kind…";
      sel.appendChild(blank);
      (trackingVocab.kinds || []).forEach((k) => {
        const opt = document.createElement("option");
        opt.value = k;
        opt.textContent = k;
        if (k === kind) opt.selected = true;
        sel.appendChild(opt);
      });
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ghost tiny";
      btn.textContent = "Save";
      btn.addEventListener("click", async () => {
        const next = sel.value;
        if (!next) return;
        setError("#track-error", "");
        const { res, data } = await api("/api/tracking/labels/correct", {
          method: "POST",
          body: JSON.stringify({
            session_id: ev.session_id,
            event_id: targetId,
            kind: next,
            auto_kind: kind || undefined,
          }),
        });
        if (!res.ok) {
          setError("#track-error", data?.error || "Failed to correct kind");
          return;
        }
        loadTracking();
      });
      fix.appendChild(sel);
      fix.appendChild(btn);
      li.appendChild(fix);
    }
    return li;
  }

  async function loadTracking() {
    setError("#track-error", "");
    pingCaptureExtension();
    const vocab = await api("/api/tracking/labels/vocab");
    if (vocab.res.ok && vocab.data?.kinds) {
      trackingVocab = {
        kinds: vocab.data.kinds,
        topics: vocab.data.topics || [],
        intents: vocab.data.intents || [],
        states: vocab.data.states || [],
      };
    }
    const summary = await api("/api/tracking/summary");
    if (!summary.res.ok) {
      setError("#track-error", summary.data?.error || "Failed to load tracking");
      return;
    }
    const active = summary.data?.active_session;
    const activeLabels = Array.isArray(active?.labels) && active.labels.length
      ? ` · ${active.labels.join(", ")}`
      : "";
    $("#active-session").textContent = active
      ? `${active.title} (${active.session_id})${activeLabels}`
      : "None";
    const scopeHint = $("#active-scope");
    if (active && active.capture_scope) {
      scopeHint.hidden = false;
      scopeHint.textContent = `Scope: ${SCOPE_LABELS[active.capture_scope] || active.capture_scope}`;
      const radio = document.querySelector(
        `input[name="capture-scope"][value="${active.capture_scope}"]`
      );
      if (radio) radio.checked = true;
    } else {
      scopeHint.hidden = true;
      scopeHint.textContent = "";
    }
    const trackingOn = !!(active && active.status === "open");
    $("#start-session-btn").disabled = trackingOn;
    $("#end-session-btn").disabled = !trackingOn;
    $("#capture-scope-fieldset").disabled = trackingOn;

    const list = $("#summary-list");
    list.innerHTML = "";
    const s = summary.data?.summary || {};
    Object.entries(s).forEach(([k, v]) => {
      const li = document.createElement("li");
      li.textContent = `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`;
      list.appendChild(li);
    });

    const sessions = await api("/api/tracking/sessions?limit=30");
    const tbody = $("#sessions-table tbody");
    tbody.innerHTML = "";
    (sessions.data?.sessions || []).forEach((row) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(row.title)}</td>
        <td>${labelChipsHtml(row.labels)}</td>
        <td>${escapeHtml(row.surface)}</td>
        <td>${escapeHtml(row.status)}</td>
        <td>${escapeHtml(fmtTime(row.started_at))}</td>`;
      tbody.appendChild(tr);
    });

    const events = await api("/api/tracking/events?limit=40");
    const el = $("#events-list");
    el.innerHTML = "";
    (events.data?.events || []).forEach((ev) => {
      el.appendChild(renderEventRow(ev));
    });
  }

  $("#start-session-btn").addEventListener("click", async () => {
    const title = ($("#session-title").value || "").trim() || "Research session";
    const capture_scope = selectedCaptureScope();
    const { res, data } = await api("/api/tracking/session/start", {
      method: "POST",
      body: JSON.stringify({ title, capture_scope }),
    });
    if (!res.ok) {
      setError("#track-error", data?.error || "Failed to start session");
      return;
    }
    const session = data?.session || {};
    const session_id = session.session_id || null;
    if (BROWSER_SCOPES.has(capture_scope)) {
      // Bridge opens the tab picker; websites cannot open the Chrome toolbar popup.
      postToCapture("session_started", { capture_scope, session_id });
      if (!captureExt.connected) {
        pingCaptureExtension();
        setError(
          "#track-error",
          "Tracking started, but Capture is not connected — tab visits will not be recorded until you install/reload the extension."
        );
      }
    }
    loadTracking();
  });
  const openCaptureBtn = $("#open-capture-btn");
  if (openCaptureBtn) {
    openCaptureBtn.addEventListener("click", () => {
      const activeText = $("#active-session")?.textContent || "";
      const match = activeText.match(/\(sess_[^)]+\)/);
      const session_id = match ? match[0].slice(1, -1) : null;
      openCapturePicker({
        capture_scope: selectedCaptureScope(),
        session_id,
      });
    });
  }
  $("#end-session-btn").addEventListener("click", async () => {
    await api("/api/tracking/session/end", {
      method: "POST",
      body: JSON.stringify({ tags: ["neutral"] }),
    });
    loadTracking();
  });
  $("#add-note-btn").addEventListener("click", async () => {
    const text = $("#session-note").value.trim();
    if (!text) return;
    await api("/api/tracking/session/note", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    $("#session-note").value = "";
    loadTracking();
  });
  const classifyBtn = $("#classify-pending-btn");
  if (classifyBtn) {
    classifyBtn.addEventListener("click", async () => {
      setError("#track-error", "");
      const { res, data } = await api("/api/tracking/classify-pending", {
        method: "POST",
        body: JSON.stringify({ limit: 20 }),
      });
      if (!res.ok) {
        setError("#track-error", data?.error || "Classify failed");
        return;
      }
      loadTracking();
    });
  }

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

  $("#privacy-tracking-btn")?.addEventListener("click", () => switchTab("tracking"));

  // --- Settings → Models -----------------------------------------------------

  const FRONTIER_PRESETS = {
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
    const id = $("#frontier-provider")?.value || "anthropic";
    const preset = FRONTIER_PRESETS[id] || FRONTIER_PRESETS.anthropic;
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

  // --- Bootstrap -------------------------------------------------------------

  async function bootstrap() {
    const { res, data } = await api("/api/me");
    // App shell requires a real account — invite-only / guest sessions stay on login.
    if (!res.ok || !data?.authenticated) {
      showAuth();
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
