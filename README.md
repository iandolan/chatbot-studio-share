# Chatbot Studio

The current dashboard includes a shared inbox, background training, source history, reliable notifications, and persistent visitor conversations. See [Studio v2 implementation notes](docs/studio-v2.md) for migration, compatibility, worker configuration, verification, and deployment details.


Multi-bot platform: build AI assistants in a dashboard, drop each one onto any website with a
single `<script>` tag. Flask + SQLite, OpenAI/Anthropic/Gemini/OpenRouter via a provider-agnostic LLM layer,
**streaming replies**, RAG over your own content (paste / Q&A / crawl / **PDF·DOCX upload**,
with **scheduled re-crawls**), lead capture, intent-based redirects, **human handoff** with live
replies from the dashboard, per-bot **analytics**, and an **FAQ miner** that turns what visitors
actually ask into approved training Q&As plus a schema.org website-FAQ export.

## Quick start

```bash
cd chatbot-studio
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # optional — runs in mock AI mode with zero keys
python app.py               # http://127.0.0.1:5090
```

**Mock AI mode:** with no provider key in `.env`, the app auto-runs fully offline — deterministic
embeddings, canned replies that echo your training data, and keyword-triggered redirects/lead
forms. Every screen and the whole widget pipeline works, so you can demo or develop without
spending a cent. Add `OPENAI_API_KEY` to go live.

## How it fits together

| Piece | Where |
|---|---|
| Provider-agnostic LLM layer, streaming + non-streaming (OpenAI / Anthropic / Gemini / OpenRouter + mock) | `server/llm.py` |
| Global daily LLM-call cap (runaway-spend protection) | `server/budget.py` |
| RAG: chunking, embeddings, cosine retrieval in SQLite | `server/rag.py` |
| Turn engine: prompt assembly, SSE streaming, ACTION protocol (redirect / lead form / handoff) | `server/chat.py` |
| Training crawler (same-domain BFS, SSRF-guarded) | `server/crawler.py` |
| PDF / DOCX / TXT / MD extraction for uploads | `server/extract.py` |
| Scheduled re-crawls (daemon thread, optimistic claims) | `server/scheduler.py` |
| FAQ miner: question extraction, clustering, weak-answer flags, FAQ export | `server/faq_miner.py` |
| Bot presets (starter-config library: legal intake, local service, internal KB) | `server/presets.py` |
| Widget API (CORS, rate limited, SSE stream, live-update polling, KB passphrase gate) | `server/routes_public.py` |
| Dashboard API (bots, sources, leads, transcripts, analytics, human replies) | `server/routes_admin.py` |
| Lead + handoff webhooks, SMTP alerts | `server/notify.py` |
| Embeddable Shadow-DOM widget (streams replies, shows live team messages) | `static/widget.js` |

**How actions work:** the model writes a plain-text reply and may end with one
`ACTION: {...}` line (redirect / lead_form / handoff). The streaming splitter withholds that
line from the visitor, validates it server-side (redirect URLs must be on the bot's allowlist),
and the widget executes it. **Human handoff:** visitor asks for a person → conversation is
flagged + webhook/email fires → you reply from the Conversations tab → your message appears in
their widget within seconds (the widget polls) and the AI pauses until you hand back.

## FAQ miner — "What people ask"

Each bot has a **What people ask** tab that closes the deploy → learn → improve loop:

1. **Mine.** Visitor messages are scanned for questions (`?` sentences, interrogative starts,
   imperative asks like "tell me about…"), normalized, and clustered by token overlap (plus an
   embedding-assisted merge pass — mock-safe). Clusters are ranked by frequency + recency.
2. **Flag.** A cluster gets a **bot struggles** badge when the answers looked weak: retrieval
   gaps, canned "I don't know" / fallback replies, very short replies, human handoffs, or the
   visitor leaving right after.
3. **Draft.** *Draft FAQ answer* asks the bot's LLM (grounded in its training). With no API key
   or on failure it deterministically stitches the bot's best existing reply — or leaves a TODO.
4. **Approve.** One click adds the Q&A to the bot's training data (a regular Q&A source, same as
   the Training tab) *and* to the website FAQ list. Approved/dismissed questions stay hidden from
   future mining, including new phrasings (fuzzy suppression).
