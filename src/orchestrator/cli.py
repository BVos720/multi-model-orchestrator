from __future__ import annotations

import asyncio
import os
import shutil
import sys

import click

# Cloud-model output routinely contains characters (→, em dashes, checkmarks)
# that Windows' legacy console codepage (cp1252) can't encode - reconfigure
# to UTF-8 so `orchest run` doesn't crash printing a perfectly normal
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
from .modes import code as code_mode
from .modes import debate, plan_execute
from . import network
from .router import DEFAULT_BIAS
from .settings_store import PRESETS, SettingsStore
from .task_list_store import TaskListStore, render


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context):
    """OrchestCLI: Claude Code + Antigravity + Copilot + Ollama (+ others), working together.

    Run with no arguments for an interactive arrow-key menu. Every action
    below is also a direct command, for scripting/automation.
    """
    if ctx.invoked_subcommand is None:
        main_menu()


@main.command()
def menu():
    """Launch the interactive menu (same as running `orchest` with no arguments)."""
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


async def _on_exhausted(failed_agent, error, candidates):
    """When a planner looks out of quota mid-run: ask which agent to
    continue with (interactive), or auto-pick the first candidate and say
    so (non-interactive/scripted use) instead of just crashing the run."""
    if not candidates:
        return None
    if not sys.stdin.isatty():
        click.echo(f"! {failed_agent.name} looks out of capacity - continuing with {candidates[0].name}.")
        return candidates[0]
    click.echo(f"\n! {failed_agent.name} looks out of capacity: {error}")
    click.echo("Continue with a different model?")
    for i, a in enumerate(candidates, 1):
        click.echo(f"  {i}. {a.name}")
    click.echo(f"  {len(candidates) + 1}. abort")
    choice = click.prompt("Choice", default="1")
    try:
        idx = int(choice)
        return None if idx == len(candidates) + 1 else candidates[idx - 1]
    except (ValueError, IndexError):
        return None


@main.command()
@click.argument("task")
@click.option("--max-steps", default=12, show_default=True, help="Cap on plan steps to execute.")
@click.option("--local", "local_choice", default=None, help="Registered host name, comma-list, or 'all'. Prompts if omitted and >1 host is registered.")
@click.option("--bias", "bias", type=click.IntRange(0, 10), default=None, help="Local<->cloud balance for this run only (0=always cloud, 10=always local first). Defaults to the persisted setting.")
def run(task: str, max_steps: int, local_choice: str | None, bias: int | None):
    """Plan with a heavy model, execute steps (local model first, escalate on complexity). Read-only - see `code` to actually edit files."""
    click.echo(f"Working in: {os.getcwd()}\n")
    fleet = build_fleet(local_hosts=_pick_local_hosts(local_choice))
    store = ContextStore()
    result = asyncio.run(
        plan_execute.run(task, fleet, store, max_steps=max_steps, local_bias=bias, on_exhausted=_on_exhausted)
    )
    click.echo("\n=== FINAL RESULT ===\n")
    click.echo(result)


@main.command()
@click.argument("task")
def code(task: str):
    """Actually edit/write files - like calling Claude Code directly, but
    auto-picking whichever of Claude Code / Antigravity CLI / Copilot CLI
    is available. Operates on the CURRENT directory (cd there first, same
    as you would with `claude`). NOT read-only, unlike `run`/`ask` - see
    README "Coding mode" for exactly what each CLI is allowed to do.
    """
    click.echo(f"Working in: {os.getcwd()}\n")
    result = asyncio.run(code_mode.run(task))
    click.echo("\n=== DONE ===\n")
    click.echo(result)


@main.command()
def tasks():
    """Show the task list (plan) from the current/last run, with live status per step."""
    task_run = TaskListStore().load()
    if not task_run:
        click.echo("No task list yet - run `orchest run \"...\"` first.")
        return
    click.echo(render(task_run))
    click.echo("\n(finished)" if task_run.finished else "\n(in progress or interrupted)")


