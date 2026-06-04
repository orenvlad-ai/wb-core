# WB-Core Read-Only MCP Contract

## Purpose

`wb-core-readonly-mcp` is a repo-owned minimal MCP server that gives a ChatGPT Project safe read-only access to a `wb-core` repository checkout.

Supported modes:
- local stdio mode for a user-controlled local checkout;
- remote HTTP mode for a separate managed clone that tracks GitHub `origin/main`.

Primary scenario:
- user asks ChatGPT for a `wb-core` task;
- ChatGPT reads current repo code/docs through MCP;
- ChatGPT searches related implementation areas and contracts;
- ChatGPT prepares a precise prompt for manual execution in Codex CLI;
- user manually runs that prompt in Codex CLI.

DevControl is not part of this flow. The MCP is a repository-reading boundary only, not a lane for Codex execution, production orchestration, deploy, PR management or live mutation.

Current repo implementation:
- policy/service: `packages/application/wb_core_readonly_mcp.py`;
- stdio MCP entrypoint: `apps/wb_core_readonly_mcp.py`;
- HTTP MCP entrypoint: `apps/wb_core_readonly_mcp_http.py`;
- hosted loopback probe: `apps/wb_core_readonly_mcp_hosted_probe.py`;
- targeted local smoke: `apps/wb_core_readonly_mcp_smoke.py`;
- targeted remote-mode smoke: `apps/wb_core_readonly_mcp_remote_smoke.py`;
- targeted hosted-artifacts smoke: `apps/wb_core_readonly_mcp_hosted_artifacts_smoke.py`;
- non-secret example config: `artifacts/wb_core_readonly_mcp/input/config.example.json`.
- non-secret remote example config: `artifacts/wb_core_readonly_mcp/input/remote.config.example.json`.
- hosted service artifacts: `artifacts/wb_core_readonly_mcp/systemd/`, `artifacts/wb_core_readonly_mcp/env/`, `artifacts/wb_core_readonly_mcp/nginx/`, `artifacts/wb_core_readonly_mcp/bin/`.

## Explicit Non-Goals

The MCP must not provide:
- writes to repo files or any other filesystem path;
- `git commit`, `git push`, merge, rebase or branch mutation;
- PR creation, PR update, review submission or GitHub write actions;
- deploy, publish, service restart or runtime rollout;
- SSH access, live runtime mutation, remote shell execution or remote file sync;
- reads of secrets, env files, cookies, browser sessions or storage-state files;
- DevControl production lane access or DevControl-backed execution;
- product-plane routes on `api.selleros.pro` or any other production runtime;
- unauthenticated public internet exposure.

The server is intentionally unable to "just run the task". Its output is repo context and prompt material for a human-controlled Codex CLI run.

## Allowed Repo Read Scope

Allowed reads are limited to reviewable repository source material under the configured `repo_root` after deny rules are applied:
- `README.md`;
- `AGENTS.md`, if present;
- `docs/**`;
- `migration/**`;
- `packages/**`;
- `apps/**`;
- `registry/**`;
- `artifacts/**` only for repo-tracked non-secret fixtures/configs;
- templates and static files;
- tests and smokes, if present.

Allowed scope is not enough by itself. Every read must also pass path traversal checks, symlink checks, size limits, binary handling rules and deny patterns.

## Denied Paths And Patterns

The MCP must reject reads, listings and search result previews for sensitive or non-reviewable local state, including:
- `.env*`;
- files or directories containing secrets, tokens, cookies, sessions or storage state;
- browser profiles and automation profile directories;
- runtime state directories;
- local DB dumps unless explicitly allowlisted as fixture/test data;
- private keys, certificates and certificate bundles;
- node/python caches and virtual environments;
- large binary artifacts unless explicitly allowlisted as fixture data.