5. **Export.** Copy the approved FAQs as a paste-ready HTML block with `FAQPage` schema.org
   JSON-LD (rich results / AI answer engines) or as plain Markdown.

## Presets: start from a ready-made bot

**+ New bot** offers a **preset** picker. A preset is just a bundle of the same editable bot
fields a hand-built bot has (system prompt, tone, lead fields, guardrails, mode), so instantiating
one is a normal bot create with those fields pre-filled. It can never do anything a manual bot
can't. Definitions live in `server/presets.py`; the dashboard reads them from `GET /api/presets`.

- **Law Firm Intake Receptionist** is the productized legal-intake bot. A hardened prompt greets by
  firm name, captures caller name / contact / matter type / urgency, routes by practice area, asks
  qualifying questions, runs a **conflict-check capture** (asks the opposing party's name and flags
  it for a human; the bot never *decides* conflicts), and after hours reassures then captures the
  lead. It ships **hard guardrails**: the bot never gives legal advice, never quotes case
  values/outcomes, never confirms representation or fees, and always frames next steps as *"an
  attorney will follow up."* Those guardrails aren't baked-once prose. They're a config list
  (`bots.guardrails`) re-asserted at the top of **every** turn, and the Guardrails tab explains the
  UPL / bar-discipline reason each one exists.
- **Local Service Lead Capture** is for trades (plumbing, HVAC, roofing): answer basics, capture
  the job plus contact fast, with guardrails against quoting a firm price or arrival time unseen.
- **Internal Knowledge Assistant** is a passphrase-gated internal KB (see below) with citations on.

**Guardrails** are a first-class bot field (Guardrails & mode tab). Any bot, preset or not, can
have a list of refusal topics that `build_system_prompt` injects as a HARD GUARDRAILS block that
overrides everything else in the prompt.

## Knowledge-base mode: internal KB with gated access + citations

Every bot has a **mode** (`bots.mode`): `public` (the embeddable website widget) or `internal_kb`,
an internal knowledge assistant for staff rather than a public embed.

