# Railway deployment and account activation

Deploy only `forge-hub`. It distributes the CLI wheel and serves the basic user
login panel. It never runs user code. No volume, database, model key or identity
provider is required for release-only operation. Do not set OPENROUTER_API_KEY
on Railway: model credentials belong on users' machines.

## Release deployment

1. Connect the private `getkcoin-alt/scrappy_forge` GitHub repository to Railway.
   Keep the repository private unless the owner explicitly changes that choice.
   The public wheel intentionally makes its distributable Python code public.
2. Create the `forge-hub` service from main in a dedicated project. The root
   Dockerfile builds the wheel, installs the backend extra and runs non-root.
3. Set Dockerfile path `Dockerfile`, health check `/readyz`, health timeout 180
   seconds and restart policy ON_FAILURE with 3 retries on the service. Keep one
   replica/worker; the account limiter is in-process. The root Dockerfile is
   automatically detected. These settings must be applied in Railway, not just
   written in documentation.
4. Generate a Railway HTTPS domain. RAILWAY_PUBLIC_DOMAIN determines public
   links; alternatively set FORGE_PUBLIC_URL to the exact HTTPS origin. Redeploy
   after changing it. Port comes from Railway's PORT variable (default 8080).
5. Keep FORGE_SIGNUP_ENABLED=0, with no identity secrets for release-only mode.
6. Verify /healthz and /readyz return 200, /v1/status reports release_ready=true
   and accounts_configured=false, /v1/releases/latest has the expected version,
   and the exact downloaded wheel matches its SHA256.
7. Verify /install.sh has no unresolved placeholders and /guide, /plans,
   /world-model and /login are reachable. Account endpoints should fail closed
   while unconfigured; /admin must not exist.

Deployment success is evidence of service availability, not a security audit,
model-quality benchmark or proof that account flows work with a real provider.
Test the installer on clean supported Macs before broad public rollout.

Do not add legacy railway.toml/railway.json files for a new service: Railway's
current documentation says new services cannot opt into Config as Code. For
future declarative management, link the dedicated project, run `railway config
pull`, inspect the generated .railway/railway.ts, then `railway config plan`.
Never apply a plan that deletes unrelated services or secrets. The current
deployment uses the connected Railway service-configuration API instead.

## Environment contract

| Variable | Release-only | Account-enabled purpose |
| --- | --- | --- |
| PORT | Railway-provided | HTTP listener inside container |
| RAILWAY_PUBLIC_DOMAIN | Generated domain | Canonical public origin when FORGE_PUBLIC_URL absent |
| FORGE_PUBLIC_URL | Optional HTTPS origin override | Same-origin guard, download and confirmation links |
| FORGE_RELEASE_DIR | Docker sets /app/releases | Exact built wheel directory |
| RAILWAY_GIT_COMMIT_SHA | Railway-provided | Release provenance, never an authorization signal |
| FORGE_SIGNUP_ENABLED | 0 | Set 1 only after launch gates below |
| SUPABASE_URL | Unset | Chosen project's HTTPS origin |
| SUPABASE_PUBLISHABLE_KEY | Unset | Supabase publishable/legacy anon API key |
| SUPABASE_SECRET_KEY | Unset | Backend-only privileged key for owner administration |
| FORGE_OWNER_SUBJECT | Unset | Verified owner's immutable Auth user UUID |

Never commit secrets or copy them into frontend scripts. Use Railway's secret
configuration. A partial identity configuration fails startup rather than
silently issuing insecure local sessions. The service does not decode a bearer
token and trust its self-asserted role: protected calls check /auth/v1/user.

## Activate accounts deliberately

This implementation supports Supabase Auth, but no account project is
provisioned automatically. The owner must select the project and region.

1. Read the current [Supabase API-key guidance](https://supabase.com/docs/guides/api/api-keys)
   and [session behavior](https://supabase.com/docs/guides/auth/sessions).
2. Configure email/password Auth, required email confirmation, exact site URL
   and /login redirect allowlist. Disable anonymous users. Do not rely on
   user_metadata for privileged roles; this backend ignores it.
3. Configure [production SMTP](https://supabase.com/docs/guides/auth/auth-smtp).
   The default development mail service is not a public-signup delivery plan.
4. Create/confirm the owner's account via the provider's controlled workflow.
   Obtain its UUID from the provider dashboard. Set FORGE_OWNER_SUBJECT to that
   UUID; there is no first-user-wins or public bootstrap endpoint.
5. Set the provider URL and keys. Test normal login and owner login separately.
   Validate the selected key type against actual Auth admin endpoints; mock
   transport tests do not establish live-provider compatibility.
6. Complete password recovery/account deletion UX, abuse controls/CAPTCHA,
   chosen license/terms/privacy/retention/support policy and security review.
   These are not included by merely setting environment variables.
7. Test email confirmation, bad password, refresh rotation, token expiry,
   logout, user denial of admin APIs, owner list/suspend/restore and suspended
   access. Review the provider's refresh-token reuse/expiry settings.
8. Only then opt into FORGE_SIGNUP_ENABLED=1. Verify the live status explicitly.

## Owner backend (no admin frontend)

Sign in using your verified owner account, not a service-role key in the CLI:

```sh
forge account login
forge account me
forge account users
forge account suspend USER_UUID
forge account restore USER_UUID
```

Suspend/restore require a terminal confirmation. The API accepts only the
specific boolean suspension action, cannot suspend its configured owner and
does not let users change roles. User listing is paginated in the backend.
There is no remote access to local source, keys, graphs, memory or execution.
Disabling an account does not disable local/offline BYOK coding.

## Verification and rollback

```sh
python -m pip install -e '.[dev,auth,server]'
ruff check src tests examples validation
ruff format --check src tests examples validation
pytest -q
python validation/terminal_smoke.py
python validation/release_smoke.py
```

CI additionally runs Python 3.11/3.12/3.13, a macOS job and a Docker readiness
check. A configured workflow is not evidence that those hosted jobs passed.
The terminal smoke uses scripted model/OAuth fixtures with real edits/checks.
The release smoke builds and installs the wheel into an isolated tool directory,
then exercises the installed commands and a real localhost HTTP service.

Use Railway's previous known-good deployment to roll back the release service;
do not reset Git history. Record the release version, commit and checksum. A
backend rollback does not downgrade already-installed CLIs; users choose a
verified previous wheel explicitly. Back up local state before client upgrades.
There are no application database migrations in this release. If accounts are
later activated, separately preserve provider configuration and recovery plans.

Disable signup immediately if account abuse or an auth issue is discovered.
Rotate compromised keys at their issuing provider, update backend secrets,
redeploy and verify; deleting a local token record alone is not revocation.

Sources: [Railway configuration](https://docs.railway.com/infrastructure-as-code),
[Railway health checks](https://docs.railway.com/deployments/healthchecks),
[uv tool installation](https://docs.astral.sh/uv/guides/tools/).
