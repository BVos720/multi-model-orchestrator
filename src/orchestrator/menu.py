from __future__ import annotations

import asyncio
import os
import shutil

import questionary

from .actions import add_account, resolve_agent, save_first_account, save_gemini_key
from .attachments import save_clipboard_image
from .banner import BANNER
from .cluster_store import ClusterConfig, ClusterStore
from .config import build_fleet
from .context_store import ContextStore
from .env_store import set_env_var
from .hosts_store import HostConfig, HostsStore, POPULAR_MODELS
from . import network
from .modes import debate, plan_execute
from .router import DEFAULT_BIAS
from .settings_store import PRESETS, SettingsStore
from .task_list_store import TaskListStore
from .task_list_store import render as render_tasks
from . import dispatch_log, usage_tracker

BIAS_LEVELS = [
    (10, "██████████ 10  Max savings   - always try local first"),
    (8, "████████░░ 8   Mostly local"),
    (6, "██████░░░░ 6   Lean local"),
    (5, "█████░░░░░ 5   Balanced (default)"),
    (4, "████░░░░░░ 4   Lean cloud"),
    (2, "██░░░░░░░░ 2   Mostly cloud"),
    (0, "░░░░░░░░░░ 0   Max precision - always escalate to cloud"),
]

STYLE = questionary.Style(
    [
        ("qmark", "fg:#00d7ff bold"),
        ("question", "bold"),
        ("pointer", "fg:#00d7ff bold"),
        ("highlighted", "fg:#00d7ff bold"),
        ("selected", "fg:#00d7ff"),
    ]
)

def _pause() -> None:
    """No-op. Every menu action used to block here on "press enter to
    continue" before showing the next prompt - pure friction, since
    nothing clears the screen, so whatever was just printed stays visible
    in scrollback either way. Kept as a function (rather than deleting
    the ~35 call sites) so removing the wait was a one-line change."""
    return


def _pull_model_with_progress(url: str, model: str) -> bool:
    """Pull `model` on the Ollama instance at `url`, rendering
    network.pull_model's progress as one self-overwriting line."""

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


def _pick_model(url: str) -> tuple[str, bool]:
    """Let the user pick a model for `url` from what's already pulled
    there plus a curated shortlist, or type any other name. Returns
    (model, needs_pull) - needs_pull is True when the pick isn't already
    on that machine, so the caller knows to offer `_pull_model_with_progress`."""
    pulled = asyncio.run(network.list_models(url)) if url else []
    choices = [questionary.Choice(f"{m}  (already pulled)", value=m) for m in pulled]
    choices += [questionary.Choice(m, value=m) for m in POPULAR_MODELS if m not in pulled]
    choices.append(questionary.Choice("Other (type a model name)", value="__other__"))
    picked = questionary.select(
        f"Model to use on {url}:" if url else "Model to use:",
        choices=choices,
        style=STYLE,
    ).ask()
    if picked is None:
        return "", False
    if picked == "__other__":
        picked = questionary.text("Model name (Ollama tag, e.g. gemma4:12b):", style=STYLE).ask() or ""
    return picked, bool(picked) and picked not in pulled


def _print_status() -> None:
    fleet = build_fleet()
    local = fleet.local.name + " " + str([h.name for h in fleet.local.hosts]) if fleet.local else "none"
    bias = os.environ.get("LOCAL_BIAS", str(DEFAULT_BIAS))
    print(f"Planners: {[a.name for a in fleet.planners]}")
    print(f"Cloud-fast: {[a.name for a in fleet.cloud_fast]}")
    print(f"Local-hard: {[a.name for a in fleet.local_hard]} (free RPC cluster(s), tried before spending planner budget)")
    print(f"Cloud:    {[a.name for a in fleet.cloud]}")
    print(f"Local:    {local}")
    print(f"Bias:     {bias}/10 (0=max cloud precision, 10=max local savings)")


