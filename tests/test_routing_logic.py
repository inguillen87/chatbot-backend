import unittest
from unittest.mock import patch, MagicMock
from services.routing_logic import route_action
from services.actions.base_action import BaseAction

class MockAction(BaseAction):
    def __init__(self, can_handle_response, execute_response):
        self._can_handle_response = can_handle_response
        self._execute_response = execute_response

    def can_handle(self, context, **kwargs):
        return self._can_handle_response

    def execute(self, **kwargs):
        return self._execute_response

class TestRoutingLogic(unittest.TestCase):

    def test_route_action_no_handler(self):
        """
        Tests that if no action handler can handle the context, a default response is returned.
        """
        action_handlers = [MockAction(False, {})]
        context = {"some_key": "some_value"}
        result = route_action(context, action_handlers)
        self.assertIn("error", result)
        self.assertEqual(result["error"], "No action handler found for the given context.")

    def test_route_action_first_handler_handles(self):
        """
        Tests that the first handler that can handle the context is executed.
        """
        handler1 = MockAction(True, {"result": "handler1"})
        handler2 = MockAction(True, {"result": "handler2"})
        action_handlers = [handler1, handler2]
        context = {"some_key": "some_value"}
        result = route_action(context, action_handlers)
        self.assertEqual(result, {"result": "handler1"})

    def test_route_action_second_handler_handles(self):
        """
        Tests that if the first handler cannot handle, the second one is tried and executed.
        """
        handler1 = MockAction(False, {})
        handler2 = MockAction(True, {"result": "handler2"})
        action_handlers = [handler1, handler2]
        context = {"some_key": "some_value"}
        result = route_action(context, action_handlers)
        self.assertEqual(result, {"result": "handler2"})

    def test_route_action_with_kwargs(self):
        """
        Tests that kwargs are passed correctly to the can_handle and execute methods.
        """
        # Using MagicMock to inspect calls
        handler = MagicMock(spec=BaseAction)
        handler.can_handle.return_value = True
        handler.execute.return_value = {"status": "ok"}

        action_handlers = [handler]
        context = {"user_id": 1}
        kwargs = {"pregunta": "hola", "chat_session_id": "xyz"}

        route_action(context, action_handlers, **kwargs)

        handler.can_handle.assert_called_once_with(context, **kwargs)
        handler.execute.assert_called_once_with(**kwargs)

    def test_route_action_empty_handlers_list(self):
        """
        Tests that an empty list of handlers returns the default error.
        """
        action_handlers = []
        context = {"some_key": "some_value"}
        result = route_action(context, action_handlers)
        self.assertIn("error", result)

if __name__ == '__main__':
    unittest.main()
