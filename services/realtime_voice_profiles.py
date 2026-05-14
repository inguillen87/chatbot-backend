from __future__ import annotations

import os
from typing import Any, Mapping

from services.education_contracts import is_education_tenant


REALTIME_VOICE_CONTRACT_VERSION = "realtime.voice_capabilities.v1"
DEFAULT_REALTIME_VOICE_MODEL = "gpt-realtime"
FALLBACK_REALTIME_VOICE_MODEL = "gpt-realtime"
DEFAULT_REALTIME_TRANSLATION_MODEL = "gpt-realtime-translate"
DEFAULT_REALTIME_VOICE = "marin"
DEFAULT_PHONE_PRIMARY_TRANSPORT = "openai_realtime_sip"
DEFAULT_PHONE_BRIDGE_TRANSPORT = "twilio_media_streams"
DEFAULT_TRANSLATION_TARGET_LANGUAGE = "es"
DEFAULT_SUPPORTED_TRANSLATION_LANGUAGES = (
    {"code": "es", "label": "Espanol"},
    {"code": "en", "label": "English"},
    {"code": "pt", "label": "Portugues"},
)


def _get(mapping: Mapping[str, Any] | None, *keys: str) -> Any:
    if not mapping:
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def resolve_realtime_model(
    cfg: Mapping[str, Any] | None = None,
    app_config: Mapping[str, Any] | None = None,
) -> str:
    return str(
        _get(cfg, "openai_realtime_model", "realtime_voice_model")
        or _get(app_config, "OPENAI_REALTIME_SPEECH_MODEL", "OPENAI_REALTIME_MODEL")
        or os.environ.get("OPENAI_REALTIME_SPEECH_MODEL")
        or os.environ.get("OPENAI_REALTIME_MODEL")
        or DEFAULT_REALTIME_VOICE_MODEL
    )


def resolve_realtime_fallback_model(
    cfg: Mapping[str, Any] | None = None,
    app_config: Mapping[str, Any] | None = None,
) -> str:
    return str(
        _get(cfg, "openai_realtime_fallback_model", "realtime_voice_fallback_model")
        or _get(app_config, "OPENAI_REALTIME_FALLBACK_MODEL")
        or os.environ.get("OPENAI_REALTIME_FALLBACK_MODEL")
        or FALLBACK_REALTIME_VOICE_MODEL
    )


def resolve_realtime_voice(
    cfg: Mapping[str, Any] | None = None,
    app_config: Mapping[str, Any] | None = None,
) -> str:
    return str(
        _get(cfg, "openai_realtime_voice", "realtime_voice")
        or _get(app_config, "OPENAI_REALTIME_VOICE")
        or os.environ.get("OPENAI_REALTIME_VOICE")
        or DEFAULT_REALTIME_VOICE
    )


