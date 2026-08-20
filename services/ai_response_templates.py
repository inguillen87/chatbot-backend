from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from werkzeug.exceptions import RequestEntityTooLarge

from models import (
    MunicipioTicket,
    PlantillasRespuesta,
    PymeTicket,
    TenantProfile,
    TenantTicket,
    User,
)
from services.employee_ticket_access import employee_ticket_category_access_allows
from services.tenant_ticket_scope import scoped_municipio_ticket_query


AI_TEMPLATES_CONTRACT_VERSION = "ai.templates.v2"
AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION = "ai.template_suggestions.v2"
AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION = "ai.template_ticket_preview.v1"

MAX_SUGGESTION_REQUEST_BYTES = 12_288
MAX_TEMPLATE_MUTATION_REQUEST_BYTES = 24_576
MAX_PREVIEW_REQUEST_BYTES = 4_096
MAX_GENERATE_TEMPLATE_REQUEST_BYTES = 10_240
MAX_IMPROVE_TEMPLATE_REQUEST_BYTES = 18_432
MAX_SUGGESTION_SUBJECT_BYTES = 512
MAX_SUGGESTION_CONTEXT_BYTES = 8_192
MAX_TEMPLATE_NAME_BYTES = 255
MAX_TEMPLATE_TEXT_BYTES = 16_384
MAX_TEMPLATE_KEYWORDS = 32
MAX_TEMPLATE_KEYWORD_BYTES = 128
MAX_TOP_N = 10

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}")
_MAX_RENDERED_TEXT_BYTES = 16_384
_MAX_CONTEXT_VALUE_BYTES = 2_048
_SEMANTIC_REQUEST_LIMIT_ENV_KEY = "chatboc.ai_templates.semantic_request_limit"
_SUPPORTED_SOURCE_MODELS = frozenset(
    {"TenantTicket", "MunicipioTicket", "PymeTicket"}
)


class AITemplateContractError(ValueError):
    """Stable validation error that never carries submitted values."""

    def __init__(
        self,
        code: str,
        *,
        field: str | None = None,
        status_code: int = 400,
        message: str | None = None,
    ) -> None:
        self.code = str(code)
        self.field = field
        self.status_code = int(status_code)
        self.message = message or "La solicitud no cumple el contrato de plantillas."
        super().__init__(self.code)

    def to_dict(self, *, contract_version: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": contract_version,
            # Keep the historical string field while exposing a stable code.
            "error": self.message,
            "reason_code": self.code,
        }
        if self.field:
            payload["field"] = self.field
        return payload


def validate_request_size(content_length: Any, *, max_bytes: int) -> None:
    if content_length is None:
        return
    try:
        size = int(content_length)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AITemplateContractError(
            "request_size_invalid",
            field="body",
            status_code=400,
            message="El tamaño del cuerpo de la solicitud no es válido.",
        ) from exc
    if size < 0 or size > max_bytes:
        raise AITemplateContractError(
            "request_body_too_large",
            field="body",
            status_code=413,
            message=f"El cuerpo de la solicitud supera el máximo de {max_bytes} bytes.",
        )


def prime_bounded_request_stream(flask_request: Any, *, max_bytes: int) -> int:
    """Install the stream ceiling before app-level ``before_request`` hooks run."""

    stored_semantic_max = flask_request.environ.get(_SEMANTIC_REQUEST_LIMIT_ENV_KEY)
    if stored_semantic_max is not None:
        effective_max = min(int(stored_semantic_max), int(max_bytes))
    else:
        configured_max = flask_request.max_content_length
        effective_max = (
            min(max(int(configured_max), 0), int(max_bytes))
            if configured_max is not None
            else int(max_bytes)
        )
    flask_request.environ[_SEMANTIC_REQUEST_LIMIT_ENV_KEY] = effective_max
    validate_request_size(
        flask_request.content_length,
        max_bytes=effective_max,
    )
    # One sentinel byte distinguishes an exact-cap body from a longer
    # unknown-length stream. The route parser rejects that sentinel byte.
    flask_request.max_content_length = effective_max + 1
    return effective_max


def parse_bounded_json_object(flask_request: Any, *, max_bytes: int) -> dict[str, Any]:
    """Read and parse a JSON object without buffering an unbounded request body."""

    effective_max = prime_bounded_request_stream(
        flask_request,
        max_bytes=max_bytes,
    )

    # Werkzeug can return exactly the configured cap for an unknown-length
    # stream without proving that the stream ended. A single sentinel byte lets
    # it reject chunked requests before JSON parsing without buffering the body.
    try:
        raw_body = flask_request.get_data(cache=True)
    except RequestEntityTooLarge as exc:
        raise AITemplateContractError(
            "request_body_too_large",
            field="body",
            status_code=413,
            message=(
                f"El cuerpo de la solicitud supera el máximo de {effective_max} bytes."
            ),
        ) from exc
    if len(raw_body) > effective_max:
        raise AITemplateContractError(
            "request_body_too_large",
            field="body",
            status_code=413,
            message=(
                f"El cuerpo de la solicitud supera el máximo de {effective_max} bytes."
            ),
        )

    payload = flask_request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise AITemplateContractError(
            "request_body_must_be_object",
            field="body",
            message="Request body debe ser un objeto JSON.",
        )
    return payload


