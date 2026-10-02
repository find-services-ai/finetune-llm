#!/usr/bin/env python3
"""Synthetic message-length workload model for PII-redaction sizing.

Stdlib only, seeded, reproducible:  python3 synth_lengths.py

WHAT IS GROUNDED vs ASSUMED
  Grounded (published, see README/plan):
    * LMSYS-Chat-1M: mean user prompt = 69.5 tokens (Llama-2 tokenizer).
    * WildChat: mean user turn = 295.58 tokens, sd = 1609.18 (heavy tail).
  Everything else (chatops, SMS, email, logs, documents, traffic mixes) is an
  ASSUMPTION chosen to span a plausible range. Replace with your own traffic
  sample (Phase 0) before making any capacity or SLO commitment.

Each segment is a lognormal over tokens, clipped to [min, cap].
"""
import json
import math
import random
import statistics
import sys

SEED = 20260930
N = 1_000_000  # samples per profile

# name: (median_tokens, sigma, min_tokens, cap_tokens, note)
SEGMENTS = {
    "chatops":        (14,   0.85, 1,   400,    "Slack/Teams/Discord style messages (ASSUMED)"),
    "sms_push":       (18,   0.55, 1,   60,     "SMS/notification, ~160-char cap (ASSUMED)"),
    "support_chat":   (30,   0.90, 1,   600,    "Customer-support chat turns (ASSUMED)"),
    "llm_conv":       (None, 1.10, 1,   4000,   "LLM conversational prompt; mean pinned to 69.5 (LMSYS, GROUNDED)"),
    "llm_paste":      (None, 1.85, 1,   32000,  "LLM heavy-tail prompt; mean~296, sd~1609 (WildChat, GROUNDED)"),
    "email":          (170,  0.90, 5,   4000,   "Email body (ASSUMED)"),
    "form_ticket":    (110,  0.80, 5,   2000,   "Forms/tickets/intake with personal details (ASSUMED)"),
    "log_line":       (45,   0.60, 3,   600,    "App/log line (ASSUMED)"),
    "document":       (1800, 0.95, 200, 128000, "Pasted docs/transcripts/contracts (ASSUMED)"),
}


def lognormal_params_from_mean(mean):
    return None


def mu_from_mean(mean, sigma):
    # E[X] = exp(mu + sigma^2/2)
    return math.log(mean) - sigma * sigma / 2.0


def sigma_from_mean_sd(mean, sd):
    return math.sqrt(math.log(1.0 + (sd / mean) ** 2))


def make_sampler(name, rng):
    median, sigma, lo, hi, _ = SEGMENTS[name]
    if name == "llm_conv":
        mu = mu_from_mean(69.5, sigma)
    elif name == "llm_paste":
        sigma = sigma_from_mean_sd(295.58, 1609.18)
        mu = mu_from_mean(295.58, sigma)
    else:
        mu = math.log(median)

    def draw():
        x = rng.lognormvariate(mu, sigma)
        return int(min(max(round(x), lo), hi))

    return draw


# Traffic mixes (ASSUMED). Weights sum to 1.
PROFILES = {
    "chatops_heavy": {
        "chatops": 0.55, "log_line": 0.15, "support_chat": 0.10, "sms_push": 0.08,
        "email": 0.05, "form_ticket": 0.03, "llm_conv": 0.03, "llm_paste": 0.005, "document": 0.005,
    },
    "llm_gateway": {
        "llm_conv": 0.55, "llm_paste": 0.30, "chatops": 0.03, "support_chat": 0.03,
        "form_ticket": 0.03, "email": 0.02, "document": 0.04,
    },
    "mixed_enterprise": {
        "chatops": 0.25, "llm_conv": 0.15, "support_chat": 0.12, "log_line": 0.12,
        "email": 0.10, "form_ticket": 0.08, "llm_paste": 0.08, "sms_push": 0.05, "document": 0.05,
    },
}

BUCKETS = [("XS<=32", 32), ("S<=128", 128), ("M<=512", 512), ("L<=2048", 2048), ("XL>2048", None)]
THRESH = [64, 128, 256, 512, 1024, 2048, 8192]


def pct(sorted_vals, p):
    i = min(len(sorted_vals) - 1, int(math.ceil(p / 100.0 * len(sorted_vals))) - 1)
    return sorted_vals[max(i, 0)]


def bucket_shares(vals):
    n = len(vals)
    tot = sum(vals)
    out = {}
    prev = 0
    for label, hi in BUCKETS:
        if hi is None:
            sel = [v for v in vals if v > prev]
        else:
            sel = [v for v in vals if prev < v <= hi]
        out[label] = {
            "msg_share": round(len(sel) / n, 4),
            "token_share": round(sum(sel) / tot, 4),
        }
        if hi is not None:
            prev = hi
    return out