Minimum denied glob/name set:
- `.env`, `.env.*`, `*.env`;
- `**/secrets/**`, `**/.secrets/**`, `**/tokens/**`, `**/cookies/**`, `**/sessions/**`;
- `**/*credential*.json`, `**/*service-account*.json`, `**/*storage-state*.json`, `storage_state*`;
- `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.crt`, `*.cer`;
- `*.sqlite`, `*.sqlite3`, `*.db`, `*.dump`, `*.bak`, unless allowlisted as fixture/test data;
- `node_modules/**`, `.venv/**`, `venv/**`, `__pycache__/**`, `.pytest_cache/**`, `.mypy_cache/**`;
- `.git/**`;
- browser profile directories such as `Default/**`, `Profile */**`, `User Data/**`, `playwright/.auth/**`.

The implementation must treat this as a minimum deny list, not an exhaustive list. Unknown sensitive-looking paths are denied by default. Documentation files that discuss secrets as a contract topic are not secret files by name alone, but their contents still pass through redaction before response.

## Required MCP Tools

The MCP contract requires these tools:

| Tool | Required behavior |
| --- | --- |
| `repo_status` | Report repo root, current branch, current commit SHA, dirty tracked-file summary and freshness metadata without exposing denied paths. Sensitive untracked paths may be counted but not named. |
| `list_tree` | List files/directories under an allowed path with depth and item-count limits. Denied paths are omitted or reported as denied without content. |
| `search_text` | Search allowed text files by literal text or bounded regex, returning capped matches with file path, line number and short redacted snippets. |
| `read_file` | Read an allowed text file up to configured size/response limits, with redaction applied before response. |
| `read_file_range` | Read an allowed line range from a text file, with max line count and redaction. |
| `find_files` | Find files by glob/name under allowed roots while applying deny rules and caps. |
| `get_file_metadata` | Return path, size, mtime, detected text/binary/large type and optional hash metadata without file contents. |

Optional tools:
- `git_grep` for Git-backed text search over tracked files only, still filtered through the same allow/deny/redaction layer.

No tool may execute arbitrary shell commands supplied by ChatGPT.

## Safety Model

The MCP must enforce safety at the filesystem boundary, not only in prompt instructions.

Required controls:
- path traversal protection: normalize requested paths and reject anything outside `repo_root`;
- symlink escape protection: resolve symlinks and reject targets outside `repo_root` or into denied paths;
- max file size limits before reading full contents;
- max response size limits after redaction and formatting;
- binary file handling: do not return raw binary content; return metadata only unless a fixture allowlist explicitly permits a safe representation;
- secret redaction before response for all snippets and file reads;
- deny-by-default handling for unknown sensitive names, extensions or directories;
- process-level read-only enforcement, preferably a read-only filesystem mount or OS/container policy that prevents writes even if server code has a bug.

Recommended redaction classes:
- API tokens and bearer tokens;
- cookie/session values;
- private key bodies;
- password-like assignments;
- service-account JSON private key fields;
- DSNs and URLs with embedded credentials.

If a file is denied, the MCP should return a structured denial reason such as `denied_sensitive_path`, `denied_symlink_escape`, `denied_binary`, `denied_size_limit` or `denied_outside_repo`.

## Freshness Model And Managed Clone Source

The MCP reads from one configured source repo path:
- local mode: `repo_root` may be a user-controlled checkout;
- remote mode: `repo_root` must be a separate managed clone, not `/opt/wb-core-runtime/app`, not production runtime state and not a random local Mac checkout.

Remote managed-clone config records:
- `source_mode = managed_clone`;
- `repo_url = https://github.com/orenvlad-ai/wb-core.git`;
- `branch = main`;
- `refresh_policy = none | external_manual | external_managed`.

Current implementation does not auto-fetch and does not mutate the clone. A separate external process may update the managed clone if `refresh_policy` documents that ownership. Served files still pass through the same read policy after any external refresh.

`repo_status` must report:
- resolved `repo_root`;
- source mode;
- configured repo URL and branch;
- refresh policy;
- current branch name;
- current commit SHA;
- dirty tracked-file state;
- actual `origin` URL if configured;
- whether the checkout has an upstream configured;
- last known `FETCH_HEAD` mtime if available without reading `.git` file contents.

Default contract: the MCP reads the current checkout and does not auto-fetch. This keeps the server read-only and avoids hidden network or Git side effects.