def _bias_menu() -> None:
    current = int(os.environ.get("LOCAL_BIAS", str(DEFAULT_BIAS)))
    print(f"Current local/cloud balance: {current}/10\n")
    choices = [questionary.Choice(label, value=val) for val, label in BIAS_LEVELS]
    closest = min(choices, key=lambda c: abs(c.value - current))
    picked = questionary.select(
        "Local <-> Cloud balance (favor free local models vs. paid cloud precision):",
        choices=choices,
        default=closest,
        style=STYLE,
    ).ask()
    if picked is None:
        return
    set_env_var("LOCAL_BIAS", str(picked))
    print(f"Saved - local/cloud balance set to {picked}/10.")
    _pause()


def _view_tasks() -> None:
    task_run = TaskListStore().load()
    if not task_run:
        print("No task list yet - run a task first.")
    else:
        print(render_tasks(task_run))
        print("\n(finished)" if task_run.finished else "\n(in progress or interrupted)")
    _pause()


def _print_settings_list() -> None:
    print("Built-in agents:")
    print(f"  {'claude-code':12} " + ("ok (CLI login)" if shutil.which("claude") else "claude CLI not found"))
    if shutil.which("agy"):
        print(f"  {'gemini':12} ok (Google account login via Antigravity CLI)")
    elif os.environ.get("GEMINI_API_KEY"):
        print(f"  {'gemini':12} ok (GEMINI_API_KEY)")
    else:
        print(f"  {'gemini':12} not configured")
    if shutil.which("copilot"):
        print(f"  {'copilot':12} ok (GitHub account login via copilot CLI)")
    else:
        print(f"  {'copilot':12} not installed (npm install -g @github/copilot)")

    hosts = HostsStore().load()
    if hosts:
        print(f"  {'ollama':12} {len(hosts)} registered host(s)")
    else:
        legacy = os.environ.get("OLLAMA_HOSTS") or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        print(f"  {'ollama':12} no named hosts registered, using env default: {legacy}")

    clusters = ClusterStore().load()
    print(f"  {'cluster(s)':12} {len(clusters)} registered" if clusters else f"  {'cluster(s)':12} none")

    print("\nCustom (OpenAI-compatible) agents:")
    custom = SettingsStore().load()
    if not custom:
        print("  (none)")
    for p in custom:
        free_tag = "free" if PRESETS.get(p.name, {}).get("free") else "paid"
        print(f"  {p.name:12} model={p.model:32} tier={p.tier:8} [{free_tag}] {len(p.accounts)} account(s):")
        for a in p.accounts:
            has_key = "ok" if os.environ.get(a.api_key_env) else "MISSING KEY"
            print(f"      {a.label:12} weight={a.weight:<3} key={a.api_key_env} [{has_key}]")


def _pick_local_hosts_interactive() -> list[str] | None:
    registered = HostsStore().load()
    if not registered:
        return None
    if len(registered) == 1:
        return [registered[0].name]

    choices = [
        questionary.Choice(f"{h.name} - {h.model} @ {h.url}", value=h.name, checked=True) for h in registered
    ]
    picked = questionary.checkbox(
        "Which local machine(s) handle local steps this run? (space to toggle, enter to confirm)",
        choices=choices,
        style=STYLE,
    ).ask()
    if not picked:  # None (Ctrl-C) or nothing ticked -> use everything
        return [h.name for h in registered]
    return picked


async def _on_exhausted_interactive(failed_agent, error, candidates):
    """Menu version of 'continue with a different model, wait for this one
    specifically, or abort'."""
    if not candidates:
        return None
    print(f"\n! {failed_agent.name} looks out of capacity: {error}")
    choices = (
        [questionary.Choice(f"switch to {a.name}", value=a) for a in candidates]
        + [questionary.Choice(f"wait for {failed_agent.name} to recover", value="wait")]
        + [questionary.Choice("abort", value=None)]
    )
    return questionary.select("What now?", choices=choices, style=STYLE).ask()


async def _on_all_exhausted_interactive(planners, error):
    """Menu version: every planner is out of capacity, ask any/all/abort."""
    print(f"\n! Every planner is out of capacity: {error}")
    choices = [
        questionary.Choice("wait for ANY of them to recover", value="any"),
        questionary.Choice("wait for ALL of them to recover", value="all"),
        questionary.Choice("abort", value=None),
    ]
    return questionary.select("What now?", choices=choices, style=STYLE).ask()


