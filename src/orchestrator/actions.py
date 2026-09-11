from __future__ import annotations

from dataclasses import dataclass

from .env_store import set_env_var
from .settings_store import PRESETS, ProviderAccount, ProviderConfig, SettingsStore

# Shared between cli.py (click commands, for scripting) and menu.py
# (interactive arrow-key menu) so the two interfaces can't drift apart on
# what a valid agent config actually is - only *how you get prompted* differs.


def _account_env_var(name: str, label: str) -> str:
    base = name.upper().replace("-", "_")
    if label in ("", "default"):
        return f"{base}_API_KEY"
    return f"{base}_{label.upper().replace('-', '_')}_API_KEY"


@dataclass
class ResolvedAgent:
    name: str
    base_url: str
    model: str
    tier: str
    free: bool


def resolve_agent(
    name: str,
    preset: str | None,
    base_url: str | None,
    model: str | None,
    tier: str | None,
) -> ResolvedAgent:
    """Work out base_url/model/tier/free for an agent - no disk access, no
    secret involved. Callers prompt for the key themselves (click hidden
    prompt, questionary password, ...) and pass it to save_first_account()
    or add_account() separately, so the key only ever exists where typed."""
    preset_cfg = PRESETS.get(preset or name, {})
    resolved_base_url = base_url or preset_cfg.get("base_url")
    resolved_model = model or preset_cfg.get("model")
    resolved_tier = tier or preset_cfg.get("tier", "cloud")
    if not resolved_base_url or not resolved_model:
        raise ValueError(
            f"Unknown preset for '{name}'. Provide a base URL + model explicitly, "
            f"or use a known preset: {', '.join(PRESETS)}"
        )
    return ResolvedAgent(
        name=name,
        base_url=resolved_base_url,
        model=resolved_model,
        tier=resolved_tier,
        free=bool(preset_cfg.get("free")),
    )


def save_first_account(resolved: ResolvedAgent, label: str, api_key: str, weight: int = 1) -> str:
    """Create the provider entry with its first account. Returns the env var name used."""
    env_var = _account_env_var(resolved.name, label)
    set_env_var(env_var, api_key)
    SettingsStore().add_provider(
        ProviderConfig(
            name=resolved.name,
            base_url=resolved.base_url,
            model=resolved.model,
            tier=resolved.tier,
            accounts=[ProviderAccount(label=label, api_key_env=env_var, weight=weight)],
        )
    )
    return env_var


def add_account(name: str, label: str, api_key: str, weight: int = 1) -> str:
    """Add another account (credential) to an already-registered provider.
    Raises ValueError if `name` isn't registered yet."""
    env_var = _account_env_var(name, label)
    if not SettingsStore().add_account(name, ProviderAccount(label=label, api_key_env=env_var, weight=weight)):
        raise ValueError(f"'{name}' isn't registered yet - add its first account before adding another.")
    set_env_var(env_var, api_key)
    return env_var


def save_gemini_key(api_key: str) -> None:
    set_env_var("GEMINI_API_KEY", api_key)
