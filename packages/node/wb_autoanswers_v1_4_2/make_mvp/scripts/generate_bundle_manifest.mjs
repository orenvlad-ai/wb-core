import {createHash} from "node:crypto";
import {readdir, readFile, writeFile} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {EVALUATION_SIGNATURE, PROMPT_BUNDLE_VERSION} from "./constants.mjs";

const mvpRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const frozenRoot = path.join(mvpRoot, "frozen_bundle");

async function filesUnder(directory) {
  const entries = await readdir(directory, {withFileTypes: true});
  return (await Promise.all(entries.map(async (entry) => {
    const target = path.join(directory, entry.name);
    return entry.isDirectory() ? filesUnder(target) : [target];
  }))).flat();
}

const files = (await filesUnder(frozenRoot)).sort();
const artifacts = {};
for (const file of files) {
  const relative = path.relative(frozenRoot, file).split(path.sep).join("/");
  artifacts[relative] = `sha256:${createHash("sha256").update(await readFile(file)).digest("hex")}`;
}

const manifest = {
  schema_version: "1.0.0",
  bundle_version: PROMPT_BUNDLE_VERSION,
  evaluation_signature: EVALUATION_SIGNATURE,
  artifact_count: files.length,
  artifacts
};
await writeFile(path.join(mvpRoot, "bundle_manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
