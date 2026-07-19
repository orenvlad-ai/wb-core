import {createHash} from "node:crypto";
import {mkdir, readFile, writeFile} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {schemaForStructuredOutput} from "../frozen_bundle/tools/build_context.mjs";
import {MODEL_ID, ROLE_MAX_OUTPUT_TOKENS, ROLE_OUTPUT_SCHEMA} from "./constants.mjs";

const mvpRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const bundleRoot = path.join(mvpRoot, "frozen_bundle");
const outputRoot = path.join(mvpRoot, "payloads");
const roles = ["classifier", "writer", "validator", "rewrite"];

const tagged = (tag, value) => `<${tag}>\n${typeof value === "string" ? value : JSON.stringify(value)}\n</${tag}>`;
const sha = (value) => createHash("sha256").update(value).digest("hex");

async function json(relativePath) {
  return JSON.parse(await readFile(path.join(bundleRoot, relativePath), "utf8"));
}

async function classifierStaticContext() {
  const [taxonomy_context, issue_playbook, route_policy, product] = await Promise.all([
    json("contracts/issue_taxonomy.json"),
    json("contracts/issue_playbook.json"),
    json("contracts/route_policy.json"),
    json("contracts/product_context.json")
  ]);
  const {line_contexts, sku_index, ...universal_product_context} = product;
  return {
    request_schema_version: "1.0.0",
    taxonomy_context,
    issue_playbook,
    route_policy,
    universal_product_context
  };
}

function staticNote(role) {
  return {
    cache_protocol_version: "1.1.0",
    role,
    note: "The role instructions rendered before this boundary are static. The request JSON follows after the boundary."
  };
}

async function templateFor(role) {
  const [instructions, schema] = await Promise.all([
    readFile(path.join(bundleRoot, "prompts", `${role}.system.md`), "utf8"),
    json(`contracts/${ROLE_OUTPUT_SCHEMA[role]}`)
  ]);
  const cacheable = role !== "rewrite";
  const staticPayload = role === "classifier" ? await classifierStaticContext() : staticNote(role);
  const staticText = cacheable ? tagged("static_context_json", staticPayload) : null;
  const dynamicName = role === "classifier" ? "CLASSIFIER_DYNAMIC_CONTEXT_JSON" : `${role.toUpperCase()}_REQUEST_JSON`;
  const input = cacheable
    ? [{
        type: "message",
        role: "user",
        content: [
          {type: "input_text", text: staticText, prompt_cache_breakpoint: {mode: "explicit"}},
          {type: "input_text", text: tagged("request_json", `{{${dynamicName}}}`)}
        ]
      }]
    : tagged("request_json", "{{REWRITE_REQUEST_JSON}}");
  const body = {
    model: MODEL_ID,
    store: false,
    reasoning: {effort: "medium"},
    safety_identifier: "{{SAFETY_IDENTIFIER}}",
    instructions,
    input,
    prompt_cache_options: {mode: "explicit", ttl: "30m"},
    max_output_tokens: ROLE_MAX_OUTPUT_TOKENS[role],
    text: {
      format: {
        type: "json_schema",
        name: `wb_${role}_v1`,
        strict: true,
        schema: schemaForStructuredOutput(schema)
      }
    }
  };
  if (cacheable) {
    body.prompt_cache_key = [
      "wb13",
      "terra",
      sha(MODEL_ID).slice(0, 8),
      role,
      sha(instructions).slice(0, 8),
      sha(staticText).slice(0, 8),
      "s0"
    ].join(":");
  }
  return body;
}

await mkdir(outputRoot, {recursive: true});
for (const role of roles) {
  const target = path.join(outputRoot, `${role}.responses.template.json`);
  await writeFile(target, `${JSON.stringify(await templateFor(role), null, 2)}\n`, "utf8");
}
