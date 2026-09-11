from __future__ import annotations

import itertools
import os

import httpx

from ..hosts_store import HostConfig
from .base import Agent


class OllamaAgent(Agent):
    """Local model pool via Ollama's REST API - free, no tokens spent.

    Each host can run its own model (a 6GB laptop GPU and an 8GB desktop
    3070 don't have the same ceiling), round-robins between whichever hosts
    are actually reachable, and fails over to the next host if one is down,
    busy, or errors. See README "Networking two PCs" for the secure setup
    and `orchestrator settings hosts` for registering machines.
    """

    def __init__(self, name: str = "ollama", tier: str = "local", hosts: list[HostConfig] | None = None):
        self.name = name
        self.tier = tier

        if hosts is None:
            # Legacy single-model env fallback (no named hosts registered yet).
            model = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
            raw = os.environ.get("OLLAMA_HOSTS") or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
            hosts = [
                HostConfig(name=f"host{i}", url=u.strip(), model=model)
                for i, u in enumerate(raw.split(","))
                if u.strip()
            ]
        if not hosts:
            raise RuntimeError("No Ollama hosts configured.")

        self.hosts = hosts
        self._round_robin = itertools.cycle(self.hosts)

    def _ordered_hosts(self) -> list[HostConfig]:
        """Hosts starting from the next round-robin pick, wrapped once."""
        start = next(self._round_robin)
        idx = self.hosts.index(start)
        return self.hosts[idx:] + self.hosts[:idx]

    async def complete(self, prompt: str, system: str | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=300) as client:
            for host in self._ordered_hosts():
                try:
                    resp = await client.post(
                        f"{host.url}/api/chat",
                        json={"model": host.model, "messages": messages, "stream": False},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    return data.get("message", {}).get("content", "").strip()
                except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
                    last_error = e
                    continue  # try the next machine in the pool

            names = ", ".join(f"{h.name}({h.url})" for h in self.hosts)
            raise RuntimeError(
                f"No Ollama host reachable (tried: {names}). "
                f"Is Ollama running on at least one machine? Last error: {last_error}"
            )
