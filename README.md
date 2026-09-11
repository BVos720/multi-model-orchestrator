# multi-model-orchestrator

A swarm that gets Claude Code, Gemini, a local Ollama pool, and (optionally) any
OpenAI-compatible model working on one task together - instead of you copy-pasting
between chat windows.

## How it works

```
            task
             |
             v
      +-------------+
      |   PLANNER   |   (Claude Code and/or Gemini - "heavy" tier)
      | writes plan |   Breaks the task into steps, tags each low/high complexity.
      +-------------+
             |
             v
   for each step -----------------------------------------------+
       |                                                         |
       v                                                         |
   route_step()  --low complexity-->  OLLAMA POOL (local, free)  |
       |                               one Ollama host per PC,   |
       |                               round-robins & fails over |
       |                                        |                |
       |                            thin/failed output? --escalate--+
       |                                                         |
       +--high complexity------------------->  PLANNER (cloud)   |
                                                                  |
   every step's output is appended to the shared, persisted <----+
   context store (.orchestrator/context.json), so later steps
   and the final review see everything that happened before.
             |
             v
      +-------------+
      |   REVIEWER  |   (a heavy model) merges every step's output into
      |             |   one final, consistent answer.
      +-------------+
```

The context store auto-**compacts**: once the shared log passes ~24k characters,
everything except the most recent entries gets summarized (by the local model,
so summarizing doesn't cost cloud tokens) into one entry. It persists to
`.orchestrator/context.json` so a run survives a crash and you can inspect
exactly what each agent said and did.

## Modes

- `orchestrator run "<task>"` - plan/execute/review swarm (the diagram above).
  Best for coding tasks: local model does the boilerplate, cloud models do the
  parts that need judgment.
- `orchestrator ask "<question>"` - debate mode: every configured cloud/planner
  model answers independently, then one of them merges/judges a final verdict.
  Good for cross-checking factual or design questions.
- `orchestrator status` - see which agents are actually configured and ready.
- `orchestrator reset` - clear the shared context file and start fresh.

## Setup

**Automated (recommended):**

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

```bash
# macOS/Linux
./scripts/setup.sh
```

This installs Python if missing, creates a `.venv`, installs the package,
checks for the `claude` CLI, copies `.env.example` -> `.env`, installs Ollama
and pulls the default coding model. It checks free disk space first and warns
before pulling a large model.

**Manual:**

1. `python -m venv .venv && .venv\Scripts\pip install -e .`
2. `cp .env.example .env` and fill in `GEMINI_API_KEY` (free:
   https://aistudio.google.com/apikey - note this is separate from a Gemini
   Advanced/Pro consumer subscription).
3. Make sure `claude` is on PATH and you're logged in (`claude` once,
   interactively).
