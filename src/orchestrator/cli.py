from __future__ import annotations

import asyncio
import os
import shutil
import sys

import click

# Cloud-model output routinely contains characters (→, em dashes, checkmarks)
# that Windows' legacy console codepage (cp1252) can't encode - reconfigure
# to UTF-8 so `orchestrator run` doesn't crash printing a perfectly normal
# review. Python 3.7+; a no-op on platforms where this isn't needed/possible.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from .actions import add_account, resolve_agent, save_first_account, save_gemini_key
from .cluster_store import ClusterConfig, ClusterStore
from .config import build_fleet
from .context_store import ContextStore
from .env_store import set_env_var
from .hosts_store import HostConfig, HostsStore
from .menu import main_menu
from .modes import debate, plan_execute
from .router import DEFAULT_BIAS
from .settings_store import PRESETS, SettingsStore
from .task_list_store import TaskListStore, render


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context):
    """Multi-model orchestrator: Claude Code + Gemini + Ollama (+ others), working together.

    Run with no arguments for an interactive arrow-key menu. Every action
    below is also a direct command, for scripting/automation.
    """
    if ctx.invoked_subcommand is None:
        main_menu()


@main.command()
def menu():
    """Launch the interactive menu (same as running `orchestrator` with no arguments)."""
    main_menu()


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
@click.option("--bias", "bias", type=click.IntRange(0, 10), default=None, help="Local<->cloud balance for this run only (0=always cloud, 10=always local first). Defaults to the persisted setting.")
def run(task: str, max_steps: int, local_choice: str | None, bias: int | None):
    """Plan with a heavy model, execute steps (local model first, escalate on complexity)."""
    fleet = build_fleet(local_hosts=_pick_local_hosts(local_choice))
    store = ContextStore()
    result = asyncio.run(plan_execute.run(task, fleet, store, max_steps=max_steps, local_bias=bias))
    click.echo("\n=== FINAL RESULT ===\n")
    click.echo(result)


@main.command()
def tasks():
    """Show the task list (plan) from the current/last run, with live status per step."""
    task_run = TaskListStore().load()
    if not task_run:
        click.echo("No task list yet - run `orchestrator run \"...\"` first.")
        return
    click.echo(render(task_run))
    click.echo("\n(finished)" if task_run.finished else "\n(in progress or interrupted)")


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
    click.echo(f"Cloud-fast: {[a.name for a in fleet.cloud_fast]}")
    click.echo(f"Local-hard: {[a.name for a in fleet.local_hard]} (free RPC cluster(s) - hard steps before spending planner budget)")
    click.echo(f"Cloud:    {[a.name for a in fleet.cloud]}")
    click.echo(f"Local:    {fleet.local.name + ' ' + str([h.name for h in fleet.local.hosts]) if fleet.local else 'none'}")
    click.echo(f"Bias:     {os.environ.get('LOCAL_BIAS', str(DEFAULT_BIAS))}/10 (0=max cloud precision, 10=max local savings)")


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
    if shutil.which("agy"):
        click.echo(f"  {'gemini':12} ok (Google account login via Antigravity CLI)")
    elif os.environ.get("GEMINI_API_KEY"):
        click.echo(f"  {'gemini':12} ok (GEMINI_API_KEY)")
    else:
        click.echo(f"  {'gemini':12} not configured - see `orchestrator settings add gemini`")
    if shutil.which("copilot"):
        click.echo(f"  {'copilot':12} ok (GitHub account login via copilot CLI)")
    else:
        click.echo(f"  {'copilot':12} not installed - see `orchestrator settings add copilot`")

    hosts = HostsStore().load()
    if hosts:
        click.echo(f"  {'ollama':12} {len(hosts)} registered host(s) - see `orchestrator settings hosts`")
    else:
        legacy = os.environ.get("OLLAMA_HOSTS") or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        click.echo(f"  {'ollama':12} no named hosts registered, using env default: {legacy}")

    clusters = ClusterStore().load()
    click.echo(f"  {'cluster(s)':12} {len(clusters)} registered - see `orchestrator settings clusters`" if clusters else f"  {'cluster(s)':12} none - see README \"Running a big model across two PCs\"")

    click.echo("\nCustom (OpenAI-compatible) agents:")
    custom = SettingsStore().load()
    if not custom:
        click.echo("  (none - try `orchestrator settings presets` then `settings add <name>`)")
    for p in custom:
        free_tag = "free" if PRESETS.get(p.name, {}).get("free") else "paid"
        click.echo(f"  {p.name:12} model={p.model:32} tier={p.tier:8} [{free_tag}] {len(p.accounts)} account(s):")
        for a in p.accounts:
            has_key = "ok" if os.environ.get(a.api_key_env) else "MISSING KEY"
            click.echo(f"      {a.label:12} weight={a.weight:<3} key={a.api_key_env} [{has_key}]")


