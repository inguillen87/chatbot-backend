import logging
from typing import Dict, Any, Optional
from schemas.ai_contracts import GatewayRequest, GatewayInputItem
from services.ai_gateway import ai_gateway
from schemas.multimodal_ticket import TicketDraftSchema

logger = logging.getLogger(__name__)

class MultimodalTicketService:
    def __init__(self):
        self.system_instructions = (
            "Analyze the provided image and extract information to create a support ticket or claim. "
            "Identify the category, subcategory, priority, a brief summary, any visible location text, "
            "and evidence tags. "
            "If the image is unclear, ambiguous, or contains sensitive Personally Identifiable Information (PII), "
            "set 'needs_human_review' to true. "
            "Provide a 'confidence' score between 0.0 and 1.0."
        )

    def analyze_image_for_ticket(self, tenant_id: int, image_url: str, actor_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Uses the AIGateway to process an image and return a structured ticket draft.
        """

        request = GatewayRequest(
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_type="user",
            channel="web", # or portal/widget based on where it came from
            model="gpt-4o", # Model capable of vision
            instructions=self.system_instructions,
            input_items=[
                GatewayInputItem(type="image", url=image_url)
            ],
            output_schema=TicketDraftSchema.model_json_schema()
        )

        response = ai_gateway.execute(request)

        if response.status == "error" or response.status == "refused":
            logger.error(f"Failed to extract ticket from image: {response.error_message or response.refusal}")
            return {
                "error": True,
                "message": "No se pudo analizar la imagen.",
                "details": response.error_message or response.refusal
            }

        if response.structured_output:
            draft = response.structured_output

            # Post-processing logic to determine if we should auto-create or return as draft.
            # Enforce CODEX rule: Do not auto-create if confidence < threshold or needs_human_review is True
            confidence = draft.get("confidence", 0.0)
            needs_review = draft.get("needs_human_review", True)

            if confidence < 0.85 or needs_review:
                draft["auto_create_eligible"] = False
                draft["reason"] = "Requiere revisión humana debido a ambigüedad o baja confianza."
            else:
                draft["auto_create_eligible"] = True

            return {
                "error": False,
                "draft": draft
            }

        return {
            "error": True,
            "message": "El modelo no devolvió un formato estructurado válido."
        }

multimodal_ticket_service = MultimodalTicketService()
