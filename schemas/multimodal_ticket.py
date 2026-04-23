from typing import List, Optional
from pydantic import BaseModel, Field

class TicketDraftSchema(BaseModel):
    category: str = Field(description="The primary category of the incident (e.g., 'bache', 'luminaria', 'limpieza').")
    subcategory: Optional[str] = Field(None, description="The subcategory (e.g., 'calzada', 'poste roto').")
    priority: str = Field(description="The priority level: 'alta', 'media', or 'baja'.")
    confidence: float = Field(description="A confidence score between 0.0 and 1.0 regarding the accuracy of this extraction.")
    summary: str = Field(description="A brief description of what the image shows.")
    suggested_location_text: Optional[str] = Field(None, description="Any location details visible or inferred from the image.")
    evidence_tags: List[str] = Field(default_factory=list, description="Tags describing key elements in the image.")
    needs_human_review: bool = Field(description="Set to true if there is ambiguity, poor image quality, or potential PII.")
