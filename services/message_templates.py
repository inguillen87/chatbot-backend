from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Mapping
from urllib.parse import urlsplit


BASE_DIR = os.path.dirname(os.path.dirname(__file__))
TEMPLATES_PATH = os.path.join(BASE_DIR, "templates", "messages.json")

WHATSAPP_TEMPLATE_PACK_CATALOG_VERSION = "2026.07.30"
WHATSAPP_TEMPLATE_PACK_CONTRACT_VERSION = "whatsapp.template_pack.v1"
WHATSAPP_TEMPLATE_LIFECYCLE_STATES = (
    "local_draft",
    "content_created",
    "approval_pending",
    "approved",
    "rejected",
    "stale",
)
WHATSAPP_TEMPLATE_PROVIDER_EVIDENCE_MAX_AGE = timedelta(days=7)

_TEMPLATE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,127}$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:_[A-Z]{2})?$")
_NUMBERED_VARIABLE_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")
_ANY_DOUBLE_BRACE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_NAMED_VARIABLE_RE = re.compile(r"(?:\$?\{[A-Za-z_][A-Za-z0-9_.-]*\})")
_ALLOWED_CATEGORIES = {"UTILITY", "MARKETING", "AUTHENTICATION"}
_ALLOWED_CTA_TYPES = {"URL", "PHONE_NUMBER", "QUICK_REPLY"}
_INTENT_LABELS = {
    "confirmation": "Confirmacion",
    "follow_up": "Seguimiento",
    "appointment": "Turno",
    "payment": "Pago",
    "handoff": "Derivacion humana",
}


class WhatsAppTemplateValidationError(ValueError):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("; ".join(error["message"] for error in errors))


