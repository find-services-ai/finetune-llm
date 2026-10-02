# PII redaction at scale: research and plan

Status: **research and design. Nothing here is implemented except a small synthetic workload model.**
Snapshot date: 2026-10-02 (documents written 2026-09-30).

## Goal
A PII detection and redaction system for LLM and chat traffic: ~1B messages/day, inline p99 under 5 ms for short messages,
cloud-agnostic regional cells (EU, US, China, Russia, Vietnam), hybrid detection (rules + small token classifier, optional
non-generative adjudicator), and auto-labeling gated by human-verified audits.

## Documents
| Doc | What it is |
|---|---|
| [plan-v2.md](plan-v2.md) | Architecture, performance levers, compliance and residency design, auto-labeling acceptance test, phased plan, unverified list |
| [workload-and-latency.md](workload-and-latency.md) | Synthetic message-length model by use case; what a 5 ms p99 implies; proposed length-tiered SLOs |
| [discovery-optimization.md](discovery-optimization.md) | What O(1) / O(log n) can and cannot mean for PII discovery; graph-memory risks; long-input design (option a) |
| [decisions.md](decisions.md) | Decisions so far (rounds 1-2) |
| [workload/](workload/) | `synth_lengths.py` (stdlib only, seeded) and its `results.json` |

## Read these caveats first
- Every external claim links a source. Numbers that came from search summaries, or that are arithmetic or assumptions, are labelled.
  The unverified list is in `plan-v2.md` section 7b.
- The message-length model is calibrated only where noted (two LLM-prompt datasets); the traffic mixes and the 5 ms overhead constants are
  assumptions to be replaced with measurements in Phase 1.
- Legal notes are engineering input, not legal advice.

## Reproduce the workload model
```
python3 research/plan/workload/synth_lengths.py          # regenerate results.json
python3 research/plan/workload/synth_lengths.py --check  # fail if results.json is stale
```

## Related
The team review pack and a questionnaire-driven feedback loop (issue form, validation, CI) live on the
`design/pii-redaction-review` branch of `find-services-ai/OpenDelegate`.

## Next step
Phase 0-1 of `plan-v2.md`: traffic profile from real data, then a latency and recall benchmark harness.