def _bool_config(
    cfg: Mapping[str, Any] | None,
    app_config: Mapping[str, Any] | None,
    key: str,
    env_key: str,
    *,
    default: bool,
) -> bool:
    value = _get(cfg, key) if cfg else None
    if value is None:
        value = _get(app_config, env_key) if app_config else None
    if value is None:
        value = os.environ.get(env_key)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _language_codes(raw: Any) -> list[str]:
    if isinstance(raw, str):
        candidates = [part.strip().lower() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        candidates = [str(part).strip().lower() for part in raw]
    else:
        candidates = []
    allowed = {"es", "en", "pt"}
    result = [code for code in candidates if code in allowed]
    return list(dict.fromkeys(result)) or [item["code"] for item in DEFAULT_SUPPORTED_TRANSLATION_LANGUAGES]


def build_multilingual_translation_policy(
    cfg: Mapping[str, Any] | None = None,
    app_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    supported_codes = _language_codes(
        _get(cfg, "translation_supported_languages", "supported_languages")
        or _get(app_config, "TRANSLATION_SUPPORTED_LANGUAGES")
        or os.environ.get("TRANSLATION_SUPPORTED_LANGUAGES")
    )
    languages_by_code = {item["code"]: item for item in DEFAULT_SUPPORTED_TRANSLATION_LANGUAGES}
    target_language = str(
        _get(cfg, "translation_target_language", "admin_default_language", "default_language")
        or _get(app_config, "TRANSLATION_TARGET_LANGUAGE")
        or os.environ.get("TRANSLATION_TARGET_LANGUAGE")
        or DEFAULT_TRANSLATION_TARGET_LANGUAGE
    ).strip().lower()
    if target_language not in supported_codes:
        target_language = DEFAULT_TRANSLATION_TARGET_LANGUAGE

    return {
        "enabled": _bool_config(
            cfg,
            app_config,
            "translation_enabled",
            "TRANSLATION_ENABLED",
            default=True,
        ),
        "supported_languages": [languages_by_code[code] for code in supported_codes],
        "target_language": target_language,
        "user_response_mode": str(
            _get(cfg, "translation_user_response_mode")
            or _get(app_config, "TRANSLATION_USER_RESPONSE_MODE")
            or os.environ.get("TRANSLATION_USER_RESPONSE_MODE")
            or "mirror_user_language"
        ),
        "admin_record_language": "es",
        "dedicated_realtime_translation": {
            "model": str(
                _get(cfg, "openai_realtime_translation_model", "realtime_translation_model")
                or _get(app_config, "OPENAI_REALTIME_TRANSLATION_MODEL")
                or os.environ.get("OPENAI_REALTIME_TRANSLATION_MODEL")
                or DEFAULT_REALTIME_TRANSLATION_MODEL
            ),
            "use_when": "live_interpreter_or_caption_mode",
        },
        "channels": {
            "realtime_voice_call": True,
            "whatsapp_audio_note": True,
            "web_audio_note": True,
            "admin_transcript": True,
        },
        "rules": {
            "preserve_original_text": True,
            "normalize_business_fields_to_spanish": True,
            "do_not_translate_names_addresses_or_product_names": True,
            "ask_language_preference_when_unclear": True,
        },
    }


def infer_realtime_voice_vertical(tenant: Any = None, *, tenant_tipo: str | None = None) -> str:
    if tenant is not None and is_education_tenant(tenant):
        return "colegio"
    tipo = (tenant_tipo or getattr(tenant, "tipo", None) or "").strip().lower()
    if tipo == "municipio":
        return "municipio"
    if tipo == "pyme":
        return "pyme"
    return "general"


_BASE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "transferir_humano",
        "description": "Transfiere la llamada a un agente humano cuando el usuario pide ayuda humana, hay riesgo, enojo o una consulta sensible.",
        "parameters": {
            "type": "object",
            "properties": {"motivo": {"type": "string"}},
            "required": ["motivo"],
        },
    },
    {
        "type": "function",
        "name": "finalizar_llamada",
        "description": "Finaliza la llamada cuando el usuario confirma que no necesita nada mas.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

_MUNICIPIO_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "crear_reclamo",
        "description": "Registra un reclamo municipal con descripcion y ubicacion.",
        "parameters": {
            "type": "object",
            "properties": {
                "categoria": {"type": "string", "description": "Categoria del reclamo, por ejemplo alumbrado, limpieza, arbolado, transito o seguridad."},
                "descripcion": {"type": "string", "description": "Resumen breve de lo que paso."},
                "ubicacion": {"type": "string", "description": "Direccion, esquina o referencia del hecho."},
            },
            "required": ["descripcion", "ubicacion"],
        },
    },
    {
        "type": "function",
        "name": "consultar_estado_reclamo",
        "description": "Consulta el estado de un reclamo municipal existente. Usa el ultimo reclamo de la sesion si el usuario dice que quiere ver el que acaba de crear.",
        "parameters": {
            "type": "object",
            "properties": {
                "nro_ticket": {"type": "string", "description": "Numero de reclamo o ticket si el usuario lo menciona."},
                "pin": {"type": "string", "description": "PIN de consulta si el usuario lo menciona."},
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "consultar_tramite",
        "description": "Busca informacion de un tramite municipal, turno o requisito sin crear reclamo.",
        "parameters": {
            "type": "object",
            "properties": {
                "nombre_tramite": {"type": "string", "description": "Nombre del tramite, por ejemplo licencia, habilitacion, libre deuda o poda."},
            },
            "required": ["nombre_tramite"],
        },
    },
]

_PYME_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "consultar_producto",
        "description": "Busca un producto o servicio en el catalogo antes de vender o armar un pedido.",
        "parameters": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre, descripcion o necesidad del producto/servicio."}
            },
            "required": ["nombre"],
        },
    },
    {
        "type": "function",
        "name": "crear_pedido",
        "description": "Registra un pedido de venta para una pyme.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {"type": "string", "description": "Productos, cantidades y variantes confirmadas."},
                "direccion_entrega": {"type": "string", "description": "Direccion de entrega si aplica."},
            },
            "required": ["items"],
        },
    },
    {
        "type": "function",
        "name": "consultar_estado_pedido",
        "description": "Consulta el estado de un pedido de venta existente. Usa el ultimo pedido de la sesion si corresponde.",
        "parameters": {
            "type": "object",
            "properties": {
                "nro_pedido": {"type": "string", "description": "Numero de pedido si el usuario lo menciona."},
            },
            "required": [],
        },
    },
]

