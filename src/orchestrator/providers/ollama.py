from __future__ import annotations

import asyncio
import itertools
import os
import time
from dataclasses import dataclass, field

import httpx

from .. import dispatch_log, usage_tracker
from ..hosts_store import HostConfig
from .base import Agent


@dataclass
class _HostHealth:
    consecutive_failures: int = 0
    cooldown_until: float = 0.0  # epoch seconds - skip this host until then
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))


class OllamaAgent(Agent):
    """Local model pool via Ollama's REST API - free, no tokens spent.

    Each host can run its own model (a 6GB laptop GPU and an 8GB desktop
    3070 don't have the same ceiling; register the same machine twice under
    different names to offer both a small and a big model - see
    HostConfig.size). This is where "the supervisor and workers decide for
    themselves if a model is holding things up" actually lives:

    - Each host gets its own concurrency limit (max_concurrent_per_host,
      default 1 - a single loaded Ollama model serializes requests anyway,
      so piling more on just slows everything down).
    - A host that fails `failure_threshold` times in a row goes into
      cooldown and gets skipped for `cooldown_seconds` - it "removes
      itself" rather than repeatedly stalling every call on a dead machine.
    - If every host is either at capacity or in cooldown, complete() is the
      "wait list": it polls briefly and retries for up to `wait_timeout`
      seconds before finally raising (which plan_execute's existing
      escalate-on-failure path then bumps up to cloud-fast/cloud) - a task
      is held for a bit in case a slot frees up, instead of being bounced
      to a paid model over what might be a two-second blip.
    """

    def __init__(
        self,
        name: str = "ollama",
        tier: str = "local",
        hosts: list[HostConfig] | None = None,
        max_concurrent_per_host: int = 1,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        wait_timeout: float = 30.0,
    ):
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
        self.wait_timeout = wait_timeout
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._round_robin = itertools.cycle(self.hosts)
        self._health: dict[str, _HostHealth] = {
            h.name: _HostHealth(semaphore=asyncio.Semaphore(max_concurrent_per_host)) for h in self.hosts
        }

    def _ordered_hosts(self, prefer_size: str | None) -> list[HostConfig]:
        """Round-robin starting point, then sorted so a size match comes first."""
        start = next(self._round_robin)
        idx = self.hosts.index(start)
        ordered = self.hosts[idx:] + self.hosts[:idx]
        if prefer_size:
            ordered.sort(key=lambda h: h.size != prefer_size)
        return ordered

    def _is_cooled_down(self, host: HostConfig) -> bool:
        return time.time() < self._health[host.name].cooldown_until

    def _note_failure(self, host: HostConfig) -> None:
        h = self._health[host.name]
        h.consecutive_failures += 1
        if h.consecutive_failures >= self.failure_threshold:
            h.cooldown_until = time.time() + self.cooldown_seconds

    def _note_success(self, host: HostConfig) -> None:
        h = self._health[host.name]
        h.consecutive_failures = 0
        h.cooldown_until = 0.0

    async def _dispatch(self, build_body, prefer_size: str | None = None) -> dict:
        """Shared host-selection/retry/failover loop - the actual pooling
        logic behind complete(); split out so raw_chat() (tool-calling,
        used by local_coder's agentic file-editing loop) doesn't have to
        duplicate it. build_body(host) returns this call's JSON body for
        that host (so the right host.model gets substituted in unless
        overridden) - returns Ollama's raw parsed JSON response, from
        whichever host actually served it."""
        deadline = time.time() + self.wait_timeout
        last_error: Exception | None = None

        async with httpx.AsyncClient(timeout=300) as client:
            while True:
                candidates = [h for h in self.hosts if not self._is_cooled_down(h)] or self.hosts
                for host in self._ordered_hosts(prefer_size):
                    if host not in candidates:
                        continue  # in cooldown, and at least one other host isn't
                    sem = self._health[host.name].semaphore
                    if sem.locked():
                        continue  # this host is at its concurrency limit - try the next one
                    async with sem:
                        try:
                            body = build_body(host)
                            preview = "\n".join(
                                f"[{m.get('role')}] {m.get('content', '')}" for m in body.get("messages", [])
                            )
                            dispatch_log.record(host.name, host.url, body.get("model", host.model), preview)
                            resp = await client.post(f"{host.url}/api/chat", json=body)
                            resp.raise_for_status()
                            self._note_success(host)
                            return resp.json()
                        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
                            self._note_failure(host)
                            last_error = e
                            continue

                # Every host was either in cooldown or at capacity this pass -
                # the "wait list": hold briefly and retry rather than fail fast.
                if time.time() >= deadline:
                    names = ", ".join(f"{h.name}({h.url})" for h in self.hosts)
                    raise RuntimeError(
                        f"No Ollama host had capacity within {self.wait_timeout}s (tried: {names}). "
                        f"All busy or unhealthy. Last error: {last_error}"
                    )
                await asyncio.sleep(1)

    async def complete(self, prompt: str, system: str | None = None, prefer_size: str | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        data = await self._dispatch(
            lambda host: {"model": host.model, "messages": messages, "stream": False}, prefer_size
        )
        usage_tracker.record(self.name, data.get("prompt_eval_count"), data.get("eval_count"))
        return data.get("message", {}).get("content", "").strip()

    async def raw_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        prefer_size: str | None = None,
    ) -> dict:
        """Like complete(), but returns Ollama's raw response dict (so a
        caller can see message.tool_calls, not just the text content) and
        takes a full `messages` list plus optional `tools` - what
        local_coder's agentic file-editing loop needs for real tool-use,
        instead of the plain text-completion every other caller gets.

        model, if given, overrides whichever host's own configured model
        for this call only (e.g. force a bigger local model for more
        reliable tool-calling, accepting it'll be slower per call) -
        falls back to the picked host's normal model otherwise."""

        def build_body(host):
            body = {"model": model or host.model, "messages": messages, "stream": False}
            if tools:
                body["tools"] = tools
            return body

        data = await self._dispatch(build_body, prefer_size)
        usage_tracker.record(self.name, data.get("prompt_eval_count"), data.get("eval_count"))
        return data
