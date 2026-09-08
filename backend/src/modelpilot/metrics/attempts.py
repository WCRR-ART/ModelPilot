from datetime import UTC, datetime

from modelpilot.metrics.costs import estimate_cost
from modelpilot.metrics.models import AttemptRecord, ModelPricing
from modelpilot.providers import ProviderOutcome


def attempt_from_outcome(
    outcome: ProviderOutcome,
    request_id: str,
    *,
    attempt_index: int = 0,
    pricing: ModelPricing | None = None,
) -> AttemptRecord:
    return AttemptRecord(
        request_id=request_id,
        attempt_index=attempt_index,
        provider=outcome.provider,
        model=outcome.model,
        started_at=outcome.started_at,
        finished_at=outcome.finished_at,
        latency_ms=outcome.latency_ms,
        success=outcome.success,
        error_type=outcome.error_type.value if outcome.error_type is not None else None,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        total_tokens=outcome.total_tokens,
        estimated_cost=estimate_cost(
            pricing,
            outcome.input_tokens,
            outcome.output_tokens,
        ),
        created_at=datetime.now(UTC),
    )
