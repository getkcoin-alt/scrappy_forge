# Production-readiness gates (Scrappy Forge local coding agent)

This checklist applies to the **local single-user CLI agent** and its **release-only Forge Hub**.
It does not certify the software as an unattended multi-tenant agent or regulated-finance service.
The public hub serves releases, documentation and optional account endpoints; it must **never**
execute users' tools, repositories, terminals or model keys.

## Reliability invariants

- Mission Controller persists every selected node as `running` with incremented
  attempt count **before** invoking any executor. If persistence fails, no operation starts.
- On restart, only side-effect-free, read-only work can be automatically rescheduled.
  Interrupted workspace edits, host commands and external writes remain `uncertain`
  until an operator or trustworthy reconciler checks the outcome.
- Cancelling an in-flight write is **not** proof it had no effect: it becomes `uncertain`.
- Node deadlines are **absolute Unix timestamps (seconds)**; expired nodes fail before
  dispatch, while an in-flight timeout follows normal retry/uncertainty policy.
- Idempotent execution must have a real idempotency guarantee. Do not label a write
  `idempotent` merely because retrying it seems convenient.
- Task graph persistence is local and single-controller. It is not an HA distributed
  scheduler, exactly-once processing guarantee, or an authorization policy on its own.

## Required gates before asking someone else to depend on it

1. **Code and CI:** Linux Python 3.11/3.12/3.13, macOS, lint, formatting,
   unit tests, terminal smoke, Docker build/readiness all green on the exact commit.
2. **Fault injection:** demonstrate killing the controller while an external action is
   in flight and verifying restoration enters `uncertain` rather than replaying it.
   Test read-only resume and explicit reconciliation separately.
3. **Live model run:** use least-privilege development credentials on a disposable repo.
   Prove file inspection, proposed changes, reviewed approval, verification, and refusal
   of a prohibited tool. Capture event evidence, not just model narration.
4. **MCP and security:** validate token/scope handling, OAuth callback, permission denials,
   command sandbox boundaries, prompt-injection cases, secret redaction and recovery.
   Never run unreviewed host-process plugins or connector code.
5. **Release integrity:** build and install the exact wheel, compare SHA256, test
   clean macOS/Linux installs, pin/sign build artifacts before claiming publisher
   authenticity. SHA256 from the same HTTPS service is not independent provenance.
6. **Hub operations:** verify `/readyz`, `/v1/status`, `/v1/releases/latest`,
   `/install.sh` and wheel download, Railway replica=1, alerting, rollback, and
   deployed commit. `/healthz` alone is not release readiness.
7. **Public accounts:** keep `FORGE_SIGNUP_ENABLED=0` until external auth,
   production SMTP, recovery, abuse controls, privacy/retention policy and live
   permission tests are complete. Current in-process rate limiting is not
   safe for a multi-replica public login rollout.
8. **Data handling:** verify owner-only filesystem permissions, disk backups,
   recoverability and documented retention/deletion. Local memory and transcripts
   are not encrypted by the application.
9. **Independent review:** commission a threat model review and penetration testing
   for privileged connectors and external writes before high-stakes production use.

## Reproducible verification

```bash
python -m pip install -e '.[dev,auth,server]'
ruff check src tests examples validation
ruff format --check src tests examples validation
pytest -q
python validation/terminal_smoke.py
python validation/release_smoke.py
docker build -t scrappy-forge:verification .
docker run --rm -p 8080:8080 scrappy-forge:verification
```

Review GitHub Actions jobs from the feature branch and from the merged commit.
A CI success is necessary but not a substitute for a real model, real connector,
failure-injection exercise and outside security review.

## Founder demo acceptance

In a throwaway repository, start a mission that reads code, proposes a constrained
patch, requests permission, runs checks and explains the evidence. Demonstrate a
denied action, an injected/untrusted instruction, and an interrupted external
operation being held for reconciliation. Show the task states, audit events and
the exact verification result. Never call a mock provider response a live deployment.
