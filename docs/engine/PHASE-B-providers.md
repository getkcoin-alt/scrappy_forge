# Phase B — Free inference capability matrix

Researched 2026-11-15 from each provider's **own current documentation**, fetched from this
session. Provider limits change often, and several providers now publish account-specific
limits only in their consoles. Every number below is labelled:

- **doc**: stated on the provider's page, with the URL in Sources.
- **measured**: observed from this project's own account or server.
- **unverified**: only third-party sources found. Treat these as unknown until checked in
  the console.

Policy applied, as approved in Phase A:
- One account per provider.
- No key rotation to get around limits.
- Private repositories go only to providers that do not train on prompts, or to local
  models.

## 1. Verdict summary

| Provider | Free at ₹0 today? | Use in Scrappy Engine | Private code allowed? |
| --- | --- | --- | --- |
| **Groq** | **Yes**, permanent free plan (doc) | **Primary hosted coder and reviewer.** Fast; small token-per-minute budget | **Yes**. Does not retain inference data by default (doc) |
| **Cloudflare Workers AI** | **Yes**, 10,000 neurons/day (doc) | Secondary coder, embeddings, overflow | **Yes**. Zero retention, no training (doc) |
| **OpenRouter `:free`** | **Yes**, **50 requests/day** on this account (measured) | Tertiary. Model diversity for review | Only to providers that honour `data_collection: deny`. The larger free models refuse it (measured) |
| **Google Gemini API** | **Yes**, free tier per project; per-model numbers shown only in AI Studio (doc) | **Public or synthetic code only**: research, docs, critiques of non-private code | **No**. Free-tier prompts may be used to improve products and read by human reviewers (doc) |
| **Mistral La Plateforme** | Free "Experiment" mode exists; limits only in the console (doc) | Optional, after you opt out of training in the console | **Only after opting out**. Free mode trains on data by default (doc) |
| **SambaNova Cloud** | Free tier when no payment method is linked; RPM/RPD/TPD limits (doc) | Optional extra coder once the per-model numbers are read from the console | Not verified; needs a check of their data policy |
| **Cerebras** | **No**. Only a $5 trial, 30-day expiry, needs a verified payment method (doc) | **Excluded** under ₹0 | — |
| **Hugging Face Inference Providers** | **No**. Free users get no monthly credits (doc) | **Excluded** | — |
| **GitHub Models** | **No. Retired 30 July 2026** (doc) | **Excluded** | — |
| **NVIDIA API catalogue** | Page not machine-readable from here | Not evaluated | — |
| **Local Ollama on `nfc`** | Yes. Hardware you already own | **Always-available floor.** Never rate-limited | **Yes**. Data never leaves the server |

## 2. Hosted providers in detail

### Groq (free plan, limits per organisation, doc)
| Model | RPM | RPD | TPM | TPD | Context | Max output |
| --- | --- | --- | --- | --- | --- | --- |
| `openai/gpt-oss-120b` | 30 | 1,000 | 8K | 200K | 131,072 | 65,536 |
| `openai/gpt-oss-20b` | 30 | 1,000 | 8K | 200K | 131,072 | 65,536 |
| `qwen/qwen3.8-27b` | 30 | 1,000 | 8K | 200K | see console | see console |

- **Limits apply to the organisation, not to users or keys** (doc). Extra keys add no
  capacity.
- **Cached tokens do not count** towards limits (doc), so a stable brief prefix helps.
- **Rate-limit headers** report the remaining requests and tokens. Phase C's quota ledger
  will use them.
- **The binding constraint is 8K tokens per minute.** One 20k-token brief (today's
  engine) cannot be sent at all. Briefs must be under about 6K tokens. Phase E is a hard
  prerequisite for Groq.
- **Daily ceiling:** at 200K tokens/day, about 30 model calls of ~6K tokens, so roughly
  3–5 missions a day per model. Two models have separate buckets.
- Tool calling and structured outputs are documented. The engine uses JSON output.

### Cloudflare Workers AI (doc)
- **Free allowance:** 10,000 neurons/day on the Workers Free plan. Exceeding it requires the
  paid plan, so the free plan simply stops.
- **Rate limit:** 300 requests per minute for text generation.

