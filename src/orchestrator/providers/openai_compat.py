from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass

import httpx

from .base import Agent


@dataclass
class _AccountHealth:
    consecutive_failures: int = 0
    cooldown_until: float = 0.0  # epoch seconds - skip this account until then


class OpenAICompatAgent(Agent):
    """Any OpenAI-compatible chat-completions endpoint - OpenAI, DeepSeek,
    Groq, OpenRouter, Cerebras, Mistral, etc. These all require their own
    paid-or-free-tier API key (no subscription/OAuth shortcut like Claude
    Code, the Gemini CLI, or Copilot CLI have).

    Can hold more than one account's key for the same provider (e.g. two
    Groq signups). Selection is weighted, not plain round-robin: an account
    with a higher `weight` gets picked more often in ordinary rotation, but
    every account still gets a turn, so a low-weight account isn't starved -
    it's just not the default choice. On a 429 (rate limited), the account
    goes into cooldown honoring the server's Retry-After header when it
    sends one (else a default cooldown) - "taken out of the pool until
    there are tokens again." Repeated non-429 failures (connection errors,
    5xx) also trigger a cooldown, same as OllamaAgent's circuit breaker. If
    every account is cooling down or at capacity, complete() waits briefly
    and retries (the "wait list") before finally raising, which
    plan_execute's existing escalation path then bumps to a stronger tier.

    Normally constructed by `orchest settings add`/`settings add-account`
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
        weights: list[int] | None = None,
        failure_threshold: int = 3,
        default_cooldown_seconds: float = 60.0,
        wait_timeout: float = 20.0,
    ):
        self.name = name
        self.tier = tier
        self.base_url = base_url
        self.model = model
        if not api_keys:
            raise RuntimeError(f"No API key configured for agent '{name}'")
        self.api_keys = api_keys
        self.weights = weights or [1] * len(api_keys)
        self.failure_threshold = failure_threshold
        self.default_cooldown_seconds = default_cooldown_seconds
        self.wait_timeout = wait_timeout
        self._health: dict[str, _AccountHealth] = {k: _AccountHealth() for k in api_keys}

    def _weighted_order(self) -> list[str]:
        """Weighted-random draw without replacement: higher-weight accounts
        tend to come first, but this isn't deterministic - it spreads load
        rather than always hammering the single highest-weight account."""
        now = time.time()
        pool = [(k, w) for k, w in zip(self.api_keys, self.weights) if self._health[k].cooldown_until <= now]
        if not pool:  # everyone's cooling down - try anyway, oldest cooldown first
            pool = sorted(zip(self.api_keys, self.weights), key=lambda kw: self._health[kw[0]].cooldown_until)

        remaining = list(pool)
        order: list[str] = []
        while remaining:
            total = sum(w for _, w in remaining)
            pick = random.uniform(0, total)
            upto = 0.0
            for i, (k, w) in enumerate(remaining):
                upto += w
                if upto >= pick:
                    order.append(k)
                    remaining.pop(i)
                    break
        return order

    def _note_failure(self, key: str, cooldown_seconds: float | None = None) -> None:
        h = self._health[key]
        h.consecutive_failures += 1
        if cooldown_seconds is not None:
            h.cooldown_until = time.time() + cooldown_seconds
        elif h.consecutive_failures >= self.failure_threshold:
            h.cooldown_until = time.time() + self.default_cooldown_seconds

    def _note_success(self, key: str) -> None:
        h = self._health[key]
        h.consecutive_failures = 0
        h.cooldown_until = 0.0

    async def complete(self, prompt: str, system: str | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        deadline = time.time() + self.wait_timeout
        last_error: Exception | None = None

        async with httpx.AsyncClient(timeout=120) as client:
            while True:
                for key in self._weighted_order():
                    try:
                        resp = await client.post(
                            f"{self.base_url}/chat/completions",
                            headers={"Authorization": f"Bearer {key}"},
                            json={"model": self.model, "messages": messages},
                        )
                        if resp.status_code == 429:
                            retry_after = resp.headers.get("Retry-After")
                            cooldown = float(retry_after) if retry_after and retry_after.strip().isdigit() else None
                            self._note_failure(key, cooldown_seconds=cooldown or self.default_cooldown_seconds)
                            last_error = RuntimeError(f"429 rate limited (cooldown {cooldown or self.default_cooldown_seconds}s)")
                            continue
                        resp.raise_for_status()
                        self._note_success(key)
                        data = resp.json()
                        return data["choices"][0]["message"]["content"].strip()
                    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
                        self._note_failure(key)
                        last_error = e
                        continue

                if time.time() >= deadline:
                    raise RuntimeError(
                        f"All {len(self.api_keys)} account(s) for '{self.name}' rate-limited/failed "
                        f"within {self.wait_timeout}s. Last error: {last_error}"
                    )
                await asyncio.sleep(1)
