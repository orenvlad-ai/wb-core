# MAX webhook durable inbox

## Contract

The hosted WebCore process can start a loopback listener on `127.0.0.1:8783`.
Nginx forwards only `POST /v1/max/webhook` to it; its request queue is independent
of slow WebCore requests. It uses no Mac, browser, bot API token, outbound API,
model, messages or business database.

Authentication requires one `X-Max-Bot-Api-Secret` header, checked in constant
time before reading the body. Limits: 1 MiB UTF-8 JSON, eight-second total body
read and two-second SQLite busy timeout. Ambiguous framing, duplicate JSON keys,
invalid envelopes and encodings fail without a receipt. Application logs contain
only fixed failure messages. Nginx access logging is off for this route; ordinary
error logging remains enabled. Never put source text/secrets in URLs or proxy logs.

Only `message_created`, `message_edited` and `message_removed` from the exact
configured group allowlist are retained. Create/edit scope uses the current
`message.recipient.chat_id` with `chat_type=chat`, never forwarded/replied-to IDs.
Removal uses root `chat_id` and `message_id`. Authenticated unsupported, private,
channel and foreign events receive 200 without a database write.

Accepted events append the first raw JSON body, configured bot ID, group/message
IDs, event timestamp and separate receipt time to `max_inbox.sqlite3`. Bot ID is
deployment provenance, not an identity asserted by Update. Verify its association
with the secret during subscription setup. A SHA-256 key covers bot ID and the
canonical complete Update: JSON formatting retries deduplicate; different event
types, timestamps or edit payloads remain distinct. MAX has no global Update ID.

SQLite uses FULL synchronous mode and a rollback journal. Accepted events receive
200 only after commit or a confirmed existing duplicate; storage failure returns
503. Lost responses can retry safely. Edits/deletions retain earlier receipts;
there is no business processing, history backfill, retention deletion or worker.

## Private configuration

Keep real IDs, secrets and receipts outside this public repository. Set these in
the existing service environment (`/opt/wb-ai/.env`) without echoing their values
or placing secrets in process arguments:

```text
WB_CORE_MAX_WEBHOOK_ENABLED=1
WB_CORE_MAX_BOT_ID=<verified bot user_id from GET /me>
WB_CORE_MAX_WEBHOOK_CHAT_IDS=<comma-separated explicitly authorized negative group IDs>
WB_CORE_MAX_WEBHOOK_SECRET=<fresh random 32–256 character A-Z/a-z/0-9/_/- secret>
WB_CORE_MAX_INBOX_DIR=/opt/wb-core-runtime/max-inbox
```

These placeholders are not a usable environment file. There is no allowlist
default: IDs must fit signed int64; up to 100 groups are allowed. New groups
require authorization. The receiver needs no bot API token. Subscription clients
keep that token and any scoped TLS trust configuration separately; this code
does not install certificates or alter global trust.

Pre-create the dedicated directory as mode 0700, owned by the service user,
outside the business runtime directory. Existing database files must be regular,
service-owned and mode 0600. Do not symlink the database. Config/health checks
create no files. Missing/exposed storage fails readiness.

Absent/`0` opt-in opens no listener or storage. Invalid enabled configuration or
bind failure logs a fixed error and leaves MAX unavailable without stopping
shared WebCore. Configuration changes require a canonical restart.

## Release and activation

1. Follow the [release contract](../architecture/11_github_release_train.md),
   including the cycle window and ownership. Verify exact target/release, service
   user, private environment, directory, free disk/inodes and port 8783. Preserve
   the prior private environment for rollback. Deployment and MAX subscription
   changes are separate authorized stages.
2. Deploy/restart through the canonical runner; do not edit the tracked unit.
   The managed nginx route sets 1 MiB, request buffering, ten-second body/send
   timeouts and a 25-second upstream read timeout.
3. Verify shared-service health, then run
   `curl --fail --silent http://127.0.0.1:8783/healthz` on the server. Expect 200
   with `configured`. This checks storage permissions and remembers a prior
   failure until a successful durable receipt; it does not prove a future commit.
   Missing/unready MAX fails activation even if WebCore is healthy.
4. External unauthenticated POST must return 401 without login redirect; public
   GET returns 405. Keep `/healthz` loopback-only. Verify trusted TLS, full chain
   and HTTPS port 443.
5. Through a protected API client, read `GET /me` and `GET /subscriptions`.
   Verify bot identity and existing subscriptions before registering the exact
   HTTPS URL, three supported `update_types` and matching secret. No subscription
   is created by this code. If a write response is uncertain, read that URL's
   subscription before considering another submit.
6. Verify receipt using natural group activity or a separately authorized
   synthetic create/edit/delete exercise. Open the inbox with `mode=ro` and
   `PRAGMA query_only=ON`; inspect counts/types/timestamps, never raw payloads in
   shared logs. Confirm exact retries retain one row and no outgoing message or
   business-data change occurs.

Local checks require no credentials, network or production data:

```text
python3 apps/max_webhook_inbox_smoke.py
python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py
python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py
```

## Monitoring and recovery

Monitor loopback readiness, fixed component/nginx errors, free disk/inodes and
receipt freshness relative to known group activity. Quiet groups do not prove
failure; missing history is not zero or complete history. No monitor is created.

On 503 fix the underlying storage issue while preserving receipts. A successful
retry commits once and clears failure state; health alone cannot clear it.
Do not manually acknowledge, delete/recreate the DB or send a synthetic receipt
without authorization. MAX expects 200 within 30 seconds and retries failures;
after eight hours without success it may unsubscribe. Read subscriptions after
an outage before an authorized re-registration. Record any delivery gap;
automatic re-subscription/history recovery is not included.

## Rollback

If activated, remove the exact MAX subscription under an authorized operation;
reconcile uncertain responses by reading subscriptions. Set opt-in to `0` or
restore the prior private environment, then use canonical restart/release
rollback under the owned window. Confirm shared health, absent listener and
absent subscription. The inactive public route may return 502 until the previous
manifest is restored through the same release mechanism.

Preserve the inbox unchanged. Backup, retention deletion and downstream use are
separate work. Never restore older data over newer receipts; record stop/gap times.

API references: [subscriptions](https://dev.max.ru/docs-api/methods/POST/subscriptions)
and [Update schema](https://dev.max.ru/docs-api/objects/Update).
