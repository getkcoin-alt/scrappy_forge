# Phase A — Repository audit and architecture report

Date: 2026-11-15 · Scope: the four repositories attached to this work, plus a
read-only inventory of the operator's server (`nfc`). Nothing was changed outside this
document.

**Method.** I read the module structure, docstrings and key code paths of every
repository. For `scrappy_forge` I also used the test suite, the live evaluation and the
PostgreSQL runs from earlier phases. I counted the tests in the other three repositories
but did not run them; their pass/fail state is unverified here. The server facts come
from a read-only session run on the server (2026-11-15).

---

## 1. What exists: four repositories, three of them overlapping

| Repository | What it is | Maturity | Role in the zero-cost architecture |
| --- | --- | --- | --- |
| `scrappy_forge` (this repo) | Forge, a terminal coding agent (`scrappy_forge/`), plus Scrappy Engine (`scrappy_engine/`), the durable mission system built in SE-001 | 208 test functions (266 test runs with backend parametrisation), CI, two live evaluations | **Home of the engine.** All Phase B–G work lands here. |
| `-scrappy-os` | Linux control plane. Typed tools, policy, approvals, audit, event bus, Ollama and OpenAI-compatible providers | v0.2, 470 test functions | Source of proven patterns (provider contract, event bus, health checks). Not a runtime dependency. |
| `itsmay` | **Production** personal AI operator (FastAPI on Railway, Groq, Postgres + pgvector, Redis, voice). Also the fully local **Mini AI** companion | 554 test functions, deployed | Has the only working Redis, pgvector and local-embedding code. **Production: no changes from engine work.** |
| `vault-zeta` | Continuity library: memory with provenance, event journal, mission snapshots (SQLite) | Alpha, 5 test functions | Memory-model reference. Forge's `vault_memory.py` already embodies the same rules. |

**Decision: keep them separate; do not merge or rewrite.** The engine reuses ideas
and, where licensing and coupling allow, code. It does not take runtime dependencies on
`itsmay` (production) or `-scrappy-os` (a different product boundary).

---

## 2. Audit by area

Evidence is cited as `repo:path`. "Engine" means `scrappy_forge:src/scrappy_engine/`.

### 2.1 Agent architecture
- **Engine:** six roles (architect, engineer, debugger, reviewer, QA, director) in one
  sequential state machine. Model roles return JSON-Schema-validated proposals and never
  execute anything. Separation of duties is enforced by the policy engine and the
  acceptance gate (`runner.py`, `autonomy.py`, `contracts.py`).
- **Also present:** `scrappy_forge:specialist_runtime.py` (budgeted specialist runs);
  `-scrappy-os:agents/{brahma,vishnu,mahesh}.py` (plan → review → recover, same
  "agents cannot execute" rule); `itsmay:core/agents/experts.py` (sub-agents exposed as
  tools).
- **Weakness:** one agent at a time. Analysis, research and review never run in
  parallel, even when independent.

### 2.2 Provider adapters and routing
| Where | Adapters | Routing |
| --- | --- | --- |
| Engine `intelligence.py` | `ChatJSONProvider` over Forge `ChatProvider`; `ScriptedProvider` | Per-role preference, failover, review diversity across all writer families, performance ranking from durable `agent_runs` |
| Forge `providers.py` | **OpenRouter `:free` only, or a loopback URL only** | — |
| Forge `model_router.py` + `reasoning_policy.py` | — | Capability, context, privacy, latency, cost and reliability scoring. **Not wired into the engine.** |
| `-scrappy-os:models/` | Ollama (native), OpenAI-compatible, mock; health checks | `ModelRouter.for_role` stub (same provider for every role) |
| `itsmay:core/brain/llm.py` | OpenAI-compatible streaming (Groq default), Ollama | None; one configured model, multi-key rotation (see §3) |

**Weaknesses:**
- **No direct adapters for Groq, Gemini, Cerebras or Hugging Face.** The engine reaches
  models only through OpenRouter or a local server, because Forge's provider hard-codes
  those two.
- **No quota awareness.** Rate-limit headers are ignored, and a 429 counts as an ordinary
  failure.
- **No circuit breaker.** Failure counts never decay, so a provider that recovers stays
  penalised in-process.
- **Timeouts too short for CPU inference.** Forge uses `(10, 50)` s; prefill alone for a
  20k-token brief on this server's CPU takes minutes.
