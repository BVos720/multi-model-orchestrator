from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

TASKS_FILE = Path(".orchestrator") / "tasks.json"

STATUS_MARK = {
    "pending": " ",
    "running": "~",
    "done": "x",
    "escalated": "x",
    "failed": "!",
}


@dataclass
class TaskItem:
    id: int
    description: str
    complexity: str | None = None  # "low" | "high" | None - the planner's own tag
    tier: str | None = None        # "local" | "cloud" once routed
    status: str = "pending"        # pending | running | done | escalated | failed
    assigned_agent: str | None = None
    result_preview: str = ""       # first ~200 chars of the result, for a quick glance
    updated_ts: float = field(default_factory=time.time)


@dataclass
class TaskRun:
    """The plan, made visible: this *is* the "models keep a task list and
    divide it among themselves" list - one row per plan step, updated live
    as each gets routed, executed, and (maybe) escalated."""

    task: str
    items: list[TaskItem] = field(default_factory=list)
    started_ts: float = field(default_factory=time.time)
    finished: bool = False


class TaskListStore:
    def __init__(self, path: Path = TASKS_FILE):
        self.path = path

    def save(self, run: TaskRun) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(run), indent=2), encoding="utf-8")

    def load(self) -> TaskRun | None:
        if not self.path.exists():
            return None
        raw = json.loads(self.path.read_text(encoding="utf-8") or "null")
        if raw is None:
            return None
        return TaskRun(
            task=raw["task"],
            items=[TaskItem(**i) for i in raw.get("items", [])],
            started_ts=raw.get("started_ts", 0),
            finished=raw.get("finished", False),
        )


def render(run: TaskRun) -> str:
    lines = [f"Task: {run.task}", ""]
    for item in run.items:
        mark = STATUS_MARK.get(item.status, " ")
        tag = item.assigned_agent or (item.tier or "queued")
        if item.status == "escalated":
            tag += " -> escalated"
        elif item.status == "running":
            tag += "..."
        elif item.status == "failed":
            tag += " - FAILED"
        lines.append(f"  [{mark}] {item.id}. {item.description}  ({tag})")
        lines.append("")
    return "\n".join(lines).rstrip("\n")


class LiveTaskPrinter:
    """Keeps one checklist on screen, redrawn in place, instead of the
    naive `print(render(run))` on every status change - a plan with 10
    steps changes status ~3x each (routed, running, done/escalated), so
    that would scroll ~30 near-duplicate full-list dumps past by the end
    of the run. Redrawing over the previous one in place is what actually
    makes progress overseeable at a glance.

    Falls back to plain sequential prints when stdout isn't a real
    terminal (piped/redirected output, a log file, a non-interactive
    CI run) - the ANSI cursor codes below would otherwise show up as
    garbage there instead of doing anything useful."""

    def __init__(self, enabled: bool = True):
        self._enabled = enabled and sys.stdout.isatty()
        self._lines_printed = 0

    def show(self, run: TaskRun) -> None:
        text = render(run)
        if not self._enabled:
            print(text)
            return
        if self._lines_printed:
            # Move the cursor up over everything printed last time, then
            # clear from there to the end of the screen, before redrawing.
            print(f"\x1b[{self._lines_printed}A\x1b[J", end="")
        print(text)
        self._lines_printed = text.count("\n") + 1
