# V0.4 Architecture

```text
explicit local Benchmark Definition (suite_id, version)
  -> explicitly invoked Benchmark Runner (later)
  -> existing Provider adapter -> Model Response
  -> versioned deterministic Evaluator -> CaseEvaluation
  -> BenchmarkCaseResult -> BenchmarkRun
  -> separate benchmark store -> scoped QualitySnapshot
  -> future confidence-blended quality input to Router
```

## Boundaries

The new `modelpilot.benchmarks` package owns definitions and loading without importing FastAPI,
Router, providers, health or stores. V04-001 ends at loading: none of the downstream arrows execute.
Evaluation is a pure response-to-score operation; the runner owns I/O and outcome/error handling.
Later runner calls must be explicit, use dedicated cases and never intercept production traffic.
Benchmark calls must not automatically create production attempts or drive circuit transitions.
Runner configuration must explicitly bound requests/time/cost before any real execution is enabled.

Production attempts describe live latency, reliability and estimated cost. Benchmark records describe
dedicated task evaluation. Separate storage tables/interfaces and source labels prevent the latter
from contaminating operational metrics. Persistence/schema migration is deferred to V04-004.

## Later quality integration

Health eligibility remains before scoring. Existing quality/cost/latency/reliability preference weights
and deterministic ordering remain intact. Only the quality component gains a measured input in
V04-006: `quality = (1 - confidence) * static_quality + confidence * measured_quality`.
Use a single configured suite/version/category profile for comparisons across candidates; never
mix incompatible suites or infer categories with an LLM. A candidate lacking compatible evidence
uses static quality. Other measured dimensions retain their V0.3 algorithms.

Definitions, run provenance, evaluator versions and normalization are explicit. Runs pin a suite
content fingerprint and provider/model/settings so accidental reuse of a changed version can be
detected in the later registry/store. V04-001 has no registry or global version uniqueness enforcement.

## Privacy and trust

Only user-selected local files are read. JSON is data: no import, eval, exec, network or code execution.
Original fixture prompts are safe to version; production prompts must never be imported automatically.
Later result retention should default to scores and sanitized error/provenance metadata, not raw
responses or credentials. The CLI must not silently download or execute third-party datasets.
