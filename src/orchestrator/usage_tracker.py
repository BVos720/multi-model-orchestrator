from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

USAGE_FILE = Path(".orchestrator") / "usage.json"


@dataclass
class UsageRecord:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    # Calls where the provider didn't report usable token counts (e.g. an
    # error before any response, or a provider whose usage-reporting flag
    # itself failed) - counted so the overview doesn't silently understate
    # activity for an agent, without pretending we know its token cost.
    unknown_calls: int = 0


def record(agent_name: str, input_tokens: int | None, output_tokens: int | None, cost_usd: float | None = None) -> None:
    """Log one completed call's usage against `agent_name`. Called as a
    side effect from inside each provider's complete()/raw_chat() - kept
    separate from the Agent.complete() -> str interface so adding this
    didn't require touching every call site across the whole app."""
    data = _load()
    rec = data.setdefault(agent_name, asdict(UsageRecord()))
    rec["calls"] += 1
    if input_tokens is None and output_tokens is None:
        rec["unknown_calls"] += 1
    else:
        rec["input_tokens"] += input_tokens or 0
        rec["output_tokens"] += output_tokens or 0
    if cost_usd:
        rec["cost_usd"] += cost_usd
    _save(data)


def _load() -> dict:
    if not USAGE_FILE.exists():
        return {}
    return json.loads(USAGE_FILE.read_text(encoding="utf-8") or "{}")


def _save(data: dict) -> None:
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_all() -> dict[str, dict]:
    return _load()


def clear() -> None:
    _save({})


def render() -> str:
    data = load_all()
    if not data:
        return "No usage recorded yet."
    lines = [f"  {'agent':16} {'calls':>6} {'input':>10} {'output':>10} {'cost (USD)':>12}"]
    total_calls = total_in = total_out = 0
    total_cost = 0.0
    for name, rec in sorted(data.items(), key=lambda kv: -kv[1].get("input_tokens", 0) - kv[1].get("output_tokens", 0)):
        calls = rec.get("calls", 0)
        input_tokens = rec.get("input_tokens", 0)
        output_tokens = rec.get("output_tokens", 0)
        cost = rec.get("cost_usd", 0.0)
        unknown = rec.get("unknown_calls", 0)
        cost_str = f"${cost:.4f}" if cost else ("-" if not unknown else "?")
        note = f" ({unknown} call(s) w/ unknown usage)" if unknown else ""
        lines.append(f"  {name:16} {calls:>6} {input_tokens:>10} {output_tokens:>10} {cost_str:>12}{note}")
        total_calls += calls
        total_in += input_tokens
        total_out += output_tokens
        total_cost += cost
    lines.append(f"  {'-' * 16} {'-' * 6} {'-' * 10} {'-' * 10} {'-' * 12}")
    lines.append(f"  {'total':16} {total_calls:>6} {total_in:>10} {total_out:>10} ${total_cost:>10.4f}")
    lines.append(
        "\nCost is only shown where a provider actually reports USD (Claude Code does; Ollama is "
        "free; Codex/Copilot/custom API usage is token counts only here - check that provider's own "
        "billing page for $ cost)."
    )
    return "\n".join(lines)
