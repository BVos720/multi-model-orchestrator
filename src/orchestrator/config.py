from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from dotenv import load_dotenv

from .providers.base import Agent
from .providers.claude_code import ClaudeCodeAgent
from .providers.copilot_cli import CopilotCliAgent
from .providers.gemini import GeminiAgent
from .providers.gemini_cli import GeminiCliAgent
from .providers.ollama import OllamaAgent
from .providers.openai_compat import OpenAICompatAgent
from .hosts_store import HostsStore
from .settings_store import SettingsStore

load_dotenv()


@dataclass
class Fleet:
    planners: list[Agent]        # heavy/default models: write plans, do final review, strong escalations
    cloud_fast: list[Agent]      # cheap+fast variants (haiku, gemini-flash): steps too big for local but not high-stakes
    cloud: list[Agent]           # custom OpenAI-compatible agents (DeepSeek, Groq, ...): escalated/complex steps
    local: Agent | None          # Ollama pool: cheap/simple steps


def build_fleet(local_hosts: list[str] | None = None) -> Fleet:
    """local_hosts: names of registered Ollama hosts (see hosts_store.py) to
    restrict this run's local tier to. None = use every registered host
    (or the legacy single-pool env fallback if none are registered)."""
    planners: list[Agent] = []
    cloud_fast: list[Agent] = []
    cloud: list[Agent] = []
    local: Agent | None = None

    if shutil.which("claude"):
        planners.append(ClaudeCodeAgent(name="claude-code", tier="planner"))
        try:
            # "haiku" is a rolling alias Claude Code resolves to its own
            # current fast/cheap model - not pinned here, so this stays
            # correct as Anthropic ships new Haiku releases.
            cloud_fast.append(ClaudeCodeAgent(name="claude-code-haiku", tier="cloud-fast", model="haiku"))
        except Exception as e:
            print(f"! claude-code-haiku (fast tier) unavailable - {e}")
    else:
        print("! claude CLI not found on PATH - skipping Claude Code agent")

    # Prefer the Gemini CLI (your Google account login - covers a Gemini
    # subscription's included usage, no separate billed API key) over the
    # API-key-based agent. Falls back to the API key only if the CLI isn't
    # installed but GEMINI_API_KEY is set.
    if shutil.which("gemini"):
        try:
            planners.append(GeminiCliAgent(name="gemini", tier="planner"))
            # gemini-flash-latest is Google's own rolling alias for their
            # current fast/cheap Flash model - same reasoning as "haiku" above.
            cloud_fast.append(GeminiCliAgent(name="gemini-flash", tier="cloud-fast", model="gemini-flash-latest"))
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

    if shutil.which("copilot"):
        try:
            planners.append(CopilotCliAgent(name="copilot", tier="planner"))
        except Exception as e:
            print(f"! Copilot CLI agent unavailable - {e}")
    else:
        print(
            "! No GitHub Copilot access - `npm install -g @github/copilot` and log in "
            "with your GitHub account (needs a Copilot plan; no separate API key)"
        )

    for p in SettingsStore().load():
        # keep keys/weights aligned - only accounts that actually have a key set
        keyed = [(os.environ.get(a.api_key_env), a.weight) for a in p.accounts]
        keyed = [(k, w) for k, w in keyed if k]
        missing = len(p.accounts) - len(keyed)
        if not keyed:
            print(f"! {p.name}: no account has a key set - run `orchestrator settings add {p.name}`")
            continue
        if missing:
            print(f"! {p.name}: {missing} account(s) missing their key, pooling the other {len(keyed)}")
        try:
            agent = OpenAICompatAgent(
                name=p.name, tier=p.tier, base_url=p.base_url, model=p.model,
                api_keys=[k for k, _ in keyed], weights=[w for _, w in keyed],
            )
            target = {"planner": planners, "cloud-fast": cloud_fast}.get(p.tier, cloud)
            target.append(agent)
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

    return Fleet(planners=planners, cloud_fast=cloud_fast, cloud=cloud, local=local)