@main.command()
@click.argument("question")
def ask(question: str):
    """Ask every configured cloud model the same question, merge into one verdict."""
    click.echo(f"Working in: {os.getcwd()}\n")
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
        click.echo(f"  {'gemini':12} not configured - see `orchest settings add gemini`")
    if shutil.which("copilot"):
        click.echo(f"  {'copilot':12} ok (GitHub account login via copilot CLI)")
    else:
        click.echo(f"  {'copilot':12} not installed - see `orchest settings add copilot`")

    hosts = HostsStore().load()
    if hosts:
        click.echo(f"  {'ollama':12} {len(hosts)} registered host(s) - see `orchest settings hosts`")
    else:
        legacy = os.environ.get("OLLAMA_HOSTS") or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        click.echo(f"  {'ollama':12} no named hosts registered, using env default: {legacy}")

    clusters = ClusterStore().load()
    click.echo(f"  {'cluster(s)':12} {len(clusters)} registered - see `orchest settings clusters`" if clusters else f"  {'cluster(s)':12} none - see README \"Running a big model across two PCs\"")

    click.echo("\nCustom (OpenAI-compatible) agents:")
    custom = SettingsStore().load()
    if not custom:
        click.echo("  (none - try `orchest settings presets` then `settings add <name>`)")
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
    cloud, 10=always try local first). `orchest run --bias N` overrides
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
        "tiers as of writing - see README for sources. `orchest settings add <name>`.\n"
        "Have more than one account for a provider? `orchest settings add-account <name>` "
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
    """Add a new agent (its first account), e.g.: orchest settings add deepseek

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
            f"'{name}' is already registered. Use `orchest settings add-account {name}` "
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

    orchest settings add-account groq --label personal
    orchest settings add-account groq --label work --weight 5

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


def _pull_model_with_progress(url: str, model: str) -> bool:
    """Pull `model` on the Ollama instance at `url`, rendering
    network.pull_model's progress as one self-overwriting line - plain
    \\r, no ANSI needed, so it's safe on any Windows console."""

    def _on_progress(chunk: dict) -> None:
        status = chunk.get("status", "")
        total = chunk.get("total")
        completed = chunk.get("completed")
        if total and completed:
            print(f"\r  {status}: {completed / total * 100:5.1f}%   ", end="", flush=True)
        else:
            print(f"\r  {status}" + " " * 20, end="", flush=True)

    ok = asyncio.run(network.pull_model(url, model, on_progress=_on_progress))
    print()
    return ok


@settings.command("add-host")
@click.argument("name")
@click.option("--url", required=True, help="e.g. http://localhost:11434 or a Tailscale IP for a remote PC.")
@click.option("--model", required=True, help="Model to run on THIS machine - pick one that fits its VRAM.")
@click.option("--size", type=click.Choice(["small", "standard", "big"]), default="standard", help="Routing hint - short steps prefer a 'small' host, longer ones prefer 'big'.")
def settings_add_host(name: str, url: str, model: str, size: str):
    """Register a machine running Ollama, e.g.:

    orchest settings add-host laptop --url http://localhost:11434 --model qwen2.5-coder:7b

    Register the SAME machine twice (same --url) with different --model/
    --size to let it serve both a fast small model and a stronger big one:

    orchest settings add-host laptop-small --url http://localhost:11434 --model qwen2.5-coder:1.5b --size small
    orchest settings add-host laptop-big   --url http://localhost:11434 --model qwen2.5-coder:7b   --size big

    If --model isn't already pulled on that machine, this pulls it on
    demand (via Ollama's own /api/pull - no SSH/CLI access to that
    machine needed) before registering.
    """
    if asyncio.run(network.ollama_reachable(url)) and model not in asyncio.run(network.list_models(url)):
        pull_now = not sys.stdin.isatty() or click.confirm(
            f"'{model}' isn't pulled on {url} yet - pull it now?", default=True
        )
        if pull_now:
            click.echo(f"Pulling '{model}' on {url} (this can take a while for a big model)...")
            if _pull_model_with_progress(url, model):
                click.echo(f"'{model}' pulled.")
            else:
                click.echo(f"! Pull failed - '{model}' may not be usable on {url} yet.")
        else:
            click.echo(f"! Skipping pull - registering anyway, but '{model}' isn't on {url} yet.")

    HostsStore().add(HostConfig(name=name, url=url, model=model, size=size))
    click.echo(f"Registered host '{name}' -> {model} @ {url} (size={size})")
    if asyncio.run(network.ollama_reachable(url)):
        click.echo(f"Neural handshake complete - '{name}' is online and drift-compatible.")
    else:
        click.echo(f"(Not reachable yet at {url} - fine if that worker just isn't up right now.)")
    warning = network.connection_warning(url)
    if warning:
        click.echo(f"! {warning}")


