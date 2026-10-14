/* Chatbot Studio embeddable widget.
 * Usage: <script src="https://your-studio-host/widget.js" data-bot-key="pk_..." async></script>
 * Renders inside a Shadow DOM so host-site CSS can't interfere. No dependencies. */
(function () {
  "use strict";

  var script = document.currentScript;
  if (!script) {
    var candidates = document.querySelectorAll("script[data-bot-key]");
    script = candidates[candidates.length - 1];
  }
  if (!script) return;
  var BOT_KEY = script.getAttribute("data-bot-key");
  if (!BOT_KEY) return;
  var HOST = script.getAttribute("data-host") || new URL(script.src).origin;
  if (window.__chatbotStudioLoaded === BOT_KEY) return;
  window.__chatbotStudioLoaded = BOT_KEY;

  var SESSION_STORAGE_KEY = "cbs_session_" + BOT_KEY;
  var conversationToken = "", activeSession = "", ready = false, pollTimer = null, gated = false;
  function authHeaders() {
    var headers = { "Content-Type": "application/json" };
    if (unlockToken) headers.Authorization = "Bearer " + unlockToken;
    if (conversationToken) headers["X-Conversation-Token"] = conversationToken;
    return headers;
  }
  function setStatus(text) {
    var el = root && root.querySelector(".cbs-status");
    if (el) el.textContent = text;
  }
  function conversationStatus(status) {
    setStatus(status === "human" ? "You are talking with the team" :
      status === "needs_human" ? "The team has been notified. You can leave more details." : "Ask a question or leave your details");
  }
  function checkAccess(data) {
    if (data.gate_required) { unlockToken = null; showGate(); }
    if (data.session_expired) { ready = false; setStatus("Conversation expired. Use New chat to continue."); }
  }

  function sessionId() {
    if (activeSession) return activeSession;
    try {
      var sid = localStorage.getItem(SESSION_STORAGE_KEY);
      if (!sid) {
        sid = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
          : "s" + Date.now() + Math.random().toString(36).slice(2, 10);
        localStorage.setItem(SESSION_STORAGE_KEY, sid);
      }
      return sid;
    } catch (_e) {
      if (!window.__cbsMemSession) {
        window.__cbsMemSession = "s" + Date.now() + Math.random().toString(36).slice(2, 10);
      }
      return window.__cbsMemSession;
    }
  }

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* Tiny markdown: bold, [links](url), bare URLs, "- " bullets, line breaks. */
  function renderMarkdown(text) {
    var html = esc(text);
    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]*)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    html = html.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/_\(([^)]*)\)_/g, "<em>($1)</em>");
    var lines = html.split("\n").map(function (line) {
      var m = line.match(/^\s*[-•]\s+(.*)$/);
      return m ? '<span class="cbs-li">• ' + m[1] + "</span>" : line;
    });
    return lines.join("<br>").replace(/(<\/span>)<br>/g, "$1");
  }

  function contrastText(hex) {
    var r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16),
        b = parseInt(hex.slice(5, 7), 16);
    return (0.299 * r + 0.587 * g + 0.114 * b) > 160 ? "#111827" : "#ffffff";
  }

  var config = null;
  var root, panel, messagesEl, inputEl, launcher;
  var open = false;
  var leadFormShown = false;
  var starterWrap = null;
  var unlockToken = null;

  function css(themeColor, position, textOnTheme) {
    var side = position === "left" ? "left" : "right";
    return "" +
      ":host { all: initial; }" +
      "* { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }" +
      ".cbs-launcher { position: fixed; bottom: 20px; " + side + ": 20px; z-index: 2147483000;" +
      "  display: flex; align-items: center; gap: 8px; padding: 13px 20px; border: none; border-radius: 999px;" +
      "  background: " + themeColor + "; color: " + textOnTheme + "; font-size: 15px; font-weight: 600;" +
      "  cursor: pointer; box-shadow: 0 6px 24px rgba(0,0,0,.22); transition: transform .15s; }" +
      ".cbs-launcher:hover { transform: scale(1.04); }" +
      ".cbs-panel { position: fixed; bottom: 88px; " + side + ": 20px; z-index: 2147483001;" +
      "  width: 380px; max-width: calc(100vw - 32px); height: 560px; max-height: min(75vh, 640px);" +
      "  background: #fff; border-radius: 16px; box-shadow: 0 12px 48px rgba(0,0,0,.28);" +
      "  display: flex; flex-direction: column; overflow: hidden; opacity: 0; transform: translateY(12px);" +
      "  pointer-events: none; transition: opacity .18s, transform .18s; }" +
      ".cbs-panel.open { opacity: 1; transform: translateY(0); pointer-events: auto; }" +
      ".cbs-header { background: " + themeColor + "; color: " + textOnTheme + "; padding: 14px 16px;" +
      "  display: flex; align-items: center; justify-content: space-between; }" +
      ".cbs-header h3 { margin: 0; font-size: 15px; font-weight: 700; }" +
      ".cbs-close { background: none; border: none; color: inherit; font-size: 20px; cursor: pointer; line-height: 1; opacity: .85; }" +
      ".cbs-close:hover { opacity: 1; }" +
      ".cbs-messages { flex: 1; overflow-y: auto; padding: 16px 14px; display: flex; flex-direction: column; gap: 10px; background: #f8fafc; }" +
      ".cbs-msg { max-width: 82%; padding: 10px 13px; border-radius: 14px; font-size: 14px; line-height: 1.5; word-break: break-word; }" +
      ".cbs-msg.bot { align-self: flex-start; background: #fff; color: #111827; border: 1px solid #e5e7eb; border-bottom-left-radius: 4px; }" +
      ".cbs-msg.user { align-self: flex-end; background: " + themeColor + "; color: " + textOnTheme + "; border-bottom-right-radius: 4px; }" +
      ".cbs-msg.agent { align-self: flex-start; background: #ecfdf5; color: #111827; border: 1px solid #a7f3d0; border-bottom-left-radius: 4px; }" +
      ".cbs-who { display: block; font-size: 11px; font-weight: 700; color: #047857; margin-bottom: 3px; }" +
      ".cbs-unread { position: absolute; top: -3px; right: -3px; width: 12px; height: 12px; border-radius: 50%; background: #ef4444; border: 2px solid #fff; }" +
      ".cbs-msg a { color: " + themeColor + "; }" +
      ".cbs-msg.user a { color: inherit; }" +
      ".cbs-li { display: block; padding-left: 4px; }" +
      ".cbs-typing { align-self: flex-start; background: #fff; border: 1px solid #e5e7eb; border-radius: 14px; padding: 12px 16px; display: flex; gap: 4px; }" +
      ".cbs-typing i { width: 7px; height: 7px; border-radius: 50%; background: #9ca3af; animation: cbsBlink 1.2s infinite; }" +
      ".cbs-typing i:nth-child(2) { animation-delay: .2s; } .cbs-typing i:nth-child(3) { animation-delay: .4s; }" +
      "@keyframes cbsBlink { 0%, 70%, 100% { opacity: .3; } 35% { opacity: 1; } }" +
      ".cbs-starters { display: flex; flex-wrap: wrap; gap: 6px; }" +
      ".cbs-starter { border: 1px solid " + themeColor + "; color: " + themeColor + "; background: #fff;" +
      "  border-radius: 999px; padding: 6px 12px; font-size: 13px; cursor: pointer; }" +
      ".cbs-starter:hover { background: " + themeColor + "; color: " + textOnTheme + "; }" +
      ".cbs-inputrow { display: flex; gap: 8px; padding: 12px; border-top: 1px solid #e5e7eb; background: #fff; }" +
      ".cbs-input { flex: 1; border: 1px solid #d1d5db; border-radius: 999px; padding: 10px 16px; font-size: 14px; outline: none; }" +
      ".cbs-input:focus { border-color: " + themeColor + "; }" +
      ".cbs-send { border: none; border-radius: 999px; width: 42px; height: 42px; background: " + themeColor + ";" +
      "  color: " + textOnTheme + "; font-size: 16px; cursor: pointer; flex: none; }" +
      ".cbs-send:disabled { opacity: .5; cursor: default; }" +
      ".cbs-action-btn { display: inline-block; margin-top: 8px; padding: 8px 14px; border-radius: 8px;" +
      "  background: " + themeColor + "; color: " + textOnTheme + " !important; text-decoration: none; font-size: 13px; font-weight: 600; }" +
      ".cbs-form { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }" +
      ".cbs-form input, .cbs-form textarea { border: 1px solid #d1d5db; border-radius: 8px; padding: 8px 10px; font-size: 13px; width: 100%; }" +
      ".cbs-form .cbs-err { color: #dc2626; font-size: 12px; margin: 0; }" +
      ".cbs-form button { border: none; border-radius: 8px; padding: 9px 14px; background: " + themeColor + ";" +
      "  color: " + textOnTheme + "; font-size: 13px; font-weight: 600; cursor: pointer; }" +
      ".cbs-cites { margin-top: 10px; border-top: 1px solid #e5e7eb; padding-top: 8px; }" +
      ".cbs-cites-label { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: #9ca3af; margin-bottom: 4px; }" +
      ".cbs-cite { font-size: 12px; margin: 2px 0; }" +
      ".cbs-cite summary { cursor: pointer; color: " + themeColor + "; font-weight: 600; }" +
      ".cbs-cite-snippet { color: #6b7280; margin: 4px 0 6px; font-size: 12px; line-height: 1.4; }" +
      ".cbs-gate { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }" +
      ".cbs-gate .cbs-gate-msg { color: #dc2626; font-size: 12px; }" +
      ".cbs-powered { text-align: center; font-size: 11px; color: #9ca3af; padding: 0 0 8px; background: #fff; }" +
      ".cbs-powered a { color: #9ca3af; }" +
      ".cbs-status { padding: 8px 14px; font-size: 12px; color: #475569; background: #f1f5f9; }" +
      ".cbs-new { background: transparent; color: inherit; border: 1px solid currentColor; border-radius: 5px; padding: 4px 7px; cursor: pointer; }" +
      "button:focus-visible, input:focus-visible { outline: 3px solid #0891b2; outline-offset: 2px; }" +
      ".cbs-panel:not(.open) { visibility: hidden; }" +
      "@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }" +
      "@media (max-width: 480px) { .cbs-panel { " + side + ": 8px; bottom: 84px; width: calc(100vw - 16px); height: 70vh; } }";
  }

  function addMessage(role, html) {
    var el = document.createElement("div");
    el.className = "cbs-msg " + role;
    el.innerHTML = html;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }

  function renderCitations(bubble, citations) {
    if (!citations || !citations.length || !bubble) return;
    var wrap = document.createElement("div");
    wrap.className = "cbs-cites";
    var label = document.createElement("div");
    label.className = "cbs-cites-label";
    label.textContent = "Sources";
    wrap.appendChild(label);
    citations.forEach(function (c) {
      var item = document.createElement("details");
      item.className = "cbs-cite";
      var sum = document.createElement("summary");
      sum.textContent = c.title || "Source";
      item.appendChild(sum);
      if (c.snippet) {
        var sn = document.createElement("div");
        sn.className = "cbs-cite-snippet";
        sn.textContent = c.snippet;
        item.appendChild(sn);
      }
      wrap.appendChild(item);
    });
    bubble.appendChild(wrap);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function showTyping() {
    var el = document.createElement("div");
    el.className = "cbs-typing";
    el.innerHTML = "<i></i><i></i><i></i>";
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }

  function handleAction(action) {
    if (!action) return;
    if (action.type === "handoff") {
      showLeadForm();
      return;
    }
    if (action.type === "redirect" && action.url) {
      if (action.auto) {
        addMessage("bot", "Taking you to <strong>" + esc(action.label || action.url) + "</strong>…");
        setTimeout(function () { window.location.assign(action.url); }, 1400);
      } else {
        var el = addMessage("bot", "");
        var a = document.createElement("a");
        a.className = "cbs-action-btn";
        a.href = action.url;
        a.textContent = (action.label || "Open page") + " →";
        if (/^https?:\/\//.test(action.url) &&
            new URL(action.url, window.location.href).origin !== window.location.origin) {
          a.target = "_blank";
          a.rel = "noopener";
        }
        el.appendChild(a);
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
    } else if (action.type === "lead_form") {
      showLeadForm();
    }
  }

  function showLeadForm() {
    if (leadFormShown || !config.lead_fields || !config.lead_fields.length) return;
    leadFormShown = true;
    var el = addMessage("bot", "");
    var form = document.createElement("form");
    form.className = "cbs-form";
    config.lead_fields.forEach(function (field) {
      var input = document.createElement(field.type === "textarea" ? "textarea" : "input");
      if (field.type !== "textarea") input.type = field.type || "text";
      else input.rows = 3;
      input.name = field.key;
      input.placeholder = field.label + (field.required ? " *" : "");
      form.appendChild(input);
    });
    var err = document.createElement("p");
    err.className = "cbs-err";
    var submit = document.createElement("button");
    submit.type = "submit";
    submit.textContent = "Send";
    form.appendChild(err);
    form.appendChild(submit);
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      err.textContent = "";
      var fields = {};
      var missing = null;
      config.lead_fields.forEach(function (field) {
        var value = form.elements[field.key] ? form.elements[field.key].value.trim() : "";
        if (field.required && !value && !missing) missing = field.label;
        if (value) fields[field.key] = value;
      });
      if (missing) { err.textContent = missing + " is required"; return; }
      submit.disabled = true;
      post("/api/widget/lead", {
        key: BOT_KEY, session_id: sessionId(), fields: fields, page_url: window.location.href,
      }).then(function (data) {
        if (data && data.ok) {
          el.innerHTML = renderMarkdown(data.message || "Thanks! The team will be in touch.");
        } else {
          submit.disabled = false;
          err.textContent = (data && data.error) || "Something went wrong. try again.";
          leadFormShown = true;
        }
      }).catch(function () {
        submit.disabled = false;
        err.textContent = "Network error. try again.";
      });
    });
    el.appendChild(form);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function post(path, body) {
    body.conversation_token = conversationToken;
    if (unlockToken) body.unlock_token = unlockToken;
    return fetch(HOST + path, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(body),
    }).then(function (resp) { return resp.json(); }).then(function (data) { checkAccess(data); return data; });
  }

  var sending = false;

  function finishSend() {
    sending = false;
    root.querySelector(".cbs-send").disabled = !ready;
    inputEl.focus();
  }

  function send(text) {
    text = (text || inputEl.value).trim();
    if (!text || sending || !ready) return;
    sending = true;
    inputEl.value = "";
    if (starterWrap) { starterWrap.remove(); starterWrap = null; }
    addMessage("user", esc(text));
    var typing = showTyping();
    root.querySelector(".cbs-send").disabled = true;
    var payload = {
      key: BOT_KEY, session_id: sessionId(), message: text, page_url: window.location.href,
      unlock_token: unlockToken, conversation_token: conversationToken,
      request_id: (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : Date.now() + Math.random().toString(36),
    };
    sendStreaming(payload, typing, function () { sendPlain(payload, typing); });
  }

  /* Streaming transport: reads SSE frames from a fetch body. Falls back to the
   * plain JSON endpoint if streaming isn't available or fails before output. */
  function sendStreaming(payload, typing, fallback) {
    var supported = window.fetch && window.TextDecoder && window.ReadableStream;
    if (!supported) { fallback(); return; }
    var controller = window.AbortController ? new AbortController() : null;
    var timeout = controller ? setTimeout(function () { controller.abort(); }, 90000) : null;
    fetch(HOST + "/api/widget/chat/stream", {
      signal: controller ? controller.signal : undefined,
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(payload),
    }).then(function (resp) {
      var ctype = resp.headers.get("content-type") || "";
      if (!resp.ok) {
        return resp.json().then(function (data) {
          typing.remove(); checkAccess(data);
          if (!data.gate_required) addMessage("bot", esc(data.reply || data.error || "Message could not be sent."));
          finishSend();
        });
      }
      if (!resp.body || ctype.indexOf("text/event-stream") === -1) {
        throw new Error("stream-unavailable");
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      var acc = "";
      var bubble = null;
      var finished = false;

      function handleEvent(ev) {
        if (ev.delta) {
          if (!bubble) {
            typing.remove();
            bubble = addMessage("bot", "");
          }
          acc += ev.delta;
          bubble.innerHTML = renderMarkdown(acc);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        }
        if (ev.done) {
          finished = true;
          if (!bubble) {
            typing.remove();
            bubble = addMessage("bot", "");
          }
          bubble.innerHTML = renderMarkdown(acc.trim() || "…");
          renderCitations(bubble, ev.citations);
          handleAction(ev.action);
          finishSend();
        }
      }

      function pump() {
        return reader.read().then(function (result) {
          if (result.done) {
            if (!finished) {
              if (!bubble) { typing.remove(); addMessage("bot", "Sorry. the reply was cut off. Try again?"); }
              finishSend();
            }
            return;
          }
          buffer += decoder.decode(result.value, { stream: true });
          var idx;
          while ((idx = buffer.indexOf("\n\n")) !== -1) {
            var frame = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            var lines = frame.split("\n");
            for (var i = 0; i < lines.length; i++) {
              if (lines[i].indexOf("data: ") === 0) {
                try { handleEvent(JSON.parse(lines[i].slice(6))); } catch (_e) { /* skip frame */ }
              }
            }
          }
          return pump();
        });
      }
      return pump().catch(function () {
        if (!bubble) { fallback(); return; }
        setStatus("Connection interrupted. Your partial reply is shown above.");
        finishSend();
      });
    }).catch(function () { fallback(); }).finally(function () { if (timeout) clearTimeout(timeout); });
  }

  function sendPlain(payload, typing) {
    post("/api/widget/chat", payload).then(function (data) {
      typing.remove();
      if (data && data.reply) {
        var bubble = addMessage("bot", renderMarkdown(data.reply));
        renderCitations(bubble, data.citations);
        handleAction(data.action);
      } else {
        addMessage("bot", esc((data && data.error) || "Sorry, something went wrong."));
      }
    }).catch(function () {
      typing.remove();
      addMessage("bot", "Sorry. I couldn't reach the server. Please try again in a moment.");
    }).finally(finishSend);
  }

  /* ---- live team replies (poll for agent messages) ---- */

  var lastAgentId = null;
  var pollSkip = 0;

  function addAgentMessage(content) {
    var el = addMessage("agent", "");
    var who = document.createElement("span");
    who.className = "cbs-who";
    who.textContent = "👤 " + (config.name || "Team");
    el.appendChild(who);
    var body = document.createElement("span");
    body.innerHTML = renderMarkdown(content);
    el.appendChild(body);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function setUnread(on) {
    var dot = launcher.querySelector(".cbs-unread");
    if (on && !dot) {
      dot = document.createElement("span");
      dot.className = "cbs-unread";
      launcher.style.position = "fixed";
      launcher.appendChild(dot);
    } else if (!on && dot) {
      dot.remove();
    }
  }

  function pollUpdates() {
    if (!ready || gated) return;
    var after = lastAgentId === null ? "" : "&after=" + lastAgentId;
    fetch(HOST + "/api/widget/updates?key=" + encodeURIComponent(BOT_KEY) +
          "&session_id=" + encodeURIComponent(sessionId()) + after, { headers: authHeaders() })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data) return;
        checkAccess(data);
        if (data.error) return;
        conversationStatus(data.status);
        if (lastAgentId === null) {
          lastAgentId = data.latest_id || 0;
          return;
        }
        (data.messages || []).forEach(function (m) {
          addAgentMessage(m.content);
          if (m.id > lastAgentId) lastAgentId = m.id;
        });
        if ((data.messages || []).length && !open) setUnread(true);
      })
      .catch(function () { /* transient network issue; next poll retries */ });
  }

  function startPolling() {
    clearInterval(pollTimer);
    pollTimer = setInterval(function () {
      if (!open) {
        if (document.hidden) return; // closed + backgrounded: nothing to show
        pollSkip = (pollSkip + 1) % 3; // every ~18s while closed
        if (pollSkip) return;
      }
      pollUpdates();
    }, 6000);
  }

  function togglePanel(forceOpen) {
    open = forceOpen !== undefined ? forceOpen : !open;
    panel.classList.toggle("open", open);
    panel.inert = !open;
    launcher.setAttribute("aria-expanded", String(open));
    if (open) {
      setUnread(false);
      var target = gated ? panel.querySelector(".cbs-gate input") : inputEl;
      if (target) target.focus();
    } else { launcher.focus(); }
  }

  function build() {
    var host = document.createElement("div");
    host.setAttribute("data-chatbot-studio", "");
    document.body.appendChild(host);
    root = host.attachShadow({ mode: "open" });

    var themeColor = /^#[0-9a-fA-F]{6}$/.test(config.theme_color) ? config.theme_color : "#4f46e5";
    var textOnTheme = contrastText(themeColor);

    var style = document.createElement("style");
    style.textContent = css(themeColor, config.position, textOnTheme);
    root.appendChild(style);

    launcher = document.createElement("button");
    launcher.className = "cbs-launcher";
    launcher.setAttribute("aria-expanded", "false");
    launcher.innerHTML = "💬 <span>" + esc(config.launcher_text || "Chat with us") + "</span>";
    launcher.addEventListener("click", function () { togglePanel(); });
    root.appendChild(launcher);

    panel = document.createElement("div");
    panel.className = "cbs-panel";
    panel.inert = true;
    panel.setAttribute("role", "region");
    panel.setAttribute("aria-label", config.name + " chat");
    panel.innerHTML =
      '<div class="cbs-header"><h3>' + esc(config.name) + "</h3>" +
      '<button class="cbs-new" type="button">New chat</button>' +
      '<button class="cbs-close" aria-label="Close">✕</button></div>' +
      '<div class="cbs-status" role="status">Connecting...</div>' +
      '<div class="cbs-messages" role="log" aria-live="polite" aria-label="Conversation"></div>' +
      '<div class="cbs-inputrow"><input class="cbs-input" aria-label="Your message" type="text" placeholder="Type your question…" maxlength="4000">' +
      '<button class="cbs-send" aria-label="Send">➤</button></div>' +
      '<div class="cbs-powered">Powered by Chatbot Studio</div>';
    root.appendChild(panel);

    messagesEl = panel.querySelector(".cbs-messages");
    inputEl = panel.querySelector(".cbs-input");
    panel.querySelector(".cbs-close").addEventListener("click", function () { togglePanel(false); });
    panel.querySelector(".cbs-send").addEventListener("click", function () { send(); });
    inputEl.addEventListener("keydown", function (e) { if (e.key === "Enter") send(); });

    panel.querySelector(".cbs-new").addEventListener("click", function () {
      if (sending || !window.confirm("Start a new conversation? The team will keep the previous conversation.")) return;
      conversationToken = ""; activeSession = "";
      try { localStorage.removeItem(SESSION_STORAGE_KEY + "_token"); localStorage.removeItem(SESSION_STORAGE_KEY); } catch (_e) {}
      if (!gated) connectConversation();
    });
    panel.addEventListener("keydown", function (e) { if (e.key === "Escape") togglePanel(false); });
    try { unlockToken = sessionStorage.getItem(SESSION_STORAGE_KEY + "_unlock"); } catch (_e) {}
    if (config.gated && !unlockToken) { showGate(); return; }
    connectConversation();
  }

  function showWelcome() {
    if (config.welcome_message) addMessage("bot", renderMarkdown(config.welcome_message));
    if (config.starter_questions && config.starter_questions.length) {
      starterWrap = document.createElement("div");
      starterWrap.className = "cbs-starters";
      config.starter_questions.forEach(function (question) {
        var chip = document.createElement("button");
        chip.className = "cbs-starter";
        chip.textContent = question;
        chip.addEventListener("click", function () { send(question); });
        starterWrap.appendChild(chip);
      });
      messagesEl.appendChild(starterWrap);
    }
  }

  /* Passphrase gate for internal-KB bots. Chat stays disabled until the shared
   * passphrase is exchanged for an unlock token. */
  function showGate() {
    if (gated) return;
    gated = true; ready = false; clearInterval(pollTimer);
    messagesEl.innerHTML = "";
    try { sessionStorage.removeItem(SESSION_STORAGE_KEY + "_unlock"); } catch (_e) {}
    setStatus("Private knowledge base");
    if (inputEl) inputEl.disabled = true;
    var sendBtn = panel.querySelector(".cbs-send");
    if (sendBtn) sendBtn.disabled = true;

    addMessage("bot", "🔒 This knowledge base is private. Enter the passphrase to continue.");
    var el = addMessage("bot", "");
    var form = document.createElement("form");
    form.className = "cbs-gate";
    var input = document.createElement("input");
    input.type = "password";
    input.className = "cbs-input";
    input.placeholder = "Passphrase";
    input.autocomplete = "off";
    var btn = document.createElement("button");
    btn.className = "cbs-action-btn";
    btn.type = "submit";
    btn.textContent = "Unlock";
    var msg = document.createElement("div");
    msg.className = "cbs-gate-msg";
    form.appendChild(input);
    form.appendChild(btn);
    form.appendChild(msg);
    el.appendChild(form);
    input.focus();

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      msg.textContent = "";
      btn.disabled = true;
      post("/api/widget/unlock", { key: BOT_KEY, passphrase: input.value })
        .then(function (data) {
          if (data && data.ok && data.unlock_token) {
            unlockToken = data.unlock_token;
            gated = false;
            try { sessionStorage.setItem(SESSION_STORAGE_KEY + "_unlock", unlockToken); } catch (_e) {}
            el.remove();
            if (inputEl) inputEl.disabled = false;
            if (sendBtn) sendBtn.disabled = false;
            connectConversation();
            if (inputEl) inputEl.focus();
          } else {
            msg.textContent = (data && data.error) || "Incorrect passphrase.";
            btn.disabled = false;
          }
        })
        .catch(function () { msg.textContent = "Couldn't reach the server. Try again."; btn.disabled = false; });
    });
  }

  function connectConversation() {
    ready = false; clearInterval(pollTimer);
    inputEl.disabled = true;
    root.querySelector(".cbs-send").disabled = true;
    setStatus("Connecting...");
    try { conversationToken = localStorage.getItem(SESSION_STORAGE_KEY + "_token") || ""; } catch (_e) {}
    post("/api/widget/session", { key: BOT_KEY, session_id: sessionId() }).then(function (data) {
      if (data.error) throw new Error(data.error);
      activeSession = data.session_id; conversationToken = data.conversation_token;
      try { localStorage.setItem(SESSION_STORAGE_KEY, activeSession); localStorage.setItem(SESSION_STORAGE_KEY + "_token", conversationToken); } catch (_e) {}
      return fetch(HOST + "/api/widget/history?key=" + encodeURIComponent(BOT_KEY) +
        "&session_id=" + encodeURIComponent(activeSession), { headers: authHeaders() }).then(function (resp) { return resp.json(); });
    }).then(function (data) {
      checkAccess(data);
      if (data.error) throw new Error(data.error);
      messagesEl.innerHTML = ""; starterWrap = null; leadFormShown = false;
      if (data.messages.length) {
        data.messages.forEach(function (m) {
          if (m.role === "agent") addAgentMessage(m.content);
          else addMessage(m.role === "user" ? "user" : "bot", renderMarkdown(m.content));
        });
      } else { showWelcome(); }
      lastAgentId = data.latest_id || 0;
      ready = true; inputEl.disabled = false; root.querySelector(".cbs-send").disabled = false;
      conversationStatus(data.status); startPolling();
    }).catch(function (err) {
      if (!gated) setStatus(err.message || "Could not connect. Use New chat to retry.");
    });
  }

  function init() {
    fetch(HOST + "/api/widget/config?key=" + encodeURIComponent(BOT_KEY))
      .then(function (resp) {
        if (!resp.ok) throw new Error("config " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        config = data;
        if (document.body) build();
        else document.addEventListener("DOMContentLoaded", build);
      })
      .catch(function (err) {
        if (window.console) console.warn("[Chatbot Studio] widget failed to load:", err.message);
      });
  }

  init();
})();