_COLEGIO_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "crear_caso_escolar",
        "description": "Registra un caso escolar para secretaria, asistencia, inasistencias, convivencia, documentacion, pagos, admisiones o mantenimiento.",
        "parameters": {
            "type": "object",
            "properties": {
                "case_type": {
                    "type": "string",
                    "description": "Tipo de caso: secretaria, preceptoria, inasistencia, comunicados, agenda_academica, documentacion, cobranza, admisiones, convivencia, mantenimiento, tecnologia, transporte o comedor.",
                },
                "asunto": {"type": "string", "description": "Titulo breve del caso."},
                "descripcion": {"type": "string", "description": "Resumen del pedido de la familia, alumno o personal."},
                "alumno": {"type": "string", "description": "Nombre del alumno si el usuario lo menciona."},
                "curso": {"type": "string", "description": "Curso, sala, grado o anio si se menciona."},
                "fecha": {"type": "string", "description": "Fecha relevante para asistencia, agenda o tramite."},
                "ubicacion": {"type": "string", "description": "Sede, puerta, aula o referencia si corresponde."},
                "sensitivity_level": {"type": "string", "description": "normal, private, sensitive o critical."},
            },
            "required": ["descripcion"],
        },
    },
    {
        "type": "function",
        "name": "consultar_caso_escolar",
        "description": "Consulta el estado de un caso escolar existente. Usa el ultimo caso de la sesion si el usuario no recuerda el numero.",
        "parameters": {
            "type": "object",
            "properties": {
                "school_case_id": {"type": "string", "description": "Numero de caso escolar si el usuario lo menciona."},
            },
            "required": [],
        },
    },
]


def build_realtime_voice_tools(vertical: str | None) -> list[dict[str, Any]]:
    normalized = (vertical or "general").strip().lower()
    if normalized == "municipio":
        return [*_MUNICIPIO_TOOLS, *_BASE_TOOLS]
    if normalized == "pyme":
        return [*_PYME_TOOLS, *_BASE_TOOLS]
    if normalized == "colegio":
        return [*_COLEGIO_TOOLS, *_BASE_TOOLS]
    return [*_MUNICIPIO_TOOLS, *_PYME_TOOLS, *_COLEGIO_TOOLS, *_BASE_TOOLS]


def tool_names_for_vertical(vertical: str | None) -> list[str]:
    return [tool["name"] for tool in build_realtime_voice_tools(vertical)]


