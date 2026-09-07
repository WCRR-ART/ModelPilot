# V0.2 Architecture

## V0.1 baseline

The v0.1.0 request path is deliberately small:

1. FastAPI validates `ChatCompletionRequest`.
2. `ModelRouter` filters unconfigured providers and ranks `ModelCandidate` values from four static scores.
3. `GatewayService` attempts providers in score order and measures each attempt with `perf_counter()`.
4. The successful provider response receives a small `modelpilot` extension.
5. `RequestLogStore` keeps recent `RequestLog` objects in a process-local bounded deque.
6. `GET /v1/logs` exposes that deque; the Dashboard only fetches `/health` and otherwise renders static values.

Provider adapters own transport translation. OpenAI and DeepSeek pass through OpenAI-compatible responses; Gemini translates its response and usage fields. No common usage/cost result contract or durable metrics store exists.

## Minimal V0.2 architecture

V0.2 adds four bounded components around the existing path:

- `MetricsStore`: a narrow persistence interface with a SQLite implementation and an in-memory test implementation.
- `MetricsReader`: deterministic recent-window queries and percentile calculations over completed attempt records.
- `RoutingScorer`: combines static capability data with an immutable metrics snapshot and pricing metadata.
- Read-only metrics API: supplies the existing Dashboard with summary, provider, decision, and failure data.

It does not add a queue, scheduler, cache server, worker service, or external database.

```text
                         +----------------------+
client request ----------> FastAPI chat endpoint|
                         +----------+-----------+
                                    |
                                    v
                         +----------------------+
                         | MetricsReader        |
                         | recent SQLite window |
                         +----------+-----------+
                                    | immutable snapshot
                                    v
 +----------------+      +----------------------+      +-------------------+
 | static model   |----->| RoutingScorer/Router |----->| ranked candidates |
 | capabilities   |      +----------+-----------+      +---------+---------+
 +----------------+                 |                            |
                                    | decision                   v
                                    |                  +-------------------+
                                    |                  | Provider adapter  |
                                    |                  +---------+---------+
                                    |                            |
                                    v                            v
                         +----------------------+      normalized outcome
                         | SQLite MetricsStore  |<---------------+
                         | attempts + decisions|
                         +----------+-----------+
                                    |
                                    v
                         +----------------------+
                         | read-only metrics API|
                         +----------+-----------+
                                    |
                                    v
                              Next.js Dashboard
```

## Request data flow

1. Create a gateway request ID and UTC request timestamp.
2. For `model="auto"`, read one metrics snapshot for all eligible provider/model candidates. Do not query between candidate comparisons.
3. Score and sort candidates. Persist one `RoutingDecision` containing the input weights, candidate explanations, selection, and metrics cutoff time.
4. Immediately before each provider call, capture UTC start time and monotonic start time.
5. Normalize the provider response into a provider outcome containing the original response plus nullable usage values. Provider absence of usage remains `unavailable`/`NULL`.
6. Capture UTC end time and monotonic elapsed milliseconds for success or failure.
7. If pricing metadata and real usage are available, calculate estimated cost using the pricing version selected for the request timestamp. Otherwise store `NULL` plus the reason it is unavailable.
8. Insert the completed `RequestRecord` in a short SQLite transaction.
9. Continue fallback when appropriate. A metrics persistence error is logged and surfaced operationally, but it does not replace a successful LLM response.
10. Attach the already-calculated structured route explanation to a successful automatic response.

No prompt text, message content, API key, authorization header, or raw provider body is persisted.

## Router data sources

| Input | Source | Role |
| --- | --- | --- |
| Quality | Existing configured static capability metadata | Remains static in V0.2 because benchmarks/feedback are out of scope |
| Latency baseline | Existing candidate metadata | Cold start and missing-data fallback |
| Measured latency | Recent `RequestRecord` attempts | Confidence-blended dynamic signal |
| Reliability baseline | Existing candidate metadata | Bayesian prior/cold start |
| Measured reliability | Recent success/failure attempts | Confidence-blended dynamic signal |
| Cost baseline | Existing candidate metadata | Cold start or unavailable cost fallback |
| Measured estimated cost | Recent priced records | Confidence-blended dynamic signal |
| Weights | Existing `RoutingPreferences` | User-visible deterministic weighting |
| Pricing | Configured `ModelPricing` metadata | Post-response estimates; never represented as live billing |

The reader returns a frozen snapshot with a single `as_of` timestamp. The Router is pure with respect to that snapshot, which makes ranking reproducible in tests and explainable after the request.

## Metrics Store decision

Use SQLite through Python's standard `sqlite3` support.

- Enable WAL mode and a short busy timeout.
- Use parameterized SQL and short transactions.
- Keep one local database file with a configurable path and a safe local default.
- Introduce a small schema version table; avoid a migration framework in V0.2.
- Store raw attempt/decision records, then calculate aggregates at query time.
- Index `(provider, model, ended_at)` and decision time; do not add speculative indexes.

### Alternatives considered

| Option | Strength | V0.2 problem | Decision |
| --- | --- | --- | --- |
| In-memory deque | Smallest implementation | Loses data on restart and cannot support durable Dashboard history | Use only as a test double |
| JSON file | Human-readable | Unsafe concurrent updates, weak querying, difficult percentile/window access | Reject |
| SQLite | Durable, transactional, queryable, no service | Single-node only; requires write discipline | Recommended |
| PostgreSQL/Redis | Scales beyond one process | Operational complexity without a V0.2 requirement | Out of scope |

## Provider call boundary

Provider adapters continue to return the OpenAI-compatible payload, but V0.2 needs one internal normalized outcome boundary for usage availability. OpenAI/DeepSeek usage is read from their compatible response; Gemini usage is mapped from `usageMetadata`. Missing keys must map to `None`, not zero. Cost calculation remains outside provider adapters so pricing policy does not leak into transport code.

## Dashboard data source

The Dashboard stops using hard-coded metric values and reads narrow, read-only endpoints. Exact paths may be finalized during API task design, but the minimum resources are:

- summary: distinct gateway requests today, average attempt latency, attempt success rate, total estimated cost, and unavailable-cost count
- providers: attempted request count and rolling latency/reliability/cost per provider/model
- decisions: recent structured automatic routing decisions
- failures: recent failed attempts with bounded category, provider/model, and timestamp

Queries use UTC day boundaries and the same aggregation definitions as routing. Empty data is returned as zero counts plus nullable measurements; the UI displays `Unavailable` rather than substituting invented values.

Fallback must not inflate the top-level request count: `requests today` counts distinct `gateway_request_id` values. Provider breakdowns count actual attempts, so a fallback request may appear once for each provider it reached.

## Failure and consistency behavior

- Provider success remains the primary outcome. Metrics write failure must not cause fallback after a provider already succeeded.
- A failed provider attempt should still be recorded before fallback when the store is available.
- The Router never consumes partially updated aggregates; it reads raw records through a single cutoff timestamp.
- SQLite lock/IO failures are bounded by timeout and logged without credentials or prompt content.
- Corrupt or invalid usage/pricing data produces an unavailable cost, not zero cost.
- The deterministic tie-break remains provider name after final score.

## Deployment boundary

V0.2 targets one ModelPilot process or a small single-host deployment using one SQLite file. Multi-process write tuning, replication, distributed coordination, retention services, and remote Dashboard access control are explicitly deferred.
