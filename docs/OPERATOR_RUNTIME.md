# Scrappy Operator Runtime

`Scrappy` is the primary terminal interaction surface. `Forge` remains the coding/execution harness underneath it.

```text
human -> scrappy terminal -> Forge engine -> policy -> tools/MCP/connectors -> evidence
                              |                         |
                              +-> Vault continuity      +-> Git/Railway/cloud/etc.
                              +-> self-change candidate
                                        |
                                        v
                               independent evaluators
                                        |
                               approve/decline/evidence
```

## Product boundary

- `scrappy`: primary operator command and governance shell.
- `forge`: backward-compatible coding-agent CLI and execution engine.
- Scrappy OS: machine/world authority when delegated through typed capabilities.
- Vault Zeta: durable continuity and evidence archive.
- Command Center: mission-control/read-model/approval UI, not the primary conversation surface.

## Permission classes

**GREEN** — ordinary built-in reads. Autonomous in normal mode.

**AMBER** — built-in edits and memory mutations. May be delegated by explicit user policy such as `accept-edits`.

**RED** — execution, external tools/MCP, protected operations, authority expansion, production changes and self-promotion. Requires explicit governance unless a narrower user-owned policy is introduced later.

Existing tool risk remains the source primitive; the permission class is an operator-facing interpretation, not a replacement security mechanism.

## Capability requests

When Scrappy cannot complete an objective because a required resource is absent, it should create a capability request rather than hallucinating access or requesting a raw credential in model-visible chat.

Examples: proxy service, third-party API, Git provider, Railway project, database, browser, payment provider, custom MCP.

Capability records use opaque handles such as:

```text
capability://mcp/github
capability://proxy/webshare/default
capability://railway/project/example
```

Secrets stay in OS keychains, environment references, OAuth/token vaults or external secret managers. The model should normally receive the capability handle and scoped metadata, not the secret value.

Initial terminal commands:

```text
scrappy capability list
scrappy capability request KIND SCOPE REASON [--provider NAME] [--no-secret]
```

Configured MCP servers appear as available capability handles. Pending requests are stored locally under the Forge state directory.

## Self-modification

Forge may inspect and modify its own repository inside the same isolated-workspace/checkpoint/test flow used for other projects. A self-change is not self-approved.

A candidate must pass deterministic tests/benchmarks and independent evaluator review. Evaluators are replaceable cognitive resources and carry no merge/deploy authority.

The initial aggregation rule is conservative:

- default minimum two confident approvals;
- a confident decline vetoes;
- mismatched candidate hashes fail closed;
- insufficient evidence returns `needs_more_evidence`;
- evaluator output never grants execution, merge or deployment authority.

Example evaluator input can be checked with:

```text
scrappy evaluator aggregate reviews.json
```

## Connector direction

MCP is a transport/interoperability layer, not a permission bypass. GitHub, Railway, Supabase, Vercel, Cloudflare, browsers, databases and future providers should enter through scoped capabilities with independent policy/audit enforcement.

The long-term terminal loop is:

```text
objective
-> inspect world/repo
-> discover required capabilities
-> request missing capability if necessary
-> plan
-> policy gate
-> act
-> observe
-> repair
-> verify
-> remember evidence
-> if self-change: benchmark + independent review
```