def _run_task() -> None:
    print(f"Working in: {os.getcwd()}")
    task = questionary.text("What should the swarm do?", style=STYLE).ask()
    if not task:
        return

    if questionary.confirm(
        "Attach a screenshot from your clipboard? (screenshot first with Win+Shift+S, "
        "then say yes here - this reads whatever image is on the clipboard right now)",
        default=False,
        style=STYLE,
    ).ask():
        try:
            path = save_clipboard_image()
        except RuntimeError as e:
            print(f"! {e}")
            path = None
        if path:
            print(f"Saved {path}")
            task += (
                f"\n\n[Attached screenshot: {path}. Whether an agent can actually see this "
                "depends on its own capabilities - Claude Code can Read image files; a "
                "text-only local model or a CLI without local-file image support can't, "
                "and will only see this file path as text.]"
            )
        else:
            print("! Clipboard doesn't currently hold an image - continuing without one.")

    max_steps_raw = questionary.text("Max plan steps:", default="12", style=STYLE).ask()
    try:
        max_steps = int(max_steps_raw)
    except (TypeError, ValueError):
        max_steps = 12

    local_hosts = _pick_local_hosts_interactive()
    fleet = build_fleet(local_hosts=local_hosts)
    store = ContextStore()
    bias = int(os.environ.get("LOCAL_BIAS", str(DEFAULT_BIAS)))
    print(f"\nLocal/cloud balance: {bias}/10 (change in Settings). Running...\n")
    result = asyncio.run(
        plan_execute.run(
            task, fleet, store, max_steps=max_steps,
            on_exhausted=_on_exhausted_interactive, on_all_exhausted=_on_all_exhausted_interactive,
        )
    )
    print("\n=== FINAL RESULT ===\n")
    print(result)
    _pause()


def _code_task() -> None:
    """Confirm a target folder explicitly (rather than silently using
    whatever happened to be current), then either:
    - a real interactive CLI session (full permissions incl. Bash - its
      own prompts are the actual safety net, not anything enforced here), or
    - the headless auto-accept mode (edits only, Bash disallowed, no prompts).

    The headless mode's loop is a real session too, not a one-shot: pick
    the CLI once, then keep prompting for the next instruction until you
    leave blank to go back - each later prompt continues the same Claude
    Code conversation (real session memory via --continue), so it behaves
    like actually opening `claude` and typing into it.

    Neither path is an OS-level sandbox: Claude Code's Read/Write/Edit
    tools stay scoped to the chosen folder and below by default (nothing
    here grants more via --add-dir), but Bash is a real shell and could
    still reach outside it - that's exactly why the interactive path
    keeps its permission prompts on rather than pretending to be a jail
    this wrapper can't actually enforce.
    """
    from .modes import code as code_mode

    folder = questionary.text("Which folder should it work in?", default=os.getcwd(), style=STYLE).ask()
    if not folder:
        return
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        print(f"! {folder} isn't a folder that exists.")
        _pause()
        return

    mode = questionary.select(
        "How should it work here?",
        choices=[
            questionary.Choice(
                "Hybrid: local agent first, free - escalates to cloud only if it can't finish (saves tokens)",
                value="hybrid",
            ),
            questionary.Choice(
                "Specialized: Codex (frontend) + Claude Code (backend) split the task, one bounded exchange round",
                value="specialized",
            ),
            questionary.Choice(
                "Full interactive session (real permission prompts, Bash + installs allowed)",
                value="interactive",
            ),
            questionary.Choice(
                "Auto-accept edits only (faster, no prompts, Bash/tool-installs blocked)",
                value="auto",
            ),
        ],
        style=STYLE,
    ).ask()
    if not mode:
        return

    if mode == "specialized":
        print(f"Working in: {folder}")
        task = questionary.text("What should it do in this folder?", style=STYLE).ask()
        if not task:
            return
        try:
            result = asyncio.run(code_mode.run_specialized(task, folder))
        except RuntimeError as e:
            print(f"! {e}")
            _pause()
            return
        print("\n=== DONE ===\n")
        print(result)
        _pause()
        return

    if mode == "hybrid":
        print(f"Working in: {folder}")
        task = questionary.text("What should it do in this folder?", style=STYLE).ask()
        if not task:
            return
        fleet = build_fleet()
        try:
            result = asyncio.run(code_mode.run_hybrid(task, folder, fleet.local))
        except RuntimeError as e:
            print(f"! {e}")
            _pause()
            return
        print("\n=== DONE ===\n")
        print(result)
        _pause()
        return

    if mode == "interactive":
        print(f"Working in: {folder}")
        if not questionary.confirm(
            "This grants full tool access including Bash, gated by that CLI's own prompts - continue?",
            default=True, style=STYLE,
        ).ask():
            return
        task = questionary.text("Starting prompt (optional - blank just opens the session):", style=STYLE).ask()
        code_mode.run_interactive(task or None, cwd=folder)
        return

    print(f"Working in: {folder}")
    print(
        "This mode actually edits/writes files here (Bash disallowed, edits auto-accepted, no prompts) "
        "- not fully interactive. See README \"Coding mode\"."
    )
    if not questionary.confirm("Continue?", default=True, style=STYLE).ask():
        return

    old_cwd = os.getcwd()
    os.chdir(folder)
    try:
        try:
            name, agent = code_mode.pick_agent()
        except RuntimeError as e:
            print(f"! {e}")
            _pause()
            return
        print(f"Using {name} to code in this folder (see README for what --mode=code grants).")
        if name == "claude-code":
            print("Session memory is on - later prompts here remember earlier ones, like a real `claude` session.\n")

        continue_session = False
        while True:
            task = questionary.text(
                "What should it do in this folder? (blank to go back)", style=STYLE
            ).ask()
            if not task:
                return
            try:
                result = asyncio.run(code_mode.run(task, agent=agent, continue_session=continue_session))
            except RuntimeError as e:
                print(f"! {e}")
                continue
            print("\n=== DONE ===\n")
            print(result)
            print()
            continue_session = True
    finally:
        os.chdir(old_cwd)