def build_realtime_voice_instructions(
    *,
    tenant_name: str,
    vertical: str,
    user_name: str | None = None,
    user_address: str | None = None,
    translation_policy: Mapping[str, Any] | None = None,
) -> str:
    known = []
    if user_name:
        known.append(f"Nombre conocido: {user_name}.")
    if user_address:
        known.append(f"Direccion guardada: {user_address}.")
    known_data = " ".join(known) if known else "Nombre y datos del usuario aun no confirmados."

    translation_policy = translation_policy or build_multilingual_translation_policy()
    supported_codes = ", ".join(
        item.get("code", "")
        for item in translation_policy.get("supported_languages", [])
        if isinstance(item, Mapping) and item.get("code")
    ) or "es, en, pt"
    target_language = translation_policy.get("target_language") or DEFAULT_TRANSLATION_TARGET_LANGUAGE
    multilingual_rules = ""
    if translation_policy.get("enabled", True):
        multilingual_rules = (
            f"Idiomas soportados: {supported_codes}. "
            "Si el usuario habla en ingles o portugues, entendelo sin pedir que cambie de idioma. "
            "Responde en el idioma del usuario, salvo que pida traduccion o que ya haya preferencia guardada. "
            f"Para herramientas, tickets, pedidos, casos escolares y panel admin, normaliza categoria, resumen y estado al idioma operativo '{target_language}'. "
            "Conserva nombres propios, direcciones, productos, cursos, codigos y telefonos en su forma original. "
            "Si un admin o usuario pide traduccion, entrega una version breve en ambos idiomas relevantes. "
        )

    base = (
        f"Sos el asistente telefonico realtime de {tenant_name}. "
        "Este canal es voz nativa en tiempo real: escucha, razona y responde en audio sin pedirle al usuario que escriba. "
        "Habla en espanol argentino neutro, con vos, tono cercano, profesional y muy claro. "
        "Frases cortas: una o dos oraciones por turno. Deja hablar e interrumpe con naturalidad si el usuario corrige. "
        f"{known_data} "
        f"{multilingual_rules}"
        "No recites menus completos salvo que el usuario pida opciones. En llamada, propone el proximo paso mas probable y pregunta un solo dato. "
        "No inventes tickets, pedidos, pagos, turnos, stock ni confirmaciones. Solo confirma cuando una herramienta devuelve resultado. "
        "Si falta un dato obligatorio, pedi solo ese dato. Si el usuario ya dio varios datos, no los vuelvas a pedir. "
        "Si hay enojo, urgencia, datos sensibles, riesgo o pedido explicito de persona, usa transferir_humano. "
        "Cuando el usuario diga listo, nada mas, gracias o perfecto, despedi breve y usa finalizar_llamada. "
    )

    vertical = (vertical or "general").strip().lower()
    if vertical == "municipio":
        return base + (
            "Perfil municipio: ayuda con reclamos, seguimiento, consultas generales y tramites. "
            "Para reclamos, separa descripcion de ubicacion. Inferi categoria si la descripcion es clara. "
            "Ejemplos: alumbrado, arbolado, limpieza, baches, transito, seguridad, zoonosis, habilitaciones y turnos. "
            "Si pide estado de reclamo, usa consultar_estado_reclamo. Si pide licencia, turno o requisitos, usa consultar_tramite. "
            "Al crear reclamo, avisa que enviaremos el comprobante por WhatsApp y que puede responder con foto o ubicacion."
        )
    if vertical == "pyme":
        return base + (
            "Perfil pyme: vende de forma consultiva, entiende necesidad, recomienda opciones, consulta producto si hace falta y crea pedido solo con items confirmados. "
            "No presiones: ayuda a elegir, confirma variantes, cantidad, entrega/retiro y datos de contacto. "
            "Si pide estado de compra o entrega, usa consultar_estado_pedido. "
            "Si no hay stock o precio confiable, no inventes; ofrece derivar o dejar consulta."
        )
    if vertical == "colegio":
        return base + (
            "Perfil colegio: atende familias, alumnos y personal con secretaria, asistencia, inasistencias, comunicados, agenda, documentacion, pagos, admisiones, convivencia y mantenimiento. "
            "Crea caso escolar cuando haya una consulta accionable. Para inasistencia pedi alumno, curso, fecha y motivo si faltan. "
            "Si pide seguimiento de un caso, usa consultar_caso_escolar. "
            "Para convivencia, salud, retiro de alumnos o datos privados, cuida la privacidad y deriva a humano si corresponde. "
            "Si menciona certificados, comprobantes o autorizaciones, avisa que puede enviar imagen o archivo por WhatsApp luego del llamado."
        )
    return base + (
        "Perfil general SaaS: identifica si la necesidad es municipal, comercial o escolar, y usa la herramienta adecuada. "
        "Mantene la conversacion orientada a resolver y capturar un lead util."
    )


