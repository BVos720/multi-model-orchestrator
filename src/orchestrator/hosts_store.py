from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

HOSTS_FILE = Path(".orchestrator") / "hosts.json"

# A curated shortlist for the "pick a model" picker - not the full Ollama
# library (see https://ollama.com/library for that), just well-known,
# coding-friendly tags worth offering before falling back to "type your
# own." Anything picked/typed here that isn't already pulled on the target
# host gets fetched on demand via network.pull_model - no need to `ollama
# pull` by hand first.
POPULAR_MODELS = [
    "qwen2.5-coder:1.5b",
    "qwen2.5-coder:7b",
    "qwen2.5-coder:14b",
    "qwen2.5-coder:32b",
    "deepseek-coder-v2:16b",
    "codellama:13b",
    "gemma4:12b",
    "gemma3:12b",
    "gemma3:27b",
    "llama3.1:8b",
    "mistral:7b",
    "phi4:14b",
]


@dataclass
class HostConfig:
    """One (machine, model) pair - one Ollama instance running one model
    that fits its VRAM. Register the SAME machine twice with two different
    models/names (e.g. "laptop-small" + "laptop-big", same url) to let it
    serve both a fast small model and a stronger big one - Ollama loads
    whichever model a request asks for.

    size ("small"|"standard"|"big") is just a routing hint: short/simple
    local-eligible steps prefer a "small" host if one's registered, longer
    ones prefer "big" - falls back to whatever's actually healthy/free
    either way, so this is an optimization, not a hard requirement."""

    name: str
    url: str
    model: str
    size: str = "standard"


class HostsStore:
    def __init__(self, path: Path = HOSTS_FILE):
        self.path = path

    def load(self) -> list[HostConfig]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        return [HostConfig(**h) for h in raw]

    def _save(self, hosts: list[HostConfig]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(h) for h in hosts], indent=2), encoding="utf-8")

    def add(self, host: HostConfig) -> None:
        hosts = [h for h in self.load() if h.name != host.name]
        hosts.append(host)
        self._save(hosts)

    def remove(self, name: str) -> bool:
        hosts = self.load()
        kept = [h for h in hosts if h.name != name]
        self._save(kept)
        return len(kept) != len(hosts)