def _ask_question() -> None:
    print(f"Working in: {os.getcwd()}")
    question = questionary.text("What's the question?", style=STYLE).ask()
    if not question:
        return
    fleet = build_fleet()
    store = ContextStore()
    print("\nAsking every configured cloud model...\n")
    result = asyncio.run(debate.run(question, fleet, store))
    print("\n=== VERDICT ===\n")
    print(result)
    _pause()


def _add_agent_menu() -> None:
    registered = {p.name for p in SettingsStore().load()}
    choices = [
        questionary.Choice("gemini (Google account login via Antigravity CLI preferred - see README)", value="gemini"),
        questionary.Choice("copilot (GitHub account login preferred - see README)", value="copilot"),
    ]
    for name, cfg in PRESETS.items():
        tag = "free" if cfg.get("free") else "paid"
        extra = " - already added, pick again to add another account" if name in registered else ""
        choices.append(questionary.Choice(f"{name} [{tag}] - {cfg['model']}{extra}", value=name))
    choices.append(questionary.Choice("custom (your own OpenAI-compatible endpoint)", value="__custom__"))
    choices.append(questionary.Choice("(cancel)", value=None))

    choice = questionary.select("Add which agent?", choices=choices, style=STYLE).ask()
    if not choice:
        return

    if choice == "gemini":
        if shutil.which("agy"):
            print("Antigravity CLI (agy) is already installed and preferred - no key needed once you're logged in.")
        else:
            print("agy (Antigravity CLI) not found - falling back to GEMINI_API_KEY.")
            key = questionary.password("GEMINI_API_KEY (fallback if you won't use Antigravity CLI):", style=STYLE).ask()
            if key:
                save_gemini_key(key)
                print("Saved GEMINI_API_KEY to .env")
        _pause()
        return
    if choice == "copilot":
        if shutil.which("copilot"):
            print("copilot CLI is already installed and preferred - no key needed once you're logged in.")
        else:
            print("Install with: npm install -g @github/copilot, then run `copilot` once to log in.")
        _pause()
        return

    # Already-registered preset picked again -> this is an additional account, not a new agent.
    if choice in registered and choice != "__custom__":
        label = questionary.text(
            f"'{choice}' is already added. Label for this new account (e.g. 'personal', 'acct2'):", style=STYLE
        ).ask()
        if not label:
            return
        key = questionary.password(f"API key for '{choice}' account '{label}':", style=STYLE).ask()
        if not key:
            print("Cancelled (no key entered).")
            _pause()
            return
        weight_raw = questionary.text(
            "Weight vs. this provider's other accounts (higher = picked more often; default 1):",
            default="1",
            style=STYLE,
        ).ask()
        try:
            weight = int(weight_raw)
        except (TypeError, ValueError):
            weight = 1
        try:
            env_var = add_account(choice, label, key, weight=weight)
        except ValueError as e:
            print(f"! {e}")
            _pause()
            return
        print(f"Added account '{label}' to '{choice}'. Key saved to .env as {env_var}.")
        _pause()
        return

    name, preset, base_url, model, tier = choice, choice, None, None, None
    if choice == "__custom__":
        name = questionary.text("Agent name:", style=STYLE).ask()
        base_url = questionary.text("Base URL (OpenAI-compatible):", style=STYLE).ask()
        model = questionary.text("Model id:", style=STYLE).ask()
        tier = questionary.select("Tier:", choices=["cloud", "cloud-fast", "planner", "local"], style=STYLE).ask()
        preset = "custom"
        if not (name and base_url and model and tier):
            return

    try:
        resolved = resolve_agent(name, preset, base_url, model, tier)
    except ValueError as e:
        print(f"! {e}")
        _pause()
        return

    key = questionary.password(f"API key for '{resolved.name}':", style=STYLE).ask()
    if not key:
        print("Cancelled (no key entered).")
        _pause()
        return
    env_var = save_first_account(resolved, "default", key)
    free_note = "free tier" if resolved.free else "paid - billed on your own key"
    print(f"Added '{resolved.name}' (model={resolved.model}, tier={resolved.tier}, {free_note}). Key saved as {env_var}.")
    _pause()