@settings.command("hosts")
def settings_hosts():
    """List registered Ollama machines, and whether each one's model is
    actually pulled there right now (a registered-but-not-pulled model
    will fail the first time a run actually tries to use it)."""
    hosts = HostsStore().load()
    if not hosts:
        click.echo("No hosts registered - using env default (OLLAMA_HOST/OLLAMA_HOSTS/OLLAMA_MODEL).")
        return

    async def _check(url: str) -> tuple[bool, list[str]]:
        reachable = await network.ollama_reachable(url)
        models = await network.list_models(url) if reachable else []
        return reachable, models

    async def _check_all() -> dict[str, tuple[bool, list[str]]]:
        urls = {h.url for h in hosts}
        results = await asyncio.gather(*[_check(u) for u in urls])
        return dict(zip(urls, results))

    status_by_url = asyncio.run(_check_all())
    for h in hosts:
        reachable, models = status_by_url.get(h.url, (False, []))
        if not reachable:
            tag = "unreachable"
        elif h.model in models:
            tag = "installed"
        else:
            tag = "NOT installed"
        click.echo(f"  {h.name:16} {h.model:22} [{h.size:8}] {h.url:28} ({tag})")


@settings.command("remove-host")
@click.argument("name")
def settings_remove_host(name: str):
    """Unregister a machine."""
    if HostsStore().remove(name):
        click.echo(f"Removed host '{name}'.")
    else:
        click.echo(f"No host named '{name}'.")


@settings.command("register-worker")
@click.option("--model", default="qwen2.5-coder:7b", show_default=True, help="Model you have (or will) pull on THIS machine.")
@click.option("--name", "host_name", default=None, help="Name to suggest for this host (default: this machine's hostname).")
def settings_register_worker(model: str, host_name: str | None):
    """Configure THIS machine as a worker, and show what to run on the
    SUPERVISOR PC to add it.

    This machine doesn't need the orchestrator installed at all as a
    worker - just Ollama running. Run this here to get the exact
    `settings add-host` command to paste on the other PC.

    Also handles the networking setup that's easy to get wrong by hand:
    persists OLLAMA_HOST=0.0.0.0 (Ollama listens on loopback ONLY by
    default - the single most common reason a worker never answers),
    adds a Windows Firewall inbound-allow rule for its port (via a real
    UAC prompt - never done silently), and puts this CLI on the user
    PATH so it's callable from any terminal, any directory, from now on.
    """
    import socket as _socket

    ip = network.local_ip()
    name = host_name or _socket.gethostname().lower()

    click.echo("Configuring this machine's networking for worker use...")
    net = network.configure_worker_networking()
    if not net.get("platform_supported", True):
        click.echo("(Skipped - Windows-only. Set OLLAMA_HOST=0.0.0.0 and open the firewall port yourself.)")
    else:
        host_result = net["ollama_host"]
        if host_result == "set":
            click.echo(
                "  OLLAMA_HOST=0.0.0.0:11434 saved - Ollama must be RESTARTED to pick this up "
                "(quit it fully from the tray icon first, then reopen it or re-run `ollama serve`)."
            )
        elif host_result == "already-set":
            click.echo("  OLLAMA_HOST already set to 0.0.0.0:11434.")
        else:
            click.echo(
                '  ! Could not set OLLAMA_HOST automatically - set it yourself: '
                '$env:OLLAMA_HOST = "0.0.0.0:11434" (permanently, then restart Ollama).'
            )

        firewall_result = net["firewall"]
        if firewall_result == "added":
            click.echo("  Firewall rule added - inbound TCP 11434 now allowed.")
        elif firewall_result == "already-present":
            click.echo("  Firewall rule already present.")
        else:
            click.echo(
                "  ! Firewall rule not added (declined the elevation prompt, or it failed) - add it "
                'yourself: New-NetFirewallRule -DisplayName "Ollama (11434)" -Direction Inbound '
                "-Protocol TCP -LocalPort 11434 -Action Allow"
            )

    path_result = network.ensure_venv_scripts_on_path()
    if path_result == "added":
        click.echo("  Added this CLI to your user PATH - open a NEW terminal to use `orchest`/`orchestcli` from anywhere.")
    elif path_result == "already-on-path":
        click.echo("  This CLI is already on your user PATH.")

    reachable = asyncio.run(network.ollama_reachable())
    click.echo(f"\nThis machine's LAN IP: {ip}")
    click.echo(f"Ollama on :11434 (localhost): {'reachable' if reachable else 'NOT reachable - is `ollama serve` running?'}")
    if net.get("ollama_host") == "set":
        click.echo("(That localhost check can't confirm the new OLLAMA_HOST took effect yet - restart Ollama first, per above.)")

    click.echo(
        "\nMake sure this machine and the supervisor PC are on the same network "
        "(direct Ethernet cable or Tailscale - see README \"Networking two PCs\"), "
        "then on the SUPERVISOR, run:\n"
    )
    click.echo(f"  orchest settings add-host {name} --url http://{ip}:11434 --model {model}")
    click.echo(
        "\nNever port-forward 11434 to the public internet - Ollama has no built-in auth."
    )

    if network.open_ollama_log_window():
        click.echo(
            "\nOpened a live Ollama log window - watch it for requests arriving once the "
            "supervisor registers this machine and starts sending it work."
        )
    else:
        click.echo(
            f"\n(No live log window - {network.ollama_log_path()} doesn't exist yet. "
            "Start Ollama at least once, then re-run this command to get one.)"
        )