- **`trust_env=False`** in Forge's HTTP client ignores `HTTPS_PROXY`. The live-eval harness
  works around it; production code would need an explicit, per-provider network setting.
- **Token usage is discarded.** Only `usage.cost` is read, so tokens per mission are not
  measured.

### 2.3 Model configuration
- **Engine:** providers are built in code (`cli.py:build_router`, or the eval harness). No
  declarative model catalogue, no per-provider limits, no health state.
- **Forge:** `config.py` holds one provider and one model (JSON config).
- **`itsmay`:** `pydantic-settings` environment config for model, keys, embeddings
  (`fastembed` `BAAI/bge-small-en-v1.5`, 384 dimensions) and the Ollama host.
- **Weakness:** there is no single place to declare "provider X, models Y, limits Z,
  privacy class P". Phase C needs one: a validated YAML/JSON catalogue with
  hand-entered, dated quota facts.

### 2.4 Task orchestration and mission lifecycle
- **Engine:** a 12-state durable state machine, one committed transition per step,
  bounded repair loop, acceptance gate, idempotent creation, cancellation, crash recovery
  via `recover()` (`runner.py`, `states.py`, `store.py`).
- **Forge (unused by the engine):**
  - `task_graph.py`: DAG with idempotency and side-effect classes.
  - `scheduler.py`: batches by declared resources and mutation conflicts, with
    `max_concurrency`.
  - `mission_controller.py`: steering events (pause, constraint change, correction) that
    preserve verified work.
- **Weakness:** the engine's mission is a single linear plan, with no sub-task graph.
  The pieces for parallel sub-tasks already exist in Forge and are tested, but nothing
  connects them.

### 2.5 PostgreSQL and Redis
- **PostgreSQL:** the engine has a tested store on both SQLite and PostgreSQL. It has
  versioned migrations, an advisory-lock writer, an append-only hash-chained audit log
  and a CI job. `itsmay` has a separate asyncpg + pgvector schema (`infra/migrations/001–004`).
- **Redis:** **the engine has none.** `itsmay:core/util/redis_pool.py` uses Redis only as an
  optional accelerator (cooldowns with TTLs) and falls back to memory, which is the right
  stance. `-scrappy-os:core/events.py` is an in-process bus designed so a Redis Streams
  backend can be added without a rewrite.
- **Weakness / decision:** the brief asks for "Redis-backed task queues". The SE-001 rule
  is that Redis is never the system of record.
  - **Recommendation:** use a PostgreSQL work queue (`FOR UPDATE SKIP LOCKED`) as the
    durable queue.
  - Redis stays optional, for rate-limit counters, a result cache and pub/sub when
    present.
  - This avoids running a new service on a memory-constrained server and keeps a Redis
    restart harmless.
  - The server inventory did not check for a running Redis; Phase D will.

### 2.6 Memory and context management
- **Engine:** none yet.
  - Briefs are assembled by `_prioritise`, a filename-mention heuristic, and capped at
    `MAX_BRIEF_BYTES = 80_000` (≈20k tokens).
  - There is no symbol index, no summaries, no cache, and no token budget enforced per
    provider context window.
- **Available for reuse:**
  - `scrappy_forge:code_intelligence.py` provides `PythonCodeIndexer` and `CodeGraph`:
    deterministic AST symbols, imports, calls, neighbourhood queries and an LSP-fact seam.
  - `context.py:TokenBudget` gives calibrated token estimates.
  - `vault_memory.py:VaultMemory` provides hybrid memory with source fingerprints, stale
    exclusion and feedback.
  - `itsmay` has a fastembed (local ONNX, no API key) embedding pipeline with pgvector.
- **Weakness:** this is the largest gap for a CPU or free-tier world. Every model call
  re-sends large, mostly irrelevant context. It costs quota, it costs minutes of CPU
  prefill, and on free tiers it causes truncation.

### 2.7 Concurrency
- **Engine:** none inside a mission. The API runs each mission as one `asyncio` task in the
  API process, with no global limit.
- **`itsmay`:** a thread-safe key-lease pool and an `asyncio.Queue` worker bridge.
- **Forge:** a resource-aware `Scheduler` (unused).
- **Weaknesses:**
  - There are no per-provider concurrency limits, so ten missions would open ten parallel
    calls to the same free endpoint.
  - There is no RAM guard. Local inference could be started on top of the Mini AI virtual
    machine.
  - There is no backpressure and no request deduplication.

