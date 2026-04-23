import logging
from typing import Dict, Any, Callable, Optional, List
from pydantic import BaseModel
import inspect

logger = logging.getLogger(__name__)

class ToolDefinition(BaseModel):
    name: str
    description: str
    tool_schema: Dict[str, Any]
    handler: Callable

class ToolRegistry:
    """Centralized tool registry for AI Gateway function calling."""

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, name: str, description: str, schema: Dict[str, Any]):
        """Decorator to register a tool function."""
        def decorator(func: Callable):
            self._tools[name] = ToolDefinition(
                name=name,
                description=description,
                tool_schema=schema,
                handler=func
            )
            return func
        return decorator

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """Fetch a registered tool by name."""
        return self._tools.get(name)

    def execute_tool(self, name: str, kwargs: Dict[str, Any], context: Dict[str, Any]) -> Any:
        """Executes a tool with the provided kwargs, optionally injecting context."""
        tool = self.get_tool(name)
        if not tool:
            raise ValueError(f"Tool '{name}' not found in registry.")

        # Inspect signature to see if we should inject context
        sig = inspect.signature(tool.handler)
        if "context" in sig.parameters:
            kwargs["context"] = context

        try:
            result = tool.handler(**kwargs)
            return result
        except Exception as e:
            logger.error(f"Error executing tool '{name}': {e}", exc_info=True)
            return f"Error executing tool: {str(e)}"

    def get_all_schemas(self) -> List[Dict[str, Any]]:
        """Returns all registered tool schemas formatted for OpenAI."""
        schemas = []
        for tool in self._tools.values():
            schemas.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.tool_schema
                }
            })
        return schemas

# Global default registry
tool_registry = ToolRegistry()
