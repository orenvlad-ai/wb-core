# Responses API payloads

The four `*.responses.template.json` files are credential-free request-body templates generated from the frozen v1.4.2 prompts and output contracts. Regenerate them with `npm run generate:payloads`; generation does not use the network.

Mappings:

- `SAFETY_IDENTIFIER`: `wb_` plus the first 32 hexadecimal characters of SHA-256(review_id).
- `CLASSIFIER_DYNAMIC_CONTEXT_JSON`: `review_input`, the selected line context, and optional exact SKU match. It is serialized after the cache breakpoint.
- `WRITER_REQUEST_JSON`, `VALIDATOR_REQUEST_JSON`, `REWRITE_REQUEST_JSON`: the corresponding frozen request contract serialized inside `<request_json>` tags.

Classifier image items are appended by `scripts/payload_builder.mjs` after the dynamic text as `{ "type": "input_image", "image_url": "...", "detail": "high" }`. Every successfully downloaded photo and extracted video frame is included; failed or unprocessed media is not presented as analyzed.

Classifier, writer, and validator use one stable `s0` cache shard and an explicit breakpoint. Rewrite intentionally has neither a breakpoint nor `prompt_cache_key`, matching frozen decision P11; `prompt_cache_options` remains present in the common body shape. Authorization is supplied only by the Make connection and never stored in these files.
