from __future__ import annotations

from abc import ABC, abstractmethod


class Agent(ABC):
    """A model 'worker' the orchestrator can call.

    tier is one of:
      "planner" - heavy/expensive model, writes plans and does final review
      "cloud"   - mid-weight paid model, used for escalated/complex steps
      "local"   - free local model (Ollama), used for small/simple steps
    """

    name: str
    tier: str

    @abstractmethod
    async def complete(self, prompt: str, system: str | None = None) -> str:
        """Send a single-turn request and return the text response."""
        ...

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Agent {self.name} tier={self.tier}>"
