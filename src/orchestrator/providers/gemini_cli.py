from __future__ import annotations

import asyncio
import shutil

from .base import Agent


class GeminiCliAgent(Agent):
    """Wraps Google's `gemini` CLI - authenticates via your Google account
    (OAuth, "Log in with Google"), NOT a Google AI Studio API key. This is
    what lets a Gemini Advanced/Pro/student subscription cover usage instead
    of paying for API access separately, the same way ClaudeCodeAgent rides
    your existing Claude Code login instead of a raw ANTHROPIC_API_KEY.

    First-time setup (one-time, must be done by you interactively - it's
    your Google login, not something this code can do for you):
        gemini
    and follow the "Login with Google" prompt. After that, headless calls
    below reuse the cached credential.
    """

    def __init__(self, name: str = "gemini", tier: str = "planner", model: str | None = None):
        if shutil.which("gemini") is None:
            raise RuntimeError(
                "`gemini` CLI not found on PATH. Install with: npm install -g @google/gemini-cli"
            )
        self.name = name
        self.tier = tier
        self.model = model  # e.g. "gemini-2.5-pro", "gemini-2.5-flash"

    async def complete(self, prompt: str, system: str | None = None) -> str:
        args = ["gemini", "-p", "--output-format", "text"]
        if self.model:
            args += ["-m", self.model]

        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        args.append(full_prompt)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,  # never let it block waiting on a confirmation prompt
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(
                "gemini CLI timed out after 180s - if this is the first call, run `gemini` "
                "once yourself interactively to complete the Google login."
            )
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:2000]
            if "login" in err.lower() or "auth" in err.lower():
                raise RuntimeError(
                    f"gemini CLI auth issue - run `gemini` once interactively to log in "
                    f"with your Google account first. Raw error: {err}"
                )
            raise RuntimeError(f"gemini CLI failed: {err}")
        return stdout.decode(errors="replace").strip()