def build_realtime_voice_capabilities(
    tenant: Any = None,
    cfg: Mapping[str, Any] | None = None,
    app_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    vertical = infer_realtime_voice_vertical(tenant)
    model = resolve_realtime_model(cfg, app_config)
    fallback_model = resolve_realtime_fallback_model(cfg, app_config)
    voice = resolve_realtime_voice(cfg, app_config)
    translation_policy = build_multilingual_translation_policy(cfg, app_config)

    return {
        "contract_version": REALTIME_VOICE_CONTRACT_VERSION,
        "provider": "openai_realtime",
        "recommended_model": model,
        "fallback_model": fallback_model,
        "voice": voice,
        "active_vertical": vertical,
        "native_speech_to_speech": True,
        "avoid_external_stt_tts_loop": True,
        "translation": translation_policy,
        "transports": {
            "browser": "webrtc",
            "server": "websocket",
            "phone_primary": str(
                _get(cfg, "openai_realtime_phone_primary_transport")
                or _get(app_config, "OPENAI_REALTIME_PHONE_PRIMARY_TRANSPORT")
                or os.environ.get("OPENAI_REALTIME_PHONE_PRIMARY_TRANSPORT")
                or DEFAULT_PHONE_PRIMARY_TRANSPORT
            ),
            "phone_bridge": str(
                _get(cfg, "openai_realtime_phone_bridge_transport")
                or _get(app_config, "OPENAI_REALTIME_PHONE_BRIDGE_TRANSPORT")
                or os.environ.get("OPENAI_REALTIME_PHONE_BRIDGE_TRANSPORT")
                or DEFAULT_PHONE_BRIDGE_TRANSPORT
            ),
            "legacy_phone_fallback": "twilio_gather_tts",
            "sip_ready": True,
            "media_streams_ready": True,
        },
        "cost_latency_policy": {
            "primary_voice_runtime": "openai_realtime_native_audio",
            "external_tts": "fallback_only",
            "external_stt": "fallback_only",
            "reason": "native_speech_to_speech_reduces_round_trips",
        },
        "media": {
            "input": ["audio", "text", "image_context"],
            "output": ["audio", "text"],
            "phone_audio_format": "g711_ulaw",
            "browser_audio_format": "pcm16",
        },
        "features": {
            "barge_in": True,
            "semantic_vad": True,
            "server_vad": False,
            "tool_calling": True,
            "whatsapp_followup": True,
            "post_call_receipt": True,
            "human_handoff": True,
        },
        "verticals": {
            "municipio": {
                "label": "Municipios",
                "actions": tool_names_for_vertical("municipio"),
                "intents": ["crear_reclamo", "consultar_estado_reclamo", "consultar_tramite", "derivar_humano"],
                "frontend_prompts": ["Iniciar reclamo por llamada", "Consultar estado", "Consultar tramite", "Hablar con operador"],
            },
            "pyme": {
                "label": "PyMEs",
                "actions": tool_names_for_vertical("pyme"),
                "intents": ["consulta_producto", "venta_consultiva", "crear_pedido", "consultar_estado_pedido", "derivar_humano"],
                "frontend_prompts": ["Llamar para comprar", "Consultar disponibilidad", "Tomar pedido por voz", "Consultar pedido"],
            },
            "colegio": {
                "label": "Colegios",
                "actions": tool_names_for_vertical("colegio"),
                "intents": ["inasistencia", "secretaria", "comunicados", "admisiones", "convivencia", "consultar_caso_escolar"],
                "frontend_prompts": ["Justificar inasistencia", "Consultar secretaria", "Consultar caso", "Hablar con el colegio"],
            },
        },
        "frontend": {
            "session_endpoint": "/api/public/realtime/session",
            "capabilities_endpoint": "/api/public/realtime/voice-capabilities",
            "phone_webhook": "/twilio/voice/inbound",
            "legacy_phone_webhook": "/voice/welcome",
            "show_call_cta": True,
            "show_captions": True,
            "show_handoff_state": True,
            "show_whatsapp_receipt_state": True,
        },
    }