@settings.command("scan-network")
@click.option("--port", default=11434, show_default=True, help="Port to probe (Ollama's default).")
@click.option("--timeout", default=0.5, show_default=True, type=float, help="Per-host probe timeout, in seconds.")
@click.option("--add/--no-add", default=False, help="Interactively register discovered machines as hosts.")
@click.option(
    "--tailscale", "via_tailscale", is_flag=True, default=False,
    help="Ask the local Tailscale daemon for its peer list instead of sweeping the LAN subnet - "
    "finds workers anywhere on your tailnet (not just this /24), and every match is already "
    "WireGuard-encrypted, so none of this command's security warnings apply to them.",
)
def settings_scan_network(port: int, timeout: float, add: bool, via_tailscale: bool):
    """Auto-scan the network for other PCs running Ollama.

    By default sweeps every address on this machine's /24 LAN subnet in
    parallel and reports which ones answer like a real Ollama server,
    along with whatever models they already have pulled - no need to know
    a worker PC's IP ahead of time. Pass --tailscale to discover peers via
    Tailscale instead (works across subnets, and is already encrypted -
    see README "Networking two PCs"). Pass --add to register matches as
    hosts interactively instead of just listing them.
    """
    if via_tailscale:
        click.echo("Asking Tailscale for peers, then probing each for Ollama...")
        found = asyncio.run(network.scan_tailscale_peers(port=port, timeout=max(timeout, 0.5)))
        if not found and not shutil.which("tailscale"):
            click.echo("`tailscale` CLI not found - is Tailscale installed and running?")
            return
    else:
        ip = network.local_ip()
        if ip == "127.0.0.1":
            click.echo("Could not determine this machine's LAN IP - are you connected to a network?")
            return
        subnet = ip.rsplit(".", 1)[0] + ".0/24"
        click.echo(f"Scanning {subnet} for Ollama workers on port {port}...")
        found = asyncio.run(network.scan_for_workers(port=port, timeout=timeout))

    if not found:
        click.echo("No Ollama workers found.")
        return

    existing = {h.url for h in HostsStore().load()}
    for w in found:
        tag = " (already registered)" if w["url"] in existing else ""
        models = ", ".join(w["models"]) or "(no models pulled)"
        click.echo(f"  {w['url']:28} {models}{tag}")

    if not add:
        click.echo("\nRun again with --add to register these interactively, or:")
        click.echo("  orchest settings add-host <name> --url <url> --model <model>")
        return

    for w in found:
        if w["url"] in existing:
            continue
        if not click.confirm(f"\nRegister {w['url']} as a host?", default=True):
            continue
        default_name = w["ip"].replace(".", "-")
        name = click.prompt("Host name", default=default_name)
        default_model = w["models"][0] if w["models"] else ""
        model = click.prompt("Model to use", default=default_model)
        size = click.prompt("Size", default="standard", type=click.Choice(["small", "standard", "big"]))
        if model not in w["models"]:
            if click.confirm(f"'{model}' isn't pulled on {w['url']} yet - pull it now?", default=True):
                click.echo(f"Pulling '{model}' on {w['url']} (this can take a while for a big model)...")
                if _pull_model_with_progress(w["url"], model):
                    click.echo(f"'{model}' pulled.")
                else:
                    click.echo(f"! Pull failed - '{model}' may not be usable on {w['url']} yet.")
            else:
                click.echo(f"! Skipping pull - registering anyway, but '{model}' isn't on {w['url']} yet.")
        HostsStore().add(HostConfig(name=name, url=w["url"], model=model, size=size))
        click.echo(f"Registered '{name}' -> {model} @ {w['url']} (size={size})")
        click.echo(f"Neural handshake complete - '{name}' is online and drift-compatible.")
        warning = network.connection_warning(w["url"])
        if warning:
            click.echo(f"! {warning}")


@settings.command("add-cluster")
@click.argument("name")
@click.option("--url", required=True, help="llama-server's OpenAI-compatible endpoint, e.g. http://localhost:8080/v1")
@click.option("--model", required=True, help="Model name llama-server reports (see its /v1/models, or just the gguf filename).")
def settings_add_cluster(name: str, url: str, model: str):
    """Register a llama.cpp RPC cluster (a big model split across 2+ PCs) -
    see README "Running a big model across two PCs" to build/run it first.

    orchest settings add-cluster big-llama --url http://localhost:8080/v1 --model Qwen2.5-32B-Instruct

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
