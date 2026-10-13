# Scrappy Forge — self-hosted VPS deployment (no Railway)

This repository deploys only the **Forge Hub** release/API server to a user-owned
Linux VPS. Agent inference, project files, API keys, task execution and memory
stay **local to the CLI operator**. The server distributes a release wheel
and optionally proxies managed accounts. No public cloud coding executor exists.

## Target

| Setting | Value |
| --- | --- |
| VPS host | `31.97.227.171` |
| SSH bootstrap user | `root` (one-time only) |
| Continuous deploy user | `forge-deploy` |
| Canonical temporary hostname | `31-97-227-171.sslip.io` |
| Public HTTPS | Caddy on 80/443, **only when ports are free** |
| Internal release server | `127.0.0.1:18080` |
| Test candidate | `127.0.0.1:18081` |
| Deployment trigger | successful `tests` workflow on `main`, or manual workflow dispatch |

The free sslip.io hostname derives DNS from the embedded IP. It is convenient
for demos, but it is third-party DNS; use a domain you control for long-term
service identity. Verify DNS and inbound 80/443 before expecting HTTPS.

**Neither the repository nor this runbook contains an SSH private key.**
First-time setup must be performed by someone who can log into this VPS.

## 1. Create a dedicated SSH deploy key (on your workstation)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/scrappy_forge_deploy -C "scrappy-forge-github-actions"
```

Do not put this private key into the repository, terminal history, an issue,
or any chat. Keep a backup in your chosen password/secret manager.

## 2. One-time bootstrap (run from your workstation)

Clone this repository and use the bootstrap files from the reviewed `main`
branch. Verify you can sign in to the requested server with your existing
root credentials. If SSH is not listening on port 22, investigate VPS console,
IP and SSH port first; do not disable firewall rules indiscriminately.

```bash
scp ~/.ssh/scrappy_forge_deploy.pub root@31.97.227.171:/root/forge-deploy.pub
scp deploy/vps/bootstrap.sh root@31.97.227.171:/root/forge-bootstrap.sh
ssh root@31.97.227.171 \
  'bash /root/forge-bootstrap.sh /root/forge-deploy.pub 31-97-227-171.sslip.io'