def profile_stats(name, weights, rng):
    samplers = {k: make_sampler(k, rng) for k in weights}
    keys = list(weights)
    w = [weights[k] for k in keys]
    vals = []
    for _ in range(N):
        k = rng.choices(keys, w)[0]
        vals.append(samplers[k]())
    vals_sorted = sorted(vals)
    n = len(vals)
    stats = {
        "mean_tokens": round(statistics.fmean(vals), 1),
        "p50": pct(vals_sorted, 50),
        "p90": pct(vals_sorted, 90),
        "p99": pct(vals_sorted, 99),
        "p99.9": pct(vals_sorted, 99.9),
        "max": vals_sorted[-1],
        "share_msgs_over": {str(t): round(sum(1 for v in vals if v > t) / n, 4) for t in THRESH},
        "share_tokens_over": {},
        "buckets": bucket_shares(vals),
    }
    tot = sum(vals)
    for t in THRESH:
        stats["share_tokens_over"][str(t)] = round(sum(v for v in vals if v > t) / tot, 4)
    return stats, vals_sorted


def segment_stats(rng):
    out = {}
    for k in SEGMENTS:
        d = make_sampler(k, rng)
        v = sorted(d() for _ in range(200_000))
        out[k] = {
            "mean": round(statistics.fmean(v), 1),
            "p50": pct(v, 50), "p90": pct(v, 90), "p99": pct(v, 99),
            "min": v[0], "max": v[-1], "note": SEGMENTS[k][4],
        }
    return out


def coverage_table(vals_sorted):
    """Share of messages whose modelled inline service time fits a 5 ms p99 budget.

    service_ms(tokens) = fixed_ms + tokens / rate_tokens_per_s * 1000
      fixed_ms  : normalize + rules + tokenize + queue + IO   (ASSUMED 1.0 or 2.0)
      rate      : effective single-request encoder throughput  (ASSUMED sweep)
    These constants are hypotheses to be replaced by Phase-1 measurements.
    """
    n = len(vals_sorted)
    rows = []
    for fixed in (1.0, 2.0):
        for rate in (50_000, 100_000, 250_000, 500_000):
            max_tokens = int(max(0.0, (5.0 - fixed)) / 1000.0 * rate)
            # count via bisect
            lo, hi = 0, n
            while lo < hi:
                mid = (lo + hi) // 2
                if vals_sorted[mid] <= max_tokens:
                    lo = mid + 1
                else:
                    hi = mid
            rows.append({
                "fixed_ms": fixed, "rate_tok_per_s": rate,
                "max_tokens_in_5ms": max_tokens,
                "msg_share_within_budget": round(lo / n, 4),
            })
    return rows


def capacity(stats, peak_msgs_per_s=46_000):
    """Tokens/s at peak and encoder FLOP/s for two illustrative model sizes."""
    tps = stats["mean_tokens"] * peak_msgs_per_s
    return {
        "peak_msgs_per_s(ASSUMED 4x of 11.6k avg)": peak_msgs_per_s,
        "peak_tokens_per_s": round(tps),
        "avg_tokens_per_s(11.6k msg/s)": round(stats["mean_tokens"] * 11_574),
        "TFLOP_per_s_peak_7M_nonemb_student(2*params*tok)": round(2 * 7e6 * tps / 1e12, 1),
        "TFLOP_per_s_peak_50M_active(2*params*tok)": round(2 * 50e6 * tps / 1e12, 1),
        "note": "arithmetic only; excludes attention cost, padding waste, utilization (<100%) and the cascade savings",
    }


def main():
    import argparse
    import os

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json"))
    ap.add_argument("--check", action="store_true",
                    help="regenerate and fail if it differs from the committed results file")
    args = ap.parse_args()

    rng = random.Random(SEED)
    result = {"seed": SEED, "n_per_profile": N, "segments": segment_stats(rng), "profiles": {}}
    for name, weights in PROFILES.items():
        assert abs(sum(weights.values()) - 1.0) < 1e-9, name
        stats, vals_sorted = profile_stats(name, weights, rng)
        stats["weights"] = weights
        stats["inline_5ms_coverage_model"] = coverage_table(vals_sorted)
        stats["capacity"] = capacity(stats)
        result["profiles"][name] = stats
    text = json.dumps(result, indent=2)
    if args.check:
        with open(args.out) as f:
            committed = f.read()
        if committed.strip() != text.strip():
            print("results.json is stale: rerun synth_lengths.py and commit", file=sys.stderr)
            return 1
        print("results.json is up to date", file=sys.stderr)
        return 0
    with open(args.out, "w") as f:
        f.write(text)
    print("wrote", args.out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
