from __future__ import annotations

from dataclasses import dataclass

SIMPLE_HINTS = (
    "boilerplate", "rename", "typo", "format", "small", "simple",
    "trivial", "one-line", "stub", "glue code", "scaffold",
)
COMPLEX_HINTS = (
    "architecture", "design", "plan", "tradeoff", "security",
    "refactor entire", "cross-file", "ambiguous", "concurrency", "migration",
)


@dataclass
class RoutingDecision:
    tier: str  # "local" | "cloud"
    reason: str


def route_step(step_description: str, declared_complexity: str | None = None) -> RoutingDecision:
    """Decide which tier of model should handle a plan step.

    Cheap by design: keyword/word-count heuristics, plus whatever complexity
    hint the planner itself attached to the step ("low"/"high"). This is the
    "scale models based on task size" logic - small/simple steps go to the
    free local pool, anything with real judgment calls stays on cloud models.
    """
    text = step_description.lower()
    words = len(text.split())

    if declared_complexity == "high":
        return RoutingDecision("cloud", "planner flagged this step high-complexity")
    if declared_complexity == "low":
        return RoutingDecision("local", "planner flagged this step low-complexity")

    if any(h in text for h in COMPLEX_HINTS):
        return RoutingDecision("cloud", "matched complexity keyword")
    if any(h in text for h in SIMPLE_HINTS) or words <= 25:
        return RoutingDecision("local", "short/simple step")

    return RoutingDecision("cloud", "default: ambiguous size, escalate")
