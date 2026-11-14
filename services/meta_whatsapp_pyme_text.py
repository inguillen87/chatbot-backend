"""Native business TEXT slice inside a claimed Meta unit; no provider or LLM I/O."""
from copy import deepcopy
from hashlib import sha256
import json
import re

from sqlalchemy.orm.attributes import flag_modified
from models import TenantConfig
from models_memory import InteractionEvent
from services.constants import CONTEXTO_MUNICIPIO
from services.crm_intelligence import record_meta_business_interaction
from services.whatsapp_conversation_turn import _resolve_whatsapp_menu_selection
from services.whatsapp_conversation_uow import ConversationUnitOfWork, ConversationUnitOfWorkError

CONTEXT_KEY = "contexto_pyme_v2"
STATE_KEY = "meta_native_text_v1"
STATE_CONTRACT = "pyme.meta_native_text_state.v1"
CONFIGURATION_REQUIRED = "Todavía no hay una presentación de servicios publicada. Podés dejar tu consulta por escrito."
INQUIRY_RECEIVED = "Recibimos tu consulta."
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_HEX = re.compile(r"[a-f0-9]{64}")
_MESSAGE_KEYS = {"welcome", "inquiry_received", "inquiry_continued", "service_unavailable"}


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and bool(value.strip())


def _valid_block(block):
    if (not isinstance(block, dict) or set(block) != {"contract_version", "status", "messages", "services"}
            or block["contract_version"] != "tenant.meta_native_text.v1" or block["status"] != "configured"):
        return False
    messages, services = block["messages"], block["services"]
    if (not isinstance(messages, dict) or set(messages) != _MESSAGE_KEYS
            or not all(_text(value, 2048) for value in messages.values())
            or not isinstance(services, list) or len(services) > 40):
        return False
    ids = []
    for item in services:
        if (not isinstance(item, dict) or set(item) != {"id", "title", "description"}
                or not isinstance(item["id"], str) or not _ID.fullmatch(item["id"])
                or not _text(item["title"], 80) or not _text(item["description"], 3000)):
            return False
        ids.append(item["id"])
    return len(ids) == len(set(ids))


