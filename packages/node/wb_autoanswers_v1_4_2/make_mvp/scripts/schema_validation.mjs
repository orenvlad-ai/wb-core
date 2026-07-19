import {readFile} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";
import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import {ROLE_OUTPUT_SCHEMA, ROLE_REQUEST_SCHEMA} from "./constants.mjs";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const bundleRoot = path.join(projectRoot, "frozen_bundle");

const contractFiles = [
  "review_input.schema.json",
  "classification.schema.json",
  "draft_reply.schema.json",
  "validation.schema.json",
  "pipeline_result.schema.json"
];
const requestFiles = [
  "common_prompt_context.schema.json",
  "classifier_request.schema.json",
  "writer_request.schema.json",
  "validator_request.schema.json",
  "rewrite_request.schema.json"
];

async function loadJson(directory, filename) {
  return JSON.parse(await readFile(path.join(bundleRoot, directory, filename), "utf8"));
}

let registryPromise;
export function loadSchemaRegistry() {
  registryPromise ||= (async () => {
    const ajv = new Ajv2020({allErrors: true, strict: false});
    addFormats(ajv);
    const contracts = Object.fromEntries(await Promise.all(contractFiles.map(async (name) => [name, await loadJson("contracts", name)])));
    const requests = Object.fromEntries(await Promise.all(requestFiles.map(async (name) => [name, await loadJson("schemas", name)])));
    for (const schema of [...Object.values(contracts), ...Object.values(requests)]) ajv.addSchema(schema);
    return {ajv, contracts, requests};
  })();
  return registryPromise;
}

function validationError(label, validate) {
  const error = new Error(`JSON_SCHEMA_INVALID:${label}:${validate.errors?.map((item) => `${item.instancePath || "/"} ${item.message}`).join("; ")}`);
  error.validation_errors = structuredClone(validate.errors || []);
  return error;
}

export async function assertSchema(schema, value, label = schema.$id || schema.title || "value") {
  const {ajv} = await loadSchemaRegistry();
  const validate = ajv.getSchema(schema.$id) || ajv.compile(schema);
  if (!validate(value)) throw validationError(label, validate);
  return value;
}

export async function assertContract(filename, value) {
  const {contracts} = await loadSchemaRegistry();
  const schema = contracts[filename];
  if (!schema) throw new Error(`UNKNOWN_CONTRACT_SCHEMA:${filename}`);
  return assertSchema(schema, value, filename);
}

export async function assertRoleRequest(role, value) {
  const {requests} = await loadSchemaRegistry();
  const filename = ROLE_REQUEST_SCHEMA[role];
  if (!filename) throw new Error(`UNKNOWN_ROLE:${role}`);
  return assertSchema(requests[filename], value, filename);
}

export async function assertRoleOutput(role, value) {
  const {contracts} = await loadSchemaRegistry();
  const filename = ROLE_OUTPUT_SCHEMA[role];
  if (!filename) throw new Error(`UNKNOWN_ROLE:${role}`);
  return assertSchema(contracts[filename], value, filename);
}

export async function compileAllSchemas() {
  const {ajv, contracts, requests} = await loadSchemaRegistry();
  return [...Object.values(contracts), ...Object.values(requests)].map((schema) => {
    const validate = ajv.getSchema(schema.$id);
    if (!validate) throw new Error(`SCHEMA_NOT_COMPILED:${schema.$id}`);
    return schema.$id;
  });
}

export {bundleRoot};
