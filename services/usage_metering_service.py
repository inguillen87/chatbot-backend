import logging
from datetime import datetime, timezone
from extensions import db
from models_analytics_k import AIUsageLog, TenantBudget
from schemas.ai_contracts import UsageMetrics

logger = logging.getLogger(__name__)

class UsageMeteringService:
    def __init__(self, cost_per_1k_input: float = 0.0015, cost_per_1k_output: float = 0.002):
        self.cost_per_1k_input = cost_per_1k_input
        self.cost_per_1k_output = cost_per_1k_output

    def _calculate_estimated_cost(self, metrics: UsageMetrics) -> float:
        input_cost = (metrics.prompt_tokens / 1000) * self.cost_per_1k_input
        output_cost = (metrics.completion_tokens / 1000) * self.cost_per_1k_output
        return input_cost + output_cost

    def record_usage(
        self,
        tenant_id: int,
        channel: str,
        actor_type: str,
        actor_id: str,
        request_id: str,
        model: str,
        metrics: UsageMetrics,
        latency_ms: int,
        status: str,
        prompt_version: str = None,
        tool_names: str = None
    ) -> AIUsageLog:
        """
        Records the AI usage into the central ledger and attempts to deduct from budget.
        """
        estimated_cost = self._calculate_estimated_cost(metrics)

        log_entry = AIUsageLog(
            tenant_id=tenant_id,
            channel=channel,
            actor_type=actor_type,
            actor_id=actor_id,
            request_id=request_id,
            model=model,
            prompt_version=prompt_version,
            tool_names=tool_names,
            input_tokens=metrics.prompt_tokens,
            output_tokens=metrics.completion_tokens,
            cached_tokens=metrics.cached_tokens or 0,
            latency_ms=latency_ms,
            estimated_cost=estimated_cost,
            status=status,
            created_at=datetime.now(timezone.utc)
        )

        db.session.add(log_entry)

        # Also update budget if applicable
        if tenant_id:
            self._update_tenant_budget(tenant_id, estimated_cost)

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to record AI usage for request {request_id}: {e}")

        return log_entry

    def _update_tenant_budget(self, tenant_id: int, cost: float):
        budget = TenantBudget.query.filter_by(tenant_id=tenant_id).first()
        if not budget:
            # Auto-provision a default budget for now if it doesn't exist
            budget = TenantBudget(
                tenant_id=tenant_id,
                monthly_budget=50.0, # default $50
                current_spend=0.0
            )
            db.session.add(budget)

        budget.current_spend += cost

    def check_budget_status(self, tenant_id: int) -> dict:
        """
        Returns the budget status. Useful for early-rejecting requests if hard limit is reached.
        """
        if not tenant_id:
            return {"allowed": True, "reason": "No tenant scope"}

        budget = TenantBudget.query.filter_by(tenant_id=tenant_id).first()
        if not budget:
            return {"allowed": True, "reason": "No budget set"}

        is_hard_limited = budget.current_spend >= (budget.monthly_budget * budget.hard_limit_threshold)
        is_soft_limited = budget.current_spend >= (budget.monthly_budget * budget.soft_limit_threshold)

        return {
            "allowed": not is_hard_limited,
            "is_soft_limited": is_soft_limited,
            "current_spend": budget.current_spend,
            "budget": budget.monthly_budget
        }

usage_metering_service = UsageMeteringService()
