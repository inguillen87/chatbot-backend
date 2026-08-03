from __future__ import annotations

import math
import re
from string import Template
from typing import Any, Mapping

from models import MessageTemplateRegistry, NotificationTemplate
from services.message_templates import whatsapp_template_lifecycle


PROFESSIONAL_MESSAGE_PREVIEW_CONTRACT_VERSION = "professional_message_preview.v1"

_CONTEXT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_CONTENT_VARIABLE_KEY_RE = re.compile(r"^[1-9][0-9]{0,2}$")
_NUMBERED_VARIABLE_RE = re.compile(r"\{\{\s*([1-9][0-9]{0,2})\s*\}\}")
_ANY_DOUBLE_BRACE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_MAX_CONTEXT_VARIABLES = 100
_MAX_VALUE_BYTES = 2048
_MAX_BODY_BYTES_BY_CHANNEL = {
    "email": 100_000,
    "whatsapp": 4096,
    "push": 4096,
    "in_app": 16_384,
}


class ProfessionalMessageContractError(ValueError):
    """Stable, value-redacted validation error for message previews."""

    def __init__(
        self,
        code: str,
        *,
        field: str | None = None,
        variable_names: list[str] | None = None,
    ) -> None:
        self.code = str(code)
        self.field = field
        self.variable_names = sorted(set(variable_names or []))
        super().__init__(self.code)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": PROFESSIONAL_MESSAGE_PREVIEW_CONTRACT_VERSION,
            "error": self.code,
        }
        if self.field:
            payload["field"] = self.field
        if self.variable_names:
            # Only names are exposed. Context values can contain personal data
            # and must never be copied into logs or validation errors.
            payload["variable_names"] = list(self.variable_names)
        return payload


def _positive_tenant_id(value: Any) -> int:
    if isinstance(value, bool):
        raise ProfessionalMessageContractError("tenant_id_invalid", field="tenant_id")
    try:
        tenant_id = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProfessionalMessageContractError(
            "tenant_id_invalid", field="tenant_id"
        ) from exc
    if tenant_id <= 0:
        raise ProfessionalMessageContractError("tenant_id_invalid", field="tenant_id")
    return tenant_id


def _render_scalar(
    value: Any,
    *,
    field: str,
    error_code: str = "template_context_value_invalid",
) -> str:
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, int):
        rendered = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ProfessionalMessageContractError(
                error_code, field=field
            )
        rendered = str(value)
    elif isinstance(value, str):
        rendered = value
    else:
        raise ProfessionalMessageContractError(
            error_code, field=field
        )
    if not rendered.strip() or "\x00" in rendered or len(rendered.encode("utf-8")) > _MAX_VALUE_BYTES:
        raise ProfessionalMessageContractError(
            error_code, field=field
        )
    return rendered


def _normalize_named_context(context: Any) -> dict[str, str]:
    if context is None:
        return {}
    if not isinstance(context, Mapping):
        raise ProfessionalMessageContractError(
            "template_context_must_be_object", field="context"
        )
    if len(context) > _MAX_CONTEXT_VARIABLES:
        raise ProfessionalMessageContractError(
            "template_context_too_many_variables", field="context"
        )
    normalized: dict[str, str] = {}
    for raw_key, raw_value in context.items():
        key = str(raw_key or "").strip()
        if not _CONTEXT_KEY_RE.fullmatch(key):
            raise ProfessionalMessageContractError(
                "template_context_key_invalid", field="context"
            )
        normalized[key] = _render_scalar(raw_value, field=f"context.{key}")
    return normalized


def _template_identifiers(source: Any, *, field: str, required: bool) -> tuple[str, list[str]]:
    if source is None and not required:
        return "", []
    if not isinstance(source, str):
        raise ProfessionalMessageContractError(
            "template_source_must_be_string", field=field
        )
    rendered = source
    if required and not rendered.strip():
        raise ProfessionalMessageContractError(
            "template_source_required", field=field
        )
    identifiers: list[str] = []
    invalid = False
    for match in Template.pattern.finditer(rendered):
        named = match.group("named") or match.group("braced")
        if named:
            identifiers.append(named)
        elif match.group("invalid") is not None:
            invalid = True
    if invalid:
        raise ProfessionalMessageContractError(
            "template_placeholder_invalid", field=field
        )
    return rendered, sorted(set(identifiers))


