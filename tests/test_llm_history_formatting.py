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

    def test_handle_llm_interaction_does_not_require_demo_metadata(self):
        contexto = {
            "estado_conversacion": ConversationState.CONVERSACION_GENERAL_LLM.name,
            "historial_conversacion_general_llm": [],
            "datos_parciales_llm_reclamo": {}
        }
        context = {"chat_session_uuid": "abc"}
        viewer = SimpleNamespace(nombre="Vecino", direccion=None, telefono=None, email=None)
        owner = SimpleNamespace(municipio_id=1)

        with patch('services.municipio_responder.llamar_llm_con_fallback') as mock_llm:
            mock_llm.return_value = ({
                "message_body": "ok",
                "accion_backend": "responder_directamente",
                "datos_estructura": {},
                "pedir_info": None,
                "botones": []
            }, {})

            response, updated_context = handle_llm_interaction(
                None,
                "consulta general",
                context,
                viewer,
                owner,
                None,
                contexto
            )

        self.assertIsNotNone(response)
        self.assertIs(updated_context, contexto)
        mock_llm.assert_called_once()

    def test_demo_metadata_is_forwarded_to_llm_user_payload(self):
        contexto = {
            "estado_conversacion": ConversationState.CONVERSACION_GENERAL_LLM.name,
            "historial_conversacion_general_llm": [],
            "datos_parciales_llm_reclamo": {}
        }
        context = {"chat_session_uuid": "abc"}
        viewer = SimpleNamespace(nombre="Vecino", direccion=None, telefono=None, email=None)
        owner = SimpleNamespace(municipio_id=1)
        demo_metadata = {
            "prompt_context": "Información de demo",
            "key": "bodega",
            "display_name": "Demo Bodega",
            "description": "Flujo guiado"
        }

        with patch('services.municipio_responder.llamar_llm_con_fallback') as mock_llm:
            mock_llm.return_value = ({
                "message_body": "ok",
                "accion_backend": "responder_directamente",
                "datos_estructura": {},
                "pedir_info": None,
                "botones": []
            }, {})

            handle_llm_interaction(
                None,
                "consulta demo",
                context,
                viewer,
                owner,
                None,
                contexto,
                demo_metadata=demo_metadata
            )

        usuario_payload = mock_llm.call_args.kwargs.get('usuario')
        self.assertEqual(usuario_payload.get('demo_contexto'), "Información de demo")
        self.assertEqual(usuario_payload.get('demo_key'), "bodega")
        self.assertEqual(usuario_payload.get('demo_display_name'), "Demo Bodega")
        self.assertEqual(usuario_payload.get('demo_description'), "Flujo guiado")

if __name__ == '__main__':
    unittest.main()
