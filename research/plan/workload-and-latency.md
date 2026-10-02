# Synthetic workload model: message lengths and the 5 ms p99 budget

Reproduce: `python3 research/plan/workload/synth_lengths.py` (stdlib only, seed 20260930, 1M samples per profile) → `research/plan/workload/results.json`; add `--check` to verify the committed results are current.
Companion to `plan-v2.md`.

## What is grounded vs assumed
| Item | Status | Source |
|---|---|---|
| LLM user prompt mean = 69.5 tokens (Llama-2 tokenizer) | **grounded** | [LMSYS-Chat-1M](https://arxiv.org/html/2309.11998v4) |
| LLM user turn mean = 295.6 tokens, sd = 1609 (long tail) | **grounded** | [WildChat](https://arxiv.org/pdf/2405.01470), via search result; tokenizer not confirmed by me |
| WildChat languages: English 53%, Chinese 13%, Russian 12% of turns | grounded (search result) | same |
| PII shows up disproportionately in **translation (48%)** and **code editing (16%)** contexts, i.e. in long pasted content | **grounded** (abstract) | [Trust No Bot](https://arxiv.org/abs/2407.11438) |
| Chatops, SMS, support, email, forms, logs, documents: lognormal params | **ASSUMED** | my judgment; replace with your sample |
| Traffic mixes (3 profiles) | **ASSUMED** | scenarios, not measurements |
| Fixed overhead (1–2 ms) and effective per-request encoder rate (50k–500k tok/s) | **ASSUMED** | hypotheses to be replaced by Phase-1 measurements |

Note: I dropped a ">70% of conversations contain personal disclosures" figure that appeared in a search summary; the paper's abstract does not state it.
Token counts depend on the tokenizer. Multilingual text (Chinese, Russian, Vietnamese) tokenizes differently from English, so re-measure with the tokenizer you will actually ship.

## Per-use-case length ranges (tokens; synthetic)
| Use case | mean | p50 | p90 | p99 | max (cap) |
|---|---:|---:|---:|---:|---:|
| SMS / push | 21 | 18 | 36 | 60 | 60 |
| Chatops (Slack/Teams) | 20 | 14 | 42 | 102 | 400 |
| Log line | 54 | 45 | 97 | 184 | 556 |
| Support chat turn | 45 | 30 | 96 | 246 | 600 |
| LLM conversational prompt | 70 | 38 | 155 | 495 | 4,000 |
| Form / ticket with personal details | 152 | 110 | 308 | 707 | 2,000 |
| Email body | 254 | 170 | 535 | 1,384 | 4,000 |
| LLM heavy-tail prompt (paste, translate, code) | 290 | 54 | 573 | 3,982 | 32,000 |
| Documents / transcripts / contracts | 2,833 | 1,804 | 6,092 | 16,446 | 128,000 |

**Viable planning range: 1 token to ~128k tokens, with the median in the 15–60 token band.** The mean is 4–10× the median in every heavy-tailed segment, so sizing on the mean is misleading.

## Three traffic profiles (mixes are assumptions)
| Profile | mean | p50 | p90 | p99 | p99.9 | msgs ≤128 tok | tokens in msgs >2048 |
|---|---:|---:|---:|---:|---:|---:|---:|
| chatops-heavy | 60 | 22 | 95 | 578 | 4,105 | 93.2% | 20.5% |
| LLM gateway | 252 | 45 | 367 | 4,336 | 14,133 | 76.4% | 51.5% |
| mixed enterprise | 230 | 37 | 311 | 4,208 | 13,058 | 79.6% | 53.2% |

Compute is dominated by the tail: in the LLM/mixed profiles, ~2.5% of messages carry >50% of all tokens.

## Load implied at 1B msgs/day (46k msg/s peak is an assumed 4× of 11.6k avg)
| Profile | peak tokens/s | 7M-non-embedding student, TFLOP/s | 50M-active model, TFLOP/s |
|---|---:|---:|---:|
| chatops-heavy | 2.8M | 39 | 277 |
| LLM gateway | 11.6M | 162 | 1,158 |
| mixed enterprise | 10.6M | 148 | 1,058 |
Arithmetic only (2 × params × tokens): it ignores attention cost, padding waste, utilization below 100%, caching and the cascade. Distilling to a small student is a ~7× lever on its own.

## Can p99 < 5 ms hold? (model, not measurement)
Modelled service time = fixed overhead + tokens / single-request encoder rate. For p99 < 5 ms, **≥99% of messages must fit the budget**.

| Profile | fixed 1 ms, 100k tok/s | fixed 1 ms, 250k tok/s | fixed 1 ms, 500k tok/s | fixed 2 ms, 250k tok/s |
|---|---:|---:|---:|---:|
| chatops-heavy | 98.4% ✗ | 99.5% ✓ | 99.7% ✓ | 99.3% ✓ |
| LLM gateway | 90.6% ✗ | 95.2% ✗ | 97.4% ✗ | 94.1% ✗ |
| mixed enterprise | 91.7% ✗ | 95.6% ✗ | 97.5% ✗ | 94.6% ✗ |

**Verdict:** a blanket p99 < 5 ms is plausible only for chat/log-style traffic, and only if a single-request rate of ~250k tok/s is achievable. Once LLM prompts and documents are in the mix, the 99th-percentile message (~4k tokens) cannot be encoded serially in 5 ms under any rate in the sweep. So the SLO has to be defined **per length class**, with the long tail served by a different mechanism.

## Proposed length-tiered SLOs (to be confirmed by Phase-1 measurements)
| Tier | Tokens | Share of msgs (chatops / gateway / mixed) | Path | p99 target (proposal) |
|---|---|---|---|---|
| T0 | ≤128 | 93% / 76% / 80% | inline, single request | **< 5 ms** |
| T1 | 129–512 | 5.6% / 15.8% / 13.5% | inline, small batch | ~10 ms |
| T2 | 513–2,048 | 0.9% / 5.3% / 4.5% | inline, **parallel chunk fan-out** across replicas | ~25 ms |
| T3 | >2,048 | 0.25% / 2.6% / 2.5% | **stream hatch**: chunked, throughput-optimized, fail-closed | seconds |

Levers that make this work:
- **Redact per turn, cache by content hash.** Chat clients resend history (WildChat: ~41% of conversations are multi-turn), and re-encoding it would inflate the token load.
- **Chunk with overlap** for long inputs so chunks run in parallel; the model's attention window (band 128 for OPF) sets the minimum overlap.
- **Batch-queue delay ≤ ~1 ms** on the inline path, or the queue alone eats a fifth of the budget.
- **No Python or GC in the hot path**; Rust/C++ and Triton serve the T0 path.
- **Measure properly:** open-loop load generator and a high-dynamic-range histogram, so a stalled server can't hide its own tail (coordinated omission). Define 5 ms as server-side service time, at target load, excluding network.

## Decisions this surfaces
1. **What does the stream hatch mean for a synchronous caller?** For LLM chat the user is waiting, so "async" can't be the answer for a prompt about to be sent. Options: (a) T2/T3 inline with a looser SLO via fan-out; (b) hold-and-release once the stream finishes; (c) fail-closed with a "too long, processing" response. My recommendation is (a) for ≤~8k tokens and (c)/(b) beyond it.
2. **Is p99 < 5 ms an SLO over all traffic or over T0?** The numbers above say it can only be the latter for LLM-style traffic.
3. **Share of the long tail** in your real traffic decides cost more than anything else; sample it in Phase 0.

## Decision log
- **Long inputs (T2, up to ~8k tokens): option (a), inline with parallel chunk fan-out.** Beyond ~8k tokens: fail closed or route to the stream path. See `discovery-optimization.md` §6, including the tail-amplification caveat (per-chunk p99.94 needed for a 16-way fan-out to hit overall p99).
- Discovery acceleration (caches, tenant dictionary, session entity graph, template path): see `discovery-optimization.md`.

### Round-1 answers (design grilling)
| Question | Answer | Consequence |
|---|---|---|
| Reversibility | **per-tenant choice** (irreversible masking or reversible vault) | two modes; vault is per-region, per-tenant-key, TTL, erasure-capable |
| Adversary | **also deliberate evasion** | normalization + bounded decoding + cross-turn state + adversarial test corpus; injection-proof adjudicator |
| State | **session-scoped, short TTL, keyed hashes, in-region** | one session store; purge API; documented retention |
| Failure mode | **hard fail closed** | redactor availability = product availability; define deadline-miss semantics (open item) |

### Round-2 answers
| Question | Answer | Consequence |
|---|---|---|
| Deadline miss | **serve late, count in SLO** | hard-block reserved for unhealthy / unknown-state; never pass unredacted |
| Generative adjudicator | **not sure** -> default: **none in v1** (discriminative only); revisit with data | decision rule: add a generative add-only L2 only if the uncertain-band errors measured on the golden set justify it |
| Output-side redaction | **prompts now, responses later** | session state + placeholder format designed so output-side can be added without rework |
| Placement | **both, as separate products** | gateway first (authority); SDK later, sharing one Rust core; client is untrusted so the gateway always re-verifies |
