process.stdout.write(JSON.stringify({
  boundary_version: "wb_autoanswers_node_boundary_v1",
  bundle_version: "1.4.2",
  evaluation_signature: "sha256:5f305d7eceba13e90b5b51f2a774b6ce71c24b9b2af07cc2637210f2e25b30da",
  ok: false,
  error: {
    code: "OPENAI_OUTPUT_NOT_JSON",
    message: "fixture partial failure",
    partial_usage: {classifier: {input_tokens: 100, output_tokens: 5}},
    partial_cost_usd: 0.03125,
    partial_role_calls: 1
  }
}));
process.exitCode = 1;