Before preparing a Codex prompt, ChatGPT should call `repo_status` and include the observed branch/commit in its reasoning or handoff. If the user expects a different branch/commit, the user updates the checkout outside the MCP and asks ChatGPT to re-check `repo_status`.

Open decision: whether a future implementation may support an explicit off-by-default `fetch_status` or bounded refresh endpoint. Any such mode must be lock-protected, must not serve partially updated files, must not read secrets, must not target production runtime state and must be visibly reported in `repo_status`.

## Prompt-Preparation Workflow

Expected assistant workflow:

1. User asks ChatGPT for a `wb-core` task.
2. Assistant calls `repo_status` to establish current branch, commit and dirty state.
3. Assistant uses `find_files`, `search_text` or optional `git_grep` to locate relevant code/docs.
4. Assistant reads selected files or line ranges with `read_file` / `read_file_range`.
5. Assistant prepares a precise Codex CLI prompt in chat, including:
   - task classification;
   - reason for classification;
   - execution mode;
   - relevant repo paths and observed commit;
   - exact scope and validation requirements;
   - curator/footer blocks required by `docs/architecture/07_codex_execution_protocol.md`.
6. User manually runs that prompt in Codex CLI.

The MCP may help prepare the prompt; it must not call Codex, DevControl, GitHub or deploy tools on behalf of the user.

## Current Implementation And Local Run

Implementation shape:
- dependency-light Python using only the standard library;
- stdio JSON-RPC MCP transport for local MCP clients;
- HTTP JSON-RPC MCP transport for URL-style clients;
- one path resolver/policy layer for all tools;
- redaction before text/snippet response;
- no mutation, shell, deploy, SSH, GitHub, Codex or DevControl tools.

Example config shape lives at `artifacts/wb_core_readonly_mcp/input/config.example.json`:

```json
{
  "repo_root": "/absolute/path/to/wb-core",
  "source_mode": "local_checkout",
  "repo_url": "https://github.com/orenvlad-ai/wb-core.git",
  "branch": "main",
  "refresh_policy": "none",
  "max_file_bytes": 1048576,
  "max_response_chars": 262144,
  "max_range_lines": 400,
  "max_search_matches": 50,
  "max_find_results": 200,
  "max_tree_items": 500,
  "max_tree_depth": 3
}
```

Local one-shot tool call:

```bash
python3 apps/wb_core_readonly_mcp.py --repo-root . --call-tool repo_status
python3 apps/wb_core_readonly_mcp.py --repo-root . --call-tool read_file_range --arguments-json '{"path":"README.md","start_line":1,"end_line":8}'
```

Local stdio MCP run:

```bash
python3 apps/wb_core_readonly_mcp.py --repo-root /absolute/path/to/wb-core
```

ChatGPT connector configuration concept:
- connector command: `python3`;
- connector args: `["/absolute/path/to/wb-core/apps/wb_core_readonly_mcp.py", "--repo-root", "/absolute/path/to/wb-core"]`;
- connector env: no secrets;
- connector description: "Read-only access to local `wb-core` checkout for prompt preparation only."

## Remote HTTP Mode

Remote mode is for hosting `wb-core-readonly-mcp` as a separate service over a dedicated read-only managed clone. It is not a product-plane route and must not be hosted inside the WebCore production runtime.

Current HTTP endpoints:
- `POST /mcp`: stateless JSON-RPC MCP requests (`initialize`, `tools/list`, `tools/call`);
- `GET /sse`: minimal SSE descriptor that tells URL/SSE-oriented clients to use `/mcp`;
- `GET /healthz`: non-secret server/transport health metadata.

This is a minimal HTTP/SSE-compatible implementation. If ChatGPT requires a stricter stateful remote MCP transport than stateless JSON-RPC-over-HTTP plus the `/sse` descriptor, the next implementation step is to adapt this transport layer while reusing the same `McpJsonRpcServer` and read policy.

Remote config example lives at `artifacts/wb_core_readonly_mcp/input/remote.config.example.json`:

