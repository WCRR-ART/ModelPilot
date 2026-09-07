from dataclasses import dataclass

from modelpilot.providers import Provider
from modelpilot.schemas import RoutingPreferences


@dataclass(frozen=True)
class ModelCandidate:
    provider: str
    model: str
    quality: float
    cost: float
    latency: float
    reliability: float


@dataclass(frozen=True)
class RankedCandidate:
    candidate: ModelCandidate
    provider: Provider
    score: float


class ModelRouter:
    def __init__(
        self,
        providers: dict[str, Provider],
        candidates: list[ModelCandidate],
    ) -> None:
        self.providers = providers
        self.candidates = candidates

    def rank(
        self,
        requested_model: str,
        preferences: RoutingPreferences,
    ) -> list[RankedCandidate]:
        weights = preferences.normalized()
        available = [
            candidate
            for candidate in self.candidates
            if candidate.provider in self.providers
            and self.providers[candidate.provider].configured
        ]
        if requested_model != "auto":
            available = [candidate for candidate in available if candidate.model == requested_model]

        ranked = [
            RankedCandidate(
                candidate=candidate,
                provider=self.providers[candidate.provider],
                score=round(
                    candidate.quality * weights["quality"]
                    + candidate.cost * weights["cost"]
                    + candidate.latency * weights["latency"]
                    + candidate.reliability * weights["reliability"],
                    6,
                ),
            )
            for candidate in available
        ]
        return sorted(ranked, key=lambda item: (-item.score, item.candidate.provider))


def default_candidates(
    openai_model: str,
    gemini_model: str,
    deepseek_model: str,
) -> list[ModelCandidate]:
    # V0.1 baseline scores are normalized estimates. Dynamic measurement is out of scope.
    return [
        ModelCandidate("openai", openai_model, 0.90, 0.65, 0.76, 0.96),
        ModelCandidate("gemini", gemini_model, 0.84, 0.88, 0.90, 0.92),
        ModelCandidate("deepseek", deepseek_model, 0.86, 0.94, 0.82, 0.90),
    ]
