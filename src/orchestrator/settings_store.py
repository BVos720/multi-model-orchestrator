from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

PROVIDERS_FILE = Path(".orchestrator") / "providers.json"

# Known OpenAI-compatible providers - `orchestrator settings add <name>` fills
# base_url/model in from here so you only ever have to supply the API key.
# None of these have a subscription/OAuth shortcut: they're billed per-call
# on their own key, separate from any subscription you have elsewhere.
PRESETS: dict[str, dict] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "tier": "cloud",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "tier": "cloud",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "tier": "cloud",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.3-70b-instruct",
        "tier": "cloud",
    },
}


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    model: str
    tier: str = "cloud"
    api_key_env: str = ""  # name of the .env variable holding the secret


class SettingsStore:
    """Custom OpenAI-compatible agents added via `orchestrator settings`.

    Only non-secret metadata lives here (name/base_url/model/tier/which env
    var to read). The actual API key always goes to .env instead, via
    env_store.set_env_var - this file is safe to read/print/commit-ignore
    without worrying about leaking a key.
    """

    def __init__(self, path: Path = PROVIDERS_FILE):
        self.path = path

    def load(self) -> list[ProviderConfig]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        return [ProviderConfig(**p) for p in raw]

    def _save(self, providers: list[ProviderConfig]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(p) for p in providers], indent=2), encoding="utf-8")

    def add(self, config: ProviderConfig) -> None:
        providers = [p for p in self.load() if p.name != config.name]
        providers.append(config)
        self._save(providers)

    def remove(self, name: str) -> bool:
        providers = self.load()
        kept = [p for p in providers if p.name != name]
        self._save(kept)
        return len(kept) != len(providers)
