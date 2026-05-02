from __future__ import annotations

from typing import Any
import time
import unicodedata


EDUCATION_PROFILE_CONTRACT_VERSION = "education.profile.v1"
EDUCATION_QUICK_MENU_CONTRACT_VERSION = "education.quick_menu.v1"
EDUCATION_ADMIN_MENU_CONTRACT_VERSION = "education.admin_menu.v1"
EDUCATION_WHATSAPP_PLAYBOOK_CONTRACT_VERSION = "education.whatsapp_playbook.v1"
EDUCATION_CASE_INTAKE_CONTRACT_VERSION = "education.case_intake.v1"


def _repair_mojibake(value: str) -> str:
    try:
        repaired = value.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return repaired or value


def fold_text(value: Any) -> str:
    text = _repair_mojibake(str(value or ""))
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return " ".join(text.lower().split())


def _owner_rubro_label(tenant: Any) -> str:
    owner = getattr(tenant, "pyme", None) or getattr(tenant, "municipio", None)
    rubro = getattr(owner, "rubro", None) if owner else None
    return str(getattr(rubro, "nombre", None) or "")


def _education_capabilities(tenant: Any) -> dict[str, Any]:
    capabilities = getattr(tenant, "capabilities_json", None)
    if not isinstance(capabilities, dict):
        return {}
    education = capabilities.get("education")
    return education if isinstance(education, dict) else {}


def education_institution_type(tenant: Any = None, *, text: str | None = None) -> str:
    capabilities = _education_capabilities(tenant)
    configured = fold_text(capabilities.get("institution_type"))
    if configured in {"public", "private", "general"}:
        return configured

    haystack = " ".join(
        [
            fold_text(text),
            fold_text(getattr(tenant, "nombre", None)),
            fold_text(getattr(tenant, "subvertical", None)),
            fold_text(_owner_rubro_label(tenant)),
        ]
    )
    if "privad" in haystack:
        return "private"
    if any(token in haystack for token in ("public", "estatal", "municipal", "provincial")):
        return "public"
    return "general"


def is_education_tenant(tenant: Any) -> bool:
    if not tenant:
        return False

    capabilities = _education_capabilities(tenant)
    if capabilities.get("enabled") is True:
        return True

    vertical = fold_text(getattr(tenant, "vertical", None))
    if vertical in {"educacion", "education", "colegios", "schools"}:
        return True

    haystack = " ".join(
        [
            fold_text(getattr(tenant, "nombre", None)),
            fold_text(getattr(tenant, "subvertical", None)),
            fold_text(_owner_rubro_label(tenant)),
        ]
    )
    keywords = (
        "colegio",
        "escuela",
        "educacion",
        "instituto",
        "jardin",
        "campus",
        "alumno",
        "familia escolar",
        "preceptoria",
    )
    return any(keyword in haystack for keyword in keywords)


def education_module_ids(tenant: Any = None) -> list[str]:
    configured = _education_capabilities(tenant).get("modules")
    if isinstance(configured, list):
        modules = [str(item).strip() for item in configured if str(item).strip()]
        if modules:
            return modules
    return [
        "family_context",
        "attendance",
        "asistencia",
        "communications",
        "academic_agenda",
        "agenda_academica",
        "secretary_tickets",
        "tramites_secretaria",
        "admissions",
        "billing",
        "wellbeing",
    ]


