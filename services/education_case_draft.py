"""Scoped school intake drafts shared by widget and WhatsApp.

This module only accumulates supplied text and structured media metadata. It
does not classify language, interpret an attachment or create a ticket.
"""
from __future__ import annotations

from copy import deepcopy
import math
import time
import uuid
from typing import Any

from services.education_contracts import build_education_pending_case, education_prompt_for_intent


def education_case_scope(*, tenant: Any, owner: Any, actor: Any, anon_id: str | None,
                         session: Any, channel: str) -> dict[str, Any] | None:
    tenant_id = getattr(tenant, "id", None)
    owner_id = getattr(owner, "id", None)
    session_id = getattr(session, "chat_session_id", None)
    owner_field = "municipio_id" if getattr(tenant, "tipo", None) == "municipio" else "pyme_id"
    if (not tenant_id or not owner_id or not session_id or not getattr(tenant, "is_active", False)
            or getattr(tenant, owner_field, None) != owner_id
            or getattr(session, "tenant_id", None) != tenant_id):
        return None
    actor_id = getattr(actor, "id", None)
    if channel == "whatsapp" or not actor_id:
        if not anon_id or getattr(session, "anon_id", None) != anon_id:
            return None
    elif getattr(session, "user_id", None) != actor_id:
        return None
    return {"tenant_id": tenant_id, "owner_id": owner_id, "actor_id": actor_id,
            "anon_id": anon_id, "session_id": str(session_id), "channel": channel}