def _read_menu(unit, tenant):
    # Match the existing panel/public API precedence, but never a rubro/file or
    # another tenant. Presence of an invalid WhatsApp row blocks inheritance.
    source, rows = "whatsapp", []
    with unit.session.no_autoflush:
        for channel in ("whatsapp", None):
            rows = (unit.session.query(TenantConfig).filter_by(tenant_id=tenant.id,
                key="menu", channel=channel).order_by(TenantConfig.id).limit(2).with_for_update().all())
            source = "whatsapp" if channel == "whatsapp" else "shared"
            if rows:
                break
    if not rows:
        return {"status": "configuration_required", "source": None, "revision": None}
    if len(rows) != 1:
        return {"status": "configuration_required", "source": source, "revision": None}
    row = rows[0]
    if row in unit.session.new or row in unit.session.dirty or row in unit.session.deleted:
        unit.fail("meta_pyme_config_scope_changed")
    config = row.json_value
    if (not isinstance(config, dict)
            or ("enabled" in config and type(config["enabled"]) is not bool)
            or ("status" in config and not isinstance(config["status"], str))
            or config.get("enabled") is False
            or config.get("status") in ("disabled", "configuration_required")
            or not _valid_block(config.get("meta_native_text"))):
        return {"status": "configuration_required", "source": source, "revision": None}
    block = deepcopy(config["meta_native_text"])
    revision = sha256(json.dumps({"tenant_id": tenant.id, "tenant_slug": tenant.slug,
        "source": source, "row_id": row.id, "block": block}, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    return {"status": "configured", "source": source, "revision": revision,
        "messages": block["messages"], "services": block["services"],
        "options": [{"id": item["id"], "texto": item["title"]} for item in block["services"]]}


def _read_state(unit, pyme):
    raw = pyme.get(STATE_KEY)
    if raw is None:
        return {"contract_version": STATE_CONTRACT, "active_inquiry": False,
            "last_completed_turn_id": None, "menu_revision_sha256": None,
            "menu_source": None, "displayed_options": [], "selected_service_id": None,
            "inquiry_event_ids": []}
    keys = {"contract_version", "active_inquiry", "last_completed_turn_id", "menu_revision_sha256",
        "menu_source", "displayed_options", "selected_service_id", "inquiry_event_ids"}
    if (not isinstance(raw, dict) or set(raw) != keys or raw["contract_version"] != STATE_CONTRACT
            or type(raw["active_inquiry"]) is not bool
            or (raw["last_completed_turn_id"] is not None and not _text(raw["last_completed_turn_id"], 128))
            or (raw["menu_revision_sha256"] is not None and
                (not isinstance(raw["menu_revision_sha256"], str) or not _HEX.fullmatch(raw["menu_revision_sha256"])))
            or raw["menu_source"] not in (None, "whatsapp", "shared")
            or (raw["selected_service_id"] is not None and
                (not isinstance(raw["selected_service_id"], str) or not _ID.fullmatch(raw["selected_service_id"])))):
        unit.fail("meta_pyme_context_invalid")
    options, refs = raw["displayed_options"], raw["inquiry_event_ids"]
    if (not isinstance(options, list) or len(options) > 40
            or any(not isinstance(item, dict) or set(item) != {"id", "texto"}
                or not isinstance(item["id"], str) or not _ID.fullmatch(item["id"])
                or not _text(item["texto"], 80) for item in options)
            or len({item["id"] for item in options}) != len(options)
            or not isinstance(refs, list) or len(refs) > 20
            or any(type(ref) is not int or ref <= 0 for ref in refs) or len(set(refs)) != len(refs)):
        unit.fail("meta_pyme_context_invalid")
    return deepcopy(raw)


def _require_supported_context(unit, data, pyme):
    from services.meta_whatsapp_survey import STATE_KEY as SURVEY_NAVIGATION_KEY
    from services.whatsapp_survey_conversation import WHATSAPP_SURVEY_FLOW_STATE_KEY
    municipal = data.get(CONTEXTO_MUNICIPIO)
    if municipal is None:
        municipal = data.get("contexto_municipio", {})
    if not isinstance(municipal, dict):
        unit.fail("meta_pyme_context_invalid")
    multimodal = pyme.get("multimodal_state_v1", {})
    if multimodal is None:
        multimodal = {}
    if not isinstance(multimodal, dict):
        unit.fail("meta_pyme_context_invalid")
    cart = multimodal.get("cart") or {}
    if not isinstance(cart, dict):
        unit.fail("meta_pyme_context_invalid")
    claim_flow = municipal.get("reclamo_flow_v2", {})
    if not isinstance(claim_flow, dict):
        unit.fail("meta_pyme_context_invalid")
    if (SURVEY_NAVIGATION_KEY in municipal or WHATSAPP_SURVEY_FLOW_STATE_KEY in municipal
            or claim_flow.get("state")
            or data.get("active_ticket_followup") or data.get("pending_sensitive_action") is not None
            or data.get("human_chat_in_progress") or data.get("room") or data.get("education_context")
            or pyme.get("vertical") in ("educacion", "colegio")
            or pyme.get("estado_conversacion") not in (None, "", "IDLE")
            or pyme.get("live_chat_ticket_id") or pyme.get("live_chat_socket_room")
            or cart.get("items") or multimodal.get("delivery") or multimodal.get("delivery_location_pending")):
        unit.fail("meta_pyme_flow_not_ported")


def _validate_references(unit, refs, tenant_id, contact_id):
    if not refs:
        return
    with unit.session.no_autoflush:
        found = {row[0] for row in unit.session.query(InteractionEvent.id).filter(
            InteractionEvent.id.in_(refs), InteractionEvent.tenant_id == tenant_id,
            InteractionEvent.contact_id == contact_id).all()}
    if found != set(refs):
        unit.fail("meta_pyme_event_reference_invalid")


def execute_pyme_meta_text_turn(turn, sink):
    """Execute the small structured business slice; never infer a sales intent."""
    from services.municipio_responder import SIMPLE_GREETINGS, RETURN_TO_MAIN_MENU_NORMALIZED, normalizar_texto
    unit = turn.unit_of_work
    if not isinstance(unit, ConversationUnitOfWork):
        raise ConversationUnitOfWorkError("meta_pyme_owner_required")
    unit.require_turn(turn)
    tenant = turn.tenant_profile
    if (tenant.tipo != "pyme" or tenant.pyme_id != turn.owner_user.id
            or turn.owner_user.tipo_chat != "pyme" or unit.claim.provider != "meta"):
        unit.fail("meta_pyme_scope_invalid")
    if unit.claim.message_kind != "text":
        unit.fail("meta_pyme_kind_unsupported")
    body = turn.message_body
    if not isinstance(body, str) or body != unit.claim.payload["input"]["text"]:
        unit.fail("meta_pyme_input_mismatch")
    original = turn.session_context.context_data
    if not isinstance(original, dict):
        unit.fail("meta_pyme_context_invalid")
    data = deepcopy(original)
    pyme = data.get(CONTEXT_KEY, {})
    if not isinstance(pyme, dict):
        unit.fail("meta_pyme_context_invalid")
    _require_supported_context(unit, data, pyme)
    state = _read_state(unit, pyme)
    menu = _read_menu(unit, tenant)
    configured = menu["status"] == "configured"
    selected, action = None, None
    try:
        selected, action = _resolve_whatsapp_menu_selection(body,
            {"last_options_sent": state["displayed_options"]})
    except (TypeError, ValueError):
        # Numeric-looking Unicode which int() rejects is still ordinary text.
        pass
    greeting = normalizar_texto(body).strip() in SIMPLE_GREETINGS | RETURN_TO_MAIN_MENU_NORMALIZED
    outputs = []
    if greeting:
        if configured:
            if (state["menu_revision_sha256"] != menu["revision"] or state["menu_source"] != menu["source"]
                    or state["displayed_options"] != menu["options"]):
                state["selected_service_id"] = None
            outputs.append(menu["messages"]["welcome"])
            if menu["options"]:
                outputs.append("\n".join(f"{index}. {item['texto']}" for index, item in enumerate(menu["options"], 1)))
            state.update(menu_revision_sha256=menu["revision"], menu_source=menu["source"],
                displayed_options=deepcopy(menu["options"]))
            status = "greeting"
        else:
            outputs.append(CONFIGURATION_REQUIRED)
            state.update(menu_revision_sha256=None, menu_source=None, displayed_options=[],
                selected_service_id=None)
            status = "configuration_required"
    elif selected is not None:
        if not configured:
            outputs.append(CONFIGURATION_REQUIRED)
            status = "configuration_required"
        elif (state["menu_revision_sha256"] != menu["revision"] or state["menu_source"] != menu["source"]
                or state["displayed_options"] != menu["options"]):
            outputs.append(menu["messages"]["service_unavailable"])
            status = "stale_selection"
        else:
            service = next((item for item in menu["services"] if item["id"] == action), None)
            if service is None:
                outputs.append(menu["messages"]["service_unavailable"])
                status = "stale_selection"
            else:
                outputs.append(service["title"] + "\n\n" + service["description"])
                state["selected_service_id"] = service["id"]
                status = "service_detail"
    else:
        if configured:
            key = "inquiry_continued" if state["active_inquiry"] else "inquiry_received"
            outputs.append(menu["messages"][key])
        else:
            outputs.append(INQUIRY_RECEIVED)
        state["active_inquiry"] = True
        status = "inquiry_received"
    unit.require_turn(turn)
    receipt = record_meta_business_interaction(unit_of_work=unit, conversation_turn_id=unit.claim.turn_id)
    _validate_references(unit, state["inquiry_event_ids"], tenant.id, receipt["contact_id"])
    if not receipt["created"]:
        # A persisted CRM effect alone does not prove that the source-bound
        # reply/context were finalized. Never complete with an empty bundle or
        # blindly resend; the durable turn requires explicit reconciliation.
        unit.fail("meta_pyme_crm_reconciliation_required")
    state["inquiry_event_ids"] = (state["inquiry_event_ids"] + [receipt["interaction_event_id"]])[-20:]
    state["last_completed_turn_id"] = unit.claim.turn_id
    pyme[STATE_KEY] = state
    data[CONTEXT_KEY] = pyme
    turn.session_context.context_data = data
    flag_modified(turn.session_context, "context_data")
    for output in outputs:
        sink.emit_text(output)
    unit.require_turn(turn)
    return {"status": status, "source": menu["source"], "revision": menu["revision"],
        "configuration_required": not configured, "interaction_event_id": receipt["interaction_event_id"],
        "duplicate": False}