def _remove_agent_menu() -> None:
    custom = SettingsStore().load()
    if not custom:
        print("No custom agents to remove.")
        _pause()
        return
    provider_choice = questionary.select(
        "Remove from which agent?",
        choices=[c.name for c in custom] + ["(cancel)"],
        style=STYLE,
    ).ask()
    if not provider_choice or provider_choice == "(cancel)":
        return

    provider = next(p for p in custom if p.name == provider_choice)
    if len(provider.accounts) == 1:
        if questionary.confirm(f"Remove '{provider.name}' entirely? (its key stays in .env, just unused)", style=STYLE).ask():
            SettingsStore().remove(provider.name)
            print(f"Removed '{provider.name}'.")
        _pause()
        return

    account_choice = questionary.select(
        f"'{provider.name}' has {len(provider.accounts)} accounts - remove which?",
        choices=[a.label for a in provider.accounts] + ["all (remove the whole agent)", "(cancel)"],
        style=STYLE,
    ).ask()
    if not account_choice or account_choice == "(cancel)":
        return
    if account_choice.startswith("all"):
        SettingsStore().remove(provider.name)
        print(f"Removed '{provider.name}' (all accounts).")
    else:
        SettingsStore().remove_account(provider.name, account_choice)
        print(f"Removed account '{account_choice}' from '{provider.name}'.")
    _pause()