```json
{
  "repo_root": "/srv/wb-core-readonly-mcp/clone/wb-core",
  "source_mode": "managed_clone",
  "repo_url": "https://github.com/orenvlad-ai/wb-core.git",
  "branch": "main",
  "refresh_policy": "external_manual",
  "remote_auth_token_env": "WB_CORE_READONLY_MCP_TOKEN",
  "max_file_bytes": 1048576,
  "max_response_chars": 262144,
  "max_range_lines": 400,
  "max_search_matches": 50,
  "max_find_results": 200,
  "max_tree_items": 500,
  "max_tree_depth": 3
}
```

Remote server run concept:

```bash
export WB_CORE_READONLY_MCP_TOKEN='set-outside-repo'
python3 apps/wb_core_readonly_mcp_http.py \
  --config /srv/wb-core-readonly-mcp/config/remote.config.json \
  --host 127.0.0.1 \
  --port 8766
```

URL connector concept:
- URL: `https://<authenticated-host>/mcp`;
- if the client asks for an SSE URL, use `https://<authenticated-host>/sse`;
- auth: `Authorization: Bearer <token>` if `remote_auth_token_env` is configured;
- exposure: place the service behind an authenticated tunnel or reverse proxy; do not expose it as an unauthenticated public internet service;
- source: `/srv/wb-core-readonly-mcp/clone/wb-core` or equivalent managed clone, refreshed outside this MCP process from GitHub `origin/main`.

Current remote mode rejects startup when:
- `source_mode` is not `managed_clone`;
- `repo_url` or `branch` is missing;
- `repo_root` is under `/opt/wb-core-runtime` or `/opt/wb-ai`;
- actual Git `origin` or current branch does not match configured `repo_url` / `branch`;
- managed clone has tracked or untracked dirty files;
- `remote_auth_token_env` is configured but the env var is empty.

## Hosted Service Model

The hosted service is a separate tooling service on the EU host, not part of WebCore product runtime.

Repo-owned hosted artifacts:
- systemd unit: `artifacts/wb_core_readonly_mcp/systemd/wb-core-readonly-mcp.service`;
- runtime env example without secret values: `artifacts/wb_core_readonly_mcp/env/wb-core-readonly-mcp.env.example`;
- setup/update script: `artifacts/wb_core_readonly_mcp/bin/setup_hosted_readonly_mcp.sh`;
- dedicated proxy example: `artifacts/wb_core_readonly_mcp/nginx/wb-core-readonly-mcp.localhost-proxy.example.conf`;
- hosted target example: `artifacts/wb_core_readonly_mcp/input/hosted_service_target__example.json`.

Hosted directory contract:
- base dir: `/opt/wb-core-readonly-mcp`;
- app code clone: `/opt/wb-core-readonly-mcp/app`;
- served repo clone: `/opt/wb-core-readonly-mcp/repo`;
- config: `/opt/wb-core-readonly-mcp/config/remote.config.json`;
- runtime env: `/opt/wb-core-readonly-mcp/env/wb-core-readonly-mcp.env`;
- service user/group: `wb-core-readonly-mcp`;
- systemd service: `wb-core-readonly-mcp.service`;
- HTTP bind: `127.0.0.1:8766` by default.

The app clone exists only to run `apps/wb_core_readonly_mcp_http.py`. The source of code truth served to ChatGPT is the separate repo clone at `/opt/wb-core-readonly-mcp/repo`, configured as `source_mode=managed_clone`, `repo_url=https://github.com/orenvlad-ai/wb-core.git`, `branch=main`.

The setup script supports:

```bash
artifacts/wb_core_readonly_mcp/bin/setup_hosted_readonly_mcp.sh print-plan
WB_CORE_READONLY_MCP_GENERATE_TOKEN=1 artifacts/wb_core_readonly_mcp/bin/setup_hosted_readonly_mcp.sh install-or-update
artifacts/wb_core_readonly_mcp/bin/setup_hosted_readonly_mcp.sh loopback-probe
```

For manual token provisioning instead of generated token:

```bash
export WB_CORE_READONLY_MCP_TOKEN='set-outside-repo'
artifacts/wb_core_readonly_mcp/bin/setup_hosted_readonly_mcp.sh install-or-update
```

