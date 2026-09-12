# ADR-0001: Persistent execution substrate around the existing Engine

- Status: Accepted
- Date: 2026-09-13
- Decision owners: Scrappy Forge controller

## Context

Scrappy Forge already has the security properties that must remain authoritative: controller-owned permissions, isolated workspace copies, bounded checkpoints, durable event journaling, explicit authorization, verification tied to workspace tree hashes, MCP boundaries, secret isolation, and the rule that a model proposes actions while trusted code decides whether and how they execute.

The current `Engine` is intentionally bounded and mostly sequential. It persists a session, asks a provider for one response, executes proposed tool calls through `Registry`/`Policy`, journals observations, and lets the controller perform verification. Recovery treats interrupted tool calls as uncertain rather than replaying them. These properties are stronger than any model-level instruction and must not move into prompts or agent code.

The next runtime must support long-horizon missions, provider independence, task DAGs, asynchronous safe work, steering, hybrid memory, repository intelligence, bounded specialist agents, independent verification, and crash recovery without turning the model into a security boundary.

## Decision

Introduce the new runtime behind compatibility layers around the existing `Engine` instead of replacing it.

The target control hierarchy is:

```text
Operator
  -> Mission Controller
  -> Task Graph
  -> Agent Runtime
  -> Model Router
  -> Tool Scheduler
  -> Policy Engine
  -> Execution Kernel
  -> Verification Engine
  -> Evidence Ledger
```

Vault Zeta memory and the World Model are horizontal services. They provide information and observations, never authorization.

### Existing components that remain sovereign

| Existing component | New role |
| --- | --- |
| `Engine` | compatibility execution loop while Mission Controller is introduced incrementally |
| `Policy` | single authorization gate for tool risk, protected resources and explicit approvals |
| `Registry` | schema validation, tool metadata binding and invocation boundary |
| `Runner` | deterministic execution kernel for commands and verification |
| `Workspace` | isolated source copy, hash-guarded edits, snapshots and patch export |
| `Store` | durable local persistence/event journal, extended by additive schema only |
| `MCPManager` | connector boundary; MCP metadata stays untrusted |
| `Memory` | compatibility facade while hybrid Vault Zeta memory is introduced |
| `World` | observation graph; freshness remains explicit and actions must revalidate targets |

### New components

1. `model_router.py` owns provider/model capabilities and task-aware selection. It may choose a model, but cannot grant tool permission.
2. `reasoning_policy.py` chooses reasoning intensity from task complexity, uncertainty, verification failures and remaining budget.
3. `task_graph.py` owns durable mission node state and dependency semantics.
4. `scheduler.py` chooses which ready operations may run concurrently. It may never bypass `Policy` or `Registry`.
5. `steering.py` will append operator changes to the mission event stream and invalidate only affected DAG branches.
6. `verification_engine.py` will run evidence-producing checks independently of builder narrative.
7. `code_graph.py` will augment, not replace, the World Model with repository symbols and relationships.
8. `continuity.py` will reconstruct missions from durable DAG state and reconcile uncertain effects.

### Security boundary

The model router, planners, specialist agents, memory retrievers, context compiler and world/code graphs are all untrusted advisors from the controller's perspective.

Every consequential action still follows:

```text
model/agent proposal
  -> typed operation
  -> scheduler safety checks
  -> Registry schema validation
  -> Policy authorization
  -> checkpoint when required
  -> Execution Kernel
  -> observation/evidence journal
```

No memory item, model output, MCP description, repository instruction, world observation or specialist result can authorize an action.

### Concurrency rule

Concurrency is opt-in from typed operation metadata.

- Read-only operations with satisfied dependencies may run concurrently.
- Mutations with overlapping resource locks serialize.
- External side effects require explicit authorization and an idempotency classification.
- An operation with uncertain external outcome is not blindly retried. It transitions to `uncertain` until trusted reconciliation resolves it.
- Operator cancellation/supersession prevents not-yet-started work from running. Completed valid work remains intact unless a later branch explicitly invalidates it.

### Persistence and recovery

Mission DAG state is durable. Node states are:

`queued`, `ready`, `running`, `awaiting_input`, `blocked`, `uncertain`, `failed`, `verified`, `cancelled`, `superseded`.

The controller may reconstruct runnable state after a crash. Filesystem effects are reconciled by hashes/snapshots. External effects require idempotency keys or explicit reconciliation. At-least-once execution must not silently become duplicate side effects.

### Model independence

Provider state is not mission state. A failed or unavailable provider may be replaced without discarding the task graph, memory, evidence, workspace or event journal. Model capabilities are data, not branches hard-coded for vendors.

### Evaluation rule

No superiority claim is accepted from anecdotes. Before and after versions must be compared on the same representative corpus, comparable model/budget constraints and held-out acceptance tests. Machine-readable artifacts must record task completion, first-pass correctness, accepted patch rate, regressions, review time, wall-clock time, model requests, token use, tool calls, cost, recovery success and human interventions.

## Migration sequence

1. Establish benchmark artifacts and a baseline procedure.
2. Add model capability routing and reasoning policy without changing permission semantics.
3. Add `TaskGraph` and safe read-only scheduling.
4. Add steering and durable node recovery.
5. Replace lexical-only memory behind the existing facade with measured hybrid retrieval.
6. Add repository graph/LSP adapters integrated with World Model freshness.
7. Add bounded specialist roles using typed artifacts, not open-ended agent chat.
8. Add independent verification and comparative evaluations.

Each step must retain existing tests and introduce tests for the new invariants before the next step depends on it.

## Consequences

This deliberately duplicates some orchestration concepts during migration. That cost is accepted because a rewrite would put existing security and recovery invariants at risk. The architecture optimizes for deterministic control and evidence over novelty.

The brain is replaceable. Mission state, memory, evidence and deterministic execution remain controller-owned.