def _hosts_menu() -> None:
    while True:
        choice = questionary.select(
            "Local machines (Ollama hosts)",
            choices=[
                "List hosts",
                "Scan network for workers",
                "Add a host",
                "Remove a host",
                "Register THIS PC as a worker",
                "Back",
            ],
            style=STYLE,
        ).ask()
        if choice in (None, "Back"):
            return

        if choice == "List hosts":
            hosts = HostsStore().load()
            if not hosts:
                print("No hosts registered - using env default.")
            else:
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
                    print(f"  {h.name:16} {h.model:22} [{h.size:8}] {h.url:28} ({tag})")
            _pause()

        elif choice == "Scan network for workers":
            source = questionary.select(
                "Scan which way?",
                choices=[
                    questionary.Choice("Local LAN subnet (/24 sweep)", value="lan"),
                    questionary.Choice(
                        "Tailscale peers (any subnet, already encrypted)", value="tailscale"
                    ),
                ],
                style=STYLE,
            ).ask()
            if not source:
                continue

            if source == "tailscale":
                print("\nAsking Tailscale for peers, then probing each for Ollama...")
                found = asyncio.run(network.scan_tailscale_peers())
                if not found and not shutil.which("tailscale"):
                    print("`tailscale` CLI not found - is Tailscale installed and running?")
                    _pause()
                    continue
            else:
                ip = network.local_ip()
                if ip == "127.0.0.1":
                    print("Could not determine this machine's LAN IP - are you connected to a network?")
                    _pause()
                    continue
                subnet = ip.rsplit(".", 1)[0] + ".0/24"
                print(f"\nScanning {subnet} for Ollama workers (port 11434)...")
                found = asyncio.run(network.scan_for_workers())

            if not found:
                print("No Ollama workers found.")
                _pause()
                continue

            existing_urls = {h.url for h in HostsStore().load()}
            for w in found:
                tag = " (already registered)" if w["url"] in existing_urls else ""
                models = ", ".join(w["models"]) or "(no models pulled)"
                print(f"  {w['ip']:16} {models}{tag}")

            candidates = [w for w in found if w["url"] not in existing_urls]
            if not candidates:
                _pause()
                continue
            to_add = questionary.checkbox(
                "Register which of these as hosts?",
                choices=[
                    questionary.Choice(f"{w['ip']} - {', '.join(w['models']) or 'no models'}", value=i)
                    for i, w in enumerate(candidates)
                ],
                style=STYLE,
            ).ask() or []
            for i in to_add:
                w = candidates[i]
                name = questionary.text("Host name:", default=w["ip"].replace(".", "-"), style=STYLE).ask()
                model, needs_pull = _pick_model(w["url"])
                size = questionary.select(
                    "Size (routing hint):", choices=["standard", "small", "big"], style=STYLE
                ).ask()
                if name and model and size:
                    if needs_pull and questionary.confirm(
                        f"'{model}' isn't pulled on {w['url']} yet - pull it now?", default=True, style=STYLE
                    ).ask():
                        print(f"Pulling '{model}' on {w['url']} (this can take a while for a big model)...")
                        if _pull_model_with_progress(w["url"], model):
                            print(f"'{model}' pulled.")
                        else:
                            print(f"! Pull failed - '{model}' may not be usable on {w['url']} yet.")
                    HostsStore().add(HostConfig(name=name, url=w["url"], model=model, size=size))
                    print(f"Registered '{name}' -> {model} @ {w['url']} (size={size})")
                    print(f"Neural handshake complete - '{name}' is online and drift-compatible.")
                    warning = network.connection_warning(w["url"])
                    if warning:
                        print(f"! {warning}")
            _pause()

        elif choice == "Add a host":
            name = questionary.text("Host name (e.g. laptop, desktop-3070):", style=STYLE).ask()
            url = questionary.text("URL (e.g. http://localhost:11434, or a Tailscale IP):", style=STYLE).ask()
            if not (name and url):
                _pause()
                continue
            model, needs_pull = _pick_model(url)
            size = questionary.select(
                "Size (routing hint - short steps prefer 'small', longer ones prefer 'big'; "
                "register the same URL twice under different names/sizes to offer both):",
                choices=["standard", "small", "big"],
                style=STYLE,
            ).ask()
            if name and url and model and size:
                if needs_pull and questionary.confirm(
                    f"'{model}' isn't pulled on {url} yet - pull it now?", default=True, style=STYLE
                ).ask():
                    print(f"Pulling '{model}' on {url} (this can take a while for a big model)...")
                    if _pull_model_with_progress(url, model):
                        print(f"'{model}' pulled.")
                    else:
                        print(f"! Pull failed - '{model}' may not be usable on {url} yet.")
                HostsStore().add(HostConfig(name=name, url=url, model=model, size=size))
                print(f"Registered '{name}' -> {model} @ {url} (size={size})")
                if asyncio.run(network.ollama_reachable(url)):
                    print(f"Neural handshake complete - '{name}' is online and drift-compatible.")
                else:
                    print(f"(Not reachable yet at {url} - fine if that worker just isn't up right now.)")
                warning = network.connection_warning(url)
                if warning:
                    print(f"! {warning}")
            _pause()

        elif choice == "Remove a host":
            hosts = HostsStore().load()
            if not hosts:
                print("No hosts registered.")
                _pause()
                continue
            target = questionary.select(
                "Remove which host?", choices=[h.name for h in hosts] + ["(cancel)"], style=STYLE
            ).ask()
            if target and target != "(cancel)":
                HostsStore().remove(target)
                print(f"Removed '{target}'.")
            _pause()

        elif choice == "Register THIS PC as a worker":
            import socket as _socket

            model = questionary.text("Model you have (or will) pull on THIS machine:", default="qwen2.5-coder:7b", style=STYLE).ask()
            name = questionary.text("Suggested host name:", default=_socket.gethostname().lower(), style=STYLE).ask()
            ip = network.local_ip()

            print("Configuring this machine's networking for worker use...")
            net = network.configure_worker_networking()
            if not net.get("platform_supported", True):
                print("(Skipped - Windows-only. Set OLLAMA_HOST=0.0.0.0 and open the firewall port yourself.)")
            else:
                host_result = net["ollama_host"]
                if host_result == "set":
                    print(
                        "  OLLAMA_HOST=0.0.0.0:11434 saved - Ollama must be RESTARTED to pick this up "
                        "(quit it fully from the tray icon first, then reopen it or re-run `ollama serve`)."
                    )
                elif host_result == "already-set":
                    print("  OLLAMA_HOST already set to 0.0.0.0:11434.")
                else:
                    print(
                        '  ! Could not set OLLAMA_HOST automatically - set it yourself: '
                        '$env:OLLAMA_HOST = "0.0.0.0:11434" (permanently, then restart Ollama).'
                    )

                firewall_result = net["firewall"]
                if firewall_result == "added":
                    print("  Firewall rule added - inbound TCP 11434 now allowed.")
                elif firewall_result == "already-present":
                    print("  Firewall rule already present.")
                else:
                    print(
                        "  ! Firewall rule not added (declined the elevation prompt, or it failed) - add it "
                        'yourself: New-NetFirewallRule -DisplayName "Ollama (11434)" -Direction Inbound '
                        "-Protocol TCP -LocalPort 11434 -Action Allow"
                    )

            path_result = network.ensure_venv_scripts_on_path()
            if path_result == "added":
                print("  Added this CLI to your user PATH - open a NEW terminal to use `orchest`/`orchestcli` from anywhere.")
            elif path_result == "already-on-path":
                print("  This CLI is already on your user PATH.")

            reachable = asyncio.run(network.ollama_reachable())
            print(f"\nThis machine's LAN IP: {ip}")
            print(f"Ollama on :11434 (localhost): {'reachable' if reachable else 'NOT reachable - is `ollama serve` running?'}")
            if net.get("ollama_host") == "set":
                print("(That localhost check can't confirm the new OLLAMA_HOST took effect yet - restart Ollama first, per above.)")

            print(
                "\nMake sure this machine and the supervisor PC are on the same network "
                "(direct Ethernet cable or Tailscale - see README \"Networking two PCs\"), "
                "then on the SUPERVISOR, run:\n"
            )
            print(f"  orchest settings add-host {name} --url http://{ip}:11434 --model {model}")
            print("\nNever port-forward 11434 to the public internet - Ollama has no built-in auth.")
            print(
                "If it's still not reachable after all this: check Settings > Network & Internet > "
                "(your network) > Network profile type - set it to \"Private\", not \"Public\". Windows "
                "silently blocks a lot more than firewall rules alone show on a Public network."
            )

            if network.open_ollama_log_window():
                print(
                    "\nOpened a live Ollama log window - watch it for requests arriving once the "
                    "supervisor registers this machine and starts sending it work."
                )
            else:
                print(
                    f"\n(No live log window - {network.ollama_log_path()} doesn't exist yet. "
                    "Start Ollama at least once, then re-run this to get one.)"
                )
            _pause()