@lru_cache(maxsize=None)
def _load_templates():
    try:
        with open(TEMPLATES_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def get_message(key: str, **kwargs) -> str:
    """Return a formatted legacy runtime message for the given key."""

    template = _load_templates().get(key, "")
    if not template:
        return ""
    try:
        return template.format(**kwargs)
    except Exception:
        return template


def _variable(index: int, name: str, example: str) -> dict[str, Any]:
    return {"index": index, "name": name, "example": example}


def _url_cta(text: str, url: str) -> dict[str, str]:
    return {"type": "URL", "text": text, "url": url}


def _pack_template(
    *,
    intent: str,
    name: str,
    body: str,
    variables: list[dict[str, Any]],
    cta: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "intent": intent,
        "intent_label": _INTENT_LABELS.get(intent, intent),
        "name": name,
        "language": "es_AR",
        "category": "UTILITY",
        "body": body,
        "variables": variables,
        "cta": cta,
    }


_WHATSAPP_TEMPLATE_PACKS: dict[str, dict[str, Any]] = {
    "municipio": {
        "pack_id": "municipio_operaciones_es_ar",
        "pack_version": "1.0.0",
        "label": "Municipio",
        "templates": [
            _pack_template(
                intent="confirmation",
                name="chatboc_municipio_confirmacion_v1",
                body="Registramos tu reclamo {{1}}. Estado inicial: {{2}}. Vas a recibir cada novedad por este canal.",
                variables=[
                    _variable(1, "claim_code", "REC-10482"),
                    _variable(2, "status", "Ingresado"),
                    _variable(3, "tracking_path", "reclamos/REC-10482?pin=4821"),
                ],
                cta=_url_cta("Ver seguimiento", "https://www.chatboc.ar/{{3}}"),
            ),
            _pack_template(
                intent="follow_up",
                name="chatboc_municipio_seguimiento_v1",
                body="Hay una actualización en el reclamo {{1}}: {{2}}. Detalle del equipo municipal: {{3}}.",
                variables=[
                    _variable(1, "claim_code", "REC-10482"),
                    _variable(2, "status", "En tratamiento"),
                    _variable(3, "update", "La cuadrilla recibió la orden de trabajo"),
                    _variable(4, "tracking_path", "reclamos/REC-10482?pin=4821"),
                ],
                cta=_url_cta("Ver seguimiento", "https://www.chatboc.ar/{{4}}"),
            ),
            _pack_template(
                intent="appointment",
                name="chatboc_municipio_turno_v1",
                body="Tu turno {{1}} en {{2}} quedó confirmado para {{3}}. Presentate con la documentación indicada.",
                variables=[
                    _variable(1, "appointment_code", "TUR-2081"),
                    _variable(2, "office", "Centro de Atención Municipal"),
                    _variable(3, "date_time", "martes 4 de agosto, 10:30"),
                    _variable(4, "appointment_path", "turnos/TUR-2081"),
                ],
                cta=_url_cta("Ver turno", "https://www.chatboc.ar/{{4}}"),
            ),
            _pack_template(
                intent="payment",
                name="chatboc_municipio_pago_v1",
                body="Está disponible el pago de {{1}} por {{2}}. Usá únicamente el checkout seguro del municipio.",
                variables=[
                    _variable(1, "concept", "Tasa municipal"),
                    _variable(2, "amount", "$ 12.450"),
                    _variable(3, "payment_path", "pagos/municipio/OP-7204"),
                ],
                cta=_url_cta("Pagar seguro", "https://www.chatboc.ar/{{3}}"),
            ),
            _pack_template(
                intent="handoff",
                name="chatboc_municipio_derivacion_v1",
                body="Derivamos el caso {{1}} al área {{2}}. Un agente municipal continuará la atención por este mismo canal.",
                variables=[
                    _variable(1, "case_code", "REC-10482"),
                    _variable(2, "team", "Servicios Públicos"),
                ],
            ),
        ],
    },
    "colegio": {
        "pack_id": "colegio_operaciones_es_ar",
        "pack_version": "1.0.0",
        "label": "Colegio",
        "templates": [
            _pack_template(
                intent="confirmation",
                name="chatboc_colegio_confirmacion_v1",
                body="Registramos la gestión {{1}} para {{2}}. Estado inicial: {{3}}. La institución informará las novedades por este canal.",
                variables=[
                    _variable(1, "case_code", "FAM-3108"),
                    _variable(2, "student_name", "Sofía Pérez"),
                    _variable(3, "status", "Recibida"),
                    _variable(4, "portal_path", "familias/casos/FAM-3108"),
                ],
                cta=_url_cta("Abrir portal", "https://www.chatboc.ar/{{4}}"),
            ),
            _pack_template(
                intent="follow_up",
                name="chatboc_colegio_seguimiento_v1",
                body="Actualización de la gestión {{1}}: {{2}}. Próxima respuesta estimada: {{3}}.",
                variables=[
                    _variable(1, "case_code", "FAM-3108"),
                    _variable(2, "update", "Secretaría validó la documentación"),
                    _variable(3, "response_eta", "dentro de 2 días hábiles"),
                    _variable(4, "portal_path", "familias/casos/FAM-3108"),
                ],
                cta=_url_cta("Ver gestión", "https://www.chatboc.ar/{{4}}"),
            ),
            _pack_template(
                intent="appointment",
                name="chatboc_colegio_turno_v1",
                body="La entrevista {{1}} para {{2}} quedó confirmada para {{3}}. Si necesitás reprogramar, hacelo desde el portal.",
                variables=[
                    _variable(1, "appointment_code", "ENT-912"),
                    _variable(2, "student_name", "Sofía Pérez"),
                    _variable(3, "date_time", "jueves 6 de agosto, 14:00"),
                    _variable(4, "appointment_path", "familias/entrevistas/ENT-912"),
                ],
                cta=_url_cta("Ver entrevista", "https://www.chatboc.ar/{{4}}"),
            ),
            _pack_template(
                intent="payment",
                name="chatboc_colegio_pago_v1",
                body="Está disponible el pago de {{1}} para {{2}} por {{3}}. El colegio nunca pedirá datos de tarjeta por chat.",
                variables=[
                    _variable(1, "concept", "Cuota de agosto"),
                    _variable(2, "student_name", "Sofía Pérez"),
                    _variable(3, "amount", "$ 85.000"),
                    _variable(4, "payment_path", "familias/pagos/CUO-8821"),
                ],
                cta=_url_cta("Pagar seguro", "https://www.chatboc.ar/{{4}}"),
            ),
            _pack_template(
                intent="handoff",
                name="chatboc_colegio_derivacion_v1",
                body="Derivamos la gestión {{1}} a {{2}}. El equipo continuará la conversación por este canal dentro del horario institucional.",
                variables=[
                    _variable(1, "case_code", "FAM-3108"),
                    _variable(2, "team", "Secretaría académica"),
                ],
            ),
        ],
    },
    "empresa": {
        "pack_id": "empresa_operaciones_es_ar",
        "pack_version": "1.0.0",
        "label": "Empresa",
        "templates": [
            _pack_template(
                intent="confirmation",
                name="chatboc_empresa_confirmacion_v1",
                body="Confirmamos la solicitud {{1}}. Estado inicial: {{2}}. Te avisaremos por este canal cuando haya novedades.",
                variables=[
                    _variable(1, "request_code", "SOL-5824"),
                    _variable(2, "status", "Recibida"),
                    _variable(3, "tracking_path", "clientes/solicitudes/SOL-5824"),
                ],
                cta=_url_cta("Ver solicitud", "https://www.chatboc.ar/{{3}}"),
            ),
            _pack_template(
                intent="follow_up",
                name="chatboc_empresa_seguimiento_v1",
                body="Actualización de la solicitud {{1}}: {{2}}. Detalle: {{3}}.",
                variables=[
                    _variable(1, "request_code", "SOL-5824"),
                    _variable(2, "status", "En preparación"),
                    _variable(3, "update", "El pedido pasa a despacho"),
                    _variable(4, "tracking_path", "clientes/solicitudes/SOL-5824"),
                ],
                cta=_url_cta("Ver seguimiento", "https://www.chatboc.ar/{{4}}"),
            ),
            _pack_template(
                intent="appointment",
                name="chatboc_empresa_turno_v1",
                body="La reserva {{1}} para {{2}} quedó confirmada para {{3}}.",
                variables=[
                    _variable(1, "appointment_code", "RES-7741"),
                    _variable(2, "service", "Asesoría técnica"),
                    _variable(3, "date_time", "viernes 7 de agosto, 09:00"),
                    _variable(4, "appointment_path", "clientes/reservas/RES-7741"),
                ],
                cta=_url_cta("Ver reserva", "https://www.chatboc.ar/{{4}}"),
            ),
            _pack_template(
                intent="payment",
                name="chatboc_empresa_pago_v1",
                body="El pedido {{1}} está listo para pagar por {{2}}. Completá la operación en el checkout seguro.",
                variables=[
                    _variable(1, "order_code", "PED-6402"),
                    _variable(2, "amount", "$ 48.900"),
                    _variable(3, "payment_path", "checkout/PED-6402"),
                ],
                cta=_url_cta("Pagar seguro", "https://www.chatboc.ar/{{3}}"),
            ),
            _pack_template(
                intent="handoff",
                name="chatboc_empresa_derivacion_v1",
                body="Derivamos la solicitud {{1}} al equipo de {{2}}. Una persona continuará la atención por este mismo canal.",
                variables=[
                    _variable(1, "request_code", "SOL-5824"),
                    _variable(2, "team", "Soporte especializado"),
                ],
            ),
        ],
    },
}

_VERTICAL_ALIASES = {
    "municipio": "municipio",
    "gobierno": "municipio",
    "government": "municipio",
    "colegio": "colegio",
    "educacion": "colegio",
    "education": "colegio",
    "school": "colegio",
    "empresa": "empresa",
    "empresas": "empresa",
    "pyme": "empresa",
    "commerce": "empresa",
}


def normalize_whatsapp_template_vertical(value: Any) -> str | None:
    return _VERTICAL_ALIASES.get(str(value or "").strip().lower())


def _validation_error(field: str, code: str, message: str) -> dict[str, str]:
    return {"field": field, "code": code, "message": message}


def validate_whatsapp_template(template: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one provider-ready template contract without contacting a provider."""

    errors: list[dict[str, str]] = []
    name = str(template.get("name") or "").strip()
    language = str(template.get("language") or "").strip()
    category = str(template.get("category") or "").strip().upper()
    body = str(template.get("body") or "").strip()
    intent = str(template.get("intent") or "").strip().lower()

    if not _TEMPLATE_NAME_RE.fullmatch(name):
        errors.append(
            _validation_error(
                "name",
                "invalid_template_name",
                "El nombre debe usar solo minusculas, numeros y guion bajo, y comenzar con una letra.",
            )
        )
    if not _LANGUAGE_RE.fullmatch(language):
        errors.append(
            _validation_error(
                "language",
                "invalid_language",
                "El idioma debe usar formato ISO, por ejemplo es o es_AR.",
            )
        )
    if category not in _ALLOWED_CATEGORIES:
        errors.append(
            _validation_error(
                "category",
                "invalid_category",
                "La categoria debe ser UTILITY, MARKETING o AUTHENTICATION.",
            )
        )
    if not body or len(body) > 1024:
        errors.append(
            _validation_error(
                "body",
                "invalid_body_length",
                "El body es obligatorio y admite hasta 1024 caracteres.",
            )
        )
    if _NAMED_VARIABLE_RE.search(body):
        errors.append(
            _validation_error(
                "body",
                "named_variables_not_allowed",
                "Las variables de WhatsApp deben ser numeradas: {{1}}, {{2}}, etc.",
            )
        )

    placeholder_sources = [body]
    cta = template.get("cta")
    normalized_cta: dict[str, str] | None = None
    if cta not in (None, {}):
        if not isinstance(cta, Mapping):
            errors.append(_validation_error("cta", "invalid_cta", "CTA debe ser un objeto."))
        else:
            cta_type = str(cta.get("type") or "").strip().upper()
            cta_text = str(cta.get("text") or "").strip()
            if cta_type not in _ALLOWED_CTA_TYPES:
                errors.append(
                    _validation_error(
                        "cta.type",
                        "invalid_cta_type",
                        "CTA debe ser URL, PHONE_NUMBER o QUICK_REPLY.",
                    )
                )
            if not cta_text or len(cta_text) > 20:
                errors.append(
                    _validation_error(
                        "cta.text",
                        "invalid_cta_text",
                        "El texto del CTA es obligatorio y admite hasta 20 caracteres.",
                    )
                )
            normalized_cta = {"type": cta_type, "text": cta_text}
            if cta_type == "URL":
                url = str(cta.get("url") or "").strip()
                parsed = urlsplit(url)
                if (
                    not url
                    or len(url) > 2000
                    or parsed.scheme.lower() != "https"
                    or not parsed.netloc
                    or parsed.username
                    or parsed.password
                ):
                    errors.append(
                        _validation_error(
                            "cta.url",
                            "invalid_cta_url",
                            "El CTA URL debe usar una URL HTTPS absoluta y segura.",
                        )
                    )
                if _NAMED_VARIABLE_RE.search(url):
                    errors.append(
                        _validation_error(
                            "cta.url",
                            "named_variables_not_allowed",
                            "Las variables del CTA deben ser numeradas.",
                        )
                    )
                placeholder_sources.append(url)
                normalized_cta["url"] = url
            elif cta_type == "PHONE_NUMBER":
                phone_number = str(cta.get("phone_number") or "").strip()
                if not re.fullmatch(r"\+[1-9]\d{7,14}", phone_number):
                    errors.append(
                        _validation_error(
                            "cta.phone_number",
                            "invalid_cta_phone_number",
                            "El telefono del CTA debe estar en formato E.164.",
                        )
                    )
                normalized_cta["phone_number"] = phone_number

    all_tokens: list[str] = []
    numbered_indices: list[int] = []
    for source in placeholder_sources:
        all_tokens.extend(_ANY_DOUBLE_BRACE_RE.findall(source))
        numbered_indices.extend(int(value) for value in _NUMBERED_VARIABLE_RE.findall(source))
    if any(not str(token).strip().isdigit() for token in all_tokens):
        errors.append(
            _validation_error(
                "variables",
                "non_numbered_variables",
                "Todas las variables deben ser numericas.",
            )
        )

    unique_indices = sorted(set(numbered_indices))
    expected_indices = list(range(1, (max(unique_indices) if unique_indices else 0) + 1))
    if unique_indices != expected_indices:
        errors.append(
            _validation_error(
                "variables",
                "non_sequential_variables",
                "Las variables deben comenzar en {{1}} y ser correlativas, sin saltos.",
            )
        )

    raw_variables = template.get("variables")
    if not isinstance(raw_variables, list):
        raw_variables = []
        errors.append(
            _validation_error(
                "variables",
                "invalid_variable_contract",
                "variables debe ser una lista con index, name y example.",
            )
        )

    normalized_variables: list[dict[str, Any]] = []
    declared_indices: list[int] = []
    declared_names: set[str] = set()
    for position, variable in enumerate(raw_variables):
        if not isinstance(variable, Mapping):
            errors.append(
                _validation_error(
                    f"variables.{position}",
                    "invalid_variable",
                    "Cada variable debe ser un objeto.",
                )
            )
            continue
        index = variable.get("index")
        variable_name = str(variable.get("name") or "").strip()
        example = str(variable.get("example") or "").strip()
        if not isinstance(index, int) or isinstance(index, bool) or index < 1:
            errors.append(
                _validation_error(
                    f"variables.{position}.index",
                    "invalid_variable_index",
                    "El indice debe ser un entero positivo.",
                )
            )
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", variable_name):
            errors.append(
                _validation_error(
                    f"variables.{position}.name",
                    "invalid_variable_name",
                    "El nombre interno de la variable no es valido.",
                )
            )
        if variable_name in declared_names:
            errors.append(
                _validation_error(
                    f"variables.{position}.name",
                    "duplicate_variable_name",
                    "Los nombres internos de variables no pueden repetirse.",
                )
            )
        if not example or len(example) > 200:
            errors.append(
                _validation_error(
                    f"variables.{position}.example",
                    "invalid_variable_example",
                    "Cada variable requiere un ejemplo de hasta 200 caracteres.",
                )
            )
        declared_indices.append(index)
        declared_names.add(variable_name)
        normalized_variables.append({"index": index, "name": variable_name, "example": example})

    if sorted(declared_indices) != unique_indices or len(declared_indices) != len(set(declared_indices)):
        errors.append(
            _validation_error(
                "variables",
                "variable_contract_mismatch",
                "La lista de variables debe coincidir exactamente con los placeholders numerados.",
            )
        )
    if category == "AUTHENTICATION" and unique_indices:
        errors.append(
            _validation_error(
                "category",
                "authentication_custom_variables_not_allowed",
                "Las plantillas AUTHENTICATION de este contrato no admiten variables personalizadas.",
            )
        )

    normalized = {
        "intent": intent,
        "intent_label": str(template.get("intent_label") or _INTENT_LABELS.get(intent, intent)).strip(),
        "name": name,
        "language": language,
        "category": category,
        "body": body,
        "variables": sorted(normalized_variables, key=lambda item: item["index"]),
        "cta": normalized_cta,
    }
    return {"valid": not errors, "errors": errors, "normalized": normalized}


def require_valid_whatsapp_template(template: Mapping[str, Any]) -> dict[str, Any]:
    validation = validate_whatsapp_template(template)
    if not validation["valid"]:
        raise WhatsAppTemplateValidationError(validation["errors"])
    return validation["normalized"]


def whatsapp_template_definition_hash(template: Mapping[str, Any]) -> str:
    normalized = require_valid_whatsapp_template(template)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def render_whatsapp_template_preview(template: Mapping[str, Any]) -> dict[str, Any]:
    normalized = require_valid_whatsapp_template(template)
    values = {item["index"]: item["example"] for item in normalized["variables"]}

    def replace(match: re.Match[str]) -> str:
        return values.get(int(match.group(1)), match.group(0))

    body = _NUMBERED_VARIABLE_RE.sub(replace, normalized["body"])
    cta = copy.deepcopy(normalized["cta"])
    if isinstance(cta, dict) and cta.get("url"):
        cta["url"] = _NUMBERED_VARIABLE_RE.sub(replace, str(cta["url"]))
    return {
        "body": body,
        "cta": cta,
        "sample_values": {str(index): value for index, value in values.items()},
        "contains_unresolved_variables": bool(_ANY_DOUBLE_BRACE_RE.search(body))
        or bool(isinstance(cta, dict) and _ANY_DOUBLE_BRACE_RE.search(str(cta.get("url") or ""))),
    }


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def whatsapp_template_lifecycle(
    status: Any,
    *,
    source: str | None,
    provider_reference: Any = None,
    observed_at: Any = None,
    now: datetime | None = None,
    max_age: timedelta = WHATSAPP_TEMPLATE_PROVIDER_EVIDENCE_MAX_AGE,
) -> dict[str, Any]:
    """Return a fail-closed lifecycle; local snapshots are never approval evidence."""

    raw_status = str(status or "").strip().lower()
    normalized_source = str(source or "").strip().lower()
    observed = _coerce_datetime(observed_at)
    current = _coerce_datetime(now) or datetime.now(timezone.utc)
    evidence_fresh = bool(
        observed
        and observed <= current + timedelta(minutes=5)
        and current - observed <= max_age
    )
    has_provider_reference = bool(str(provider_reference or "").strip())
    local_only = normalized_source in {
        "local_twilio_manifest",
        "notification_template",
        "chatboc_versioned_pack",
        "local",
    }

    state = "local_draft"
    reason = "local_definition_only"
    if normalized_source == "local_twilio_manifest":
        state = "stale"
        reason = "unverified_global_manifest_snapshot"
    elif raw_status in {"rejected", "failed", "disabled", "paused", "approval_failed"}:
        state = "rejected"
        reason = "provider_rejected_or_disabled"
    elif raw_status in {"pending", "submitted", "in_review", "review", "twilio_review", "pending_approval", "approval_pending"}:
        state = "approval_pending" if has_provider_reference and not local_only else "local_draft"
        reason = "provider_approval_pending" if state == "approval_pending" else "provider_reference_missing"
    elif raw_status in {"created", "content_created"}:
        state = "content_created" if has_provider_reference and not local_only else "local_draft"
        reason = "provider_content_created" if state == "content_created" else "provider_reference_missing"
    elif raw_status in {"approved", "active", "ready", "published", "online"}:
        if normalized_source == "local_twilio_manifest":
            state = "stale"
            reason = "unverified_global_manifest_snapshot"
        elif local_only:
            state = "local_draft"
            reason = "local_definition_only"
        elif has_provider_reference and evidence_fresh:
            state = "approved"
            reason = "fresh_provider_evidence"
        else:
            state = "stale"
            reason = "provider_evidence_missing_or_expired"
    elif raw_status == "stale":
        state = "stale"
        reason = "explicitly_stale"
    elif has_provider_reference and not local_only:
        state = "content_created"
        reason = "provider_content_exists_without_approval"

    production_send_allowed = state == "approved"
    blocker_by_state = {
        "local_draft": "provider_content_not_created",
        "content_created": "meta_approval_not_requested",
        "approval_pending": "meta_approval_pending",
        "rejected": "meta_approval_rejected",
        "stale": "provider_status_stale",
    }
    return {
        "state": state,
        "reason": reason,
        "provider_status": raw_status or None,
        "provider_reference_present": has_provider_reference and not local_only,
        "provider_evidence_at": observed.isoformat() if observed else None,
        "provider_evidence_fresh": evidence_fresh and not local_only,
        "production_send_allowed": production_send_allowed,
        "blockers": [] if production_send_allowed else [blocker_by_state[state]],
    }


def whatsapp_template_pack(vertical: Any) -> dict[str, Any] | None:
    normalized_vertical = normalize_whatsapp_template_vertical(vertical)
    pack = _WHATSAPP_TEMPLATE_PACKS.get(normalized_vertical or "")
    return copy.deepcopy(pack) if pack else None


def whatsapp_template_pack_catalog(
    registry_by_name: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    registry_by_name = registry_by_name or {}
    packs: list[dict[str, Any]] = []
    lifecycle_counts = {state: 0 for state in WHATSAPP_TEMPLATE_LIFECYCLE_STATES}

    for vertical, raw_pack in _WHATSAPP_TEMPLATE_PACKS.items():
        templates: list[dict[str, Any]] = []
        for raw_template in raw_pack["templates"]:
            validation = validate_whatsapp_template(raw_template)
            normalized = validation["normalized"]
            registry = registry_by_name.get(normalized["name"])
            registry = registry if isinstance(registry, Mapping) else {}
            source = str(registry.get("source") or "chatboc_versioned_pack")
            lifecycle = whatsapp_template_lifecycle(
                registry.get("status") or "local_draft",
                source=source,
                provider_reference=registry.get("content_sid") or registry.get("external_template_id"),
                observed_at=registry.get("last_sync_at"),
            )
            lifecycle_counts[lifecycle["state"]] += 1
            blockers = list(lifecycle["blockers"])
            blockers.extend(error["code"] for error in validation["errors"])
            templates.append(
                {
                    **normalized,
                    "definition_hash": whatsapp_template_definition_hash(raw_template)
                    if validation["valid"]
                    else None,
                    "validation": {
                        "valid": validation["valid"],
                        "errors": validation["errors"],
                    },
                    "preview": render_whatsapp_template_preview(raw_template)
                    if validation["valid"]
                    else None,
                    "lifecycle": lifecycle,
                    "materialized": bool(registry),
                    "registry_id": registry.get("id"),
                    "blockers": list(dict.fromkeys(blockers)),
                }
            )

        packs.append(
            {
                "vertical": vertical,
                "pack_id": raw_pack["pack_id"],
                "pack_version": raw_pack["pack_version"],
                "label": raw_pack["label"],
                "templates": templates,
                "summary": {
                    "total": len(templates),
                    "valid": sum(1 for item in templates if item["validation"]["valid"]),
                    "approved": sum(1 for item in templates if item["lifecycle"]["state"] == "approved"),
                    "blocked": sum(1 for item in templates if item["blockers"]),
                },
            }
        )

    return {
        "contract_version": "whatsapp.template_pack.catalog.v1",
        "catalog_version": WHATSAPP_TEMPLATE_PACK_CATALOG_VERSION,
        "template_contract_version": WHATSAPP_TEMPLATE_PACK_CONTRACT_VERSION,
        "lifecycle_states": list(WHATSAPP_TEMPLATE_LIFECYCLE_STATES),
        "provider_calls_performed": False,
        "packs": packs,
        "summary": {
            "packs": len(packs),
            "templates": sum(pack["summary"]["total"] for pack in packs),
            "lifecycle": lifecycle_counts,
        },
        "policy": {
            "local_manifest_is_approval_evidence": False,
            "notification_template_is_meta_approved": False,
            "fresh_provider_evidence_required_for_approved": True,
            "provider_evidence_max_age_hours": int(
                WHATSAPP_TEMPLATE_PROVIDER_EVIDENCE_MAX_AGE.total_seconds() // 3600
            ),
            "provider_calls_allowed": False,
        },
        "frontend_contract": {
            "render_as": "whatsapp_versioned_template_packs",
            "copy": {
                "title": "Packs profesionales de WhatsApp",
                "description": (
                    "Previsualiza mensajes versionados por vertical. Los borradores locales nunca se muestran "
                    "como aprobados por Meta."
                ),
                "provider_notice": "Esta pantalla no realiza llamadas a Twilio ni a Meta.",
                "empty": "No hay packs disponibles para este tenant.",
                "materialize": "Crear borradores locales",
                "materialized": "Borradores locales creados",
            },
            "lifecycle_labels": {
                "local_draft": "Borrador local",
                "content_created": "Contenido creado",
                "approval_pending": "Aprobacion pendiente",
                "approved": "Aprobada",
                "rejected": "Rechazada",
                "stale": "Estado vencido",
                "unverified": "Sin verificacion",
            },
            "blocker_labels": {
                "provider_content_not_created": "Todavia no existe contenido verificado en el proveedor.",
                "meta_approval_not_requested": "El contenido aun no fue enviado a aprobacion.",
                "meta_approval_pending": "Meta todavia no confirmo la aprobacion.",
                "meta_approval_rejected": "Meta rechazo esta version; requiere revision.",
                "provider_status_stale": "La ultima evidencia del proveedor esta vencida o no es confiable.",
            },
        },
    }