| Model | Neurons per M input | Neurons per M output | Free tokens/day (all input) | Free tokens/day (realistic 4:1 input:output)* |
| --- | --- | --- | --- | --- |
| `@cf/qwen/qwen3-30b-a3b-fp8` | 4,625 | 30,475 | ~2.16M | ~0.9M |
| `@cf/zai-org/glm-4.7-flash` | 5,500 | 36,400 | ~1.8M | ~0.77M |
| `@cf/openai/gpt-oss-20b` | see page | see page | — | — |
| `@cf/openai/gpt-oss-120b` | 31,818 | 68,182 | ~314K | ~250K |
| `@cf/baai/bge-m3` (embeddings) | 1,075 | — | ~9.3M | — |

*Arithmetic from the documented neuron prices, not measured throughput.

- **Data:** zero retention for standard inference, and no training on customer content
  (doc).
- **Paid-only models:** Kimi K2.x, GLM-5.x and DeepSeek V4 require a paid billing
  method (doc). They are excluded.

### OpenRouter (measured on this account, plus doc)
- **Daily cap:** `GET /api/v1/key` reports `free_model_daily_requests.limit = 50`,
  `is_free_tier = true` (measured 2026-11-15). Buying credits would raise it, but that is
  excluded at ₹0.
- **More accounts or keys do not change limits** (doc).
- **Privacy:** with `data_collection: deny`, the large free models (Nemotron 3 Ultra/3.5,
  Particle-1) return 404 (measured 2026-11-13). The compatible free models are
  `cohere/north-mini-code`, `baidu/ernie-5.5-flash-preview`, `recursive/flint-v1.0`,
  `dots-studio/dots-3-note-preview` and `apodex/apodex-1.1-mini` (measured).
- **Reliability** (2026-11-13 live evaluation, measured):
  - Most failures were malformed or incomplete replies, or HTTP 429.
  - The daily cap was exhausted within six missions.
  - Treat it as diversity capacity, not a backbone.

### Google Gemini API (doc)
- **Limits:** per project, not per key. Daily request counts reset at midnight Pacific.
  The exact per-model free limits are shown only in AI Studio for your project, so read
  them there and enter them in the catalogue.
- **Experimental and preview models** have tighter limits (doc).
- **Data:** under unpaid services, Google uses submitted content and responses to improve
  products, and human reviewers may read it. "Do not submit sensitive, confidential, or
  personal information" (doc).
- **Engine rule:** Gemini free serves only roles whose input contains no private code.
  The classifier enforces this with a `privacy` flag per task, as Forge's `PrivacyClass`
  already models.

### Mistral (doc for data, limits unverified)
- **Data:** free mode trains on inputs and outputs by default, and opting out is available
  at any time (doc). It may be used only after you opt out in the Admin Console.
- **Limits:** third-party pages list 1 request/s, 500K TPM and 1B tokens/month, but they
  also say these are the 2024 figures (unverified). Use the console values.

### SambaNova (doc for tier rules, numbers not captured)
- **Free tier:** applies when no payment method is linked, with RPM, RPD and a daily token
  cap. The per-model table renders client-side and was not captured. Read it in the
  console before enabling.

## 3. Compute options (free GPU)

| Option | What it is | Permitted for our use? | Verdict |
| --- | --- | --- | --- |
| Google Colab (free) | Interactive notebook, dynamic and unguaranteed limits, VMs deleted when idle, maximum lifetime enforced | The free tier **disallows** bypassing the notebook UI to interact mainly through a web UI. It prioritises active notebook use (doc) | **Not an inference server.** At most: an operator runs a one-off batch job by hand in a notebook |
| Kaggle notebooks | Interactive or batch notebooks with weekly GPU quotas | The quota page could not be fetched from here | Batch experiments only, run by a person. Not an API host |
| Hugging Face ZeroGPU Spaces | Shared GPU, allocated per call. **Free accounts may host up to 2 ZeroGPU Spaces** (verified email, account older than 30 days); Gradio SDK only; default 60 s per call; daily quota (doc) | Hosting a Gradio demo of an open model is the intended use | **Optional, low-volume adapter**: a Space we own, called via its API, for public or synthetic inputs only. A Space is public infrastructure, so no private code and no keys |

