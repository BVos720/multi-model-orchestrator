from __future__ import annotations

import json
import re

from ..config import Fleet
from ..context_store import ContextStore
from ..hooks import HookRegistry, default_registry
from ..router import route_step

PLAN_SYSTEM = (
    "You are the planning agent in a multi-model coding swarm. Break the "
    "user's task into a short ordered list of concrete steps. Return ONLY "
    'JSON: a list of objects {"step": <str>, "complexity": "low"|"high"}. '
    'Mark a step "low" only if a small local coding model (7B-32B-ish) could '
    "reliably do it alone (boilerplate, a single small function, formatting, "
    'renames, simple glue code). Mark it "high" for anything needing real '
    "judgment, architecture decisions, cross-file reasoning, or security care. "
    "No prose outside the JSON."
)

REVIEW_SYSTEM = (
    "You are the reviewing agent in a multi-model coding swarm. Below is the "
    "plan and every step's output from the agents that executed it. Produce "
    "the final, consolidated answer for the user: merge the step outputs "
    "into one coherent result, fix any inconsistency between steps, and call "
    "out anything a local model likely got wrong that needs a human look."
)


def _parse_plan(raw: str) -> list[dict]:
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Planner did not return a JSON list:\n{raw[:500]}")
    return json.loads(match.group(0))


async def run(
    task: str,
    fleet: Fleet,
    store: ContextStore,
    max_steps: int = 12,
    hooks: HookRegistry | None = None,
) -> str:
    """The core swarm loop: a heavy model plans, steps get routed to the
    cheapest capable tier (local Ollama pool first), local failures/thin
    output escalate back to a heavy model, and a heavy model reviews
    everything into one final answer. Every step is written to the shared,
    persisted, auto-compacted ContextStore so agents can see each other's work.
    """
    hooks = hooks or default_registry()
    planner = fleet.planners[0]

    plan_raw = await planner.complete(task, system=PLAN_SYSTEM)
    store.add(planner.name, "plan", plan_raw)
    steps = _parse_plan(plan_raw)[:max_steps]

    for i, step in enumerate(steps, 1):
        desc = step.get("step", "")
        decision = route_step(desc, step.get("complexity"))
        await hooks.run_pre_step(desc)

        agent = fleet.local if decision.tier == "local" and fleet.local else planner
        used_fallback = agent is planner and decision.tier == "local"

        step_prompt = (
            f"Task: {task}\n\nFull plan: {json.dumps(steps)}\n\n"
            f"Prior work so far:\n{store.transcript()}\n\n"
            f"Now do step {i}: {desc}"
        )
        try:
            result = await agent.complete(step_prompt)
        except Exception:
            if agent is not planner:
                agent = planner  # local model unreachable/failed -> escalate
                result = await agent.complete(step_prompt)
                used_fallback = True
            else:
                raise

        result = await hooks.run_post_step(desc, decision.tier, result)
        if "ORCHESTRATOR_ESCALATE" in result and agent is not planner:
            agent = planner
            result = await agent.complete(step_prompt)
            used_fallback = True

        tag = f"{decision.tier}{'->escalated' if used_fallback else ''}"
        store.add(agent.name, f"step-{i}:{tag}", result)

        if fleet.local and store.needs_compaction():
            await store.compact(summarizer=fleet.local)

    final_prompt = f"Task: {task}\n\nWork log:\n{store.transcript()}"
    final = await planner.complete(final_prompt, system=REVIEW_SYSTEM)
    store.add(planner.name, "review", final)
    return final
