# Scrappy Forge — plans and availability

Release: 0.4 technical preview. These are capability plans, not a checkout page,
price commitment, service-level agreement, or claim that future features ship.

| Plan | Availability | What it includes |
| --- | --- | --- |
| Local BYOK | Implemented | Terminal loop, approvals, workspace copies, verification/apply, compaction, project memory, structured world graph, supported MCP/plugins/skills. Bring your OpenRouter key or local model. No Forge account needed. |
| Account beta | Backend implemented; disabled until configured | Verified email sign-in, profile, refresh/logout, owner-only account listing and suspend/restore. No cross-device source/session synchronization. |
| Team | Future; no price set | Shared policy, organization roles, centralized opt-in audit and usage controls require design, implementation and review. Not included today. |
| Managed execution | Future; no price set | Would require isolated workers, tenant boundaries, quotas, billing and explicit code-upload consent. Railway does not execute user projects today. |

There is no Forge payment flow in this preview. OpenRouter, model providers,
connectors, local compute and Railway may have their own quotas or charges.
The CLI never silently falls back from free OpenRouter routing to paid models.

## User and owner separation

Users run coding tools locally. Owner operations are backend-only and require
a freshly verified identity matching the configured immutable owner UUID.
User-editable profile metadata cannot grant owner access. There is deliberately
no admin frontend, arbitrary role-grant endpoint or public bootstrap-owner route.

Account suspension affects the hosted account service, not offline/local BYOK
coding. The owner has no built-in access to a user's files, API key, terminal,
local memory or world snapshots. Adding those capabilities would require a new,
explicitly consented design; they are not implicit in an account plan.

## Account launch gates

Before enabling public signup, the owner must:

1. Choose and configure the account provider. This implementation supports
   Supabase Auth; no project is provisioned by installing the CLI.
2. Create and verify the owner's account, then set its UUID in backend secrets.
3. Configure production email delivery, confirmation and redirect allowlists.
4. Finish password recovery and account deletion flows; these are not yet
   exposed by this CLI/login panel. Establish a support process meanwhile.
5. Add provider/edge abuse controls and decide CAPTCHA integration. Current
   per-process limits are only a single-instance safeguard, not DDoS protection.
6. Publish chosen license/terms, privacy/retention policy and support ownership.
7. Test real-provider login, confirmation, refresh, expiry and suspension, review
   security, and configure operational alerts and backups where needed.

Keep public registration closed until these gates are satisfied. There is no
deadline or invented price for unimplemented plans.

## What “10x better” would mean

Treat it as a measured goal, not a product fact. Evaluate representative tasks
with fixed model/budget, held-out checks, blinded patch review, completion rate,
regressions, review time, accepted changes per hour and cost. Compare the same
tasks against strong existing coding agents. Scripted integration fixtures only
verify mechanics; they do not establish intelligence or productivity superiority.