def _location(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    result = {}
    label = data.get("address") or data.get("label") or data.get("descripcion")
    if isinstance(label, str) and label.strip():
        result["address"] = label.strip()
    coordinates = []
    for keys, limit in ((("latitude", "lat"), 90), (("longitude", "lng", "lon"), 180)):
        value = next((data[key] for key in keys if data.get(key) is not None), None)
        try:
            number = float(value) if not isinstance(value, bool) else math.nan
        except (TypeError, ValueError, OverflowError):
            number = math.nan
        coordinates.append(number if math.isfinite(number) and abs(number) <= limit else None)
    if all(value is not None for value in coordinates):
        result.update(latitude=coordinates[0], longitude=coordinates[1])
    return result


def education_draft_description(case: dict[str, Any]) -> str:
    parts = list(case.get("details") or [])
    for attachment in case.get("attachments") or []:
        parts.append("Adjunto recibido: " + str(attachment.get("name") or attachment.get("mime_type") or "archivo"))
    location = case.get("location") or {}
    if location.get("address"):
        parts.append("Ubicación: " + location["address"])
    if location.get("latitude") is not None and location.get("longitude") is not None:
        parts.append(f"Ubicación compartida: {location['latitude']}, {location['longitude']}")
    return "\n".join(parts)


def education_attachment_matches_scope(attachment: Any, scope: dict[str, Any]) -> bool:
    """Recheck a saved attachment before associating it with a confirmed case."""
    if not attachment or getattr(attachment, "municipio_ticket_id", None) or getattr(attachment, "pyme_ticket_id", None):
        return False
    actor_id = scope.get("actor_id")
    user_id = getattr(attachment, "user_id", None)
    session_id = getattr(attachment, "session_id", None)
    if session_id == scope.get("session_id"):
        return user_id in {None, actor_id, scope.get("owner_id")}
    return bool(actor_id and user_id == actor_id and session_id is None)


def _payload(case: dict[str, Any], *, tenant: Any, prefix: str) -> dict[str, Any]:
    if case.get("status") == "registration_uncertain":
        return {"message_body": "No pude confirmar el resultado del envío anterior. Conservo los datos; consultá a secretaría antes de volver a enviarlos.",
                "fuente": prefix + "_case_uncertain", "message_type": "interactive_buttons",
                "options_list": [{"texto": "Hablar con secretaria", "action_id": "derivar_humano"}]}
    confirm = f"education_case_confirm:{case['_id']}:{case['_revision']}"
    edit = f"education_case_edit:{case['_id']}:{case['_revision']}"
    cancel = f"education_case_cancel:{case['_id']}:{case['_revision']}"
    has_detail = bool(case.get("details"))
    if case.get("status") == "awaiting_confirmation" and has_detail:
        body = (f"Revisá tu consulta: {case.get('label') or 'Consulta escolar'}.\n\n"
                + education_draft_description(case)
                + "\n\nTodavía no la envié. Confirmá para registrarla en el colegio.")
        options = [{"texto": "Confirmar caso", "action_id": confirm},
                   {"texto": "Editar detalle", "action_id": edit},
                   {"texto": "Cancelar", "action_id": cancel}]
        source = prefix + "_case_review"
    else:
        prompt = education_prompt_for_intent(case.get("intent"), tenant)
        if case.get("_editing"):
            body = "Escribí el detalle corregido. Conservo tus adjuntos y la ubicación del borrador."
        elif case.get("attachments") or case.get("location"):
            body = ("Conservo el adjunto o la ubicación que enviaste. "
                    "Necesito una breve descripción de tu consulta por texto o un audio con transcripción disponible.")
        else:
            body = prompt.get("message_body") or "Contame el detalle de tu consulta escolar."
        options = [{"texto": "Hablar con secretaria", "action_id": "derivar_humano"},
                   {"texto": "Cancelar", "action_id": cancel}]
        source = prefix + "_case_prompt"
    return {"message_body": body, "message_type": "interactive_buttons",
            "options_list": options, "fuente": source,
            "data": {"education_context": {"vertical": "educacion",
                     "tenant_slug": getattr(tenant, "slug", None),
                     "pending_case": {key: deepcopy(value) for key, value in case.items()
                                      if not key.startswith("_")}}}}


def advance_education_case_draft(*, context_data: dict[str, Any], scope: dict[str, Any] | None,
                                tenant: Any, intent: str | None, selected_option: dict[str, Any] | None,
                                action_id: str | None, message_body: str,
                                uploaded_file_info: dict[str, Any] | None,
                                location_info: dict[str, Any] | None,
                                prefix: str, additional_text: str | None = None) -> tuple[dict[str, Any] | None, bool, dict[str, Any] | None]:
    """Return (draft, explicitly_confirmed, response), without external effects."""
    stored = context_data.get("education_pending_case")
    action = str(action_id or "").strip()
    is_control = action.startswith(("education_case_confirm:", "education_case_edit:", "education_case_cancel:"))
    if not intent and not isinstance(stored, dict) and not is_control:
        return None, False, None
    if scope is None:
        context_data.pop("education_pending_case", None)
        return None, False, {"message_body": "No pude validar la sesión de esta consulta. Volvé a abrir el chat del colegio.",
                             "fuente": prefix + "_case_scope_invalid", "options_list": [], "message_type": "text"}
    now = time.time()
    if isinstance(stored, dict) and (stored.get("_scope") != scope
            or not isinstance(stored.get("created_at"), (int, float))
            or now - stored["created_at"] > 1800):
        context_data.pop("education_pending_case", None)
        stored = None
        if not intent:
            return None, False, {"message_body": "El borrador anterior ya no corresponde a esta sesión o venció. Indicá qué consulta querés iniciar.",
                                 "fuente": prefix + "_case_expired", "options_list": [], "message_type": "text"}
    if intent and (not isinstance(stored, dict) or stored.get("intent") != intent):
        stored = build_education_pending_case(intent, selected_option)
        stored.update(_scope=deepcopy(scope), _id=uuid.uuid4().hex, _revision=0,
                      details=[], attachments=[], location={})
    if not isinstance(stored, dict):
        return None, False, {"message_body": "Ese borrador ya terminó. Contame tu nueva consulta.",
                             "fuente": prefix + "_case_expired", "options_list": [], "message_type": "text"}
    case = deepcopy(stored)
    if is_control:
        expected_suffix = f":{case['_id']}:{case['_revision']}"
        if not action.endswith(expected_suffix):
            return case, False, _payload(case, tenant=tenant, prefix=prefix)
        if action.startswith("education_case_cancel:"):
            context_data.pop("education_pending_case", None)
            cancelled = ("Descarté el borrador local. Consultá a secretaría por el envío cuyo resultado no pude confirmar."
                         if case.get("status") == "registration_uncertain" else
                         "Cancelé el borrador de la consulta. No registré ningún caso.")
            return None, False, {"message_body": cancelled,
                                 "fuente": prefix + "_case_cancelled", "options_list": [], "message_type": "text"}
    if case.get("status") == "registration_uncertain":
        return case, False, _payload(case, tenant=tenant, prefix=prefix)
    if is_control:
        if action.startswith("education_case_edit:"):
            case.update(status="awaiting_detail", _editing=True)
            case["_revision"] += 1
            context_data["education_pending_case"] = case
            return case, False, _payload(case, tenant=tenant, prefix=prefix)

    ignored = {"", "__init__", action.casefold(), str(intent or "").casefold(),
               str(case.get("label") or "").casefold(), "confirmar caso", "editar detalle", "cancelar", "enviar detalle"}
    if action and str(message_body or "").strip().isdigit():
        ignored.add(str(message_body).strip())
    if selected_option:
        ignored.update(str(selected_option.get(key) or "").strip().casefold() for key in ("label", "texto"))
    values = [message_body, additional_text]
    media = uploaded_file_info if isinstance(uploaded_file_info, dict) else {}
    values.extend([media.get("caption"), media.get("transcribed_text")])
    new_details = []
    for value in values:
        if isinstance(value, str) and value.strip().casefold() not in ignored:
            text = value.strip()
            if text not in new_details:
                new_details.append(text)
    if case.get("_editing") and new_details:
        case["details"] = []
        case.pop("_editing", None)
    changed = False
    for text in new_details:
        if text not in case["details"]:
            case["details"].append(text)
            changed = True
    if media:
        attachment = {key: media[key] for key in ("id", "name", "mime_type") if media.get(key) is not None}
        if not attachment.get("name") and media.get("filename"):
            attachment["name"] = media["filename"]
        if attachment and attachment not in case["attachments"]:
            case["attachments"].append(attachment)
            changed = True
    location = _location(location_info)
    if location and location != case["location"]:
        case["location"] = location
        changed = True
    if changed:
        case["_revision"] += 1
    if (is_control and action.startswith("education_case_confirm:") and not changed
            and case.get("status") == "awaiting_confirmation" and case.get("details")):
        return case, True, None
    case["status"] = "awaiting_confirmation" if case.get("details") and not case.get("_editing") else "awaiting_detail"
    context_data["education_pending_case"] = case
    return case, False, _payload(case, tenant=tenant, prefix=prefix)
