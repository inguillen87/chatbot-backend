from __future__ import annotations

import io
from unittest.mock import patch

from flask import Flask

from routes.omnichannel import omnichannel_bp


def _client():
    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(omnichannel_bp)
    return app.test_client()


def test_generic_omnichannel_inbound_is_fail_closed_before_persistence():
    client = _client()

    with patch(
        "services.omnichannel_service.registrar_interaccion_omnicanal"
    ) as registrar:
        response = client.post(
            "/omnichannel/inbound",
            json={
                "tenant_id": 7,
                "canal": "messenger",
                "contacto": {"email": "victim@example.com"},
                "mensaje": "mensaje inyectado",
            },
        )

    assert response.status_code == 404
    assert response.get_json() == {
        "contract_version": "omnichannel.generic_inbound_disabled.v1",
        "error": {
            "code": "generic_omnichannel_inbound_disabled",
            "message": "Usá un adaptador de entrada autenticado para el proveedor.",
        },
        "retryable": False,
    }
    registrar.assert_not_called()


def test_generic_omnichannel_inbound_rejects_audio_before_transcription():
    client = _client()

    with patch("services.cohere_stt_bridge.transcribir_audio_cohere") as transcribe, patch(
        "services.omnichannel_service.registrar_interaccion_omnicanal"
    ) as registrar:
        response = client.post(
            "/omnichannel/inbound",
            data={
                "tenant_id": "7",
                "canal": "ivr",
                "audio": (io.BytesIO(b"untrusted audio"), "message.ogg"),
            },
            content_type="multipart/form-data",
        )

    assert response.status_code == 404
    transcribe.assert_not_called()
    registrar.assert_not_called()
