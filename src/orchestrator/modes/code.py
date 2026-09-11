from __future__ import annotations

import shutil

from ..providers.antigravity_cli import AntigravityCliAgent
from ..providers.claude_code import ClaudeCodeAgent
from ..providers.copilot_cli import CopilotCliAgent

# (display name, PATH binary to check, constructor) - tried in this order.
# Claude Code first: its --disallowedTools gives the narrowest, best-
# understood grant (files, not shell). Preference order is otherwise just a
# reasonable default, not a quality judgment between the three.
_CANDIDATES = [
    ("claude-code", "claude", lambda: ClaudeCodeAgent(mode="code", timeout=600.0)),
    ("antigravity", "agy", lambda: AntigravityCliAgent(mode="code", timeout=600.0)),
    ("copilot", "copilot", lambda: CopilotCliAgent(mode="code", timeout=600.0)),
]


def pick_agent():
    """Return (name, Agent) for the first available coding-capable CLI, or
    raise RuntimeError listing what's missing if none are installed."""
    for name, binary, build in _CANDIDATES:
        if shutil.which(binary):
            return name, build()
    tried = ", ".join(n for n, _, _ in _CANDIDATES)
    raise RuntimeError(
        f"No coding-capable CLI found on PATH (tried: {tried}). Install and log in to at "
        f"least one: Claude Code (`claude`), Antigravity CLI (`agy`), or GitHub Copilot CLI (`copilot`)."
    )


async def run(task: str) -> str:
    """Unlike plan_execute.run (the read-only swarm), this is a direct,
    single pass-through to whichever real coding-agent CLI is available,
    running in the CURRENT working directory with file-editing permissions
    on - the same trust model as running that CLI yourself. No local-model
    routing here: Ollama has no native file-editing tool use, so this mode
    is scoped to the CLIs that already have real Edit/Write/Bash tooling.
    """
    name, agent = pick_agent()
    print(f"Using {name} to code in this folder (see README for what each CLI's --mode=code actually grants)...\n")
    return await agent.complete(task)
