# PII Redaction at Scale — Plan v2 (supersedes v1 sections 3, 4, 7)

Inputs from you: **~1B messages/day, lowest latency in class, Python-first with Rust where it pays, cloud-agnostic, EU/US/China/Russia/Vietnam compliance, AI auto-labeling at ≥90%, most-used compliance regimes.**
Earlier decisions still hold: token-classifier-first hybrid, gated retrain, fail-closed, human-labeled golden set.
Legal notes are engineering input, **not legal advice**. Counsel must sign off per region.

---
## 0. Reality check on the three requirements (critique first)

| Requirement | Issue | Consequence |
|---|---|---|
| **1B msgs/day** | ≈ **11.6k msg/s average**; at an assumed 4× peak ≈ **46k msg/s** (peak factor is my guess; give me your real one). At an assumed ~200 tokens/msg that is ~2.3M tokens/s average, ~9M at peak. | A ~50M-active-parameter encoder costs ≈ 2×params ≈ 0.1 GFLOP/token → **~0.2–0.3 PFLOP/s average, ~1 PFLOP/s at peak** (my arithmetic, not a benchmark). That is "tens of GPUs", not "a datacenter", **but only with batching, quantization and a cascade**. Every claim here is a hypothesis until measured. |
| **"Quickest in class"** | I found no verified public latency numbers for competing PII systems (OPF's own card gives none). | You can only claim what a **reproducible benchmark harness** you publish shows. Build the harness first (Phase 1). |
| **Auto-label at 90% "accuracy"** | Token accuracy is meaningless here: >90% of tokens are non-PII, so a labeler that finds nothing scores >90%. | Define it as **entity-level precision AND recall ≥ 90% vs human-adjudicated audit**, per entity type × language, with confidence intervals (see §5). |
| **All jurisdictions, one system** | China, Russia and Vietnam impose localization/transfer rules (§2). A single global redaction service that ships raw data to one region is **itself the violation**. | Architecture must be **cell-based: raw data never leaves its region**. Only weights, configs and aggregate metrics cross borders (subject to counsel). |
| **Cloud-agnostic + China/Russia** | Export controls and sanctions may restrict GPU availability and service provision in those markets (from my knowledge, **not verified this session**). | Keep a **CPU-capable path** (int8, small model) so a cell can run without top-end GPUs. Get trade-compliance review before committing to those regions. |

---
## 1. Performance architecture (latency + 1B/day)

### 1.1 Two paths, same core
- **Inline path** (sync gRPC): a message in, redacted message out. Budget is a placeholder until measured (start with p50 < 5 ms, p99 < 20 ms for typical short messages, excluding network).
- **Stream path** (Kafka-API broker → workers → sink): throughput-optimized micro-batches; this carries most of the 1B/day.
- Same detect/redact library serves both, so results are identical.

### 1.2 Hot-path pipeline per message
`normalize (NFC, width/homoglyph) → L0 rules (Rust) → cascade gate → tokenizer (HF tokenizers, Rust-based) → batched GPU/CPU encoder → decode spans → merge → replace → residual check (sampled)`

### 1.3 Levers, ordered by expected payoff (all need measurement)
1. **Cascade:** a tiny high-recall first stage (rules + a very small model, or heuristics) so that the full encoder sees only the ambiguous fraction. **Caveat:** names in free text are the hard case; if names are in scope, the gate cannot be regex-only. Measure what fraction can safely skip the encoder. Anything skipped counts against recall.
2. **Dedup / template cache:** cache verdicts by content hash; system-generated and templated messages repeat heavily in many feeds. The hit rate is unknown until you sample real traffic.
3. **Dynamic batching + length bucketing** with a tiny max-queue delay (1–2 ms on the inline path). Avoid padding waste.
4. **Precision:** FP16 first; INT8 only after validating **recall on rare entities**. A benchmark I found shows TensorRT FP16 (2.1 ms batch-1; 9.8 ms batch-16; up to 64× vs CPU) [arXiv 2603.28708](https://arxiv.org/abs/2603.28708) but I could not confirm its model/hardware, and INT8 results vary: dynamic INT8 gave ~4× speedup at −0.46 pt accuracy in one study, while another reported ONNX Runtime INT8 pathologies from operator fallback [arXiv 2608.18182](https://arxiv.org/abs/2608.18182). Benchmark per hardware.
5. **Distillation:** distill a large teacher (the auto-labeling ensemble, §5) into a 4–6-layer student. This is the biggest single lever for cost and latency, and it reuses the labeling investment.
6. **Serving stack:** NVIDIA Triton (dynamic batching, TensorRT/ONNX backends) for GPU cells; ONNX Runtime/OpenVINO for CPU cells. Keep model-agnostic interfaces (spans + confidence).

### 1.4 Model choice under speed pressure
- **OpenAI Privacy Filter (OPF):** 1.5B total / 50M active MoE, sliding-window attention of 128 [HF docs](https://huggingface.co/docs/transformers/main/en/model_doc/openai_privacy_filter). MoE routing and a 1.5B memory footprint may cost more in practice than "50M active" implies; **measure tokens/s per GPU against a dense small encoder**. Its reported non-Latin collapse (Cyrillic ≈ 0.03 F1) [arXiv 2608.02616](https://arxiv.org/abs/2608.02616) (re-check tables) makes it a poor fit for Russian; likely weak for Chinese too (**unverified**).
- **Multilingual small encoder** (e.g. XLM-R-class distilled) is the default candidate for a 6+-language footprint; the bake-off decides.
- **Chinese has no whitespace; Vietnamese needs diacritic normalization; Cyrillic has homoglyph tricks.** These are tokenization and normalization requirements, not model afterthoughts.

### 1.5 Python vs Rust (pragmatic split, and your learning goal)
| Layer | Language | Why |
|---|---|---|
| Training, eval, labeling, orchestration, notebooks | **Python** | ecosystem (HF, PyTorch) |
| Model serving | **Triton** (C++ core) with Python only for glue | GIL and per-request overhead are the enemy at 46k msg/s |
| **L0 rules engine**, normalization, span merge, replacement, gateway/router | **Rust** (PyO3 + maturin for a Python binding) | hot path; predictable latency; no GC |
| CPU-only cells | Rust + ONNX Runtime bindings (evaluate `ort`) | cells without GPUs |

- **Regex engine:** don't pick from README claims. A crate README claims Hyperscan-based matching is ~71× faster than Rust `regex` for multi-pattern sets (**vendor benchmark**), while the regex maintainer's own thread says `regex-automata` often comes close to Hyperscan on `RegexSet`-style checks and that Hyperscan is x86-focused [rust-lang/regex#960](https://github.com/rust-lang/regex/discussions/960). Plan: implement with `regex-automata` + `aho-corasick`, then benchmark against Vectorscan on **your** patterns.
- **Rust learning path (low-risk):** R1 L0 engine as a Python module → R2 span-merge/replacer → R3 async gateway (tokio/tonic) → R4 optional Rust-only CPU inference. Python-only reference implementation stays as the correctness oracle (differential tests).

---
## 2. Compliance & residency design

### 2.1 Verified anchors (this session)
- **EU/UK GDPR:** pseudonymised data **remains personal data**; only truly anonymous data is out of scope, judged by "all the means reasonably likely to be used" [Recital 26](https://gdpr-info.eu/recitals/no-26/). Art. 9 special categories: racial/ethnic origin, political opinions, religion/philosophy, trade-union membership, genetic, biometric ID, health, sex life/orientation [Art. 9](https://gdpr-info.eu/art-9-gdpr/). **Consequence: typed placeholders reduce risk but do not by themselves make data anonymous.** Don't market "GDPR-anonymized".
- **HIPAA:** Safe Harbor's 18 identifier classes plus an "actual knowledge" condition [45 CFR 164.514](https://www.law.cornell.edu/cfr/text/45/164.514).
- **Vietnam:** Personal Data Protection Law 91/2025/QH15 with implementing **Decree 356/2025/ND-CP**, effective **1 Jan 2026** [DFDL](https://www.dfdl.com/insights/legal-and-tax-updates/vietnam-personal-data-protection-2026-what-foreign-organizations-need-to-know/), [Tilleke](https://www.tilleke.com/insights/vietnams-new-personal-data-protection-law-a-closer-look/). Cross-border transfers need a **Transfer Impact Assessment** dossier (submitted within 60 days of first transfer; updated on change, with a stated 6-month update cycle); cross-border fines up to **5% of prior-year revenue** or VND 3B (whichever is higher, per one summary); applies to foreign entities processing data of persons in Vietnam. Per Tilleke, **de-identified data is excluded from "personal data"**, but how the law defines de-identification (reversibility) needs counsel review. Sensitive-data lists are delegated to government decrees.
- **Russia (152-FZ):** since **1 Jul 2025** (Law 23-FZ), Art. 18(5) **prohibits** using foreign databases for recording/storing/updating/retrieving Russian citizens' data at **collection**; cross-border transfer after processing is not directly banned per one law-firm note, but practice is unsettled [Lidings](https://www.lidings.com/media/legalupdates/localization_pd_update/). No anonymization exception mentioned there. **Consequence: redaction must occur inside Russia at ingestion, before anything is exported.**
- **China (PIPL/DSL/CSL + CAC rules):** three transfer routes (CAC security assessment, standard-contract filing, certification), with March 2024 relaxations [Freshfields](https://www.freshfields.com/en/our-thinking/blogs/risk-and-compliance/china-cross-border-data-transfers-revised-requirements-for-security-assessment-102j5lm) / [CMS](https://cms.law/en/chn/legal-updates/china-relaxes-cross-border-data-transfer-rules-with-new-provisions). One summary gave thresholds (>1M individuals' PI or >10k individuals' sensitive PI since 1 Jan of the year triggers the CAC assessment). **I could not open the primary/PDF source to confirm those figures or the exemptions. Treat as unverified.**

### 2.2 Regimes I know of but did NOT verify this session (needs counsel/primary-source pass)
CCPA/CPRA (US state laws), PCI DSS (technical standard for card data, not a law), UK GDPR specifics, Brazil LGPD, India DPDP Act 2023, Japan APPI, South Korea PIPA, Singapore PDPA, Canada PIPEDA, Australia Privacy Act, Saudi/UAE PDPLs, Turkey KVKK, Kazakhstan localization rules.

### 2.3 "Most popular" compliance choice (what the taxonomy ships with)
- **Base profile (superset):** GDPR Art. 4(1) identifiers + Art. 9 special categories + HIPAA-18 + PCI PAN/CVV + secrets/credentials. Most-used globally, and the union is easy to narrow.
- **Jurisdiction overlays (YAML, versioned):** `eu`, `us-hipaa`, `us-ccpa`, `cn-pipl`, `ru-152fz`, `vn-pdpl`, added incrementally. Each overlay adds national-ID formats with validators (e.g. Chinese resident ID, Russian INN/SNILS, Vietnamese CCCD, EU IBAN/VAT). **Verify each format spec and checksum from the issuing authority before implementing** (I am working from memory).
- **Decision needed:** which regions go live first. Do EU + US first, then Vietnam; China and Russia are separate, longer legal/infra tracks.

### 2.4 Cell-based, cloud-agnostic deployment
```
                 GLOBAL CONTROL PLANE (no personal data)
   model registry · taxonomy/overlay configs · CI · aggregate metrics (counts only)
        │ signed artifacts ↓                              ↑ aggregate metrics (counsel-approved)
 ┌──────┴──────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐
 │  EU cell    │  │  US cell   │  │  CN cell   │  │  RU cell   │  │  VN cell   │
 │ ingest→detect→redact→sink; own KMS, storage, audit, review UI, golden set, GPUs/CPUs │
 └─────────────┘  └────────────┘  └────────────┘  └────────────┘  └────────────┘
   raw data, labels, golden sets, review queues NEVER leave their cell
```
- **Portable stack:** Kubernetes + OpenTofu/Terraform + Helm; Kafka-API broker (Kafka/Redpanda); S3-API object storage (incl. MinIO where hyperscalers are absent/restricted); Postgres + ClickHouse for metrics; OpenTelemetry. Local cloud/on-prem in CN and RU are likely, so no proprietary managed service in the critical path.
- **What crosses borders:** signed model weights, taxonomy YAML and code (weights must be trained only on synthetic or in-cell data), and **aggregate metrics**. Each cell evaluates candidates on its **own golden set**; promotion is per cell.
- **Residency implications:** labeling, review UI, retraining data, logs and backups all stay in-cell. **No third-party LLM API for labeling real data** (that would export raw PII). Use self-hosted models per cell.
- **Compliance ops:** per-cell audit log, retention/deletion jobs, DPIA/TIA templates, records of processing.

---
## 3. Auto-labeling to ≥90% (design + how to know you got there)

### 3.1 Evidence and risks
- LLM annotation can approach human-human agreement for some tasks, but needs validation: [Automated annotation requires validation](https://arxiv.org/abs/2306.00176). When LLM labels are wrong, human reviewers assisted by them can be **misled** [CHI'24](https://dl.acm.org/doi/10.1145/3613904.3641960).
- A production multilingual PII annotation effort (≈40k samples, ≈336 locale-specific types) needed ≥2 annotators, 85–100% agreement thresholds, expert arbitration and weekly root-cause reviews; recall for one locale moved from 10.6% to 95.9% only after training and feedback; typical confusions were credit-card vs bank account, SSN vs other national IDs, and cloud credentials [arXiv 2510.06250](https://arxiv.org/html/2510.06250v2).
- Lesson: **auto-labeling is a throughput tool with a measured error rate, never a replacement for a human-adjudicated evaluation set.**

### 3.2 Labeler design
1. **High-precision layer:** rules with validators (Luhn, IBAN, checksummed national IDs, secret detectors). These labels are near-exact and label the structured PII.
2. **Model ensemble:** 2–3 diverse in-cell models (e.g. a self-hosted LLM prompted with the overlay's definitions, plus GLiNER-class and fine-tuned encoders), with span-level voting.
3. **Routing:** agreement ≥ threshold → auto-accept; disagreement or low confidence → human queue (active learning). Humans see labels *unassisted first* for a fixed audit fraction to avoid anchoring on LLM output.
4. **Label-noise handling in training:** treat unlabeled spans with care (a missed label teaches the model to miss), use confident-learning/noise-robust losses, and keep per-source weights.
5. **Circularity guard:** the golden test set is human-only and disjoint. Never evaluate a model on labels produced by a similar model.

### 3.3 The 90% acceptance test (concrete)
- Metric: **entity-level precision and recall, exact and partial overlap**, each ≥ 0.90 against human-adjudicated audit, reported per entity type × language with Wilson 95% intervals.
- Sample size: to show a 90% rate within ±3 points at 95% confidence needs about **384 items per stratum** (1.96²·0.9·0.1/0.03²); ±5 points needs about **138**. Pre-register strata; low-frequency entities may need targeted oversampling.
- Gate: a stratum's auto-labels enter training only if its lower confidence bound clears the bar; otherwise that stratum stays human-labeled.
- **90% is a floor for bootstrapping, not for production detection.** Production recall targets on critical entities (SSN, cards, secrets, names) should be set higher and measured on the human golden set.

---
## 4. Revised pipeline and retrain loop (per cell)
1. Ingest → normalize → dedupe/template cache.
2. Detect (L0 → gate → L1 encoder → L2 adjudicator only for uncertain spans).
3. Redact → **residual check** by an independent detector on a sample and on all low-confidence items.
4. Sink to the clean topic/store; **fail closed** if any stage is unhealthy (hold in the raw zone; back-pressure upstream).
5. Review queue (sample + uncertain + residual hits) → corrections → labeled store.
6. **Retrain trigger** (N reviewed labels / drift / max age) → candidate → per-cell golden gate → shadow on live traffic → promote by registry pointer; rollback ready.
7. Distillation refresh when the teacher improves.

---
## 5. Phased plan (revised)
| Phase | Deliverable | Exit criterion |
|---|---|---|
| **0** | Base taxonomy + EU/US overlays; residency/cell design signed off by counsel; traffic profile (msg size, languages, peak factor, dup rate) | Written policy; real numbers replace my assumptions |
| **1** | **Benchmark + eval harness** (latency, throughput, recall by slice), Python reference detector, golden set v0 (human) | Reproducible p50/p99 and recall report for baseline models on your hardware |
| **2** | Rust L0 (PyO3), normalization, span merge/replacer; differential tests vs Python | ≥ target speedup on L0 with identical outputs |
| **3** | Auto-labeler + audit protocol; fine-tune candidates; **bake-off** (latency × recall × languages); distill student | Labeler passes §3.3; a student within recall budget |
| **4** | Triton/ONNX serving, stream + inline paths, one cell in **shadow mode** | Sustained load test at 1M → 100M → 1B/day equivalents with fail-closed drills |
| **5** | Go live EU/US; add VN overlay; start CN/RU legal-and-infra tracks | Per-cell gate passing; audit trail complete |

---
## 6. What I need from you (blocks Phase 0/1)
1. Average/p95 message length, languages/scripts, and peak-to-average ratio.
2. Inline vs stream split, and your latency target (p99 in ms).
3. Which regions go first; where you have infra (and in CN/RU whether a local entity/cloud exists).
4. GPU access per region (types, or CPU-only).
5. Message sources (chat, email, logs, support tickets?) since context and format drive recall.

## 7. Verification checklist
- Reproducible benchmark from a clean checkout; publish the config and hardware with every latency claim.
- Differential test: Rust L0 vs Python reference on millions of generated and real (in-cell) strings, byte-identical spans.
- Load test ladder with fail-closed and back-pressure drills.
- Labeler audit with pre-registered strata and Wilson intervals.
- Per-cell residency test: block all egress except approved artifacts and aggregate metrics; verify no raw text in logs, metrics or traces.
- Recall regression on rare entities after every quantization or distillation step.

## 7b. Unverified list (carry-forward)
Primary-source thresholds for China's March 2024 rules; national-ID formats and checksums; regimes in §2.2; export-control/sanctions posture for CN/RU; all latency/throughput/cost figures (assumptions and arithmetic only); OPF's Chinese/Vietnamese performance.
