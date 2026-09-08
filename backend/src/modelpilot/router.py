import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from math import isfinite

from modelpilot.metrics import (
    MetricsStore,
    ProviderMetricsSnapshot,
    RoutingExplanation,
    RoutingSignal,
)
from modelpilot.providers import Provider
from modelpilot.providers.base import sanitize_error_message
from modelpilot.schemas import RoutingPreferences

logger = logging.getLogger(__name__)

MIN_MEASURED_SAMPLES = 5
FULL_CONFIDENCE_SAMPLES = 50
LATENCY_TARGET_MS = 1_000.0
LATENCY_CAP_MS = 30_000.0
COST_TARGET_USD = Decimal("0.001")
COST_EPSILON = Decimal("0.000000000001")
RELIABILITY_PRIOR_STRENGTH = 10


def confidence_for_samples(sample_count: int) -> float:
    if sample_count < MIN_MEASURED_SAMPLES:
        return 0.0
    if sample_count >= FULL_CONFIDENCE_SAMPLES:
        return 1.0
    return (sample_count - MIN_MEASURED_SAMPLES) / (
        FULL_CONFIDENCE_SAMPLES - MIN_MEASURED_SAMPLES
    )


def normalize_latency(latency_ms: float) -> float:
    capped_latency = min(latency_ms, LATENCY_CAP_MS)
    return _clamp(LATENCY_TARGET_MS / max(capped_latency, 1.0))


def normalize_cost(cost_usd: Decimal) -> float:
    denominator = max(cost_usd, COST_EPSILON)
    return _clamp(float(COST_TARGET_USD / denominator))


def blend_score(static_score: float, measured_score: float, confidence: float) -> float:
    return static_score * (1 - confidence) + measured_score * confidence


@dataclass(frozen=True)
class ModelCandidate:
    provider: str
    model: str
    quality: float
    cost: float
    latency: float
    reliability: float

    def __post_init__(self) -> None:
        scores = (self.quality, self.cost, self.latency, self.reliability)
        if any(not isfinite(score) or score < 0 or score > 1 for score in scores):
            raise ValueError("candidate scores must be between 0 and 1")


@dataclass(frozen=True)
class RankedCandidate:
    candidate: ModelCandidate
    provider: Provider
    score: float
    signal: RoutingSignal


class ModelRouter:
    def __init__(
        self,
        providers: dict[str, Provider],
        candidates: list[ModelCandidate],
        metrics: MetricsStore | None = None,
    ) -> None:
        self.providers = providers
        self.candidates = candidates
        self.metrics = metrics

    def rank(
        self,
        requested_model: str,
        preferences: RoutingPreferences,
    ) -> list[RankedCandidate]:
        ranked, _ = self.rank_with_explanation(requested_model, preferences)
        return ranked

    def rank_with_explanation(
        self,
        requested_model: str,
        preferences: RoutingPreferences,
    ) -> tuple[list[RankedCandidate], RoutingExplanation | None]:
        weights = preferences.normalized()
        available = [
            candidate
            for candidate in self.candidates
            if candidate.provider in self.providers
            and self.providers[candidate.provider].configured
        ]
        if requested_model != "auto":
            available = [candidate for candidate in available if candidate.model == requested_model]

        snapshots = self._read_snapshots(available) if requested_model == "auto" else {}
        ranked = [
            self._score_candidate(
                candidate,
                weights,
                snapshots.get((candidate.provider, candidate.model)),
            )
            for candidate in available
        ]
        ranked = sorted(
            ranked,
            key=lambda item: (-item.score, item.candidate.provider, item.candidate.model),
        )
        if requested_model != "auto" or not ranked:
            return ranked, None
        explanation = RoutingExplanation(
            strategy="weighted_measured_v1",
            selected=ranked[0].signal,
            candidates=tuple(item.signal for item in ranked),
            created_at=datetime.now(UTC),
        )
        return ranked, explanation

    def _read_snapshots(
        self, candidates: list[ModelCandidate]
    ) -> dict[tuple[str, str], ProviderMetricsSnapshot]:
        if self.metrics is None:
            return {}
        eligible = {(candidate.provider, candidate.model) for candidate in candidates}
        try:
            available = self.metrics.list_provider_metrics()
        except Exception as exc:
            logger.warning(
                "failed to read routing metrics; using static scoring: %s",
                sanitize_error_message(str(exc)),
            )
            return {}
        return {
            (snapshot.provider, snapshot.model): snapshot
            for snapshot in available
            if (snapshot.provider, snapshot.model) in eligible
        }

    def _score_candidate(
        self,
        candidate: ModelCandidate,
        weights: dict[str, float],
        snapshot: ProviderMetricsSnapshot | None,
    ) -> RankedCandidate:
        latency_score, latency_source, latency_confidence = _latency_signal(
            candidate.latency, snapshot
        )
        reliability_score, reliability_source, reliability_confidence = (
            _reliability_signal(candidate.reliability, snapshot)
        )
        cost_score, cost_source, cost_confidence = _cost_signal(candidate.cost, snapshot)
        final_score = round(
            candidate.quality * weights["quality"]
            + cost_score * weights["cost"]
            + latency_score * weights["latency"]
            + reliability_score * weights["reliability"],
            6,
        )
        sample_count = snapshot.sample_count if snapshot is not None else 0
        signal = RoutingSignal(
            provider=candidate.provider,
            model=candidate.model,
            quality_score=candidate.quality,
            latency_score=round(latency_score, 6),
            reliability_score=round(reliability_score, 6),
            cost_score=round(cost_score, 6),
            measured_sample_count=sample_count,
            measured_confidence=round(confidence_for_samples(sample_count), 6),
            final_score=final_score,
            sources={
                "quality": "configured",
                "latency": latency_source,
                "reliability": reliability_source,
                "cost": cost_source,
            },
            reason_metadata={
                "latency_sample_count": sample_count if snapshot is not None else 0,
                "latency_confidence": round(latency_confidence, 6),
                "reliability_sample_count": sample_count if snapshot is not None else 0,
                "reliability_confidence": round(reliability_confidence, 6),
                "cost_sample_count": (
                    snapshot.priced_sample_count if snapshot is not None else 0
                ),
                "cost_confidence": round(cost_confidence, 6),
            },
        )
        return RankedCandidate(
            candidate=candidate,
            provider=self.providers[candidate.provider],
            score=final_score,
            signal=signal,
        )


