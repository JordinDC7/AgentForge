"""Smart cost router. Routes each task to the cheapest provider that can handle it.

This is the core innovation: instead of using one expensive model for everything,
we match task complexity to the cheapest capable provider. 80% of work should
happen at $0 (Gemini free tier) or near-$0 (Codex subscription).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from providers.base import BaseProvider, Capability, CostTier


class TaskComplexity(Enum):
    """How hard is this task? Determines minimum provider capability."""
    TRIVIAL = "trivial"    # Boilerplate, simple edits, formatting
    SIMPLE = "simple"      # Write tests, docs, single-file changes
    MEDIUM = "medium"      # Multi-file impl, API endpoints, components
    HARD = "hard"          # Architecture, complex bugs, refactoring
    EXPERT = "expert"      # System design, security review, optimization


# Map task types to required capabilities and minimum complexity
TASK_TYPE_REQUIREMENTS: dict[str, tuple[list[Capability], TaskComplexity]] = {
    "architecture": ([Capability.ARCHITECTURE, Capability.LONG_CONTEXT], TaskComplexity.EXPERT),
    "backend": ([Capability.CODE_GENERATION, Capability.MULTI_FILE], TaskComplexity.MEDIUM),
    "frontend": ([Capability.CODE_GENERATION, Capability.MULTI_FILE], TaskComplexity.MEDIUM),
    "testing": ([Capability.TESTING, Capability.CODE_GENERATION], TaskComplexity.SIMPLE),
    "review": ([Capability.CODE_REVIEW], TaskComplexity.HARD),
    "docs": ([Capability.DOCUMENTATION], TaskComplexity.TRIVIAL),
    "refactor": ([Capability.REFACTORING, Capability.MULTI_FILE], TaskComplexity.HARD),
    "debug": ([Capability.DEBUGGING], TaskComplexity.HARD),
    "research": ([Capability.WEB_SEARCH], TaskComplexity.SIMPLE),
}

# Minimum accuracy score for each complexity level
COMPLEXITY_MIN_ACCURACY: dict[TaskComplexity, float] = {
    TaskComplexity.TRIVIAL: 0.20,
    TaskComplexity.SIMPLE: 0.35,
    TaskComplexity.MEDIUM: 0.50,
    TaskComplexity.HARD: 0.65,
    TaskComplexity.EXPERT: 0.75,
}


@dataclass
class RouteDecision:
    """The router's decision on which provider to use."""
    provider: BaseProvider
    reason: str
    estimated_cost: float  # USD for this task
    fallback: Optional[BaseProvider] = None  # Escalation target if this fails


