import logging
import json
import time
from typing import Dict, Any, List, Optional
import openai
from openai import OpenAI

from schemas.ai_contracts import (
    GatewayRequest,
    GatewayResponse,
    GatewayOutputItem,
    ToolCall,
    UsageMetrics
)
from services.tool_registry import tool_registry

logger = logging.getLogger(__name__)

class OpenAIResponsesProvider:
    """Adapter for OpenAI API, specifically targeting the Chat Completions API with tools and structured outputs."""

    def __init__(self, client: Optional[OpenAI] = None):
        # Allow injecting a client, otherwise try to fall back to the bridge instance or create one.
        if client:
            self.client = client
        else:
            try:
                from services.openai_bridge import client as openai_client
                self.client = openai_client
            except ImportError:
                self.client = OpenAI()

    def generate_stream(self, request: GatewayRequest):
        """
        Executes a complete AI request utilizing the OpenAI SDK with streaming.
        Yields dictionaries representing typed events for the AIStreamService.
        """
        start_time = time.perf_counter()

        # 1. Build messages payload
        messages = []
        messages.append({"role": "system", "content": request.instructions})

        if not hasattr(self, "_message_state"):
            self._message_state = {}

        if request.previous_response_id and request.previous_response_id in self._message_state:
            messages = self._message_state[request.previous_response_id].copy()

        content_items = []
        tool_results = []
        for item in request.input_items:
            if item.type == "text" and item.text:
                content_items.append({"type": "text", "text": item.text})
            elif item.type == "image" and item.url:
                content_items.append({"type": "image_url", "image_url": {"url": item.url}})
            elif item.type == "tool_result" and item.tool_call_id:
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": item.tool_call_id,
                    "content": item.text or ""
                })

        if content_items:
            messages.append({"role": "user", "content": content_items})

        if tool_results:
            messages.extend(tool_results)

        kwargs = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "parallel_tool_calls": request.parallel_tool_calls,
            "stream": True
        }

        tools = request.tools
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = request.tool_choice if tools else None

        if request.output_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": True,
                    "schema": request.output_schema
                }
            }

        if request.provider_options:
            for k, v in request.provider_options.items():
                if k not in kwargs:
                    kwargs[k] = v

        try:
            stream = self.client.chat.completions.create(**kwargs)

            full_text = []
            tool_calls_buffer = {}
            current_provider_id = None
            usage_metrics = UsageMetrics()

            for chunk in stream:
                if chunk.id and not current_provider_id:
                    current_provider_id = chunk.id

                choice = chunk.choices[0] if chunk.choices else None
                if choice and choice.delta:
                    delta = choice.delta

                    if delta.content:
                        full_text.append(delta.content)
                        yield {
                            "type": "response.delta",
                            "data": {"text": delta.content}
                        }

                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_buffer:
                                tool_calls_buffer[idx] = {"id": tc.id, "name": tc.function.name, "arguments": ""}
                                yield {
                                    "type": "tool.started",
                                    "data": {"tool_call_id": tc.id, "name": tc.function.name}
                                }
                            if tc.function.arguments:
                                tool_calls_buffer[idx]["arguments"] += tc.function.arguments

            # Yield completion
            finish_reason = None
            if chunk.choices:
                finish_reason = chunk.choices[0].finish_reason

            if tool_calls_buffer:
                for idx, tc in tool_calls_buffer.items():
                    yield {
                        "type": "tool.completed",
                        "data": tc
                    }

                # Signal the orchestrator that tools are pending
                yield {
                    "type": "response.completed",
                    "data": {
                        "status": "tool_calls_pending",
                        "tool_calls": list(tool_calls_buffer.values()),
                        "provider_response_id": current_provider_id
                    }
                }
            else:
                yield {
                    "type": "response.completed",
                    "data": {
                        "status": "completed",
                        "text": "".join(full_text),
                        "provider_response_id": current_provider_id
                    }
                }

        except Exception as e:
            logger.error(f"Provider Stream Exception: {e}", exc_info=True)
            yield {
                "type": "response.error",
                "data": {"error_code": "provider_error", "message": str(e)}
            }

    def generate(self, request: GatewayRequest) -> GatewayResponse:
        """Executes a complete AI request utilizing the OpenAI SDK."""
        start_time = time.perf_counter()

        # 1. Build messages payload
        messages = []

        # We enforce instructions as the primary system prompt
        messages.append({
            "role": "system",
            "content": request.instructions
        })

        # We need a persistent session cache for tool calls/responses if we are simulating the new
        # stateful Responses API on top of standard Chat Completions until the actual SDK for Responses
        # is fully available in the environment, or we can use it to emulate previous_response_id.
        if not hasattr(self, "_message_state"):
            self._message_state = {}

        # If chaining from previous_response_id, fetch history
        if request.previous_response_id and request.previous_response_id in self._message_state:
            messages = self._message_state[request.previous_response_id].copy()

        # Combine input items into the prompt
        content_items = []
        tool_results = []
        for item in request.input_items:
            if item.type == "text" and item.text:
                content_items.append({"type": "text", "text": item.text})
            elif item.type == "image" and item.url:
                content_items.append({
                    "type": "image_url",
                    "image_url": {"url": item.url}
                })
            elif item.type == "tool_result" and item.tool_call_id:
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": item.tool_call_id,
                    "content": item.text or ""
                })

        if content_items:
            messages.append({
                "role": "user",
                "content": content_items
            })

        if tool_results:
            messages.extend(tool_results)

        # 2. Build Tool definitions
        tools = request.tools
        if tools is None:
            # If not explicitly provided, we could optionally default to the registry
            # tools = tool_registry.get_all_schemas()
            pass

        tool_choice = request.tool_choice if tools else None

        # 3. Handle structured outputs
        response_format = None
        if request.output_schema:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": True,
                    "schema": request.output_schema
                }
            }

        kwargs = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            # We enforce deterministic parallel_tool_calls according to user requirement
            "parallel_tool_calls": request.parallel_tool_calls
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        if response_format:
            kwargs["response_format"] = response_format

        # Provider specific options
        if request.provider_options:
            for k, v in request.provider_options.items():
                if k not in kwargs:
                    kwargs[k] = v

        try:
            # Note: We don't loop here. The orchestrator loop handles multiple tools calls.
            # This provider method does ONE generation round.

            response = self.client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            message = choice.message

            # Store the current message history into state for potential chaining
            messages.append(message.model_dump(exclude_none=True))
            self._message_state[response.id] = messages

            # Map Response Status
            finish_reason = choice.finish_reason
            if finish_reason == "tool_calls":
                status = "tool_calls_pending"
            elif finish_reason == "stop":
                status = "completed"
            elif finish_reason == "length":
                status = "incomplete"
            elif finish_reason == "content_filter":
                status = "refused"
            else:
                status = "error"

            # Parse tool calls
            mapped_tool_calls = []
            if message.tool_calls:
                for tc in message.tool_calls:
                    mapped_tool_calls.append(ToolCall(
                        id=tc.id,
                        type="function",
                        name=tc.function.name,
                        arguments=tc.function.arguments
                    ))

            # Parse structured data or raw text
            text_val = None
            structured_val = None

            if message.content:
                text_val = message.content
                if response_format:
                    try:
                        structured_val = json.loads(text_val)
                    except Exception:
                        logger.warning("Failed to parse structured output.")

            # Create Usage Metrics
            usage_metrics = UsageMetrics()
            if response.usage:
                usage_metrics.prompt_tokens = response.usage.prompt_tokens
                usage_metrics.completion_tokens = response.usage.completion_tokens
                usage_metrics.total_tokens = response.usage.total_tokens
                # cached tokens isn't always returned depending on model/SDK version
                cached = getattr(response.usage, "prompt_tokens_details", None)
                if cached:
                    usage_metrics.cached_tokens = getattr(cached, "cached_tokens", 0)

            latency = int((time.perf_counter() - start_time) * 1000.0)

            output_items = []
            if text_val:
                output_items.append(GatewayOutputItem(type="text", text=text_val))

            return GatewayResponse(
                request_id=request.request_id,
                provider_response_id=response.id,
                conversation_id=request.conversation_id,
                model=response.model,
                status=status,
                text=text_val,
                structured_output=structured_val,
                output_items=output_items,
                tool_calls=mapped_tool_calls,
                refusal=message.refusal if hasattr(message, "refusal") else None,
                incomplete_reason="max_tokens_reached" if finish_reason == "length" else None,
                usage=usage_metrics,
                latency_ms=latency,
                cost_estimate=0.0, # Handled by telemetry later
            )

        except Exception as e:
            logger.error(f"Provider Exception: {e}", exc_info=True)
            latency = int((time.perf_counter() - start_time) * 1000.0)
            return GatewayResponse(
                request_id=request.request_id,
                model=request.model,
                conversation_id=request.conversation_id,
                status="error",
                error_code="provider_error",
                error_message=str(e),
                latency_ms=latency
            )