@settings.command("bias")
@click.argument("level", type=click.IntRange(0, 10), required=False)
def settings_bias(level: int | None):
    """Show, or persist, the local<->cloud balance (0=always escalate to
    cloud, 10=always try local first). `orchestrator run --bias N` overrides
    this for a single run without changing the saved default."""
    if level is None:
        current = os.environ.get("LOCAL_BIAS", str(DEFAULT_BIAS))
        click.echo(f"Current local/cloud balance: {current}/10 (0=max cloud precision, 10=max local savings)")
        return
    set_env_var("LOCAL_BIAS", str(level))
    click.echo(f"Saved - local/cloud balance set to {level}/10.")


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
        "tiers as of writing - see README for sources. `orchestrator settings add <name>`.\n"
        "Have more than one account for a provider? `orchestrator settings add-account <name>` "
        "pools them - round-robin, failover on rate limits."
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
@click.option("--tier", type=click.Choice(["planner", "cloud-fast", "cloud", "local"]), default=None)
@click.option("--label", default="default", help="Account label, if you'll add more accounts for this provider later.")
@click.option("--weight", default=1, type=int, help="Selection weight vs. other accounts on this provider (higher = picked more often in ordinary rotation).")
def settings_add(name: str, preset: str | None, base_url: str | None, model: str | None, tier: str | None, label: str, weight: int):
    """Add a new agent (its first account), e.g.: orchestrator settings add deepseek

    Already added this provider and want a second account (e.g. two Groq
    signups, to pool their rate limits)? Use `settings add-account` instead.

    NAME "gemini" is special-cased - prefer Antigravity CLI instead (Windows:
    irm https://antigravity.google/cli/install.ps1 | iex, then run `agy` once),
    which needs no key. Falls back to prompting for GEMINI_API_KEY if you'd
    rather not use the CLI.
    NAME "copilot" is special-cased too - prefer `npm install -g @github/copilot
    && copilot` (GitHub account login), no key needed either.
    Anything else is added as an OpenAI-compatible agent via .env + providers.json.
    """
    if name == "gemini":
        if shutil.which("agy"):
            click.echo("Antigravity CLI (agy) is already installed and preferred - no key needed once you're logged in.")
            return
        click.echo("agy (Antigravity CLI) not found. Falling back to GEMINI_API_KEY.")
        key = click.prompt("GEMINI_API_KEY (input hidden)", hide_input=True)
        save_gemini_key(key)
        click.echo("Saved GEMINI_API_KEY to .env")
        return
    if name == "copilot":
        if shutil.which("copilot"):
            click.echo("copilot CLI is already installed and preferred - no key needed once you're logged in.")
        else:
            click.echo("copilot CLI not found. No API-key fallback is offered for Copilot - install it: npm install -g @github/copilot")
        return

    if SettingsStore().get(name):
        raise click.UsageError(
            f"'{name}' is already registered. Use `orchestrator settings add-account {name}` "
            f"to add another account to it instead."
        )

    try:
        resolved = resolve_agent(name, preset, base_url, model, tier)
    except ValueError as e:
        raise click.UsageError(str(e))

    key = click.prompt(f"API key for '{name}' account '{label}' (input hidden)", hide_input=True)
    env_var = save_first_account(resolved, label, key)
    free_note = "free tier" if resolved.free else "paid - billed on your own key"
    click.echo(
        f"Added agent '{resolved.name}' (model={resolved.model}, tier={resolved.tier}, {free_note}). "
        f"Key saved to .env as {env_var}."
    )


@settings.command("add-account")
@click.argument("name")
@click.option("--label", required=True, help="A short label for this account, e.g. 'personal', 'acct2'.")
@click.option("--weight", default=1, type=int, help="Selection weight vs. this provider's other accounts (higher = picked more often in ordinary rotation; a low-weight account still gets used if the higher ones are all rate-limited).")
def settings_add_account(name: str, label: str, weight: int):
    """Add another account to an already-registered provider, e.g.:

    orchestrator settings add-account groq --label personal
    orchestrator settings add-account groq --label work --weight 5

    Pools the keys: weighted rotation across accounts, and a 429 (rate
    limit) takes just that account out of the pool until it cools down -
    the other accounts keep working in the meantime.
    """
    key = click.prompt(f"API key for '{name}' account '{label}' (input hidden)", hide_input=True)
    try:
        env_var = add_account(name, label, key, weight=weight)
    except ValueError as e:
        raise click.UsageError(str(e))
    click.echo(f"Added account '{label}' to '{name}'. Key saved to .env as {env_var}.")


