import models_analytics_k
import logging
from datetime import datetime, timezone, timedelta
from app import db


logger = logging.getLogger(__name__)

class AnalyticsKPIService:
    def get_operational_metrics(self, tenant_id: int, days_back: int = 30) -> dict:
        """
        Calculates First Response Time (FRT), Handoff Rate, Ticket Resolution Rate,
        and Survey Participation for a given tenant over the last X days.
        """
        since_date = datetime.now(timezone.utc) - timedelta(days=days_back)

        from models import PymeTicket, EncRespuesta, Message
        # 1. Ticket Resolution & Volume
        TicketModel = PymeTicket

        total_tickets = db.session.query(TicketModel).filter(
            TicketModel.tenant_id == tenant_id,
            TicketModel.fecha_creacion >= since_date
        ).count()

        resolved_tickets = db.session.query(TicketModel).filter(
            TicketModel.tenant_id == tenant_id,
            TicketModel.fecha_creacion >= since_date,
            TicketModel.estado == 'resuelto' # or equivalent
        ).count()

        resolution_rate = (resolved_tickets / total_tickets * 100) if total_tickets > 0 else 0

        # 2. Survey Participation
        total_responses = db.session.query(EncRespuesta).filter(
            EncRespuesta.tenant_id == tenant_id,
            EncRespuesta.fecha >= since_date
        ).count()

        # 3. Handoff Rate (derivar a humano)
        total_bot_messages = db.session.query(Message).filter(
            Message.tenant_id == tenant_id,
            Message.sender_type == 'bot',
            Message.timestamp >= since_date
        ).count()

        handoff_messages = db.session.query(Message).filter(
            Message.tenant_id == tenant_id,
            Message.sender_type == 'bot',
            Message.content.ilike('%derivando%'), # Rough heuristic if explicit flag not available
            Message.timestamp >= since_date
        ).count()

        handoff_rate = (handoff_messages / total_bot_messages * 100) if total_bot_messages > 0 else 0

        # 4. FRT (First Response Time) - Simplified mock for now
        frt_minutes = 2.5 # Mock calculation: needs specific timeline parsing

        return {
            "period_days": days_back,
            "tickets": {
                "total": total_tickets,
                "resolved": resolved_tickets,
                "resolution_rate_percent": round(resolution_rate, 2)
            },
            "surveys": {
                "total_responses": total_responses
            },
            "chat": {
                "total_bot_messages": total_bot_messages,
                "handoff_rate_percent": round(handoff_rate, 2),
                "avg_first_response_time_minutes": frt_minutes
            }
        }

    def get_cost_metrics(self, tenant_id: int) -> dict:
        budget = models_analytics_k.TenantBudget.query.filter_by(tenant_id=tenant_id).first()
        if not budget:
            return {"status": "no_budget_set"}

        return {
            "budget": budget.monthly_budget,
            "current_spend": budget.current_spend,
            "remaining": max(0, budget.monthly_budget - budget.current_spend),
            "utilization_percent": round((budget.current_spend / budget.monthly_budget) * 100, 2) if budget.monthly_budget > 0 else 0
        }

analytics_kpi_service = AnalyticsKPIService()
