from __future__ import annotations

import json
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
    return "\n".join(lines)
