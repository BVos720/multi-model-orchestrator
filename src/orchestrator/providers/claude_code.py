from __future__ import annotations

import asyncio
import shutil

from .base import Agent


class ClaudeCodeAgent(Agent):
    """Wraps the `claude` CLI.

    Uses your existing Claude Code login - no separate API key needed.

    mode="text" (default, used everywhere in the swarm): --permission-mode
    plan with Bash/Edit/Write/NotebookEdit all disallowed - pure text
    generation, it never touches disk.

    mode="code" (only used by `orchestrator code`, never the plan/execute
    swarm): allows Edit/Write so it can actually make changes in the
    current directory, via --permission-mode acceptEdits (auto-accepts file
    edits without an interactive prompt - required for headless use). Bash
    stays disallowed even in this mode - it can write code, not run
    arbitrary shell commands - a deliberate, narrower line than plain
    `claude` gives you interactively.
    """

    def __init__(
        self,
        name: str = "claude-code",
        tier: str = "planner",
        model: str | None = None,
        timeout: float = 180.0,
        mode: str = "text",
    ):
        if shutil.which("claude") is None:
            raise RuntimeError("`claude` CLI not found on PATH. Install/login to Claude Code first.")
        self.name = name
        self.tier = tier
        self.model = model  # e.g. "sonnet", "opus" - alias passed via --model
        self.timeout = timeout
        self.mode = mode

    async def complete(self, prompt: str, system: str | None = None) -> str:
        if self.mode == "code":
            args = [
                "claude", "-p", "--output-format", "text",
                "--permission-mode", "acceptEdits",
                "--disallowedTools", "Bash,NotebookEdit",
                "--no-session-persistence",
            ]
        else:
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
            stdin=asyncio.subprocess.DEVNULL,  # never let it block waiting on a permission/input prompt
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()  # reap it - avoid leaving a zombie/orphaned process behind
            raise RuntimeError(f"claude CLI timed out after {self.timeout}s and was killed.")
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {stderr.decode(errors='replace')[:2000]}")
        return stdout.decode(errors="replace").strip()
