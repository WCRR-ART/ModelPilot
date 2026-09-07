from modelpilot.providers.base import Provider
from modelpilot.router import ModelCandidate, ModelRouter
from modelpilot.schemas import ChatCompletionRequest, RoutingPreferences


class ConfiguredProvider(Provider):
    def __init__(self, name: str) -> None:
        super().__init__("test-key")
        self.name = name

    async def complete(self, request: ChatCompletionRequest, model: str) -> dict:
        return {"model": model}


def test_router_respects_cost_priority() -> None:
    providers = {
        "quality": ConfiguredProvider("quality"),
        "cheap": ConfiguredProvider("cheap"),
    }
    router = ModelRouter(
        providers,
        [
            ModelCandidate("quality", "model-q", 1.0, 0.1, 0.8, 0.9),
            ModelCandidate("cheap", "model-c", 0.7, 1.0, 0.8, 0.9),
        ],
    )

    ranked = router.rank(
        "auto", RoutingPreferences(quality=0.1, cost=0.9, latency=0, reliability=0)
    )

    assert [item.candidate.provider for item in ranked] == ["cheap", "quality"]


def test_router_excludes_unconfigured_providers() -> None:
    provider = ConfiguredProvider("ready")
    disabled = ConfiguredProvider("disabled")
    disabled.api_key = None
    router = ModelRouter(
        {"ready": provider, "disabled": disabled},
        [
            ModelCandidate("ready", "ready-model", 0.5, 0.5, 0.5, 0.5),
            ModelCandidate("disabled", "disabled-model", 1, 1, 1, 1),
        ],
    )

    ranked = router.rank("auto", RoutingPreferences())

    assert [item.candidate.provider for item in ranked] == ["ready"]
