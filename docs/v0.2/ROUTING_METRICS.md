# V0.2 Routing Metrics

## Goals

Routing metrics must be deterministic, explainable, testable, conservative under small samples, and honest about unavailable data. V0.2 does not infer quality, tokens, billing, or benchmark results.

All normalized scores are in `[0, 1]`, where larger is better. Calculations use one immutable snapshot with an `as_of` UTC timestamp and are rounded to six decimal places only at the explanation/output boundary.

## Common recent window

The V0.2 default window for one provider/model is the newest 100 completed attempts whose `ended_at` is within the previous seven days.

- The record-count bound prevents infinite-history averages.
- The age bound lets stale performance expire.
- Window ordering is deterministic: `ended_at DESC, id DESC`.
- UTC is used for persistence and age filtering.
- Raw latency is stored unchanged; routing may apply documented caps during aggregation.

The Dashboard may query a UTC calendar-day range for "today," but Router inputs always use the common recent window above.

## Confidence and cold start

Measured latency, reliability, and cost do not influence routing until five eligible samples exist. Influence then ramps linearly and reaches full weight at 50 samples:

```text
confidence(n) = 0                         when n < 5
confidence(n) = (n - 5) / 45              when 5 <= n < 50
confidence(n) = 1                         when n >= 50

effective_score =
    (1 - confidence) * static_baseline_score
  + confidence * measured_score
```

Each metric has its own eligible sample count. A model can therefore have measured reliability but unavailable measured cost. This confidence ramp is part of the explanation.

## Latency

### Definition and source

- Start/end timestamps: UTC wall-clock values immediately around each provider attempt.
- `latency_ms`: monotonic elapsed time from immediately before the adapter call until it returns or raises.
- Include completed successful and failed attempts. This represents time consumed by a routing attempt, while reliability separately represents its outcome.
- Reject negative/invalid values. Store actual values; cap only the value used by routing at the configured provider timeout (30 seconds by default).

### Aggregates

- `average_latency_ms`: arithmetic mean of valid values in the selected query window.
- `rolling_latency_ms`: arithmetic mean of capped values in the common recent window.
- `p50_latency_ms` and `p95_latency_ms`: nearest-rank percentiles over raw valid window values. For `n` sorted values, select index `ceil(p * n) - 1`.

The Dashboard exposes average, p50, and p95. Routing uses capped p50 because it is less sensitive to one extreme request than the mean; p95 remains an operational tail-latency signal.

### Normalization

Use an absolute, configured routing policy target rather than min/max normalization across the current candidates:

```text
measured_latency_score = clamp(latency_target_ms / max(p50_latency_ms, 1), 0, 1)
```

The initial global `latency_target_ms` is 1,000 ms. It is a routing policy threshold, not a performance claim. Identical inputs always produce the same score, and adding a candidate does not change another candidate's score.

### Missing data and cold start

With fewer than five valid latency samples, use the static latency baseline only. From 5–49 samples, blend measured and static scores with the common confidence function.

### Stability controls

- bounded 100-attempt/seven-day window
- p50 rather than a raw latest value
- timeout cap for the routing calculation
- confidence ramp from 5 to 50 samples
- no update during a ranking operation; one snapshot per request

## Reliability

### Definition and source

Every completed provider attempt is either successful or failed. A fallback sequence therefore contributes one observation for each provider actually attempted, not only one gateway-level result.

```text
observed_success_rate = successful_attempts / total_attempts
```

The Dashboard exposes the observed rate. The Router uses a conservative Beta-prior estimate based on the candidate's static reliability baseline:

```text
prior_strength = 10
smoothed_reliability =
  (successful_attempts + static_reliability * prior_strength)
  / (total_attempts + prior_strength)
```

### Normalization

`smoothed_reliability` already lies in `[0, 1]`. It is then confidence-blended with the static reliability score using the common eligible attempt count.

### Missing data and cold start

No attempts means the static reliability baseline. Fewer than five attempts have zero measured influence. Failures are never silently discarded because of missing usage or pricing.

### Stability controls

- Beta prior prevents one success/failure from producing 100%/0% reliability.
- Confidence reaches full influence only after 50 attempts.
- The common recent window allows recovery after old incidents.
- No separate circuit breaker is added in V0.2.

## Cost

### Definition and source

