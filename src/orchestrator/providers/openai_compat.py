from __future__ import annotations

import os

import httpx

from .base import Agent


class OpenAICompatAgent(Agent):
    """Any OpenAI-compatible chat-completions endpoint - OpenAI, DeepSeek,
    Groq, OpenRouter, etc. These all require their own paid API key (no
    subscription/OAuth shortcut like Claude Code or the Gemini CLI have).

    Normally constructed by `orchestrator settings add <name>` (see
    settings_store.py) rather than directly - that command writes the key to
    .env and the rest of this config to .orchestrator/providers.json.
    """

    def __init__(
        self,
        name: str,
        tier: str = "cloud",
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ):
        self.name = name
        self.tier = tier
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        if not self.api_key:
            raise RuntimeError(f"No API key configured for agent '{name}'")

    async def complete(self, prompt: str, system: str | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "messages": messages},
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
