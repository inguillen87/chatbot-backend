import json
import logging
from typing import Generator, Any
from schemas.ai_stream import AIStreamEvent
from schemas.ai_contracts import GatewayRequest

logger = logging.getLogger(__name__)

class AIStreamService:
    """
    Handles Semantic Streaming via Server-Sent Events (SSE).
    Orchestrates streaming calls and maps them into typed AIStreamEvents.
    """

    def __init__(self, provider=None):
        self.provider = provider

    def _format_sse(self, event: AIStreamEvent) -> str:
        """Formats the Pydantic event into an SSE compliant string."""
        payload = event.model_dump(exclude_none=True)
        return f"event: {event.event_type}\ndata: {json.dumps(payload)}\n\n"

    def stream_response(self, request: GatewayRequest, provider_generator: Any) -> Generator[str, None, None]:
        """
        Consumes the raw provider stream generator and yields strictly typed
        SSE events to the frontend.
        """

        start_event = AIStreamEvent(
            event_type="response.started",
            request_id=request.request_id,
            correlation_id=request.trace_id,
            data={"model": request.model}
        )
        yield self._format_sse(start_event)

        try:
            for chunk in provider_generator:
                # The provider adapter is expected to yield raw dictionaries representing parsed stream state
                # or predefined intermediate models.
                # Since we are isolating the provider, we map their chunks into our AIStreamEvents.

                event_type = chunk.get("type")
                data = chunk.get("data", {})

                event = AIStreamEvent(
                    event_type=event_type,
                    request_id=request.request_id,
                    correlation_id=request.trace_id,
                    data=data
                )
                yield self._format_sse(event)

        except Exception as e:
            logger.error(f"Stream error on request {request.request_id}: {e}", exc_info=True)
            err_event = AIStreamEvent(
                event_type="response.error",
                request_id=request.request_id,
                correlation_id=request.trace_id,
                data={"error_code": "stream_error", "message": str(e)}
            )
            yield self._format_sse(err_event)

# Global service
stream_service = AIStreamService()