def validate_utf8_text(
    value: Any,
    *,
    field: str,
    max_bytes: int,
    required: bool = True,
) -> str:
    if value is None and required:
        raise AITemplateContractError(
            f"{field}_required",
            field=field,
            message=f"El campo '{field}' es obligatorio.",
        )
    if not isinstance(value, str):
        raise AITemplateContractError(
            f"{field}_must_be_string",
            field=field,
            message=f"El campo '{field}' debe ser un string.",
        )
    normalized = value.strip()
    if required and not normalized:
        raise AITemplateContractError(
            f"{field}_required",
            field=field,
            message=f"El campo '{field}' es obligatorio.",
        )
    if "\x00" in normalized:
        raise AITemplateContractError(
            f"{field}_invalid",
            field=field,
            message=f"El campo '{field}' contiene caracteres no permitidos.",
        )
    try:
        encoded_length = len(normalized.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise AITemplateContractError(
            f"{field}_invalid",
            field=field,
            message=f"El campo '{field}' no contiene texto UTF-8 válido.",
        ) from exc
    if encoded_length > max_bytes:
        raise AITemplateContractError(
            f"{field}_too_long",
            field=field,
            status_code=413,
            message=f"El campo '{field}' supera el máximo de {max_bytes} bytes UTF-8.",
        )
    return normalized


def validate_top_n(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AITemplateContractError(
            "top_n_invalid",
            field="top_n",
            message="El campo 'top_n' debe ser un entero positivo.",
        )
    if value > MAX_TOP_N:
        raise AITemplateContractError(
            "top_n_exceeds_limit",
            field="top_n",
            message=f"El campo 'top_n' no puede superar {MAX_TOP_N}.",
        )
    return value


def validate_active_only(value: Any) -> bool:
    if value is None or value == "":
        return False
    normalized = str(value).strip().lower()
    if normalized in {"1", "true"}:
        return True
    if normalized in {"0", "false"}:
        return False
    raise AITemplateContractError(
        "active_only_invalid",
        field="active_only",
        message="El filtro 'active_only' debe ser 1, 0, true o false.",
    )


def validate_keywords(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise AITemplateContractError(
            "keywords_must_be_array",
            field="keywords",
            message="El campo 'keywords' debe ser una lista de strings.",
        )
    if len(value) > MAX_TEMPLATE_KEYWORDS:
        raise AITemplateContractError(
            "keywords_too_many",
            field="keywords",
            status_code=413,
            message=f"El campo 'keywords' admite hasta {MAX_TEMPLATE_KEYWORDS} elementos.",
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        keyword = validate_utf8_text(
            item,
            field="keywords",
            max_bytes=MAX_TEMPLATE_KEYWORD_BYTES,
        )
        key = keyword.casefold()
        if key not in seen:
            normalized.append(keyword)
            seen.add(key)
    return normalized


def _positive_ticket_id(value: Any) -> int:
    if isinstance(value, bool):
        raise AITemplateContractError(
            "ticket_id_invalid",
            field="ticket_id",
            message="El campo 'ticket_id' debe ser un entero positivo.",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AITemplateContractError(
            "ticket_id_invalid",
            field="ticket_id",
            message="El campo 'ticket_id' debe ser un entero positivo.",
        ) from exc
    if parsed <= 0:
        raise AITemplateContractError(
            "ticket_id_invalid",
            field="ticket_id",
            message="El campo 'ticket_id' debe ser un entero positivo.",
        )
    return parsed


def _source_model(value: Any) -> str:
    if not isinstance(value, str) or value not in _SUPPORTED_SOURCE_MODELS:
        raise AITemplateContractError(
            "source_model_invalid",
            field="source_model",
            message=(
                "El campo 'source_model' debe ser TenantTicket, MunicipioTicket "
                "o PymeTicket."
            ),
        )
    return value


def _safe_scalar(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    rendered = str(value).strip()
    if (
        not rendered
        or "\x00" in rendered
        or len(rendered.encode("utf-8")) > _MAX_CONTEXT_VALUE_BYTES
    ):
        return None
    return rendered


def _first_safe(*values: Any) -> str | None:
    for value in values:
        safe = _safe_scalar(value)
        if safe is not None:
            return safe
    return None


def _ticket_extra(ticket: Any) -> Mapping[str, Any]:
    value = getattr(ticket, "datos_extra", None)
    return value if isinstance(value, Mapping) else {}


def _contact_value(extra: Mapping[str, Any], *keys: str) -> str | None:
    contact = extra.get("contact")
    contact = contact if isinstance(contact, Mapping) else {}
    return _first_safe(
        *(contact.get(key) for key in keys),
        *(extra.get(key) for key in keys),
    )


def _load_authorized_ticket(
    *,
    tenant: TenantProfile,
    actor: User,
    ticket_id: int,
    source_model: str,
) -> Any | None:
    if source_model == "TenantTicket":
        ticket = TenantTicket.query.filter_by(
            id=ticket_id,
            tenant_id=tenant.id,
        ).first()
    elif source_model == "MunicipioTicket":
        ticket = (
            scoped_municipio_ticket_query(tenant)
            .filter(MunicipioTicket.id == ticket_id)
            .first()
        )
    else:
        # Legacy PymeTicket rows without an explicit tenant are intentionally
        # excluded: owner fallback would be ambiguous in a multi-tenant inbox.
        ticket = PymeTicket.query.filter_by(
            id=ticket_id,
            tenant_id=tenant.id,
        ).first()
    if ticket is None or not employee_ticket_category_access_allows(actor, ticket):
        return None
    return ticket


def _ticket_context(
    *,
    tenant: TenantProfile,
    ticket: Any,
    source_model: str,
) -> dict[str, str]:
    extra = _ticket_extra(ticket)
    ticket_number = _first_safe(
        getattr(ticket, "nro_ticket", None),
        extra.get("claim_code"),
        extra.get("public_code"),
        getattr(ticket, "id", None),
    )
    customer_name = None
    if source_model == "MunicipioTicket":
        customer_name = _first_safe(
            getattr(ticket, "nombre_vecino", None),
            _contact_value(extra, "name", "display_name", "nombre_cliente"),
        )
    elif source_model == "TenantTicket":
        customer_name = _first_safe(
            getattr(getattr(ticket, "user", None), "name", None),
            _contact_value(extra, "name", "display_name", "nombre_cliente"),
        )
    else:
        customer_name = _contact_value(
            extra,
            "name",
            "display_name",
            "nombre_cliente",
        )

    address = _first_safe(
        getattr(ticket, "direccion", None),
        extra.get("address"),
        extra.get("direccion"),
    )
    subject = _first_safe(
        getattr(ticket, "asunto", None),
        extra.get("title"),
    )
    values = {
        "ticket_id": _safe_scalar(getattr(ticket, "id", None)),
        "nro_ticket": ticket_number,
        "ticket_nro": ticket_number,
        "ticket_number": ticket_number,
        "estado": _safe_scalar(getattr(ticket, "estado", None)),
        "status": _safe_scalar(getattr(ticket, "estado", None)),
        "categoria": _safe_scalar(getattr(ticket, "categoria", None)),
        "category": _safe_scalar(getattr(ticket, "categoria", None)),
        "asunto": subject,
        "subject": subject,
        "direccion": address,
        "address": address,
        "nombre_cliente": customer_name,
        "customer_name": customer_name,
        "user_name": customer_name,
        "tenant_nombre": _safe_scalar(tenant.nombre),
        "municipio_nombre": _safe_scalar(tenant.nombre),
        "pyme_nombre": _safe_scalar(tenant.nombre),
        "organization_name": _safe_scalar(tenant.nombre),
    }
    # This exact allowlist is the security boundary. Raw complaint bodies,
    # phone numbers, email addresses, DNI and arbitrary datos_extra keys are
    # deliberately unavailable to canned-response templates.
    return {key: value for key, value in values.items() if value is not None}


def _template_variables(source: str) -> list[str]:
    matches = list(_PLACEHOLDER_RE.finditer(source))
    remainder = _PLACEHOLDER_RE.sub("", source)
    if "{{" in remainder or "}}" in remainder:
        raise AITemplateContractError(
            "template_placeholder_invalid",
            field="template_id",
            message="La plantilla contiene variables con formato no permitido.",
        )
    return sorted({match.group(1) for match in matches})


def _render_template(source: str, context: Mapping[str, str]) -> str:
    rendered = _PLACEHOLDER_RE.sub(
        lambda match: context[match.group(1)],
        source,
    )
    if len(rendered.encode("utf-8")) > _MAX_RENDERED_TEXT_BYTES:
        raise AITemplateContractError(
            "rendered_text_too_long",
            field="template_id",
            status_code=413,
            message="La respuesta renderizada supera el límite permitido.",
        )
    return rendered


@dataclass(frozen=True)
class TicketTemplatePreview:
    template: PlantillasRespuesta
    ticket: Any
    source_model: str
    rendered_text: str | None
    required_variables: tuple[str, ...]
    resolved_variables: tuple[str, ...]
    unresolved_variables: tuple[str, ...]

    def to_dict(self, *, tenant: TenantProfile) -> dict[str, Any]:
        ready = not self.unresolved_variables and self.rendered_text is not None
        return {
            "contract_version": AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION,
            "tenant": {"id": tenant.id, "slug": tenant.slug},
            "template": {
                "id": self.template.id,
                "name": self.template.name,
                "scope": "global" if self.template.tenant_id is None else "tenant",
                "readonly": self.template.tenant_id is None,
            },
            "ticket": {
                "id": self.ticket.id,
                "source_model": self.source_model,
            },
            "rendered_text": self.rendered_text,
            "required_variables": list(self.required_variables),
            "resolved_variables": list(self.resolved_variables),
            "unresolved_variables": list(self.unresolved_variables),
            "readiness": {
                "ready_to_insert": ready,
                "server_rendered": ready,
                "blocker": None if ready else "unresolved_variables",
            },
            "rendering_policy": {
                "content_type": "text/plain",
                "html_allowed": False,
                "client_interpolation_allowed": False,
            },
            "side_effects": {
                "provider_calls_performed": False,
                "messages_queued": 0,
                "messages_sent": 0,
                "records_written": 0,
            },
        }


def build_ticket_template_preview(
    *,
    tenant: TenantProfile,
    actor: User,
    template_id: Any,
    ticket_id: Any,
    source_model: Any,
) -> TicketTemplatePreview:
    normalized_template_id = validate_utf8_text(
        template_id,
        field="template_id",
        max_bytes=64,
    )
    normalized_ticket_id = _positive_ticket_id(ticket_id)
    normalized_source_model = _source_model(source_model)

    template = PlantillasRespuesta.query.filter(
        PlantillasRespuesta.id == normalized_template_id,
        PlantillasRespuesta.is_active.is_(True),
        (
            (PlantillasRespuesta.tenant_id == tenant.id)
            | PlantillasRespuesta.tenant_id.is_(None)
        ),
    ).first()
    if template is None:
        raise AITemplateContractError(
            "template_not_found",
            field="template_id",
            status_code=404,
            message="Plantilla no encontrada.",
        )

    ticket = _load_authorized_ticket(
        tenant=tenant,
        actor=actor,
        ticket_id=normalized_ticket_id,
        source_model=normalized_source_model,
    )
    if ticket is None:
        # Category and tenant denials intentionally collapse into not-found.
        raise AITemplateContractError(
            "ticket_not_found",
            field="ticket_id",
            status_code=404,
            message="Ticket no encontrado.",
        )

    source = validate_utf8_text(
        template.text,
        field="template_text",
        max_bytes=MAX_TEMPLATE_TEXT_BYTES,
    )
    required = _template_variables(source)
    context = _ticket_context(
        tenant=tenant,
        ticket=ticket,
        source_model=normalized_source_model,
    )
    resolved = sorted(variable for variable in required if variable in context)
    unresolved = sorted(set(required) - set(resolved))
    rendered = None if unresolved else _render_template(source, context)

    return TicketTemplatePreview(
        template=template,
        ticket=ticket,
        source_model=normalized_source_model,
        rendered_text=rendered,
        required_variables=tuple(required),
        resolved_variables=tuple(resolved),
        unresolved_variables=tuple(unresolved),
    )


__all__ = [
    "AI_TEMPLATES_CONTRACT_VERSION",
    "AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION",
    "AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION",
    "AITemplateContractError",
    "MAX_PREVIEW_REQUEST_BYTES",
    "MAX_GENERATE_TEMPLATE_REQUEST_BYTES",
    "MAX_IMPROVE_TEMPLATE_REQUEST_BYTES",
    "MAX_SUGGESTION_CONTEXT_BYTES",
    "MAX_SUGGESTION_REQUEST_BYTES",
    "MAX_SUGGESTION_SUBJECT_BYTES",
    "MAX_TEMPLATE_MUTATION_REQUEST_BYTES",
    "MAX_TEMPLATE_NAME_BYTES",
    "MAX_TEMPLATE_TEXT_BYTES",
    "MAX_TOP_N",
    "build_ticket_template_preview",
    "parse_bounded_json_object",
    "prime_bounded_request_stream",
    "validate_active_only",
    "validate_keywords",
    "validate_request_size",
    "validate_top_n",
    "validate_utf8_text",
]
