from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

# Borrowed concept from claude-flow's hooks system, reimplemented minimal:
# a few named extension points the swarm calls around each step, so you can
# plug in things like "run the linter after a local-model coding step" or
# "escalate automatically if the step output looks broken" without touching
# the core plan/execute loop.

PreStepHook = Callable[[str], Awaitable[None]]          # (step_description) -> None
PostStepHook = Callable[[str, str, str], Awaitable[str]]  # (step_desc, tier, output) -> possibly-modified output


@dataclass
class HookRegistry:
    pre_step: list[PreStepHook] = field(default_factory=list)
    post_step: list[PostStepHook] = field(default_factory=list)

    def on_pre_step(self, fn: PreStepHook) -> PreStepHook:
        self.pre_step.append(fn)
        return fn

    def on_post_step(self, fn: PostStepHook) -> PostStepHook:
        self.post_step.append(fn)
        return fn

    async def run_pre_step(self, step_description: str) -> None:
        for fn in self.pre_step:
            await fn(step_description)

    async def run_post_step(self, step_description: str, tier: str, output: str) -> str:
        for fn in self.post_step:
            output = await fn(step_description, tier, output)
        return output


def default_registry() -> HookRegistry:
    """A registry with one built-in safety hook: flag suspiciously short or
    empty local-model output so plan_execute can escalate it."""
    registry = HookRegistry()

    async def flag_thin_output(step_description: str, tier: str, output: str) -> str:
        if tier == "local" and len(output.strip()) < 10:
            return f"[ORCHESTRATOR_ESCALATE: local output too thin] original: {output!r}"
        return output

    registry.on_post_step(flag_thin_output)
    return registry
