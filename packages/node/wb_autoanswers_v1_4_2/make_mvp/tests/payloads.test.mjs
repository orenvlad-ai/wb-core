import assert from "node:assert/strict";
import {readFile, readdir} from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import {buildResponsesPayload} from "../scripts/payload_builder.mjs";
import {normalizeTelegramInput} from "../scripts/normalizer.mjs";
import {buildClassifierRequest, readJson} from "../frozen_bundle/tools/build_context.mjs";
import {mvpRoot} from "./fixture_runtime.mjs";

const payloadRoot = path.join(mvpRoot, "payloads");

for (const role of ["classifier", "writer", "validator", "rewrite"]) {
  test(`${role} request template uses strict Structured Output`, async () => {
    const payload = JSON.parse(await readFile(path.join(payloadRoot, `${role}.responses.template.json`), "utf8"));
    assert.equal(payload.model, "gpt-5.6-terra");
    assert.equal(payload.store, false);
    assert.deepEqual(payload.reasoning, {effort: "medium"});
    assert.equal(payload.text.format.type, "json_schema");
    assert.equal(payload.text.format.strict, true);
    assert.equal(payload.text.format.schema.additionalProperties, false);
  });
}

test("cacheable roles put dynamic request data after the explicit breakpoint", async () => {
  for (const role of ["classifier", "writer", "validator"]) {
    const payload = JSON.parse(await readFile(path.join(payloadRoot, `${role}.responses.template.json`), "utf8"));
    const content = payload.input[0].content;
    assert.deepEqual(content[0].prompt_cache_breakpoint, {mode: "explicit"});
    assert.doesNotMatch(content[0].text, /\{\{.*REQUEST_JSON\}\}/u);
    assert.match(content[1].text, /\{\{.*(?:REQUEST|CONTEXT)_JSON\}\}/u);
    assert.match(payload.prompt_cache_key, /:s0$/u);
    assert.deepEqual(payload.prompt_cache_options, {mode: "explicit", ttl: "30m"});
  }
});

test("rewrite is intentionally uncached and has no cache breakpoint or cache key", async () => {
  const payload = JSON.parse(await readFile(path.join(payloadRoot, "rewrite.responses.template.json"), "utf8"));
  assert.equal("prompt_cache_key" in payload, false);
  assert.doesNotMatch(JSON.stringify(payload.input), /prompt_cache_breakpoint/u);
  assert.match(payload.input, /REWRITE_REQUEST_JSON/u);
});

test("classifier appends every downloaded photo after the dynamic text and cache breakpoint", async () => {
  const productContext = await readJson("contracts/product_context.json");
  const reviewInput = normalizeTelegramInput({
    ingestion_id: "payload-media",
    review_id: "payload-media",
    review_version: "1",
    rating: 2,
    text: "На фото видна проблема",
    seller_article: "(Anti-Spy) iPhone 14 Pro",
    media: {
      photos: [
        {full_size_url: "https://example.invalid/one.jpg", fetch_status: "downloaded"},
        {full_size_url: "https://example.invalid/two.jpg", fetch_status: "downloaded"}
      ],
      video: {present: false}
    }
  }, productContext);
  const request = await buildClassifierRequest(reviewInput);
  const payload = await buildResponsesPayload("classifier", request, "payload-media");
  const content = payload.input[0].content;
  assert.deepEqual(content.slice(-2).map((item) => item.type), ["input_image", "input_image"]);
  assert.ok(content.findIndex((item) => item.prompt_cache_breakpoint) < content.findIndex((item) => item.type === "input_image"));
});

test("runtime scripts contain no network client and package contains no credential value", async () => {
  const scriptFiles = (await readdir(path.join(mvpRoot, "scripts"))).filter((name) => name.endsWith(".mjs"));
  const scriptText = (await Promise.all(scriptFiles.map((name) => readFile(path.join(mvpRoot, "scripts", name), "utf8")))).join("\n");
  assert.doesNotMatch(scriptText, /\bfetch\s*\(/u);

  async function collect(directory) {
    const entries = await readdir(directory, {withFileTypes: true});
    return (await Promise.all(entries.map(async (entry) => {
      const target = path.join(directory, entry.name);
      return entry.isDirectory() ? collect(target) : [target];
    }))).flat();
  }
  const files = (await collect(mvpRoot)).filter((name) => !name.endsWith(".zip"));
  const text = (await Promise.all(files.map(async (name) => {
    try { return await readFile(name, "utf8"); } catch { return ""; }
  }))).join("\n");
  assert.doesNotMatch(text, /\bsk-[A-Za-z0-9_-]{20,}\b/u);
  assert.doesNotMatch(text, /Bearer\s+(?!\{\{|\[)[A-Za-z0-9._-]{20,}/u);
});
