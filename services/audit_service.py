import hashlib
import json
import logging
from typing import Any, Dict, Optional
from datetime import datetime, timezone

from database import db
from models_audit import AIRequestLog, AIToolCallLog
from schemas.ai_contracts import GatewayRequest, GatewayResponse

logger = logging.getLogger(__name__)

class AuditService:
    """Handles logging AI requests, tool execution, and maintaining hash chains for auditability."""

    def __init__(self):
        pass

    def _get_last_hash(self, tenant_id: Any) -> Optional[str]:
        # Simple lookup for the latest hash chain link for a tenant
        last_log = AIRequestLog.query.filter_by(tenant_id=tenant_id).order_by(AIRequestLog.id.desc()).first()
        return last_log.hash if last_log else None

    def _compute_hash(self, prev_hash: Optional[str], payload: Dict[str, Any]) -> str:
        data = f"{prev_hash or ''}|{json.dumps(payload, sort_keys=True)}"
        return hashlib.sha256(data.encode('utf-8')).hexdigest()

    def log_request(self, request: GatewayRequest, response: GatewayResponse) -> AIRequestLog:
        """Logs a completed request and response, updating the hash chain."""
        try:
            prev_hash = self._get_last_hash(request.tenant_id)

            # Hash payload representing core auditable facts
            payload_to_hash = {
                "request_id": response.request_id,
                "tenant_id": request.tenant_id,
                "model": response.model,
                "status": response.status,
                "latency": response.latency_ms,
                "usage": response.usage.model_dump()
            }

            current_hash = self._compute_hash(prev_hash, payload_to_hash)

            log_entry = AIRequestLog(
                request_id=response.request_id,
                tenant_id=int(request.tenant_id) if str(request.tenant_id).isdigit() else None,
                channel=request.channel,
                actor_type=request.actor_type or "system",
                actor_id=str(request.actor_id) if request.actor_id else None,
                session_id=request.conversation_id,
                prompt_key=None, # Filled by prompt registry later
                prompt_version=getattr(request, "prompt_version", None),
                model=response.model,
                latency_ms=response.latency_ms,
                token_usage_prompt=response.usage.prompt_tokens,
                token_usage_completion=response.usage.completion_tokens,
                decision_status=response.status,
                prev_hash=prev_hash,
                hash=current_hash
            )

            db.session.add(log_entry)
            db.session.commit()
            return log_entry

        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to audit log request {response.request_id}: {e}")
            return None

    def log_tool_calls(self, request_log_id: int, tool_calls_data: list[Dict[str, Any]]):
        """Logs individual tool calls related to a request."""
        if not request_log_id:
            return

        try:
            for tc in tool_calls_data:
                tool_log = AIToolCallLog(
                    request_log_id=request_log_id,
                    tool_call_id=tc.get("id"),
                    tool_name=tc.get("name"),
                    arguments=tc.get("arguments"),
                    result=tc.get("result"),
                    is_error=tc.get("is_error", False)
                )
                db.session.add(tool_log)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to audit log tool calls for req log {request_log_id}: {e}")

audit_service = AuditService()
