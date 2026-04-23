from typing import Optional, List, Dict, Any, Union, Literal
from pydantic import BaseModel, Field, model_validator
import uuid

class GatewayOutputItem(BaseModel):
    type: Literal["text"] = "text"
    text: str

class ToolCall(BaseModel):
    id: str
    type: str = "function"
    name: str
    arguments: str # JSON string of arguments

class ToolResult(BaseModel):
    tool_call_id: str
    output: str

class GatewayInputItem(BaseModel):
    type: Literal["text", "image", "file", "tool_result"] = "text"
    text: Optional[str] = None
    # For images/files, we could store URLs or base64. Using a generic 'url' for now.
    url: Optional[str] = None
    tool_call_id: Optional[str] = None

class UsageMetrics(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

class GatewayRequest(BaseModel):
    """Input envelope for the AI Gateway."""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: Union[str, int]
    actor_id: Optional[Union[str, int]] = None
    actor_type: Optional[str] = None
    channel: str
    trace_id: Optional[str] = None

    model: str = "gpt-4o-mini"
    instructions: Optional[str] = None
    instructions_key: Optional[str] = None
    prompt_variables: Dict[str, Any] = Field(default_factory=dict)
    input_items: List[GatewayInputItem] = Field(default_factory=list)

    # XOR fields for continuation
    conversation_id: Optional[str] = None
    previous_response_id: Optional[str] = None

    output_schema: Optional[Dict[str, Any]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = "auto"
    parallel_tool_calls: bool = False

    max_output_tokens: Optional[int] = None
    metadata: Dict[str, str] = Field(default_factory=dict)

    timeout_ms: int = 30000
    max_tool_rounds: int = 5
    idempotency_key: Optional[str] = None

    provider_options: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_continuation_xor(self):
        if self.conversation_id and self.previous_response_id:
            raise ValueError("conversation_id and previous_response_id cannot be used together.")
        return self

class GatewayResponse(BaseModel):
    """Normalized response envelope from the AI Gateway."""
    request_id: str
    provider_response_id: Optional[str] = None
    conversation_id: Optional[str] = None
    model: str

    status: Literal["completed", "incomplete", "refused", "tool_calls_pending", "error"]

    text: Optional[str] = None
    structured_output: Optional[Dict[str, Any]] = None
    output_items: List[GatewayOutputItem] = Field(default_factory=list)
    tool_calls: List[ToolCall] = Field(default_factory=list)

    refusal: Optional[str] = None
    incomplete_reason: Optional[str] = None

    usage: UsageMetrics = Field(default_factory=UsageMetrics)
    latency_ms: int = 0
    cost_estimate: float = 0.0

    error_code: Optional[str] = None
    error_message: Optional[str] = None