def _cluster_menu() -> None:
    while True:
        choice = questionary.select(
            "Hard-task cluster (llama.cpp RPC across 2+ PCs) - see README "
            '"Running a big model across two PCs" to set the cluster itself up first',
            choices=["List clusters", "Add a cluster", "Remove a cluster", "Back"],
            style=STYLE,
        ).ask()
        if choice in (None, "Back"):
            return

        if choice == "List clusters":
            clusters = ClusterStore().load()
            if not clusters:
                print("No clusters registered.")
            for c in clusters:
                print(f"  {c.name:16} {c.model:28} {c.base_url}")
            _pause()

        elif choice == "Add a cluster":
            name = questionary.text("Cluster name (e.g. big-llama):", style=STYLE).ask()
            url = questionary.text("llama-server URL (e.g. http://localhost:8080/v1):", style=STYLE).ask()
            model = questionary.text("Model name llama-server reports:", style=STYLE).ask()
            if name and url and model:
                ClusterStore().add(ClusterConfig(name=name, base_url=url, model=model))
                print(f"Registered '{name}' -> {model} @ {url}")
            _pause()

        elif choice == "Remove a cluster":
            clusters = ClusterStore().load()
            if not clusters:
                print("No clusters registered.")
                _pause()
                continue
            target = questionary.select(
                "Remove which cluster?", choices=[c.name for c in clusters] + ["(cancel)"], style=STYLE
            ).ask()
            if target and target != "(cancel)":
                ClusterStore().remove(target)
                print(f"Removed '{target}'.")
            _pause()


