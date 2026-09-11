from __future__ import annotations

import asyncio

from ..config import Fleet
from ..context_store import ContextStore

JUDGE_SYSTEM = (
    "Multiple models independently answered the same question. Compare their "
    "answers, keep what they agree on, resolve disagreements using your own "
    "judgment, and give one final best answer. Note any point where the "
    "models meaningfully disagreed."
)


async def run(question: str, fleet: Fleet, store: ContextStore) -> str:
    """Cross-check mode: ask every configured cloud/planner model the same
    question in parallel, then have one of them judge/merge into a verdict."""
    panel = fleet.planners + fleet.cloud
    if len(panel) < 2:
        raise RuntimeError("Debate mode needs at least 2 cloud/planner agents configured.")

    answers = await asyncio.gather(*(a.complete(question) for a in panel), return_exceptions=True)

    bundle_parts = []
    for agent, answer in zip(panel, answers):
        if isinstance(answer, Exception):
            store.add(agent.name, "debate-error", str(answer))
            continue
        store.add(agent.name, "debate-answer", answer)
        bundle_parts.append(f"=== {agent.name} ===\n{answer}")

    if not bundle_parts:
        raise RuntimeError("Every panel agent failed - see .orchestrator/context.json for errors.")

    judge = panel[0]
    verdict = await judge.complete(
        f"Question: {question}\n\n" + "\n\n".join(bundle_parts), system=JUDGE_SYSTEM
    )
    store.add(judge.name, "debate-verdict", verdict)
    return verdict
