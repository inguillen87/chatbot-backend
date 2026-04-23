import logging
from typing import Dict, Any, Optional

from schemas.ai_contracts import GatewayRequest, GatewayResponse, GatewayInputItem
from providers.openai_responses import OpenAIResponsesProvider
from services.tool_registry import tool_registry
from services.ai_policy import policy_engine
from services.audit_service import audit_service
from services.prompt_registry import prompt_registry

logger = logging.getLogger(__name__)

class AIGateway:
    """
    Central orchestration layer for all AI inference requests.
    Manages policy checks, provider delegation, the tool-calling loop, and telemetry.
    """

    def __init__(self, provider: Optional[Any] = None):
        self.provider = provider or OpenAIResponsesProvider()

    def _resolve_prompts_and_policy(self, request: GatewayRequest) -> GatewayRequest:
        """Helper to resolve instructions from registry and run pre-flight policy."""
        # 0. Resolve Prompts
        current_request = request.model_copy()

        if current_request.instructions_key and not current_request.instructions:
            prompt_obj = prompt_registry.get_active_prompt(
                prompt_key=current_request.instructions_key,
                tenant_id=current_request.tenant_id,
                channel=current_request.channel
            )
            if prompt_obj:
                current_request.instructions = prompt_registry.render_instructions(
                    prompt_obj, current_request.prompt_variables
                )
                current_request.model = prompt_obj.model_default or current_request.model
                # Merge dynamic provider options or response schemas if the prompt enforces them
                if prompt_obj.response_schema and not current_request.output_schema:
                    current_request.output_schema = prompt_obj.response_schema
            else:
                logger.warning(f"Could not resolve prompt key '{current_request.instructions_key}'. Proceeding with empty instructions.")
                current_request.instructions = current_request.instructions or ""
        elif not current_request.instructions:
            current_request.instructions = ""

        # 1. Policy Pre-flight
        blocked_response = policy_engine.check_pre_flight(current_request)
        if blocked_response:
            # We raise here because this flow handles both sync and streaming now.
            # Catchers should handle this appropriately.
            raise ValueError(f"Policy blocked request: {blocked_response.error_message}")

        return current_request

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        """
        Main entry point for AI processing.
        1. Pre-flight policy checks
        2. Provider generation
        3. Tool execution loop (if tools are returned)
        4. Post-flight policy checks
        5. Telemetry logging
        """
        try:
            current_request = self._resolve_prompts_and_policy(request)
        except ValueError as e:
            return GatewayResponse(
                request_id=request.request_id,
                model=request.model,
                status="refused",
                error_message=str(e),
                latency_ms=0
            )

        rounds_executed = 0
        total_latency = 0

        all_tool_logs = []

        while rounds_executed < request.max_tool_rounds:
            rounds_executed += 1

            # 2. Provider execution
            response = self.provider.generate(current_request)
            total_latency += response.latency_ms

            if response.status == "error" or response.status == "refused":
                response.latency_ms = total_latency
                return self._finalize(current_request, response, all_tool_logs)

            # 3. Tool Loop
            if response.status == "tool_calls_pending" and response.tool_calls:
                # Execute tools
                new_input_items = []
                for tc in response.tool_calls:
                    import time
                    start_tc = time.perf_counter()
                    tool_log = {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                        "result": None,
                        "is_error": False
                    }
                    try:
                        import json
                        args = json.loads(tc.arguments)
                        # Inject tenant/context details
                        context = {"tenant_id": request.tenant_id, "channel": request.channel}

                        tool_result = tool_registry.execute_tool(tc.name, args, context)
                        # Serialize result
                        res_str = json.dumps(tool_result) if not isinstance(tool_result, str) else str(tool_result)
                        tool_log["result"] = res_str

                        new_input_items.append(GatewayInputItem(
                            type="tool_result",
                            tool_call_id=tc.id,
                            text=res_str
                        ))
                    except Exception as e:
                        err_str = f"Error executing tool: {e}"
                        tool_log["result"] = err_str
                        tool_log["is_error"] = True
                        new_input_items.append(GatewayInputItem(
                            type="tool_result",
                            tool_call_id=tc.id,
                            text=err_str
                        ))
                    finally:
                        all_tool_logs.append(tool_log)

                # Create next round request.
                # We specifically link this request to the previous response to maintain state.
                # We also clear the original instructions and tools per OpenAI docs
                # when chaining using previous_response_id, but the adapter will handle what actually
                # gets sent depending on whether it's managing a local message history or using true Responses.
                current_request = current_request.model_copy(update={
                    "input_items": new_input_items,
                    "previous_response_id": response.provider_response_id,
                    "conversation_id": None # Mutual exclusivity
                })

                # Continue loop
            else:
                # Final response reached
                response.latency_ms = total_latency
                return self._finalize(current_request, response, all_tool_logs)

        # If we exit the loop, we hit the max_tool_rounds limit
        logger.warning(f"Request {request.request_id} hit max tool rounds ({request.max_tool_rounds})")

        final_response = response.model_copy(update={
            "status": "incomplete",
            "incomplete_reason": "max_tool_rounds_reached",
            "latency_ms": total_latency
        })

        return self._finalize(current_request, final_response, all_tool_logs)

    def execute_stream(self, request: GatewayRequest):
        """
        Streaming entry point for AI processing.
        Handles policy checks, then yields SSE formatted events using the AIStreamService.
        """
        from services.ai_stream_service import stream_service
        from schemas.ai_stream import AIStreamEvent

        try:
            current_request = self._resolve_prompts_and_policy(request)
        except ValueError as e:
            err_event = AIStreamEvent(
                event_type="response.error",
                request_id=request.request_id,
                correlation_id=request.trace_id,
                data={"error_code": "policy_blocked", "message": str(e)}
            )
            yield stream_service._format_sse(err_event)
            return

        # Note: Tool loop inside a stream requires careful buffer management.
        # This yields the stream to the client. Real multi-round tool loops over streaming
        # usually pause the stream, execute the tool internally, and then restart the stream
        # from the provider.
        # For this foundation step, we delegate the generator out to stream_service.

        provider_generator = self.provider.generate_stream(current_request)

        # Since we are yielding directly to the client, post-flight policy on the full text
        # usually has to happen asynchronously or by accumulating the stream buffer and
        # checking the final text.

        # In a real enterprise system, an aggregator buffer would live here to rebuild the
        # final GatewayResponse to pass to self._finalize(...) at the end of the stream.
        # For now, we simply yield the semantic stream events.

        for event_str in stream_service.stream_response(current_request, provider_generator):
            yield event_str

    def _finalize(self, request: GatewayRequest, response: GatewayResponse, tool_calls_log: list = None) -> GatewayResponse:
        """Handles Post-flight policy checks and Telemetry logging."""
        # Post-flight policy
        checked_response = policy_engine.check_post_flight(request, response)

        # Telemetry & Audit
        log_entry = audit_service.log_request(request, checked_response)
        if log_entry and tool_calls_log:
            audit_service.log_tool_calls(log_entry.id, tool_calls_log)

        return checked_response

# Global orchestrator instance
ai_gateway = AIGateway()
