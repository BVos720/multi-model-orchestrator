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

# Substrings (lowercased) that show up across providers when a model/account
# is genuinely out of capacity - as opposed to a transient network blip or a
# real bug. Deliberately over-inclusive: a false positive here just offers a
# switch the user can decline (or that auto-picks a reasonable fallback);
# a false negative crashes the whole run instead, which is worse.
QUOTA_ERROR_HINTS = (
    "usage limit", "rate limit", "quota", "ineligibletier", "429",
    "insufficient credit", "exceeded your current quota", "billing",
    "resource_exhausted", "out of capacity", "credit balance",
)


def _is_quota_error(exc: Exception) -> bool:
    return any(hint in str(exc).lower() for hint in QUOTA_ERROR_HINTS)


def _parse_plan(raw: str) -> list[dict]:
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Planner did not return a JSON list:\n{raw[:500]}")
    return json.loads(match.group(0))


def _first_choice(decision_tier: str, fleet: Fleet, planner) -> tuple:
    """Map a routing decision to (agent, dispatch_tier). dispatch_tier is
    finer than decision_tier - it's the only thing that distinguishes a
    free local-hard cluster answer from a real planner answer, since both
    can serve decision_tier == "cloud" (the cluster is tried first, no
    subscription/API cost, before spending the planner's budget)."""
    if decision_tier == "local" and fleet.local:
        return fleet.local, "local"
    if decision_tier == "cloud-fast" and fleet.cloud_fast:
        return fleet.cloud_fast[0], "cloud-fast"
    if decision_tier == "cloud" and fleet.local_hard:
        return fleet.local_hard[0], "cloud-hard"
    return planner, "cloud"


async def run(
    task: str,
    fleet: Fleet,
    store: ContextStore,
    max_steps: int = 12,
    hooks: HookRegistry | None = None,
    local_bias: int | None = None,
    show_task_list: bool = True,
    on_exhausted=None,
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

    on_exhausted: optional async callback(exhausted_agent, error, candidates)
    -> Agent | None, called when the planner hits what looks like a quota/
    usage-limit error (not just any failure). Return the Agent to continue
    with, or None to abort. If omitted, auto-picks the first candidate
    (another planner, then cloud-fast, then local) and prints what it chose
    - this is what makes a `run` that outlives one model's quota rather than
    just crashing. The swap is sticky: once switched, later steps/the final
    review use the new planner too, not just the one call that failed.
    """
    if local_bias is None:
        local_bias = int(os.environ.get("LOCAL_BIAS", str(DEFAULT_BIAS)))

    hooks = hooks or default_registry()
    planner = fleet.planners[0]
    tasks = TaskListStore()

    async def call_planner(prompt: str, system: str) -> tuple[str, object]:
        """Try the current planner; on a quota-shaped error, swap to a
        fallback (offered via on_exhausted, or auto-picked) and retry.
        Returns (result, agent_that_actually_answered)."""
        nonlocal planner
        try:
            return await planner.complete(prompt, system=system), planner
        except Exception as e:
            if not _is_quota_error(e):
                raise
            candidates = [a for a in fleet.planners if a is not planner]
            candidates += list(fleet.cloud_fast)
            if fleet.local:
                candidates.append(fleet.local)

            if on_exhausted:
                choice = await on_exhausted(planner, e, candidates)
            elif candidates:
                choice = candidates[0]
                print(f"! {planner.name} looks out of capacity ({e}) - continuing with {choice.name}.")
            else:
                choice = None

            if choice is None:
                raise
            planner = choice
            return await planner.complete(prompt, system=system), planner

    plan_raw, _ = await call_planner(task, PLAN_SYSTEM)
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

        agent, dispatch_tier = _first_choice(decision.tier, fleet, planner)
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
            if agent is fleet.local and decision.tier == "local":
                result = await agent.complete(step_prompt, system=STEP_SYSTEM, prefer_size=decision.size_hint)
            else:
                result = await agent.complete(step_prompt, system=STEP_SYSTEM)
        except Exception:
            if agent is not planner:
                result, agent = await call_planner(step_prompt, STEP_SYSTEM)  # local/cloud-fast down -> escalate
                used_fallback = True
            else:
                item.status = "failed"
                tasks.save(task_run)
                raise

        result = await hooks.run_post_step(desc, dispatch_tier, result)
        if "ORCHESTRATOR_ESCALATE" in result and agent is not planner:
            result, agent = await call_planner(step_prompt, STEP_SYSTEM)
            used_fallback = True

        tag = f"{dispatch_tier}{'->escalated' if used_fallback else ''}"
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
    final, _ = await call_planner(final_prompt, REVIEW_SYSTEM)
    store.add(planner.name, "review", final)
    return final