`ModelPricing` is manually configured metadata with currency, per-million input-token price, per-million output-token price, source label, and effective timestamp. It is not live provider billing.

For real provider-reported usage:

```text
estimated_cost_usd =
    input_tokens  * input_price_per_million  / 1_000_000
  + output_tokens * output_price_per_million / 1_000_000
```

Calculate with decimal arithmetic and store sufficient precision. If input/output usage or matching pricing is unavailable, estimated cost is `NULL`; zero is valid only when the calculation genuinely produces zero.

### Routing-time normalization

The Router cannot know response usage before calling a model. V0.2 therefore uses the median estimated request cost from eligible successful, priced records in the common recent window:

```text
measured_cost_score = clamp(cost_target_usd / max(p50_estimated_cost_usd, epsilon), 0, 1)
```

`cost_target_usd` is an explicit routing-policy setting, initially `0.001 USD`; `epsilon` only prevents division by zero. This score describes historical estimated request cost for this installation. It is not a quote for the current request or a provider invoice.

### Missing data and cold start

- Fewer than five priced records: use the static cost baseline.
- Missing provider usage: record usage/cost as unavailable and exclude it from cost aggregates.
- Missing/expired pricing metadata: record cost as unavailable and exclude it.
- Never substitute requested `max_tokens`, character counts, or fabricated token estimates in V0.2.

### Stability controls

Median cost limits the effect of one unusually long completion. Per-metric confidence prevents sparse priced samples from dominating. The explanation includes priced sample count and the configured metadata version.

## Quality

### Definition and source

Quality remains the existing static capability score configured for each candidate. V0.2 has no automated benchmark, semantic grader, user feedback signal, or quality crawler.

### Normalization

The configured value must already be in `[0, 1]`. It is used directly.

### Cold start, missing data, and window

Quality has no measured window in V0.2. If a candidate lacks a valid configured quality score, configuration validation fails at startup; no quality score is invented.

### Stability

Quality changes only through reviewed configuration changes. This provides an anchor while operational signals evolve.

## Final route score

Use the existing normalized user preferences:

```text
final_score =
    quality_weight     * static_quality_score
  + cost_weight        * effective_cost_score
  + latency_weight     * effective_latency_score
  + reliability_weight * effective_reliability_score
```

Sort by descending final score, then provider name as the stable tie-breaker. Explicit model requests retain v0.1.0 behavior. A fallback follows the precomputed ranking; the Router does not re-rank midway through one request.

## Structured route explanation

Every successful automatic response extends the existing `modelpilot` object with structured data. Field names may be refined during schema implementation, but the information contract is:

```json
{
  "routing": {
    "strategy": "weighted_measured_v1",
    "as_of": "2026-01-01T12:00:00Z",
    "window": {"max_attempts": 100, "max_age_days": 7},
    "weights": {
      "quality": 0.25,
      "cost": 0.25,
      "latency": 0.25,
      "reliability": 0.25
    },
    "selected": {
      "provider": "deepseek",
      "model": "deepseek-chat",
      "final_score": 0.9
    },
    "candidates": [
      {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "final_score": 0.9,
        "components": {
          "quality": {"source": "configured", "effective_score": 0.86},
          "latency": {
            "source": "measured",
            "raw_value_ms": 742.0,
            "effective_score": 0.91,
            "sample_count": 68,
            "confidence": 1.0
          },
          "cost": {
            "source": "measured_estimate",
            "raw_value_usd": 0.0008,
            "effective_score": 0.94,
            "sample_count": 51,
            "confidence": 1.0
          },
          "reliability": {
            "source": "measured",
            "raw_success_rate": 0.88,
            "effective_score": 0.89,
            "sample_count": 76,
            "confidence": 1.0
          }
        }
      }
    ]
  }
}
```

When measured input is unavailable, `source` is `static_fallback`, raw values are `null`, and confidence/sample count explain why. The response does not include natural-language claims or hidden scoring steps.

## Test invariants

- Scores are bounded and identical for identical snapshots.
- Candidate iteration order does not change results except for the documented tie-break.
- One observation cannot influence a dynamic component before the five-sample floor.
- Missing usage never produces a zero cost sample.
- Records older than seven days and records beyond the latest 100 do not affect routing.
- Percentiles follow the documented nearest-rank rule for odd, even, one-value, and empty inputs.
- A metrics record written during a provider attempt cannot alter that request's precomputed ranking.