def _latency_signal(
    static_score: float,
    snapshot: ProviderMetricsSnapshot | None,
) -> tuple[float, str, float]:
    if snapshot is None or snapshot.p50_latency_ms is None:
        return static_score, "static_unavailable", 0.0
    confidence = confidence_for_samples(snapshot.sample_count)
    if confidence == 0:
        return static_score, "static_cold_start", confidence
    score = blend_score(
        static_score, normalize_latency(snapshot.p50_latency_ms), confidence
    )
    return score, _measured_source(confidence), confidence


def _reliability_signal(
    static_score: float,
    snapshot: ProviderMetricsSnapshot | None,
) -> tuple[float, str, float]:
    if snapshot is None or snapshot.success_rate is None:
        return static_score, "static_unavailable", 0.0
    confidence = confidence_for_samples(snapshot.sample_count)
    if confidence == 0:
        return static_score, "static_cold_start", confidence
    smoothed = (
        snapshot.success_count + static_score * RELIABILITY_PRIOR_STRENGTH
    ) / (snapshot.sample_count + RELIABILITY_PRIOR_STRENGTH)
    score = blend_score(static_score, _clamp(smoothed), confidence)
    return score, _measured_source(confidence), confidence


def _cost_signal(
    static_score: float,
    snapshot: ProviderMetricsSnapshot | None,
) -> tuple[float, str, float]:
    if snapshot is None or snapshot.p50_estimated_cost is None:
        return static_score, "static_unavailable", 0.0
    confidence = confidence_for_samples(snapshot.priced_sample_count)
    if confidence == 0:
        return static_score, "static_cold_start", confidence
    score = blend_score(
        static_score, normalize_cost(snapshot.p50_estimated_cost), confidence
    )
    return score, _measured_source(confidence), confidence


def _measured_source(confidence: float) -> str:
    return "measured" if confidence == 1 else "blended"


def _clamp(value: float) -> float:
    return max(0.0, min(value, 1.0))


def default_candidates(
    openai_model: str,
    gemini_model: str,
    deepseek_model: str,
) -> list[ModelCandidate]:
    # Static normalized estimates remain the cold-start and missing-data baselines.
    return [
        ModelCandidate("openai", openai_model, 0.90, 0.65, 0.76, 0.96),
        ModelCandidate("gemini", gemini_model, 0.84, 0.88, 0.90, 0.92),
        ModelCandidate("deepseek", deepseek_model, 0.86, 0.94, 0.82, 0.90),
    ]
