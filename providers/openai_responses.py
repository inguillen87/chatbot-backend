import logging
import json
import time
from typing import Dict, Any, List, Optional
import openai
from openai import OpenAI

from cachetools import TTLCache

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
    """Adapter for the current OpenAI Responses API.

    Chat Completions is used only by clients that do not expose ``responses``.
    Once a Responses request starts, an exception has an ambiguous outcome and
    must never trigger a second provider request automatically.
    """

    def __init__(self, client: Optional[OpenAI] = None):
        # Allow injecting a client, otherwise try to fall back to the bridge instance or create one.
        self._uses_managed_client = client is None
        if client:
            self.client = client
        else:
            try:
                from services.openai_bridge import responses_client
                self.client = responses_client
            except ImportError:
                self.client = OpenAI()

    def _request_model(self, model: str) -> str:
        if not self._uses_managed_client:
            return model
        from services.openai_bridge import _model_for_openai_transport

        return _model_for_openai_transport(model)

    def _uses_cloudflare_transport(self) -> bool:
        if not self._uses_managed_client:
            return False
        from services.openai_bridge import _cloudflare_ai_gateway_config

        return _cloudflare_ai_gateway_config() is not None

    def _supports_responses_api(self) -> bool:
        return bool(getattr(self.client, "responses", None))

    def _supports_responses_stream(self) -> bool:
        responses = getattr(self.client, "responses", None)
        return callable(getattr(responses, "stream", None))

    def _normalize_tools_for_responses(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        if not tools:
            return None
        normalized = []
        for tool in tools:
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                fn = tool["function"]
                normalized.append({
                    "type": "function",
                    "name": fn.get("name"),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters") or {},
                    "strict": bool(fn.get("strict", False)),
                })
            else:
                normalized.append(tool)
        return normalized

    def _build_responses_input(self, request: GatewayRequest) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        user_content: List[Dict[str, Any]] = []

        for item in request.input_items:
            if item.type == "text" and item.text:
                user_content.append({"type": "input_text", "text": item.text})
            elif item.type == "image" and item.url:
                user_content.append({"type": "input_image", "image_url": item.url})
            elif item.type == "file" and item.url:
                user_content.append({"type": "input_file", "file_url": item.url})
            elif item.type == "tool_result" and item.tool_call_id:
                items.append({
                    "type": "function_call_output",
                    "call_id": item.tool_call_id,
                    "output": item.text or "",
                })

        if user_content:
            items.insert(0, {"role": "user", "content": user_content})

        return items or [{"role": "user", "content": [{"type": "input_text", "text": ""}]}]

    def _get_attr(self, obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _extract_response_text(self, response: Any) -> Optional[str]:
        output_text = self._get_attr(response, "output_text")
        if output_text:
            return output_text

        chunks: List[str] = []
        for item in self._get_attr(response, "output", []) or []:
            if self._get_attr(item, "type") != "message":
                continue
            for content in self._get_attr(item, "content", []) or []:
                ctype = self._get_attr(content, "type")
                if ctype in {"output_text", "text"}:
                    text = self._get_attr(content, "text")
                    if text:
                        chunks.append(text)
        return "".join(chunks) or None

    def _extract_response_tool_calls(self, response: Any) -> List[ToolCall]:
        calls: List[ToolCall] = []
        for item in self._get_attr(response, "output", []) or []:
            if self._get_attr(item, "type") != "function_call":
                continue
            call_id = self._get_attr(item, "call_id") or self._get_attr(item, "id")
            calls.append(ToolCall(
                id=call_id,
                type="function",
                name=self._get_attr(item, "name") or "",
                arguments=self._get_attr(item, "arguments") or "{}",
            ))
        return calls

    def _usage_from_responses(self, response: Any) -> UsageMetrics:
        usage_metrics = UsageMetrics()
        usage = self._get_attr(response, "usage")
        if not usage:
            return usage_metrics

        usage_metrics.prompt_tokens = self._get_attr(usage, "input_tokens", 0) or 0
        usage_metrics.completion_tokens = self._get_attr(usage, "output_tokens", 0) or 0
        usage_metrics.total_tokens = self._get_attr(usage, "total_tokens", 0) or (
            usage_metrics.prompt_tokens + usage_metrics.completion_tokens
        )
        input_details = self._get_attr(usage, "input_tokens_details")
        if input_details:
            usage_metrics.cached_tokens = self._get_attr(input_details, "cached_tokens", 0) or 0
        return usage_metrics

    def _responses_request_kwargs(self, request: GatewayRequest) -> Dict[str, Any]:
        tools = self._normalize_tools_for_responses(request.tools)
        kwargs: Dict[str, Any] = {
            "model": self._request_model(request.model),
            "instructions": request.instructions or "",
            "input": self._build_responses_input(request),
            "parallel_tool_calls": request.parallel_tool_calls,
        }
        if self._uses_cloudflare_transport():
            kwargs["store"] = False

        if request.max_output_tokens is not None:
            kwargs["max_output_tokens"] = request.max_output_tokens

        if request.previous_response_id:
            kwargs["previous_response_id"] = request.previous_response_id

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = request.tool_choice if request.tool_choice else "auto"

        if request.output_schema:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "structured_output",
                    "strict": True,
                    "schema": request.output_schema,
                }
            }

        if request.provider_options:
            for k, v in request.provider_options.items():
                if k not in kwargs:
                    kwargs[k] = v

        return kwargs

    def _generate_with_responses_api(self, request: GatewayRequest, *, start_time: float) -> GatewayResponse:
        kwargs = self._responses_request_kwargs(request)

        response = self.client.responses.create(**kwargs)
        text_val = self._extract_response_text(response)
        mapped_tool_calls = self._extract_response_tool_calls(response)

        status_raw = str(self._get_attr(response, "status", "completed") or "completed")
        if mapped_tool_calls:
            status = "tool_calls_pending"
        elif status_raw == "completed":
            status = "completed"
        elif status_raw == "incomplete":
            status = "incomplete"
        elif status_raw in {"refused", "content_filter"}:
            status = "refused"
        else:
            status = "error"

        structured_val = None
        if text_val and request.output_schema:
            try:
                structured_val = json.loads(text_val)
            except Exception:
                logger.warning("Failed to parse structured Responses API output.")

        output_items = []
        if text_val:
            output_items.append(GatewayOutputItem(type="text", text=text_val))

        incomplete_details = self._get_attr(response, "incomplete_details")
        incomplete_reason = self._get_attr(incomplete_details, "reason") if incomplete_details else None

        latency = int((time.perf_counter() - start_time) * 1000.0)
        return GatewayResponse(
            request_id=request.request_id,
            provider_response_id=self._get_attr(response, "id"),
            conversation_id=request.conversation_id,
            model=self._get_attr(response, "model", request.model),
            status=status,
            text=text_val,
            structured_output=structured_val,
            output_items=output_items,
            tool_calls=mapped_tool_calls,
            refusal=None,
            incomplete_reason=incomplete_reason,
            usage=self._usage_from_responses(response),
            latency_ms=latency,
            cost_estimate=0.0,
        )

    def _generate_stream_with_responses_api(self, request: GatewayRequest):
        kwargs = self._responses_request_kwargs(request)
        text_chunks: List[str] = []

        with self.client.responses.stream(**kwargs) as stream:
            for event in stream:
                event_type = self._get_attr(event, "type", "")
                if event_type == "response.output_text.delta":
                    delta = self._get_attr(event, "delta", "") or ""
                    if delta:
                        text_chunks.append(delta)
                        yield {"type": "response.delta", "data": {"text": delta}}

            response = stream.get_final_response()

        tool_calls = self._extract_response_tool_calls(response)
        mapped_tool_calls = [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in tool_calls
        ]
        for call in mapped_tool_calls:
            yield {
                "type": "tool.started",
                "data": {"tool_call_id": call["id"], "name": call["name"]},
            }
            yield {"type": "tool.completed", "data": call}

        status_raw = str(self._get_attr(response, "status", "completed") or "completed")
        if mapped_tool_calls:
            status = "tool_calls_pending"
        elif status_raw == "completed":
            status = "completed"
        elif status_raw == "incomplete":
            status = "incomplete"
        elif status_raw in {"refused", "content_filter"}:
            status = "refused"
        else:
            status = "error"

        text_value = "".join(text_chunks) or self._extract_response_text(response)
        yield {
            "type": "response.completed",
            "data": {
                "status": status,
                "text": text_value,
                "tool_calls": mapped_tool_calls,
                "provider_response_id": self._get_attr(response, "id"),
            },
        }

    def generate_stream(self, request: GatewayRequest):
        """
        Executes a complete AI request utilizing the OpenAI SDK with streaming.
        Yields dictionaries representing typed events for the AIStreamService.
        """
        start_time = time.perf_counter()

        if self._supports_responses_stream():
            try:
                yield from self._generate_stream_with_responses_api(request)
            except Exception as exc:
                logger.error(
                    "OpenAI Responses stream failed request_id=%s error_type=%s status_code=%s",
                    request.request_id,
                    type(exc).__name__,
                    getattr(exc, "status_code", None),
                )
                yield {
                    "type": "response.error",
                    "data": {
                        "error_code": "provider_outcome_unknown",
                        "message": "OpenAI Responses stream failed; automatic replay is disabled.",
                    },
                }
            return

        # 1. Build messages payload
        messages = []
        messages.append({"role": "system", "content": request.instructions})

        if not hasattr(self, "_message_state"):
            self._message_state = TTLCache(maxsize=1000, ttl=3600)

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
            "model": self._request_model(request.model),
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

        except Exception as exc:
            logger.error(
                "OpenAI Chat Completions stream failed request_id=%s error_type=%s status_code=%s",
                request.request_id,
                type(exc).__name__,
                getattr(exc, "status_code", None),
            )
            yield {
                "type": "response.error",
                "data": {
                    "error_code": "provider_outcome_unknown",
                    "message": "OpenAI stream failed; automatic replay is disabled.",
                }
            }

    def generate(self, request: GatewayRequest) -> GatewayResponse:
        """Executes a complete AI request utilizing the OpenAI SDK."""
        start_time = time.perf_counter()

        if self._supports_responses_api():
            try:
                return self._generate_with_responses_api(request, start_time=start_time)
            except Exception as exc:
                logger.error(
                    "OpenAI Responses request failed request_id=%s error_type=%s status_code=%s",
                    request.request_id,
                    type(exc).__name__,
                    getattr(exc, "status_code", None),
                )
                latency = int((time.perf_counter() - start_time) * 1000.0)
                return GatewayResponse(
                    request_id=request.request_id,
                    model=request.model,
                    conversation_id=request.conversation_id,
                    status="error",
                    error_code="provider_outcome_unknown",
                    error_message="OpenAI Responses request failed; automatic replay is disabled.",
                    latency_ms=latency,
                )

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
            self._message_state = TTLCache(maxsize=1000, ttl=3600) # Keep 1 hour of states max

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
            "model": self._request_model(request.model),
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

        except Exception as exc:
            logger.error(
                "OpenAI Chat Completions request failed request_id=%s error_type=%s status_code=%s",
                request.request_id,
                type(exc).__name__,
                getattr(exc, "status_code", None),
            )
            latency = int((time.perf_counter() - start_time) * 1000.0)
            return GatewayResponse(
                request_id=request.request_id,
                model=request.model,
                conversation_id=request.conversation_id,
                status="error",
                error_code="provider_outcome_unknown",
                error_message="OpenAI request failed; automatic replay is disabled.",
                latency_ms=latency
            )
