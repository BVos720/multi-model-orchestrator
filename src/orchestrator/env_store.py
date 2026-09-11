from __future__ import annotations

from pathlib import Path

ENV_FILE = Path(".env")
ENV_EXAMPLE = Path(".env.example")


def set_env_var(key: str, value: str, path: Path = ENV_FILE) -> None:
    """Set/replace KEY=value in .env, preserving every other line.

    Creates .env (seeded from .env.example if present) the first time this
    is called, so `orchest settings add ...` works even before setup.
    """
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif ENV_EXAMPLE.exists():
        lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}=") and not line.strip().startswith("#"):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
