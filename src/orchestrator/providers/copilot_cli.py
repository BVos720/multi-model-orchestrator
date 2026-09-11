from __future__ import annotations

import asyncio
import shutil

from .base import Agent


class CopilotCliAgent(Agent):
    """Wraps GitHub's `copilot` CLI - authenticates via your GitHub account
    (device-code login), NOT a separate API key. Usage draws on your GitHub
    Copilot plan (Free/Pro/Business/Enterprise), the same "ride the existing
    subscription login" pattern as ClaudeCodeAgent and AntigravityCliAgent.

    First-time setup (one-time, must be done by you interactively - it's
    your GitHub login, not something this code can do for you):
        npm install -g @github/copilot
        copilot
    and follow the device-code login prompt. After that, headless calls
    below reuse the cached credential.

    Runs with no --allow-tool/--allow-all-tools granted, so it can't act on
    the filesystem or shell - it only ever generates text here, mirroring
    how ClaudeCodeAgent is locked to --permission-mode plan.
    """

    def __init__(self, name: str = "copilot", tier: str = "planner", model: str | None = None):
        if shutil.which("copilot") is None:
            raise RuntimeError(
                "`copilot` CLI not found on PATH. Install with: npm install -g @github/copilot"
            )
        self.name = name
        self.tier = tier
        self.model = model  # e.g. "claude-sonnet-4.6", "gpt-5" - whatever your plan offers

    async def complete(self, prompt: str, system: str | None = None) -> str:
        args = ["copilot", "-p", "-s", "--no-ask-user"]
        if self.model:
            args += [f"--model={self.model}"]

        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        args.append(full_prompt)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,  # never let it block waiting on a tool-approval prompt
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(
                "copilot CLI timed out after 180s - if this is the first call, run `copilot` "
                "once yourself interactively to complete the GitHub device-code login."
            )
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:2000]
            if "login" in err.lower() or "auth" in err.lower():
                raise RuntimeError(
                    f"copilot CLI auth issue - run `copilot` once interactively to log in "
                    f"with your GitHub account first. Raw error: {err}"
                )
            raise RuntimeError(f"copilot CLI failed: {err}")
        return stdout.decode(errors="replace").strip()