@settings.command("remove")
@click.argument("name")
@click.option("--label", default=None, help="Remove just this one account instead of the whole agent.")
def settings_remove(name: str, label: str | None):
    """Remove a custom agent, or just one of its accounts with --label."""
    if label:
        if SettingsStore().remove_account(name, label):
            click.echo(f"Removed account '{label}' from '{name}'.")
        else:
            click.echo(f"No account '{label}' on '{name}'.")
        return
    if SettingsStore().remove(name):
        click.echo(f"Removed '{name}' (all accounts).")
    else:
        click.echo(f"No custom agent named '{name}'.")


@settings.command("add-host")
@click.argument("name")
@click.option("--url", required=True, help="e.g. http://localhost:11434 or a Tailscale IP for a remote PC.")
@click.option("--model", required=True, help="Model to run on THIS machine - pick one that fits its VRAM.")
@click.option("--size", type=click.Choice(["small", "standard", "big"]), default="standard", help="Routing hint - short steps prefer a 'small' host, longer ones prefer 'big'.")
def settings_add_host(name: str, url: str, model: str, size: str):
    """Register a machine running Ollama, e.g.:

    orchestrator settings add-host laptop --url http://localhost:11434 --model qwen2.5-coder:7b

    Register the SAME machine twice (same --url) with different --model/
    --size to let it serve both a fast small model and a stronger big one:

    orchestrator settings add-host laptop-small --url http://localhost:11434 --model qwen2.5-coder:1.5b --size small
    orchestrator settings add-host laptop-big   --url http://localhost:11434 --model qwen2.5-coder:7b   --size big
    """
    HostsStore().add(HostConfig(name=name, url=url, model=model, size=size))
    click.echo(f"Registered host '{name}' -> {model} @ {url} (size={size})")


@settings.command("hosts")
def settings_hosts():
    """List registered Ollama machines."""
    hosts = HostsStore().load()
    if not hosts:
        click.echo("No hosts registered - using env default (OLLAMA_HOST/OLLAMA_HOSTS/OLLAMA_MODEL).")
        return
    for h in hosts:
        click.echo(f"  {h.name:16} {h.model:22} [{h.size:8}] {h.url}")


@settings.command("remove-host")
@click.argument("name")
def settings_remove_host(name: str):
    """Unregister a machine."""
    if HostsStore().remove(name):
        click.echo(f"Removed host '{name}'.")
    else:
        click.echo(f"No host named '{name}'.")


@settings.command("add-cluster")
@click.argument("name")
@click.option("--url", required=True, help="llama-server's OpenAI-compatible endpoint, e.g. http://localhost:8080/v1")
@click.option("--model", required=True, help="Model name llama-server reports (see its /v1/models, or just the gguf filename).")
def settings_add_cluster(name: str, url: str, model: str):
    """Register a llama.cpp RPC cluster (a big model split across 2+ PCs) -
    see README "Running a big model across two PCs" to build/run it first.

    orchestrator settings add-cluster big-llama --url http://localhost:8080/v1 --model Qwen2.5-32B-Instruct

    Hard-complexity steps try this (free, no API cost) before falling back
    to the planner. Not Ollama - this points at a raw llama-server instance.
    """
    ClusterStore().add(ClusterConfig(name=name, base_url=url, model=model))
    click.echo(f"Registered cluster '{name}' -> {model} @ {url}")


@settings.command("clusters")
def settings_clusters():
    """List registered llama.cpp RPC clusters."""
    clusters = ClusterStore().load()
    if not clusters:
        click.echo("No clusters registered - see README \"Running a big model across two PCs\".")
        return
    for c in clusters:
        click.echo(f"  {c.name:16} {c.model:28} {c.base_url}")


@settings.command("remove-cluster")
@click.argument("name")
def settings_remove_cluster(name: str):
    """Unregister a cluster."""
    if ClusterStore().remove(name):
        click.echo(f"Removed cluster '{name}'.")
    else:
        click.echo(f"No cluster named '{name}'.")


if __name__ == "__main__":
    main()
