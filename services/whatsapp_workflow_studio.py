from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from typing import Any, Mapping
from urllib.parse import quote


WORKFLOW_STUDIO_CONTRACT_VERSION = "whatsapp.workflow_studio.v1"
WORKFLOW_DRAFT_SCHEMA_VERSION = "whatsapp.workflow_draft.v1"
WORKFLOW_VALIDATION_CONTRACT_VERSION = "whatsapp.workflow_validation.v1"
WORKFLOW_SIMULATION_CONTRACT_VERSION = "whatsapp.workflow_simulation.v1"

MAX_DRAFT_BYTES = 64 * 1024
MAX_EVENT_BYTES = 16 * 1024
MAX_STEPS = 50

_SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
_SUPPORTED_TRIGGER_TYPES = ("inbound_message", "broadcast_reply")
_SUPPORTED_STEP_TYPES = ("branch", "reply_template", "create_ticket", "handoff", "stop")
_SUPPORTED_CONDITION_FIELDS = (
    "message.text",
    "message.type",
    "broadcast.id",
    "contact.language",
)
_SUPPORTED_OPERATORS = ("equals", "contains", "starts_with", "exists")
_SUPPORTED_MESSAGE_TYPES = (
    "text",
    "emoji",
    "image",
    "audio",
    "voice",
    "video",
    "document",
    "location",
    "sticker",
    "contact",
    "interactive",
    "call",
    "unsupported",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(*values: Any) -> str:
    material = "\n".join(_canonical_json(value) for value in values)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _tenant_ref(tenant_id: int, tenant_slug: str) -> dict[str, Any]:
    normalized_id = int(tenant_id)
    normalized_slug = str(tenant_slug or "").strip()
    if normalized_id < 1 or not normalized_slug:
        raise ValueError("tenant_scope_required")
    return {"id": normalized_id, "slug": normalized_slug}


def _issue(
    code: str,
    path: str,
    message: str,
    *,
    severity: str = "blocking",
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "path": path,
        "message": message,
        "severity": severity,
    }
    if details:
        payload["details"] = dict(details)
    return payload


def _publication_blockers(
    *,
    durable_control_plane_ready: bool = False,
    includes_broadcast_reply: bool = False,
) -> list[dict[str, Any]]:
    blockers = []
    if not durable_control_plane_ready:
        blockers.extend(
            [
                _issue(
                    "workflow_durable_storage_missing",
                    "publication",
                    "La persistencia durable no esta habilitada para este tenant.",
                ),
                _issue(
                    "workflow_version_ledger_missing",
                    "publication",
                    "El ledger inmutable de versiones no esta habilitado para este tenant.",
                ),
                _issue(
                    "workflow_publish_rollback_missing",
                    "publication",
                    "Publicar y volver a una version anterior permanecen deshabilitados.",
                ),
            ]
        )
    blockers.extend(
        [
        _issue(
            "workflow_runtime_binding_missing",
            "publication",
            "El runtime conversacional aun no consume este contrato de Workflow Studio.",
        ),
        ]
    )
    if includes_broadcast_reply:
        blockers.append(
            _issue(
                "broadcast_reply_binding_missing",
                "trigger.broadcast_id",
                "La asociacion runtime entre una respuesta de broadcast y un workflow aun no esta implementada.",
            )
        )
    return blockers


def build_workflow_studio_contract(
    *,
    tenant_id: int,
    tenant_slug: str,
    app_config: Mapping[str, Any] | None = None,
    plan_allowed: bool = True,
) -> dict[str, Any]:
    tenant = _tenant_ref(tenant_id, tenant_slug)
    # Local import avoids coupling the deterministic validator to persistence.
    from services.whatsapp_workflow_versioning import workflow_durable_control_plane_gate

    durable_gate = workflow_durable_control_plane_gate(
        app_config,
        tenant_id=tenant["id"],
        plan_allowed=plan_allowed,
    )
    durable_ready = durable_gate["available"] is True
    encoded_slug = quote(tenant["slug"], safe="")
    base_endpoint = f"/api/v2/tenants/{encoded_slug}/whatsapp/workflow-studio"
    blockers = _publication_blockers(
        durable_control_plane_ready=durable_ready,
        includes_broadcast_reply=True,
    )
    return {
        "contract_version": WORKFLOW_STUDIO_CONTRACT_VERSION,
        "tenant": tenant,
        "mode": "durable_control_plane" if durable_ready else "prepublication_read_only",
        "durable_control_plane_gate": durable_gate,
        "capabilities": {
            "draft": {
                "label": "Borrador durable" if durable_ready else "Borrador efimero",
                "available": True,
                "status": "ready" if durable_ready else "request_only",
                "persistent": durable_ready,
                "endpoint": f"{base_endpoint}/drafts" if durable_ready else None,
                "revision_endpoint_template": f"{base_endpoint}/workflows/{{workflow_id}}/drafts",
                "description": (
                    "Cada guardado agrega una revision inmutable por tenant."
                    if durable_ready
                    else "El borrador viaja en cada request y no se guarda."
                ),
            },
            "validate": {
                "label": "Validacion determinista",
                "available": True,
                "status": "ready",
                "endpoint": f"{base_endpoint}/validate",
                "method": "POST",
                "external_effects": False,
            },
            "simulate": {
                "label": "Simulacion determinista",
                "available": True,
                "status": "ready",
                "endpoint": f"{base_endpoint}/simulate",
                "method": "POST",
                "external_effects": False,
                "database_writes": False,
            },
            "versioning": {
                "label": "Versionado",
                "available": durable_ready,
                "status": "ready" if durable_ready else "blocked",
                "blocker": None if durable_ready else "workflow_version_ledger_missing",
                "history_policy": "append_only",
                "list_endpoint": f"{base_endpoint}/workflows",
                "detail_endpoint_template": f"{base_endpoint}/workflows/{{workflow_id}}",
            },
            "review": {
                "label": "Revision independiente",
                "available": durable_ready,
                "status": "ready" if durable_ready else "blocked",
                "blocker": None if durable_ready else "workflow_publish_rollback_missing",
                "endpoint_template": f"{base_endpoint}/workflows/{{workflow_id}}/reviews",
                "decision_contract": ["approved", "rejected"],
                "ordering": "monotonic_sequence_per_exact_subject",
                "latest_decision_required": True,
                "later_review_supersedes_prior_approval": True,
                "same_actor_publish_forbidden": True,
                "publish_reviewer_must_differ_from": ["draft_author", "publisher"],
                "rollback_reviewer_must_differ_from": ["publisher"],
            },
            "runtime": {
                "label": "Runtime conversacional",
                "available": False,
                "status": "blocked",
                "blocker": "workflow_runtime_binding_missing",
            },
            "publish": {
                "label": "Publicacion",
                "available": durable_ready,
                "status": "control_plane_only" if durable_ready else "blocked",
                "blocker": None if durable_ready else "workflow_publish_rollback_missing",
                "review_required": True,
                "separation_of_duties": True,
                "transactional_serialization": "immutable_first_draft_anchor",
                "revalidates_after_lock": [
                    "idempotency",
                    "latest_draft",
                    "latest_review",
                    "active_content_digest",
                ],
                "runtime_effect": False,
                "endpoint_template": f"{base_endpoint}/workflows/{{workflow_id}}/publish",
                "method": "POST",
            },
            "rollback": {
                "label": "Rollback",
                "available": durable_ready,
                "status": "control_plane_only" if durable_ready else "blocked",
                "blocker": None if durable_ready else "workflow_publish_rollback_missing",
                "creates_new_version": True,
                "mutates_history": False,
                "review_required": True,
                "transactional_serialization": "immutable_first_draft_anchor",
                "revalidates_after_lock": [
                    "idempotency",
                    "latest_review",
                    "active_content_digest",
                ],
                "endpoint_template": f"{base_endpoint}/workflows/{{workflow_id}}/rollback",
                "method": "POST",
            },
            "broadcast_reply_binding": {
                "label": "Respuestas de broadcast",
                "available": False,
                "status": "simulation_only",
                "blocker": "broadcast_reply_binding_missing",
            },
            "template_catalog": {
                "label": "Plantillas de workflows",
                "available": False,
                "status": "not_implemented",
                "blocker": "workflow_template_catalog_missing",
            },
            "import_export": {
                "label": "Importar y exportar",
                "available": False,
                "status": "not_implemented",
                "blocker": "workflow_import_export_missing",
            },
        },
        "draft_contract": {
            "schema_version": WORKFLOW_DRAFT_SCHEMA_VERSION,
            "max_bytes": MAX_DRAFT_BYTES,
            "max_steps": MAX_STEPS,
            "trigger_types": list(_SUPPORTED_TRIGGER_TYPES),
            "step_types": list(_SUPPORTED_STEP_TYPES),
            "condition_fields": list(_SUPPORTED_CONDITION_FIELDS),
            "operators": list(_SUPPORTED_OPERATORS),
            "message_types": list(_SUPPORTED_MESSAGE_TYPES),
            "tenant_scope_policy": "authenticated_tenant_only",
        },
        "publication_readiness": {
            "ready": durable_ready,
            "status": "control_plane_ready" if durable_ready else "blocked",
            "scope": "control_plane_only",
            "blockers": [] if durable_ready else blockers,
            "runtime_blockers": [
                blocker
                for blocker in blockers
                if blocker["code"]
                in {"workflow_runtime_binding_missing", "broadcast_reply_binding_missing"}
            ],
            "next_contract": "tenant-safe runtime binding with an independently verified activation smoke",
        },
        "side_effect_policy": {
            "validate": "none",
            "simulate": "proposed_effects_only",
            "publish": "database_control_plane_only" if durable_ready else "disabled",
            "rollback": "append_only_database_control_plane_only" if durable_ready else "disabled",
            "provider_calls": False,
            "messages_sent": False,
            "tickets_created": False,
            "handoffs_created": False,
        },
        "frontend_contract": {
            "render_as": "workflow_studio_readiness",
            "title": "Workflow Studio",
            "description": (
                "Guarda, revisa y versiona flujos por tenant sin conectarlos al runtime."
                if durable_ready
                else "Valida y simula flujos por tenant antes de habilitar persistencia o publicacion."
            ),
            "status_label": "Plano de control durable" if durable_ready else "Prepublicacion segura",
            "blockers_title": "Bloqueos reales para publicar",
            "safety_note": "Validar y simular no guarda datos, no llama proveedores y no ejecuta acciones.",
            "capability_order": [
                "draft",
                "validate",
                "simulate",
                "versioning",
                "review",
                "runtime",
                "publish",
                "rollback",
                "broadcast_reply_binding",
                "template_catalog",
                "import_export",
            ],
            "status_labels": {
                "ready": "Listo",
                "request_only": "Solo request",
                "blocked": "Bloqueado",
                "simulation_only": "Solo simulacion",
                "control_plane_only": "Solo plano de control",
                "not_implemented": "No implementado",
            },
        },
    }


def _normalized_text(
    value: Any,
    *,
    path: str,
    blockers: list[dict[str, Any]],
    required: bool = True,
    max_length: int = 120,
) -> str:
    if not isinstance(value, str):
        if required or value is not None:
            blockers.append(_issue("invalid_string", path, "Se esperaba un texto valido."))
        return ""
    normalized = value.strip()
    if required and not normalized:
        blockers.append(_issue("required_field", path, "Este campo es obligatorio."))
        return ""
    if len(normalized) > max_length:
        blockers.append(
            _issue(
                "string_too_long",
                path,
                f"El valor supera el maximo de {max_length} caracteres.",
            )
        )
        return normalized[:max_length]
    return normalized


def _scope_override_blockers(value: Mapping[str, Any], *, path: str) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for key in ("tenant_id", "tenant_slug", "tenant"):
        if key in value:
            blockers.append(
                _issue(
                    "tenant_scope_override_forbidden",
                    f"{path}.{key}",
                    "El tenant se toma exclusivamente de la identidad autenticada.",
                )
            )
    return blockers


def _unknown_field_blockers(
    value: Mapping[str, Any],
    *,
    allowed: set[str],
    path: str,
) -> list[dict[str, Any]]:
    unknown_all = [str(key)[:80] for key in value.keys() if key not in allowed]
    if not unknown_all:
        return []
    unknown = sorted(unknown_all)[:20]
    return [
        _issue(
            "unknown_fields_not_allowed",
            path,
            "El contrato es estricto; los campos desconocidos bloquean la operacion.",
            details={
                "fields": unknown,
                "total": len(unknown_all),
                "truncated": len(unknown_all) > len(unknown),
            },
        )
    ]


def _step_edges(step: Mapping[str, Any]) -> list[str]:
    if step.get("type") == "branch":
        return [str(step.get("on_true") or ""), str(step.get("on_false") or "")]
    next_step = str(step.get("next") or "")
    return [next_step] if next_step else []


def _graph_blockers(
    normalized_steps: list[dict[str, Any]],
    *,
    entry_step_id: str,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    step_by_id = {
        str(step.get("id")): step
        for step in normalized_steps
        if isinstance(step.get("id"), str) and step.get("id")
    }
    if entry_step_id and entry_step_id not in step_by_id:
        blockers.append(
            _issue(
                "entry_step_not_found",
                "draft.entry_step_id",
                "El paso inicial no existe en steps.",
            )
        )

    for index, step in enumerate(normalized_steps):
        for edge in _step_edges(step):
            if edge and edge not in step_by_id:
                blockers.append(
                    _issue(
                        "dangling_step_reference",
                        f"draft.steps[{index}]",
                        f"La referencia '{edge}' no apunta a un paso existente.",
                    )
                )

    if entry_step_id in step_by_id:
        reachable: set[str] = set()
        pending = deque([entry_step_id])
        while pending:
            step_id = pending.popleft()
            if step_id in reachable or step_id not in step_by_id:
                continue
            reachable.add(step_id)
            pending.extend(edge for edge in _step_edges(step_by_id[step_id]) if edge)
        unreachable = sorted(set(step_by_id) - reachable)
        if unreachable:
            blockers.append(
                _issue(
                    "unreachable_steps",
                    "draft.steps",
                    "Todos los pasos deben ser alcanzables desde entry_step_id.",
                    details={"step_ids": unreachable},
                )
            )

    colors: dict[str, int] = {}
    cycle_path: list[str] | None = None

    def visit(step_id: str, stack: list[str]) -> None:
        nonlocal cycle_path
        if cycle_path is not None or step_id not in step_by_id:
            return
        color = colors.get(step_id, 0)
        if color == 1:
            start = stack.index(step_id) if step_id in stack else 0
            cycle_path = [*stack[start:], step_id]
            return
        if color == 2:
            return
        colors[step_id] = 1
        for edge in _step_edges(step_by_id[step_id]):
            if edge:
                visit(edge, [*stack, step_id])
        colors[step_id] = 2

    for step_id in sorted(step_by_id):
        visit(step_id, [])
    if cycle_path:
        blockers.append(
            _issue(
                "workflow_cycle_not_allowed",
                "draft.steps",
                "La primera version del simulador exige un grafo aciclico.",
                details={"step_ids": cycle_path},
            )
        )
    return blockers


def validate_workflow_draft(
    draft: Any,
    *,
    tenant_id: int,
    tenant_slug: str,
    durable_control_plane_ready: bool = False,
) -> dict[str, Any]:
    tenant = _tenant_ref(tenant_id, tenant_slug)
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not isinstance(draft, Mapping):
        blockers.append(_issue("draft_required", "draft", "Se requiere un borrador JSON."))
        draft = {}
    else:
        blockers.extend(_scope_override_blockers(draft, path="draft"))
        blockers.extend(
            _unknown_field_blockers(
                draft,
                allowed={"schema_version", "name", "trigger", "entry_step_id", "steps"},
                path="draft",
            )
        )
        try:
            draft_size = len(_canonical_json(draft).encode("utf-8"))
        except (TypeError, ValueError):
            draft_size = MAX_DRAFT_BYTES + 1
        if draft_size > MAX_DRAFT_BYTES:
            blockers.append(
                _issue(
                    "draft_too_large",
                    "draft",
                    f"El borrador supera el maximo de {MAX_DRAFT_BYTES} bytes.",
                )
            )

    schema_version = _normalized_text(
        draft.get("schema_version"),
        path="draft.schema_version",
        blockers=blockers,
    )
    if schema_version and schema_version != WORKFLOW_DRAFT_SCHEMA_VERSION:
        blockers.append(
            _issue(
                "unsupported_schema_version",
                "draft.schema_version",
                f"La version soportada es {WORKFLOW_DRAFT_SCHEMA_VERSION}.",
            )
        )
    name = _normalized_text(draft.get("name"), path="draft.name", blockers=blockers)
    entry_step_id = _normalized_text(
        draft.get("entry_step_id"),
        path="draft.entry_step_id",
        blockers=blockers,
        max_length=64,
    )
    if entry_step_id and not _SAFE_ID.fullmatch(entry_step_id):
        blockers.append(
            _issue("invalid_step_id", "draft.entry_step_id", "El identificador inicial no es valido.")
        )

    trigger_raw = draft.get("trigger")
    if not isinstance(trigger_raw, Mapping):
        blockers.append(_issue("invalid_trigger", "draft.trigger", "Se requiere un trigger JSON."))
        trigger_raw = {}
    else:
        blockers.extend(
            _unknown_field_blockers(
                trigger_raw,
                allowed={"type", "broadcast_id"},
                path="draft.trigger",
            )
        )
    trigger_type = _normalized_text(
        trigger_raw.get("type"),
        path="draft.trigger.type",
        blockers=blockers,
        max_length=40,
    )
    if trigger_type and trigger_type not in _SUPPORTED_TRIGGER_TYPES:
        blockers.append(
            _issue(
                "unsupported_trigger_type",
                "draft.trigger.type",
                "El trigger no esta soportado por el simulador.",
            )
        )
    normalized_trigger: dict[str, Any] = {"type": trigger_type}
    if trigger_type == "broadcast_reply":
        broadcast_id = _normalized_text(
            trigger_raw.get("broadcast_id"),
            path="draft.trigger.broadcast_id",
            blockers=blockers,
        )
        if broadcast_id and not _SAFE_KEY.fullmatch(broadcast_id):
            blockers.append(
                _issue(
                    "invalid_broadcast_id",
                    "draft.trigger.broadcast_id",
                    "El broadcast_id contiene caracteres no permitidos.",
                )
            )
        normalized_trigger["broadcast_id"] = broadcast_id
        warnings.append(
            _issue(
                "broadcast_reply_simulation_only",
                "draft.trigger.broadcast_id",
                "La asociacion se puede simular, pero el runtime aun no la ejecuta.",
                severity="warning",
            )
        )
    elif "broadcast_id" in trigger_raw:
        blockers.append(
            _issue(
                "trigger_field_not_allowed",
                "draft.trigger.broadcast_id",
                "broadcast_id solo esta permitido para el trigger broadcast_reply.",
            )
        )

    steps_raw = draft.get("steps")
    if not isinstance(steps_raw, list):
        blockers.append(_issue("invalid_steps", "draft.steps", "steps debe ser una lista."))
        steps_raw = []
    elif not steps_raw:
        blockers.append(_issue("steps_required", "draft.steps", "Se requiere al menos un paso."))
    elif len(steps_raw) > MAX_STEPS:
        blockers.append(
            _issue(
                "too_many_steps",
                "draft.steps",
                f"El borrador supera el maximo de {MAX_STEPS} pasos.",
            )
        )

    normalized_steps: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, step_raw in enumerate(steps_raw[:MAX_STEPS]):
        path = f"draft.steps[{index}]"
        if not isinstance(step_raw, Mapping):
            blockers.append(_issue("invalid_step", path, "Cada paso debe ser un objeto JSON."))
            continue
        step_id = _normalized_text(
            step_raw.get("id"), path=f"{path}.id", blockers=blockers, max_length=64
        )
        step_type = _normalized_text(
            step_raw.get("type"), path=f"{path}.type", blockers=blockers, max_length=40
        )
        if step_id and not _SAFE_ID.fullmatch(step_id):
            blockers.append(_issue("invalid_step_id", f"{path}.id", "El identificador no es valido."))
        if step_id in seen_ids:
            blockers.append(_issue("duplicate_step_id", f"{path}.id", "El identificador esta repetido."))
        if step_id:
            seen_ids.add(step_id)
        if step_type and step_type not in _SUPPORTED_STEP_TYPES:
            blockers.append(
                _issue("unsupported_step_type", f"{path}.type", "El tipo de paso no esta soportado.")
            )
        allowed_step_fields = {
            "branch": {"id", "type", "condition", "on_true", "on_false"},
            "reply_template": {"id", "type", "template_key", "next"},
            "create_ticket": {"id", "type", "category", "next"},
            "handoff": {"id", "type", "queue", "next"},
            "stop": {"id", "type", "outcome"},
        }.get(step_type, {"id", "type"})
        blockers.extend(
            _unknown_field_blockers(
                step_raw,
                allowed=allowed_step_fields,
                path=path,
            )
        )
        normalized_step: dict[str, Any] = {"id": step_id, "type": step_type}

        if step_type == "branch":
            condition_raw = step_raw.get("condition")
            if not isinstance(condition_raw, Mapping):
                blockers.append(
                    _issue("invalid_condition", f"{path}.condition", "Se requiere una condicion JSON.")
                )
                condition_raw = {}
            else:
                blockers.extend(
                    _unknown_field_blockers(
                        condition_raw,
                        allowed={"field", "operator", "value"},
                        path=f"{path}.condition",
                    )
                )
            field = _normalized_text(
                condition_raw.get("field"),
                path=f"{path}.condition.field",
                blockers=blockers,
                max_length=40,
            )
            operator = _normalized_text(
                condition_raw.get("operator"),
                path=f"{path}.condition.operator",
                blockers=blockers,
                max_length=30,
            )
            if field and field not in _SUPPORTED_CONDITION_FIELDS:
                blockers.append(
                    _issue(
                        "unsupported_condition_field",
                        f"{path}.condition.field",
                        "El campo no esta habilitado para simulacion.",
                    )
                )
            if operator and operator not in _SUPPORTED_OPERATORS:
                blockers.append(
                    _issue(
                        "unsupported_condition_operator",
                        f"{path}.condition.operator",
                        "El operador no esta soportado.",
                    )
                )
            normalized_condition: dict[str, Any] = {"field": field, "operator": operator}
            if operator != "exists":
                normalized_condition["value"] = _normalized_text(
                    condition_raw.get("value"),
                    path=f"{path}.condition.value",
                    blockers=blockers,
                    max_length=500,
                )
            normalized_step["condition"] = normalized_condition
            normalized_step["on_true"] = _normalized_text(
                step_raw.get("on_true"),
                path=f"{path}.on_true",
                blockers=blockers,
                max_length=64,
            )
            normalized_step["on_false"] = _normalized_text(
                step_raw.get("on_false"),
                path=f"{path}.on_false",
                blockers=blockers,
                max_length=64,
            )
        elif step_type == "reply_template":
            template_key = _normalized_text(
                step_raw.get("template_key"),
                path=f"{path}.template_key",
                blockers=blockers,
            )
            if template_key and not _SAFE_KEY.fullmatch(template_key):
                blockers.append(
                    _issue(
                        "invalid_template_key",
                        f"{path}.template_key",
                        "La referencia de plantilla no es valida.",
                    )
                )
            normalized_step["template_key"] = template_key
        elif step_type == "create_ticket":
            normalized_step["category"] = _normalized_text(
                step_raw.get("category"),
                path=f"{path}.category",
                blockers=blockers,
            )
        elif step_type == "handoff":
            normalized_step["queue"] = _normalized_text(
                step_raw.get("queue"), path=f"{path}.queue", blockers=blockers
            )
        elif step_type == "stop":
            normalized_step["outcome"] = _normalized_text(
                step_raw.get("outcome"),
                path=f"{path}.outcome",
                blockers=blockers,
                required=False,
            ) or "completed"

        if step_type in {"reply_template", "create_ticket", "handoff"}:
            next_raw = step_raw.get("next")
            if next_raw is not None:
                normalized_step["next"] = _normalized_text(
                    next_raw,
                    path=f"{path}.next",
                    blockers=blockers,
                    max_length=64,
                )
        normalized_steps.append(normalized_step)

    blockers.extend(_graph_blockers(normalized_steps, entry_step_id=entry_step_id))
    normalized_draft = {
        "schema_version": schema_version or WORKFLOW_DRAFT_SCHEMA_VERSION,
        "name": name,
        "trigger": normalized_trigger,
        "entry_step_id": entry_step_id,
        "steps": normalized_steps,
    }
    draft_digest = _digest(normalized_draft)
    draft_id = f"draft_{_digest(tenant, draft_digest)[:24]}"
    publication_blockers = _publication_blockers(
        durable_control_plane_ready=durable_control_plane_ready,
        includes_broadcast_reply=trigger_type == "broadcast_reply"
    )
    valid = not blockers
    return {
        "contract_version": WORKFLOW_VALIDATION_CONTRACT_VERSION,
        "tenant": tenant,
        "valid": valid,
        "status": "valid_for_simulation" if valid else "blocked",
        "draft_id": draft_id,
        "draft_digest": draft_digest,
        "normalized_draft": normalized_draft,
        "blockers": blockers,
        "warnings": warnings,
        "simulation_allowed": valid,
        "publication": {
            "available": durable_control_plane_ready,
            "publishable": durable_control_plane_ready and valid,
            "status": (
                "control_plane_ready"
                if durable_control_plane_ready and valid
                else "blocked"
            ),
            "scope": "control_plane_only",
            "blockers": (
                [] if durable_control_plane_ready and valid else publication_blockers
            ),
            "runtime_blockers": [
                blocker
                for blocker in publication_blockers
                if blocker["code"]
                in {"workflow_runtime_binding_missing", "broadcast_reply_binding_missing"}
            ],
        },
        "side_effects": {
            "database_writes": 0,
            "provider_calls": 0,
            "messages_sent": 0,
            "tickets_created": 0,
            "handoffs_created": 0,
        },
    }


def _normalize_event(event: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    if not isinstance(event, Mapping):
        return {}, [_issue("event_required", "event", "Se requiere un evento JSON para simular.")]
    blockers.extend(_scope_override_blockers(event, path="event"))
    blockers.extend(
        _unknown_field_blockers(
            event,
            allowed={"message", "broadcast_id", "contact"},
            path="event",
        )
    )
    try:
        event_size = len(_canonical_json(event).encode("utf-8"))
    except (TypeError, ValueError):
        event_size = MAX_EVENT_BYTES + 1
    if event_size > MAX_EVENT_BYTES:
        blockers.append(
            _issue(
                "event_too_large",
                "event",
                f"El evento supera el maximo de {MAX_EVENT_BYTES} bytes.",
            )
        )
    message_raw = event.get("message")
    if not isinstance(message_raw, Mapping):
        blockers.append(_issue("invalid_message", "event.message", "Se requiere message como objeto JSON."))
        message_raw = {}
    else:
        blockers.extend(
            _unknown_field_blockers(
                message_raw,
                allowed={"type", "text"},
                path="event.message",
            )
        )
    message_type = _normalized_text(
        message_raw.get("type"),
        path="event.message.type",
        blockers=blockers,
        max_length=32,
    ).lower()
    if message_type and message_type not in _SUPPORTED_MESSAGE_TYPES:
        blockers.append(
            _issue(
                "unsupported_message_type",
                "event.message.type",
                "El tipo de mensaje no esta soportado por el simulador.",
            )
        )
    normalized: dict[str, Any] = {"message": {"type": message_type}}
    if "text" in message_raw:
        normalized["message"]["text"] = _normalized_text(
            message_raw.get("text"),
            path="event.message.text",
            blockers=blockers,
            required=False,
            max_length=2000,
        )
    broadcast_id = event.get("broadcast_id")
    if broadcast_id is not None:
        normalized_broadcast_id = _normalized_text(
            broadcast_id,
            path="event.broadcast_id",
            blockers=blockers,
            max_length=120,
        )
        if normalized_broadcast_id and not _SAFE_KEY.fullmatch(normalized_broadcast_id):
            blockers.append(
                _issue(
                    "invalid_broadcast_id",
                    "event.broadcast_id",
                    "El broadcast_id contiene caracteres no permitidos.",
                )
            )
        normalized["broadcast"] = {"id": normalized_broadcast_id}
    contact_raw = event.get("contact")
    if contact_raw is not None and not isinstance(contact_raw, Mapping):
        blockers.append(
            _issue("invalid_contact", "event.contact", "contact debe ser un objeto JSON.")
        )
    elif isinstance(contact_raw, Mapping) and "language" in contact_raw:
        blockers.extend(
            _unknown_field_blockers(
                contact_raw,
                allowed={"language"},
                path="event.contact",
            )
        )
        normalized["contact"] = {
            "language": _normalized_text(
                contact_raw.get("language"),
                path="event.contact.language",
                blockers=blockers,
                required=False,
                max_length=16,
            ).lower()
        }
    elif isinstance(contact_raw, Mapping):
        blockers.extend(
            _unknown_field_blockers(
                contact_raw,
                allowed={"language"},
                path="event.contact",
            )
        )
    return normalized, blockers


def _event_value(event: Mapping[str, Any], dotted_path: str) -> Any:
    value: Any = event
    for part in dotted_path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _condition_matches(condition: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    observed = _event_value(event, str(condition.get("field") or ""))
    operator = str(condition.get("operator") or "")
    if operator == "exists":
        return observed is not None and str(observed).strip() != ""
    actual = str(observed or "").casefold()
    expected = str(condition.get("value") or "").casefold()
    if operator == "equals":
        return actual == expected
    if operator == "contains":
        return expected in actual
    if operator == "starts_with":
        return actual.startswith(expected)
    return False


def simulate_workflow_draft(
    draft: Any,
    event: Any,
    *,
    tenant_id: int,
    tenant_slug: str,
    durable_control_plane_ready: bool = False,
) -> dict[str, Any]:
    validation = validate_workflow_draft(
        draft,
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        durable_control_plane_ready=durable_control_plane_ready,
    )
    tenant = validation["tenant"]
    normalized_event, event_blockers = _normalize_event(event)
    simulation_id = f"sim_{_digest(tenant, validation['draft_digest'], normalized_event)[:24]}"
    base = {
        "contract_version": WORKFLOW_SIMULATION_CONTRACT_VERSION,
        "tenant": tenant,
        "simulation_id": simulation_id,
        "draft_id": validation["draft_id"],
        "deterministic": True,
        "determinism_scope": "authenticated_tenant_plus_normalized_draft_plus_normalized_event",
        "request_id_excluded_from_determinism": True,
        "validation": {
            "valid": validation["valid"],
            "blockers": validation["blockers"],
            "warnings": validation["warnings"],
        },
        "publication": validation["publication"],
        "side_effects": {
            "mode": "proposed_only",
            "database_writes": 0,
            "provider_calls": 0,
            "messages_sent": 0,
            "tickets_created": 0,
            "handoffs_created": 0,
        },
    }
    blockers = [*validation["blockers"], *event_blockers]
    if blockers:
        return {
            **base,
            "status": "blocked",
            "blockers": blockers,
            "trigger": {"matched": False, "reason": "simulation_input_invalid"},
            "trace": [],
            "proposed_effects": [],
        }

    normalized_draft = validation["normalized_draft"]
    trigger = normalized_draft["trigger"]
    trigger_type = trigger["type"]
    trigger_matches = trigger_type == "inbound_message"
    trigger_reason = "inbound_message_matched"
    if trigger_type == "broadcast_reply":
        expected_broadcast = str(trigger.get("broadcast_id") or "")
        observed_broadcast = str(_event_value(normalized_event, "broadcast.id") or "")
        trigger_matches = bool(expected_broadcast and expected_broadcast == observed_broadcast)
        trigger_reason = (
            "broadcast_reply_matched" if trigger_matches else "broadcast_reply_not_matched"
        )
    if not trigger_matches:
        return {
            **base,
            "status": "not_triggered",
            "blockers": [],
            "trigger": {"matched": False, "reason": trigger_reason},
            "trace": [],
            "proposed_effects": [],
        }

    step_by_id = {step["id"]: step for step in normalized_draft["steps"]}
    current_step_id = normalized_draft["entry_step_id"]
    trace: list[dict[str, Any]] = []
    proposed_effects: list[dict[str, Any]] = []
    outcome = "completed"
    for _ in range(MAX_STEPS + 1):
        step = step_by_id[current_step_id]
        step_type = step["type"]
        trace_item: dict[str, Any] = {"step_id": current_step_id, "type": step_type}
        next_step_id = ""
        if step_type == "branch":
            matched = _condition_matches(step["condition"], normalized_event)
            next_step_id = step["on_true"] if matched else step["on_false"]
            trace_item.update({"matched": matched, "next_step_id": next_step_id})
        elif step_type == "reply_template":
            proposed_effects.append(
                {
                    "type": "reply_template",
                    "template_key": step["template_key"],
                    "execution": "proposed_only",
                }
            )
            next_step_id = str(step.get("next") or "")
            trace_item["next_step_id"] = next_step_id or None
        elif step_type == "create_ticket":
            proposed_effects.append(
                {
                    "type": "create_ticket",
                    "category": step["category"],
                    "execution": "proposed_only",
                }
            )
            next_step_id = str(step.get("next") or "")
            trace_item["next_step_id"] = next_step_id or None
        elif step_type == "handoff":
            proposed_effects.append(
                {
                    "type": "handoff",
                    "queue": step["queue"],
                    "execution": "proposed_only",
                }
            )
            next_step_id = str(step.get("next") or "")
            trace_item["next_step_id"] = next_step_id or None
        elif step_type == "stop":
            outcome = str(step.get("outcome") or "completed")
            trace_item["outcome"] = outcome
        trace.append(trace_item)
        if not next_step_id:
            break
        current_step_id = next_step_id

    return {
        **base,
        "status": "completed",
        "outcome": outcome,
        "blockers": [],
        "trigger": {"matched": True, "reason": trigger_reason},
        "trace": trace,
        "proposed_effects": proposed_effects,
    }
