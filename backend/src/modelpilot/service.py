from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from modelpilot.logging import RequestLogStore
from modelpilot.providers import ProviderError, ProviderOutcome
from modelpilot.providers.base import sanitize_error_message
from modelpilot.router import ModelRouter
from modelpilot.schemas import AttemptLog, ChatCompletionPayload, ChatCompletionRequest, RequestLog


class NoProviderAvailable(RuntimeError):
    pass


class AllProvidersFailed(RuntimeError):
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(f"all provider attempts failed for request {request_id}")


class GatewayService:
    def __init__(self, router: ModelRouter, logs: RequestLogStore) -> None:
        self.router = router
        self.logs = logs

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionPayload:
        request_id = f"req_{uuid4().hex}"
        started = perf_counter()
        ranked = self.router.rank(request.model, request.modelpilot.preferences)
        if not ranked:
            raise NoProviderAvailable("no configured provider can serve the requested model")

        attempts: list[AttemptLog] = []
        for item in ranked:
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
            result.setdefault("modelpilot", {})
            result["modelpilot"].update(
                {
                    "request_id": request_id,
                    "provider": item.candidate.provider,
                    "score": item.score,
                    "attempts": len(attempts),
                }
            )
            return result

        self._record(
            request_id=request_id,
            request=request,
            attempts=attempts,
            started=started,
            success=False,
        )
        raise AllProvidersFailed(request_id)

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