def build_education_profile(tenant: Any = None, *, rubro_label: str | None = None) -> dict[str, Any]:
    is_education = is_education_tenant(tenant) or any(
        keyword in fold_text(rubro_label)
        for keyword in ("colegio", "escuela", "educacion", "instituto", "jardin")
    )
    if not is_education:
        return {"is_education": False}

    tenant_tipo = str(getattr(tenant, "tipo", None) or "pyme").strip().lower() or "pyme"
    endpoint = "/ask/municipio" if tenant_tipo == "municipio" else "/ask/pyme"
    institution_type = education_institution_type(tenant, text=rubro_label)

    return {
        "contract_version": EDUCATION_PROFILE_CONTRACT_VERSION,
        "is_education": True,
        "vertical": "educacion",
        "subvertical": getattr(tenant, "subvertical", None) or "colegio",
        "institution_type": institution_type,
        "modules": education_module_ids(tenant),
        "surfaces": ["widget", "whatsapp", "admin_panel", "tenant_profile", "demo"],
        "chat_endpoint": endpoint,
        "ticket_type": "pyme" if tenant_tipo != "municipio" else "municipio",
        "features": [
            "asistencia_y_justificaciones",
            "comunicados_familiares",
            "agenda_academica",
            "tramites_secretaria",
            "documentacion_y_certificados",
            "pagos_admisiones_si_aplica",
            "convivencia_con_handoff",
        ],
        "media_inputs": {
            "text": True,
            "audio": True,
            "image": True,
            "location": True,
            "file": True,
        },
        "admin_endpoints": {
            "capabilities": "/api/v1/education/tenant/capabilities",
            "admin_menu": "/api/v1/education/admin/menu",
            "taxonomy": "/api/v1/education/cases/taxonomy",
            "family_context": "/api/v1/education/me/family-context",
            "cases": "/api/v1/education/cases",
        },
        "public_endpoints": {
            "guardian_lookup": "/api/v1/education/guardians/lookup",
            "guardian_verify": "/api/v1/education/guardians/verify",
        },
    }


def education_case_taxonomy() -> list[dict[str, Any]]:
    return [
        {
            "key": "secretaria",
            "label": "Secretaria",
            "description": "Constancias, certificados, documentacion y tramites generales.",
            "sensitivity_level": "normal",
            "queue": "secretaria",
        },
        {
            "key": "preceptoria",
            "label": "Preceptoria",
            "description": "Asistencia, inasistencias, llegadas tarde y seguimiento del curso.",
            "sensitivity_level": "normal",
            "queue": "preceptoria",
        },
        {
            "key": "inasistencia",
            "label": "Inasistencia",
            "description": "Justificaciones, certificados medicos y avisos de ausencia.",
            "sensitivity_level": "normal",
            "queue": "preceptoria",
        },
        {
            "key": "comunicados",
            "label": "Comunicados",
            "description": "Consultas sobre mensajes institucionales enviados a familias.",
            "sensitivity_level": "normal",
            "queue": "comunicaciones",
        },
        {
            "key": "agenda_academica",
            "label": "Agenda academica",
            "description": "Fechas, reuniones, actos, evaluaciones y actividades.",
            "sensitivity_level": "normal",
            "queue": "academica",
        },
        {
            "key": "documentacion",
            "label": "Documentacion",
            "description": "Certificados, autorizaciones, legajos y archivos adjuntos.",
            "sensitivity_level": "normal",
            "queue": "secretaria",
        },
        {
            "key": "cobranza",
            "label": "Tesoreria",
            "description": "Cuotas, pagos, comprobantes, becas y deuda.",
            "sensitivity_level": "private",
            "queue": "tesoreria",
        },
        {
            "key": "admisiones",
            "label": "Admisiones",
            "description": "Inscripciones, vacantes, entrevistas y documentacion de ingreso.",
            "sensitivity_level": "normal",
            "queue": "admisiones",
        },
        {
            "key": "convivencia",
            "label": "Convivencia",
            "description": "Situaciones de convivencia, bullying, cuidado y derivacion adulta.",
            "sensitivity_level": "sensitive",
            "queue": "bienestar",
            "requires_handoff": True,
        },
        {
            "key": "mantenimiento",
            "label": "Mantenimiento",
            "description": "Infraestructura, limpieza, seguridad edilicia y espacios del colegio.",
            "sensitivity_level": "normal",
            "queue": "operaciones",
        },
        {
            "key": "tecnologia",
            "label": "Tecnologia",
            "description": "Campus virtual, accesos, dispositivos, plataformas y soporte tecnico.",
            "sensitivity_level": "normal",
            "queue": "tecnologia",
        },
        {
            "key": "transporte",
            "label": "Transporte",
            "description": "Recorridos, retiro, autorizaciones, ubicacion y cambios de logistica.",
            "sensitivity_level": "private",
            "queue": "operaciones",
        },
        {
            "key": "comedor",
            "label": "Comedor",
            "description": "Menu, alergias informadas, viandas y consultas de comedor.",
            "sensitivity_level": "private",
            "queue": "comedor",
        },
    ]


def education_taxonomy_dict() -> dict[str, str]:
    return {item["key"]: item["label"] for item in education_case_taxonomy()}


