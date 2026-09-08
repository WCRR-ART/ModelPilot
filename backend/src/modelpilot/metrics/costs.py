from decimal import Decimal

from modelpilot.metrics.models import ModelPricing

_TOKENS_PER_MILLION = Decimal(1_000_000)


def estimate_cost(
    pricing: ModelPricing | None,
    input_tokens: int | None,
    output_tokens: int | None,
) -> Decimal | None:
    if pricing is None or input_tokens is None or output_tokens is None:
        return None
    return (
        Decimal(input_tokens) * pricing.input_cost_per_million_tokens
        + Decimal(output_tokens) * pricing.output_cost_per_million_tokens
    ) / _TOKENS_PER_MILLION
