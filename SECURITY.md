# Security and deployment boundaries

## Reporting a vulnerability

Please do **not** open a public issue for a suspected vulnerability.

While the repository is private, report security issues to **karnveer@scriza.in** with a short reproduction and affected version/commit. Once the repository is public, GitHub private vulnerability reporting / repository security advisories should be the preferred channel when enabled.

Do not include real API keys, access tokens, customer data or unrelated private material in the report.

Scrappy Forge 0.4 separates a local single-user coding agent from a public
release/optional-account service. It is not a multi-tenant execution service.
Do not expose CLI state or wrap code execution in a public API. Railway runs
`forge-hub`, never users' tasks, terminal commands or projects.

## New 0.4 trust boundaries

- Saved model keys and Forge account sessions are **unencrypted**, owner-only
  records outside repositories. MCP OAuth's optional encrypted store is a
  different facility. Environment keys take precedence; model keys never go
  to the Forge account service. Approved host code can still steal local keys.
- World graphs contain sensitive metadata even without file contents, command
  arguments, environment variables, window titles or screenshots. Model-facing
  graph access needs approval. Observations are untrusted, partial and dated;
  do not treat them as authority or an atomic view of the machine.
- Public downloads expose the distributable Python source even when the GitHub
  repository is private. This distribution is intentional. SHA256 delivered
  over the same HTTPS service detects corruption, not a compromised publisher;
  independent release signing and hashed cross-platform dependencies remain
  follow-up work. The uv bootstrap is a separate trusted supply chain.
- Account endpoints fail closed until configured. Password handling/session
  issuance is delegated to Supabase Auth; the hub proxies credentials over TLS
  without persistence. Fresh provider identity is checked on protected requests;
  user metadata cannot grant roles. The owner is an immutable configured UUID.
- No admin frontend, public owner-bootstrap route, remote execution or source
  collection exists. Owner account suspension cannot disable offline CLI use.
- The browser uses memory-only access tokens, no auth cookies/localStorage,
  same-origin requests, CSP and no-store headers. Logout revokes refresh ability
  for the session; issued access tokens may survive until expiry. The CLI rotates
  refresh tokens under a binding lock; ambiguous refresh failures require login.
- Rate limits are bounded and in-process. Use one worker/replica until a shared
  limiter exists. Do not trust arbitrary forwarding headers. Behind Railway's
  proxy the peer-based limit may be shared by multiple users; add a verified
  edge/rate-limit design before account signup at scale.
- Password recovery, account deletion, MFA UX, CAPTCHA, production email,
  chosen terms/privacy policy and live-provider review are public-signup gates,
  not completed features. Keep registration closed until the runbook is met.

## Boundaries that exist

- Original repositories are copied into a separate session workspace.
- Built-in file tools reject traversal and symlinks and check hashes before edits.
- Tool arguments pass JSON Schema validation; remote schema references are rejected.
- Tool approvals are evaluated outside the model. Plan mode cannot be overridden by an allow-list entry.
- Protected file edits require approval even in accept-edits mode.
- Commands use argv arrays, not implicit shell interpretation. A user can still approve an explicit shell invocation; inspect it.
- Docker code execution has no network, no privileged mode, no Docker socket and no inherited credential environment.
- MCP and plugins cannot turn their own metadata into an automatic permission grant.
- Tool deadlines and bounded subprocess output prevent some runaway executions.
- Interrupted actions are journaled as uncertain; they are not automatically replayed.
- Stored evidence is tied to the current source hash and downgraded if stale.
- Original-project application is a user command, with exact-patch approval, source verification, drift checks, staging, backups and a durable pending marker.
- OAuth callbacks bind to loopback only, validate state and Host, and close after completion or timeout. S256 support and scope limits are checked.
- OAuth uses memory-only credentials by default. Optional persistent records use authenticated encryption and a separately supplied key; token refresh is locked per binding.

## Boundaries that do not exist

- `trusted-local`, host command plugins and stdio MCP servers are host processes. The copy of the repository does not sandbox them.
- The Docker daemon, image and kernel are trusted. These settings are not a defense against every container escape.
- Path checks do not protect against a malicious concurrent host process racing filesystem operations.
- Config manifests are user-owned executable policy. Do not load unreviewed manifests from repositories.
- Protected tests and fresh copies are not tamper-proof evaluation. Candidate code may subvert its test runtime.
- Filename exclusions and redaction patterns are not comprehensive secret detection. Source files, outputs and memory can still contain sensitive data.
- SQLite, transcripts and artifacts are not encrypted. The state directory is created private for a new installation; verify permissions and disk encryption yourself, especially for existing directories.
- An OAuth encryption key in the process environment does not protect against malicious approved host code. Local logout is not remote token revocation.
- Multi-file application is not an atomic filesystem transaction. Handled failures trigger rollback; a killed process or machine crash can leave partial changes that require backup-based recovery. Forge locks do not control editors or other external processes.
- Authorization to a connector does not guarantee the connector's implementation is safe or honest.
- Checkpoints cannot undo external writes, emails, deployments, payments or deleted remote data.
- `/undo` does not reverse `/apply` in the original project. No automatic commit, push, merge or deployment occurs.

## Safe usage

1. Start with a disposable, non-sensitive repository and a prepared container image.
2. Use least-privilege credentials for each connector; never give the model the credential value.
3. Read approval details, especially exact commands, host execution, endpoint and request body.
4. Do not use blanket tool grants for untrusted work.
5. Keep production deployments and irreversible writes behind separate human review.
6. Review patches and run your normal CI before merging.
7. Delete local sessions/notes when no longer needed, according to your data retention requirements.

No security audit, penetration test or production-readiness claim is made by the included unit/integration suite.