def education_quick_menu(
    tenant: Any = None,
    *,
    institution_type: str | None = None,
    surface: str = "widget",
) -> list[dict[str, Any]]:
    institution = institution_type or education_institution_type(tenant)
    shared: list[dict[str, Any]] = [
        {
            "id": "menu_asistencia",
            "label": "Asistencia",
            "intent": "asistencia_alumno",
            "category": "preceptoria",
            "requires_verification": True,
        },
        {
            "id": "menu_inasistencia",
            "label": "Justificar inasistencia",
            "intent": "justificar_inasistencia",
            "category": "inasistencia",
            "requires_verification": True,
            "accepted_media": ["image", "file", "audio", "text"],
        },
        {
            "id": "menu_comunicados",
            "label": "Comunicados",
            "intent": "comunicados_familias",
            "category": "comunicados",
        },
        {
            "id": "menu_agenda",
            "label": "Agenda academica",
            "intent": "agenda_academica",
            "category": "agenda_academica",
        },
        {
            "id": "menu_documentacion",
            "label": "Documentacion",
            "intent": "documentacion_certificados",
            "category": "documentacion",
            "accepted_media": ["image", "file", "text"],
        },
        {
            "id": "menu_tramites",
            "label": "Tramites secretaria",
            "intent": "tramites_secretaria",
            "category": "secretaria",
        },
        {
            "id": "menu_convivencia",
            "label": "Convivencia",
            "intent": "convivencia_escolar",
            "category": "convivencia",
            "sensitivity_level": "sensitive",
            "requires_handoff": True,
        },
        {
            "id": "menu_humano",
            "label": "Hablar con secretaria",
            "intent": "derivar_humano",
            "category": "secretaria",
        },
    ]

    if institution in {"private", "general"}:
        shared.insert(
            6,
            {
                "id": "menu_pagos",
                "label": "Pagos y cuotas",
                "intent": "pagos_cuotas",
                "category": "cobranza",
                "requires_verification": True,
                "accepted_media": ["image", "file", "text"],
            },
        )
        shared.insert(
            7,
            {
                "id": "menu_admisiones",
                "label": "Admisiones",
                "intent": "admisiones_colegio",
                "category": "admisiones",
            },
        )

    surfaces = ["widget", "whatsapp", "admin_panel", "tenant_profile"]
    menu = []
    for item in shared:
        enriched = {
            **item,
            "contract_version": EDUCATION_QUICK_MENU_CONTRACT_VERSION,
            "institution_type": institution,
            "vertical": "educacion",
            "surface": surface,
            "channel_support": {
                "widget": "widget" in surfaces,
                "whatsapp": "whatsapp" in surfaces,
                "panel": "admin_panel" in surfaces,
            },
        }
        menu.append(enriched)
    return menu


def education_menu_item_for_intent(intent: str | None, tenant: Any = None) -> dict[str, Any] | None:
    normalized = str(intent or "").strip().lower()
    if not normalized:
        return None
    for item in education_quick_menu(tenant, surface="whatsapp"):
        if normalized in {item.get("intent"), item.get("id")}:
            return item
    return None


def education_intents(tenant: Any = None) -> set[str]:
    values: set[str] = set()
    for item in education_quick_menu(tenant, surface="whatsapp"):
        values.add(str(item.get("intent") or ""))
        values.add(str(item.get("id") or ""))
    return {item for item in values if item}


def education_prompt_for_intent(intent: str | None, tenant: Any = None) -> dict[str, Any]:
    item = education_menu_item_for_intent(intent, tenant) or {}
    category = item.get("category") or "secretaria"
    prompts = {
        "asistencia_alumno": "Decime nombre y apellido del alumno, curso y fecha. Si preferis, manda audio.",
        "justificar_inasistencia": "Mandame alumno, curso, fecha y el motivo. Podes adjuntar foto o PDF del certificado.",
        "comunicados_familias": "Contame sobre que comunicado queres consultar y de que curso o familia.",
        "agenda_academica": "Decime curso, fecha o actividad que queres revisar.",
        "documentacion_certificados": "Indica que documento necesitas o adjunta la autorizacion/certificado.",
        "tramites_secretaria": "Contame el tramite y para que alumno o familia corresponde.",
        "pagos_cuotas": "Contame la consulta de pago o adjunta comprobante. No compartas datos completos de tarjeta.",
        "admisiones_colegio": "Decime nivel, sala/grado/ano y datos de contacto para admisiones.",
        "convivencia_escolar": "Contame que paso con el mayor detalle posible. Si hay riesgo inmediato, avisa al colegio o emergencias.",
        "derivar_humano": "Te puedo derivar con secretaria. Contame el motivo y mejor horario de contacto.",
    }
    body = prompts.get(str(item.get("intent") or intent), "Contame el detalle y lo dejo encaminado.")
    return {
        "category": category,
        "message_body": body,
        "options_list": [
            {"texto": "Enviar detalle", "action_id": "education_send_detail"},
            {"texto": "Hablar con secretaria", "action_id": "derivar_humano"},
            {"texto": "Menu colegio", "action_id": "menu_principal"},
        ],
    }


