# Changes

## 0.4.0

- Added interactive masked model-key onboarding, fixed OpenRouter validation,
  environment-key precedence and explicit private-file persistence/removal.
- Added structured local files/processes/network/macOS-window observations,
  bounded graph queries/deltas, freshness and collector-coverage metadata.
  Screenshot fallback and a privileged event-driven OS agent are not implemented.
- Added an isolated terminal installer, public wheel/checksum distribution,
  non-root Railway container, readiness endpoint and deployment runbook.
- Added optional managed account login/signup/refresh/logout, verified identities,
  origin-bound CLI tokens and owner-only backend list/suspend/restore endpoints.
  User panel only; no admin frontend. Accounts fail closed until configured.
- Added user/plan/world-model documents and explicit account-launch gates.
- Retained and hardened the 0.3 loop, compaction, memory, verification/apply,
  connector management, OAuth and extension interfaces.

Tests include credential handling, scoped graph parsing/querying, permission
gates, account role forgery, refresh/logout and release endpoints. Scripted model
and identity fixtures verify mechanics, not real-model ability or live-provider
compatibility. See release verification for completed runtime checks. No 10x
performance, universal-plugin or independently audited security claim is made.

## 0.3.0

- Added terminal management of MCP servers, native plugins and versioned skills. Explicit `mcpServers` imports support stdio/HTTP, environment remapping and environment-bound header prefixes. Registration never installs or executes packages.
- Added MCP OAuth authorization-code sign-in with a local callback, PKCE/state checks and user approval. Memory-only credentials are the default; optional encrypted records preserve client/issuer metadata, expiry and refresh tokens across connections.
- Added encrypted-cache locks, local logout, guarded authorization origins and sanitized token-endpoint failures. Real-provider compatibility remains subject to testing.
- Added user-only application of verified text changes, reviewed-patch hashes, original-drift checks, backups, staged copies, rollback after handled failures and durable markers for interrupted applications.
- Added free/tool-capable model discovery and interactive model/permission switching.
- Excluded common client credential configuration files from source snapshots and tightened manifest/config validation.
- Retained the v0.2 loop, compaction, memory, lazy tools, MCP transports, command/REST plugins, checkpoints, resume and evidence reports.

Validation: 91 automated tests plus the installed-command PTY workflow. Model responses and the authorization issuer in integration tests are explicitly scripted fixtures. There is no measured productivity advantage or universal-plugin claim.

### Upgrading

Install this release into your existing environment or a fresh virtual environment. Back up your state directory and trusted config first. The v0.2 session data shape is retained, but session records contain absolute workspace paths; do not move the state directory casually. Unsupported configuration fields now fail validation instead of being ignored.

Model-upload and execution permissions remain invocation-owned. Configuration changes require a restart. A successfully applied session keeps its starting baseline, so start a new session for the next original-project change.

OpenRouter's zero model-price limit does not cover connector subscriptions, sandbox infrastructure or other external service charges. Review those separately.
