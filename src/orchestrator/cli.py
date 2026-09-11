from __future__ import annotations

import asyncio
import os
import shutil
import sys

import click

from .config import build_fleet
from .context_store import ContextStore
from .env_store import set_env_var
from .hosts_store import HostConfig, HostsStore
from .modes import debate, plan_execute
from .settings_store import PRESETS, ProviderConfig, SettingsStore


@click.group()
def main():
    """Multi-model orchestrator: Claude Code + Gemini + Ollama (+ others), working together."""


def _pick_local_hosts(explicit: str | None) -> list[str] | None:
    """Resolve which registered Ollama host(s) this run should use.

    None means "no preference" (build_fleet falls back to every registered
    host, or the legacy single-pool env config if none are registered)."""
    registered = HostsStore().load()
    if not registered:
        return None
    if explicit:
        if explicit == "all":
            return [h.name for h in registered]
        return [n.strip() for n in explicit.split(",")]
    if len(registered) == 1:
        return [registered[0].name]
    if not sys.stdin.isatty():
        return [h.name for h in registered]  # non-interactive: pool everything

    click.echo("Multiple local machines registered - which should handle local steps this run?")
    for i, h in enumerate(registered, 1):
        click.echo(f"  {i}. {h.name} - {h.model} @ {h.url}")
    all_idx = len(registered) + 1
    click.echo(f"  {all_idx}. all (pool everything)")
    choice = click.prompt("Choice", default=str(all_idx))
    try:
        idx = int(choice)
        if idx == all_idx:
            return [h.name for h in registered]
        return [registered[idx - 1].name]
    except (ValueError, IndexError):
        return [n.strip() for n in choice.split(",")]


@main.command()
@click.argument("task")
@click.option("--max-steps", default=12, show_default=True, help="Cap on plan steps to execute.")
@click.option("--local", "local_choice", default=None, help="Registered host name, comma-list, or 'all'. Prompts if omitted and >1 host is registered.")
def run(task: str, max_steps: int, local_choice: str | None):
    """Plan with a heavy model, execute steps (local model first, escalate on complexity)."""
    fleet = build_fleet(local_hosts=_pick_local_hosts(local_choice))
    store = ContextStore()
    result = asyncio.run(plan_execute.run(task, fleet, store, max_steps=max_steps))
    click.echo("\n=== FINAL RESULT ===\n")
    click.echo(result)


@main.command()
@click.argument("question")
def ask(question: str):
    """Ask every configured cloud model the same question, merge into one verdict."""
    fleet = build_fleet()
    store = ContextStore()
    result = asyncio.run(debate.run(question, fleet, store))
    click.echo("\n=== VERDICT ===\n")
    click.echo(result)


@main.command()
def status():
    """Show which agents are configured and ready."""
    fleet = build_fleet()
    click.echo(f"Planners: {[a.name for a in fleet.planners]}")
    click.echo(f"Cloud:    {[a.name for a in fleet.cloud]}")
    click.echo(f"Local:    {fleet.local.name + ' ' + str([h.name for h in fleet.local.hosts]) if fleet.local else 'none'}")


@main.command()
def reset():
    """Clear the shared context/blackboard file (.orchestrator/context.json)."""
    store = ContextStore()
    store.reset()
    click.echo("Context cleared.")


@main.group()
def settings():
    """Manage configured agents, API keys, and local (Ollama) machines."""


@settings.command("list")
def settings_list():
    """Show every agent - built-in and custom - and whether it's ready."""
    click.echo("Built-in agents:")
    click.echo(f"  {'claude-code':12} " + ("ok (CLI login)" if shutil.which("claude") else "claude CLI not found"))
    if shutil.which("gemini"):
        click.echo(f"  {'gemini':12} ok (Google account login via gemini CLI)")
    elif os.environ.get("GEMINI_API_KEY"):
        click.echo(f"  {'gemini':12} ok (GEMINI_API_KEY)")
    else:
        click.echo(f"  {'gemini':12} not configured - see `orchestrator settings add gemini`")

    hosts = HostsStore().load()
    if hosts:
        click.echo(f"  {'ollama':12} {len(hosts)} registered host(s) - see `orchestrator settings hosts`")
    else:
        legacy = os.environ.get("OLLAMA_HOSTS") or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        click.echo(f"  {'ollama':12} no named hosts registered, using env default: {legacy}")

    click.echo("\nCustom (OpenAI-compatible) agents:")
    custom = SettingsStore().load()
    if not custom:
        click.echo("  (none - try `orchestrator settings presets` then `settings add <name>`)")
    for p in custom:
        has_key = "ok" if os.environ.get(p.api_key_env) else "MISSING KEY"
        free_tag = "free" if PRESETS.get(p.name, {}).get("free") else "paid"
        click.echo(f"  {p.name:12} model={p.model:32} tier={p.tier:8} [{free_tag}] key={p.api_key_env} [{has_key}]")