def build_education_whatsapp_playbook(tenant: Any = None) -> dict[str, Any]:
    institution = education_institution_type(tenant)
    return {
        "contract_version": EDUCATION_WHATSAPP_PLAYBOOK_CONTRACT_VERSION,
        "enabled": True,
        "vertical": "educacion",
        "institution_type": institution,
        "welcome": {
            "assistant_label": "Asistente del colegio",
            "message": "Hola, soy el asistente del colegio. Puedo ayudarte con asistencia, comunicados, agenda, tramites y secretaria.",
            "menu_action": "menu_colegio",
        },
        "quick_menu": education_quick_menu(tenant, surface="whatsapp"),
        "starter_messages": [
            "Quiero justificar una inasistencia",
            "Te mando una foto del certificado medico",
            "Necesito ver comunicados del curso",
            "Quiero consultar pagos o admisiones",
        ],
        "media_intelligence": {
            "text": {
                "enabled": True,
                "expected_behavior": "Detectar alumno, curso, fecha, motivo y area; pedir solo lo faltante.",
            },
            "audio": {
                "enabled": True,
                "transcribe": True,
                "intents": ["justificar_inasistencia", "consulta_familia", "derivar_humano"],
                "expected_behavior": "Transcribir, resumir y confirmar antes de crear un caso escolar.",
            },
            "image": {
                "enabled": True,
                "intents": ["certificado_medico", "comprobante_pago", "autorizacion_firmada", "evidencia_convivencia"],
                "expected_behavior": "Leer evidencia, clasificar el tramite y adjuntar al ticket escolar.",
            },
            "location": {
                "enabled": True,
                "intents": ["transporte_escolar", "sede_mas_cercana", "retiro_alumno", "mantenimiento"],
                "expected_behavior": "Confirmar sede o referencia antes de persistir coordenadas.",
            },
            "file": {
                "enabled": True,
                "intents": ["documentacion_certificados", "pagos_cuotas", "admisiones_colegio"],
                "expected_behavior": "Guardar adjunto y abrir seguimiento con secretaria si hace falta.",
            },
        },
        "routing_rules": [
            {"when": "intent == convivencia_escolar", "action": "handoff_required", "queue": "bienestar"},
            {"when": "media.image && intent in certificados", "action": "attach_to_school_case", "queue": "secretaria"},
            {"when": "location.shared", "action": "confirm_location_then_ticket", "queue": "operaciones"},
        ],
        "safety": {
            "avoid_public_sensitive_data": True,
            "requires_guardian_verification_for": ["asistencia_alumno", "pagos_cuotas", "documentacion_certificados"],
            "handoff_required_for": ["convivencia_escolar", "retirar_alumno", "salud_urgente"],
        },
    }


