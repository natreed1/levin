(() => {
  const joinPanel = document.getElementById("join-panel");
  const chatPanel = document.getElementById("chat-panel");
  const joinForm = document.getElementById("join-form");
  const createForm = document.getElementById("create-form");
  const joinError = document.getElementById("join-error");
  const createError = document.getElementById("create-error");
  const chatError = document.getElementById("chat-error");
  const messagesEl = document.getElementById("messages");
  const sendForm = document.getElementById("send-form");
  const bodyInput = document.getElementById("body");
  const whoEl = document.getElementById("who");
  const logoutBtn = document.getElementById("logout");
  const clearChatBtn = document.getElementById("clear-chat");
  const inviteInput = document.getElementById("invite");
  const nameInput = document.getElementById("name");
  const roomTitleInput = document.getElementById("room-title");
  const creatorInviteInput = document.getElementById("creator-invite");
  const creatorNameInput = document.getElementById("creator-name");
  const showJoinBtn = document.getElementById("show-join");
  const showCreateBtn = document.getElementById("show-create");
  const roomNameEl = document.getElementById("room-name");
  const shareBox = document.getElementById("share-box");
  const shareUrlInput = document.getElementById("share-url");
  const copyShareBtn = document.getElementById("copy-share");

  let me = null;
  let currentRoom = "legacy";
  let ws = null;
  let reconnectTimer = null;
  const personalityMentions = ["@Qwen", "@Qwen-Contrarian"];

  function qsInvite() {
    const params = new URLSearchParams(window.location.search);
    return (params.get("invite") || "").trim();
  }

  function qsRoom() {
    const params = new URLSearchParams(window.location.search);
    return (params.get("room") || "legacy").trim();
  }

  function showError(el, msg) {
    if (!msg) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = msg;
  }

  function fmtTime(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      return d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
    } catch {
      return iso;
    }
  }

  function renderMessage(msg) {
    const div = document.createElement("div");
    div.className = "msg" + (msg.author === me ? " mine" : "");
    div.dataset.id = String(msg.id);
    div.innerHTML =
      '<div class="meta"><span class="author"></span><span class="time"></span></div>' +
      '<div class="body"></div>';
    div.querySelector(".author").textContent = msg.author || "";
    div.querySelector(".time").textContent = fmtTime(msg.created_at);
    div.querySelector(".body").textContent = msg.body || "";
    return div;
  }

  function appendMessage(msg) {
    if (!msg || msg.id == null) return;
    if (messagesEl.querySelector(`[data-id="${msg.id}"]`)) return;
    messagesEl.appendChild(renderMessage(msg));
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function setHistory(list) {
    messagesEl.innerHTML = "";
    (list || []).forEach(appendMessage);
  }

  function showJoin() {
    joinPanel.classList.remove("hidden");
    chatPanel.classList.add("hidden");
    const invite = qsInvite();
    if (invite) inviteInput.value = invite;
  }

  function showChat(name, roomId = "legacy", roomTitle = "Private room", shareUrl = "") {
    me = name;
    currentRoom = roomId || "legacy";
    whoEl.textContent = name;
    roomNameEl.textContent = roomTitle || "Private room";
    if (shareUrl) {
      shareUrlInput.value = shareUrl;
      shareBox.classList.remove("hidden");
    } else {
      shareBox.classList.add("hidden");
    }
    joinPanel.classList.add("hidden");
    chatPanel.classList.remove("hidden");
    connectWs();
    bodyInput.focus();
  }

  function connectWs() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onmessage = (ev) => {
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (data.type === "history") {
        setHistory(data.messages || []);
      } else if (data.type === "message") {
        appendMessage(data.message);
        showError(chatError, "");
      } else if (data.type === "cleared") {
        setHistory([]);
        showError(chatError, "");
      } else if (data.type === "error") {
        const map = {
          too_long: "Message is too long.",
          rate_limited: "Slow down — too many messages.",
        };
        showError(chatError, map[data.error] || "Could not send.");
      }
    };
    ws.onclose = () => {
      ws = null;
      if (me) {
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connectWs, 1500);
      }
    };
    ws.onerror = () => {
      try {
        ws.close();
      } catch {
        /* ignore */
      }
    };
  }

  async function bootstrap() {
    const invite = qsInvite();
    if (invite) inviteInput.value = invite;

    try {
      const res = await fetch("/api/me", { credentials: "same-origin" });
      if (res.ok) {
        const data = await res.json();
        if (data.ok && data.name) {
          showChat(data.name, data.room_id, data.room_title);
          return;
        }
      }
    } catch {
      /* fall through to join */
    }
    showJoin();
  }

  function addMentionAutofill(input) {
    const menu = document.createElement("div");
    menu.className = "mention-autofill";
    menu.hidden = true;
    input.parentElement.appendChild(menu);
    let matches = [];
    let active = 0;
    let tokenStart = -1;

    function closeMenu() {
      menu.hidden = true;
      matches = [];
      tokenStart = -1;
    }

    function choose(index) {
      const mention = matches[index];
      if (!mention || tokenStart < 0) return;
      const cursor = input.selectionStart;
      input.value =
        input.value.slice(0, tokenStart) +
        mention +
        " " +
        input.value.slice(cursor);
      const next = tokenStart + mention.length + 1;
      input.setSelectionRange(next, next);
      closeMenu();
      input.focus();
    }

    function refresh() {
      const cursor = input.selectionStart;
      const before = input.value.slice(0, cursor);
      const found = before.match(/(^|\s)(@[\w-]*)$/);
      if (!found) {
        closeMenu();
        return;
      }
      const query = found[2].toLowerCase();
      tokenStart = cursor - found[2].length;
      matches = personalityMentions.filter((mention) =>
        mention.toLowerCase().startsWith(query)
      );
      if (!matches.length) {
        closeMenu();
        return;
      }
      active = Math.min(active, matches.length - 1);
      menu.innerHTML = "";
      matches.forEach((mention, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = index === active ? "active" : "";
        button.dataset.index = String(index);
        button.textContent = mention;
        menu.appendChild(button);
      });
      menu.hidden = false;
    }

    input.addEventListener("input", refresh);
    input.addEventListener("blur", () => setTimeout(closeMenu, 120));
    input.addEventListener("keydown", (event) => {
      if (menu.hidden) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        active =
          (active +
            (event.key === "ArrowDown" ? 1 : -1) +
            matches.length) %
          matches.length;
        refresh();
      } else if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        choose(active);
      } else if (event.key === "Escape") {
        event.preventDefault();
        closeMenu();
      }
    });
    menu.addEventListener("mousedown", (event) => {
      event.preventDefault();
      const button = event.target.closest("button[data-index]");
      if (button) choose(Number(button.dataset.index));
    });
  }

  function showRoomForm(kind) {
    const creating = kind === "create";
    joinForm.classList.toggle("hidden", creating);
    createForm.classList.toggle("hidden", !creating);
    showJoinBtn.classList.toggle("active", !creating);
    showCreateBtn.classList.toggle("active", creating);
    showJoinBtn.classList.toggle("ghost", creating);
    showCreateBtn.classList.toggle("ghost", !creating);
    if (creating) {
      creatorNameInput.value = creatorNameInput.value || nameInput.value;
      roomTitleInput.focus();
    } else {
      nameInput.value = nameInput.value || creatorNameInput.value;
      inviteInput.focus();
    }
  }

  showJoinBtn.addEventListener("click", () => showRoomForm("join"));
  showCreateBtn.addEventListener("click", () => showRoomForm("create"));

  createForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    showError(createError, "");
    try {
      const res = await fetch("/api/rooms", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: roomTitleInput.value.trim(),
          creator_invite: creatorInviteInput.value.trim(),
          name: creatorNameInput.value.trim(),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        const map = {
          bad_creator_invite: "Creator key is incorrect.",
          bad_name: "Pick a display name (1–40 characters).",
          bad_title: "Enter a room name.",
        };
        showError(createError, map[data.error] || "Could not create room.");
        return;
      }
      if (window.history.replaceState) {
        window.history.replaceState(
          {},
          "",
          "/?room=" + encodeURIComponent(data.room_id)
        );
      }
      showChat(data.name, data.room_id, data.room_title, data.share_url);
    } catch {
      showError(createError, "Network error — try again.");
    }
  });

  joinForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    showError(joinError, "");
    const invite = inviteInput.value.trim();
    const name = nameInput.value.trim();
    try {
      const res = await fetch("/api/join", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ invite, name, room_id: qsRoom() }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        const map = {
          bad_invite: "Invite token is incorrect.",
          bad_name: "Pick a display name (1–40 characters).",
        };
        showError(joinError, map[data.error] || "Could not join.");
        return;
      }
      // Drop invite from the URL bar after joining.
      if (window.history.replaceState) {
        const room = data.room_id || "legacy";
        window.history.replaceState(
          {},
          "",
          room === "legacy" ? "/" : "/?room=" + encodeURIComponent(room)
        );
      }
      showChat(data.name, data.room_id, data.room_title);
    } catch {
      showError(joinError, "Network error — try again.");
    }
  });

  sendForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const body = bodyInput.value.trim();
    if (!body || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "message", body }));
    bodyInput.value = "";
  });

  copyShareBtn.addEventListener("click", async () => {
    const value = shareUrlInput.value;
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      copyShareBtn.textContent = "Copied";
      setTimeout(() => {
        copyShareBtn.textContent = "Copy link";
      }, 1500);
    } catch {
      shareUrlInput.select();
    }
  });

  logoutBtn.addEventListener("click", async () => {
    me = null;
    clearTimeout(reconnectTimer);
    if (ws) {
      try {
        ws.close();
      } catch {
        /* ignore */
      }
      ws = null;
    }
    await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
    shareBox.classList.add("hidden");
    showJoin();
  });

  clearChatBtn.addEventListener("click", async () => {
    if (
      !confirm(
        "Delete the entire room chat for everyone? This cannot be undone."
      )
    ) {
      return;
    }
    showError(chatError, "");
    clearChatBtn.disabled = true;
    try {
      const res = await fetch("/api/messages", {
        method: "DELETE",
        credentials: "same-origin",
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        showError(chatError, "Could not delete chat.");
        return;
      }
      setHistory([]);
    } catch {
      showError(chatError, "Network error — try again.");
    } finally {
      clearChatBtn.disabled = false;
    }
  });

  addMentionAutofill(bodyInput);
  bootstrap();
})();
