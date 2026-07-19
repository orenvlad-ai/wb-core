# WB autoanswers — portable Telegram + Make MVP

This package implements the local, credential-free reference workflow for frozen AI bundle v1.4.2. It accepts a review plus Telegram media, applies the deterministic classifier → route guard → writer → validator pipeline, permits at most two rewrite → validator cycles, uses only an approved same-route fallback, stores the audit trail, and returns the final public text to the manager.

It never publishes to Wildberries. There is no WB connection, WB write module, deploy command, or built-in network client.

## Frozen identity

- bundle: `1.4.2`
- doctrine: `1.0`
- model/profile: `gpt-5.6-terra`, medium reasoning
- evaluation signature: `sha256:5f305d7eceba13e90b5b51f2a774b6ce71c24b9b2af07cc2637210f2e25b30da`
- holdout adjudication: route `96.10%`, primary macro `93.65%`, false seller_chat `0`, hard gates exact

`frozen_bundle/` is a byte-for-byte snapshot of the required prompts, contracts, request schemas, guards, model profile, pricing profile, and context builder. Do not edit it. `bundle_manifest.json` records its hashes.

## Free local verification

Requirements: Node.js 20 or newer. From this directory:

```bash
npm ci
npm run verify
```

From the parent project, dependencies already installed there may also satisfy the local test runner:

```bash
npm test --prefix make_mvp
```

No verification command calls OpenAI, Telegram, Make, or Wildberries.

## Telegram input protocol

Use a deterministic three-step manager flow so albums cannot race:

1. `/review {JSON}` opens a collecting session. Required JSON fields are `review_id`, `review_version`, and integer `rating` 1–5. Optional fields are `text`, `pros`, `cons`, `wb_tags`, `nm_id`, `seller_article`, `product_name`, and history.
2. Send zero or more photos/files to the same bot chat. They are attached to the open session by Telegram `file_id`; tokens and temporary download URLs are never stored.
3. `/process` atomically locks that session and starts the worker. A duplicate `/process` resolves through the idempotency key and does not repeat role calls or allocate another case code.

For a video, the MVP records it and sets `video_present_unprocessed` unless frames are already supplied as image attachments. This prevents false claims that the video was viewed. Photo bytes are passed as image inputs after the classifier cache breakpoint.

## Make assembly

There is deliberately no `blueprint.template.json`: an unexported synthetic blueprint would pretend to be importable without a verified Make runtime format. Build the two scenarios and five data stores exactly from:

- `blueprint_build_map.json` — module-by-module operations, filters, mappings, branches, retries, and error paths;
- `architecture.md` — readable topology and invariants;
- `data_stores.json` — field-level data-store definitions;
- `connections.example.json` — connection/environment names only;
- `payloads/` — complete strict Responses API body templates.

When the scenario is assembled in Make, paste credentials only into Make connections. Never place a token in a variable, blueprint, log, data store, exported JSON, or ZIP.

## Manual connection steps

1. Create the Make data stores from `data_stores.json`.
2. Create `telegram_bot_mvp` and point intake/delivery modules to the intended manager chat allowlist.
3. Create `openai_responses_mvp`; its secret value supplies `OPENAI_API_KEY` to the HTTP Authorization header at runtime only.
4. Assemble and dry-run the scenarios from `blueprint_build_map.json` using the fixtures before enabling scheduling.
5. Confirm that Telegram receives only the final public text (or a technical/skip notice) and that no WB module exists.

## Safety boundaries

- `seller_chat` contains one orchestrator-generated case code and no public request or suggestion to prepare photos, video, screenshots, labels, evidence, or other materials.
- Classification and final route are immutable during rewrite.
- Money, replacement, compensation, return approval, or a WB decision are never promised.
- Unknown SKU is `line=unknown`, has no `sku_match`, and receives no line-specific fact.
- Empty 5-star content stops before OpenAI.
- All role inputs/outputs and the pipeline result pass the frozen JSON Schemas plus deterministic guards.
- Telegram receives text for manual copying; Wildberries receives nothing.
