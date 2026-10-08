// Read-only local policy boundary. No network, model, store or publication calls.
import {returnQuestionReplacement} from "./return_question_policy.mjs";
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
try { process.stdout.write(JSON.stringify(returnQuestionReplacement(JSON.parse(Buffer.concat(chunks).toString("utf8"))))); }
catch (error) { process.stderr.write(String(error.message)); process.exitCode = 1; }
