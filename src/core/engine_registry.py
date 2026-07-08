from dataclasses import dataclass

from src.tts.interface import ITTSEngine


@dataclass
class EngineRegistry:
    engines: dict[str, ITTSEngine]
    default_model: str

    def resolve(self, requested: str | None) -> tuple[str, ITTSEngine]:
        if requested and requested in self.engines:
            return requested, self.engines[requested]
        return self.default_model, self.engines[self.default_model]

    async def aclose(self) -> None:
        for engine in self.engines.values():
            await engine.aclose()

    def readiness(self) -> dict[str, bool]:
        """is_ready per engine id, e.g. for GET /ready."""
        return {model_id: engine.is_ready for model_id, engine in self.engines.items()}