def render_notification_template_strict(
    *,
    body_template: Any,
    subject_template: Any = None,
    context: Mapping[str, Any] | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """Render a local notification template with an exact variable contract.

    This intentionally uses ``Template.substitute`` after validating the exact
    key set. It is the fail-closed replacement for ``safe_substitute`` in queue
    and preview paths.
    """

    body_source, body_variables = _template_identifiers(
        body_template, field="body_template", required=True
    )
    subject_source, subject_variables = _template_identifiers(
        subject_template, field="subject_template", required=False
    )
    normalized_context = _normalize_named_context(context)
    required_variables = sorted(set(body_variables + subject_variables))
    received_variables = sorted(normalized_context)
    missing = sorted(set(required_variables) - set(received_variables))
    if missing:
        raise ProfessionalMessageContractError(
            "template_context_missing_variables",
            field="context",
            variable_names=missing,
        )
    extra = sorted(set(received_variables) - set(required_variables))
    if extra:
        raise ProfessionalMessageContractError(
            "template_context_unexpected_variables",
            field="context",
            variable_names=extra,
        )

    try:
        body = Template(body_source).substitute(normalized_context)
        subject = (
            Template(subject_source).substitute(normalized_context)
            if subject_template is not None
            else None
        )
    except (KeyError, ValueError) as exc:  # Defensive: contract checks should catch these.
        raise ProfessionalMessageContractError(
            "template_render_failed", field="context"
        ) from exc

    normalized_channel = str(channel or "").strip().lower()
    max_body_bytes = _MAX_BODY_BYTES_BY_CHANNEL.get(normalized_channel, 16_384)
    if len(body.encode("utf-8")) > max_body_bytes:
        raise ProfessionalMessageContractError(
            "rendered_body_too_long", field="body_template"
        )
    if subject is not None and len(subject.encode("utf-8")) > 255:
        raise ProfessionalMessageContractError(
            "rendered_subject_too_long", field="subject_template"
        )

    return {
        "body": body,
        "subject": subject,
        "required_variables": required_variables,
        "received_variables": received_variables,
        "strict_variable_contract": True,
        "contains_unresolved_variables": False,
    }


def _normalize_content_variables(values: Any) -> dict[str, str]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise ProfessionalMessageContractError(
            "content_variables_must_be_object", field="content_variables"
        )
    if len(values) > _MAX_CONTEXT_VARIABLES:
        raise ProfessionalMessageContractError(
            "content_variables_too_many", field="content_variables"
        )
    normalized: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key = str(raw_key or "").strip()
        if not _CONTENT_VARIABLE_KEY_RE.fullmatch(key):
            raise ProfessionalMessageContractError(
                "content_variable_key_invalid", field="content_variables"
            )
        normalized[key] = _render_scalar(
            raw_value,
            field=f"content_variables.{key}",
            error_code="content_variable_value_invalid",
        )
    return dict(sorted(normalized.items(), key=lambda item: int(item[0])))


def _walk_provider_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _walk_provider_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_provider_strings(nested)


def _provider_required_variables(*values: Any) -> list[str]:
    required: set[str] = set()
    for value in values:
        for source in _walk_provider_strings(value):
            tokens = _ANY_DOUBLE_BRACE_RE.findall(source)
            if any(not _CONTENT_VARIABLE_KEY_RE.fullmatch(token.strip()) for token in tokens):
                raise ProfessionalMessageContractError(
                    "provider_template_placeholder_invalid",
                    field="provider_template",
                )
            without_valid_variables = _NUMBERED_VARIABLE_RE.sub("", source)
            if "{{" in without_valid_variables or "}}" in without_valid_variables:
                raise ProfessionalMessageContractError(
                    "provider_template_placeholder_invalid",
                    field="provider_template",
                )
            required.update(_NUMBERED_VARIABLE_RE.findall(source))
    return sorted(required, key=int)


def _render_provider_value(value: Any, variables: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _NUMBERED_VARIABLE_RE.sub(
            lambda match: variables[match.group(1)], value
        )
    if isinstance(value, Mapping):
        return {
            str(key): _render_provider_value(nested, variables)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_render_provider_value(nested, variables) for nested in value]
    return value


def _provider_preview(
    registry: MessageTemplateRegistry,
    *,
    content_variables: Mapping[str, Any] | None,
) -> dict[str, Any]:
    components = registry.components
    if components is not None and not isinstance(components, (Mapping, list)):
        raise ProfessionalMessageContractError(
            "provider_template_components_invalid", field="provider_template"
        )
    body_preview = str(registry.body_preview or "")
    required_variables = _provider_required_variables(body_preview, components)
    normalized_variables = _normalize_content_variables(content_variables)
    received_variables = list(normalized_variables)
    missing = sorted(set(required_variables) - set(received_variables), key=int)
    if missing:
        raise ProfessionalMessageContractError(
            "content_variables_missing",
            field="content_variables",
            variable_names=missing,
        )
    extra = sorted(set(received_variables) - set(required_variables), key=int)
    if extra:
        raise ProfessionalMessageContractError(
            "content_variables_unexpected",
            field="content_variables",
            variable_names=extra,
        )
    lifecycle = whatsapp_template_lifecycle(
        registry.status,
        source="message_template_registry",
        provider_reference=registry.content_sid or registry.external_template_id,
        observed_at=registry.last_sync_at,
    )
    preview_available = bool(body_preview.strip() or components)
    return {
        "registry_id": registry.id,
        "provider": registry.provider,
        "name": registry.name,
        "language": registry.language,
        "category": registry.category,
        "content_sid_present": bool(str(registry.content_sid or "").strip()),
        "preview_available": preview_available,
        "required_content_variables": required_variables,
        "received_content_variables": received_variables,
        "rendered_body": _render_provider_value(body_preview, normalized_variables),
        "rendered_components": _render_provider_value(components, normalized_variables),
        "lifecycle": lifecycle,
    }


def preview_notification_template(
    *,
    tenant_id: Any,
    template_id: Any = None,
    key: Any = None,
    channel: Any = None,
    context: Mapping[str, Any] | None = None,
    content_variables: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a tenant-scoped, no-side-effect professional message preview."""

    normalized_tenant_id = _positive_tenant_id(tenant_id)
    has_id_selector = template_id not in (None, "")
    has_key_selector = key not in (None, "") or channel not in (None, "")
    if has_id_selector == has_key_selector:
        raise ProfessionalMessageContractError(
            "template_selector_invalid", field="template"
        )

    query = NotificationTemplate.query.filter_by(tenant_id=normalized_tenant_id)
    if has_id_selector:
        template = query.filter_by(id=str(template_id)).one_or_none()
    else:
        normalized_key = str(key or "").strip().lower()
        normalized_channel = str(channel or "").strip().lower()
        if not normalized_key or not normalized_channel:
            raise ProfessionalMessageContractError(
                "template_selector_invalid", field="template"
            )
        template = query.filter_by(
            key=normalized_key, channel=normalized_channel
        ).one_or_none()
    if template is None:
        # Cross-tenant IDs deliberately collapse to not-found.
        raise ProfessionalMessageContractError(
            "notification_template_not_found", field="template"
        )

    local_preview = render_notification_template_strict(
        body_template=template.body_template,
        subject_template=template.subject_template,
        context=context,
        channel=template.channel,
    )

    registry_preview = None
    blockers: list[str] = []
    if not bool(template.is_active):
        blockers.append("notification_template_inactive")
    registry_id = getattr(template, "message_template_registry_id", None)
    if registry_id is not None:
        registry = MessageTemplateRegistry.query.filter_by(
            id=registry_id,
            tenant_id=normalized_tenant_id,
            provider="twilio",
            channel="whatsapp",
        ).one_or_none()
        if registry is None or template.channel != "whatsapp":
            raise ProfessionalMessageContractError(
                "notification_template_registry_mismatch",
                field="message_template_registry_id",
            )
        registry_preview = _provider_preview(
            registry, content_variables=content_variables
        )
        blockers.extend(registry_preview["lifecycle"]["blockers"])
        if not registry_preview["content_sid_present"]:
            blockers.append("whatsapp_template_content_sid_missing")
        if not registry_preview["preview_available"]:
            blockers.append("provider_template_preview_unavailable")
    elif content_variables not in (None, {}):
        raise ProfessionalMessageContractError(
            "content_variables_require_template_registry",
            field="content_variables",
        )
    elif template.channel == "whatsapp":
        blockers.append("whatsapp_template_registry_not_linked")

    # A preview validates content only. Sender binding, consent, conversation
    # window and callback reachability are transport checks and remain outside
    # this contract, so it must never certify a production send.
    blockers.append("transport_readiness_not_checked")
    blockers = list(dict.fromkeys(blockers))
    return {
        "contract_version": PROFESSIONAL_MESSAGE_PREVIEW_CONTRACT_VERSION,
        "tenant_id": normalized_tenant_id,
        "template": {
            "id": template.id,
            "key": template.key,
            "channel": template.channel,
            "is_active": bool(template.is_active),
            "message_template_registry_id": registry_id,
        },
        "rendered": local_preview,
        "provider_template": registry_preview,
        "readiness": {
            "preview_valid": True,
            "provider_template_approval_valid": bool(
                registry_preview
                and registry_preview["lifecycle"]["production_send_allowed"]
            ),
            "provider_template_ready": bool(
                registry_preview
                and registry_preview["lifecycle"]["production_send_allowed"]
                and registry_preview["content_sid_present"]
                and registry_preview["preview_available"]
            ),
            "transport_readiness_checked": False,
            "production_send_allowed": False,
            "blockers": blockers,
        },
        "side_effects": {
            "provider_calls_performed": False,
            "messages_queued": 0,
            "messages_sent": 0,
        },
    }
