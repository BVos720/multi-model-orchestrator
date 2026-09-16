from __future__ import annotations

from pathlib import Path

PROJECT_CONTEXT_FILE = Path(".orchestrator") / "project_context.md"


def project_context_path() -> Path:
    return PROJECT_CONTEXT_FILE


def load_project_context() -> str:
    """Free-form, user-authored project notes every agent sees on every
    step, of every mode, in every run - unlike ContextStore (one run's own
    transcript, cleared by `orchest reset`), this is persistent and isn't
    run-specific. Put things here once instead of re-pasting them into
    every task: conventions, architecture decisions, constraints, whatever
    background a local model wouldn't otherwise have.

    Returns "" if nothing's been set yet - callers should skip injecting
    it entirely rather than send blank boilerplate on every call."""
    if not PROJECT_CONTEXT_FILE.exists():
        return ""
    return PROJECT_CONTEXT_FILE.read_text(encoding="utf-8").strip()


def save_project_context(text: str) -> None:
    PROJECT_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROJECT_CONTEXT_FILE.write_text(text, encoding="utf-8")


def clear_project_context() -> bool:
    if PROJECT_CONTEXT_FILE.exists():
        PROJECT_CONTEXT_FILE.unlink()
        return True
    return False


def with_project_context(prompt: str) -> str:
    """Prepend the persistent project-context file to `prompt`, if one's
    been set - this is the one place every mode plugs in to make "every
    model sees the same background info" actually happen. A no-op
    (returns `prompt` unchanged) when nothing's there yet."""
    ctx = load_project_context()
    if not ctx:
        return prompt
    return f"Project context (persistent notes for this project - see `orchest context`):\n{ctx}\n\n{prompt}"
