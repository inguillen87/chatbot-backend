# services/actions/base_action_handler.py
from abc import ABC, abstractmethod
from typing import Dict, Any

class BaseActionHandler(ABC):
    """
    Abstract base class for all action handlers.
    """
    def __init__(self, context: Dict[str, Any]):
        """
        Initialize the handler with the current context.
        Context might include user information, session data, LLM extracted data, etc.
        """
        self.context = context

    @abstractmethod
    def execute(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the action.

        :param action_data: Data specific to the action, typically from LLM's 'datos_estructura'.
        :return: A dictionary containing the result of the action, e.g.,
                 {
                     "success": True/False,
                     "message_to_user": "Message for user (DEPRECATED - use message_body)",
                     "message_body": "Primary text content for the response",
                     "message_type": "text" | "interactive_buttons" | "interactive_list" | "media",
                     "options_list": [],
                     "data": { ... }
                 }
        """
        pass

    def validate_data(self, action_data: Dict[str, Any], required_fields: list[str]) -> bool:
        """
        Basic validation to check if all required fields are present in action_data.
        More specific validation should be done in subclasses.
        """
        missing_fields = [field for field in required_fields if field not in action_data or not action_data[field]]
        if missing_fields:
            # In a real scenario, you might want to log this or prepare a specific error message.
            # For now, just returning False.
            print(f"Validation failed. Missing fields: {missing_fields}") # Replace with logger
            return False
        return True
