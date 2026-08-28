# Authentication: three separate credentials

| Credential | User command | Destination | Persistence |
| --- | --- | --- | --- |
| Model API key | `forge setup` | OpenRouter only | Memory or explicit unencrypted 0600 record; env key wins |
| Forge account | `forge account login` | Your configured Forge HTTPS service and its identity provider | Explicit unencrypted origin-bound session record |
| MCP OAuth | `/mcp connect NAME` | Approved connector's issuer/resource | Memory by default; optional encrypted token store |

Never paste any of these credentials into a task. A Forge account does not
authenticate a model or every connector; browser account login does not
automatically authenticate the terminal. The local agent works without a Forge
account. Production account configuration and owner-only backend access are
documented in [DEPLOYMENT.md](DEPLOYMENT.md). Registration is disabled by default.

## MCP OAuth sign-in

Forge supports the authorization-code flow through the official MCP Python SDK. It prints a sign-in link, receives the browser redirect on a loopback listener, and performs the token exchange. MCP sign-in does not provision Forge accounts, inherit ChatGPT/Claude sessions, or authenticate to every vendor automatically.

The supported flow was tested against a local HTTP issuer and MCP server. The test verifies S256 PKCE, the resource parameter, callback state, encrypted storage, expiry across connections, refresh-token rotation and a subsequent authenticated tool call. A real browser UI and a real SaaS account were not used in that test.

## Configure a compatible server

Use your provider's actual MCP endpoint:

```bash
forge mcp add service --url https://YOUR-SERVICE.example/mcp --oauth
forge
```

In the interactive terminal:

```text
/mcp connect service
```

Approve the connection. If the server requires OAuth, approve any new authorization origin and review the requested scopes. Open the printed link in your browser and complete the provider's consent screen. The browser redirects to `http://127.0.0.1:8766/oauth/callback`. The listener closes after the callback or timeout. No authorization code needs to be pasted into Forge or chat.

The browser must be able to reach the loopback port on the machine running Forge. This default is intended for a local terminal. A remote/headless environment needs separately configured access to the callback; do not expose the listener on a public interface.

If port 8766 is busy, set `--callback-port PORT` when adding the server. The port must be 1024–65535. For a preregistered client, register the exact resulting callback URL with the provider.

## Registration and scope controls

The SDK supports dynamic client registration where the server offers it. You may supply `--client-id PUBLIC_CLIENT_ID` for a preregistered **public client**. Confidential client secrets and client-credentials/device-code flows are not implemented by Forge's configuration layer.

For a client ID metadata document you host and control, the trusted configuration may include `oauth.client_metadata_url`. The SDK uses it where the authorization server supports it. Forge does not host that document for you.

To limit scopes:

```bash
forge mcp add service --url https://YOUR-SERVICE.example/mcp \
  --oauth --scopes "read:projects read:issues"
```

Replace those illustrative scopes with the provider's actual scope names. Forge refuses authorization if the server demands scopes outside the configured limit. Changing the limit requires an explicit configuration update. Every authorization-code flow still asks for local consent; a tool cannot authorize itself.

HTTPS is required for remote endpoints. Additional discovery/token origins require approval before requests are sent. The MCP bearer token is not forwarded to another origin. S256 must appear in the authorization server's metadata. HTTP is allowed only with an explicitly configured loopback test server.

## Token storage

**Memory is the default.** Tokens are forgotten when the process exits; no token file is created. OAuth access tokens and authorization codes are not placed in the model transcript or approval journal.

For cross-session refresh, install the optional dependency:

```bash
python -m pip install -e '.[auth]'
```

Generate a Fernet key locally, retain it in your own password/secret manager, and export it as `FORGE_TOKEN_KEY` for Forge. Do not put the key in a repository, config file or chat. Keep the same key across runs; generating a new key will make the old records unreadable.

Then add the connection with `--oauth-storage encrypted`, or use this trusted configuration:

```json
{
  "mcp": {
    "service": {
      "transport": "http",
      "url": "https://YOUR-SERVICE.example/mcp",
      "oauth": {
        "storage": "encrypted",
        "key_env": "FORGE_TOKEN_KEY",
        "callback_port": 8766,
        "timeout": 300
      }
    }
  }
}
```

Fernet provides authenticated encryption. Records contain tokens, expiry, client registration and discovered issuer metadata. A record is bound to the server name, full MCP URL and OAuth configuration. Directory/file modes are restricted, and an exclusive lock prevents two active connections from refreshing the same stored token concurrently. One encrypted connection per binding may be active at a time.

Successful encrypted sign-in can enable later noninteractive use or refresh, subject to connection/tool permissions. If a browser sign-in or approval of a new origin is needed, a noninteractive run fails rather than bypassing consent. A scope challenge during a tool call is still bounded by the tool timeout; reconnect interactively if authorization cannot complete within that budget.

The encryption key remains accessible to the Forge process. This is not protection against a compromised account or malicious approved host code. Session notes and ordinary transcripts remain unencrypted; only the OAuth records use this vault.

## Sign out

```bash
forge auth status service
forge auth logout service
```

Status reports configuration, not proof of a valid remote login. Close active connections before logout. Logout deletes local cached records; it does **not** revoke the provider's grant or invalidate tokens already issued. Revoke those from the provider's security settings where required.

## References

- [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Fernet documentation](https://cryptography.io/en/latest/fernet/)

Forge pins the SDK's tested 1.29.x API. Its small subclass adds scope/PKCE checks, persisted expiry and issuer restoration, and sanitizes token-endpoint failures. Re-run the OAuth integration tests before changing that dependency. This is not an independently audited authentication implementation.
