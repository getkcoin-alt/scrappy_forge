# Scrappy Platform

Scrappy Platform is the single-project view of the Scrappy system without collapsing component boundaries.

## Why this is not a monorepo

The four components intentionally have different ownership and trust boundaries:

- **Scrappy OS** — execution control plane and authoritative machine/action audit.
- **Vault Zeta** — identity, continuity, objectives, durable experiences and promotion history.
- **Scrappy Forge** — primary terminal operator, coding runtime and evidence-driven evolution runtime.
- **Command Center** — human governance/read-model surface; never canonical truth.

Vault Zeta's portability contract is specifically model/runtime-neutral. Physically merging all code into one shared runtime would weaken that property and make independent deployment/recovery harder.

## One project, multiple components

`platform/scrappy-platform.json` pins the exact integration branch and commit for each component. A local workspace may materialize them as siblings:

```text
scrappy-platform/
  scrappy-os/
  vault-zeta/
  scrappy-forge/
  command-center/
  platform-state/
```

The operator interacts primarily through:

```bash
scrappy
```

Forge may inspect or modify any component when Git/capability policy permits, but component actions remain attributable to their owning subsystem.

## Unified product loop

```text
human objective
  -> Scrappy terminal
  -> capability discovery
  -> Vault context/objective
  -> Scrappy OS action/verification
  -> Vault experience
  -> Forge research/experiment/candidate
  -> superior evaluator
  -> human governance
  -> controlled promotion
  -> observed result
  -> durable learning
```

## Rules

1. SYNCBOND v5 is the cross-component event contract.
2. Repositories may use different runtimes and deployment targets.
3. No raw secret is transported in SYNCBOND/Vault bundles/model context.
4. A component must not silently assume another component's authority.
5. Every cross-component objective uses one correlation ID.
6. Forge can propose and build its own changes, but it cannot be its own final evaluator or approver.
7. Command Center renders and approves; it does not become the database of record.
8. Production merge/deploy remains a separate controlled executor.

## Migration strategy

Do not rewrite repository history or copy source into this directory. Treat this workspace as a composition layer. If a dedicated `scrappy-platform` repository is created later, this directory can move there unchanged and the four repositories remain independent components.