class CostRouter:
    """Routes tasks to the cheapest capable provider.
    
    Algorithm:
    1. Determine task type → required capabilities + minimum complexity
    2. Filter providers that have all required capabilities
    3. Filter providers that meet minimum accuracy threshold
    4. Sort remaining by cost (ascending)
    5. Pick the cheapest one
    6. Set the next-cheapest-but-more-capable as fallback for escalation
    """

    def __init__(self, providers: list[BaseProvider], budget_remaining: float = float("inf")):
        self.providers = providers
        self.budget_remaining = budget_remaining

    def route(
        self,
        task_type: str,
        complexity_override: Optional[TaskComplexity] = None,
        preferred_provider: Optional[str] = None,
        estimated_duration_minutes: float = 30.0,
        failure_history: Optional[dict[str, int]] = None,
    ) -> RouteDecision:
        """Route a task to the best provider.

        Args:
            task_type: One of the keys in TASK_TYPE_REQUIREMENTS
            complexity_override: Force a specific complexity level
            preferred_provider: User override — use this provider if available
            estimated_duration_minutes: How long we expect this to take
            failure_history: Dict of "task_type:provider" → failure count from memory
        """
        # Get requirements for this task type
        if task_type in TASK_TYPE_REQUIREMENTS:
            required_caps, default_complexity = TASK_TYPE_REQUIREMENTS[task_type]
        else:
            required_caps = [Capability.CODE_GENERATION]
            default_complexity = TaskComplexity.MEDIUM

        complexity = complexity_override or default_complexity
        min_accuracy = COMPLEXITY_MIN_ACCURACY[complexity]

        # Build set of providers to avoid based on failure history
        # If a provider has failed 2+ times on this task type, skip it
        failed_providers = set()
        if failure_history:
            for key, count in failure_history.items():
                parts = key.split(":", 1)
                if len(parts) == 2 and parts[0] == task_type and count >= 2:
                    failed_providers.add(parts[1])

        # User override — just use what they asked for
        if preferred_provider:
            for p in self.providers:
                if p.name == preferred_provider:
                    cost = p.estimate_cost(estimated_duration_minutes * 60)
                    return RouteDecision(
                        provider=p,
                        reason=f"User preferred provider: {preferred_provider}",
                        estimated_cost=cost,
                    )

        # Filter: has capabilities AND meets accuracy threshold
        candidates = [
            p for p in self.providers
            if p.can_handle(required_caps) and p.config.accuracy_score >= min_accuracy
        ]

        if not candidates:
            # Relax accuracy requirement and try again
            candidates = [p for p in self.providers if p.can_handle(required_caps)]

        if not candidates:
            # Last resort: just pick the most capable provider
            candidates = sorted(self.providers, key=lambda p: p.config.accuracy_score, reverse=True)

        if not candidates:
            raise RuntimeError("No providers available. Run 'forge providers setup' first.")

        # Sort by cost tier, then by cost_per_hour
        tier_order = {CostTier.FREE: 0, CostTier.SUBSCRIPTION: 1, CostTier.LOW: 2, CostTier.MEDIUM: 3, CostTier.HIGH: 4}
        candidates.sort(key=lambda p: (tier_order.get(p.config.cost_tier, 99), p.config.cost_per_hour_usd))

        # Deprioritize providers that have failed repeatedly on this task type
        # Move them to the end rather than removing (they're still a last resort)
        if failed_providers:
            good = [p for p in candidates if p.name not in failed_providers]
            bad = [p for p in candidates if p.name in failed_providers]
            if good:
                candidates = good + bad

        # Pick cheapest
        chosen = candidates[0]
        cost = chosen.estimate_cost(estimated_duration_minutes * 60)

        # Set fallback (next provider that's more accurate)
        fallback = None
        more_capable = [p for p in candidates[1:] if p.config.accuracy_score > chosen.config.accuracy_score]
        if more_capable:
            fallback = more_capable[0]

        # Budget check
        if cost > self.budget_remaining and len(candidates) > 1:
            # Try to find something cheaper
            for p in candidates:
                if p.estimate_cost(estimated_duration_minutes * 60) <= self.budget_remaining:
                    chosen = p
                    cost = p.estimate_cost(estimated_duration_minutes * 60)
                    break

        reason = (
            f"Routed to {chosen.name} ({chosen.config.cost_tier.value}): "
            f"accuracy={chosen.config.accuracy_score}, "
            f"cost=~${cost:.2f}, "
            f"complexity={complexity.value}"
        )
        if chosen.name in failed_providers:
            reason += " (WARNING: this provider has failed before on this task type)"

        return RouteDecision(
            provider=chosen,
            reason=reason,
            estimated_cost=cost,
            fallback=fallback,
        )

    def get_escalation_chain(self, task_type: str) -> list[BaseProvider]:
        """Get the full escalation chain for a task type, cheapest to most expensive.
        
        This is what happens when agents get stuck:
        Gemini (free) → Codex ($sub) → Aider+Sonnet → Claude Sonnet → Claude Opus
        """
        if task_type in TASK_TYPE_REQUIREMENTS:
            required_caps, _ = TASK_TYPE_REQUIREMENTS[task_type]
        else:
            required_caps = [Capability.CODE_GENERATION]

        capable = [p for p in self.providers if p.can_handle(required_caps)]
        tier_order = {CostTier.FREE: 0, CostTier.SUBSCRIPTION: 1, CostTier.LOW: 2, CostTier.MEDIUM: 3, CostTier.HIGH: 4}
        capable.sort(key=lambda p: (tier_order.get(p.config.cost_tier, 99), p.config.cost_per_hour_usd))
        return capable

    def estimate_total_cost(self, task_plan: list[dict]) -> float:
        """Estimate total cost for a full task plan.
        
        Args:
            task_plan: List of {"type": str, "duration_minutes": float}
        """
        total = 0.0
        for task in task_plan:
            decision = self.route(
                task_type=task.get("type", "backend"),
                estimated_duration_minutes=task.get("duration_minutes", 30),
            )
            total += decision.estimated_cost
        return total

    def print_routing_table(self, task_types: Optional[list[str]] = None):
        """Print a table showing how each task type would be routed."""
        types = task_types or list(TASK_TYPE_REQUIREMENTS.keys())
        print(f"\n{'Task Type':<15} {'Provider':<15} {'Cost Tier':<12} {'~Cost':<10} {'Fallback':<15}")
        print("─" * 67)
        for tt in types:
            try:
                decision = self.route(tt)
                fallback_name = decision.fallback.name if decision.fallback else "—"
                print(
                    f"{tt:<15} {decision.provider.name:<15} "
                    f"{decision.provider.config.cost_tier.value:<12} "
                    f"${decision.estimated_cost:<9.2f} {fallback_name:<15}"
                )
            except Exception as e:
                print(f"{tt:<15} ERROR: {e}")
