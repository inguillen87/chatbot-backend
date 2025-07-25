import unittest
from services.chat_orchestrator import ChatOrchestrator

class TestErrorActionHandler(unittest.TestCase):
    def test_error_action(self):
        orchestrator = ChatOrchestrator(global_context={})
        llm_output = {
            'accion_backend': 'error',
            'respuesta_usuario': 'No pude procesar tu solicitud',
            'datos_estructura': {}
        }
        result = orchestrator.execute_action(llm_output)
        self.assertFalse(result['success'])
        self.assertEqual(result['message_to_user'], 'No pude procesar tu solicitud')
        self.assertEqual(result['executed_action_handler'], 'ErrorActionHandler')

if __name__ == '__main__':
    unittest.main()