def build_education_admin_menu(tenant: Any = None) -> dict[str, Any]:
    profile = build_education_profile(tenant)
    tenant_slug = getattr(tenant, "slug", None)
    return {
        "contract_version": EDUCATION_ADMIN_MENU_CONTRACT_VERSION,
        "tenant_id": getattr(tenant, "id", None),
        "tenant_slug": tenant_slug,
        "vertical": "educacion",
        "profile": profile,
        "quick_menu": education_quick_menu(tenant, surface="admin_panel"),
        "taxonomy": education_case_taxonomy(),
        "panel_sections": [
            {
                "id": "education_overview",
                "label": "Resumen colegio",
                "route": "/educacion",
                "capability": "education.overview",
                "widgets": ["tenant_health", "open_cases", "attendance_alerts"],
            },
            {
                "id": "family_context",
                "label": "Familias y alumnos",
                "route": "/educacion/familias",
                "endpoint": "/api/v1/education/me/family-context",
                "capability": "education.family_context",
            },
            {
                "id": "school_cases",
                "label": "Casos escolares",
                "route": "/educacion/casos",
                "endpoint": "/api/v1/education/cases",
                "capability": "education.cases",
            },
            {
                "id": "school_structure",
                "label": "Sedes y cursos",
                "route": "/educacion/estructura",
                "endpoints": ["/api/v1/education/schools", "/api/v1/education/campuses", "/api/v1/education/sections"],
                "capability": "education.structure",
            },
            {
                "id": "whatsapp_school",
                "label": "WhatsApp colegio",
                "route": "/integraciones/whatsapp",
                "endpoint": "/api/v1/education/whatsapp/playbook",
                "capability": "education.whatsapp",
            },
        ],
        "profile_fields": [
            {"id": "vertical", "label": "Vertical", "value": "educacion", "editable": False},
            {"id": "subvertical", "label": "Tipo de institucion", "value": getattr(tenant, "subvertical", None) or "colegio"},
            {"id": "institution_type", "label": "Gestion", "value": education_institution_type(tenant)},
            {"id": "modules", "label": "Modulos activos", "value": education_module_ids(tenant)},
        ],
        "whatsapp_playbook": build_education_whatsapp_playbook(tenant),
    }


def build_education_whatsapp_menu_payload(
    tenant: Any = None,
    *,
    profile_name: str | None = None,
    reduced: bool = False,
) -> dict[str, Any]:
    playbook = build_education_whatsapp_playbook(tenant)
    tenant_name = getattr(tenant, "nombre", None) or "el colegio"
    name_part = f", {profile_name}" if profile_name else ""
    body = (
        f"Hola{name_part}. Soy el asistente de {tenant_name}. "
        "Puedo ayudarte con asistencia, comunicados, agenda, documentacion o secretaria."
    )
    menu_items = playbook.get("quick_menu") or []
    if reduced:
        menu_items = menu_items[:5]
    options = [
        {
            "id": item.get("id"),
            "texto": item.get("label"),
            "action_id": item.get("intent"),
            "category_name": item.get("category"),
            "requires_verification": item.get("requires_verification", False),
        }
        for item in menu_items
    ]
    return {
        "message_body": body,
        "message_type": "interactive_list" if len(options) > 3 else "interactive_buttons",
        "options_list": options,
        "fuente": "education_whatsapp_menu",
        "generar_audio": True,
        "audio_text": "Hola. Soy el asistente del colegio. Elegi una opcion del menu o contame que necesitas.",
        "education_context": {
            "contract_version": EDUCATION_CASE_INTAKE_CONTRACT_VERSION,
            "vertical": "educacion",
            "institution_type": playbook.get("institution_type"),
            "media_intelligence": playbook.get("media_intelligence"),
        },
    }


def build_education_pending_case(intent: str, selected_option: dict[str, Any] | None = None) -> dict[str, Any]:
    item = education_menu_item_for_intent(intent) or selected_option or {}
    return {
        "contract_version": EDUCATION_CASE_INTAKE_CONTRACT_VERSION,
        "intent": intent,
        "category": item.get("category") or item.get("category_name") or "secretaria",
        "label": item.get("label") or item.get("texto") or "Consulta escolar",
        "requires_verification": bool(item.get("requires_verification")),
        "created_at": int(time.time()),
        "status": "awaiting_detail",
    }


def build_education_case_ack_payload(ticket: dict[str, Any] | None, *, intent: str | None = None) -> dict[str, Any]:
    ticket = ticket if isinstance(ticket, dict) else {}
    nro_ticket = ticket.get("nro_ticket") or ticket.get("id")
    body = "Listo, deje tu consulta escolar registrada para seguimiento."
    if nro_ticket:
        body = f"Listo, deje tu consulta escolar registrada con ticket #{nro_ticket}."
    return {
        "message_body": body,
        "message_type": "interactive_buttons",
        "options_list": [
            {"texto": "Ver estado", "action_id": "estado_ticket"},
            {"texto": "Menu colegio", "action_id": "menu_principal"},
            {"texto": "Hablar con secretaria", "action_id": "derivar_humano"},
        ],
        "fuente": "education_whatsapp_case_created",
        "ticket_id": ticket.get("id"),
        "data": {
            "contract_version": EDUCATION_CASE_INTAKE_CONTRACT_VERSION,
            "intent": intent,
            "ticket": ticket,
        },
    }