- **Access-gated.** Internal-KB bots sit behind a **shared passphrase** (Guardrails & mode tab,
  Access passphrase). It's stored **hashed** (PBKDF2-HMAC-SHA256, per-hash salt, never recoverable,
  the same write-only spirit as the per-bot API keys). The widget shows a passphrase prompt before
  any chat; a correct passphrase is exchanged (`POST /api/widget/unlock`) for a stable per-bot
  unlock token the widget replays on each message. Chat calls without a valid token are refused. A
  bot in `internal_kb` mode with **no** passphrase set is simply open (owner hasn't locked it yet).
- **Citations.** Turn on **Show source citations** (`bots.cite_sources`, default on for the Internal
  KB preset) and each answer surfaces the retrieval the RAG already did: a **Sources** list under the
  reply, each an expandable source title plus the matched chunk snippet plus similarity score. This
  just exposes `rag.retrieve`'s existing output, with no separate index.
- **Doc-first ingestion.** The existing **Upload a file** training (PDF / DOCX / TXT / MD) is the
  primary way to fill an internal KB; paste / Q&A / crawl still work too.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```

Covers question extraction/clustering/ranking, weak-answer detection, the approve-into-training
flow, JSON-LD export validity, the OpenRouter provider (request construction, model defaults,
embeddings-stay-on-OpenAI), the global daily LLM budget, **preset instantiation + legal guardrail
content**, and **internal-KB mode (passphrase hashing, gated access, citation surfacing)**. All
run offline, with no real API calls.

Per-bot API keys are stored **encrypted (Fernet, derived from `SECRET_KEY`) and write-only** —
the dashboard only ever sees a hint like `sk-…abcd`. Embeddings always use the platform
`OPENAI_API_KEY`, even when a bot chats through Anthropic/Gemini/OpenRouter.

## Providers & OpenRouter

`DEFAULT_PROVIDER` picks the platform-wide provider (`openai | anthropic | gemini |
openrouter`); each bot can override provider/model/key in its Behaviour + API key tabs.

**OpenRouter** (consolidated billing, one key for every model) is a first-class provider:
set `DEFAULT_PROVIDER=openrouter` + `OPENROUTER_API_KEY`, and use prefixed model names such
as `anthropic/claude-haiku-4.5` (the default) or `openai/gpt-4o-mini`. It speaks the OpenAI
chat-completions format, so streaming works exactly as with OpenAI.

> **Embeddings.** A direct `OPENAI_API_KEY` is used for embeddings when present (existing
> vectors were produced there). Without one, embeddings go through OpenRouter's
> `/embeddings` endpoint as `openai/text-embedding-3-small`, so a single OpenRouter key runs
> the whole app.
>
> **Model quirks the code handles for you.** Claude Opus 4.7 and later, Opus 5 and Sonnet 5
> reject `temperature`; the request builders only send it to models that accept it. Sonnet 5
> and Opus 5 think by default, so requests to them ask for low reasoning effort and raise
> `max_tokens` to at least 2048 (thinking and the reply share that cap).

## Runaway-spend cap

`GLOBAL_DAILY_LLM_CALLS` (in `.env`, e.g. `2000`) caps real LLM chat calls per **UTC day
across all bots** — the embedded widgets are public, so this is the platform-wide circuit
breaker on top of the per-bot rate limits. Embeddings and mock mode are never counted; the
check is a single indexed SQLite upsert per message. When the cap is reached, bots degrade
gracefully (they answer with their fallback message, exactly like an outage), the dashboard
shows a **“Daily AI budget reached”** banner with the day's count, and the event is logged.
The counter resets at midnight UTC. `0`/unset = no cap (previous behavior).

## Embedding a bot

From the bot's **Embed** tab:

```html
<script src="https://YOUR-HOST/widget.js" data-bot-key="pk_..." async></script>
```

Paste before `</body>` on any site. The widget renders in an isolated Shadow DOM. Each bot also
gets a built-in test page at `/demo?key=pk_...`.

## Deploying (Docker + Traefik)

```bash
scp -r chatbot-studio you@your-server:/path/to/chatbot-studio
# on the server:
cd /path/to/chatbot-studio
cp .env.example .env   # set SECRET_KEY, ADMIN_PASSWORD, OPENAI_API_KEY, PUBLIC_BASE_URL
docker compose up -d --build
```

`docker-compose.yml` already carries the Traefik/Coolify labels (edit the `Host()` rule) and
persists SQLite in `./data/`. **Set `ADMIN_PASSWORD` before exposing the dashboard.**

## Notes & limits

- SQLite + in-process vector math comfortably handles thousands of chunks per bot; past that,
  move to Postgres + pgvector (the `rag.py` interface is the only thing to swap).
- Rate limits are in-memory per process — run 1 gunicorn worker with threads (default here is 2
  workers; lower to 1 if you want strict limits) or move to Redis for multi-worker.
- The crawler respects a 15-page cap and refuses private/internal IPs (override for local dev
  with `CRAWLER_ALLOW_PRIVATE=1`).
- Dashboard JSON API relies on the session cookie (`SameSite=Lax`); no CSRF tokens yet — keep the
  dashboard on its own origin.

## Roadmap ideas

- **GoHighLevel native push** (contact upsert instead of generic webhook) — deliberately parked
  until the GHL sub-account is set up
- **White-label**: hide branding, custom launcher icon, per-bot domains
- OCR for scanned PDFs; sitemap-driven crawls
- Websocket/live channel instead of polling for team replies
- Postgres + pgvector, Redis rate limiting, multi-user/team accounts
