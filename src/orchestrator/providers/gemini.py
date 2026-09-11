from __future__ import annotations

import os

from .base import Agent

try:
    from google import genai
except ImportError:  # pragma: no cover
    genai = None


class GeminiAgent(Agent):
    def __init__(self, name: str = "gemini", tier: str = "planner", model: str = "gemini-2.5-pro"):
        if genai is None:
            raise RuntimeError("google-genai not installed. Run: pip install google-genai")
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY not set. Get a free key at "
                "https://aistudio.google.com/apikey and put it in .env"
            )
        self.name = name
        self.tier = tier
        self.model = model
        self._client = genai.Client(api_key=api_key)

    async def complete(self, prompt: str, system: str | None = None) -> str:
        config = {"system_instruction": system} if system else None
        response = await self._client.aio.models.generate_content(
            model=self.model, contents=prompt, config=config,
        )
        return (response.text or "").strip()
