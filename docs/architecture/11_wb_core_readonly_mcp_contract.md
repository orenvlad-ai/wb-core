# WB-Core Read-Only MCP Contract

## Purpose

`wb-core-readonly-mcp` is a repo-owned minimal MCP server that gives a ChatGPT Project safe read-only access to the current local `wb-core` repository checkout.

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
- targeted local smoke: `apps/wb_core_readonly_mcp_smoke.py`;
- non-secret example config: `artifacts/wb_core_readonly_mcp/input/config.example.json`.

## Explicit Non-Goals

The MCP must not provide:
- writes to repo files or any other filesystem path;
- `git commit`, `git push`, merge, rebase or branch mutation;
- PR creation, PR update, review submission or GitHub write actions;
- deploy, publish, service restart or runtime rollout;
- SSH access, live runtime mutation, remote shell execution or remote file sync;
- reads of secrets, env files, cookies, browser sessions or storage-state files;
- DevControl production lane access or DevControl-backed execution.

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

## Freshness Model

The MCP reads from one configured source repo path:
- `repo_root`: local checkout of `wb-core`.

`repo_status` must report:
- resolved `repo_root`;
- current branch name;
- current commit SHA;
- dirty tracked-file state;
- whether the checkout has an upstream configured;
- last known fetch metadata if available without network access.

Default contract: the MCP reads the current checkout and does not auto-fetch. This keeps the server read-only and avoids hidden network or Git side effects.

Before preparing a Codex prompt, ChatGPT should call `repo_status` and include the observed branch/commit in its reasoning or handoff. If the user expects a different branch/commit, the user updates the checkout outside the MCP and asks ChatGPT to re-check `repo_status`.

Open decision: whether a future implementation may support an explicit off-by-default `fetch_status` or `auto_fetch=false/true` mode. Any such mode must not mutate worktree files and must be visibly reported in `repo_status`.

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
- one path resolver/policy layer for all tools;
- redaction before text/snippet response;
- no mutation, shell, deploy, SSH, GitHub, Codex or DevControl tools.

Example config shape lives at `artifacts/wb_core_readonly_mcp/input/config.example.json`:

```json
{
  "repo_root": "/absolute/path/to/wb-core",
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

Targeted local validation:

```bash
python3 apps/wb_core_readonly_mcp_smoke.py
python3 -m py_compile packages/application/wb_core_readonly_mcp.py apps/wb_core_readonly_mcp.py apps/wb_core_readonly_mcp_smoke.py
git diff --check
```

## Open Decisions

| ID | Decision | Current contract stance |
| --- | --- | --- |
| O-01 | Local repo path | Must be explicit `repo_root`; exact local path is environment-specific. |
| O-02 | Whether auto-fetch is allowed | Current implementation does not auto-fetch; future explicit opt-in remains undecided. |
| O-03 | Max file size and response size | Current defaults: `1 MiB` file read and `256 KiB` response. |
| O-04 | Whether `artifacts/**` is broad-read or fixture-only | Current implementation is fixture/config-like only after deny filtering. |
| O-05 | Whether docs should be updated after implementation | This doc must be updated whenever tool/config/policy semantics change. |

## Validation Contract

For this implementation step:
- only repo-owned implementation, smoke and authoritative docs may be changed;
- no DevControl, live, deploy, public probe, SSH, browser-session or secret access is used;
- `wb_core_docs_master/**` and manifest are not updated;
- validation is limited to local MCP smoke/compile checks and diff hygiene.
