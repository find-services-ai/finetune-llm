# Decision log

Decisions from the design discussion, recorded by round. The live questionnaire loop (form, validation, CI) is on the
`design/pii-redaction-review` branch of `find-services-ai/OpenDelegate`; this file is a snapshot of rounds 1-2.

## Rounds 1-2 (recorded 2026-09-30, from the design discussion)

| Question | Decision | Consequence |
|---|---|---|
| Q-REVERSIBILITY | Per-tenant choice | Two modes; the vault is per-region, per-tenant key, TTL, erasure-capable; placeholders must be unforgeable (per-session nonce/HMAC) |
| Q-ADVERSARY | Also deliberate evasion | Normalisation, bounded decoding, cross-turn state, adversarial test corpus, injection-proof components |
| Q-STATE | Session-scoped, short TTL | Keyed hashes only, in-region, purge API, documented retention |
| Q-FAILMODE | Hard fail closed | Redactor availability equals product availability; redundancy on the critical path |
| Q-DEADLINE | Serve late, count in SLO | Hard block only for unhealthy or unknown state; never pass unredacted |
| Q-ADJUDICATOR | Non-generative only (user unsure; default adopted) | Revisit with golden-set data; any generative L2 must be add-only |
| Q-OUTPUT-SIDE | Prompts now, responses later | Session state and placeholder format designed to allow output-side later |
| Q-PLACEMENT | Both, as separate products | Gateway first; SDK after gateway gates pass; one Rust core; the gateway always re-verifies |

Also decided: long inputs (513-8k tokens) stay inline via parallel chunk fan-out (option a); beyond ~8k tokens fail closed or route to the stream path.
