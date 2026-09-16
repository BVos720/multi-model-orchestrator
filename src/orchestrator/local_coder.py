from __future__ import annotations

import json
from pathlib import Path

from .providers.ollama import OllamaAgent

# OpenAI-function-calling-shaped tool schemas - what we send Ollama's
# /api/chat "tools" field. Deliberately small: read/write/list a file plus
# an explicit "done" signal, not a general-purpose Bash tool - the whole
# point of routing here first is free, low-risk boilerplate work, not
# giving an unsupervised local model shell access.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file's contents, relative to the working folder.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file, relative to the working folder. "
            "Parent folders are created automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files/folders at a path relative to the working folder (default: '.').",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Call this once the task is fully, actually complete - not before.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]

SYSTEM = (
    "You are a local coding agent working inside ONE folder. You have tools: "
    "read_file, write_file, list_dir, and done. Use them to actually create/edit "
    "files - do not just describe what you would do, call the tools. All paths "
    "are relative to the working folder; you cannot and must not try to reach "
    "outside it (any attempt is blocked and returned to you as an error, not "
    "silently allowed). Work step by step: list_dir/read_file to see what's "
    "there before writing, write_file to make real changes, and call done with "
    "a short summary only once the task is genuinely finished. If you're not "
    "confident you can complete this correctly, call done anyway and say so "
    "plainly in the summary - a stronger model picks it up from there."
)

# Some local models (confirmed: qwen2.5-coder:7b AND :32b on Ollama 0.34)
# don't populate the native message.tool_calls field even when explicitly
# given a `tools` schema - they just write the same
# {"name":...,"arguments":...} shape into plain `content` instead, and
# will sometimes emit SEVERAL of these back to back in one turn (e.g.
# list_dir, then write_file, then done) rather than a single JSON array -
# not valid JSON as one blob, so a single json.loads() or a greedy regex
# both fail on it. json.JSONDecoder.raw_decode() parses one JSON value at
# a time from wherever it's pointed, so scanning for successive '{' and
# decoding from each handles one object, several concatenated objects, or
# a real JSON array uniformly.
_decoder = json.JSONDecoder()


def _parse_content_as_calls(content: str) -> list[dict]:
    calls: list[dict] = []
    idx = 0
    length = len(content or "")
    while idx < length:
        brace = content.find("{", idx)
        if brace == -1:
            break
        try:
            obj, end = _decoder.raw_decode(content, brace)
        except json.JSONDecodeError:
            idx = brace + 1
            continue
        if isinstance(obj, dict) and "name" in obj and "arguments" in obj:
            calls.append(obj)
        elif isinstance(obj, list):
            calls.extend(o for o in obj if isinstance(o, dict) and "name" in o and "arguments" in o)
        idx = end
    return calls


class PathEscapeError(Exception):
    pass


def _resolve_in_folder(folder: Path, rel_path: str) -> Path:
    """Resolve `rel_path` against `folder` and refuse anything that
    escapes it (../.., an absolute path elsewhere, a symlink pointing
    out) - the actual, enforceable version of "lock it to a folder" that
    the interactive CLI hand-off can't guarantee, because here WE
    implement the only file access the model has."""
    candidate = (folder / rel_path).resolve()
    try:
        candidate.relative_to(folder.resolve())
    except ValueError:
        raise PathEscapeError(f"'{rel_path}' resolves outside the working folder - refused.")
    return candidate


def _run_tool(folder: Path, name: str, args: dict) -> str:
    try:
        if name == "read_file":
            path = _resolve_in_folder(folder, args["path"])
            if not path.is_file():
                return f"error: {args['path']} does not exist"
            return path.read_text(encoding="utf-8", errors="replace")[:20_000]
        if name == "write_file":
            path = _resolve_in_folder(folder, args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"], encoding="utf-8")
            return f"wrote {args['path']} ({len(args['content'])} chars)"
        if name == "list_dir":
            path = _resolve_in_folder(folder, args.get("path", "."))
            if not path.is_dir():
                return f"error: {args.get('path', '.')} is not a directory"
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
            return "\n".join(entries) or "(empty)"
        return f"error: unknown tool '{name}'"
    except PathEscapeError as e:
        return f"error: {e}"
    except OSError as e:
        return f"error: {e}"


async def run(
    task: str,
    folder: str,
    ollama: OllamaAgent,
    model: str | None = None,
    prefer_size: str | None = "big",
    max_turns: int = 12,
) -> tuple[str, bool]:
    """Free-first coding attempt: a local model gets real (but strictly
    folder-confined) file tool-use, in a short agentic loop, instead of
    the plain text-completion `OllamaAgent.complete()` everywhere else in
    this app uses. Returns (result_text, completed) - completed=True only
    if the model explicitly called `done`; the caller (the hybrid coding
    mode) should treat completed=False as "escalate to a real cloud
    coding CLI" rather than trust an ambiguous stop.

    prefer_size defaults to "big" (see HostConfig.size) - actually writing
    files is worth spending more local compute on than a throwaway swarm
    text step: a confirmed real failure (a 7B model mis-escaping a
    string, producing a file that doesn't even parse, then confidently
    calling done anyway) is exactly the class of mistake a bigger local
    model is less likely to make. Only takes effect if you've actually
    registered a "big"-sized host (falls back to whatever's healthy/free
    otherwise, same as everywhere else prefer_size is used).

    model, if given, overrides host/size-based routing entirely - pass a
    specific model here if you want exactly that one regardless of size
    hints.
    """
    folder_path = Path(folder).resolve()
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": task},
    ]

    for _ in range(max_turns):
        raw = await ollama.raw_chat(messages, tools=TOOLS, model=model, prefer_size=prefer_size)
        message = raw.get("message", {})
        tool_calls = message.get("tool_calls") or []
        content = message.get("content", "")

        if not tool_calls:
            parsed_calls = _parse_content_as_calls(content)
            if parsed_calls:
                tool_calls = [
                    {"function": {"name": c["name"], "arguments": c["arguments"]}} for c in parsed_calls
                ]

        if not tool_calls:
            # No tool call at all, native or parsed - treat whatever text
            # it gave as a final (unconfirmed) answer rather than loop
            # forever guessing what it meant.
            return content.strip() or "(local model returned nothing)", False

        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if name == "done":
                return str(args.get("summary", "done")), True
            result = _run_tool(folder_path, name, args)
            messages.append({"role": "tool", "content": result})

    return f"Local agent hit its {max_turns}-turn limit without calling done.", False
