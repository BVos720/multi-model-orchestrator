from __future__ import annotations

import asyncio
import shutil

from .base import Agent


class AntigravityCliAgent(Agent):
    """Wraps Google's `agy` (Antigravity) CLI - authenticates via your
    Google account (browser OAuth on first run), NOT a Google AI Studio API
    key. Covers a Gemini subscription's included usage same as
    ClaudeCodeAgent rides your Claude Code login.

    This replaces the old `gemini` CLI: Google fully cut individual/free/Pro/
    Ultra accounts over from Gemini CLI to Antigravity CLI on 2026-06-18 -
    the old CLI now hard-fails with IneligibleTierError for everyone except
    enterprise API-key accounts. If you hit that error, this is why.

    First-time setup (one-time, must be done by you interactively - it's
    your Google login, not something this code can do for you):
        irm https://antigravity.google/cli/install.ps1 | iex   (Windows PowerShell)
        agy
    and follow the browser sign-in. After that, headless calls below reuse
    the cached credential from your OS keyring.
    """

    def __init__(
        self,
        name: str = "gemini",
        tier: str = "planner",
        model: str | None = None,
        effort: str | None = None,
    ):
        if shutil.which("agy") is None:
            raise RuntimeError(
                "`agy` (Antigravity CLI) not found on PATH. Install with (Windows PowerShell): "
                "irm https://antigravity.google/cli/install.ps1 | iex"
            )
        self.name = name
        self.tier = tier
        self.model = model  # a model slug, if you know a current one - optional
        self.effort = effort  # "low" | "medium" | "high" - documented, safer than guessing a model slug

    async def complete(self, prompt: str, system: str | None = None) -> str:
        args = ["agy", "-p", "--output-format", "text"]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]

        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        args.append(full_prompt)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,  # belt-and-suspenders; agy itself soft-denies rather than hangs
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("agy CLI timed out after 180s.")
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:2000]
            if "authentication" in err.lower() or "auth" in err.lower():
                raise RuntimeError(
                    f"agy CLI auth issue - run `agy` once interactively to log in "
                    f"with your Google account first. Raw error: {err}"
                )
            raise RuntimeError(f"agy CLI failed: {err}")
        return stdout.decode(errors="replace").strip()
