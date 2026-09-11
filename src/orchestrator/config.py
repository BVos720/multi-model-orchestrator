from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from dotenv import load_dotenv

from .providers.base import Agent
from .providers.claude_code import ClaudeCodeAgent
from .providers.gemini import GeminiAgent
from .providers.gemini_cli import GeminiCliAgent
from .providers.ollama import OllamaAgent
from .providers.openai_compat import OpenAICompatAgent
from .hosts_store import HostsStore
from .settings_store import SettingsStore

load_dotenv()


@dataclass
class Fleet:
    planners: list[Agent]  # heavy models: write plans, do final review
    cloud: list[Agent]     # mid-weight paid models: escalated/complex steps
    local: Agent | None    # Ollama pool: cheap/simple steps


def build_fleet(local_hosts: list[str] | None = None) -> Fleet:
    """local_hosts: names of registered Ollama hosts (see hosts_store.py) to
    restrict this run's local tier to. None = use every registered host
    (or the legacy single-pool env fallback if none are registered)."""
    planners: list[Agent] = []
    cloud: list[Agent] = []
    local: Agent | None = None

    if shutil.which("claude"):
        planners.append(ClaudeCodeAgent(name="claude-code", tier="planner"))
    else:
        print("! claude CLI not found on PATH - skipping Claude Code agent")

    # Prefer the Gemini CLI (your Google account login - covers a Gemini
    # subscription's included usage, no separate billed API key) over the
    # API-key-based agent. Falls back to the API key only if the CLI isn't
    # installed but GEMINI_API_KEY is set.
    if shutil.which("gemini"):
        try:
            planners.append(GeminiCliAgent(name="gemini", tier="planner"))
        except Exception as e:
            print(f"! Gemini CLI agent unavailable - {e}")
    elif os.environ.get("GEMINI_API_KEY"):
        try:
            planners.append(GeminiAgent(name="gemini", tier="planner"))
        except Exception as e:
            print(f"! Gemini agent unavailable - {e}")
    else:
        print(
            "! No Gemini access - either `npm install -g @google/gemini-cli` and log in "
            "with your Google account (no key needed), or set GEMINI_API_KEY in .env"
        )

    for p in SettingsStore().load():
        key = os.environ.get(p.api_key_env)
        if not key:
            print(f"! {p.name}: {p.api_key_env} not set - run `orchestrator settings add {p.name}`")
            continue
        try:
            agent = OpenAICompatAgent(name=p.name, tier=p.tier, api_key=key, base_url=p.base_url, model=p.model)
            (planners if p.tier == "planner" else cloud).append(agent)
        except Exception as e:
            print(f"! {p.name} agent unavailable - {e}")

    try:
        registered = HostsStore().load()
        if registered:
            selected = [h for h in registered if not local_hosts or h.name in local_hosts]
            if not selected:
                raise RuntimeError(
                    f"No registered host matches {local_hosts}. "
                    f"Known: {[h.name for h in registered]}"
                )
            local = OllamaAgent(name="ollama", tier="local", hosts=selected)
        else:
            local = OllamaAgent(name="ollama", tier="local")  # legacy OLLAMA_HOST(S) env fallback
    except Exception as e:
        print(f"! Ollama unavailable - {e}")

    if not planners:
        raise RuntimeError(
            "No planner-tier agent available. Set up the Claude Code CLI "
            "(`claude` on PATH, logged in) and/or the Gemini CLI / GEMINI_API_KEY."
        )

    return Fleet(planners=planners, cloud=cloud, local=local)
