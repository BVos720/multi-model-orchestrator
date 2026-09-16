from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from .. import usage_tracker
from .base import Agent


def _resolve_launch_argv(binary: str) -> list[str]:
    """`copilot` on Windows is a `copilot.cmd` npm shim, not a native .exe.
    Launching it directly (even by its full resolved path) still routes
    through cmd.exe under the hood to interpret the batch file, which
    re-splits the command line with its own cruder quoting rules and
    mangles any multi-word prompt argument (confirmed: "Invalid command
    format" from a correctly-argv-passed prompt). The shim itself is just
    `node "<npm-dir>\\node_modules\\@github\\copilot\\npm-loader.js" %*`
    (read directly from copilot.cmd) - invoking that real node.exe/script
    pair ourselves sidesteps cmd.exe entirely, so normal argv passing
    works exactly like any other native executable. Falls back to the
    bare .cmd path if node or the loader script can't be found (e.g. a
    future npm layout change), so this degrades rather than hard-fails."""
    if not binary.lower().endswith(".cmd"):
        return [binary]
    node = shutil.which("node")
    loader = Path(binary).parent / "node_modules" / "@github" / "copilot" / "npm-loader.js"
    if node and loader.is_file():
        return [node, str(loader)]
    return [binary]


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

    mode="text" (default, used everywhere in the swarm): no --allow-tool/
    --allow-all-tools granted, so it can't act on the filesystem or shell -
    it only ever generates text here, mirroring ClaudeCodeAgent's
    --permission-mode plan.

    mode="code" (only used by `orchest code`): adds --allow-all-tools
    (every tool auto-approved, no per-call confirmation) - coarser than
    Claude Code's Edit/Write-only allowance, since Copilot CLI's flags don't
    confirmedly expose a "files but not shell" split the way disallowedTools
    does. Documented as such in `orchest code`'s help text.
    """

    def __init__(
        self,
        name: str = "copilot",
        tier: str = "planner",
        model: str | None = None,
        mode: str = "text",
        timeout: float = 180.0,
    ):
        binary = shutil.which("copilot")
        if binary is None:
            raise RuntimeError(
                "`copilot` CLI not found on PATH. Install with: npm install -g @github/copilot"
            )
        self._launch = _resolve_launch_argv(binary)
        self.name = name
        self.tier = tier
        self.model = model  # e.g. "claude-sonnet-4.6", "gpt-5" - whatever your plan offers
        self.mode = mode
        self.timeout = timeout

    async def complete(self, prompt: str, system: str | None = None) -> str:
        # -p's value must immediately follow it - confirmed empirically:
        # copilot's own arg parser rejects it if any other flag comes
        # between -p and the prompt text ("Invalid command format...
        # extra words treated as separate arguments"), even though that's
        # a perfectly normal, correctly-quoted argv element by the time
        # it reaches this process. So -p and the prompt go last.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            usage_path = f.name

        args = [*self._launch, "-s", "--no-ask-user", "--usage-output-file", usage_path]
        if self.model:
            args += [f"--model={self.model}"]
        if self.mode == "code":
            args += ["--allow-all-tools"]

        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        args += ["-p", full_prompt]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,  # never let it block waiting on a tool-approval prompt
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()  # reap it - avoid leaving a zombie/orphaned process behind
                raise RuntimeError(
                    f"copilot CLI timed out after {self.timeout}s - if this is the first call, run "
                    f"`copilot` once yourself interactively to complete the GitHub device-code login."
                )
            if proc.returncode != 0:
                err = stderr.decode(errors="replace")[:2000]
                if "login" in err.lower() or "auth" in err.lower():
                    raise RuntimeError(
                        f"copilot CLI auth issue - run `copilot` once interactively to log in "
                        f"with your GitHub account first. Raw error: {err}"
                    )
                raise RuntimeError(f"copilot CLI failed: {err}")

            input_tokens = output_tokens = None
            try:
                usage_data = json.loads(Path(usage_path).read_text(encoding="utf-8"))
                input_tokens = usage_data.get("lastCallInputTokens")
                output_tokens = usage_data.get("lastCallOutputTokens")
            except (OSError, json.JSONDecodeError):
                pass
            usage_tracker.record(self.name, input_tokens, output_tokens)

            return stdout.decode(errors="replace").strip()
        finally:
            try:
                os.unlink(usage_path)
            except OSError:
                pass
