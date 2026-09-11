from __future__ import annotations

import asyncio
import shutil

from .base import Agent


class ClaudeCodeAgent(Agent):
    """Wraps the `claude` CLI in non-interactive, read-only mode.

    Uses your existing Claude Code login - no separate API key needed.
    Always runs with --permission-mode plan and every mutating tool
    disallowed: this agent is used purely as a planner/reviewer/text
    generator inside the swarm, it must never edit files on its own.
    """

    def __init__(self, name: str = "claude-code", tier: str = "planner", model: str | None = None):
        if shutil.which("claude") is None:
            raise RuntimeError("`claude` CLI not found on PATH. Install/login to Claude Code first.")
        self.name = name
        self.tier = tier
        self.model = model  # e.g. "sonnet", "opus" - alias passed via --model

    async def complete(self, prompt: str, system: str | None = None) -> str:
        args = [
            "claude", "-p", "--output-format", "text",
            "--permission-mode", "plan",
            "--disallowedTools", "Bash,Edit,Write,NotebookEdit",
            "--no-session-persistence",
        ]
        if self.model:
            args += ["--model", self.model]
        if system:
            args += ["--append-system-prompt", system]
        args.append(prompt)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {stderr.decode(errors='replace')[:2000]}")
        return stdout.decode(errors="replace").strip()
