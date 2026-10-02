# Optimizing PII discovery: what O(1) / O(log n) can and cannot mean

Status: design analysis, not measurement. Sources are linked; claims marked *(unverified)* come from my background knowledge.

## 1. The complexity claim, stated precisely
- **Unseen text cannot be handled in sublinear time.** Discovery must read the message: O(n) in tokens, and the encoder costs roughly O(n · layers · d²) plus attention. You can reduce *how much* text reaches the encoder, not make first-time discovery O(1).
- **What can be O(1) / O(log N):**
  | Mechanism | Cost | Notes |
  |---|---|---|
  | Verdict cache keyed by content hash of a turn/chunk | O(1) expected per chunk | only for exact repeats (resent chat history, templates) |
  | Tenant dictionary (customers, employees, internal IDs) | Aho-Corasick: O(n + m + z) — linear in text, **independent of dictionary size** after O(m) build ([overview](https://www.upgrad.com/blog/aho-corasick-algorithm/)) | "exact data matching"; catches rare names no model knows |
  | Session entity graph lookups | O(1) hash per token | alias/coreference propagation |
  | ANN nearest-neighbour over stored contexts | ~O(log N) *(unverified, approximate, HNSW-style)* | but see the cost trap below |
- **Cost trap:** embedding the query for retrieval takes an encoder pass, about as expensive as classifying the message with a small student. Retrieval-first is cheaper only if the key is cheap (hash, MinHash/SimHash, shingles), not a neural embedding.
- **Retrieval improves accuracy, not speed, in the literature:** retrieval-augmented and kNN NER help hard cases ([kNN-NER](https://arxiv.org/abs/2203.17103), [U-RaNER, won 9 of 13 MultiCoNER-2 tracks](https://arxiv.org/abs/2305.03688)) and they add lookup cost. Use them for the adjudicator and for offline mining, not the T0 path.

## 2. Where a relational/graph memory genuinely pays (ranked)
1. **Session entity graph (per conversation, TTL):** nodes are entities and aliases, edges are "same-as" / "mentioned-in-turn". Once "Alice Smith" is detected, later "Alice" or "Smith" in the same conversation is caught by lookup, and pseudonyms stay consistent. This improves recall across turns at O(1) per token.
2. **Tenant-authoritative dictionary** (Aho-Corasick or Bloom+hash) built from the tenant's own records.
3. **Template/cluster cache:** near-duplicate detection (MinHash/SimHash LSH *(unverified)*) to reuse the *structure* of earlier verdicts for templated messages, then run the model only on variable slots.
4. **Cascade gate** that skips the encoder when L0 + memory are confident.
5. **Offline:** cluster hard examples to drive active learning and distillation.

## 3. Why "cluster → classify the cluster" is dangerous
PII occurs in a minority of messages inside any semantic cluster (an "address-like" cluster still contains messages with no address, and an "innocuous" cluster still contains a stray SSN). Cluster-level verdicts are only safe for **template clusters with known slots**. Anything semantic must stay at span level.

## 4. Risks that decide the design
| Risk | Evidence / reasoning | Mitigation |
|---|---|---|
| **The memory is a PII honeypot** | Embeddings are invertible: Vec2Text recovers 92% of 32-token inputs exactly ([arXiv 2310.06816](https://arxiv.org/pdf/2310.06816)); the authors say embeddings deserve the same care as raw text | store keyed hashes (HMAC, per-tenant key), never raw text or raw embeddings of PII |
| **Graph memories leak structure** | GraphRAG extraction attacks leaked 50–73% of entities and up to 74% of relationships on Enron in targeted tests ([arXiv 2508.17222](https://arxiv.org/pdf/2508.17222)) | no free-form query interface; per-tenant, per-session isolation; short TTL; in-region only |
| **Cache poisoning / bypass** | an attacker who can get "X is not PII" cached can later slip X through | cache **positives** freely; cache negatives only for exact-hash chunks, short TTL, never for structured types; L0 always runs |
| **Context dependence** | the same string is PII in one context and not another | key by (string, local-context hash), restrict to high-confidence types |
| **Staleness** | model and taxonomy change | include model + taxonomy version in every key |
| **Erasure and retention** | hashed identifiers are still personal data under GDPR's pseudonymisation logic ([Recital 26](https://gdpr-info.eu/recitals/no-26/)) | TTL + purge API; retention documented per region |
| **Latency** | a remote lookup costs ~0.2–1 ms of a 5 ms budget *(assumption)* | in-process LRU for T0; remote/shared lookups only for T1+ |
| **Hit rate is unknown** | depends entirely on your traffic | measure in Phase 0: duplicate-turn rate, template rate |

## 5. Proposed "discovery accelerator" stack (cheapest first)
0. Exact-hash verdict cache (per turn/chunk)
1. L0 rules + tenant dictionary (Aho-Corasick)
2. Session entity graph propagation
3. Template-slot path (LSH)
4. Small encoder (SLM) on the residual only
5. Adjudicator for the uncertain band

**Acceptance test:** a *non-inferiority* test against an always-run-the-encoder baseline on the human golden set: recall may not drop by more than a pre-agreed margin on any critical entity, and report the encoder-skip rate and cache-hit rate actually achieved. The whole accelerator is worthless if it buys latency with recall.

## 6. Option (a) spec: long inputs stay inline via parallel chunking (up to ~8k tokens)
- Chunk the input with overlap. The model's attention band (128 for OPF) sets the minimum overlap; start with ~512-token chunks and ≥128 tokens of overlap each side, so overhead is about 1.3–1.5× tokens.
- Fan out across replicas with a shared deadline, then join and merge spans (deduplicate overlaps, keep the max-confidence label at boundaries).
- **Tail amplification is the catch.** If a request splits into k=16 chunks, all must finish in time. To get an overall p99, each chunk needs a per-chunk quantile of 0.99^(1/16) ≈ **p99.94**. Mitigations: hedged requests, load-aware routing, and a deadline-based fallback.
- **Fallback when a chunk misses its deadline:** fail closed for that chunk (mask the chunk conservatively or return a structured "retry/blocked" result). Never pass it through unredacted.
- Above ~8k tokens: fail closed with a clear "too long, processing" response (option c), or route to the stream path.
- Targets (proposals, to be measured): T2 p99 ≈ 25 ms; beyond T2, seconds.
