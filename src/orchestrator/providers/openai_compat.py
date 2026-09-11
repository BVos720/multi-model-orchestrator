from __future__ import annotations

import itertools

import httpx

from .base import Agent


class OpenAICompatAgent(Agent):
    """Any OpenAI-compatible chat-completions endpoint - OpenAI, DeepSeek,
    Groq, OpenRouter, Cerebras, Mistral, etc. These all require their own
    paid-or-free-tier API key (no subscription/OAuth shortcut like Claude
    Code, the Gemini CLI, or Copilot CLI have).

    Can hold more than one account's key for the same provider (e.g. two
    Groq signups) - round-robins between them and fails over to the next on
    an error (a 429 from one rate-limited account just moves on to the
    other), the same pattern OllamaAgent uses for pooling machines.

    Normally constructed by `orchestrator settings add`/`settings add-account`
    (see actions.py) rather than directly - those write keys to .env and the
    rest of this config to .orchestrator/providers.json.
    """

    def __init__(
        self,
        name: str,
        tier: str = "cloud",
        base_url: str | None = None,
        model: str | None = None,
        api_keys: list[str] | None = None,
    ):
        self.name = name
        self.tier = tier
        self.base_url = base_url
        self.model = model
        if not api_keys:
            raise RuntimeError(f"No API key configured for agent '{name}'")
        self.api_keys = api_keys
        self._round_robin = itertools.cycle(range(len(api_keys)))

    def _ordered_keys(self) -> list[str]:
        start = next(self._round_robin)
        return self.api_keys[start:] + self.api_keys[:start]

    async def complete(self, prompt: str, system: str | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=120) as client:
            for key in self._ordered_keys():
                try:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"model": self.model, "messages": messages},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    return data["choices"][0]["message"]["content"].strip()
                except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
                    last_error = e
                    continue  # rate-limited/errored account - try the next one in the pool

            raise RuntimeError(
                f"All {len(self.api_keys)} account(s) for '{self.name}' failed. Last error: {last_error}"
            )
