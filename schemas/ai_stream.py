from typing import Optional, Dict, Any, Literal
from pydantic import BaseModel, Field

class AIStreamEvent(BaseModel):
    event_type: Literal[
        "response.started",
        "response.delta",
        "response.reasoning_summary",
        "tool.started",
        "tool.completed",
        "citation.added",
        "moderation.warning",
        "response.completed",
        "response.error",
        "usage.reported"
    ]
    request_id: str
    correlation_id: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)
