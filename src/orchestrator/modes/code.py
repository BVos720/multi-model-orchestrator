from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess

from .. import local_coder
from ..folder_lock import folder_lock, sync_folder_lock
from ..project_context import with_project_context
from ..providers.antigravity_cli import AntigravityCliAgent
from ..providers.claude_code import ClaudeCodeAgent
from ..providers.codex_cli import CodexCliAgent
from ..providers.copilot_cli import CopilotCliAgent

# (display name, PATH binary to check, constructor) - tried in this order.
# Claude Code first: its --disallowedTools gives the narrowest, best-
# understood grant (files, not shell). Preference order is otherwise just a
# reasonable default, not a quality judgment between the four.
_CANDIDATES = [
    ("claude-code", "claude", lambda: ClaudeCodeAgent(mode="code", timeout=600.0)),
    ("antigravity", "agy", lambda: AntigravityCliAgent(mode="code", timeout=600.0)),
    ("copilot", "copilot", lambda: CopilotCliAgent(mode="code", timeout=600.0)),
    ("codex", "codex", lambda: CodexCliAgent(mode="code", timeout=600.0)),
]

# Same binaries, same preference order, but for a genuine interactive
# hand-off rather than the headless/capture-output path above - just the
# bare CLI invocation, no --disallowedTools/--permission-mode/--sandbox
# overrides.
_INTERACTIVE_CANDIDATES = [
    ("claude-code", "claude", lambda task: ["claude"] + ([task] if task else [])),
    ("antigravity", "agy", lambda task: ["agy"] + ([task] if task else [])),
    ("copilot", "copilot", lambda task: ["copilot"] + ([task] if task else [])),
    ("codex", "codex", lambda task: ["codex"] + ([task] if task else [])),
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


async def run(task: str, agent=None, continue_session: bool = False) -> str:
    """Unlike plan_execute.run (the read-only swarm), this is a direct,
    single pass-through to whichever real coding-agent CLI is available,
    running headless in the CURRENT working directory with file-editing
    auto-accepted - narrower than actually running that CLI yourself,
    though: Bash/arbitrary commands stay disallowed here on purpose (no
    prompts to answer in headless mode, so nothing risky can quietly run
    unattended). No local-model routing: Ollama has no native file-editing
    tool use, so this mode is scoped to CLIs with real Edit/Write tooling.
    See `run_interactive` for the real, full-permission, prompted version.

    Pass an already-picked `agent` (from `pick_agent()`) to reuse the same
    one across repeated calls instead of re-detecting it each time - what
    the menu's coding loop does, so it doesn't reprint "Using X..." every
    prompt. continue_session=True carries real conversation memory across
    calls (Claude Code only, via --continue) - ignored for CLIs that don't
    support it.
    """
    if agent is None:
        name, agent = pick_agent()
        print(f"Using {name} to code in this folder (see README for what each CLI's --mode=code actually grants)...\n")
    else:
        name = agent.name
    kwargs = {"continue_session": True} if continue_session and isinstance(agent, ClaudeCodeAgent) else {}
    async with folder_lock(os.getcwd(), owner=name):
        return await agent.complete(with_project_context(task), **kwargs)


def run_interactive(task: str | None = None, cwd: str | None = None) -> bool:
    """Hand off directly to a REAL, fully-interactive coding CLI session -
    unlike `run()` (headless, edits auto-accepted, Bash disallowed), this
    is genuinely the same trust model as typing `claude`/`agy`/`copilot`
    yourself: full tool access including Bash - installing packages,
    scaffolding a whole project, anything - gated entirely by that CLI's
    own real "allow this?" prompts, not anything this wrapper controls.

    `cwd` pins which folder it launches in - pass this explicitly (the
    menu makes the user confirm it) rather than relying on whatever
    directory happened to be current. Be clear-eyed about what that
    actually buys you: Claude Code's Read/Write/Edit tools stay scoped to
    the launched folder and below by default (nothing here ever passes
    --add-dir to grant more), but Bash is a real shell - `cd ..` or an
    absolute path can still reach outside it. There's no OS-level sandbox
    here; the actual boundary against that is the permission prompt on
    each Bash command, which is exactly why this mode keeps them on
    rather than trying to fake a hard filesystem jail this wrapper can't
    truly enforce.

    Runs in the foreground with the terminal's stdio inherited (not
    captured), so it blocks until you exit that session (Ctrl+D or
    /exit) - there's no "result text" to return here, unlike every other
    mode, because you're conversing with it directly in real time.
    Returns False if no coding-capable CLI is on PATH (and prints the
    same guidance `pick_agent()`'s error would)."""
    for name, binary, build_argv in _INTERACTIVE_CANDIDATES:
        if shutil.which(binary):
            print(
                f"Handing off to a real, interactive {name} session in {cwd or '.'} (full permissions incl. "
                "Bash - its own prompts apply, not this app's) - exit that session (Ctrl+D or /exit) to come back here.\n"
            )
            with sync_folder_lock(cwd or os.getcwd(), owner=name):
                subprocess.run(build_argv(task), cwd=cwd)
            return True
    tried = ", ".join(n for n, _, _ in _INTERACTIVE_CANDIDATES)
    print(
        f"No coding-capable CLI found on PATH (tried: {tried}). Install and log in to at "
        f"least one: Claude Code (`claude`), Antigravity CLI (`agy`), or GitHub Copilot CLI (`copilot`)."
    )
    return False


async def run_hybrid(task: str, folder: str, local_agent=None) -> str:
    """The actual token-saving path: try the free local tool-use loop
    (local_coder.run) first, and only spend cloud/API tokens escalating
    to a real coding CLI if it can't finish confidently - the same
    local-first, escalate-on-failure principle plan_execute.py already
    uses for read-only swarm steps, applied here to real file-editing.

    local_agent is a fleet.local OllamaAgent (or None if no host is
    registered, which just skips straight to cloud). Whatever the local
    agent actually got done stays on disk either way - the cloud escalation
    is told what was attempted and picks up from there, not from scratch.

    Holds `folder`'s lock (see folder_lock.py) for the whole call, local
    attempt through cloud escalation - another agent can't sneak in and
    edit the same files between the two.
    """
    async with folder_lock(folder, owner="hybrid"):
        if local_agent is not None:
            print("Trying the local agent first (free, no cloud/API tokens spent)...")
            try:
                result, completed = await local_coder.run(task, folder, local_agent)
            except Exception as e:
                completed, result = False, str(e)
            if completed:
                print(f"Local agent finished it: {result}")
                return result
            print(f"Local agent didn't finish confidently ({result[:300]!r}) - escalating to a cloud coding CLI...")
        else:
            result = None
            print("No local Ollama host registered - going straight to a cloud coding CLI...")

        old_cwd = os.getcwd()
        os.chdir(folder)
        try:
            name, agent = pick_agent()
            print(f"Using {name} to finish this in {folder}...")
            escalation_prompt = task
            if result:
                escalation_prompt = (
                    f"{task}\n\n(A local model already attempted this first and got partway - its own "
                    f"account of progress/what's left: {result})"
                )
            return await agent.complete(with_project_context(escalation_prompt))
        finally:
            os.chdir(old_cwd)


SPLIT_SYSTEM = (
    "You are splitting a coding task between two specialists: FRONTEND "
    "(UI, layout, styling, client-side behavior) and BACKEND (server "
    "logic, data, APIs, business rules). Return ONLY JSON: "
    '{"frontend": "<their part, or empty string if none>", '
    '"backend": "<their part, or empty string if none>"}. '
    "Leave one side empty if the task is entirely the other's domain - "
    "don't invent work for a specialist who has nothing real to do. "
    "No prose outside the JSON."
)

EXCHANGE_SYSTEM = (
    "You are one of two specialists (FRONTEND or BACKEND) about to work on "
    "part of a shared task, in the same project folder as the other. "
    "You've been shown your assigned piece and the other specialist's "
    "piece. This is your ONE chance to object or reassign scope before "
    "work starts - there is no back-and-forth after this. Reply with "
    "EITHER 'no objection' (do your assigned piece as-is) OR a short "
    "counter-proposal if you genuinely believe you should own something "
    "currently assigned to the other specialist (say what, and why, in "
    "one or two sentences). Do not object just to have something to say - "
    "silence (no objection) is the expected, normal answer most of the time."
)


def _parse_json_object(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Expected a JSON object, got:\n{raw[:500]}")
    return json.loads(match.group(0))


async def run_specialized(task: str, folder: str) -> str:
    """Two specialists, not one generalist: Codex leans frontend/UI,
    Claude Code leans backend/logic - a task gets split between them by
    domain, each gets exactly ONE bounded round to object or claim part
    of the other's scope (not an open-ended negotiation - that's how
    tokens quietly disappear), and then both actually execute their
    (possibly reassigned) piece in the same folder, sequentially so they
    don't race each other's file writes.

    Falls back to a single specialist doing the whole thing if the split
    call decides the task is entirely one domain - no manufactured
    collaboration on a task that doesn't need it.

    Holds `folder`'s lock (see folder_lock.py) for the whole call - split
    decision through both specialists' actual execution - so nothing else
    can start writing into the same folder mid-negotiation or between the
    backend and frontend passes.
    """
    codex = CodexCliAgent(mode="code", timeout=600.0)
    claude = ClaudeCodeAgent(mode="code", timeout=600.0)

    async with folder_lock(folder, owner="specialized"):
        split_raw = await claude.complete(with_project_context(task), system=SPLIT_SYSTEM)
        split = _parse_json_object(split_raw)
        frontend_task = (split.get("frontend") or "").strip()
        backend_task = (split.get("backend") or "").strip()

        old_cwd = os.getcwd()
        if frontend_task and not backend_task:
            print("Whole task is frontend-shaped - Codex handles it alone.")
            os.chdir(folder)
            try:
                return await codex.complete(with_project_context(task))
            finally:
                os.chdir(old_cwd)
        if backend_task and not frontend_task:
            print("Whole task is backend-shaped - Claude Code handles it alone.")
            os.chdir(folder)
            try:
                return await claude.complete(with_project_context(task))
            finally:
                os.chdir(old_cwd)
        if not frontend_task and not backend_task:
            print("Split call returned nothing usable - Claude Code handles the whole task.")
            os.chdir(folder)
            try:
                return await claude.complete(with_project_context(task))
            finally:
                os.chdir(old_cwd)

        print(f"Split - Codex (frontend): {frontend_task}\nClaude Code (backend): {backend_task}")
        print("One bounded exchange round before work starts...")

        exchange_prompt_for = lambda mine, theirs, my_role, their_role: (  # noqa: E731
            f"Full task: {task}\n\nYour assigned piece ({my_role}): {mine}\n\n"
            f"Other specialist's piece ({their_role}): {theirs}"
        )
        codex_reply, claude_reply = await asyncio.gather(
            codex.complete(
                exchange_prompt_for(frontend_task, backend_task, "FRONTEND", "BACKEND"), system=EXCHANGE_SYSTEM
            ),
            claude.complete(
                exchange_prompt_for(backend_task, frontend_task, "BACKEND", "FRONTEND"), system=EXCHANGE_SYSTEM
            ),
        )
        print(f"Codex: {codex_reply}\nClaude Code: {claude_reply}")

        final_frontend = frontend_task
        final_backend = backend_task
        if "no objection" not in codex_reply.lower():
            final_frontend = f"{frontend_task}\n\n(Your own counter-proposal from the exchange round: {codex_reply})"
        if "no objection" not in claude_reply.lower():
            final_backend = f"{backend_task}\n\n(Your own counter-proposal from the exchange round: {claude_reply})"

        os.chdir(folder)
        try:
            print("Claude Code working on backend piece...")
            backend_result = await claude.complete(with_project_context(
                f"Full task: {task}\n\nYour piece (backend): {final_backend}\n\n"
                f"(Frontend is being handled separately by another agent - don't redo it.)"
            ))
            print("Codex working on frontend piece...")
            frontend_result = await codex.complete(with_project_context(
                f"Full task: {task}\n\nYour piece (frontend): {final_frontend}\n\n"
                f"Backend was just completed by another agent - here's what they did: {backend_result}"
            ))
        finally:
            os.chdir(old_cwd)

    return f"=== Backend (Claude Code) ===\n{backend_result}\n\n=== Frontend (Codex) ===\n{frontend_result}"