@settings.command("presets")
def settings_presets():
    """List known providers you can `settings add`, with free/paid status."""
    click.echo("free  name          model                                    signup\n")
    urls = {
        "groq": "console.groq.com",
        "openrouter": "openrouter.ai/keys",
        "cerebras": "cloud.cerebras.ai",
        "mistral": "console.mistral.ai",
        "deepseek": "platform.deepseek.com",
        "openai": "platform.openai.com",
    }
    for name, cfg in sorted(PRESETS.items(), key=lambda kv: not kv[1].get("free")):
        tag = "YES " if cfg.get("free") else "no  "
        click.echo(f"{tag}  {name:12}  {cfg['model']:38} {urls.get(name, '')}")
    click.echo(
        "\nFree ones need no card and have real (if rate-limited) standing free "
        "tiers as of writing - see README for sources. `orchestrator settings add <name>`."
    )


@settings.command("add")
@click.argument("name")
@click.option(
    "--preset",
    type=click.Choice(list(PRESETS.keys()) + ["custom"]),
    default=None,
    help="Known provider preset. Defaults to matching NAME (e.g. `add deepseek` uses the deepseek preset).",
)
@click.option("--base-url", default=None, help="Required with --preset custom.")
@click.option("--model", default=None, help="Required with --preset custom.")
@click.option("--tier", type=click.Choice(["planner", "cloud", "local"]), default=None)
def settings_add(name: str, preset: str | None, base_url: str | None, model: str | None, tier: str | None):
    """Add/configure an agent, e.g.: orchestrator settings add deepseek

    NAME "gemini" is special-cased to just set GEMINI_API_KEY - prefer
    `npm install -g @google/gemini-cli && gemini` instead, which needs no key.
    Anything else is added as an OpenAI-compatible agent via .env + providers.json.
    """
    if name == "gemini":
        if shutil.which("gemini"):
            click.echo(
                "gemini CLI is already installed - run `gemini` once to log in with your "
                "Google account and you won't need an API key at all. Continuing to set "
                "GEMINI_API_KEY anyway as a fallback."
            )
        key = click.prompt("GEMINI_API_KEY (input hidden)", hide_input=True)
        set_env_var("GEMINI_API_KEY", key)
        click.echo("Saved GEMINI_API_KEY to .env")
        return

    preset_cfg = PRESETS.get(preset or name, {})
    resolved_base_url = base_url or preset_cfg.get("base_url")
    resolved_model = model or preset_cfg.get("model")
    resolved_tier = tier or preset_cfg.get("tier", "cloud")

    if not resolved_base_url or not resolved_model:
        raise click.UsageError(
            f"Unknown preset for '{name}'. Pass --preset custom --base-url ... --model ..., "
            f"or use a known preset: {', '.join(PRESETS)}"
        )

    api_key_env = f"{name.upper().replace('-', '_')}_API_KEY"
    key = click.prompt(f"{api_key_env} (input hidden)", hide_input=True)
    set_env_var(api_key_env, key)

    SettingsStore().add(
        ProviderConfig(
            name=name, base_url=resolved_base_url, model=resolved_model, tier=resolved_tier, api_key_env=api_key_env
        )
    )
    free_note = "free tier" if preset_cfg.get("free") else "paid - billed on your own key"
    click.echo(
        f"Added agent '{name}' (model={resolved_model}, tier={resolved_tier}, {free_note}). "
        f"Key saved to .env as {api_key_env}."
    )


@settings.command("remove")
@click.argument("name")
def settings_remove(name: str):
    """Remove a custom agent (built-in ones are removed by unsetting their key/CLI instead)."""
    if SettingsStore().remove(name):
        click.echo(f"Removed '{name}'.")
    else:
        click.echo(f"No custom agent named '{name}'.")


@settings.command("add-host")
@click.argument("name")
@click.option("--url", required=True, help="e.g. http://localhost:11434 or a Tailscale IP for a remote PC.")
@click.option("--model", required=True, help="Model to run on THIS machine - pick one that fits its VRAM.")
def settings_add_host(name: str, url: str, model: str):
    """Register a machine running Ollama, e.g.:

    orchestrator settings add-host laptop --url http://localhost:11434 --model qwen2.5-coder:7b
    orchestrator settings add-host desktop-3070 --url http://100.x.y.2:11434 --model qwen2.5-coder:14b
    """
    HostsStore().add(HostConfig(name=name, url=url, model=model))
    click.echo(f"Registered host '{name}' -> {model} @ {url}")


@settings.command("hosts")
def settings_hosts():
    """List registered Ollama machines."""
    hosts = HostsStore().load()
    if not hosts:
        click.echo("No hosts registered - using env default (OLLAMA_HOST/OLLAMA_HOSTS/OLLAMA_MODEL).")
        return
    for h in hosts:
        click.echo(f"  {h.name:16} {h.model:22} {h.url}")


@settings.command("remove-host")
@click.argument("name")
def settings_remove_host(name: str):
    """Unregister a machine."""
    if HostsStore().remove(name):
        click.echo(f"Removed host '{name}'.")
    else:
        click.echo(f"No host named '{name}'.")


if __name__ == "__main__":
    main()
