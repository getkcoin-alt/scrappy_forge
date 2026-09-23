# Scrappy Forge 0.4 — local-first terminal coding agent

A terminal coding agent with an execution loop, context compaction, persistent project memory, permission-aware tools, plugins and MCP connections. Version 0.4 adds masked API-key onboarding, a queryable files/processes/network/windows graph, a public wheel installer, and a Railway release service with optional account authentication. It builds on the tested 0.3 core; this is not a new foundation-model training project.

**This is a working prototype, not a proven 10× improvement or a drop-in clone of Claude Code.** It can edit code in any text-based language and run installed toolchains through its terminal tool. Success still depends on the model, environment, task and verification. Start with non-sensitive projects.

## Project status

Scrappy Forge is moving toward a **public beta**. The local coding path, permission model, project memory, tool layer, MCP support, verification flow and release service are implemented and test-covered, but I am deliberately not calling it “production-ready” yet. The remaining bar is boring but important: stable hosted CI, broader live-provider testing, an external security review, release signing and more time with contributors using it on real projects.

**Contributions are welcome.** If you want to improve the runtime, adapters, tests, docs, safety boundaries or developer experience, start with [CONTRIBUTING.md](CONTRIBUTING.md) and the open issues. Security findings should follow [SECURITY.md](SECURITY.md), not a public issue.

The project is built in public-beta spirit: make claims we can verify, keep execution permissioned, and prefer a smaller mechanism that works over a bigger demo that only looks impressive.

## What was added

Start with the [user guide](src/forge_hub/static/user-guide.md),
[plans and availability](src/forge_hub/static/plans.md),
[world-model contract](src/forge_hub/static/world-model.md), and
[Railway deployment runbook](DEPLOYMENT.md).

The account panel is user-only. There is no admin frontend, hosted code runner,
source sync or billing. Account registration stays closed until configured;
local BYOK coding does not require an account. The owner cannot read users'
local files/keys or disable offline local coding through this backend.

| Area | Implemented |
| --- | --- |
| Terminal experience | `forge`, one-shot `-p`, interactive approvals, session resume, slash commands, status and reports |
| Master loop | Inspect → call tools → observe → repair → verify; bounded steps; durable journal; cancellation and recovery |
| Context | Automatic and manual extractive compaction; output reserve; token estimates calibrated from provider usage; complete tool-call groups preserved |
| Memory | SQLite/FTS5 project notes, provenance, source hashes, stale-note filtering, search and deletion |
| Tools | JSON Schema validation, risk classes, permissions, deadlines, checkpoints and lazy tool discovery |
| Plugins | Versioned manifests for JSON-over-stdin command tools and fixed-endpoint REST tools |
| Skills | Versioned Markdown playbooks with metadata discovery, on-demand loading and change detection |
| MCP | Official Python SDK; stdio and Streamable HTTP; tools, resources and prompts; environment-bound credentials |
| OAuth | Browser authorization link, loopback callback, PKCE/state checks, scope limits, memory or encrypted token storage and refresh |
| Connector setup | `forge mcp add/list/remove/import`; explicit imports of supported `mcpServers` configurations |
| Extension setup | `forge plugins add/list/remove` and `forge skills add/list/remove` for reviewed local manifests |
| Models | OpenRouter free-only routing; local OpenAI-compatible endpoints such as a configured Ollama server |
| Applying changes | User-only `/apply`; exact verified source hash, original-drift checks, backups and rollback after handled write failures |
| Evidence | Before/after configured checks, patch, source hashes, event journal and JSON/Markdown reports |
| Onboarding | `forge setup`, masked key input, OpenRouter key validation, opt-in owner-only storage, environment-key precedence |
| World model | Bounded read-only graph, collector status/freshness, snapshot queries and diffs, permissioned host metadata |
| Distribution | Isolated uv install; Apple Silicon/Intel macOS and Linux/WSL targets; public wheel with SHA256 |
| Accounts | Optional Supabase Auth adapter, verified user identity, refresh/logout and backend-only owner administration; disabled until configured |

## Install on macOS or Linux

