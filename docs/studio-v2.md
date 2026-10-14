# Studio v2 implementation notes

This release keeps the existing Flask application, bot records, embed keys, provider configuration, and widget script URL. The dashboard now starts with a workspace overview and shared inbox.

## What changed

- Workspace overview with searchable bots and configuration checks.
- Guided bot creation with an optional background website import.
- Shared inbox with search, bot/status filters, unread counts, assignments, human replies, resolution, and return-to-bot controls.
- Durable notification history, automatic retries, manual retry after terminal failure, and optional webhook signatures.
- Background training, source editing, previous versions, and restore.
- Atomic training replacement that preserves active content and embeddings on failure.
- Signed visitor sessions, conversation restoration, a New chat action, and duplicate-turn protection.
- Revocable, expiring private-bot access tokens; private history, updates, and leads use the same access gate.
- Shared rate limits, login throttling, dashboard CSRF protection, safe login redirects, and production credential checks.
- Crawler connections pinned to validated addresses, checked redirects, download limits, and no private-network bypass from mock AI mode.
- Lead stages and deletion. Conversation conversion uses unique converted conversations. Retrieval coverage is named accurately.
- Docker build excludes local secrets, database files, and development environments.

## Design decisions

The interface is a working console for an operator managing several client bots. The main view uses a bot directory, setup progress, and a waiting-conversation count. The inbox uses a list beside the current transcript.

Palette: navy #19315B for text, blue #2457D6 for actions, white #FFFFFF for work areas, pale blue #F2F5FA for the page, slate #65758B for secondary text, and green #178068 for completed setup. Avenir Next with system fallbacks keeps the interface readable without downloading fonts. Existing bot theme colors remain available for public widgets.

The source editor keeps long text in a focused dialog. Setup steps are numbered because they describe a sequence. Unnecessary icons were removed from builder navigation.

## Storage and migration

New tables: source_versions, work_jobs, notification_outbox, rate_limits, and chat_turns. Existing tables receive additive columns for revisions, source attempts, inbox state, lead stage, and protected visitor sessions. Existing records and embed keys remain intact.

The first update to a legacy source preserves its original content in version history even when it has no prior revision number. Source content, chunks, the job completion state, and resolved answer gaps commit together.

The migration is idempotent. Test migrations against a database backup before production deployment. Never replace production with the local development database.

## Worker behavior

The application starts separate training and notification worker threads in each web process. SQLite claims and leases coordinate them across processes. Training leases expire after 20 minutes; notification leases expire after 120 seconds. A worker that loses its training lease cannot publish its result.

Notifications use at-least-once delivery. Receivers must deduplicate on X-Studio-Event-ID or payload.event_id. HTTP 3xx and error statuses are failures. Six failed attempts move a delivery to failed, where the dashboard offers retry. A process failure after the remote service accepted delivery can cause a duplicate on retry.

Keep RUN_SCHEDULER enabled in production. Setting it to 0 disables the scheduler and both work loops. Jobs remain stored until a worker starts. For larger traffic volumes, these modules can move into separate worker processes without changing the job tables or API.

## Access and compatibility

Existing embed snippets continue using /widget.js. New widget sessions use signed conversation credentials. Legacy clients can finish existing conversations using their secret session ID; newly protected conversations reject requests without the signed credential. History always requires the signed credential. Legacy widget sessions start a fresh conversation when upgraded because the new client cannot prove ownership of an old ID.

Old private-bot unlock tokens are deliberately invalidated. Visitors must enter the passphrase again. Changing the passphrase also invalidates current tokens. Access expires after ACCESS_TOKEN_TTL seconds, default 43200.

Existing authenticated dashboard sessions receive a CSRF token when a page is loaded. API clients that use an admin session must send X-CSRF-Token on mutations. The public widget API does not use admin cookies.

PRODUCTION=1 requires an admin password and a non-default session secret. Docker Compose explicitly enables production mode. TRUSTED_PROXY_HOPS defaults to 0. Configure the exact trusted proxy count only after checking how the deployed proxy supplies client addresses; untrusted forwarded headers are ignored.

Provider-key encryption still depends on SECRET_KEY. Do not rotate it without a separate key migration.

## Verification

The suite includes failure preservation, invalid embedding batches, stale revision and worker rejection, private-token rotation and expiry, protected transcripts, duplicate streaming/non-streaming turns, overlapping turns, background imports, rollback, retry behavior, atomic lead notifications, shared limits, authenticated mutations, migration compatibility, and crawler redirect/download restrictions.

Browser checks cover bot creation, source editing through the background worker, saved history, conversation assignment and replies, visitor transcript restoration after reload, narrow-screen fit, and browser console errors. Provider responses in the browser preview are simulated. Real provider quality and real notification destinations require a staging check before production rollout.

## Deployment and rollback

1. Identify the running production revision and take a SQLite backup using its backup API. Keep environment secrets separate.
2. Run the full test suite. Build the container with the existing production data volume and environment file.
3. Confirm admin sign-in, private-bot unlock, one known training question, a lead delivery, and a human reply using clearly labeled test records.
4. Check notification history and source job status. Verify client-IP handling before increasing public traffic.
5. Keep the previous image and database backup. Coordinate rollback with any new records written after deployment; restoring an old database would discard those new records. Older code may tolerate additive schema changes, but do not assume it can safely process new queued jobs or tokens.

This release does not introduce client accounts, subscriptions, direct calendar booking, a native GHL OAuth connector, or a saved evaluation dashboard. GHL and other systems can receive the existing webhook payload through the reliable delivery queue. Existing provider integrations and the grounding evaluation script remain available.
