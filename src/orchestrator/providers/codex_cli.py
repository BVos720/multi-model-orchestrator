from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from .. import usage_tracker
from .base import Agent


class CodexCliAgent(Agent):
    """Wraps OpenAI's `codex` CLI, via `codex exec` (its headless/non-
    interactive subcommand - confirmed empirically to never block on an
    approval prompt in either sandbox mode below: `codex exec` always
    reports "approval: never" regardless of --sandbox).

    mode="text" (default, used everywhere in the swarm): --sandbox
    read-only - pure text generation/analysis, any attempted write is
    sandboxed/blocked, matching ClaudeCodeAgent's mode="text".

    mode="code" (only used by `orchest code`, never the plan/execute
    swarm): --sandbox workspace-write, confined to `cwd` via -C - it can
    actually create/edit files there (and, per Codex's own sandbox,
    inside the OS temp dir too - not this project's config, Codex's own
    default exception for scratch files).

    Output is captured via --output-last-message to a temp file rather
    than scraped from stdout, which also prints the full turn-by-turn
    transcript (diffs, tool calls, token counts) - the temp file gets
    just the final clean response text.
    """

    def __init__(
        self,
        name: str = "codex",
        tier: str = "planner",
        model: str | None = None,
        timeout: float = 180.0,
        mode: str = "text",
    ):
        # Resolve and store the full path (not just check existence) -
        # `codex` ships as a `codex.cmd` npm shim with no native .exe, and
        # asyncio.create_subprocess_exec on Windows can't launch a bare
        # ".cmd" command name directly (confirmed: FileNotFoundError even
        # though shutil.which finds it) - only the fully-resolved path
        # (extension included) actually works.
        self._binary = shutil.which("codex")
        if self._binary is None:
            raise RuntimeError("`codex` CLI not found on PATH. Install/login to Codex first.")
        self.name = name
        self.tier = tier
        self.model = model
        self.timeout = timeout
        self.mode = mode

    async def complete(self, prompt: str, system: str | None = None, cwd: str | None = None) -> str:
        sandbox = "workspace-write" if self.mode == "code" else "read-only"
        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            output_path = f.name

        args = [
            self._binary, "exec",
            "--sandbox", sandbox,
            "--skip-git-repo-check",
            "--json",  # JSONL to stdout - the only way to get real token usage back
            "--output-last-message", output_path,
        ]
        if cwd:
            args += ["-C", cwd]
        if self.model:
            args += ["-m", self.model]
        args.append(full_prompt)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,  # never let it block waiting on stdin/a prompt
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()  # reap it - avoid leaving a zombie/orphaned process behind
                raise RuntimeError(f"codex CLI timed out after {self.timeout}s and was killed.")
            if proc.returncode != 0:
                # Confirmed by hitting a real exhausted-quota account: codex's
                # actual error/limit message often lands in stdout (as a
                # --json event) rather than stderr - a RuntimeError built
                # from stderr alone can be an unhelpful "Reading additional
                # input from stdin..." with the real reason silently
                # dropped, which also means _is_quota_error() never gets a
                # chance to recognize it. Include both, stdout first, since
                # that's where the actually useful text tends to be.
                err = stdout.decode(errors="replace").strip()
                stderr_text = stderr.decode(errors="replace").strip()
                if stderr_text:
                    err = f"{err}\n{stderr_text}" if err else stderr_text
                raise RuntimeError(f"codex CLI failed: {err[:2000]}")

            input_tokens = output_tokens = None
            for line in stdout.decode(errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "turn.completed":
                    usage = event.get("usage") or {}
                    input_tokens = usage.get("input_tokens")
                    output_tokens = usage.get("output_tokens")
            usage_tracker.record(self.name, input_tokens, output_tokens)

            path = Path(output_path)
            return path.read_text(encoding="utf-8", errors="replace").strip() if path.exists() else ""
        finally:
            try:
                os.unlink(output_path)
            except OSError:
                pass
