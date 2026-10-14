"""Per-call cost for the replay scripts, by model and prompt-size tier.

Rates are USD per million tokens as (input, output), with a higher tier for prompts
over 100K tokens. Only models whose rates were checked are listed; any other model
reports no cost rather than a wrong one. Confirm against Anthropic's pricing page
before adding a model.
"""

TIER_TOKENS = 100_000
PRICES = {
    # Haiku 5.5, 2026-10 (secondary source; see compendium IMPR-1311 § Sources).
    "claude-haiku-5-5": {"base": (0.10, 0.50), "long": (0.50, 2.50)},
}


def call_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    rates = PRICES.get(model)
    if rates is None:
        return None
    rin, rout = rates["long" if input_tokens > TIER_TOKENS else "base"]
    return (input_tokens * rin + output_tokens * rout) / 1e6


def run_cost(model: str, calls: list[tuple[int, int]]) -> float | None:
    costs = [call_cost(model, i, o) for i, o in calls]
    return None if any(c is None for c in costs) else sum(costs)
