/* Chatbot Studio dashboard. vanilla JS, no build step. */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function esc(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function toast(message, isError) {
    const el = $("#toast");
    el.textContent = message;
    el.classList.toggle("error", !!isError);
    el.hidden = false;
    clearTimeout(el._timer);
    el._timer = setTimeout(() => { el.hidden = true; }, 3200);
  }

  async function api(method, url, body) {
    const opts = { method, headers: { "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    let data = {};
    try { data = await resp.json(); } catch (_e) { /* empty body */ }
    if (resp.status === 401) {
      window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
      throw new Error("Not authenticated");
    }
    if (!resp.ok && resp.status !== 207) {
      throw new Error(data.error || ("Request failed (" + resp.status + ")"));
    }
    return data;
  }

  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  }

  function bindModals() {
    $$(".modal-backdrop").forEach((backdrop) => {
      backdrop.addEventListener("click", (e) => {
        if (e.target === backdrop || e.target.closest("[data-close-modal]")) backdrop.hidden = true;
      });
    });
  }

  /* ================= dashboard page ================= */

  async function initDashboard() {
    bindModals();
    const grid = $("#bot-grid");
    let selectedPreset = "";

    async function loadPresets() {
      const wrap = $("#preset-list");
      if (!wrap) return;
      try {
        const data = await api("GET", "/api/presets");
        wrap.innerHTML = "";
        const blank = document.createElement("button");
        blank.type = "button";
        blank.className = "preset-pick active";
        blank.dataset.slug = "";
        blank.innerHTML = "<strong>Blank bot</strong><span class='muted'>Start from scratch</span>";
        wrap.appendChild(blank);
        data.presets.forEach((p) => {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "preset-pick";
          b.dataset.slug = p.slug;
          const gr = p.guardrails && p.guardrails.length
            ? "<span class='badge badge-warn'>" + p.guardrails.length + " guardrails</span>" : "";
          const kb = p.mode === "internal_kb" ? "<span class='badge'>internal KB</span>" : "";
          b.innerHTML = "<strong>" + esc(p.name) + "</strong> " + gr + kb +
            "<span class='muted'>" + esc(p.tagline) + "</span>";
          wrap.appendChild(b);
        });
        wrap.addEventListener("click", (e) => {
          const pick = e.target.closest(".preset-pick");
          if (!pick) return;
          $$(".preset-pick", wrap).forEach((el) => el.classList.remove("active"));
          pick.classList.add("active");
          selectedPreset = pick.dataset.slug;
        });
      } catch (_e) { wrap.innerHTML = ""; }
    }

    async function refresh() {
      try {
        const data = await api("GET", "/api/bots");
        grid.innerHTML = "";
        if (!data.bots.length) {
          grid.innerHTML = '<div class="card"><h3>No bots yet</h3><p class="muted">' +
            'Create your first bot. add training, tweak its personality, then drop the embed ' +
            'snippet onto any site.</p></div>';
          return;
        }
        data.bots.forEach((bot) => {
          const card = document.createElement("div");
          card.className = "bot-card";
          card.innerHTML =
            '<h3><span class="bot-dot" style="background:' + esc(bot.theme_color) + '"></span>' +
            esc(bot.name) + "</h3>" +
            '<div class="bot-stats">' +
            "<span><strong>" + bot.source_count + "</strong> sources</span>" +
            "<span><strong>" + bot.conversation_count + "</strong> chats</span>" +
            "<span><strong>" + bot.lead_count + "</strong> leads</span></div>" +
            (bot.gap_count ? '<div><span class="badge badge-warn">🕳️ ' + bot.gap_count +
              (bot.gap_count === 1 ? " visitor question needs an answer" : " visitor questions need answers") +
              "</span></div>" : "") +
            '<div class="bot-card-foot"><code>' + esc(bot.embed_key) + "</code>" +
            '<span class="btn btn-small">Open builder →</span></div>';
          card.addEventListener("click", () => { window.location.href = "/bots/" + bot.id; });
          grid.appendChild(card);
        });
      } catch (err) {
        grid.innerHTML = '<p class="error-text">' + esc(err.message) + "</p>";
      }
    }

    $("#new-bot-btn").addEventListener("click", () => {
      $("#new-bot-modal").hidden = false;
      $("#new-bot-name").value = "";
      $("#new-bot-name").focus();
    });
    async function createBot() {
      const name = $("#new-bot-name").value.trim();
      if (!name && !selectedPreset) { toast("Give the bot a name or pick a preset", true); return; }
      try {
        const data = await api("POST", "/api/bots",
          selectedPreset ? { name, preset: selectedPreset } : { name });
        window.location.href = "/bots/" + data.bot.id;
      } catch (err) { toast(err.message, true); }
    }
    $("#create-bot-btn").addEventListener("click", createBot);
    $("#new-bot-name").addEventListener("keydown", (e) => { if (e.key === "Enter") createBot(); });

    loadPresets();
    refresh();
  }

  /* ================= builder page ================= */

  const BOT_ID = document.body.dataset.botId;
  const BASE_URL = document.body.dataset.baseUrl || window.location.origin;
  let bot = null;

  function switchTab(name) {
    $$(".tab-btn").forEach((b) => { b.classList.toggle("active", b.dataset.tab === name); if (b.dataset.tab === name) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
    $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
    if (window.location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
    const loader = { overview: loadOverview, training: loadTraining, redirects: loadRedirects, leads: loadLeads,
                     conversations: loadConversations, apikey: loadUsage,
                     analytics: loadAnalytics, faq: loadFaq }[name];
    if (loader) loader();
  }

  /* ---- behaviour tab ---- */

  const MODEL_CHOICES = (() => {
    const el = $("#model-choices-json");
    try { return el ? JSON.parse(el.textContent) : {}; } catch (_e) { return {}; }
  })();

  function refreshModelOptions() {
    const provider = $("#f-provider").value || null;
    const datalist = $("#model-options");
    datalist.innerHTML = "";
    const names = provider ? (MODEL_CHOICES[provider] || [])
                           : Object.values(MODEL_CHOICES).flat();
    names.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m;
      datalist.appendChild(opt);
    });
  }

  function starterRow(value) {
    const row = document.createElement("div");
    row.className = "starter-row";
    row.innerHTML = '<input type="text" maxlength="120" placeholder="e.g. What are your prices?">' +
      '<button class="icon-btn" title="Remove">✕</button>';
    row.querySelector("input").value = value || "";
    row.querySelector("button").addEventListener("click", () => row.remove());
    return row;
  }

  function fillBehaviour() {
    $("#f-system_prompt").value = bot.system_prompt;
    $$(".tone-pill").forEach((p) => p.classList.toggle("active", p.dataset.tone === bot.tone));
    $("#f-welcome_message").value = bot.welcome_message;
    $("#f-fallback_message").value = bot.fallback_message;
    $("#f-handoff_enabled").checked = !!bot.handoff_enabled;
    $("#f-provider").value = bot.provider;
    $("#f-model").value = bot.model;
    $("#f-temperature").value = bot.temperature;
    $("#temp-label").textContent = bot.temperature;
    const list = $("#starter-list");
    list.innerHTML = "";
    (bot.starter_questions || []).forEach((q) => list.appendChild(starterRow(q)));
    refreshModelOptions();
  }

  function collectBehaviour() {
    return {
      system_prompt: $("#f-system_prompt").value,
      tone: ($(".tone-pill.active") || {}).dataset ? $(".tone-pill.active").dataset.tone : bot.tone,
      welcome_message: $("#f-welcome_message").value,
      fallback_message: $("#f-fallback_message").value,
      handoff_enabled: $("#f-handoff_enabled").checked,
      provider: $("#f-provider").value,
      model: $("#f-model").value.trim(),
      temperature: parseFloat($("#f-temperature").value),
      starter_questions: $$("#starter-list input").map((i) => i.value.trim()).filter(Boolean),
    };
  }

  /* ---- guardrails & mode tab ---- */

  let presetMeta = null;

  async function fillGuardrails() {
    $("#f-guardrails").value = (bot.guardrails || []).join("\n");
    $("#f-mode").value = bot.mode || "public";
    $("#f-cite_sources").checked = !!bot.cite_sources;
    updatePassphraseStatus();
    // Show the preset's "why these guardrails matter" note when applicable.
    if (bot.preset && !presetMeta) {
      try { presetMeta = (await api("GET", "/api/presets")).presets; } catch (_e) { presetMeta = []; }
    }
    const preset = (presetMeta || []).find((p) => p.slug === bot.preset);
    const noteCard = $("#guardrail-note");
    if (preset && preset.guardrail_note) {
      $("#guardrail-note-text").textContent = preset.guardrail_note;
      noteCard.hidden = false;
    } else {
      noteCard.hidden = true;
    }
  }

  function updatePassphraseStatus() {
    const gated = bot.access_gated;
    $("#passphrase-status").innerHTML = gated
      ? "🔒 A passphrase is set; the widget is locked."
      : (bot.mode === "internal_kb"
          ? "⚠️ No passphrase set; this internal KB is currently open to anyone with the key."
          : "No passphrase set (only used in internal-KB mode).");
  }

  function collectGuardrails() {
    return {
      guardrails: $("#f-guardrails").value.split("\n").map((s) => s.trim()).filter(Boolean),
      mode: $("#f-mode").value,
      cite_sources: $("#f-cite_sources").checked,
    };
  }

  function bindPassphrase() {
    $("#passphrase-save").addEventListener("click", async () => {
      const pass = $("#passphrase-input").value.trim();
      if (pass.length < 4) { toast("Passphrase must be at least 4 characters", true); return; }
      try {
        await api("POST", "/api/bots/" + BOT_ID + "/passphrase", { passphrase: pass });
        $("#passphrase-input").value = "";
        toast("Passphrase set (hashed)");
        await reloadBot();
        updatePassphraseStatus();
      } catch (err) { toast(err.message, true); }
    });
    $("#passphrase-clear").addEventListener("click", async () => {
      if (!confirm("Remove the passphrase? The knowledge base will no longer be gated.")) return;
      await api("DELETE", "/api/bots/" + BOT_ID + "/passphrase");
      toast("Passphrase removed");
      await reloadBot();
      updatePassphraseStatus();
    });
  }

  /* ---- appearance tab ---- */

  function fillAppearance() {
    $("#f-theme_color").value = bot.theme_color;
    $("#f-launcher_text").value = bot.launcher_text;
    $("#f-position").value = bot.position;
    updateLauncherPreview();
  }

  function updateLauncherPreview() {
    $("#launcher-preview").style.background = $("#f-theme_color").value;
    $("#launcher-preview-text").textContent = $("#f-launcher_text").value || "Chat with us";
  }

  function collectAppearance() {
    return {
      theme_color: $("#f-theme_color").value,
      launcher_text: $("#f-launcher_text").value,
      position: $("#f-position").value,
    };
  }

  /* ---- lead capture tab ---- */

  function leadFieldRow(field) {
    const row = document.createElement("div");
    row.className = "lead-field-row";
    row.innerHTML =
      '<input type="text" class="lf-label" placeholder="Label (e.g. Work email)" maxlength="60">' +
      '<select class="lf-type"><option value="text">Text</option><option value="email">Email</option>' +
      '<option value="tel">Phone</option><option value="textarea">Long text</option></select>' +
      '<label class="inline"><input type="checkbox" class="lf-required"> required</label>' +
      '<button class="icon-btn" title="Remove">✕</button>';
    row.querySelector(".lf-label").value = field.label || "";
    row.querySelector(".lf-type").value = field.type || "text";
    row.querySelector(".lf-required").checked = !!field.required;
    row.dataset.key = field.key || "";
    row.querySelector(".icon-btn").addEventListener("click", () => row.remove());
    return row;
  }

  function fillLeadCapture() {
    const list = $("#lead-fields-list");
    list.innerHTML = "";
    (bot.lead_fields || []).forEach((f) => list.appendChild(leadFieldRow(f)));
    $("#f-lead_rule").value = bot.lead_rule;
    $("#f-lead_webhook_url").value = bot.lead_webhook_url;
    $("#f-lead_alert_email").value = bot.lead_alert_email;
  }

  function collectLeadCapture() {
    return {
      lead_fields: $$("#lead-fields-list .lead-field-row").map((row) => ({
        key: row.dataset.key || undefined,
        label: row.querySelector(".lf-label").value.trim(),
        type: row.querySelector(".lf-type").value,
        required: row.querySelector(".lf-required").checked,
      })).filter((f) => f.label),
      lead_rule: $("#f-lead_rule").value,
      lead_webhook_url: $("#f-lead_webhook_url").value.trim(),
      lead_alert_email: $("#f-lead_alert_email").value.trim(),
    };
  }

  /* ---- training tab ---- */

  const TYPE_ICONS = { text: "📝", qa: "❓", url: "🌐", file: "📄" };
  const RECRAWL_LABELS = { 0: "Never", 24: "Daily", 168: "Weekly" };
  let pendingGapId = null;  // gap being answered via the Q&A form prefill

  async function loadTraining() {
    try {
      const [srcData, gapData] = await Promise.all([
        api("GET", "/api/bots/" + BOT_ID + "/sources"),
        api("GET", "/api/bots/" + BOT_ID + "/gaps"),
      ]);
      const tbody = $("#sources-table tbody");
      tbody.innerHTML = "";
      $("#sources-empty").hidden = srcData.sources.length > 0;
      srcData.sources.forEach((src) => {
        const tr = document.createElement("tr");
        const status = src.status === "pending" ? '<span class="badge badge-warn">queued</span>' : src.status === "ready"
          ? '<span class="badge badge-ok">ready</span>'
          : '<span class="badge badge-err" title="' + esc(src.error) + '">error</span>';
        const recrawlBtn = src.type === "url"
          ? '<button class="btn btn-small" data-recrawl="' + src.id + '">Re-crawl</button> ' : "";
        const recrawlCell = src.type === "url"
          ? '<select class="recrawl-select" data-recrawl-select="' + src.id + '">' +
            [0, 24, 168].map((h) => '<option value="' + h + '"' +
              (src.recrawl_hours === h ? " selected" : "") + ">" + RECRAWL_LABELS[h] + "</option>").join("") +
            "</select>"
          : '<span class="muted">. </span>';
        tr.innerHTML =
          "<td>" + (TYPE_ICONS[src.type] || "") + " " + esc(src.type) + "</td>" +
          "<td>" + esc(src.title) + (src.error ? '<div class="error-text small">' + esc(src.error) + "</div>" : "") + "</td>" +
          "<td>" + src.chunk_count + "</td>" +
          "<td>" + status + "</td>" +
          "<td>" + recrawlCell + "</td>" +
          "<td>" + fmtDate(src.created_at) + "</td>" +
          '<td class="cell-actions">' + recrawlBtn +
          '<button class="btn btn-small" data-edit-source="' + src.id + '">Edit / history</button> ' +
          '<button class="btn btn-small btn-danger-ghost" data-del-source="' + src.id + '">Delete</button></td>';
        tbody.appendChild(tr);
      });

      const gaps = $("#gaps-list");
      gaps.innerHTML = "";
      $("#gaps-empty").hidden = gapData.gaps.length > 0;
      gapData.gaps.forEach((gap) => {
        const item = document.createElement("div");
        item.className = "gap-item";
        item.innerHTML = '<span class="q">' + esc(gap.question) + "</span>" +
          '<span class="muted small">' + fmtDate(gap.created_at) + "</span>" +
          '<button class="btn btn-small" data-gap-answer="' + gap.id + '">Answer it</button>' +
          '<button class="icon-btn" data-gap-dismiss="' + gap.id + '" title="Dismiss">✕</button>';
        item.dataset.question = gap.question;
        gaps.appendChild(item);
      });
    } catch (err) { toast(err.message, true); }
  }

  function bindTraining() {
    $("#add-text-btn").addEventListener("click", () => addSource($("#add-text-btn"), {
      type: "text", title: $("#src-text-title").value, content: $("#src-text-content").value,
    }, () => { $("#src-text-title").value = ""; $("#src-text-content").value = ""; }));

    $("#add-qa-btn").addEventListener("click", () => addSource($("#add-qa-btn"), {
      type: "qa", question: $("#src-qa-q").value, answer: $("#src-qa-a").value,
    }, () => {
      $("#src-qa-q").value = ""; $("#src-qa-a").value = "";
      if (pendingGapId !== null) {  // answering a logged gap closes it
        pendingGapId = null;
      }
    }));

    $("#add-url-btn").addEventListener("click", () => {
      $("#crawl-status").textContent = "Crawling… this can take up to a minute for multi-page crawls.";
      addSource($("#add-url-btn"), {
        type: "url", url: $("#src-url").value, max_pages: parseInt($("#src-url-pages").value, 10) || 5,
        recrawl_hours: parseInt($("#src-url-recrawl").value, 10) || 0,
      }, () => { $("#src-url").value = ""; $("#crawl-status").textContent = ""; },
      () => { $("#crawl-status").textContent = ""; });
    });

    $("#add-file-btn").addEventListener("click", async () => {
      const input = $("#src-file");
      if (!input.files.length) { toast("Choose a file first", true); return; }
      const btn = $("#add-file-btn");
      btn.disabled = true;
      btn.textContent = "Uploading…";
      try {
        const form = new FormData();
        form.append("file", input.files[0]);
        const resp = await fetch("/api/bots/" + BOT_ID + "/sources/upload?background=1",
                                 { method: "POST", body: form, headers: { "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content } });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok && resp.status !== 207) throw new Error(data.error || "Upload failed");
        if (data.error) toast("Uploaded, but indexing failed: " + data.error, true);
        else toast("File uploaded. Training queued.");
        loadJobs();
        input.value = "";
        loadTraining();
      } catch (err) { toast(err.message, true); }
      finally { btn.disabled = false; btn.textContent = "Upload & index"; }
    });

    $("#sources-table").addEventListener("change", async (e) => {
      const select = e.target.closest("[data-recrawl-select]");
      if (!select) return;
      try {
        await api("PATCH", "/api/bots/" + BOT_ID + "/sources/" + select.dataset.recrawlSelect,
                  { recrawl_hours: parseInt(select.value, 10) });
        toast(select.value === "0" ? "Scheduled re-crawl off" :
              "Re-crawls " + RECRAWL_LABELS[select.value].toLowerCase());
      } catch (err) { toast(err.message, true); loadTraining(); }
    });

    $("#sources-table").addEventListener("click", async (e) => {
      const del = e.target.closest("[data-del-source]");
      const recrawl = e.target.closest("[data-recrawl]");
      const edit = e.target.closest("[data-edit-source]");
      if (edit) { openSourceEditor(edit.dataset.editSource); return; }
      try {
        if (del && confirm("Delete this source and its chunks?")) {
          await api("DELETE", "/api/bots/" + BOT_ID + "/sources/" + del.dataset.delSource);
          toast("Source deleted");
          loadTraining();
        } else if (recrawl) {
          recrawl.disabled = true;
          recrawl.textContent = "Crawling…";
          await api("POST", "/api/bots/" + BOT_ID + "/jobs", {source_id: Number(recrawl.dataset.recrawl), kind: "recrawl"});
          toast("Website refresh queued");
          loadJobs();
          loadTraining();
        }
      } catch (err) { toast(err.message, true); loadTraining(); }
    });

    $("#gaps-list").addEventListener("click", async (e) => {
      const answer = e.target.closest("[data-gap-answer]");
      const dismiss = e.target.closest("[data-gap-dismiss]");
      try {
        if (answer) {
          $("#src-qa-q").value = answer.closest(".gap-item").dataset.question;
          pendingGapId = parseInt(answer.dataset.gapAnswer, 10);
          $("#src-qa-a").focus();
          window.scrollTo({ top: 0, behavior: "smooth" });
          toast("Question copied into the Q&A form. write the answer");
        } else if (dismiss) {
          await api("DELETE", "/api/bots/" + BOT_ID + "/gaps/" + dismiss.dataset.gapDismiss);
          loadTraining();
        }
      } catch (err) { toast(err.message, true); }
    });
  }

  async function addSource(button, payload, onSuccess, onError) {
    button.disabled = true;
    try {
      if (pendingGapId !== null && payload.type === "qa") payload.gap_id = pendingGapId;
      await api("POST", "/api/bots/" + BOT_ID + "/jobs", payload);
      toast("Training queued. Progress appears below.");
      loadJobs();
      onSuccess();
      loadTraining();
    } catch (err) {
      toast(err.message, true);
      if (onError) onError();
    } finally {
      button.disabled = false;
      if (button.id === "add-url-btn") button.textContent = "Crawl & index";
    }
  }

  /* ---- FAQ miner tab (what people ask) ---- */

  let faqClusters = [];

  const SIGNAL_LABELS = {
    no_reply: "no reply", gap: "retrieval gap", handoff: "human handoff",
    canned_reply: "canned reply", short_reply: "very short reply",
    ended_after: "visitor left right after",
  };

  async function loadFaq() {
    try {
      const [cData, eData] = await Promise.all([
        api("GET", "/api/bots/" + BOT_ID + "/faq/clusters"),
        api("GET", "/api/bots/" + BOT_ID + "/faq/entries"),
      ]);
      faqClusters = cData.clusters;
      renderFaqClusters();
      renderFaqEntries(eData.entries);
    } catch (err) { toast(err.message, true); }
  }

  function renderFaqClusters() {
    const list = $("#faq-clusters");
    list.innerHTML = "";
    $("#faq-empty").hidden = faqClusters.length > 0;
    faqClusters.forEach((cl, i) => {
      const signals = Object.entries(cl.weak_signals || {})
        .map(([k, n]) => (SIGNAL_LABELS[k] || k) + " ×" + n).join(", ");
      const item = document.createElement("div");
      item.className = "faq-cluster";
      item.innerHTML =
        '<div class="faq-cluster-head">' +
          '<span class="q"><strong>' + esc(cl.question) + "</strong></span>" +
          '<span class="badge">asked ×' + cl.count + "</span>" +
          (cl.weak
            ? '<span class="badge badge-warn" title="' + esc(signals) + '">bot struggles</span>'
            : '<span class="badge badge-ok" title="' + esc(signals || "answers look fine") + '">handled ok</span>') +
          '<span class="muted small">' + fmtDate(cl.last_seen) + "</span>" +
        "</div>" +
        (cl.examples.length > 1
          ? '<details class="faq-examples"><summary class="muted small">' + cl.examples.length +
            " example phrasings</summary>" +
            cl.examples.map((ex) => '<div class="muted small">“' + esc(ex) + "”</div>").join("") +
            "</details>"
          : "") +
        '<div class="faq-draft" hidden>' +
          '<textarea rows="4" class="faq-answer" placeholder="FAQ answer…"></textarea>' +
          '<div class="row">' +
            '<button class="btn btn-primary btn-small" data-faq-approve="' + i + '">Approve into training</button>' +
            '<button class="btn btn-small" data-faq-cancel="' + i + '">Cancel</button>' +
            '<span class="muted small faq-draft-origin"></span>' +
          "</div>" +
        "</div>" +
        '<div class="row faq-actions">' +
          '<button class="btn btn-small" data-faq-draft="' + i + '">Draft FAQ answer</button>' +
          '<button class="btn btn-small btn-danger-ghost" data-faq-dismiss="' + i + '">Dismiss</button>' +
        "</div>";
      list.appendChild(item);
    });
  }

  function renderFaqEntries(entries) {
    const tbody = $("#faq-entries-table tbody");
    tbody.innerHTML = "";
    $("#faq-entries-empty").hidden = entries.length > 0;
    entries.forEach((entry) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + esc(entry.question) + "</td>" +
        '<td class="muted">' + esc(entry.answer.length > 140 ? entry.answer.slice(0, 140) + "…" : entry.answer) + "</td>" +
        "<td>" + fmtDate(entry.created_at) + "</td>" +
        '<td class="cell-actions"><button class="btn btn-small btn-danger-ghost" data-faq-del-entry="' +
        entry.id + '">Remove</button></td>';
      tbody.appendChild(tr);
    });
  }

  const DRAFT_ORIGIN_LABELS = {
    llm: "AI draft grounded in this bot's training. review before approving.",
    stitched: "Stitched from the bot's best existing reply. review before approving.",
    todo: "The bot never answered this well. write the answer yourself.",
  };

  function bindFaq() {
    $("#faq-refresh").addEventListener("click", () => { loadFaq(); toast("Re-mined visitor questions"); });

    $("#faq-clusters").addEventListener("click", async (e) => {
      const draft = e.target.closest("[data-faq-draft]");
      const approve = e.target.closest("[data-faq-approve]");
      const cancel = e.target.closest("[data-faq-cancel]");
      const dismiss = e.target.closest("[data-faq-dismiss]");
      const btn = draft || approve || cancel || dismiss;
      if (!btn) return;
      const idx = parseInt(btn.dataset.faqDraft || btn.dataset.faqApprove ||
                           btn.dataset.faqCancel || btn.dataset.faqDismiss, 10);
      const cl = faqClusters[idx];
      const item = btn.closest(".faq-cluster");
      try {
        if (draft) {
          draft.disabled = true;
          draft.textContent = "Drafting…";
          const data = await api("POST", "/api/bots/" + BOT_ID + "/faq/draft", {
            question: cl.question, best_answer: cl.best_answer || "", count: cl.count,
          });
          item.querySelector(".faq-draft").hidden = false;
          item.querySelector(".faq-answer").value = data.answer;
          item.querySelector(".faq-draft-origin").textContent = DRAFT_ORIGIN_LABELS[data.origin] || "";
          item.querySelector(".faq-answer").focus();
          draft.textContent = "Re-draft";
          draft.disabled = false;
        } else if (approve) {
          const answer = item.querySelector(".faq-answer").value.trim();
          if (!answer) { toast("Write or draft an answer first", true); return; }
          approve.disabled = true;
          const data = await api("POST", "/api/bots/" + BOT_ID + "/faq/approve", {
            question: cl.question, answer, members_norm: cl.members_norm || [],
          });
          if (data.error) toast("Approved, but indexing failed: " + data.error, true);
          else toast("Approved. added to training as a Q&A source + to the website FAQ");
          loadFaq();
        } else if (cancel) {
          item.querySelector(".faq-draft").hidden = true;
        } else if (dismiss) {
          await api("POST", "/api/bots/" + BOT_ID + "/faq/dismiss",
                    { members_norm: cl.members_norm || [] });
          toast("Dismissed. this question won't be mined again");
          loadFaq();
        }
      } catch (err) {
        toast(err.message, true);
        if (draft) { draft.textContent = "Draft FAQ answer"; draft.disabled = false; }
        if (approve) approve.disabled = false;
      }
    });

    $("#faq-entries-table").addEventListener("click", async (e) => {
      const del = e.target.closest("[data-faq-del-entry]");
      if (!del) return;
      if (!confirm("Remove from the website FAQ export? (The Q&A training source stays. " +
        "delete it from the Training tab if you want it gone entirely.)")) return;
      try {
        await api("DELETE", "/api/bots/" + BOT_ID + "/faq/entries/" + del.dataset.faqDelEntry);
        loadFaq();
      } catch (err) { toast(err.message, true); }
    });

    async function copyExport(format, label) {
      try {
        const resp = await fetch("/api/bots/" + BOT_ID + "/faq/export?format=" + format);
        if (!resp.ok) throw new Error("Export failed (" + resp.status + ")");
        const text = await resp.text();
        await navigator.clipboard.writeText(text);
        toast(label + " copied. paste it into the website");
      } catch (err) { toast(err.message, true); }
    }
    $("#faq-copy-html").addEventListener("click", () => copyExport("html", "HTML + FAQPage JSON-LD"));
    $("#faq-copy-md").addEventListener("click", () => copyExport("markdown", "Markdown FAQ"));
  }

  /* ---- redirects tab ---- */

  async function loadRedirects() {
    try {
      const data = await api("GET", "/api/bots/" + BOT_ID + "/redirects");
      const tbody = $("#redirects-table tbody");
      tbody.innerHTML = "";
      data.redirects.forEach((r) => {
        const tr = document.createElement("tr");
        tr.innerHTML =
          "<td>" + esc(r.title) + "</td>" +
          '<td><a href="' + esc(r.url) + '" target="_blank" rel="noopener">' + esc(r.url) + "</a></td>" +
          "<td>" + esc(r.description) + "</td>" +
          "<td>" + (r.mode === "auto" ? '<span class="badge badge-warn">auto</span>'
                                       : '<span class="badge badge-ok">button</span>') + "</td>" +
          '<td class="cell-actions"><button class="btn btn-small btn-danger-ghost" data-del-redirect="' +
          r.id + '">Delete</button></td>';
        tbody.appendChild(tr);
      });
    } catch (err) { toast(err.message, true); }
  }

  function bindRedirects() {
    $("#add-redirect-btn").addEventListener("click", async () => {
      try {
        await api("POST", "/api/bots/" + BOT_ID + "/redirects", {
          title: $("#rd-title").value, url: $("#rd-url").value,
          description: $("#rd-desc").value, mode: $("#rd-mode").value,
        });
        $("#rd-title").value = $("#rd-url").value = $("#rd-desc").value = "";
        toast("Redirect added");
        loadRedirects();
      } catch (err) { toast(err.message, true); }
    });
    $("#redirects-table").addEventListener("click", async (e) => {
      const del = e.target.closest("[data-del-redirect]");
      if (!del) return;
      await api("DELETE", "/api/bots/" + BOT_ID + "/redirects/" + del.dataset.delRedirect);
      loadRedirects();
    });
  }

  /* ---- api key tab ---- */

  function fillApiKey() {
    $("#apikey-provider").textContent = bot.resolved_provider;
    $("#apikey-model").textContent = bot.resolved_model;
    $("#apikey-status").textContent = bot.api_key_set
      ? "This bot has its own key (" + bot.api_key_hint + "). usage bills to that account."
      : "No bot-specific key. using the platform key from .env.";
  }

  async function loadUsage() {
    try {
      const data = await api("GET", "/api/bots/" + BOT_ID + "/usage");
      const grid = $("#usage-grid");
      const fmt = (n) => n.toLocaleString();
      grid.innerHTML =
        '<div class="usage-stat"><strong>' + fmt(data.last_30_days.messages) + "</strong><span>replies · 30 days</span></div>" +
        '<div class="usage-stat"><strong>' + fmt(data.last_30_days.tokens_in) + "</strong><span>tokens in · 30 days</span></div>" +
        '<div class="usage-stat"><strong>' + fmt(data.last_30_days.tokens_out) + "</strong><span>tokens out · 30 days</span></div>" +
        '<div class="usage-stat"><strong>' + fmt(data.all_time.messages) + "</strong><span>replies · all time</span></div>";
    } catch (err) { toast(err.message, true); }
  }

  function bindApiKey() {
    $("#apikey-save").addEventListener("click", async () => {
      const key = $("#apikey-input").value.trim();
      if (!key) { toast("Paste a key first", true); return; }
      try {
        await api("POST", "/api/bots/" + BOT_ID + "/apikey", { api_key: key });
        $("#apikey-input").value = "";
        toast("Key saved (encrypted)");
        await reloadBot();
        fillApiKey();
      } catch (err) { toast(err.message, true); }
    });
    $("#apikey-test").addEventListener("click", async () => {
      const el = $("#apikey-test-result");
      el.textContent = "Testing…";
      try {
        const data = await api("POST", "/api/bots/" + BOT_ID + "/apikey/test");
        el.textContent = (data.ok ? "✅ " : "❌ ") + data.detail;
      } catch (err) { el.textContent = "❌ " + err.message; }
    });
    $("#apikey-clear").addEventListener("click", async () => {
      if (!confirm("Remove this bot's key and fall back to the platform key?")) return;
      await api("DELETE", "/api/bots/" + BOT_ID + "/apikey");
      toast("Key removed");
      await reloadBot();
      fillApiKey();
    });
  }

  /* ---- embed tab ---- */

  function fillEmbed() {
    const snippet = '<script src="' + BASE_URL + '/widget.js" data-bot-key="' +
      bot.embed_key + '" async><\/script>';
    $("#embed-snippet").textContent = snippet;
    $("#embed-key").textContent = bot.embed_key;
    $("#open-demo").href = BASE_URL + "/demo?key=" + encodeURIComponent(bot.embed_key);
  }

  function bindEmbed() {
    $("#copy-snippet").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($("#embed-snippet").textContent);
        toast("Snippet copied. paste before </body>");
      } catch (_e) { toast("Copy failed. select and copy manually", true); }
    });
    $("#rotate-key").addEventListener("click", async () => {
      if (!confirm("Rotate the embed key? The old snippet stops working immediately.")) return;
      const data = await api("POST", "/api/bots/" + BOT_ID + "/rotate-key");
      bot = data.bot;
      fillEmbed();
      toast("Embed key rotated");
    });
  }

  /* ---- leads tab ---- */

  async function loadLeads() {
    try {
      const data = await api("GET", "/api/bots/" + BOT_ID + "/leads");
      $("#leads-count").textContent = data.leads.length ? "(" + data.leads.length + ")" : "";
      $("#leads-empty").hidden = data.leads.length > 0;
      const keys = [];
      (bot.lead_fields || []).forEach((f) => { if (f.key) keys.push(f.key); });
      data.leads.forEach((lead) => Object.keys(lead.data).forEach((k) => {
        if (!keys.includes(k)) keys.push(k);
      }));
      const thead = $("#leads-table thead");
      const tbody = $("#leads-table tbody");
      thead.innerHTML = "<tr>" + keys.map((k) => "<th>" + esc(k) + "</th>").join("") +
        "<th>Page</th><th>When</th><th>Stage</th><th></th></tr>";
      tbody.innerHTML = "";
      data.leads.forEach((lead) => {
        const tr = document.createElement("tr");
        tr.innerHTML = keys.map((k) => "<td>" + esc(lead.data[k] || "") + "</td>").join("") +
          '<td class="muted small">' + esc((lead.page_url || "").replace(/^https?:\/\//, "")) + "</td>" +
          "<td>" + fmtDate(lead.created_at) + "</td>" +
          '<td><select aria-label="Lead stage" data-lead-stage="' + lead.id + '">' +
          ["new","contacted","qualified","booked","closed"].map(stage=>'<option value="'+stage+'"'+(stage===lead.stage?' selected':'')+'>'+stage+'</option>').join("") + '</select></td>' +
          '<td><button class="btn btn-small btn-danger-ghost" data-delete-lead="'+lead.id+'">Delete</button></td>';
        tbody.appendChild(tr);
      });
      tbody.onchange = async e => {const el=e.target.closest("[data-lead-stage]");if(!el)return;try{await api("PATCH","/api/bots/"+BOT_ID+"/leads/"+el.dataset.leadStage,{stage:el.value});toast("Lead stage saved");}catch(err){toast(err.message,true);loadLeads();}};
      tbody.onclick = async e => {const el=e.target.closest("[data-delete-lead]");if(!el || !confirm("Delete this lead? Its conversation remains available."))return;try{await api("DELETE","/api/bots/"+BOT_ID+"/leads/"+el.dataset.deleteLead);loadLeads();}catch(err){toast(err.message,true);}};
      $("#export-leads").href = "/api/bots/" + BOT_ID + "/leads?format=csv";
    } catch (err) { toast(err.message, true); }
  }

  /* ---- conversations tab ---- */

  const STATUS_BADGES = {
    bot: '<span class="badge badge-ok">bot</span>',
    needs_human: '<span class="badge badge-warn">needs human</span>',
    human: '<span class="badge badge-err">human live</span>',
  };
  const ACTION_LABELS = {
    redirect: (a) => "→ redirect: " + (a.label || a.url),
    lead_form: () => "→ showed lead form",
    handoff: () => "→ flagged for the team",
  };
  let openConvoId = null;

  async function loadConversations() {
    try {
      const data = await api("GET", "/api/bots/" + BOT_ID + "/conversations");
      const tbody = $("#convos-table tbody");
      $("#convos-empty").hidden = data.conversations.length > 0;
      tbody.innerHTML = "";
      data.conversations.forEach((c) => {
        const tr = document.createElement("tr");
        tr.innerHTML =
          "<td>" + esc((c.first_message || "(empty)").slice(0, 80)) + "</td>" +
          "<td>" + c.message_count + "</td>" +
          "<td>" + (STATUS_BADGES[c.status] || "") + "</td>" +
          "<td>" + (c.lead_count ? "🧲 yes" : ". ") + "</td>" +
          "<td>" + fmtDate(c.last_message_at) + "</td>" +
          '<td class="cell-actions"><button class="btn btn-small" data-view-convo="' + c.id + '">View</button> ' +
          '<button class="btn btn-small btn-danger-ghost" data-del-convo="' + c.id + '">Delete</button></td>';
        tbody.appendChild(tr);
      });
    } catch (err) { toast(err.message, true); }
  }

  async function openTranscript(convoId) {
    const data = await api("GET", "/api/bots/" + BOT_ID + "/conversations/" + convoId);
    openConvoId = convoId;
    const body = $("#transcript-body");
    body.innerHTML = "";
    data.messages.forEach((m) => {
      const bubble = document.createElement("div");
      bubble.className = "bubble " + (m.role === "user" ? "user" : m.role === "agent" ? "agent" : "assistant");
      if (m.role === "agent") {
        const who = document.createElement("span");
        who.className = "who";
        who.textContent = "👤 Team";
        bubble.appendChild(who);
      }
      bubble.appendChild(document.createTextNode(m.content));
      if (m.action && ACTION_LABELS[m.action.type]) {
        const tag = document.createElement("span");
        tag.className = "action-tag";
        tag.textContent = ACTION_LABELS[m.action.type](m.action);
        bubble.appendChild(tag);
      }
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = fmtDate(m.created_at);
      bubble.appendChild(meta);
      body.appendChild(bubble);
    });
    const status = data.conversation.status;
    $("#transcript-status").innerHTML = STATUS_BADGES[status] || "";
    $("#handback-btn").hidden = status === "bot";
    $("#transcript-modal").hidden = false;
    body.scrollTop = body.scrollHeight;
  }

  function bindConversations() {
    $("#convos-table").addEventListener("click", async (e) => {
      const view = e.target.closest("[data-view-convo]");
      const del = e.target.closest("[data-del-convo]");
      try {
        if (view) {
          await openTranscript(parseInt(view.dataset.viewConvo, 10));
        } else if (del && confirm("Delete this conversation?")) {
          await api("DELETE", "/api/bots/" + BOT_ID + "/conversations/" + del.dataset.delConvo);
          loadConversations();
        }
      } catch (err) { toast(err.message, true); }
    });

    $("#send-agent-reply").addEventListener("click", async () => {
      const textarea = $("#agent-reply");
      const message = textarea.value.trim();
      if (!message || openConvoId === null) return;
      const btn = $("#send-agent-reply");
      btn.disabled = true;
      try {
        await api("POST", "/api/bots/" + BOT_ID + "/conversations/" + openConvoId + "/reply",
                  { message });
        textarea.value = "";
        toast("Sent. the visitor sees it live; AI is paused for this chat");
        await openTranscript(openConvoId);
        loadConversations();
      } catch (err) { toast(err.message, true); }
      finally { btn.disabled = false; }
    });

    $("#handback-btn").addEventListener("click", async () => {
      if (openConvoId === null) return;
      try {
        await api("POST", "/api/bots/" + BOT_ID + "/conversations/" + openConvoId + "/status",
                  { status: "bot" });
        toast("Handed back. the AI answers again");
        await openTranscript(openConvoId);
        loadConversations();
      } catch (err) { toast(err.message, true); }
    });
  }

  /* ---- analytics tab ---- */

  async function loadAnalytics() {
    try {
      const data = await api("GET", "/api/bots/" + BOT_ID + "/analytics");
      const k = data.last_30_days;
      const pct = (v) => v == null ? ". " : Math.round(v * 100) + "%";
      $("#analytics-kpis").innerHTML =
        '<div class="usage-stat"><strong>' + k.conversations + "</strong><span>conversations</span></div>" +
        '<div class="usage-stat"><strong>' + k.user_messages + "</strong><span>visitor messages</span></div>" +
        '<div class="usage-stat"><strong>' + k.leads + "</strong><span>leads</span></div>" +
        '<div class="usage-stat"><strong>' + pct(k.lead_conversion) + "</strong><span>lead conversion</span></div>" +
        '<div class="usage-stat"><strong>' + pct(k.answer_rate) + "</strong><span>retrieval coverage</span></div>" +
        '<div class="usage-stat"><strong>' + k.gaps + "</strong><span>answer gaps</span></div>" +
        '<div class="usage-stat"><strong>' + k.handoffs + "</strong><span>human handoffs</span></div>";
      const chart = $("#chart-messages");
      const max = Math.max(1, ...data.daily.map((d) => d.messages), ...data.daily.map((d) => d.leads));
      chart.innerHTML = "";
      data.daily.forEach((d) => {
        const col = document.createElement("div");
        col.className = "bar-col";
        col.dataset.tip = d.date + ": " + d.messages + " msgs, " + d.leads + " leads";
        const pair = document.createElement("div");
        pair.className = "bar-pair";
        const msgBar = document.createElement("div");
        msgBar.className = "bar";
        msgBar.style.height = Math.round((d.messages / max) * 100) + "%";
        const leadBar = document.createElement("div");
        leadBar.className = "bar leads";
        leadBar.style.height = Math.round((d.leads / max) * 100) + "%";
        pair.appendChild(msgBar);
        pair.appendChild(leadBar);
        const label = document.createElement("div");
        label.className = "bar-label";
        label.textContent = d.date.slice(5);
        col.appendChild(pair);
        col.appendChild(label);
        chart.appendChild(col);
      });
    } catch (err) { toast(err.message, true); }
  }

  const dirtyPanels = new Set();
  let editingSource = null, jobsSignature = "";
  async function loadOverview() {
    try {
      const data = await api("GET", "/api/bots/"+BOT_ID+"/readiness");
      $("#setup-checks").innerHTML=data.checks.map((c,i)=>'<a class="setup-step '+(c.done?'complete':'')+'" href="#'+c.tab+'" data-setup-tab="'+c.tab+'"><span class="step-number">'+(c.done?'✓':i+1)+'</span><span>'+esc(c.label)+'</span><small>'+(c.done?'Configured':'Set up')+'</small></a>').join("");
      $("#setup-checks").querySelectorAll("a").forEach(a=>a.onclick=e=>{e.preventDefault();switchTab(a.dataset.setupTab);});
      $("#setup-demo").href=BASE_URL+"/demo?key="+encodeURIComponent(bot.embed_key);
      loadJobs();
    } catch(err) {toast(err.message,true);}
  }
  async function loadJobs() {
    try {
      const data=await api("GET","/api/bots/"+BOT_ID+"/jobs");
      const signature=JSON.stringify(data.jobs);
      const html=data.jobs.slice(0,6).map(j=>'<div class="job-line"><strong>'+esc(j.kind==='recrawl'?'Website refresh':j.kind==='restore'?'Restore source':'Train source')+'</strong><span class="badge '+(j.status==='failed'?'badge-err':j.status==='succeeded'?'badge-ok':'badge-warn')+'">'+esc(j.status)+'</span><span>'+fmtDate(j.created_at)+'</span>'+(j.status==='failed'?'<button class="btn btn-small" data-job-retry="'+j.id+'">Retry</button>':'')+(j.error?'<span class="error-text">'+esc(j.error)+'</span>':'')+'</div>').join("") || '<p class="muted small">No training jobs yet. Add a knowledge source to get started.</p>';
      ["setup-jobs","training-jobs"].forEach(id=>{const el=$("#"+id);if(el){el.innerHTML=html;el.querySelectorAll("[data-job-retry]").forEach(b=>b.onclick=async()=>{b.disabled=true;try{await api("POST","/api/bots/"+BOT_ID+"/jobs/"+b.dataset.jobRetry+"/retry",{});loadJobs();}catch(err){toast(err.message,true);b.disabled=false;}});}});
      if(jobsSignature && signature!==jobsSignature) loadTraining();
      jobsSignature=signature;
    } catch(err) {toast(err.message,true);}
  }
  async function openSourceEditor(id) {
    try {
      const data=await api("GET","/api/bots/"+BOT_ID+"/sources/"+id);
      editingSource=data.source;
      $("#edit-source-title").value=data.source.title;
      $("#edit-source-content").value=data.source.content;
      $("#source-versions").innerHTML=data.versions.map(v=>'<div class="job-line"><span>'+esc(v.title)+' / '+fmtDate(v.created_at)+'</span><button class="btn btn-small" data-restore-version="'+v.id+'">Restore</button></div>').join("") || '<p class="muted small">Previous content is saved after the first successful update.</p>';
      $("#source-versions").querySelectorAll("[data-restore-version]").forEach(b=>b.onclick=async()=>{if(!confirm("Restore this version? Current content will be kept in history."))return;try{await api("POST","/api/bots/"+BOT_ID+"/jobs",{source_id:editingSource.id,kind:"restore",version_id:Number(b.dataset.restoreVersion)});$("#source-editor").close();toast("Restore queued");loadJobs();}catch(err){toast(err.message,true);}});
      $("#source-editor").showModal();
    }catch(err){toast(err.message,true);}
  }
  function bindSourceEditor() {
    $("#close-source-editor").onclick=()=>$("#source-editor").close();
    $("#source-editor-form").onsubmit=async e=>{e.preventDefault();$("#save-source").disabled=true;try{await api("POST","/api/bots/"+BOT_ID+"/jobs",{source_id:editingSource.id,kind:"index",title:$("#edit-source-title").value,content:$("#edit-source-content").value});$("#source-editor").close();toast("Update queued. Current knowledge stays available.");loadJobs();}catch(err){toast(err.message,true);}finally{$("#save-source").disabled=false;}};
  }

  /* ---- shared save / init ---- */

  async function reloadBot() {
    const data = await api("GET", "/api/bots/" + BOT_ID);
    bot = data.bot;
    $("#bot-title").textContent = bot.name;
  }

  const COLLECTORS = {
    behaviour: collectBehaviour,
    guardrails: collectGuardrails,
    appearance: collectAppearance,
    leadcapture: collectLeadCapture,
  };

  async function initBuilder() {
    bindModals();
    try {
      await reloadBot();
    } catch (err) {
      toast(err.message, true);
      return;
    }
    fillBehaviour();
    fillGuardrails();
    fillAppearance();
    fillLeadCapture();
    fillApiKey();
    fillEmbed();

    $$(".tab-btn").forEach((btn) =>
      btn.addEventListener("click", () => switchTab(btn.dataset.tab)));
    loadOverview();
    bindSourceEditor();
    const hash = window.location.hash.replace("#", "");
    if (/^[a-z]+$/.test(hash) && $('[data-panel="' + hash + '"]')) switchTab(hash);

    $$("[data-save]").forEach((btn) => btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const payload = COLLECTORS[btn.dataset.save]();
        const data = await api("PATCH", "/api/bots/" + BOT_ID, payload);
        bot = data.bot;
        if (btn.dataset.save === "guardrails") updatePassphraseStatus();
        dirtyPanels.delete(btn.dataset.save);
        btn.closest(".tab-panel").querySelectorAll(".unsaved-label").forEach(el=>el.remove());
        loadOverview();
        toast("Changes saved");
      } catch (err) { toast(err.message, true); }
      finally { btn.disabled = false; }
    }));

    $("#tone-grid").addEventListener("click", (e) => {
      const pill = e.target.closest(".tone-pill");
      if (!pill) return;
      $$(".tone-pill").forEach((p) => p.classList.remove("active"));
      pill.classList.add("active");
    });
    $("#f-provider").addEventListener("change", refreshModelOptions);
    $("#f-temperature").addEventListener("input", () => {
      $("#temp-label").textContent = $("#f-temperature").value;
    });
    $("#add-starter").addEventListener("click", () => {
      if ($$("#starter-list .starter-row").length >= 6) { toast("Max 6 starters", true); return; }
      $("#starter-list").appendChild(starterRow(""));
    });
    ["f-theme_color", "f-launcher_text"].forEach((id) =>
      $("#" + id).addEventListener("input", updateLauncherPreview));
    $("#add-lead-field").addEventListener("click", () => {
      if ($$("#lead-fields-list .lead-field-row").length >= 8) { toast("Max 8 fields", true); return; }
      $("#lead-fields-list").appendChild(leadFieldRow({ type: "text" }));
    });
    $("#delete-bot-btn").addEventListener("click", async () => {
      if (!confirm('Delete "' + bot.name + '" and ALL its training, leads and conversations? ' +
        "This cannot be undone.")) return;
      await api("DELETE", "/api/bots/" + BOT_ID);
      window.location.href = "/";
    });

    document.querySelectorAll(".tab-panel input, .tab-panel textarea, .tab-panel select").forEach(el => el.addEventListener("input", () => {
      const panel = el.closest(".tab-panel");
      if (!COLLECTORS[panel.dataset.panel]) return;
      dirtyPanels.add(panel.dataset.panel);
      const row = panel.querySelector(".save-row");
      if (row && !row.querySelector(".unsaved-label")) { const label=document.createElement("span");label.className="unsaved-label";label.textContent="Unsaved changes";row.prepend(label); }
    }));
    window.addEventListener("beforeunload", e => {if(dirtyPanels.size) {e.preventDefault();e.returnValue="";}});
    document.querySelectorAll("[data-setup-tab]").forEach(b=>b.onclick=()=>switchTab(b.dataset.setupTab));
    setInterval(()=>{ if (!document.hidden) loadJobs(); },5000);
    bindTraining();
    bindFaq();
    bindRedirects();
    bindApiKey();
    bindPassphrase();
    bindEmbed();
    bindConversations();
    loadTraining();
    loadLeads();
  }

  if (document.body.dataset.page === "dashboard") initDashboard();
  else if (document.body.dataset.page === "builder") initBuilder();
})();
