import logging
from typing import Dict, Any, Optional

from schemas.ai_contracts import GatewayRequest, GatewayResponse, GatewayInputItem
from providers.openai_responses import OpenAIResponsesProvider
from services.tool_registry import tool_registry
from services.ai_policy import policy_engine
from services.audit_service import audit_service

logger = logging.getLogger(__name__)

class AIGateway:
    """
    Central orchestration layer for all AI inference requests.
    Manages policy checks, provider delegation, the tool-calling loop, and telemetry.
    """

    def __init__(self, provider: Optional[Any] = None):
        self.provider = provider or OpenAIResponsesProvider()

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        """
        Main entry point for AI processing.
        1. Pre-flight policy checks
        2. Provider generation
        3. Tool execution loop (if tools are returned)
        4. Post-flight policy checks
        5. Telemetry logging
        """
        # 1. Policy Pre-flight
        blocked_response = policy_engine.check_pre_flight(request)
        if blocked_response:
            return blocked_response

        current_request = request
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
