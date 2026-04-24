import logging
from typing import Dict, Any, List, Optional
from schemas.ai_contracts import GatewayRequest, GatewayResponse, GatewayInputItem
from database import db
from models_audit import ModerationEvent

logger = logging.getLogger(__name__)

class AIPolicyEngine:
    """Pre and post moderation checks for the AI Gateway."""

    def __init__(self):
        pass

    def check_pre_flight(self, request: GatewayRequest) -> Optional[GatewayResponse]:
        """
        Evaluate policies before sending to the model.
        Returns a GatewayResponse if blocked, otherwise None.
        """
        # Ex: Check for restricted tenants, rate limits, basic bad words, etc.
        # This acts as an initial safety net. For now, it just passes.
        return None

    def check_post_flight(self, request: GatewayRequest, response: GatewayResponse) -> GatewayResponse:
        """
        Evaluate policies after the model generates a response.
        May redact PII or block completely based on tenant rules.
        """
        # Ex: PII redaction or hallucination check.
        # For now, it passes the response straight through.
        return response

    def log_moderation_event(self, tenant_id: Any, channel: str, action: str, rule_id: Optional[int] = None):
        """Persists a moderation event."""
        try:
            event = ModerationEvent(
                tenant_id=int(tenant_id) if str(tenant_id).isdigit() else None,
                channel=channel,
                action_taken=action,
                rule_triggered=rule_id
            )
            db.session.add(event)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to log moderation event: {e}")

# Global instance for easy access
policy_engine = AIPolicyEngine()
