from types import SimpleNamespace
from unittest.mock import patch

from services.flows.smalltalk import handle


def test_smalltalk_marks_llm_call_as_whatsapp_realtime():
    chat_context = SimpleNamespace(
        context_data={
            "mensajes_previos_llm_formato": [
                {"role": "user", "parts": [{"text": "hola"}]},
            ]
        }
    )
    owner = SimpleNamespace(id=1, nombre="Municipio", email="muni@test.com")
    viewer = SimpleNamespace(id=2, nombre="Vecino", email="vecino@test.com")

    with patch("services.flows.smalltalk.llamar_llm_con_fallback") as mocked_llm:
        mocked_llm.return_value = (
            {
                "message_body": "Te ayudo.",
                "accion_backend": "responder_directamente",
                "datos_estructura": {},
                "botones": [],
                "pedir_info": None,
            },
            {},
        )

        response = handle(
            "necesito ayuda",
            {
                "app": None,
                "user_obj": owner,
                "viewer_user_obj": viewer,
                "chat_db_context": chat_context,
                "chat_session_uuid": "smalltalk-session",
            },
        )

    assert response["fuente"] == "llm_fallback"
    assert mocked_llm.call_args.kwargs["task_type"] == "whatsapp_realtime"
