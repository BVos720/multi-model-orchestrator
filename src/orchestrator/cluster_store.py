from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

CLUSTER_FILE = Path(".orchestrator") / "cluster.json"


@dataclass
class ClusterConfig:
    """A llama.cpp RPC cluster's master node - `llama-server` running on one
    of your machines, splitting one big model's layers across it and
    whatever `rpc-server` workers it's pointed at (e.g. your other PC).
    This is NOT Ollama - it's raw llama.cpp, a separate build/run per
    README "Running a big model across two PCs". base_url points at
    llama-server's own OpenAI-compatible endpoint (usually :8080/v1)."""

    name: str
    base_url: str
    model: str  # whatever name llama-server reports for the loaded gguf


class ClusterStore:
    def __init__(self, path: Path = CLUSTER_FILE):
        self.path = path

    def load(self) -> list[ClusterConfig]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        return [ClusterConfig(**c) for c in raw]

    def _save(self, clusters: list[ClusterConfig]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(c) for c in clusters], indent=2), encoding="utf-8")

    def add(self, cluster: ClusterConfig) -> None:
        clusters = [c for c in self.load() if c.name != cluster.name]
        clusters.append(cluster)
        self._save(clusters)

    def remove(self, name: str) -> bool:
        clusters = self.load()
        kept = [c for c in clusters if c.name != name]
        self._save(kept)
        return len(kept) != len(clusters)
