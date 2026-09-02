# Omni-City v0

Omni-City v0 is the first integration benchmark for the Scrappy Continuity Architecture.

It is **not a physical-city controller**. It is a deterministic, machine-readable simulation used to measure whether an agent can understand state, respect preconditions, choose actions, recover from bad plans and satisfy goals under resource constraints.

## Why it lives in Forge

Scrappy Forge owns experiments, benchmarks and evidence. The simulator therefore starts here rather than becoming another product/repository. Once the benchmark contract is stable, Scrappy OS can expose real-world observations using compatible world-model concepts without inheriting simulated authority.

## Current domains

- `water_continuity.json` — failed primary pump, backup activation and hospital supply continuity.
- `hospital_power.json` — failed grid feeder, backup generator and vaccine cold storage.
- `logistics_road_closure.json` — closed road, medical vehicle reroute and shipment delivery.

## Episode contract

A scenario contains:

- stable entities and relations;
- explicit initial attributes;
- goals with weights;
- available simulated actions;
- action preconditions;
- deterministic effects;
- action costs and an episode budget.

The environment returns:

- a structured world snapshot;
- `simulated: true` on observations and events;
- provenance scoped to `simulation_only`;
- a content-derived snapshot ID;
- an audit event for every accepted action;
- final weighted goal score, spend and evidence.

Unknown actions are rejected. Failed preconditions are rejected. Budget violations are rejected. The simulator never invents a capability to rescue a plan.

## Baseline sequences

```text
water-continuity-001
  isolate-primary -> activate-backup

hospital-power-001
  isolate-feeder -> start-generator -> restore-coldstore

logistics-road-001
  reroute-bypass -> complete-delivery
```

Each reference sequence reaches a goal score of `1.0`. They are correctness fixtures, not intelligence baselines.

## What we measure next

Agent evaluations should add metrics around the simulator rather than altering the simulator to favor a model:

1. goal completion rate;
2. invalid-action rate;
3. precondition violations;
4. budget efficiency;
5. number of planning/replanning steps;
6. recovery after injected state change;
7. uncertainty calibration when observations are incomplete;
8. cross-domain transfer across held-out scenarios;
9. model requests/tokens/cost;
10. human intervention rate.

The held-out benchmark set must remain separate from prompts and training memories. Passing examples seen in context is not evidence of generality.

## Planned progression

### v0.1 — Agent harness

Expose `observe`, `available actions`, `apply simulated action` and `score` through bounded Forge tools. Run a fixed model/budget and store a signed evaluation report.

### v0.2 — Dynamic events

Inject deterministic mid-episode changes: second-order failures, stale observations, sensor disagreement and resource contention.

### v0.3 — Multi-agent comparison

Only after a single agent has a measured baseline, compare planner/verifier separation against the same held-out scenarios.

### v1 — Scrappy Continuity integration

Feed simulator observations through SYNCBOND, let Vault retrieve relevant prior experience, let Scrappy OS-compatible policy semantics gate proposed actions, and let Forge score the episode.

### Later — Kinetic Mesh

Real infrastructure adapters must remain explicitly distinct from simulation. A simulator action never implies authority over a physical system.
