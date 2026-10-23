// Pure local boundary: no provider, database or publication capabilities.
import {missingKitReplacement} from "./missing_kit_policy.mjs";
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
try { process.stdout.write(JSON.stringify(missingKitReplacement(JSON.parse(Buffer.concat(chunks).toString("utf8"))))); }
catch (error) { process.stderr.write(String(error.message)); process.exitCode = 1; }