The script:
- creates/updates only `/opt/wb-core-readonly-mcp/**` and `/etc/systemd/system/wb-core-readonly-mcp.service`;
- creates a least-privilege service user if missing;
- clones/pulls `origin/main` as that service user with `git pull --ff-only`;
- refuses dirty managed clones;
- writes runtime config without secret values;
- writes the bearer token only to the runtime env file when provided/generated;
- restarts only `wb-core-readonly-mcp.service`.

The app and repo clones are owned by the service user to keep Git `safe.directory` checks valid at runtime. The systemd unit still runs with `ProtectSystem=strict`, `NoNewPrivileges=true` and `ReadOnlyPaths=/opt/wb-core-readonly-mcp/app /opt/wb-core-readonly-mcp/repo /opt/wb-core-readonly-mcp/config /opt/wb-core-readonly-mcp/env`, so the service process itself cannot write to the clones while serving requests.

The script must not:
- touch `/opt/wb-core-runtime/app`;
- read `/opt/wb-ai/.env`;
- touch Seller Portal/browser/session state;
- edit broad nginx catch-all config;
- publish a route under the WebCore product-plane domain.

Hosted loopback verification:

```bash
set -a
. /opt/wb-core-readonly-mcp/env/wb-core-readonly-mcp.env
set +a
python3 /opt/wb-core-readonly-mcp/app/apps/wb_core_readonly_mcp_hosted_probe.py --base-url http://127.0.0.1:8766
```

The probe checks:
- `GET /healthz`;
- MCP `initialize`;
- MCP `tools/list`;
- MCP `repo_status`;
- one `read_file_range`;
- one `search_text`;
- denied `.env` behavior;
- absence of mutation-like tools.

Connector URL guidance:
- direct connector URL is only safe after an authenticated tunnel/reverse proxy is configured;
- if exposed through a dedicated authenticated hostname, use `/mcp` as the MCP URL and `/sse` only if the client requires an SSE discovery URL;
- the connector needs the bearer token out-of-band; do not paste token values into repo docs, PRs or handoffs;
- until authenticated URL/proxy is configured, the service is loopback-only and not externally reachable.

Targeted local validation:

```bash
python3 apps/wb_core_readonly_mcp_smoke.py
python3 apps/wb_core_readonly_mcp_remote_smoke.py
python3 apps/wb_core_readonly_mcp_hosted_artifacts_smoke.py
python3 -m py_compile packages/application/wb_core_readonly_mcp.py apps/wb_core_readonly_mcp.py apps/wb_core_readonly_mcp_http.py apps/wb_core_readonly_mcp_smoke.py apps/wb_core_readonly_mcp_remote_smoke.py apps/wb_core_readonly_mcp_hosted_probe.py apps/wb_core_readonly_mcp_hosted_artifacts_smoke.py
git diff --check
```

## Open Decisions

| ID | Decision | Current contract stance |
| --- | --- | --- |
| O-01 | Local repo path | Must be explicit `repo_root`; exact local path is environment-specific. |
| O-02 | Whether auto-fetch is allowed | Current implementation does not auto-fetch; future explicit opt-in remains undecided. |
| O-03 | Max file size and response size | Current defaults: `1 MiB` file read and `256 KiB` response. |
| O-04 | Whether `artifacts/**` is broad-read or fixture-only | Current implementation is fixture/config-like only after deny filtering. |
| O-05 | Exact ChatGPT remote MCP transport shape | Current implementation provides stateless HTTP JSON-RPC at `/mcp` and a minimal `/sse` descriptor; adjust if ChatGPT requires a stricter transport. |
| O-06 | Whether docs should be updated after implementation | This doc must be updated whenever tool/config/policy semantics change. |

## Validation Contract

For this implementation step:
- only repo-owned implementation, smoke and authoritative docs may be changed;
- no DevControl, live, deploy, public probe, SSH, browser-session or secret access is used;
- `wb_core_docs_master/**` and manifest are not updated;
- validation is limited to local MCP smoke/compile checks and diff hygiene.