4. Install Ollama (https://ollama.com) and `ollama pull qwen2.5-coder:32b`
   (or a smaller tag - see note below).
5. `orchestrator status` to confirm what's wired up.

### Local model size

`qwen2.5-coder:32b` is the default - a strong coding model, but a ~20GB
download that wants 24GB+ RAM/VRAM to run comfortably. If that doesn't fit
your machine, set `OLLAMA_MODEL=qwen2.5-coder:7b` (~5GB) in `.env` instead.

## Networking two PCs: Supervisor + Worker(s)

You don't need a second copy of this project on the second PC. One machine
is the **Supervisor** - it's the one where you run `orchestrator`, and it's
the only one that ever talks to the cloud models (Claude Code, Gemini,
DeepSeek, ...). Any other PC is a **Worker** - all it needs is Ollama itself
running, nothing else installed. The Supervisor dispatches "local complexity"
plan steps to whichever registered Worker (or itself) is free - this is what
`orchestrator settings add-host` + `HostsStore` already builds: it's not a
separate mode, it's just how you use hosts.json once you register more than
one machine. That's the "build your own scalable computer" part - add a
Worker, register it, the pool gets bigger.

**Do this, in order:**

1. **Network the machines.** Two options:
   - **Direct Ethernet cable** between the two PCs (fastest, lowest latency,
     and inherently private - nothing else is on that link): give each NIC a
     static IP (e.g. `192.168.50.1` / `192.168.50.2`) in Windows' adapter
     settings.
   - **Over WiFi/wider network**: install [Tailscale](https://tailscale.com)
     on both PCs, same account. This gives each machine a private,
     WireGuard-encrypted IP (100.x.y.z) that only your devices can reach.
2. On the Worker PC(s), just run `ollama serve` (or let the Ollama app run
   in the background) - it listens on `11434` by default. Nothing else to
   install there.
3. **Never port-forward 11434 on your router.** Ollama has no built-in
   authentication - anyone who can reach that port can run arbitrary prompts
   on that GPU. A direct cable or Tailscale keeps it private to your own
   devices; that's the whole security model here.
4. On the Supervisor, register each machine by name:
   ```
   orchestrator settings add-host laptop      --url http://localhost:11434     --model qwen2.5-coder:7b
   orchestrator settings add-host desktop-3070 --url http://192.168.50.2:11434 --model qwen2.5-coder:14b
   ```
   `orchestrator run` will prompt you at startup to pick which registered
   host(s) handle local steps for that run (or pass `--local desktop-3070`,
   `--local all`, etc.) - see `orchestrator settings hosts`.
   `OllamaAgent` round-robins across whichever hosts you pick and skips one
   that's unreachable or errors, so one PC being off just means the other
   picks up the work.

If you'll have more than just your own devices on the network (e.g. a
shared WiFi with other people), put [Caddy](https://caddyserver.com) or
`nginx` in front of each Worker's Ollama instance as a reverse proxy that
checks a shared-secret header - Ollama itself won't do that for you, and a
direct cable/Tailscale alone assumes only your two PCs are on that link.

## Providers

| Provider | Tier | Needs | Notes |
|---|---|---|---|
| `claude-code` | planner | `claude` CLI on PATH, logged in | Shells out to `claude -p`, runs with `--permission-mode plan` and every mutating tool disallowed - it only ever generates text here, never edits files itself. |
| `gemini` | planner | `GEMINI_API_KEY` | Google AI Studio, free tier. |
| `openai` | cloud | `OPENAI_API_KEY` | Any OpenAI-compatible endpoint - OpenAI, Groq, OpenRouter, etc. This is the "some other model" slot. Skipped entirely if unset. |
| `ollama` | local | Ollama running, model pulled | Free, local, pooled across `OLLAMA_HOSTS`. |

Add another provider by implementing `Agent` in `src/orchestrator/providers/`
(one `async def complete(prompt, system=None) -> str` method) and wiring it
into `Fleet` in `config.py`.

## On claude-flow / "ruflo"

If you've used `claude-flow`/`ruflo` before: this project intentionally does
**not** depend on it. It's Claude-only (no Gemini/Ollama), and the installed
copy on this machine ships marketing/telemetry files alongside the actual
tool (`funnel-rotation.json`, `statusline-promo.json`, etc.) plus a
self-inserted global `CLAUDE.md` instruction pushing its own tools by
default - which is why that block was removed from
`~/.claude/CLAUDE.md`. A few of its genuinely useful *ideas* are reimplemented
here cleanly instead:

- **topology / routing table** - `router.py`'s complexity-based routing is a
  simplified version of its "route task type -> agent tier" concept.
- **hooks** - `hooks.py` is a minimal pre/post-step hook registry, same idea
  as its hooks system, without the daemon/neural-training baggage.
- **scoped memory** - `context_store.py`'s persisted, compacted blackboard is
  the same shape as its per-project memory concept, without the vector-DB
  machinery this project doesn't need.

## Project layout

```
src/orchestrator/
  providers/       one file per model backend, all implementing Agent
  modes/           plan_execute.py (swarm), debate.py (cross-check)
  router.py        complexity -> tier routing
  hooks.py         pre/post-step extension points
  context_store.py shared, persisted, auto-compacting blackboard
  config.py        builds the Fleet from what's actually available
  cli.py           `orchestrator run|ask|status|reset`
scripts/
  setup.ps1        automated setup (Windows)
  setup.sh         automated setup (macOS/Linux)
```
