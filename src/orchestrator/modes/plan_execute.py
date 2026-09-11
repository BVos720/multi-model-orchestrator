from __future__ import annotations

import json
import os
import re

from ..config import Fleet
from ..context_store import ContextStore
from ..hooks import HookRegistry, default_registry
from ..router import DEFAULT_BIAS, route_step
from ..task_list_store import TaskItem, TaskListStore, TaskRun, render

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
    "out anything a local model likely got wrong that needs a human look. "
    "Only flag something as wrong/corrupted/suspicious if you can point to it "
    "in the actual text you were given - never assert a specific bug (a wrong "
    "value, a missing character, a mangled operator) you can't quote from the "
    "material in front of you right now."
)

# Every step-executing agent (local or cloud-fast) gets this - the plan/review
# steps have their own system prompts above, this one is for the actual work.
STEP_SYSTEM = (
    "You are a step-executing agent in a multi-model coding swarm. You've "
    "been handed exactly one step from a larger plan someone else wrote - do "
    "that step, don't expand scope, don't re-plan, don't second-guess the "
    "plan itself. Match the style/conventions of any existing code shown in "
    "the prior work log. State assumptions explicitly rather than inventing "
    "unstated requirements. If you're not sure something is correct, say so "
    "plainly instead of asserting it confidently - a reviewer checks your "
    "output afterward, but only catches what you flag. Do not treat other "
    "agents' prior claims in the work log as verified fact just because "
    "they're written confidently - if something claimed there matters for "
    "your step, re-derive or re-check it yourself rather than repeating it."
)


def _parse_plan(raw: str) -> list[dict]:
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Planner did not return a JSON list:\n{raw[:500]}")
    return json.loads(match.group(0))


async def _complete(agent, prompt: str, decision, fleet: Fleet) -> str:
    """Every step execution gets STEP_SYSTEM; the local pool additionally
    gets a size preference when that's who's answering."""
    if agent is fleet.local and decision.tier == "local":
        return await agent.complete(prompt, system=STEP_SYSTEM, prefer_size=decision.size_hint)
    return await agent.complete(prompt, system=STEP_SYSTEM)


async def run(
    task: str,
    fleet: Fleet,
    store: ContextStore,
    max_steps: int = 12,
    hooks: HookRegistry | None = None,
    local_bias: int | None = None,
    show_task_list: bool = True,
) -> str:
    """The core swarm loop: a heavy model plans, steps get routed to the
    cheapest capable tier (local Ollama pool first), local failures/thin
    output escalate back to a heavy model, and a heavy model reviews
    everything into one final answer. Every step is written to the shared,
    persisted, auto-compacted ContextStore so agents can see each other's work.

    local_bias (0-10): None reads the persisted default (env LOCAL_BIAS,
    settable via `orchestrator settings bias` / the menu's balance screen).
    The plan itself is mirrored into a persisted, live-updated TaskRun
    (.orchestrator/tasks.json) - this *is* the "models keep a task list and
    divide work among themselves" list, printed as a checklist as it runs.
    """
    if local_bias is None:
        local_bias = int(os.environ.get("LOCAL_BIAS", str(DEFAULT_BIAS)))

    hooks = hooks or default_registry()
    planner = fleet.planners[0]
    tasks = TaskListStore()

    plan_raw = await planner.complete(task, system=PLAN_SYSTEM)
    store.add(planner.name, "plan", plan_raw)
    steps = _parse_plan(plan_raw)[:max_steps]

    task_run = TaskRun(
        task=task,
        items=[
            TaskItem(id=i, description=s.get("step", ""), complexity=s.get("complexity"))
            for i, s in enumerate(steps, 1)
        ],
    )
    tasks.save(task_run)
    if show_task_list:
        print(render(task_run))

    for i, step in enumerate(steps, 1):
        item = task_run.items[i - 1]
        desc = step.get("step", "")
        decision = route_step(desc, step.get("complexity"), local_bias=local_bias)
        item.tier = decision.tier
        item.status = "running"
        tasks.save(task_run)
        if show_task_list:
            print(render(task_run))

        await hooks.run_pre_step(desc)

        if decision.tier == "local" and fleet.local:
            agent = fleet.local
        elif decision.tier == "cloud-fast" and fleet.cloud_fast:
            agent = fleet.cloud_fast[0]
        else:
            agent = planner
        # "fallback" = we wanted a cheaper tier but had to use the strong
        # planner anyway because that tier isn't configured (distinct from
        # an *escalation*, which happens below on failure/thin output).
        used_fallback = agent is planner and decision.tier != "cloud"

        step_prompt = (
            f"Task: {task}\n\nFull plan: {json.dumps(steps)}\n\n"
            f"Prior work so far:\n{store.transcript()}\n\n"
            f"Now do step {i}: {desc}"
        )
        try:
            result = await _complete(agent, step_prompt, decision, fleet)
        except Exception:
            if agent is not planner:
                agent = planner  # local/cloud-fast unreachable or unhealthy -> escalate
                result = await _complete(agent, step_prompt, decision, fleet)
                used_fallback = True
            else:
                item.status = "failed"
                tasks.save(task_run)
                raise

        result = await hooks.run_post_step(desc, decision.tier, result)
        if "ORCHESTRATOR_ESCALATE" in result and agent is not planner:
            agent = planner
            result = await _complete(agent, step_prompt, decision, fleet)
            used_fallback = True

        tag = f"{decision.tier}{'->escalated' if used_fallback else ''}"
        store.add(agent.name, f"step-{i}:{tag}", result)

        item.status = "escalated" if used_fallback else "done"
        item.assigned_agent = agent.name
        item.result_preview = result[:200]
        tasks.save(task_run)
        if show_task_list:
            print(render(task_run))

        if fleet.local and store.needs_compaction():
            await store.compact(summarizer=fleet.local)

    task_run.finished = True
    tasks.save(task_run)

    final_prompt = f"Task: {task}\n\nWork log:\n{store.transcript()}"
    final = await planner.complete(final_prompt, system=REVIEW_SYSTEM)
    store.add(planner.name, "review", final)
    return final
