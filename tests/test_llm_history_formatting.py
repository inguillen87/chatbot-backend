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

        with patch('services.municipio_responder.llamar_gemini') as mock_llm:
            mock_llm.return_value = ({"message_body": "ok", "accion_backend": "responder_directamente", "datos_estructura": {}, "pedir_info": None, "botones": []}, {})
            handle_llm_interaction(None, "ubicacion", context, viewer, owner, None, contexto)

            historial_enviado = mock_llm.call_args.kwargs.get('historial')
            self.assertEqual(mock_llm.call_args.kwargs.get('task_type'), "reclamo")
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

        with patch('services.municipio_responder.llamar_gemini') as mock_llm:
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
        self.assertEqual(mock_llm.call_args.kwargs.get('task_type'), "whatsapp_realtime")

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

        with patch('services.municipio_responder.llamar_gemini') as mock_llm:
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

    def test_canonical_whatsapp_modality_is_forwarded_without_raw_provider_data(self):
        contexto = {
            "estado_conversacion": ConversationState.CONVERSACION_GENERAL_LLM.name,
            "historial_conversacion_general_llm": [],
            "datos_parciales_llm_reclamo": {},
        }
        inbound_context = {
            "contract_version": "whatsapp.inbound_content.v1",
            "kind": "emoji",
            "durable_message_kind": "text",
            "is_language_input": True,
            "has_media": False,
            "media_mime_type": None,
            "evidence_policy": "not_applicable",
            "reason": None,
        }
        context = {
            "chat_session_uuid": "abc",
            "whatsapp_inbound_content": inbound_context,
        }
        viewer = SimpleNamespace(nombre="Vecino", direccion=None, telefono=None, email=None)
        owner = SimpleNamespace(municipio_id=1)

        with patch('services.municipio_responder.llamar_gemini') as mock_llm:
            mock_llm.return_value = ({
                "message_body": "ok",
                "accion_backend": "responder_directamente",
                "datos_estructura": {},
                "pedir_info": None,
                "botones": [],
            }, {})

            handle_llm_interaction(
                None,
                "👍🏽",
                context,
                viewer,
                owner,
                None,
                contexto,
            )

        usuario_payload = mock_llm.call_args.kwargs["usuario"]
        self.assertEqual(usuario_payload["entrada_whatsapp"], inbound_context)
        self.assertNotIn("provider_url", usuario_payload["entrada_whatsapp"])
        self.assertNotIn("body", usuario_payload["entrada_whatsapp"])

if __name__ == '__main__':
    unittest.main()