Public release service: [Scrappy Forge](https://forge-hub-production.up.railway.app).
[User guide](https://forge-hub-production.up.railway.app/guide) ·
[Plans](https://forge-hub-production.up.railway.app/plans) ·
[Release metadata](https://forge-hub-production.up.railway.app/v1/releases/latest).

Download, inspect, then run the installer:

```sh
forge_installer=$(mktemp)
curl --proto '=https' --tlsv1.2 -fL 'https://forge-hub-production.up.railway.app/install.sh' -o "$forge_installer"
less "$forge_installer"
sh "$forge_installer"
```

It installs a private Python 3.12 environment through uv, without
sudo or requiring a GitHub checkout. It also configures the optional
account-service origin. Open a new terminal, enter your project, and run `forge`.

The installer targets Apple Silicon/Intel macOS and 64-bit Linux/WSL. Support
depends on a compatible OS/Python/native dependencies; it is not a notarized
standalone macOS binary. No universal device-compatibility claim is made.

For contributors building from source, Python 3.11+ is required:

```bash
git clone https://github.com/getkcoin-alt/scrappy_forge.git
cd scrappy_forge
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[auth]'
forge setup
forge doctor
```

The optional `auth` extra enables encrypted OAuth storage. `pip install -e .` is sufficient for memory-only OAuth and the other core features. Alternatively, install the included wheel from `dist/` into a virtual environment; its dependencies still need installation. `requirements-tested.txt` records the versions used for validation; it is an environment snapshot, not a cross-platform hashed lockfile.

`forge` is available while that environment is activated. For a global isolated
source installation, use an existing uv installation:

```bash
uv tool install '/absolute/path/to/scrappy_forge[auth]'
```

### Try the pipeline without an API key

After installing, run:

```bash
python examples/offline_demo.py
```

This creates a tiny buggy project, uses **scripted responses (not an AI model)** to request a real edit, and runs real before/after tests. It prints the patch and report paths. Only its own generated project is executed locally. No external inference, credentials or Docker are needed. This is a setup check, not an intelligence benchmark.

## Open it like a terminal coding assistant

```bash
cd /path/to/project
forge
```

Forge prints the session ID and its workspace copy. Type an ordinary task:

```text
Trace the login request, find why expired sessions still work, and add a regression test.
```

It prompts for permission before edits and commands. Read the displayed tool, arguments, origin and execution details. Type `approve` to permit that one action. A plugin's description or MCP read-only annotation is not treated as permission.

**It starts with a separate copy of your source.** It does not inherit `.git` or automatically merge, push or deploy. Use `/diff` and `/verify`, then `/apply` to review and explicitly approve copying verified changes into your original project. The model has no apply tool. `/export` remains available for a manually applied patch.

## Configure a model

### OpenRouter free models

First run asks for your key with masked input, or run `forge setup`. Validation
contacts only OpenRouter's key endpoint, not a model. Opt-in saving uses a 0600
local file, not encryption or macOS Keychain. `forge setup --forget-key` removes
the saved key. The Railway service never receives this key.

Alternatively, set `OPENROUTER_API_KEY` in your local environment or secret
manager; this takes precedence over a saved key. Do not put it in a task, config
JSON, repository or chat. In Bash, hidden input is one option:

```bash
read -s -p "OpenRouter key: " OPENROUTER_API_KEY
export OPENROUTER_API_KEY
echo
forge
```

Before external inference, Forge asks for permission to transmit task text, selected source, retrieved memory and tool outputs. For a one-shot run, give that consent explicitly with `--allow-code-upload`.

The default is `openrouter/free`. You can select a currently available tool-capable `vendor/model:free` ID with `--model`. The router can change the underlying model between calls; response model IDs and usage are recorded. Free quotas and capacity may interrupt long runs. There is no paid-model fallback.

Run `forge models`, or `/models` in a session, to query the public catalog for free IDs that advertise tools and sufficient context. Use `/model MODEL_ID` to change subsequent requests. Discovery sends no source or API key. Catalog metadata does not guarantee model quality, current provider capacity or compatibility with every tool schema.

The adapter sends zero maximum prompt/completion/request prices and `data_collection: deny`. Privacy restrictions can leave no eligible free provider; Forge fails rather than silently relaxing them. This is not an unconditional zero-retention guarantee. Review OpenRouter and provider policies before sending private code.

### Local model

Configure an already running, tool-capable local OpenAI-compatible server:

```json
{
  "provider": "local",
  "model": "YOUR_INSTALLED_TOOL_CAPABLE_MODEL",
  "endpoint": "http://127.0.0.1:11434/v1"
}
```

Save this as a trusted config outside the project, then run `forge --config /absolute/local.json`. Local endpoints must use loopback addresses. Forge does not install models or run Ollama for you. Compatibility with a particular local model must be tested; the HTTP adapter was exercised with a scripted local fixture, not a real Ollama model.

## Execution and verification

Docker is the default for terminal commands and configured checks. It uses a fresh container with networking disabled, no host credentials, reduced privileges and resource limits. Prepare the image yourself:

```bash
docker pull python:3.12-slim
forge
```

For Node, pytest, a compiler or other dependencies, build a trusted development image containing the required tools and set `image` in the config. There is no automatic image pull or dependency bootstrap. The sandbox cannot reach production services or download packages.

For code you trust, you may explicitly allow host execution:

```bash
forge --execution trusted-local --allow-local-execution
```

**Trusted-local is not sandboxed.** A test, dependency script or command can affect the host despite the separate project copy. Do not use it for untrusted repositories.

Add checks from the terminal. The command runs only after permission is granted:

```text
/check add unit ["python", "-m", "unittest", "discover", "-s", "tests"]
/verify
```

Or define `checks` in your trusted config before starting a session. Forge records baseline results before implementation and runs final checks in fresh copies. Verification rejects mutations to included source files inside the check copy. An arbitrary terminal command with exit code zero is not automatically counted as verification.

Without configured checks, changed code is reported as **unverified**, never as verified success. `review_ready` means the configured checks passed for the reported source hash; it does not mean all requirements are satisfied or that deployment is safe.

## Terminal commands

| Command | Purpose |
| --- | --- |
| `/help`, `/status` | Help and current session details |
| `/world [scopes]`, `/world query JSON` | Capture/query local structured state; host scopes need approval |
| `/models`, `/model ID` | Discover models or select one for subsequent requests |
| `/permissions MODE` | Switch between ask, plan and accept-edits as a user command |
| `/resume` | Continue current/interrupted work |
| `/compact` | Compact old context while retaining the raw event archive |
| `/memory QUERY` | Search notes, including stale labels in the terminal |
| `/remember NOTE`, `/forget ID` | Add/delete project-scoped notes |
| `/tools QUERY`, `/tools reset` | Discover tools; unload lazy schemas |
| `/skills`, `/skill NAME` | List/load a versioned workflow |
| `/mcp`, `/mcp connect NAME` | List/approve configured MCP connections |
| `/check add NAME JSON_ARGV`, `/verify` | Configure and run checks |
| `/diff`, `/undo`, `/export` | Inspect patch, restore local checkpoint, export evidence |
| `/apply preview`, `/apply` | Preview or explicitly apply verified changes to the original project |
| `/quit` | Save and exit |

Ctrl-C stops the process and saves the session. Resume with:

```bash
forge sessions
forge --resume SESSION_ID
```

If the previous run used trusted-local, opt into that execution mode again. A session does not carry execution privileges into future invocations. MCP connections also require approval again. Incomplete tool calls are marked uncertain and are not blindly replayed. Check external state before repeating a possibly completed write.

## One-shot mode

```bash
forge --repo /path/to/project \
  --config /path/to/trusted-config.json \
  --allow-code-upload \
  --allow-tool file_edit \
  --allow-tool file_write \
  --allow-tool verification_run \
  -p "Fix the reported bug and verify it"
```

Noninteractive approval defaults to deny. `--allow-tool` is an explicit user grant for a named tool, not a model request. Protected paths still need interactive approval. Do not broadly allow `terminal_run` or external-write tools unless you intend to grant their full capabilities.

`--permission plan` permits only built-in read operations. `--permission accept-edits` permits ordinary file edits while retaining prompts for protected paths, commands and connectors. Default `ask` prompts for writes. Unknown or denied tools cannot grant themselves access.

## Add plugins and connectors

Read [CONNECTORS.md](CONNECTORS.md) for configuration formats, a GitHub MCP example, command/REST plugins and versioned skills. Nothing automatically reuses ChatGPT's installed connectors or account sessions.

An extension can add a tool without modifying the core agent. MCP tools are discovered after you approve a connection. The model uses `tool_search` to select relevant schemas rather than loading every connector into the prompt.

Configuration commands are available directly in the terminal:

```bash
forge mcp list
forge mcp add localtools --command /absolute/path/to/server --args '["stdio"]'
forge mcp import /absolute/path/to/reviewed-mcp.json
forge plugins add /absolute/path/to/plugin.json
forge skills add /absolute/path/to/skill.json
```

These commands register configuration; they do not download or execute packages. Restart Forge and use `/mcp connect NAME` to connect with approval. Existing ChatGPT, Claude, VS Code and other native plugin bundles are not automatically portable. Supported MCP configurations can be imported explicitly; unsupported fields are rejected rather than silently ignored.

OAuth is described in [AUTH.md](AUTH.md). Its flow was exercised against a local HTTP test issuer and MCP server, including PKCE and refresh. Real SaaS accounts still require their own setup and compatibility testing.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the loop, module map, state recovery and verification semantics.

## Memory and compaction details

- SQLite stores sessions and a durable event journal locally, under `SCRAPPY_FORGE_HOME` or `~/.local/share/scrappy-forge`.
- Memory is isolated by the original project's absolute path. Notes may be linked to a source hash and become stale after changes. Stale notes are excluded from automatic retrieval.
- Search uses FTS5 lexical matching, not embedding-based semantic search. No embedding API or vector database is required.
- Compaction is extractive: a bounded action/observation handoff plus recent complete message groups. The current task and acceptance criteria remain pinned.
- Large tool results become referenced artifacts. `history_read` and `artifact_read` let the model retrieve details later.
- Token counts are estimates calibrated upward from reported usage. Different free models use different tokenizers; no exact universal tokenizer is claimed.
- A pinned task or tool schema that cannot fit causes an explicit stop. Context compaction cannot create infinite reliable memory.

## Review and apply

`/apply` only accepts changed code with passing configured checks for its current hash. It shows the patch and destination for approval, checks that the original project still matches the starting snapshot, creates a backup and stages the candidate, then replaces changed files. Handled write failures trigger rollback. A process or machine crash can leave a partial application; the durable pending marker points to the backup and blocks further application until the state is reconciled. Concurrent external editors are not governed by Forge's process lock.

For a saved session:

```bash
forge apply SESSION_ID --preview
forge apply SESSION_ID
```

Noninteractive application requires an explicitly supplied reviewed patch hash: `forge apply SESSION_ID --expected-patch SHA256`. A mismatched hash is refused. This command does not run or configure checks for you. After applying, start a new session for the next change; the old session retains its original baseline.

`/export` writes `report.md`, `report.json` and `changes.patch` into the session directory. For manual Git application after review:

```bash
git apply --check /absolute/session/changes.patch
git apply /absolute/session/changes.patch
```

Run normal project checks again before committing. `/undo` restores only a workspace checkpoint. It does not reverse an applied patch in the original project, API calls, notifications, database writes or deployments.

## Validation and limitations

See [RELEASE-VERIFICATION.md](RELEASE-VERIFICATION.md) and the repeatable scripts in
`validation/`. The suite exercises real subprocesses, CLI-to-HTTP-fixture repair
and application, compaction, SQLite memory, local MCP/OAuth transports, encrypted
token refresh, connector management, world graphs and account authorization.
The wheel-install test covers masked terminal key entry, verified apply and a
real localhost release server. Scripted fixtures do not establish LLM reasoning
quality or real-provider compatibility. Generated transcripts are not committed.

Still outside this release: automatic account provisioning or universal OAuth compatibility, legacy MCP SSE/WebSocket transports, server-initiated sampling/elicitation, a marketplace/package installer, embedding memory, a language server, distributed/multi-agent scheduling, production deployment automation, service-fixture orchestration, native Windows support, and exhaustive third-party plugin compatibility. Binary patch export, symlink projects and file/directory replacements through `/apply` are unsupported. Snapshots are capped at 100 MB/10,000 files, with 5 MB per included file and 1 MB per file for text tools. Common caches and credential filenames are excluded, including `.mcp.json`, but full `.gitignore` semantics and comprehensive secret detection are not implemented.

The code tools work on text in any language; execution requires a suitable installed toolchain or prepared image. Docker and live OpenRouter inference were not available for validation here. Do not use this prototype as an unrestricted production operator.

**Use it for progressively broader coding tasks after validating each workflow.** Do not call it universally compatible or 10× better before measuring accepted changes, human review time, failures and cost against a strong baseline.

## References

Implementation references checked 27 August 2026:

- [MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [OpenRouter tool calling](https://openrouter.ai/docs/guides/features/tool-calling)
- [OpenRouter price/privacy routing](https://openrouter.ai/docs/guides/routing/provider-selection)
- [OpenRouter free router](https://openrouter.ai/openrouter/free)
- [OpenRouter model catalog](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties)
- [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp)
