from __future__ import annotations

import json
import time
from pathlib import Path

DISPATCH_LOG_FILE = Path(".orchestrator") / "dispatch_log.jsonl"

# How much of a prompt/response to keep in the persisted record. Full
# content can be huge (a step prompt embeds the whole work log so far) -
# capped so the log file stays readable and doesn't balloon; the console
# line printed live is capped even shorter, for the same reason.
_PERSIST_PREVIEW_CHARS = 4000
_PRINT_PREVIEW_CHARS = 200


def record(host_name: str, host_url: str, model: str, prompt_preview: str) -> None:
    """Append one line documenting exactly what was sent to which local
    host - the supervisor-side half of "what did we send, and who
    received it." JSONL (one JSON object per line) so it's easy to tail
    or grep without loading the whole file, and never silently
    overwrites history the way a single JSON blob would.

    Also prints a short one-line summary live, so it's visible in the
    console during a run, not just on later inspection of the file.
    """
    entry = {
        "ts": time.time(),
        "host": host_name,
        "url": host_url,
        "model": model,
        "prompt_preview": prompt_preview[:_PERSIST_PREVIEW_CHARS],
    }
    DISPATCH_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DISPATCH_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    one_line = prompt_preview.replace("\n", " ")[:_PRINT_PREVIEW_CHARS]
    print(f"→ Sent to {host_name} ({host_url}, model={model}): {one_line!r}")


def render_recent(limit: int = 20) -> str:
    """Human-readable view of the last `limit` dispatches - what `orchest
    dispatch-log` prints."""
    if not DISPATCH_LOG_FILE.exists():
        return "No dispatches logged yet."
    lines = DISPATCH_LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        return "No dispatches logged yet."
    out = []
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.get("ts", 0)))
        preview = entry.get("prompt_preview", "").replace("\n", " ")[:_PRINT_PREVIEW_CHARS]
        out.append(f"[{when}] {entry.get('host')} ({entry.get('url')}, {entry.get('model')}): {preview!r}")
    return "\n".join(out)


def clear() -> None:
    if DISPATCH_LOG_FILE.exists():
        DISPATCH_LOG_FILE.unlink()
