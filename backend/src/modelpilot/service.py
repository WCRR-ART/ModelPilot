import logging
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from modelpilot.health.manager import HealthPersistenceError, ProviderHealthManager
from modelpilot.logging import RequestLogStore
from modelpilot.metrics import (
    MetricsStore,
    ModelPricing,
    RoutingDecision,
    RoutingExplanation,
    attempt_from_outcome,
)
from modelpilot.providers import ProviderError, ProviderOutcome
from modelpilot.providers.base import sanitize_error_message
from modelpilot.router import ModelRouter
from modelpilot.schemas import AttemptLog, ChatCompletionPayload, ChatCompletionRequest, RequestLog

logger = logging.getLogger(__name__)


class NoProviderAvailable(RuntimeError):
    pass


class AllProvidersFailed(RuntimeError):
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(f"all provider attempts failed for request {request_id}")


class GatewayService:
    def __init__(
        self,
        router: ModelRouter,
        logs: RequestLogStore,
        metrics: MetricsStore | None = None,
        health: ProviderHealthManager | None = None,
    ) -> None:
        self.router = router
        self.logs = logs
        self.metrics = metrics
        self.health = health

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionPayload:
        request_id = f"req_{uuid4().hex}"
        started = perf_counter()
        ranked, routing = self.router.rank_with_explanation(
            request.model,
            request.modelpilot.preferences,
            request_id,
        )
        if not ranked:
            raise NoProviderAvailable("no configured provider can serve the requested model")

        attempts: list[AttemptLog] = []
        for attempt_index, item in enumerate(ranked):
            attempt_started = perf_counter()
            try:
                provider_result = await item.provider.complete(request, item.candidate.model)
            except ProviderError as exc:
                attempts.append(
                    AttemptLog(
                        provider=item.candidate.provider,
                        model=item.candidate.model,
                        score=item.score,
                        latency_ms=round((perf_counter() - attempt_started) * 1000, 2),
                        success=False,
                        error=sanitize_error_message(str(exc)),
                    )
                )
                continue

            if isinstance(provider_result, ProviderOutcome):
                self._record_provider_attempt(provider_result, request_id, attempt_index)
                self._update_provider_health(provider_result, request_id)
                if not provider_result.success:
                    attempts.append(
                        AttemptLog(
                            provider=item.candidate.provider,
                            model=item.candidate.model,
                            score=item.score,
                            latency_ms=round(provider_result.latency_ms, 2),
                            success=False,
                            error=provider_result.error_message,
                        )
                    )
                    continue
                result = provider_result.response
                if result is None:
                    raise RuntimeError("successful provider outcome did not contain a response")
                attempt_latency_ms = provider_result.latency_ms
            else:
                # Preserve compatibility for third-party V0.1 Provider implementations.
                result = provider_result
                attempt_latency_ms = (perf_counter() - attempt_started) * 1000

            attempts.append(
                AttemptLog(
                    provider=item.candidate.provider,
                    model=item.candidate.model,
                    score=item.score,
                    latency_ms=round(attempt_latency_ms, 2),
                    success=True,
                )
            )
            self._record(
                request_id=request_id,
                request=request,
                attempts=attempts,
                started=started,
                success=True,
                selected_provider=item.candidate.provider,
                selected_model=item.candidate.model,
            )
            if routing is not None:
                routing = routing.with_served_by(
                    item.candidate.provider, item.candidate.model
                )
                self._record_routing_decision(routing)
            result.setdefault("modelpilot", {})
            extension = {
                "request_id": request_id,
                "provider": item.candidate.provider,
                "score": item.score,
                "attempts": len(attempts),
                "served_by": {
                    "provider": item.candidate.provider,
                    "model": item.candidate.model,
                },
            }
            if routing is not None:
                extension["routing"] = routing.model_dump(mode="json")
            result["modelpilot"].update(extension)
            return result

        self._record(
            request_id=request_id,
            request=request,
            attempts=attempts,
            started=started,
            success=False,
        )
        if routing is not None:
            self._record_routing_decision(routing)
        raise AllProvidersFailed(request_id)

    def _update_provider_health(self, outcome: ProviderOutcome, request_id: str) -> None:
        if self.health is None:
            return
        try:
            self.health.update(outcome)
        except HealthPersistenceError:
            logger.warning("health persistence failed for request %s", request_id)
        except ValueError:
            # OPEN-before-cooldown and stale timestamps retain the domain contract.
            logger.warning("health transition rejected for request %s", request_id)

    def _record_routing_decision(self, explanation: RoutingExplanation) -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.record_routing_decision(
                RoutingDecision.from_explanation(explanation)
            )
        except Exception as exc:
            logger.warning(
                "failed to persist routing decision for request %s: %s",
                explanation.request_id,
                sanitize_error_message(str(exc)),
            )

    def _record_provider_attempt(
        self,
        outcome: ProviderOutcome,
        request_id: str,
        attempt_index: int,
    ) -> None:
        if self.metrics is None:
            return
        pricing: ModelPricing | None = None
        try:
            pricing = self.metrics.get_pricing(outcome.provider, outcome.model)
        except Exception as exc:
            logger.warning(
                "failed to look up pricing for request %s and provider %s: %s",
                request_id,
                outcome.provider,
                sanitize_error_message(str(exc)),
            )
        try:
            attempt = attempt_from_outcome(
                outcome,
                request_id,
                attempt_index=attempt_index,
                pricing=pricing,
            )
            self.metrics.record_attempt(attempt)
        except Exception as exc:
            logger.warning(
                "failed to record provider attempt for request %s and provider %s: %s",
                request_id,
                outcome.provider,
                sanitize_error_message(str(exc)),
            )

    def _record(
        self,
        request_id: str,
        request: ChatCompletionRequest,
        attempts: list[AttemptLog],
        started: float,
        success: bool,
        selected_provider: str | None = None,
        selected_model: str | None = None,
    ) -> None:
        self.logs.append(
            RequestLog(
                request_id=request_id,
                requested_model=request.model,
                selected_provider=selected_provider,
                selected_model=selected_model,
                success=success,
                total_latency_ms=round((perf_counter() - started) * 1000, 2),
                attempts=attempts,
                created_at=datetime.now(UTC).isoformat(),
            )
        )