ssh -i ~/.ssh/scrappy_forge_deploy forge-deploy@31.97.227.171 'docker info >/dev/null && echo DEPLOY_READY'
```

Bootstrap requires Debian/Ubuntu with apt and enough disk/RAM for Docker builds.
It installs Docker and creates the non-root `forge-deploy` SSH identity.
**Docker group membership is host-root equivalent:** this deploy identity is
trusted to deploy code from this repository. Protect `main` and SSH secrets.
The public hub image itself runs with a non-root UID and no Linux capabilities.

### If another app already uses 80/443

Bootstrap **does not stop or replace** other web servers. It leaves a
Caddyfile and reports that the public proxy must be configured manually.
If your VPS runs Nginx, adapt
`deploy/vps/nginx-site.conf.example` as a new server block, verify it
with `nginx -t`, reload without replacing unrelated sites, then install a
valid HTTPS certificate for the hostname. Never serve login/account endpoints
over plain HTTP or bypass certificate verification.

If 80/443 are free, bootstrap starts isolated Caddy with automatic HTTPS.
Its ACME issuance depends on correct DNS and inbound TCP 80/443.
See Caddy documentation: https://caddyserver.com/docs/automatic-https

## 3. Set GitHub Actions secrets

In `getkcoin-alt/scrappy_forge` →
**Settings → Secrets and variables → Actions**, create:

- `FORGE_VPS_SSH_KEY` (secret): exact contents of
  `~/.ssh/scrappy_forge_deploy`, including BEGIN/END lines.
- `FORGE_VPS_KNOWN_HOSTS` (secret): the server's SSH host public key
  in known_hosts format, e.g. `31.97.227.171 ssh-ed25519 AAAA...`.
  Obtain it from the **trusted VPS console** by reading
  `/etc/ssh/ssh_host_ed25519_key.pub` and prefixing the IP. Confirm
  the fingerprint from the VPS console, not a blind `ssh-keyscan`.

Optionally set an **Actions variable** `FORGE_DOMAIN` to a domain you control,
after configuring its DNS and public proxy. Default is
`31-97-227-171.sslip.io`.

Do not configure `SUPABASE_SECRET_KEY`, account signup or model API keys on
the public server for a release-only deployment.

## 4. Automatic deployments

`.github/workflows/deploy-vps.yml` is triggered by a successful
`.github/workflows/ci.yml` run caused by a push to `main`.
The workflow checks out the exact tested commit and refuses to deploy a
stale commit after a newer one has reached `main`.

Pipeline:

1. GitHub tests pass on `main`.
2. Source archived at the exact tested SHA and sent over pinned SSH.
3. VPS builds immutable `scrappy-forge:<SHA>` Docker image.
4. Candidate container passes `/readyz` on 127.0.0.1:18081.
5. Previous container is retained for rollback; new image is promoted to
   127.0.0.1:18080 and health-checked.
6. On failed promotion, previous container is restarted automatically.
7. Workflow verifies public HTTPS `/readyz` and `/v1/status` commit.

The promotion has a small bounded interruption; it is not a zero-downtime
multi-replica deployment. No automatic garbage-collection of previous images
is performed, so monitor disk use.

Use the **Deploy Forge Hub to VPS** Actions workflow's `Run workflow`
control for the first deployment after configuring credentials.
No deployment occurs while secrets/bootstrap/SSH are missing.

## 5. Verification and troubleshooting

```bash
# On VPS:
docker ps --filter name=scrappy-forge-hub
curl -fsS http://127.0.0.1:18080/readyz
curl -fsS http://127.0.0.1:18080/v1/status
docker logs --tail 100 scrappy-forge-hub
docker logs --tail 100 scrappy-forge-caddy

# From an external machine, after configuring HTTPS:
curl --fail https://31-97-227-171.sslip.io/readyz
curl --fail https://31-97-227-171.sslip.io/v1/releases/latest
```

`/healthz` is liveness, whereas `/readyz` validates the presence
of the distributable wheel. SSH failure prevents uploading bundles; inspect
`sshd`, firewall, cloud-provider security rules and the host-key fingerprint.
If Caddy cannot issue TLS, check A records and external ports 80/443.

The workflow **only deploys after passing CI**. Keep actual release validation,
security audit, external provider checks, and the
`docs/PRODUCTION-READINESS.md` launch gates separate. Local SQLite state,
OAuth, and executable connectors are not mounted or uploaded to this server.

## 6. Environment contract

| Variable | Meaning |
| --- | --- |
| `FORGE_PUBLIC_URL` | Canonical HTTPS origin provided to the container |
| `FORGE_GIT_COMMIT_SHA` | Exact image/source commit shown at `/v1/status` |
| `FORGE_RELEASE_DIR` | Wheel directory baked into Docker image, `/app/releases` |
| `FORGE_SIGNUP_ENABLED` | Forced to `0` by the production deployment |
| `PORT` | Internal ASGI listener, default 8080 |
| `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` | Optional, not configured in release-only mode |
| `SUPABASE_SECRET_KEY`, `FORGE_OWNER_SUBJECT` | Optional privileged account administration; not configured |

The backend uses `FORGE_PUBLIC_URL` and `FORGE_GIT_COMMIT_SHA`
regardless of hosting provider. No Railway variables, volumes, services,
billing account, or Railway CLI are necessary.

## Operational caveats

- Keep production SSH keys private and rotate if exposed.
- Protect `main`, require CI and review before merging untrusted changes.
- The public wheel distributes the Python package source by design.
- Caddy/sslip.io are external internet/TLS dependencies, not Railway.
- This is a single-server deployment without HA. Configure backups, monitoring
  and an offsite recovery procedure before promising high availability.
- Do not interpret a passed Docker health check as a security certification.
