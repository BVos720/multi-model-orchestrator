from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

CONTEXT_DIR = Path(".orchestrator")
CONTEXT_FILE = CONTEXT_DIR / "context.json"

# Rough budget in characters (~4 chars/token). Compact once the raw log
# passes this so prompts to every agent stay bounded as a run grows.
COMPACT_CHAR_THRESHOLD = 24_000
KEEP_RECENT_ENTRIES = 6  # always keep the last N entries uncompressed


@dataclass
class Entry:
    ts: float
    agent: str
    role: str  # "plan" | "step-N:<tier>" | "review" | "summary" | "debate-answer" | ...
    content: str


class ContextStore:
    """The shared blackboard every agent reads from and writes to.

    Persisted to .orchestrator/context.json so a run survives a crash/resume,
    and periodically compacted: everything but the most recent entries gets
    collapsed into one summary entry (written by a cheap/local model) once
    the log grows past COMPACT_CHAR_THRESHOLD.
    """

    def __init__(self, path: Path = CONTEXT_FILE):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entries: list[Entry] = self._load()

    def _load(self) -> list[Entry]:
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
            return [Entry(**e) for e in raw]
        return []

    def _save(self) -> None:
        self.path.write_text(
            json.dumps([asdict(e) for e in self.entries], indent=2),
            encoding="utf-8",
        )

    def add(self, agent: str, role: str, content: str) -> None:
        self.entries.append(Entry(ts=time.time(), agent=agent, role=role, content=content))
        self._save()

    def transcript(self) -> str:
        return "\n\n".join(f"[{e.agent}/{e.role}] {e.content}" for e in self.entries)

    def size_chars(self) -> int:
        return sum(len(e.content) for e in self.entries)

    def needs_compaction(self) -> bool:
        return self.size_chars() > COMPACT_CHAR_THRESHOLD and len(self.entries) > KEEP_RECENT_ENTRIES

    async def compact(self, summarizer) -> None:
        """Collapse everything but the last KEEP_RECENT_ENTRIES into one summary.

        `summarizer` is an Agent - normally the local Ollama model, since
        summarizing is cheap/low-stakes and this keeps it off the token bill.
        """
        if not self.needs_compaction():
            return
        old, recent = self.entries[:-KEEP_RECENT_ENTRIES], self.entries[-KEEP_RECENT_ENTRIES:]
        old_text = "\n\n".join(f"[{e.agent}/{e.role}] {e.content}" for e in old)
        summary = await summarizer.complete(
            prompt=old_text,
            system=(
                "Summarize this multi-agent work log into a compact brief: "
                "decisions made, current plan state, and any facts later steps "
                "will need. Be terse. Keep code snippets only if still relevant."
            ),
        )
        self.entries = [
            Entry(ts=time.time(), agent=summarizer.name, role="summary", content=summary)
        ] + recent
        self._save()

    def reset(self) -> None:
        self.entries = []
        self._save()