def _settings_menu() -> None:
    while True:
        choice = questionary.select(
            "Settings",
            choices=[
                "List agents",
                "Add an agent",
                "Remove an agent",
                "Manage local machines (Ollama hosts)",
                "Manage hard-task cluster (llama.cpp RPC)",
                "Local <-> Cloud balance",
                "Back",
            ],
            style=STYLE,
        ).ask()
        if choice in (None, "Back"):
            return
        if choice == "List agents":
            _print_settings_list()
            _pause()
        elif choice == "Add an agent":
            _add_agent_menu()
        elif choice == "Remove an agent":
            _remove_agent_menu()
        elif choice == "Manage local machines (Ollama hosts)":
            _hosts_menu()
        elif choice == "Manage hard-task cluster (llama.cpp RPC)":
            _cluster_menu()
        elif choice == "Local <-> Cloud balance":
            _bias_menu()


def _reset_menu() -> None:
    if questionary.confirm("Clear the shared context/blackboard file?", default=False, style=STYLE).ask():
        ContextStore().reset()
        print("Context cleared.")
    _pause()


def _usage_menu() -> None:
    print(usage_tracker.render())
    print()
    if questionary.confirm("Clear accumulated usage stats?", default=False, style=STYLE).ask():
        usage_tracker.clear()
        print("Usage stats cleared.")
    _pause()


def _dispatch_log_menu() -> None:
    print(dispatch_log.render_recent())
    print()
    if questionary.confirm("Clear the dispatch log?", default=False, style=STYLE).ask():
        dispatch_log.clear()
        print("Dispatch log cleared.")
    _pause()


def main_menu() -> None:
    """Entry point for `orchest` with no subcommand, or `orchest menu`."""
    print(BANNER)
    print()
    while True:
        choice = questionary.select(
            "What do you want to do?",
            choices=[
                "Run a task (plan + execute swarm - read-only)",
                "Code in this folder (edits/writes files - not read-only)",
                "Ask a question (cross-check across models)",
                "View task list (current/last run)",
                "Status",
                "Token usage",
                "Dispatch log (what was sent to which local host)",
                "Settings",
                "Reset shared context",
                "Exit",
            ],
            style=STYLE,
        ).ask()
        if choice in (None, "Exit"):
            print("Bye.")
            return
        if choice.startswith("Run a task"):
            _run_task()
        elif choice.startswith("Code in this folder"):
            _code_task()
        elif choice.startswith("Ask a question"):
            _ask_question()
        elif choice.startswith("View task list"):
            _view_tasks()
        elif choice == "Status":
            _print_status()
            _pause()
        elif choice == "Token usage":
            _usage_menu()
        elif choice.startswith("Dispatch log"):
            _dispatch_log_menu()
        elif choice == "Settings":
            _settings_menu()
        elif choice == "Reset shared context":
            _reset_menu()
