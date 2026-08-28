# Scrappy Forge 0.4 — user guide

Forge is a local terminal coding agent. Railway distributes the CLI and can host
an optional account service. It does not receive your model key, execute your
project, or store your world graph. Accounts are not required for local BYOK use.

## Install

On the live service, the origin below is filled in automatically. In the source
file, replace `@@FORGE_URL@@` with the verified release-service origin.

```sh
forge_installer=$(mktemp)
curl --proto '=https' --tlsv1.2 -fL '@@FORGE_URL@@/install.sh' -o "$forge_installer"
less "$forge_installer"
sh "$forge_installer"
```

Review the installer before running it. It checks the wheel's SHA256, installs
uv from Astral if missing, obtains Python 3.12 if needed, and installs an isolated
tool without sudo. It updates shell PATH configuration. Open a new terminal:

```sh
cd /path/to/your/project
forge
```

Apple Silicon and Intel macOS, 64-bit Linux, and WSL are installer targets.
An old macOS release may not support the required Python/native dependencies;
this is not a notarized standalone macOS application. Native Windows is not a
supported execution target. `scrappy-forge` is an alternative command name if
another installed application already owns `forge`; the installer will not
silently overwrite a conflicting command.

## First run

Enter your OpenRouter key at the masked terminal prompt. Validation contacts
OpenRouter's key endpoint, not a model; it sends no project source. Choose to
save it locally or use it only for this session. Saving uses a private 0600 file,
not encryption or macOS Keychain. Never paste the key into chat or a task.

`forge setup` changes the saved key; `forge setup --forget-key` removes it.
`OPENROUTER_API_KEY`, when present, takes precedence over a saved key. Use
`forge setup --skip-validation` when you explicitly do not want a validation
request; inference still requires a working key later.

Forge asks separately before uploading task text, selected source, memories or
tool results to the model provider. The default `openrouter/free` has no paid
fallback. Free capacity and quotas are not an availability guarantee. A local,
tool-capable OpenAI-compatible server is supported through trusted config.

Docker is the default execution boundary. If unavailable, choose read-only plan
mode or explicitly consent to unsandboxed local execution for this session.
Local execution and host MCP/plugin processes can access your computer as your
user; a copied project is not a security sandbox. Use non-sensitive projects
while evaluating this preview.

## Coding workflow

1. Run `forge doctor` to inspect configuration and execution availability.
2. Start `forge` in your project and describe the task and acceptance criteria.
3. Review each requested tool action; `approve` authorizes it once.
4. Use `/help`, `/diff`, `/verify`, and `/status` to inspect progress and evidence.
5. Configure your real project checks; no configured checks means no claim of
   tested correctness. The agent works in a separate source copy.
6. Use `/apply preview`, then `/apply` and approve the exact patch to update the
   original project. Forge checks verified hashes and original-project drift.
7. Run your normal CI and review the patch. Start a new session for the next
   change after applying. `/undo` does not undo an applied original-project edit.

`/quit` prints the resume command. Sessions, artifacts, project memories and
compaction history stay locally. Their contents are not encrypted. Automatic
compaction preserves complete tool-call groups; it is bounded extractive
compression, not lossless long-term reasoning or learned semantic memory.

## Structured world model

Start with files only (metadata and hashes, not contents):

```sh
forge world snapshot --repo . --scopes files --output /tmp/forge-world.json
forge world query --snapshot /tmp/forge-world.json --kind file --contains src
```

Output paths must not already exist. For a host snapshot, explicitly consent:

```sh
forge world snapshot --scopes files,processes,network,windows --allow-host-read --output /tmp/forge-host-world.json
```

In a session, `/world` captures locally; `/world processes,network,windows` asks
for host-read approval. Local slash-command results are not added to model
context. Separate model-facing `world_capture` and `world_query` tools require
approval. See the world-model document for schema, platform limits and freshness.

## Plugins and connectors

Use `forge mcp --help`, `forge plugins --help`, and `forge skills --help` to
register reviewed configurations. MCP supports stdio and Streamable HTTP;
command/REST plugins and Markdown skills use versioned local manifests. OAuth
support is provider-dependent. Imported configurations are not automatically
trusted, downloaded or executed. No promise of compatibility with every plugin
or connector is made. See the repository's CONNECTORS.md and AUTH.md.

## Optional account

The installer configures the release-service origin. `forge account login`
checks availability before asking for a password. If sign-in is not configured,
continue using local BYOK. When enabled, `forge account signup`, `me`, `refresh`,
and `logout` manage your account. Passwords are masked; tokens saved with your
consent are local, unencrypted, owner-only files bound to the service origin.
Browser login does not sign the terminal in automatically. The browser keeps
its access token in memory only; reloading signs that browser view out.

There is no billing, paid subscription, remote execution, account-based source
sync, or admin frontend in this release. See /plans for availability and limits.

## Updates, removal and support

Re-run the current verified installer for an update; inspect the release version
and checksum at /v1/releases/latest. Back up your local state/config first.
`uv tool uninstall scrappy-forge` removes commands, not your projects or saved
state. Clear saved keys/account sessions with the commands above before removal
if desired. Do not send keys, private source or unredacted session logs in bug
reports. Report version, OS, failing command and a sanitized reproduction.

This is a technical preview, not a proven 10x improvement. Keep ordinary code
review and production-access controls in place.
