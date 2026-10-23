// Dedicated local boundary: no live provider runner or network capability.
// Python verifies the immutable bundle through the existing boundary first.
import {runJob} from "./orchestrator.mjs";
import {MemoryStore} from "./memory_store.mjs";
import {isUnspecifiedMissingKit, MISSING_KIT_POLICY} from "./missing_kit_policy.mjs";
import {EVALUATION_SIGNATURE, PROMPT_BUNDLE_VERSION} from "./constants.mjs";
const identity = {boundary_version: "wb_autoanswers_node_boundary_v1",
  bundle_version: PROMPT_BUNDLE_VERSION, evaluation_signature: EVALUATION_SIGNATURE};
try {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const envelope = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (envelope.boundary_version !== identity.boundary_version || envelope.operation !== "run"
    || envelope.local_policy !== MISSING_KIT_POLICY
    || !isUnspecifiedMissingKit(envelope.raw_input || {}, envelope.raw_input?.media)) {
    throw Object.assign(new Error("local policy scope changed before execution"), {code: "LOCAL_POLICY_SCOPE_MISMATCH"});
  }
  const store = new MemoryStore();
  const pipeline = await runJob(envelope.raw_input, {store, roleRunner: () => {
    throw Object.assign(new Error("local policy cannot call a provider"), {code: "LOCAL_POLICY_PROVIDER_FORBIDDEN"});
  }});
  if (pipeline.model_calls_this_run !== 0 || pipeline.estimated_cost_usd !== 0) throw new Error("LOCAL_POLICY_PROVIDER_USAGE");
  process.stdout.write(JSON.stringify({...identity, ok: true, data: {
    processing_key: envelope.processing_key, pipeline, audit: await store.getAudit(pipeline.idempotency_key)}}));
} catch (error) {
  process.stdout.write(JSON.stringify({...identity, ok: false, error: {code: error.code || "LOCAL_POLICY_ERROR", message: String(error.message)}}));
  process.exitCode = 1;
}
