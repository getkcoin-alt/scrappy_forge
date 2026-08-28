# Plugins, MCP connectors and skills

Configuration is user-owned. Forge does not automatically execute configuration found in a cloned repository. Put trusted configuration outside the project, or create it with `forge init-config` at `~/.config/scrappy-forge/config.json`.

Use absolute paths in extension configuration. Pin and inspect packages before running them. Names, URLs and environment variable bindings are configuration; credentials remain in your local environment/secret manager.

## Terminal management

```bash
forge mcp list
forge mcp add myserver --command /absolute/path/to/mcp-server --args '["stdio"]' --env SERVICE_API_TOKEN
forge mcp add service --url https://YOUR-SERVICE.example/mcp --header-env Authorization=SERVICE_AUTHORIZATION
forge mcp remove service
forge plugins add /absolute/path/to/plugin.json
forge plugins list
forge plugins remove formatter
forge skills add /absolute/path/to/skill.json
forge skills list
forge skills remove backend-review
```

Append `--config /absolute/path/to/trusted-config.json` to use a non-default configuration. Updates are atomic and locked; adding an existing MCP name needs explicit `--replace`. Registration does not download or execute a package. Plugin/skill removal only removes the configuration entry, not your source files. Restart Forge to load configuration changes. Existing running connections are not disconnected by editing their config.

### Import supported configurations from another client

```bash
forge mcp import /absolute/path/to/reviewed-mcp.json
```

The file must contain an `mcpServers` object. Imported entries support literal stdio command/argv and Streamable HTTP URLs. Environment values must be `${VARIABLE}` references. Headers may use `${VARIABLE}`, `Bearer ${VARIABLE}`, or `Basic ${VARIABLE}`. Literal credential values, unsupported fields and transports are rejected, and a failed import leaves the config unchanged. Environment remapping such as `"TARGET_TOKEN": "${SOURCE_TOKEN}"` is supported.

This is an explicit configuration converter, not execution of a vendor's native plugin bundle. It does not expand vendor-specific plugin roots, install packages, import account sessions or automatically load project configuration. Repository `.mcp.json` and `.claude.json` files are excluded from normal snapshots because they may contain credentials.

