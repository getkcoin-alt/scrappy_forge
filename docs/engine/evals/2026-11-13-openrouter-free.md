# Live evaluation — 2026-11-13, OpenRouter free models

First run of Scrappy Engine with real models (`validation/engine_live_eval.py`).
Raw results, including the patches, are in `2026-11-13-openrouter-free.json`.

**Result: 0 of 6 missions completed.** No mission reported false success: every
completion claim requires the held-out check, and none passed it.

## Setup

| | |
| --- | --- |
| Builder | `cohere/north-mini-code:free` |
| Reviewer | `baidu/ernie-5.5-flash-preview:free` |
| Fallback | `recursive/flint-v1.0:free` |
| Privacy | Forge default `data_collection: deny`. Larger free models (Nemotron 3 Ultra/3.5, Particle-1) return 404 under this policy and were excluded instead of weakening it |
| Tasks | `auth` (prefix-password bug), `pagination` (1-based off-by-one + page count), `duration` (implement parser). Each has a held-out check outside the repository |
| Budget | max 2 retries, 16 model calls per mission, 2 provider rounds |
| Runs | 2 per task, sequential |

## Outcomes

| Run | Task | State | Attempts | Model calls | Why |
| --- | --- | --- | --- | --- | --- |
| 0 | auth | BLOCKED | 1 | 10 | attempt 1 failed held-out check; no provider then returned a valid engineer reply |
| 0 | pagination | BLOCKED | 1 | 10 | same pattern |
| 0 | duration | BLOCKED | 1 | 11 | same pattern; one reply was truncated JSON |
| 1 | auth | FAILED | 3 | 12 | all 3 attempts failed the held-out check |
| 1 | pagination | BLOCKED | 0 | 9 | HTTP 429 from every provider |
| 1 | duration | BLOCKED | 0 | 6 | HTTP 429, then the account's free daily request quota was exhausted |

## What this shows

1. **The gates held.** Fixes that passed the repository's own tests but not the held-out
   check were rejected every time. Nothing was committed or reported complete.
2. **Free-tier endpoints are the bottleneck.** Most failed calls were "invalid or
   incomplete provider response" from Forge's strict parser. The parser did not record
   which check failed. It now does, so the next run will show whether these were
   `length` cut-offs, empty content from reasoning models, or something else. The rest
   were HTTP 429 and the daily free quota.
3. **Model quality on these tasks is unmeasured.** Too few valid engineer replies
   arrived to say how well these models fix bugs. This run measures the plumbing under
   hostile endpoints, not intelligence.
4. **Independence leak found and fixed.** Under failover the reviewer model also wrote
   code, and independence was checked only against the latest engineer. It is now
   checked against every model family that wrote code in the mission.

## Next run

A meaningful measurement needs a reliable endpoint: OpenRouter credits (which lift
the free daily cap), a paid model, or a local model. It also needs the 10–20 task set.
Then report completion rate and cost per completed mission per model.
