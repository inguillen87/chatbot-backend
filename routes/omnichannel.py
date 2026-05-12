from __future__ import annotations

import logging
from typing import Any, Dict

from flask import Blueprint, jsonify, request

from services.cohere_stt_bridge import transcribir_audio_cohere
from services.omnichannel_service import (
    get_conversation_timeline,
    registrar_interaccion_omnicanal,
    resolve_or_create_conversation,
)

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


@omnichannel_bp.route("/api/conversations/resolve", methods=["POST"])
def resolve_conversation():
    """Resolve or create a canonical omnichannel conversation."""

    payload = request.get_json(silent=True) or {}
    channel = str(payload.get("channel") or "").strip().lower()
    if not channel:
        return jsonify({"error": "channel_requerido"}), 400

    conversation = resolve_or_create_conversation(
        tenant_id=payload.get("tenant_id"),
        channel=channel,
        anon_id=payload.get("anon_id"),
        external_key=payload.get("external_key"),
        legacy_chat_session_id=payload.get("chat_session_id"),
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
    )
    if not conversation:
        return jsonify({"error": "conversation_no_disponible"}), 404

    return jsonify({"conversation": conversation.to_dict()}), 200


@omnichannel_bp.route("/api/conversations/<string:conversation_id>/timeline", methods=["GET"])
def get_conversation_timeline_endpoint(conversation_id: str):
    timeline = get_conversation_timeline(conversation_id)
    if not timeline:
        return jsonify({"error": "conversation_no_encontrada"}), 404
    return jsonify(timeline), 200