The common wrapper shape is documented in [Claude Code's MCP guide](https://code.claude.com/docs/en/mcp). Unsupported native features need an explicit adapter.

## 1. MCP: local servers

```json
{
  "mcp": {
    "myserver": {
      "transport": "stdio",
      "command": "/absolute/path/to/mcp-server",
      "args": ["stdio"],
      "env": ["SERVICE_API_TOKEN"]
    }
  }
}
```

In Forge:

```text
/mcp
/mcp connect myserver
/tools myserver
```

Approve the process launch only if you trust that server. **Stdio MCP servers run as host processes**, not inside Forge's Docker sandbox. They receive a minimal environment plus the explicitly named variables, and their working directory is the session workspace. This does not constrain what a trusted host process can access. Use an independently sandboxed MCP server where stronger isolation is required.

Tool calls require separate permission. Tool `readOnlyHint` annotations are not trusted authorization decisions. An external tool can have side effects even if its name says "get".

## 2. MCP: remote Streamable HTTP

```json
{
  "mcp": {
    "service": {
      "transport": "http",
      "url": "https://YOUR-SERVICE.example/mcp",
      "headers_env": {"Authorization": "SERVICE_AUTHORIZATION"}
    }
  }
}
```

`SERVICE_AUTHORIZATION` must contain the complete value expected by that server, such as `Bearer ...`. Never put the token in JSON. HTTP redirects are not followed automatically. Plain HTTP is permitted only for literal loopback hosts with `allow_loopback: true`, primarily for local testing.

The official SDK handles MCP initialization, sessions and tools over Streamable HTTP. Resources and prompts are available through namespaced tools. Prompt text remains untrusted guidance; it does not replace Forge's system instructions.

### GitHub example

GitHub documents a hosted MCP endpoint and a read-only route. A starting configuration is:

```json
{
  "mcp": {
    "github": {
      "transport": "http",
      "url": "https://api.githubcopilot.com/mcp/readonly",
      "headers_env": {"Authorization": "GITHUB_MCP_AUTHORIZATION"}
    }
  }
}
```

Use a suitably scoped token that the service accepts. GitHub account/organization policies still apply. This example was checked against public documentation, not authenticated against your account. It uses an environment-bound token. Forge also has MCP OAuth support, described in [AUTH.md](AUTH.md); that does not imply this specific service accepts automatic client registration or that its OAuth flow has been tested here.

References: [GitHub remote MCP server](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md), [GitHub setup and authentication](https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp-in-your-ide/set-up-the-github-mcp-server).

## 3. Command plugin

Create a manifest and list its absolute path under `plugins` in your trusted config:

```json
{
  "format": 1,
  "name": "formatter",
  "version": "1.0.0",
  "tools": [{
    "name": "inspect",
    "description": "Inspect formatting through a trusted helper",
    "type": "command",
    "argv": ["python", "/work/tools/format_helper.py"],
    "timeout": 30,
    "input_schema": {
      "type": "object",
      "properties": {"path": {"type": "string"}},
      "required": ["path"],
      "additionalProperties": false
    }
  }]
}
```

The helper receives one JSON object on stdin and must write one JSON value to stdout. Arguments are never interpolated into a shell command. The `argv` array is fixed by the manifest. The helper still needs to validate its inputs.

By default, command plugins use the configured runner. Under Docker, helper scripts and dependencies must exist in the image or workspace. `host: true` explicitly runs the command on the host; Forge shows that fact during approval. Treat this as arbitrary host code execution. A plugin cannot import Python into the controller simply by being registered.

## 4. REST connector plugin

```json
{
  "format": 1,
  "name": "status",
  "version": "1.0.0",
  "tools": [{
    "name": "health",
    "description": "Read the configured service health endpoint",
    "type": "http",
    "method": "GET",
    "url": "https://YOUR-SERVICE.example/health",
    "headers_env": {"Authorization": "SERVICE_AUTHORIZATION"},
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": false}
  }]
}
```

GET arguments become query parameters. POST/PUT/PATCH/DELETE arguments become a JSON body. The model cannot change the configured endpoint or method. Credentials are read from named environment variables only. Responses are bounded; redirects are refused. REST plugins run network requests from the controller, outside the code-execution container, so each call requires permission.

## 5. Versioned skill packs

Forge skill packs are simple playbook files, not automatically compatible with every vendor's plugin format. Example manifest:

```json
{
  "format": 1,
  "name": "backend-review",
  "version": "1.0.0",
  "description": "Trace request handling and verify error paths",
  "file": "workflow.md"
}
```

Put `workflow.md` beside the manifest and list the manifest's absolute path under `skills`. Only metadata is loaded initially. `/skill backend-review` or the `skill_read` tool loads the body. Bodies are hash-checked after registration; a changed file needs a restart/review. A skill does not grant tools, install packages or override permissions.

## Compatibility boundary

| Integration | Status |
| --- | --- |
| MCP stdio tools/resources/prompts | Implemented; tested with local fixture server |
| MCP Streamable HTTP | Implemented; tested with local fixture server |
| Environment-bound bearer/API-key headers | Implemented |
| OAuth authorization-code/PKCE and refresh | Implemented; tested with local HTTP issuer and MCP server |
| Encrypted OAuth cache | Optional; requires a separately managed Fernet key; no keyring integration |
| Public-client preregistration / client metadata URL | Configurable; provider support required |
| Confidential client secrets / device-code flow | Not implemented |
| Supported `mcpServers` configuration imports | Explicit conversion; no auto-execution or account import |
| Legacy HTTP+SSE transport | Not implemented |
| Server-initiated model sampling/elicitation | Not exposed |
| Fixed-command plugins | Implemented; JSON input/output |
| Fixed-endpoint REST connectors | Implemented |
| Claude/ChatGPT/VS Code native plugins | Not automatically compatible; use MCP or adapt explicitly |
| Existing ChatGPT connector sessions | Not inherited |

For any real service: configure the endpoint, grant narrow account scopes, inspect discovered tools, try a read-only operation, then test writes on non-production data. Do not grant production permissions merely because the MCP handshake succeeded.