### 2.8 Logging, retries and failure handling
- **Strong:**
  - An append-only, hash-chained audit log of every state change, tool call and agent run.
  - Uncertain-outcome marking on crash.
  - Bounded repair loop, provider rounds, budgets.
  - Proven in the live run: 0/6 completed, with no false completions.
- **Weak:**
  - No structured operational logging and no metrics. There are no counters or latency
    histograms, and no CPU or RAM sampling.
  - Retries make no distinction between 429, 5xx, timeout and schema failure. Each needs
    different handling: wait for the quota reset, back off, retry elsewhere, or re-prompt.
  - No circuit breaker.

### 2.9 Tests and deployment
- **Tests:**
  - Engine: 20 tests × 2 store backends.
  - Forge: 208 test functions.
  - Live evaluation harness: `validation/engine_live_eval.py`.
  - The other repos have test suites, but I did not run them in this audit.
- **Deployment:**
  - The engine has a loopback-only Postgres compose file and runs on the host.
  - The engine has no service unit, no worker process and no dashboard.
  - `itsmay` deploys to Railway and to the Linux host via CI.

---

## 3. Compliance risks found

1. **`itsmay:core/util/keypool.py` is built to rotate several free-tier keys of one vendor
   past an exhausted quota.** Its own docstring gives "several Groq free-tier keys" as the
   example.
   - If those keys belong to separate accounts, this circumvents the provider's per-account
     limits. That conflicts with this mission's rules and almost certainly with the
     provider's terms.
   - If they belong to one organisation, rotation gains nothing, because Groq applies limits
     per organisation.
   - **The engine will not adopt multi-key rotation.** It will use one documented quota per
     provider account, and wait on 429.
   - I have not changed `itsmay`. It is in production, and that decision is yours.
2. **"5 OpenRouter keys / 5 Grok keys" (your message).** The same rule applies. One account
   per provider, within its documented limits.
3. **Remote Control runs as `root` on `nfc`.** Sessions started there have full control of
   a server that also hosts production virtual machines (`mini-ai-inference`,
   `thunder-workspace`) and the `vehicleapi` service. I recommend a dedicated
   unprivileged `scrappy` user before any Phase D worker runs there.

---

## 4. Server facts that constrain the design (measured 2026-11-15)

| Fact | Value | Consequence |
| --- | --- | --- |
| CPU | 1 × Xeon Gold 6138, 20 cores / 40 threads, AVX-512 (no VNNI, no AMX), single NUMA node | AVX-512 builds of llama.cpp or Ollama; no int8 acceleration |
| Memory | 128 GB DDR4-2400, **2 of 6 channels populated** (2 × 64 GB) | About 38 GB/s theoretical bandwidth. That caps CPU token generation, so models with roughly 3B active parameters are the realistic class |
| Free RAM | ~71 GB available | Virtual machines hold 56 GB: `mini-ai-inference` (40 GB, 36 vCPUs) and `thunder-workspace` (16 GB, 8 vCPUs) |
| CPU contention | Load average 0.14 now; 44 vCPUs allocated to virtual machines on a 40-thread host | Local inference must be capped (threads and RAM) so it cannot starve production virtual machines |
| GPU | None (ASPEED onboard graphics only) | GPU is a future enhancement, not a dependency |
| Disk | 669 GB free of 1.5 TB | Room for model files |
| Software | Ollama installed (not listening on the host); Docker installed; Hugging Face reachable | No new runtime needs installing |
| Unknown | What runs inside `mini-ai-inference`. It is probably Mini AI's Ollama (`itsmay:docs/mini.md`) | Could be reused as an endpoint instead of loading a second copy. Needs your confirmation |

---

## 5. Architectural weaknesses, ranked by impact on a ₹0, CPU-only system

1. **Context bloat.** Up to 80 KB per call, mostly irrelevant. *(Fix: Phase E, using the
   Forge indexer.)*
2. **No quota or rate-limit model.** Free tiers are exhausted, then 429s are retried blindly.
   *(Fix: Phase C.)*
3. **Only OpenRouter and loopback endpoints are reachable.** *(Fix: Phase C, a generic
   OpenAI-compatible adapter with catalogue entries for each provider.)*
4. **Sequential agents, no global concurrency or RAM control.** *(Fix: Phase D, a
   PostgreSQL queue, per-provider semaphores and a resource guard.)*
5. **No result cache or request deduplication.** Identical briefs are paid for twice.
   *(Fix: Phase E.)*
6. **No metrics.** There is no measured basis for "which model is best for each task".
   *(Fix: Phase F/G, the dashboard you described.)*
