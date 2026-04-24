from __future__ import annotations

import logging
from typing import Any, Dict

from flask import Blueprint, jsonify, request

from services.cohere_stt_bridge import transcribir_audio_cohere
from services.omnichannel_service import registrar_interaccion_omnicanal

omnichannel_bp = Blueprint("omnichannel_bp", __name__)
logger = logging.getLogger(__name__)


@omnichannel_bp.route("/omnichannel/inbound", methods=["POST"])
def inbound_interaction():
    """Webhook único para nuevos canales (Messenger, Telegram, email, IVR)."""

    payload: Dict[str, Any] = {}
    if request.is_json:
        payload.update(request.get_json(silent=True) or {})
    else:
        payload.update(request.form.to_dict())

    # Si viene audio (IVR) intentamos transcribirlo automáticamente
    if "audio" in request.files and not payload.get("mensaje"):
        audio_file = request.files["audio"]
        try:
            transcript = transcribir_audio_cohere(audio_file.read(), audio_file.mimetype)
            if transcript:
                payload["transcripcion"] = transcript
        except Exception as exc:  # pragma: no cover - logging only
            logger.error("Error transcribiendo audio omnicanal: %s", exc, exc_info=True)

    resultado = registrar_interaccion_omnicanal(payload)
    status_code = 200 if resultado.get("exito") else 400
    return jsonify(resultado), status_code
