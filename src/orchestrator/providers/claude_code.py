from __future__ import annotations

import asyncio
import json
import shutil

from .. import usage_tracker
from .base import Agent


class ClaudeCodeAgent(Agent):
    """Wraps the `claude` CLI.

    Uses your existing Claude Code login - no separate API key needed.

    mode="text" (default, used everywhere in the swarm): --permission-mode
    plan with Bash/Edit/Write/NotebookEdit all disallowed - pure text
    generation, it never touches disk.

    mode="code" (only used by `orchest code`, never the plan/execute
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

    async def complete(self, prompt: str, system: str | None = None, continue_session: bool = False) -> str:
        """continue_session=True keeps this cwd's real Claude Code session
        alive across calls (via --continue) instead of the isolated,
        memoryless one-shot every swarm step uses - what makes repeated
        `orchest code` calls in the menu's loop actually feel like a normal
        interactive `claude` session (it remembers earlier turns/edits),
        rather than each prompt starting from a blank slate. Only meaningful
        with mode="code"; --continue is a no-op-safe first call too (starts
        fresh if there's nothing yet to continue).

        --output-format json (not "text") is deliberate: it's the only way
        to get real token/cost usage back (see usage_tracker), and it still
        carries the plain answer in its "result" field.

        Argument order below is load-bearing, not cosmetic: --disallowedTools
        takes a value list and (confirmed empirically) greedily swallows
        EVERY following bare word - including the prompt itself - unless
        another recognized --flag immediately follows it. --continue /
        --no-session-persistence are what stop that here; if you ever
        reorder these args, --disallowedTools must still be immediately
        followed by another --flag, never left as the last flag before the
        positional prompt."""
        if self.mode == "code":
            args = [
                "claude", "-p", "--output-format", "json",
                "--permission-mode", "acceptEdits",
                "--disallowedTools", "Bash,NotebookEdit",
            ]
            args.append("--continue" if continue_session else "--no-session-persistence")
        else:
            args = [
                "claude", "-p", "--output-format", "json",
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

        try:
            data = json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError:
            # Fall back to treating stdout as the plain answer - safer than
            # crashing a whole run over a usage-tracking nicety if some
            # future CLI version's --output-format=json output ever changes
            # shape unexpectedly.
            return stdout.decode(errors="replace").strip()

        usage = data.get("usage") or {}
        input_tokens = (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
        )
        usage_tracker.record(self.name, input_tokens, usage.get("output_tokens"), data.get("total_cost_usd"))
        return str(data.get("result", "")).strip()
