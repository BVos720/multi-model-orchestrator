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


DEFAULT_BIAS = 5  # 0 = always favor cloud/precision, 10 = always favor local/savings


def route_step(
    step_description: str,
    declared_complexity: str | None = None,
    local_bias: int = DEFAULT_BIAS,
) -> RoutingDecision:
    """Decide which tier of model should handle a plan step.

    Cheap by design: keyword/word-count heuristics, plus whatever complexity
    hint the planner itself attached to the step ("low"/"high"). This is the
    "scale models based on task size" logic - small/simple steps go to the
    free local pool, anything with real judgment calls stays on cloud models.

    local_bias (0-10, default 5/balanced) is the "slider": it widens or
    narrows what counts as "simple enough for local" - see settings.bias in
    cli.py / the menu's Local<->Cloud balance screen. It's a bias, not an
    override of every safety net: a security-flagged step still escalates
    unless bias is maxed out, and plan_execute still escalates on thin/failed
    local output regardless of bias.
    """
    local_bias = max(0, min(10, local_bias))
    text = step_description.lower()
    words = len(text.split())
    hit_complex = any(h in text for h in COMPLEX_HINTS)
    hit_simple = any(h in text for h in SIMPLE_HINTS)

    if "security" in text and local_bias < 10:
        return RoutingDecision("cloud", "security-sensitive step - escalated regardless of bias")

    # word-count threshold scales with bias: wider at high bias, narrower at low
    word_threshold = max(5, 25 + (local_bias - DEFAULT_BIAS) * 5)

    if local_bias <= 1:  # max precision: only the most trivial steps go local
        if hit_simple and not hit_complex and words <= 8:
            return RoutingDecision("local", f"bias={local_bias}: trivial even at max-precision bias")
        return RoutingDecision("cloud", f"bias={local_bias}: max-precision - escalate by default")

    if local_bias >= 9:  # max savings: try local unless a hard complexity keyword hit
        if hit_complex:
            return RoutingDecision("cloud", f"bias={local_bias}: matched complexity keyword, still escalating")
        return RoutingDecision("local", f"bias={local_bias}: max-savings - try local by default")

    if declared_complexity == "high":
        return RoutingDecision("cloud", "planner flagged this step high-complexity")
    if declared_complexity == "low":
        return RoutingDecision("local", "planner flagged this step low-complexity")

    if hit_complex:
        return RoutingDecision("cloud", "matched complexity keyword")
    if hit_simple or words <= word_threshold:
        return RoutingDecision("local", f"short/simple step (<= {word_threshold} words, bias={local_bias})")

    return RoutingDecision("cloud", f"default: ambiguous size, escalate (bias={local_bias})")
