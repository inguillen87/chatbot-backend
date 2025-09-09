import unittest
from unittest.mock import patch
from types import SimpleNamespace

from services.municipio_responder import handle_llm_interaction, ConversationState

class LLMHistoryFormattingTest(unittest.TestCase):
    def test_history_is_formatted_before_llm_call(self):
        contexto = {
            "estado_conversacion": ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name,
            "historial_llm_reclamo": [
                {"pregunta_usuario": "hola", "respuesta_ia": "respuesta"}
            ],
            "datos_parciales_llm_reclamo": {}
        }
        context = {"chat_session_uuid": "abc"}
        viewer = SimpleNamespace(nombre="Vecino", direccion=None, telefono=None, email=None)
        owner = SimpleNamespace(municipio_id=1)

        with patch('services.municipio_responder.llamar_llm_con_fallback') as mock_llm:
            mock_llm.return_value = ({"message_body": "ok", "accion_backend": "responder_directamente", "datos_estructura": {}, "pedir_info": None, "botones": []}, {})
            handle_llm_interaction(None, "ubicacion", context, viewer, owner, None, contexto)

            historial_enviado = mock_llm.call_args.kwargs.get('historial')
            self.assertEqual(
                historial_enviado,
                [
                    {"role": "user", "parts": [{"text": "hola"}]},
                    {"role": "model", "parts": [{"text": "respuesta"}]}
                ]
            )

if __name__ == '__main__':
    unittest.main()
