from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROVIDERS_FILE = Path(".orchestrator") / "providers.json"

# Known OpenAI-compatible providers - `orchestrator settings add <name>` fills
# base_url/model in from here so you only ever have to supply the API key.
# None of these have a subscription/OAuth shortcut like Claude Code or the
# Gemini CLI: they're billed per-call on their own key. "free": True means a
# genuine standing $0 tier as of writing (no card needed) - not a trial that
# expires. Sources: https://openrouter.ai/blog/tutorials/free-llm-apis-compared/,
# https://inference-docs.cerebras.ai/resources/openai, https://docs.mistral.ai
PRESETS: dict[str, dict] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "tier": "cloud",
        "free": False,  # cheap, not free - your own key, billed per token
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "tier": "cloud",
        "free": False,
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "tier": "cloud",
        "free": True,  # no card; ~30 RPM / 1,000 RPD; ~320 tok/s on LPU hardware
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        # the ":free" suffix matters - without it this is a billed model.
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "tier": "cloud",
        "free": True,  # no card; ~20 RPM / 50 RPD across ~20+ ":free" models
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "model": "llama-3.3-70b",
        "tier": "cloud",
        "free": True,  # no card; ~30 RPM / ~1M tokens per day, very fast
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "model": "codestral-latest",  # Mistral's coding model - good fit here
        "tier": "cloud",
        "free": True,  # "Experiment" tier, no card, ~1B tokens/month, rate-limited
    },
}


@dataclass
class ProviderAccount:
    """One credential for a provider. Multiple accounts on the same free
    tier (e.g. two Groq signups) let the pool round-robin/failover between
    them - each one's own rate limit, combined."""

    label: str
    api_key_env: str  # name of the .env variable holding this account's key


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    model: str
    tier: str = "cloud"
    accounts: list[ProviderAccount] = field(default_factory=list)


class SettingsStore:
    """Custom OpenAI-compatible agents added via `orchestrator settings`.

    Only non-secret metadata lives here (name/base_url/model/tier/which env
    vars hold each account's secret). The actual API keys always go to .env
    instead, via env_store.set_env_var - this file is safe to read/print/
    commit-ignore without worrying about leaking a key.
    """

    def __init__(self, path: Path = PROVIDERS_FILE):
        self.path = path

    def load(self) -> list[ProviderConfig]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        providers = []
        for p in raw:
            if "accounts" not in p and "api_key_env" in p:
                # old single-key shape - migrate transparently on read
                p = {**p, "accounts": [{"label": "default", "api_key_env": p.pop("api_key_env")}]}
            providers.append(
                ProviderConfig(
                    name=p["name"],
                    base_url=p["base_url"],
                    model=p["model"],
                    tier=p.get("tier", "cloud"),
                    accounts=[ProviderAccount(**a) for a in p.get("accounts", [])],
                )
            )
        return providers

    def _save(self, providers: list[ProviderConfig]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(p) for p in providers], indent=2), encoding="utf-8")

    def get(self, name: str) -> ProviderConfig | None:
        for p in self.load():
            if p.name == name:
                return p
        return None

    def add_provider(self, config: ProviderConfig) -> None:
        """Create (or fully replace) a provider entry - used for the first account."""
        providers = [p for p in self.load() if p.name != config.name]
        providers.append(config)
        self._save(providers)

    def add_account(self, name: str, account: ProviderAccount) -> bool:
        """Append another account to an already-registered provider.
        Returns False if `name` isn't registered yet (create it first)."""
        providers = self.load()
        for p in providers:
            if p.name == name:
                p.accounts = [a for a in p.accounts if a.label != account.label] + [account]
                self._save(providers)
                return True
        return False

    def remove_account(self, name: str, label: str) -> bool:
        """Remove one account. Drops the whole provider if that was its last account."""
        providers = self.load()
        for p in providers:
            if p.name == name:
                before = len(p.accounts)
                p.accounts = [a for a in p.accounts if a.label != label]
                if not p.accounts:
                    providers = [x for x in providers if x.name != name]
                self._save(providers)
                return len(p.accounts) != before or name not in [x.name for x in providers]
        return False

    def remove(self, name: str) -> bool:
        """Remove a provider entirely, all its accounts included."""
        providers = self.load()
        kept = [p for p in providers if p.name != name]
        self._save(kept)
        return len(kept) != len(providers)