7. **Forge routing intelligence unused.** `ModelRouter` capability, privacy and cost scoring
   sits idle. *(Fix: Phase C.)*

---

## 6. What will be reused (not rebuilt)

| Need from your brief | Existing component | Action |
| --- | --- | --- |
| Capability / context / latency / privacy routing | `scrappy_forge:model_router.py`, `reasoning_policy.py` | Wire into engine `ProviderRouter` |
| Historical performance | Engine `store.provider_stats()` | Extend with latency, tokens, error classes |
| Symbol and dependency index | `scrappy_forge:code_intelligence.py` | Drive relevant-file selection |
| Token budgeting | `scrappy_forge:context.py:TokenBudget` | Enforce per-provider context windows |
| Parallel sub-tasks with conflict control | `scrappy_forge:task_graph.py`, `scheduler.py` | Run independent analysis and review in parallel |
| Long-term memory with provenance | `scrappy_forge:vault_memory.py` | Mission summaries and failure memory |
| Local embeddings | `itsmay`'s fastembed pattern (`bge-small-en-v1.5`, 384 dimensions) | Re-implement in engine (no cross-repo import) |
| Optional Redis accelerator | `itsmay:core/util/redis_pool.py` pattern | Same "accelerator, never required" stance |
| Ollama adapter and health check | `-scrappy-os:models/ollama.py` pattern; OpenAI-compatible `/v1` of Ollama | Generic adapter covers it |
| Durable store, audit, gate, evals | Engine (SE-001) | Keep unchanged |

---

## 7. Proposed plan for Phases B–G

- **B: provider research.** Build a capability matrix from providers' *current published*
  documentation: limits, context, models, tool calling, data retention, commercial-use
  terms and region. Every quota row is labelled "documented" or "measured", with a date.
  Live checks (one cheap call per provider) happen only where you add a key.
- **C: adapters and routing.**
  - A provider catalogue (validated file).
  - One generic OpenAI-compatible adapter: base URL, key from an environment variable,
    timeouts per provider type.
  - A quota ledger: requests and tokens per window, updated from response headers, and
    a 429 means waiting until the reset.
  - A circuit breaker with decay.
  - A task classifier that routes by role, size and criticality.
  - Wire in `ModelRouter`.
- **D: workers and resources.**
  - A PostgreSQL `SKIP LOCKED` job queue and a worker process.
  - Per-provider semaphores.
  - A RAM/CPU guard (refuse local inference below a free-memory floor; cap threads).
  - Priority, deduplication and backpressure.
  - Optional Redis.
- **E: context.**
  - Indexer-driven file selection.
  - A compressed mission brief with a fixed schema, cached by content hash.
  - Persistent per-mission summaries.
  - A token budget per provider context window.
  - Local embeddings (optional).
- **F: evaluation and failure testing.**
  - A metrics schema and recorded runs.
  - Controlled same-task model comparisons.
  - Fault injection: 429 storms, timeouts, a provider going dark mid-mission, memory
    pressure.
  - A security review of the adapters (key handling, no prompt/data leakage to logs).
- **G: deployment and dashboard.**
  - A systemd unit (unprivileged user).
  - Local model setup with thread and RAM caps.
  - The operations runbook.
  - The read-only dashboard: provider health, quota remaining, local model speed, CPU/RAM,
    workers, success rate, ₹ per mission (target 0), best model per task.

**Out of scope with ₹0 (optional, flagged in Phase B):**
- Persistent free GPU hosting. Colab and Kaggle are interactive or batch environments and
  their terms do not cover serving an API.
- Any paid credits.
- The memory upgrade from §4.

---

## 8. Decisions needed before Phase C

1. **`mini-ai-inference` VM.** Is it Mini AI's Ollama, and may the engine use it as an
   endpoint, with a concurrency limit so Mini AI keeps priority?
2. **Accounts.** Which providers do you hold *one* account for and are willing to use
   (Groq, Gemini, Cerebras, OpenRouter, Hugging Face)? Keys stay on the server as
   environment variables or in this cloud environment's secret store, never in git or chat.
3. **Unprivileged user on `nfc`.** May I create `scrappy` (no sudo) for the engine worker,
   instead of running as root?
4. **Data policy.** Several free tiers may train on prompts (as seen on OpenRouter). Default
   proposal: such providers are allowed only for public or synthetic code, and private
   repositories route to local models or no-training providers. Confirm or adjust.
