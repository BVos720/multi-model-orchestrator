from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

HOSTS_FILE = Path(".orchestrator") / "hosts.json"


@dataclass
class HostConfig:
    """One Ollama instance - one machine's GPU - with the model that
    actually fits it. Different machines can run different models: a 6GB
    laptop GPU and an 8GB desktop 3070 don't have the same ceiling."""

    name: str
    url: str
    model: str


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