No free option gives a persistent GPU inference API that is permitted and reliable. GPU
stays an enhancement, as the mission brief requires.

## 4. Local inference on `nfc`
Measured results are in §4.1, filled in from the server benchmark. Design constraints
from Phase A:
- About 38 GB/s memory bandwidth (2 of 6 channels populated).
- About 71 GB of free RAM.
- 16 threads at most for inference, so the production VMs are not starved.
- The Mini AI VM's Ollama may also be used, with lower priority than Mini AI (approved).

### 4.1 Measurements
*(pending — server benchmark in progress)*

## 5. Routing implications for Phase C

| Task class | First choice | Fallbacks | Never |
| --- | --- | --- | --- |
| Architect or engineer on a private repo | Groq `gpt-oss-120b` | Cloudflare `qwen3-30b-a3b` → local `qwen3-coder:30b` | Gemini free; OpenRouter models that train on data |
| Reviewer (independent family) | Groq `qwen3.8-27b` or Cloudflare `glm-4.7-flash` (a family the builder did not use) | Local `gpt-oss:20b` | Any family that wrote code in the mission |
| Debugger (short failure context) | Groq `gpt-oss-20b` | Local | — |
| Public or synthetic tasks (docs, research, evals) | Gemini free | OpenRouter free | — |
| Embeddings | Local (fastembed `bge-small-en-v1.5`) | Cloudflare `bge-m3` | — |
| Classification and summarisation | Local, smallest model | Groq `gpt-oss-20b` | — |
| Tests, indexing, diffing | Deterministic CPU tools, no LLM | — | Any LLM |

**Three engineering consequences:**
1. **Context first.** Groq's 8K tokens-per-minute cap and CPU prefill speed both demand
   compressed, relevant-only briefs. Phase E is a prerequisite for using Groq at all, not
   an optimisation.
2. **Quota ledger.** Track per-provider request and token windows from response headers
   and the documented limits. Wait for the reset; never rotate keys.
3. **Privacy as a routing constraint.** Each provider gets a `trains_on_prompts` and
   `retention` attribute. Tasks on private repositories can only reach providers that pass.

## 6. What you need to do (one-time, ₹0)
1. **Groq:** create a key in the console and store it as `GROQ_API_KEY` on `nfc`, readable
   only by the `scrappy` user.
2. **Cloudflare:** create an account and an API token with the Workers AI permission. Note
   the account ID.
3. **Gemini:** create a key in AI Studio, and copy the free-tier numbers for your project
   from the AI Studio rate-limit page.
4. **Optional:**
   - Mistral: opt out of training first.
   - SambaNova: read the free-tier numbers in its console.
5. **OpenRouter:** the existing key is already stored in this cloud environment.

## Sources
- Groq rate limits: https://console.groq.com/docs/rate-limits
- Groq models: https://console.groq.com/docs/models
- Groq data retention: https://console.groq.com/docs/your-data
- Cloudflare Workers AI pricing: https://developers.cloudflare.com/workers-ai/platform/pricing/
- Cloudflare Workers AI limits: https://developers.cloudflare.com/workers-ai/platform/limits/
- Cloudflare Workers AI data usage: https://developers.cloudflare.com/workers-ai/platform/data-usage/
- OpenRouter limits: https://openrouter.ai/docs/api-reference/limits
- OpenRouter key status: `GET https://openrouter.ai/api/v1/key` (measured)
- Gemini rate limits: https://ai.google.dev/gemini-api/docs/rate-limits
- Gemini terms: https://ai.google.dev/gemini-api/terms
- Cerebras rate limits and free trial: https://inference-docs.cerebras.ai/support/rate-limits
- Hugging Face Inference Providers pricing: https://huggingface.co/docs/inference-providers/pricing
- Hugging Face ZeroGPU: https://huggingface.co/docs/hub/spaces-zerogpu
- GitHub Models retirement: https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models
- Mistral data training: https://help.mistral.ai/en/articles/323757-do-you-use-my-user-data-to-train-your-artificial-intelligence-models
- SambaNova rate limits: https://docs.sambanova.ai/docs/en/models/rate-limits
- Colab FAQ: https://research.google.com/colaboratory/faq.html
